"""Phase 7: FastAPI with VAD + STT + Claude + TTS + Barge-in + Vision + Langfuse telemetry."""

import base64
import json
import logging
import re
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

from app import audio_events
from app.audio import AudioProcessor, transcribe
from app.conversation import ConversationEngine, MODEL as CLAUDE_MODEL, APIKeyError
from app.memory import (
    _ID_RE as _MEMORY_ID_RE,
    delete_user_context,
    load_user_context,
    save_user_context,
)
from app.session import Session, UserContext
from app.tts import prewarm as tts_prewarm, prewarm_cache as tts_prewarm_cache
from app.tts_cache_store import learned_phrases, stats as tts_store_stats
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
MODELS_DIR = Path(__file__).parent.parent / "models"

# Phase U follow-up — serve the self-hosted MediaPipe assets (vendor
# bundle, WASM runtime) and on-device ML models (YAMNet tflite, pose
# landmarker .task) directly from the repo. Removes the CDN dependency
# on cdn.jsdelivr.net + storage.googleapis.com that Phase U.2/U.3
# originally introduced — that CDN path was a supply-chain XSS risk
# since the page holds user API keys in localStorage. `check_dir=False`
# so the app still starts even if /models is absent (e.g. user cloned
# without the binaries, or Intel Mac where YAMNet isn't usable).
from fastapi.staticfiles import StaticFiles

if (FRONTEND_DIR / "vendor").exists():
    app.mount(
        "/vendor",
        StaticFiles(directory=str(FRONTEND_DIR / "vendor")),
        name="vendor",
    )
if MODELS_DIR.exists():
    app.mount(
        "/models",
        StaticFiles(directory=str(MODELS_DIR)),
        name="models",
    )


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
        # The done-callback surfaces any unhandled exception — previously
        # this was a no-op lambda that silently discarded failures.
        t = asyncio.create_task(telemetry.verify_langfuse_async(timeout=3.0))
        t.add_done_callback(_log_prewarm_exception("Langfuse verify"))


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    """Flush Langfuse traces and close persistent HTTP clients."""
    try:
        telemetry.flush(telemetry.init_langfuse())
    except Exception as e:
        log.debug("Langfuse flush on shutdown failed (%s)", type(e).__name__)
    # Close all module-level persistent httpx clients
    from app.tts import aclose as tts_close
    from app.vision import aclose as vision_close
    for name, closer in [("analyze", _analyze_client.aclose), ("tts", tts_close), ("vision", vision_close)]:
        try:
            await closer()
        except Exception as e:
            log.debug("Shutdown: closing %s client failed (%s)", name, type(e).__name__)


# ── Session analysis endpoint ──
# One-shot Claude call at session end to review what Abide got right/wrong.
# Frontend cannot call the Anthropic API directly (CORS blocked), so this
# lightweight proxy accepts conversation history + activity log and returns
# a structured analysis string.

# Persistent HTTP/2 client for the analysis endpoint. Module-level so we
# don't pay TCP+TLS handshake per call (~300-500ms). Reused across sessions.
# Split connect vs read timeout: 5 s is enough to establish TLS to Anthropic;
# 25 s gives the model time to stream up to 500 tokens. A combined 30 s
# timeout would let a hung connection stall the handler for 30 s before
# the read phase even starts.
_analyze_client = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=5.0, read=25.0),
    http2=True,
)

# Most-recent session's Anthropic key, stored server-side at WS config time.
# /api/analyze uses this instead of accepting a key from the request body,
# preventing an adversarial page from relaying arbitrary keys through the
# local server. Falls back to the body key only if no session has run since
# server start (e.g. a test caller hitting the endpoint directly).
_last_session_anthropic_key: str | None = None

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

    # Input validation ── size caps prevent a multi-MB payload from
    # being forwarded verbatim to the Anthropic API.
    raw_history = body.get("history", [])
    if not isinstance(raw_history, list):
        raw_history = []
    # Sanitize: only user/assistant turns, cap per-message content, last 60 turns.
    history = [
        {
            "role": m["role"],
            "content": str(m.get("content", ""))[:5_000],
        }
        for m in raw_history[-60:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant")
    ]

    activity_log = body.get("activity_log", "")
    if not isinstance(activity_log, str):
        activity_log = ""
    activity_log = activity_log[:50_000]  # 50 KB max

    # Prefer the key cached from the most recent WS session so the API
    # key doesn't need to travel in the request body. Fall back to the
    # body key for backward compat (e.g. direct test calls).
    api_key = _last_session_anthropic_key or body.get("api_key", "")
    if not api_key:
        return JSONResponse({"error": "No API key available — start a session first"}, status_code=400)
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
                # Use the same MODEL as the live conversation path so a
                # Phase-P-style upgrade covers both. Hardcoding
                # `claude-sonnet-4-20250514` here (what this line used
                # to say) left a silent deprecation bomb — Anthropic
                # sunsets that model on 2026-06-15.
                "model": CLAUDE_MODEL,
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
        log.error("Analysis request failed: %s: %r", type(e).__name__, e)
        return JSONResponse({"error": "Analysis failed"}, status_code=500)


# Echo suppression tunables.
#
# Phase L: these values are tuned for the Logitech MeetUp (and any
# conference device with hardware Acoustic Echo Cancellation). MeetUp
# does AEC in firmware — the mic signal it sends to the PC is already
# stripped of whatever the MeetUp's speaker is playing, so residual
# echo on a barge-in candidate is near-zero and we can gate
# aggressively without phantom interrupts.
#
# On a LAPTOP (built-in mic + speakers, no hardware AEC) these values
# will likely cause false barge-ins from TTS bleeding through the
# microphone. Revert to the pre-Phase-L defaults if deploying there:
#     SUSTAINED_SPEECH_MS = 400
#     BARGE_IN_MIN_LOUD_WINDOWS = 6
# See DESIGN-NOTES.md D80 for the full trade-off history.
POST_TTS_COOLDOWN_MS = 300   # ignore VAD this long after last TTS chunk sent
SUSTAINED_SPEECH_MS = 150    # MeetUp-tuned; was 400 pre-Phase-L
# Require SUSTAINED above-threshold audio, not just a peak. Each VAD
# window is ~32ms at 16kHz (VAD_CHUNK=512). 4 windows ≈ 128ms of
# cumulative audio whose per-window RMS cleared MIN_SPEECH_RMS (0.015).
# Pre-Phase-L this was 6 (≈192ms); MeetUp's hardware AEC lets us drop
# it to 4 because there's essentially no TTS-echo leakage for the
# counter to trip on. A single loud keypress/cough/click still fails
# to reach 4 loud windows, so false-positive protection is intact.
BARGE_IN_MIN_LOUD_WINDOWS = 4  # MeetUp-tuned; was 6 pre-Phase-L

# Defensive limits on client-supplied payloads
MAX_FRAME_B64_CHARS = 500_000  # ~350 KB decoded — well above our 512x384 JPEGs (~15-40 KB)
MAX_AUDIO_CHUNK_BYTES = 32_768  # 2048 float32 samples = 8192 bytes; 4x headroom

# Cheap character-class validator for base64 frame payloads. Faster than
# a full b64decode roundtrip (regex is ~1000x quicker on 40 KB strings)
# and sufficient to catch client bugs / garbage injection before we pass
# the string on to OpenAI's vision API. Full decode validation would
# defeat the purpose of pass-through (the previous code decoded then
# re-encoded inside vision.py, costing ~20-40 ms per vision cycle).
_FRAME_B64_RE = re.compile(r'^[A-Za-z0-9+/=\s]*$')


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
# Baseline seed list — phrases we prewarm on a fresh install before the
# auto-populated cache (tts_cache_store) has accumulated any data. Once
# the system has seen a few sessions, `learned_phrases()` returns the
# phrases Claude actually emits repeatedly, and those get merged in.
# The seed stays as a floor so first-run UX is still snappy.
#
# Cache hit is exact-string-after-normalization (see _normalize_phrase
# in tts.py), so hits depend on Claude producing matching punctuation
# + wording. The system prompt nudges toward short acknowledgements,
# and the learning layer picks up whatever sentences actually occur.
TTS_CACHE_SEED_PHRASES = [
    # Stock greetings / check-ins (used for welcome + proactive prompts).
    # The first four are the time-of-day welcome variants; select_welcome_greeting()
    # picks one based on the browser-reported local hour.
    "Good morning! I'm Abide. How are you today?",
    "Good afternoon! I'm Abide. How are you today?",
    "Good evening! I'm Abide. How are you today?",
    "Hello, I'm Abide. How are you tonight?",
    # Generic fallback used when the browser hasn't reported a timezone yet.
    "Hello, I'm Abide. How are you today?",
    "Hello there!",
    "I'm listening.",
    "Could you repeat that?",
    "I'm here if you need me.",
    "Are you alright?",
    # Short acknowledgements that pair with the SYSTEM_PROMPT nudge
    # encouraging Claude to open replies briefly.
    "That's good.",
    "That's good to hear.",
    "That sounds nice.",
    "I see.",
    "Oh really?",
    "Tell me more.",
]


def _select_welcome_greeting(session: "Session") -> str:
    """Pick a time-of-day-appropriate welcome based on the browser-reported
    local hour. Falls back to the generic greeting when the timezone hasn't
    been reported yet (shouldn't happen in practice — the config message
    arrives before the 1.2s welcome delay — but defensive).

    Phase K: if the hydrated UserContext has a name (returning resident),
    splice it in. The exact string is not in the seed list — it gets
    prewarmed asynchronously in the config handler so it's cached by the
    time the welcome delay elapses. On cache miss, synthesize() falls
    back to an API call (~1 s) — still correct, just not instant."""
    bucket = session.time_of_day_bucket()
    name = (session.user_context.name or "").strip()
    if name:
        return {
            "morning":   f"Good morning, {name}! I'm Abide. How are you today?",
            "afternoon": f"Good afternoon, {name}! I'm Abide. How are you today?",
            "evening":   f"Good evening, {name}! I'm Abide. How are you today?",
            "night":     f"Hello, {name}. I'm Abide. How are you tonight?",
        }.get(bucket, f"Hello, {name}. I'm Abide. How are you today?")
    return {
        "morning": "Good morning! I'm Abide. How are you today?",
        "afternoon": "Good afternoon! I'm Abide. How are you today?",
        "evening": "Good evening! I'm Abide. How are you today?",
        "night": "Hello, I'm Abide. How are you tonight?",
    }.get(bucket, "Hello, I'm Abide. How are you today?")

# Hard cap on how many phrases we prewarm per session start. Prewarm is
# parallel via asyncio.gather, so too many at once can spike concurrent
# OpenAI TTS requests. 30 is generous and well under `_TTS_CACHE_MAX_ENTRIES`.
_PREWARM_TOTAL_CAP = 30

# All time-of-day welcome variants (including the generic fallback).
# _relevant_welcome_phrases() picks the subset that matches the server's
# current clock-hour bucket; the others are filtered out of the prewarm
# list so we don't pay ~4 × 2 s of OpenAI TTS per startup warming greetings
# the session will never play. Generic "today" fallback stays in every
# bucket as insurance for the window where the browser tz hasn't arrived.
_ALL_WELCOME_PHRASES = frozenset({
    "Good morning! I'm Abide. How are you today?",
    "Good afternoon! I'm Abide. How are you today?",
    "Good evening! I'm Abide. How are you today?",
    "Hello, I'm Abide. How are you tonight?",
    "Hello, I'm Abide. How are you today?",
})
_GENERIC_WELCOME = "Hello, I'm Abide. How are you today?"


def _server_hour_bucket() -> str:
    """Server-local time-of-day bucket, using same thresholds as
    Session.time_of_day_bucket(). Used at prewarm time when the browser's
    tz offset hasn't arrived yet; server clock is a close enough proxy
    for local-only deployments."""
    from datetime import datetime
    h = datetime.now().hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"


def _relevant_welcome_phrases() -> set[str]:
    bucket = _server_hour_bucket()
    matching = {
        "morning": "Good morning! I'm Abide. How are you today?",
        "afternoon": "Good afternoon! I'm Abide. How are you today?",
        "evening": "Good evening! I'm Abide. How are you today?",
        "night": "Hello, I'm Abide. How are you tonight?",
    }[bucket]
    return {matching, _GENERIC_WELCOME}


def _prewarm_phrase_list() -> list[str]:
    """Baseline seed + phrases learned from prior sessions, deduped and
    bounded. Dedup preserves first-seen order so the seed wins ties.

    Drops time-of-day welcome variants that don't match the server's
    current hour — saves ~3 × ~2 s of OpenAI TTS on startup since
    previously all four buckets were prewarmed unconditionally. The
    matching variant + generic fallback are always kept. Learned phrases
    get the same filter so variants that accumulated in `phrase_counts.json`
    from prior sessions at other times of day don't sneak back in."""
    relevant = _relevant_welcome_phrases()
    to_drop = _ALL_WELCOME_PHRASES - relevant
    # Pull extra learned phrases so after filtering we still have enough.
    learned = learned_phrases(limit=_PREWARM_TOTAL_CAP * 2)
    seed_filtered = [p for p in TTS_CACHE_SEED_PHRASES if p not in to_drop]
    learned_filtered = [p for p in learned if p not in to_drop]
    merged = list(dict.fromkeys(seed_filtered + learned_filtered))
    return merged[:_PREWARM_TOTAL_CAP]


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


_ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Reject cross-origin browser connections. Browsers always send an
    # Origin header for WebSocket upgrades; curl/wscat/dev scripts don't.
    # Allowing missing-Origin lets developer tooling through while blocking
    # a malicious page at http://evil.com from opening ws://localhost:8000/ws
    # and driving the session (or receiving API keys from the config message).
    origin = ws.headers.get("origin")
    if origin and origin not in _ALLOWED_ORIGINS:
        log.warning("[WS] Rejected connection from disallowed origin: %s", origin)
        await ws.close(code=1008)  # 1008 = Policy Violation
        return
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

    # Phase U.3 follow-up — per-connection face_bbox receive-rate tracker.
    # Live session `abide-585f1dec5ee2` showed TTFA regress from ~3300 ms
    # to ~4968 ms on a long (9 min) session. One hypothesis: the 5 Hz
    # pose-bbox WS traffic from the browser congests the event loop
    # alongside the voice loop. This counter logs once per ~100 received
    # messages so we can see whether the client is adhering to its 5 Hz
    # cap or misbehaving. Non-invasive — no throttling here, just
    # observability. Dedicated rate tracking on the receive side because
    # `dispatch_face_bbox` already throttles at the dispatch point and
    # so hides the raw incoming rate.
    face_bbox_recv_count = 0
    face_bbox_rate_window_start = time.monotonic()
    # Simple per-message rate gate for face_bbox. Client should send at
    # ≤5 Hz; we tolerate up to 15 Hz before dropping silently. Prevents
    # a runaway client from flooding the event loop with bbox validation
    # work even though dispatch_face_bbox already throttles PTZ nudges.
    _BBOX_MIN_INTERVAL_S = 1.0 / 15  # 15 Hz ceiling
    _bbox_last_accept_ts = 0.0

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
        # Consistent with the other prewarm/background tasks: if the
        # probe raises something we didn't anticipate, surface it at
        # WARNING rather than discarding with a silent
        # `t.exception() if not t.cancelled() else None` lambda. The
        # body of `_emit_probe` already catches Exception, so this
        # done-callback should rarely have work to do.
        _probe_task.add_done_callback(_log_prewarm_exception("Langfuse probe"))

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
                    # Browser-reported timezone offset (JS getTimezoneOffset).
                    # Validate to a sane range (-14h to +14h in minutes) so a
                    # malicious/buggy client can't throw off time-of-day
                    # buckets in silly ways.
                    try:
                        tz_raw = data.get("timezone_offset_minutes")
                        if tz_raw is not None:
                            tz_val = int(tz_raw)
                            if -840 <= tz_val <= 840:
                                session.tz_offset_minutes = tz_val
                    except (TypeError, ValueError):
                        pass

                    # Cross-session memory (Phase E): if the browser sent a
                    # resident_id that passes the strict format check, hydrate
                    # UserContext from disk. The path-traversal guard lives in
                    # app.memory._safe_path; we re-check the format here so we
                    # don't log-spam on a garbage id and so session.resident_id
                    # only gets set when the id is usable for later saves.
                    rid = data.get("resident_id")
                    if isinstance(rid, str) and _MEMORY_ID_RE.match(rid):
                        session.resident_id = rid
                        persisted = await asyncio.to_thread(load_user_context, rid)
                        if persisted:
                            session.user_context = UserContext.from_dict(persisted)
                            log.info(
                                "[MEMORY] Hydrated UserContext for %s: %s",
                                rid,
                                session.user_context.as_prompt()[:120],
                            )
                    # Push the current snapshot (hydrated or empty) so the
                    # UI "What Abide remembers" panel renders immediately on
                    # connect instead of waiting for the first fact update.
                    await Session._safe_send_json(
                        ws,
                        {"type": "remember_snapshot", "context": session.user_context.snapshot()},
                    )

                    if anthropic_key:
                        global _last_session_anthropic_key
                        _last_session_anthropic_key = anthropic_key
                        engine = ConversationEngine(api_key=anthropic_key)
                        # Phase S.1 — bake per-session camera capabilities
                        # into the cacheable system prompt once at connect.
                        # Cleaner than re-injecting per-turn (which would
                        # fragment the cache) and honest to the user's
                        # actual hardware (MeetUp exposing pan/tilt is
                        # firmware-dependent — see D82 correction history).
                        axes = session._ptz.axes_available
                        if axes:
                            cap_note = (
                                "\n\nSession camera capabilities:\n"
                                f"- Available axes: {', '.join(axes)}.\n"
                                "- Zoom responds to spoken requests via the "
                                "[[CAM:...]] markers described above.\n"
                                + (
                                    "- Pan and tilt are driven automatically "
                                    "by subject-following (the camera "
                                    "re-centres on the person as they move); "
                                    "no spoken command is needed. If the user "
                                    "asks to pan or tilt, reassure them that "
                                    "the camera is tracking them.\n"
                                    if ("pan" in axes or "tilt" in axes)
                                    else "- Pan and tilt are NOT available on "
                                    "this camera. If the user asks for pan or "
                                    "tilt, briefly and honestly say so.\n"
                                )
                            )
                        else:
                            cap_note = (
                                "\n\nSession camera capabilities:\n"
                                "- No motorised camera control available on "
                                "this setup. If the user asks to zoom, pan, "
                                "or tilt, briefly and honestly say that the "
                                "current camera cannot move.\n"
                            )
                        from app.conversation import SYSTEM_PROMPT as _SP
                        engine.system_prompt_override = _SP + cap_note

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
                    # Pre-load YAMNet (audio-event classifier). Prior to
                    # this, the tflite interpreter lazy-loaded on the
                    # first speech segment, paying ~600-900 ms of model
                    # load + tensor allocation inside the TTFA window of
                    # turn 1. The `[TIMING] speech_end → _run_response
                    # started` anchor shipped D96 showed this as a
                    # consistent 2.5 s tax on turn 1 that disappeared
                    # from turn 2 onward. Loading it here, off-loop,
                    # moves the cost into the connect-to-first-utterance
                    # window where the user is not waiting. No API key
                    # required — model is local, so this runs
                    # unconditionally alongside the cloud prewarms.
                    t_audio_events = asyncio.create_task(audio_events.prewarm())
                    t_audio_events.add_done_callback(
                        _log_prewarm_exception("YAMNet")
                    )
                    if engine is not None:
                        t_claude = asyncio.create_task(engine.prewarm())
                        t_claude.add_done_callback(_log_prewarm_exception("Claude"))
                    if openai_key:
                        t_tts = asyncio.create_task(tts_prewarm(openai_key))
                        t_tts.add_done_callback(_log_prewarm_exception("TTS"))
                        t_vision = asyncio.create_task(vision_prewarm(openai_key))
                        t_vision.add_done_callback(_log_prewarm_exception("Vision"))
                        # Pre-generate TTS for the seed list + whatever
                        # Claude has said repeatedly in prior sessions
                        # (auto-populated from app/tts_cache_store.py).
                        prewarm_list = _prewarm_phrase_list()
                        log.info(
                            "[TTS-CACHE] Prewarming %d phrases (store: %s)",
                            len(prewarm_list),
                            tts_store_stats(),
                        )
                        t_cache = asyncio.create_task(
                            tts_prewarm_cache(prewarm_list, openai_key)
                        )
                        t_cache.add_done_callback(_log_prewarm_exception("TTS cache"))

                        # Personalized welcome: if UserContext hydrated a
                        # name AND we know the time-of-day bucket, prewarm
                        # the one matching name-aware greeting variant so
                        # the welcome handler (1.2 s later) finds it in
                        # the cache and serves at 0 ms. The string itself
                        # isn't in the seed list because it's per-user.
                        # Fire-and-forget; on cache miss, say_canned
                        # falls through to an API call — still correct.
                        if (
                            session.user_context.name
                            and session.tz_offset_minutes is not None
                        ):
                            personalized = _select_welcome_greeting(session)
                            log.info(
                                "[TTS-CACHE] Prewarming personalized greeting: %r",
                                personalized,
                            )
                            t_name_cache = asyncio.create_task(
                                tts_prewarm_cache([personalized], openai_key)
                            )
                            t_name_cache.add_done_callback(
                                _log_prewarm_exception("Personalized greeting")
                            )

                    # Launch the proactive check-in background loop.
                    # Every CHECK_IN_INTERVAL_S seconds of user silence, Abide
                    # initiates conversation based on what it sees on camera.
                    checkin_task = asyncio.create_task(
                        _proactive_checkin_loop(ws, session, engine, openai_key)
                    )
                    # Surface any unhandled exception from the check-in
                    # loop instead of letting Python's "Task exception
                    # was never retrieved" warning surface at GC time
                    # (which happens long after the WS has closed). The
                    # loop body already wraps each iteration in a try/
                    # except, but errors in the guard-check window
                    # between `asyncio.sleep` and the inner try would
                    # otherwise disappear.
                    checkin_task.add_done_callback(
                        _log_prewarm_exception("Check-in loop")
                    )

                    await Session._safe_send_json(ws,{"type": "status", "state": "listening"})

                    # Welcome greeting: play the cached "Hello, I'm Abide..."
                    # immediately on connect so the user knows Abide is alive.
                    # Waits briefly for the cache prewarm to populate that
                    # phrase, then falls through to the API path if needed.
                    # Runs as a task so it doesn't block the WS receive loop.
                    # `say_canned` resets status to "listening" on failure,
                    # so the done_callback here is just to surface any
                    # exception that bubbles out of that recovery path —
                    # otherwise silent task deaths leave the UI frozen on
                    # "speaking". Mirrors the prewarm-task pattern above.
                    if engine is not None and openai_key:
                        async def _welcome():
                            # Let the cache prewarm generate the first phrase
                            # (~1s). If it's not ready by then, synthesize()
                            # will just call the API directly — still correct.
                            await asyncio.sleep(1.2)
                            greeting = _select_welcome_greeting(session)
                            await session.say_canned(
                                ws, engine, greeting, openai_key,
                            )
                        t_welcome = asyncio.create_task(_welcome())
                        t_welcome.add_done_callback(_log_prewarm_exception("Welcome"))

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
                    if not _FRAME_B64_RE.fullmatch(b64):
                        log.warning("Frame b64 has invalid characters, dropping")
                        continue
                    # Pass the base64 string through as-is — vision.py
                    # constructs the data URL directly, avoiding a decode/
                    # re-encode cycle on the hot path.
                    session.process_frames([b64], openai_key, ws)

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
                    valid_b64s: list[str] = []
                    for b64 in b64_list:
                        if not isinstance(b64, str):
                            continue
                        if len(b64) > MAX_FRAME_B64_CHARS:
                            log.warning(
                                "Frame in batch exceeds size limit (%d chars), skipping",
                                len(b64),
                            )
                            continue
                        if not _FRAME_B64_RE.fullmatch(b64):
                            log.warning("Frame in batch has invalid b64 chars, skipping")
                            continue
                        valid_b64s.append(b64)
                    if valid_b64s:
                        session.process_frames(valid_b64s, openai_key, ws)

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

                elif msg_type == "face_bbox":
                    # Phase U.2 — client-side MediaPipe pose landmarks →
                    # min-max bbox of visible keypoints → smooth PTZ. Up
                    # to ~5 Hz from the browser; server rate-limits again
                    # in Session.dispatch_face_bbox so DirectShow doesn't
                    # get hammered.
                    #
                    # Strict validation: bbox must be 4 numeric floats (NOT
                    # bools — `isinstance(True, int)` returns True without
                    # the explicit exclusion) AND each coordinate must be
                    # in the [0, 1] range MediaPipe normalises to. A bogus
                    # or adversarial client can't feed us overflow-sized
                    # floats that would blow up `nudge_to_bbox`'s arithmetic.
                    bbox = data.get("bbox")
                    _now_bbox = time.monotonic()
                    if _now_bbox - _bbox_last_accept_ts >= _BBOX_MIN_INTERVAL_S:
                        _bbox_last_accept_ts = _now_bbox
                        if (
                            isinstance(bbox, list)
                            and len(bbox) == 4
                            and all(
                                isinstance(v, (int, float))
                                and not isinstance(v, bool)
                                and 0.0 <= float(v) <= 1.0
                                for v in bbox
                            )
                            and float(bbox[0]) < float(bbox[2])   # x1 < x2
                            and float(bbox[1]) < float(bbox[3])   # y1 < y2
                        ):
                            session.dispatch_face_bbox([float(v) for v in bbox])

                    # Receive-rate telemetry. Client is supposed to cap
                    # at 5 Hz (POSE_BBOX_SEND_INTERVAL_MS = 200 in
                    # frontend); if we see much more than that something
                    # is wrong and it likely correlates with TTFA drift.
                    face_bbox_recv_count += 1
                    if face_bbox_recv_count >= 100:
                        now_ts = time.monotonic()
                        window = max(1e-3, now_ts - face_bbox_rate_window_start)
                        rate_hz = face_bbox_recv_count / window
                        log.info(
                            "[FACE-BBOX] recv rate: %.1f Hz over last %d msgs (%.1fs window)",
                            rate_hz,
                            face_bbox_recv_count,
                            window,
                        )
                        face_bbox_recv_count = 0
                        face_bbox_rate_window_start = now_ts

                elif msg_type == "fall_alert":
                    # Phase U.3 — client-side pose fall heuristic fired
                    # (nose y ≥ hip y for ~1 s of sustained horizontal
                    # torso). Treat identically to a vision-prompt FALL:
                    # prefix: red banner + urgent next-turn context.
                    raw_text = data.get("text", "")
                    if isinstance(raw_text, str):
                        text = raw_text[:200].strip() or "Possible fall detected by pose estimation."
                        await session.handle_client_fall(ws, text)

                elif msg_type == "forget_me":
                    # Cross-session memory wipe (Phase E). Delete the
                    # on-disk memory file, reset the in-memory UserContext,
                    # and clear resident_id so subsequent fact extractions
                    # don't resurrect the record. Client is responsible
                    # for regenerating a fresh UUID after acknowledgement.
                    rid = session.resident_id
                    if rid:
                        deleted = await asyncio.to_thread(delete_user_context, rid)
                        log.info("[MEMORY] Forget-me: resident_id=%s deleted=%s", rid, deleted)
                    session.user_context = UserContext()
                    session.resident_id = None
                    await Session._safe_send_json(ws, {"type": "forget_me_ok"})
                    # Push the now-empty snapshot so the UI panel reverts
                    # to the empty state without needing a round-trip.
                    await Session._safe_send_json(
                        ws,
                        {"type": "remember_snapshot", "context": session.user_context.snapshot()},
                    )

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
                        loud_windows = processor.current_loud_window_count
                        max_rms = processor.current_max_rms
                        if loud_windows >= BARGE_IN_MIN_LOUD_WINDOWS:
                            log.info(
                                "Sustained speech confirmed (%.0fms, loud_windows=%d, max_rms=%.4f) — firing barge-in!",
                                elapsed_ms,
                                loud_windows,
                                max_rms,
                            )
                            await session.cancel(ws)
                            pending_barge_in_start = None
                            # Keep current VAD state — user is still speaking.
                            # The speech already collected will complete naturally.
                            continue
                        # Not enough sustained loudness. Likely a spike from
                        # TTS echo / keypress / cough rather than real speech.
                        # Do NOT fire barge-in. Leave pending_barge_in_start set
                        # so we can re-evaluate on the next chunk — if the user
                        # DOES start speaking, the counter will climb past the
                        # threshold and we'll fire then.
                        # Log only once every ~500ms of elapsed to avoid spam.
                        if int(elapsed_ms) % 500 < 50:
                            log.info(
                                "Barge-in candidate not sustained (%.0fms, loud_windows=%d < %d, max_rms=%.4f) — likely spike, holding",
                                elapsed_ms,
                                loud_windows,
                                BARGE_IN_MIN_LOUD_WINDOWS,
                                max_rms,
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

                    audio_events_task: asyncio.Task | None = None
                    try:
                        stt_t0 = time.monotonic()
                        # Phase U.3 follow-up #3 — fire audio-event
                        # classification off as a background task BEFORE
                        # awaiting Whisper, instead of the prior sequential
                        # transcribe → classify flow. YAMNet inference
                        # (~280 ms CPU via `asyncio.to_thread`) can complete
                        # alongside Groq Whisper (typical ~300 ms API call)
                        # so by the time the transcript lands the audio
                        # events are usually also ready. Removes ~200-280 ms
                        # of TTFA drag on every turn. Silent no-op path
                        # (returns []) on missing PCM / classifier errors.
                        pcm = processor.last_speech_pcm
                        processor.last_speech_pcm = None
                        if pcm is not None:
                            audio_events_task = asyncio.create_task(
                                audio_events.classify_segment(pcm)
                            )
                            # Fire-and-forget: store result on session when
                            # done; injected into the NEXT turn's Claude prompt.
                            # This removes the entire YAMNet await (~900-1400 ms
                            # on longer utterances) from the TTFA critical path.
                            # A cough detected in turn N reaches Claude in turn
                            # N+1 ("I noticed you coughed a moment ago…"), which
                            # is imperceptible for welfare signals. D102 adds
                            # _pending_audio_events_context to Session.
                            def _store_audio_events(task, _sess=session):
                                if task.cancelled():
                                    return
                                exc = task.exception()
                                if exc is not None:
                                    log.debug("Audio events task failed (%s)", type(exc).__name__)
                                    return
                                events = task.result()
                                if events:
                                    _sess._pending_audio_events_context = (
                                        audio_events.format_events_for_prompt(events)
                                    )
                                    log.info(
                                        "[AUDIO-EVENTS] detected: %s",
                                        ", ".join(f"{e.tag}:{e.confidence:.2f}" for e in events),
                                    )
                            audio_events_task.add_done_callback(_store_audio_events)
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

                                # Pull audio-events context detected during the
                                # PREVIOUS turn's YAMNet run (fire-and-forget,
                                # stored by _store_audio_events callback above).
                                # Consume and clear so each event is injected once.
                                audio_events_context = session._pending_audio_events_context
                                session._pending_audio_events_context = ""

                                # Launch response as concurrent task (non-blocking).
                                # Pass through speech_end_ts + stt_latency_ms so
                                # Session can compute TTFA and the per-stage
                                # rollup at session-summary time (see D85).
                                session.start_response(
                                    ws, engine, text, openai_key,
                                    turn_trace=turn_trace,
                                    speech_end_ts=processor.last_speech_end_ts,
                                    stt_latency_ms=stt_latency_ms,
                                    audio_events_context=audio_events_context,
                                )
                            else:
                                await Session._safe_send_json(ws,{"type": "error", "message": "Anthropic API key not set"})
                        else:
                            log.info("Empty transcript, skipping")
                            # If audio-events was pre-dispatched for this
                            # segment but transcript was empty/filtered,
                            # cancel the task so we don't run YAMNet for
                            # nothing (saves CPU, doesn't affect TTFA).
                            if audio_events_task is not None and not audio_events_task.done():
                                audio_events_task.cancel()
                            if not session.is_audible:
                                await Session._safe_send_json(ws,{"type": "status", "state": "listening"})
                    except asyncio.TimeoutError:
                        log.error("Transcription timed out")
                        if audio_events_task is not None and not audio_events_task.done():
                            audio_events_task.cancel()
                        await Session._safe_send_json(ws,{
                            "type": "error",
                            "message": "I didn't catch that — please try again.",
                        })
                        if not session.is_audible:
                            await Session._safe_send_json(ws,{"type": "status", "state": "listening"})
                    except APIKeyError as e:
                        log.error("Transcription auth error: %s", e)
                        if audio_events_task is not None and not audio_events_task.done():
                            audio_events_task.cancel()
                        await Session._safe_send_json(ws, {
                            "type": "error",
                            "message": str(e),
                            "open_settings": True,
                        })
                    except Exception as e:
                        # Check for Groq authentication errors (groq SDK raises
                        # groq.AuthenticationError on 401, which is an httpx-
                        # based exception whose class name we check here to avoid
                        # a hard import of the groq package at module level).
                        type_name = type(e).__name__
                        if type_name == "AuthenticationError" or (
                            hasattr(e, "status_code") and getattr(e, "status_code", None) == 401
                        ):
                            log.error("Groq authentication error: %s", e)
                            if audio_events_task is not None and not audio_events_task.done():
                                audio_events_task.cancel()
                            await Session._safe_send_json(ws, {
                                "type": "error",
                                "message": "Please check your Groq API key in the settings panel (gear icon).",
                                "open_settings": True,
                            })
                        else:
                            # Log the real error server-side but send a safe,
                            # non-leaky message to the client.
                            log.error("Transcription failed: %s", e)
                            if audio_events_task is not None and not audio_events_task.done():
                                audio_events_task.cancel()
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
            import statistics as _stats  # stdlib only — deferred import keeps startup slim
            duration_s = time.monotonic() - session_start_ts

            def _p50_p95(samples: list[float]) -> tuple[float, float]:
                """Return (P50, P95) rounded to 1 dp, with max-fallback
                when <20 samples to avoid a bogus percentile on short
                sessions. Empty → (0, 0)."""
                if not samples:
                    return 0, 0
                p50 = round(_stats.median(samples), 1)
                if len(samples) >= 20:
                    p95 = round(_stats.quantiles(samples, n=20)[-1], 1)
                else:
                    p95 = round(max(samples), 1)
                return p50, p95

            latencies = session.stats.get("turn_latencies_ms", [])
            avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else 0
            p50_turn, p95_turn = _p50_p95(latencies)

            # Per-stage percentiles — D85. Whole-turn metric is retained
            # for backwards-compat with any existing Langfuse dashboards;
            # the new stage fields tell you WHERE the latency actually
            # went. `ttfa_*` is the brief's real SLA number.
            ttfa_p50, ttfa_p95 = _p50_p95(session.stats.get("ttfa_ms_samples", []))
            stt_p50, stt_p95 = _p50_p95(session.stats.get("stt_ms_samples", []))
            ttft_p50, ttft_p95 = _p50_p95(
                session.stats.get("claude_ttft_ms_samples", [])
            )
            tts_fb_p50, tts_fb_p95 = _p50_p95(
                session.stats.get("tts_first_byte_ms_samples", [])
            )

            summary = {
                "session_id": session_id,
                "duration_seconds": round(duration_s, 1),
                "total_turns": session.stats["total_turns"],
                "completed_turns": session.stats["completed_turns"],
                "barge_in_count": session.stats["barge_in_count"],
                "fall_count": session.stats["fall_count"],
                "vision_calls": session.stats["vision_calls"],
                # Whole-turn (speech_end → last TTS byte handed to WS).
                # Kept for backwards compat; see D85 for why this is
                # different from — and bigger than — TTFA.
                "avg_turn_latency_ms": avg_latency,
                "p50_turn_latency_ms": p50_turn,
                "p95_turn_latency_ms": p95_turn,
                "max_turn_latency_ms": max(latencies) if latencies else 0,
                "min_turn_latency_ms": min(latencies) if latencies else 0,
                # Stage breakdown — the actually-diagnosable metrics.
                # TTFA = speech_end → first audio byte on the wire;
                # the brief's <1.5s SLA target applies to this number.
                "ttfa_p50_ms": ttfa_p50,
                "ttfa_p95_ms": ttfa_p95,
                "stt_p50_ms": stt_p50,
                "stt_p95_ms": stt_p95,
                "claude_ttft_p50_ms": ttft_p50,
                "claude_ttft_p95_ms": ttft_p95,
                "tts_first_byte_p50_ms": tts_fb_p50,
                "tts_first_byte_p95_ms": tts_fb_p95,
            }
            log.info("Session summary: %s", summary)
            telemetry.log_session_summary(lf, session_id, summary)
            telemetry.flush(lf)
        except Exception as e:
            log.debug("Session summary telemetry skipped: %s", e)

        # Cancel proactive check-in loop
        if checkin_task is not None and not checkin_task.done():
            checkin_task.cancel()

        # Belt-and-suspenders final save of UserContext (Phase E). The
        # in-session save hook in Session._extract_user_facts writes after
        # each fact update, but if the client disconnected mid-extraction
        # the latest update might not have landed yet. Runs synchronously
        # on the way out since there's no event loop left to off-load to.
        if session.resident_id and session.user_context:
            try:
                save_user_context(session.resident_id, session.user_context.to_dict())
            except Exception as e:
                log.debug("[MEMORY] Final flush failed: %s", type(e).__name__)

        # Phase N — return the PTZ camera to a neutral pose so the next
        # session starts centred. Silent no-op when PTZ isn't available.
        try:
            session._ptz.center()
        except Exception as e:
            log.debug("[PTZ] center failed on cleanup (%s)", type(e).__name__)

        if engine is not None:
            try:
                await engine.aclose()
            except Exception:
                pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
