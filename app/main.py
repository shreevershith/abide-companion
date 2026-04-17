"""Phase 7: FastAPI with VAD + STT + Claude + TTS + Barge-in + Vision + Langfuse telemetry."""

import base64
import json
import logging
import time
import uuid

import asyncio
import httpx
import numpy as np
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketState
from pathlib import Path

# Load .env BEFORE importing app modules so os.environ has Langfuse
# credentials available when app.telemetry is imported. We ONLY load
# from the explicit project-root path — no CWD fallback. Loading from
# CWD would let an attacker who controls the working directory swap
# in a malicious .env and exfiltrate any secrets written there.
from dotenv import load_dotenv
_PROJECT_ROOT = Path(__file__).parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"
_DOTENV_LOADED = False
try:
    if _DOTENV_PATH.exists():
        _DOTENV_LOADED = load_dotenv(_DOTENV_PATH, override=False)
except Exception as _dotenv_exc:
    # Malformed .env file — log and keep going, relying on process env vars.
    # We deliberately don't expose _dotenv_exc details in case the parser
    # echoes any part of the file contents in its error message.
    _DOTENV_LOADED = False
    _DOTENV_LOAD_ERROR: Exception | None = _dotenv_exc
else:
    _DOTENV_LOAD_ERROR = None

from app.audio import AudioProcessor, transcribe
from app.conversation import ConversationEngine
from app.session import Session
from app.tts import prewarm as tts_prewarm, prewarm_cache as tts_prewarm_cache
from app.vision import prewarm as vision_prewarm
from app import telemetry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("abide")

if _DOTENV_LOADED:
    log.info(".env loaded from %s", _DOTENV_PATH)
elif _DOTENV_LOAD_ERROR is not None:
    log.warning(
        ".env parse failed (%s) — relying on process env vars",
        type(_DOTENV_LOAD_ERROR).__name__,
    )
elif not _DOTENV_PATH.exists():
    log.info(".env not found at %s — relying on process env vars", _DOTENV_PATH)
else:
    log.info(".env at %s produced no values", _DOTENV_PATH)

app = FastAPI(title="Abide Companion")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


@app.on_event("startup")
async def _on_startup() -> None:
    """Initialize Langfuse once at process start so the 'connected' /
    'disabled' banner shows up immediately in the uvicorn log, not on
    first client connect. Subsequent init_langfuse() calls are cached.

    Wrapped in try/except so a telemetry bug never prevents uvicorn from
    accepting connections. Credential verification (auth_check) runs as
    a separate bounded-timeout background task so a slow Langfuse
    endpoint can't delay server startup.
    """
    try:
        lf = telemetry.init_langfuse()
    except Exception as e:
        log.warning("Startup telemetry init failed (non-fatal): %s", type(e).__name__)
        return
    if lf is not None:
        # Fire-and-forget credential check. verify_langfuse_async logs
        # the final status; we just need to kick it off without awaiting.
        t = asyncio.create_task(telemetry.verify_langfuse_async(timeout=3.0))
        t.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    """Flush Langfuse traces and close persistent HTTP clients."""
    try:
        telemetry.flush(telemetry.init_langfuse())
    except Exception:
        pass
    # Close all module-level persistent httpx clients
    from app.tts import aclose as tts_close
    from app.vision import aclose as vision_close
    for name, closer in [("analyze", _analyze_client.aclose), ("tts", tts_close), ("vision", vision_close)]:
        try:
            await closer()
        except Exception:
            pass


# ── Session analysis endpoint ──
# One-shot Claude call at session end to review what Abide got right/wrong.
# Frontend cannot call the Anthropic API directly (CORS blocked), so this
# lightweight proxy accepts conversation history + activity log and returns
# a structured analysis string.

# Persistent HTTP/2 client for the analysis endpoint. Module-level so we
# don't pay TCP+TLS handshake per call (~300-500ms). Reused across sessions.
_analyze_client = httpx.AsyncClient(timeout=30.0, http2=True)

ANALYZE_PROMPT = (
    "Review this conversation between Abide (an AI elderly care companion) and a user. "
    "Also consider the activity log showing what Abide's vision system observed.\n\n"
    "Identify:\n"
    "1. Activities or interpretations Abide got correct "
    "(user confirmed or did not correct)\n"
    "2. Activities or interpretations Abide got wrong "
    "(user corrected with phrases like 'no', 'that's wrong', "
    "'actually I was', 'you misunderstood')\n"
    "3. Anything ambiguous or unclear\n\n"
    "Return a short structured summary with two sections: "
    "Correct Interpretations and Corrections Made. "
    "Keep it brief — 3-5 bullet points each maximum."
)


@app.post("/api/analyze")
async def analyze_session(request: Request) -> JSONResponse:
    """Analyze a completed session's conversation for correctness."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    history = body.get("history", [])
    activity_log = body.get("activity_log", "")
    api_key = body.get("api_key", "")

    if not api_key:
        return JSONResponse({"error": "Missing API key"}, status_code=400)
    if not history:
        return JSONResponse({"analysis": "No conversation to analyze."})

    # Build a single user message with the full context for analysis
    conversation_text = "\n".join(
        f"{'User' if m.get('role') == 'user' else 'Abide'}: {m.get('content', '')}"
        for m in history
    )
    user_content = f"Conversation:\n{conversation_text}"
    if activity_log:
        user_content += f"\n\nActivity Log (vision observations):\n{activity_log}"

    try:
        resp = await _analyze_client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 500,
                "system": ANALYZE_PROMPT,
                "messages": [{"role": "user", "content": user_content}],
            },
        )
        if resp.status_code != 200:
            log.error("Analysis API error %d: %s", resp.status_code, resp.text[:300])
            return JSONResponse({"error": "Claude API error"}, status_code=502)

        data = resp.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return JSONResponse({"analysis": text.strip()})

    except Exception as e:
        log.error("Analysis request failed: %s", e)
        return JSONResponse({"error": "Analysis failed"}, status_code=500)


# Echo suppression tunables
POST_TTS_COOLDOWN_MS = 300   # ignore VAD this long after last TTS chunk sent
SUSTAINED_SPEECH_MS = 400    # require this much continuous speech before firing barge-in
# Also require the speech-in-progress to have a real voice-level RMS.
# TTS echo leaking through the mic is around ~0.005; real speech is ~0.03+.
# Matches MIN_SPEECH_RMS in audio.py (the post-hoc quiet-segment filter).
BARGE_IN_MIN_RMS = 0.015

# Defensive limits on client-supplied payloads
MAX_FRAME_B64_CHARS = 500_000  # ~350 KB decoded — well above our 512x384 JPEGs (~15-40 KB)
MAX_AUDIO_CHUNK_BYTES = 32_768  # 2048 float32 samples = 8192 bytes; 4x headroom


def _log_prewarm_exception(name: str):
    """Returns a done-callback that logs an unhandled exception from a
    fire-and-forget prewarm task. Without this, Python silently swallows
    task exceptions and bad API keys don't surface until the first turn."""
    def _cb(task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("%s prewarm failed (non-fatal): %s", name, exc)
    return _cb


@app.get("/")
async def root():
    return FileResponse(FRONTEND_DIR / "index.html")


# ── Proactive check-in ──
# If the user has been silent for this many seconds, Abide initiates
# conversation based on what it sees on camera. This makes Abide feel
# like a present companion, not a passive chatbot waiting for input.
CHECK_IN_INTERVAL_S = 30

# ── TTS cache phrases ──
# Pre-generated at session start so these common utterances are served
# in 0ms instead of paying the ~1000-2000ms OpenAI TTS API call. Covers
# greetings, acknowledgements, and welfare check-in phrases that Abide
# uses repeatedly. Cache persists for the server process lifetime.
TTS_CACHE_PHRASES = [
    "Hello, I'm Abide. How are you today?",
    "I'm listening.",
    "Could you repeat that?",
    "I'm here if you need me.",
    "Are you alright?",
]


async def _proactive_checkin_loop(
    ws: WebSocket,
    session: "Session",
    engine: ConversationEngine,
    openai_key: str | None,
):
    """Background loop that fires every CHECK_IN_INTERVAL_S seconds.

    If the user has been silent and Abide is not already responding,
    sends a proactive message based on the latest vision context.
    """
    while True:
        try:
            await asyncio.sleep(CHECK_IN_INTERVAL_S)
        except asyncio.CancelledError:
            return

        # Guard: don't talk over ourselves or the user
        if session.is_responding or session.is_audible:
            continue

        # Guard: only check in if the user has actually been silent
        silence = time.monotonic() - session.last_user_speech_ts
        if silence < CHECK_IN_INTERVAL_S:
            continue

        # Guard: need vision context to say something meaningful
        vision_ctx = session.vision_buffer.as_context()
        if not vision_ctx:
            continue

        # Guard: websocket still open
        if ws.client_state != WebSocketState.CONNECTED:
            return

        log.info(
            "[CHECK-IN] User silent for %.0fs — initiating proactive message",
            silence,
        )

        # Use a system-generated prompt that tells Claude to initiate
        # based on what it sees, not respond to user speech.
        checkin_text = (
            "[System: The user has been quiet for a while. "
            "Based on what you see on camera, say something natural "
            "to start or continue a conversation. Be warm and brief — "
            "1 sentence. Don't mention that they've been quiet unless "
            "it's been a very long time.]"
        )
        try:
            session.start_response(ws, engine, checkin_text, openai_key)
        except Exception as e:
            log.error("Proactive check-in failed: %s", e)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    processor = AudioProcessor()
    session = Session()
    engine: ConversationEngine | None = None
    groq_key: str | None = None
    openai_key: str | None = None
    source_rate: int = 48000
    prev_speech = False
    checkin_task: asyncio.Task | None = None

    # Echo-suppression state
    pending_barge_in_start: float | None = None

    # Telemetry — one session id per WS connection, shared across all turns.
    # Langfuse client is None if LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are
    # not in env, in which case every telemetry call becomes a silent no-op.
    lf = telemetry.init_langfuse()
    session_id = f"abide-{uuid.uuid4().hex[:12]}"
    session.telemetry_client = lf
    session.telemetry_session_id = session_id
    session_start_ts = time.monotonic()
    turn_counter = 0

    log.info("Client connected (session_id=%s, langfuse=%s)", session_id, "on" if lf else "off")

    # Connectivity probe: emit a lightweight "connection-established" trace
    # so we can verify at a glance in the Langfuse UI that traces are
    # actually reaching the service. Runs as a fire-and-forget task so a
    # slow or unreachable Langfuse endpoint never blocks the WebSocket
    # accept path — otherwise every client connection would pay the probe's
    # network round-trip. If Langfuse is disabled the probe is a no-op.
    if lf is not None:
        async def _emit_probe():
            try:
                lf.trace(
                    name="connection-established",
                    session_id=session_id,
                    input={"event": "websocket_connect"},
                    metadata={"session_id": session_id},
                    tags=["connectivity-probe"],
                )
                telemetry.flush(lf)
                log.info(
                    "Langfuse: connectivity probe trace sent (session=%s)",
                    session_id,
                )
            except Exception as e:
                log.debug("Connectivity probe skipped: %s", e)
        _probe_task = asyncio.create_task(_emit_probe())
        _probe_task.add_done_callback(
            lambda t: t.exception() if not t.cancelled() else None
        )

    try:
        while True:
            message = await ws.receive()

            if message["type"] == "websocket.disconnect":
                break

            # Text frames = JSON control messages
            if "text" in message:
                raw_text = message["text"]
                # Guard against malformed JSON. Without this, a single bad
                # message would propagate up and kill the WebSocket session.
                try:
                    data = json.loads(raw_text)
                except (json.JSONDecodeError, TypeError):
                    log.warning("Dropped malformed JSON frame (%d bytes)", len(raw_text or ""))
                    continue
                if not isinstance(data, dict):
                    log.warning("Dropped non-object JSON frame")
                    continue
                msg_type = data.get("type")

                if msg_type == "config":
                    groq_key = data.get("groq_api_key")
                    anthropic_key = data.get("anthropic_api_key")
                    openai_key = data.get("openai_api_key")
                    try:
                        source_rate = int(data.get("sample_rate", 48000))
                        if source_rate < 8000 or source_rate > 192000:
                            source_rate = 48000
                    except (TypeError, ValueError):
                        source_rate = 48000

                    if anthropic_key:
                        engine = ConversationEngine(api_key=anthropic_key)

                    # Store refs on session so vision-reactive triggers
                    # can call start_response() from _run_vision().
                    if engine is not None:
                        session._engine_ref = engine
                    session._openai_key_ref = openai_key

                    log.info(
                        "Config received: rate=%d, groq=%s, anthropic=%s, openai=%s",
                        source_rate,
                        "set" if groq_key else "missing",
                        "set" if anthropic_key else "missing",
                        "set" if openai_key else "missing",
                    )

                    # Pre-warm all TLS connections in parallel so the user
                    # doesn't pay ~250ms of handshake on their first turn.
                    # Fire-and-forget — failures are non-fatal and logged via
                    # done_callbacks so unhandled task exceptions aren't lost.
                    if engine is not None:
                        t_claude = asyncio.create_task(engine.prewarm())
                        t_claude.add_done_callback(_log_prewarm_exception("Claude"))
                    if openai_key:
                        t_tts = asyncio.create_task(tts_prewarm(openai_key))
                        t_tts.add_done_callback(_log_prewarm_exception("TTS"))
                        t_vision = asyncio.create_task(vision_prewarm(openai_key))
                        t_vision.add_done_callback(_log_prewarm_exception("Vision"))
                        # Pre-generate TTS for stock phrases so they serve
                        # instantly on first use (greetings, check-ins, etc.)
                        t_cache = asyncio.create_task(
                            tts_prewarm_cache(TTS_CACHE_PHRASES, openai_key)
                        )
                        t_cache.add_done_callback(_log_prewarm_exception("TTS cache"))

                    # Launch the proactive check-in background loop.
                    # Every CHECK_IN_INTERVAL_S seconds of user silence, Abide
                    # initiates conversation based on what it sees on camera.
                    checkin_task = asyncio.create_task(
                        _proactive_checkin_loop(ws, session, engine, openai_key)
                    )

                    await Session._safe_send_json(ws,{"type": "status", "state": "listening"})

                    # Welcome greeting: play the cached "Hello, I'm Abide..."
                    # immediately on connect so the user knows Abide is alive.
                    # Waits briefly for the cache prewarm to populate that
                    # phrase, then falls through to the API path if needed.
                    # Runs as a task so it doesn't block the WS receive loop.
                    if engine is not None and openai_key:
                        async def _welcome():
                            # Let the cache prewarm generate the first phrase
                            # (~1s). If it's not ready by then, synthesize()
                            # will just call the API directly — still correct.
                            await asyncio.sleep(1.2)
                            await session.say_canned(
                                ws, engine, TTS_CACHE_PHRASES[0], openai_key,
                            )
                        asyncio.create_task(_welcome())

                elif msg_type == "frame":
                    # Legacy single-frame message from browser (kept for
                    # back-compat). Wraps it in a 1-element list.
                    b64 = data.get("jpeg_b64", "")
                    if not isinstance(b64, str) or not b64 or not openai_key:
                        continue
                    if len(b64) > MAX_FRAME_B64_CHARS:
                        log.warning(
                            "Frame exceeds size limit (%d chars), dropping",
                            len(b64),
                        )
                        continue
                    try:
                        jpeg_bytes = base64.b64decode(b64, validate=False)
                    except Exception as e:
                        log.warning("Frame decode failed: %s", e)
                    else:
                        session.process_frames([jpeg_bytes], openai_key, ws)

                elif msg_type == "frames":
                    # Multi-frame sequence from browser (oldest → newest).
                    # Used for motion-aware vision analysis.
                    b64_list = data.get("jpeg_b64_list", [])
                    if not isinstance(b64_list, list) or not b64_list or not openai_key:
                        continue
                    # Defensive: cap the number of frames too, in case a
                    # malicious/buggy client sends a huge batch.
                    if len(b64_list) > 8:
                        log.warning("Frames batch too large (%d), truncating", len(b64_list))
                        b64_list = b64_list[:8]
                    jpegs: list[bytes] = []
                    for b64 in b64_list:
                        if not isinstance(b64, str):
                            continue
                        if len(b64) > MAX_FRAME_B64_CHARS:
                            log.warning(
                                "Frame in batch exceeds size limit (%d chars), skipping",
                                len(b64),
                            )
                            continue
                        try:
                            jpegs.append(base64.b64decode(b64, validate=False))
                        except Exception as e:
                            log.warning("Frame decode failed: %s", e)
                    if jpegs:
                        session.process_frames(jpegs, openai_key, ws)

                elif msg_type == "playback_start":
                    # Browser has started playing buffered TTS audio.
                    # Used by the barge-in gate (session.is_audible) so
                    # we can interrupt during the playback window after
                    # the server's response task has already finished.
                    session.client_playing = True

                elif msg_type == "playback_end":
                    # Browser has drained its audio queue OR has stopped
                    # playback in response to a barge-in. Idempotent.
                    session.client_playing = False

            # Binary frames = raw PCM float32 audio
            elif "bytes" in message:
                raw = message["bytes"]
                # Defensive validation: frames must be non-empty, correctly
                # sized (multiple of 4 bytes for float32), and within a
                # reasonable upper bound. A malformed client could otherwise
                # crash np.frombuffer or starve the loop with huge buffers.
                if not raw or not isinstance(raw, (bytes, bytearray, memoryview)):
                    continue
                if len(raw) > MAX_AUDIO_CHUNK_BYTES or (len(raw) % 4) != 0:
                    log.warning("Dropped malformed audio chunk (%d bytes)", len(raw))
                    continue
                try:
                    pcm = np.frombuffer(raw, dtype=np.float32)
                except ValueError as e:
                    log.warning("np.frombuffer rejected audio chunk: %s", e)
                    continue
                if pcm.size == 0:
                    continue
                now = time.monotonic()

                # Echo suppression: are we within the post-TTS cooldown?
                in_cooldown = (
                    session.is_audible
                    and session.last_tts_send_ts is not None
                    and (now - session.last_tts_send_ts) * 1000 < POST_TTS_COOLDOWN_MS
                )

                if in_cooldown:
                    # Don't feed audio to VAD during cooldown — the TTS echo
                    # leaking through the mic would trigger phantom speech events.
                    # Reset any in-progress collection and drop this chunk.
                    processor.reset()
                    pending_barge_in_start = None
                    prev_speech = False
                    continue

                wav_bytes = processor.feed(pcm, source_rate)

                # Detect speech state changes
                if processor.is_speech != prev_speech:
                    prev_speech = processor.is_speech

                    # BARGE-IN candidate: user started speaking while Abide is
                    # audible (server still producing, OR client still playing
                    # buffered TTS audio). Both count — without the latter,
                    # barge-in goes deaf the moment the last chunk is sent.
                    if prev_speech and session.is_audible:
                        pending_barge_in_start = now
                        log.info(
                            "Speech detected during response — sustained-check starting"
                        )
                        # Don't fire yet. Fall through to let audio accumulate.
                    else:
                        if not prev_speech:
                            pending_barge_in_start = None

                        state = "hearing" if prev_speech else "listening"
                        # Don't send "listening" if a response is still playing
                        if not (state == "listening" and session.is_audible):
                            await Session._safe_send_json(ws,{"type": "status", "state": state})

                # Sustained-speech check: if we've been seeing speech during a response
                # for >= SUSTAINED_SPEECH_MS, AND the audio we've collected is loud
                # enough to be real speech (not TTS echo leaking through the mic),
                # that's a real barge-in.
                if (
                    pending_barge_in_start is not None
                    and processor.is_speech
                    and session.is_audible
                ):
                    elapsed_ms = (now - pending_barge_in_start) * 1000
                    if elapsed_ms >= SUSTAINED_SPEECH_MS:
                        max_rms = processor.current_max_rms
                        if max_rms >= BARGE_IN_MIN_RMS:
                            log.info(
                                "Sustained speech confirmed (%.0fms, RMS=%.4f) — firing barge-in!",
                                elapsed_ms,
                                max_rms,
                            )
                            await session.cancel(ws)
                            pending_barge_in_start = None
                            # Keep current VAD state — user is still speaking.
                            # The speech already collected will complete naturally.
                            continue
                        # Too quiet to be real speech. This is almost certainly
                        # TTS echo leaking through the mic. Do NOT fire barge-in.
                        # Leave pending_barge_in_start set so we can re-evaluate
                        # on the next chunk — if the user DOES actually start
                        # speaking loudly, max_rms will climb and we'll fire then.
                        # Log only once every ~500ms of elapsed to avoid spam.
                        if int(elapsed_ms) % 500 < 50:
                            log.info(
                                "Barge-in candidate too quiet (%.0fms, RMS=%.4f < %.4f) — likely echo, holding",
                                elapsed_ms,
                                max_rms,
                                BARGE_IN_MIN_RMS,
                            )
                elif pending_barge_in_start is not None and not processor.is_speech:
                    # Speech stopped before the sustained threshold — false alarm (echo).
                    log.info("Pending barge-in cancelled (speech stopped, likely echo)")
                    pending_barge_in_start = None

                if wav_bytes is not None:
                    if not groq_key:
                        await Session._safe_send_json(ws,{"type": "error", "message": "Groq API key not set"})
                        continue

                    await Session._safe_send_json(ws,{"type": "status", "state": "processing"})
                    log.info("Speech segment captured, transcribing...")

                    try:
                        stt_t0 = time.monotonic()
                        text = await transcribe(
                            wav_bytes,
                            groq_key,
                            processor.last_speech_end_ts,
                            user_name=session.user_context.name,
                        )
                        stt_latency_ms = (time.monotonic() - stt_t0) * 1000
                        if text:
                            log.info("Transcript: %s", text)
                            session.last_user_speech_ts = time.monotonic()
                            # Guard: WS may have closed while STT was in flight
                            if ws.client_state != WebSocketState.CONNECTED:
                                log.info("WS closed during STT, dropping transcript")
                                continue
                            await Session._safe_send_json(ws,{"type": "transcript", "text": text})

                            if engine:
                                # Telemetry: create the parent trace for this turn
                                # and attach the STT span. The trace is handed off
                                # to session.start_response, which logs Claude + TTS
                                # spans and calls end_turn_trace when the pipeline
                                # finishes (or is barged-in).
                                turn_counter += 1
                                turn_trace = telemetry.start_turn_trace(
                                    lf,
                                    session_id=session_id,
                                    turn_number=turn_counter,
                                    transcript=text,
                                    vision_context=session.vision_buffer.as_context(),
                                )
                                telemetry.observe_stt(
                                    turn_trace,
                                    audio_bytes=len(wav_bytes),
                                    transcript=text,
                                    latency_ms=stt_latency_ms,
                                )

                                # Launch response as concurrent task (non-blocking)
                                session.start_response(
                                    ws, engine, text, openai_key,
                                    turn_trace=turn_trace,
                                )
                            else:
                                await Session._safe_send_json(ws,{"type": "error", "message": "Anthropic API key not set"})
                        else:
                            log.info("Empty transcript, skipping")
                            if not session.is_audible:
                                await Session._safe_send_json(ws,{"type": "status", "state": "listening"})
                    except asyncio.TimeoutError:
                        log.error("Transcription timed out")
                        await Session._safe_send_json(ws,{
                            "type": "error",
                            "message": "I didn't catch that — please try again.",
                        })
                        if not session.is_audible:
                            await Session._safe_send_json(ws,{"type": "status", "state": "listening"})
                    except Exception as e:
                        # Log the real error server-side but send a safe,
                        # non-leaky message to the client.
                        log.error("Transcription failed: %s", e)
                        await Session._safe_send_json(ws,{
                            "type": "error",
                            "message": "I'm having trouble hearing you right now. Please try again.",
                        })
                        if not session.is_audible:
                            await Session._safe_send_json(ws,{"type": "status", "state": "listening"})

    except WebSocketDisconnect:
        log.info("Client disconnected")
    except Exception as e:
        log.error("WebSocket error: %s", e)
    finally:
        # Telemetry: log the session summary + flush the Langfuse client.
        try:
            duration_s = time.monotonic() - session_start_ts
            latencies = session.stats.get("turn_latencies_ms", [])
            avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0
            summary = {
                "session_id": session_id,
                "duration_seconds": round(duration_s, 1),
                "total_turns": session.stats["total_turns"],
                "completed_turns": session.stats["completed_turns"],
                "barge_in_count": session.stats["barge_in_count"],
                "fall_count": session.stats["fall_count"],
                "vision_calls": session.stats["vision_calls"],
                "avg_turn_latency_ms": avg_latency,
                "max_turn_latency_ms": max(latencies) if latencies else 0,
                "min_turn_latency_ms": min(latencies) if latencies else 0,
            }
            log.info("Session summary: %s", summary)
            telemetry.log_session_summary(lf, session_id, summary)
            telemetry.flush(lf)
        except Exception as e:
            log.debug("Session summary telemetry skipped: %s", e)

        # Cancel proactive check-in loop
        if checkin_task is not None and not checkin_task.done():
            checkin_task.cancel()

        if engine is not None:
            try:
                await engine.aclose()
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
