"""Langfuse telemetry wrapper.

All telemetry is OPTIONAL. If `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
are not set in the environment, `init_langfuse()` returns None and every
helper here becomes a silent no-op. Pipeline code calls these helpers
unconditionally; they simply do nothing when the client is absent.

Every call is wrapped in try/except so that a telemetry bug can never
crash the voice/vision loop.

Usage pattern (from session.py):

    from app.telemetry import (
        init_langfuse, start_turn_trace, end_turn_trace,
        observe_stt, observe_claude, observe_tts, observe_vision,
        log_session_summary, flush,
    )

    lf = init_langfuse()  # once at startup (or on first WS connect)

    # Per user turn:
    trace = start_turn_trace(lf, session_id, turn_num, transcript, vision_context)
    observe_stt(trace, audio_bytes_len, transcript, latency_ms)
    observe_claude(trace, messages, response_text, usage, latency_ms, first_token_ms)
    observe_tts(trace, sentence, audio_bytes_len, latency_ms)
    end_turn_trace(trace, response_text, was_interrupted, total_ms)

    # At WS disconnect:
    log_session_summary(lf, session_id, stats)
    flush(lf)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

log = logging.getLogger("abide.telemetry")

# Import langfuse lazily/defensively so a missing-or-broken install
# cannot stop the app from booting.
try:
    from langfuse import Langfuse  # type: ignore
    _LANGFUSE_AVAILABLE = True
except Exception as e:  # pragma: no cover
    Langfuse = None  # type: ignore
    _LANGFUSE_AVAILABLE = False
    log.info("Langfuse package not importable (%s). Telemetry disabled.", e)


_client: Any = None


_resolved_host: str = ""  # remembered for the async auth verification


def init_langfuse() -> Any:
    """Create a Langfuse client from env vars, or return None.

    Env vars:
        LANGFUSE_PUBLIC_KEY   — required
        LANGFUSE_SECRET_KEY   — required
        LANGFUSE_HOST         — optional, default https://cloud.langfuse.com
        LANGFUSE_BASE_URL     — accepted as an alias for LANGFUSE_HOST (some
                                 .env files use this name)

    Returns the client on success, or None if any key is missing or
    client creation fails. Never raises. Prints a clear one-line status
    to the server log so the operator can tell at a glance whether
    telemetry is live.

    NOTE: credential verification (`auth_check()`) is NOT done here —
    it's a synchronous network call that could block startup for up to
    ~30s on a slow or unreachable Langfuse endpoint. Call
    `verify_langfuse_async()` from an async context (e.g. the FastAPI
    startup hook wrapped in asyncio.create_task) to run the check with
    a bounded timeout.
    """
    global _client, _resolved_host
    if _client is not None:
        return _client
    if not _LANGFUSE_AVAILABLE:
        print("Langfuse: disabled (package not importable)", flush=True)
        log.info("Langfuse: disabled (package not importable)")
        return None

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    # Accept both LANGFUSE_HOST and LANGFUSE_BASE_URL; fall back to cloud.
    host = (
        os.environ.get("LANGFUSE_HOST", "").strip()
        or os.environ.get("LANGFUSE_BASE_URL", "").strip()
        or "https://cloud.langfuse.com"
    )
    # Strip surrounding quotes if the .env quoted the value.
    host = host.strip('"').strip("'")

    if not public_key or not secret_key:
        print("Langfuse: disabled (no keys)", flush=True)
        log.info("Langfuse: disabled (no keys in env)")
        return None

    try:
        _client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
    except Exception as e:
        # Truncate exception message to defensively avoid echoing any
        # credential fragment that might appear in a malformed URL error.
        print(f"Langfuse: disabled (init failed: {type(e).__name__})", flush=True)
        log.warning("Langfuse init failed (telemetry disabled): %s", type(e).__name__)
        _client = None
        return None

    _resolved_host = host
    print(f"Langfuse: initialized (host={host}) — verifying credentials in background", flush=True)
    log.info("Langfuse: initialized (host=%s)", host)
    return _client


async def verify_langfuse_async(timeout: float = 3.0) -> None:
    """Run auth_check() in a background thread with a bounded timeout.

    Designed to be called fire-and-forget from the FastAPI startup hook:
        asyncio.create_task(telemetry.verify_langfuse_async())

    Prints the final status to the server log. Never raises. If the
    client wasn't initialized or the check times out, logs a warning
    and lets the regular background sender keep retrying in the SDK.
    """
    global _client
    if _client is None:
        return
    try:
        # auth_check() is a sync method; wrap in a thread and bound it.
        auth_ok = await asyncio.wait_for(
            asyncio.to_thread(_client.auth_check),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        print(
            f"Langfuse: auth_check timed out after {timeout}s "
            f"(traces will still be sent in background)",
            flush=True,
        )
        log.warning("Langfuse auth_check timed out after %.1fs", timeout)
        return
    except Exception as e:
        log.warning(
            "Langfuse auth_check failed (will still send traces): %s",
            type(e).__name__,
        )
        return

    if bool(auth_ok):
        print(f"Langfuse: connected (host={_resolved_host})", flush=True)
        log.info("Langfuse: connected (host=%s)", _resolved_host)
    else:
        print(
            f"Langfuse: keys rejected by {_resolved_host} — traces will be dropped",
            flush=True,
        )
        log.warning("Langfuse: keys rejected by %s", _resolved_host)


def _safe(fn):
    """Decorator: swallow any exception so telemetry errors never propagate."""

    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.debug("Telemetry no-op (%s): %s", fn.__name__, e)
            return None

    wrapper.__name__ = fn.__name__
    return wrapper


# ─── Per-turn trace lifecycle ────────────────────────────────────────────────

@_safe
def start_turn_trace(
    lf: Any,
    session_id: str,
    turn_number: int,
    transcript: str,
    vision_context: str = "",
) -> Any:
    """Create a top-level trace for one conversation turn. Returns a handle
    (opaque to callers) or None.
    """
    if lf is None:
        return None
    trace = lf.trace(
        name=f"turn-{turn_number}",
        session_id=session_id,
        input={"transcript": transcript},
        metadata={
            "turn_number": turn_number,
            "vision_context": vision_context or None,
        },
        tags=["conversation-turn"],
    )
    return trace


@_safe
def end_turn_trace(
    trace: Any,
    response_text: str,
    was_interrupted: bool,
    total_ms: float,
) -> None:
    """Finalize the turn trace with output and summary metadata."""
    if trace is None:
        return
    trace.update(
        output={"response": response_text},
        metadata={
            "was_interrupted": was_interrupted,
            "total_latency_ms": round(total_ms, 1),
        },
    )


# ─── Per-stage spans and generations ─────────────────────────────────────────

@_safe
def observe_stt(
    trace: Any,
    audio_bytes: int,
    transcript: str,
    latency_ms: float,
) -> None:
    """Attach an STT generation to the turn trace.

    Uses SECONDS as the usage unit — Groq Whisper bills by audio duration.
    16kHz 16-bit mono PCM = 32 000 bytes/s, so we derive seconds from bytes.
    """
    if trace is None:
        return
    audio_seconds = round(audio_bytes / 32_000, 2)
    gen = trace.generation(
        name="stt-groq-whisper",
        model="whisper-large-v3",
        input={"audio_seconds": audio_seconds},
        output={"transcript": transcript},
        usage={"input": audio_seconds, "unit": "SECONDS"},
        metadata={"latency_ms": round(latency_ms, 1)},
    )
    try:
        gen.end()
    except Exception:
        pass


@_safe
def observe_claude(
    trace: Any,
    model: str,
    messages: list[dict],
    response_text: str,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: float,
    first_token_ms: float | None,
) -> None:
    """Attach a Claude generation to the turn trace with token usage."""
    if trace is None:
        return
    usage: dict[str, Any] = {}
    if input_tokens is not None:
        usage["input"] = input_tokens
    if output_tokens is not None:
        usage["output"] = output_tokens
    gen = trace.generation(
        name="claude-response",
        model=model,
        input=messages,
        output=response_text,
        usage=usage or None,
        metadata={
            "latency_ms": round(latency_ms, 1),
            "first_token_ms": round(first_token_ms, 1) if first_token_ms else None,
        },
    )
    try:
        gen.end()
    except Exception:
        pass


@_safe
def observe_tts(
    trace: Any,
    sentence: str,
    audio_bytes: int,
    latency_ms: float,
) -> None:
    """Attach one TTS generation to the turn trace (one per synthesized sentence).

    Uses CHARACTERS as the usage unit — OpenAI TTS bills per 1M characters.
    """
    if trace is None:
        return
    gen = trace.generation(
        name="tts-openai",
        model="tts-1",
        input={"text": sentence},
        output={"audio_bytes": audio_bytes},
        usage={"input": len(sentence), "unit": "CHARACTERS"},
        metadata={"latency_ms": round(latency_ms, 1), "voice": "nova"},
    )
    try:
        gen.end()
    except Exception:
        pass


@_safe
def observe_vision(
    lf: Any,
    session_id: str,
    num_frames: int,
    image_bytes: int,
    activity: str,
    bbox: list[float] | None,
    latency_ms: float,
    is_fall: bool,
) -> None:
    """Log a standalone top-level generation for one vision call.

    Vision runs in a background task orthogonal to the conversation-turn
    lifecycle, so each vision call gets its own short trace rather than
    being attached to a turn.
    """
    if lf is None:
        return
    trace = lf.trace(
        name="vision-call",
        session_id=session_id,
        input={"num_frames": num_frames, "image_bytes": image_bytes},
        output={"activity": activity, "bbox": bbox},
        metadata={"latency_ms": round(latency_ms, 1), "is_fall": is_fall},
        tags=["vision"] + (["fall"] if is_fall else []),
    )
    gen = trace.generation(
        name="vision-gpt4.1-mini",
        model="gpt-4.1-mini",
        input={"num_frames": num_frames, "image_bytes": image_bytes},
        output={"activity": activity, "bbox": bbox},
        metadata={"latency_ms": round(latency_ms, 1), "is_fall": is_fall},
    )
    try:
        gen.end()
    except Exception:
        pass
    return trace


# ─── Session-level summary ───────────────────────────────────────────────────

@_safe
def log_session_summary(
    lf: Any,
    session_id: str,
    stats: dict,
) -> None:
    """Create a top-level trace summarizing the whole session."""
    if lf is None:
        return
    lf.trace(
        name="session-summary",
        session_id=session_id,
        output=stats,
        metadata=stats,
        tags=["session-summary"],
    )


@_safe
def flush(lf: Any) -> None:
    """Force the client to flush queued events. Call before process exit
    (or on WS disconnect) so short-lived sessions' traces are not lost.
    Langfuse v2's background thread normally flushes every ~0.5s, but an
    explicit flush on session boundaries is the safety net.
    """
    if lf is None:
        return
    try:
        lf.flush()
        log.debug("Langfuse: flush() completed")
    except Exception as e:
        log.debug("Langfuse flush failed: %s", e)


# ─── Convenience: monotonic timer helper ─────────────────────────────────────

class Timer:
    """Tiny context manager: `with Timer() as t: ...` then read `t.ms`."""

    def __enter__(self):
        self._start = time.monotonic()
        self.ms: float = 0.0
        return self

    def __exit__(self, *exc):
        self.ms = (time.monotonic() - self._start) * 1000.0
        return False
