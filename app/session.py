"""Per-connection session state and barge-in coordinator.

Parallel TTS pipeline:
  Producer task reads the Claude stream and launches a synthesize() task
  for each sentence boundary. Consumer task awaits those TTS tasks in
  FIFO order and sends audio to the WebSocket. Both run concurrently via
  asyncio.gather() so sentence N+1's OpenAI call overlaps with sentence N's
  playback. This finally lets HTTP/2 multiplexing do useful work.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.conversation import ConversationEngine, MODEL as CLAUDE_MODEL
from app.memory import save_user_context
from app.ptz import PTZController
from app.tts import synthesize, stream_sentence
from app.tts_cache_store import record_phrase
from app.vision import VisionBuffer, analyze_frames
from app import telemetry

log = logging.getLogger("abide.session")

_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')

# Cap on the rolling list of per-turn latencies kept in Session.stats.
# Long sessions shouldn't grow an unbounded list just for telemetry.
_MAX_TURN_LATENCY_SAMPLES = 100

# Maxsize for the producer/consumer TTS queue. In practice each turn is
# at most ~5 sentences, so the queue rarely holds more than 3 items. The
# cap is a defensive guard against runaway Claude output.
_TTS_QUEUE_MAXSIZE = 32

# Maximum wall-clock duration the `client_playing` flag is trusted.
_CLIENT_PLAYING_STALENESS_S = 60.0

# Minimum seconds between vision-reactive responses. Without this,
# a sustained wave would trigger a response every ~2.4s (vision cycle).
_VISION_REACT_COOLDOWN_S = 15.0

# Whether an observed scene is "noteworthy" is decided by the vision
# model itself (see SceneResult.noteworthy in app/vision.py). Previously
# this was a hard-coded keyword allowlist here — `_REACTIVE_ACTIVITIES`
# like {"waving", "standing up", ...}. The list had to grow by hand for
# every new activity and couldn't judge intent from context. Now the
# vision model returns a semantic flag alongside the activity, grounded
# in its own motion_cues reasoning. We just check the flag.

# Minimum seconds of user silence before a vision-reactive trigger
# may fire. Matches the "WHEN USER IS SILENT (10+ seconds)" rule in
# conversation.py's SYSTEM_PROMPT. Previously 3s, which let Abide
# interrupt the user during brief conversational pauses.
_VISION_REACT_MIN_SILENCE_S = 10.0

# Phase K — out-of-frame welfare check.
# Consecutive "Out of frame" vision cycles before Abide verbally checks
# in. 3 cycles × ~2.4 s ≈ 7 s of absence (was ~11 s before the Phase
# S.3 follow-up retune to a 2.4 s vision cycle) — short enough for
# Abide to feel present when the user briefly steps out, long enough
# to skip routine brief trips (bending to pick something up, turning
# to another person). Three-consecutive-cycles gate still prevents
# single-frame misclassification false-fires. Works alongside the
# frontend PTZ subject-follow: when the camera can't keep the person
# in view despite tracking, Abide speaks up.
_OUT_OF_FRAME_CHECKIN_THRESHOLD = 3
# Cooldown between successive welfare nudges so a sustained absence
# doesn't spam the user on their return, and so Abide doesn't chain
# multiple check-ins if the person lingers out of frame.
_OUT_OF_FRAME_CHECKIN_COOLDOWN_S = 30.0

# Phase U follow-up — minimum seconds between consecutive client-side
# fall_alert events. A buggy or adversarial client could otherwise spam
# alerts at pose-loop rate (~15-30 Hz), inflating `fall_count` and
# flashing the red UI banner continuously. 10 s matches the out-of-
# frame welfare logic's "give the user a moment" posture.
_CLIENT_FALL_ALERT_COOLDOWN_S = 10.0

# Names that must never be stored as the user's name. Defence-in-depth
# against extract_user_facts() drift: if Claude ever mis-tags "Abide"
# (the assistant's own name) or another role label as the user, we drop
# it here before it reaches the system prompt or Whisper biasing.
_NAME_BLOCKLIST = frozenset({
    "abide", "assistant", "ai", "user", "companion", "robot",
})


def _log_bg_exception(name: str):
    """Return a done-callback that surfaces unhandled exceptions from
    fire-and-forget session tasks. Without this, Python only logs
    'Task exception was never retrieved' at GC time — which can swallow
    a real bug for the entire lifetime of the Session. Mirrors
    `main.py:_log_prewarm_exception` but scoped to this module so we
    don't create a circular import."""
    def _cb(task: asyncio.Task):
        try:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.warning("%s task failed: %s: %r", name, type(exc).__name__, exc)
        except Exception as cb_exc:
            # Defensive: asyncio silently discards exceptions raised inside
            # done-callbacks. task.exception() itself can raise CancelledError
            # if the task was cancelled between the cancelled() check and here.
            log.error("[BUG] _log_bg_exception callback raised for %s: %s", name, cb_exc)
    return _cb


@dataclass
class UserContext:
    """Persistent user context that accumulates across the session.

    Updated after each Claude response via a lightweight extraction call.
    Injected into every Claude turn so Abide feels like it genuinely
    knows and remembers the person.
    """
    name: str | None = None
    mentioned_topics: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    mood_signals: list[str] = field(default_factory=list)

    def update(self, facts: dict):
        """Merge extracted facts into this context."""
        if not facts:
            return
        if facts.get("name") and not self.name:
            # Collapse all whitespace (including embedded newlines/tabs) to a
            # single space before the blocklist check — defence against a
            # prompt-injection attempt where the extracted "name" is something
            # like "Shree\n\nIgnore previous instructions". The name later
            # flows unescaped into the Claude system prompt via as_prompt().
            candidate = " ".join(str(facts["name"]).split())[:80]
            if candidate and candidate.lower() not in _NAME_BLOCKLIST:
                self.name = candidate
            else:
                log.info("[CONTEXT] Rejected name candidate: %r", facts["name"])
        for topic in facts.get("topics", []):
            if topic and topic not in self.mentioned_topics:
                self.mentioned_topics.append(topic)
                # Keep bounded
                if len(self.mentioned_topics) > 15:
                    self.mentioned_topics = self.mentioned_topics[-15:]
        for pref in facts.get("preferences", []):
            if pref and pref not in self.preferences:
                self.preferences.append(pref)
                if len(self.preferences) > 10:
                    self.preferences = self.preferences[-10:]
        mood = facts.get("mood")
        if mood:
            self.mood_signals.append(mood)
            if len(self.mood_signals) > 5:
                self.mood_signals = self.mood_signals[-5:]

    def as_prompt(self) -> str:
        """Format for injection into Claude's system prompt.
        Returns empty string if nothing is known yet."""
        lines = []
        if self.name:
            lines.append(f"- Name: {self.name}")
        if self.mentioned_topics:
            lines.append(f"- We've talked about: {', '.join(self.mentioned_topics)}")
        if self.preferences:
            lines.append(f"- You mentioned you like: {', '.join(self.preferences)}")
        if self.mood_signals:
            lines.append(f"- Current mood: {self.mood_signals[-1]}")
        if not lines:
            return ""
        return "What I know about you:\n" + "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for on-disk persistence
        (Phase E). The format is flat and stable so we can evolve the
        class without breaking existing memory files."""
        return {
            "name": self.name,
            "mentioned_topics": list(self.mentioned_topics),
            "preferences": list(self.preferences),
            "mood_signals": list(self.mood_signals),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UserContext":
        """Hydrate from a dict previously produced by ``to_dict``.
        Defensively coerces types and caps list lengths so a corrupt or
        maliciously-edited memory file can't blow up the session or
        balloon into Claude's prompt. Unknown keys are ignored.

        Re-applies the _NAME_BLOCKLIST as defence-in-depth: if a prior
        run mis-extracted "Abide" as the user's name and persisted it
        to disk, we drop it on load rather than injecting it back into
        every Claude turn."""
        if not isinstance(data, dict):
            return cls()
        raw_name = data.get("name")
        if isinstance(raw_name, str):
            # Same whitespace-collapse as update() so a tampered memory
            # file can't smuggle newlines through hydrate() back into
            # the Claude system prompt on next connect.
            cleaned_name = " ".join(raw_name.split())[:80]
        else:
            cleaned_name = ""
        if cleaned_name and cleaned_name.lower() not in _NAME_BLOCKLIST:
            name = cleaned_name
        else:
            name = None

        def _clean_str_list(raw, cap: int) -> list[str]:
            if not isinstance(raw, list):
                return []
            out = []
            seen = set()
            for item in raw:
                if not isinstance(item, str):
                    continue
                s = item.strip()
                if not s or s in seen:
                    continue
                seen.add(s)
                out.append(s[:120])
                if len(out) >= cap:
                    break
            return out

        return cls(
            name=name,
            mentioned_topics=_clean_str_list(data.get("mentioned_topics"), 15),
            preferences=_clean_str_list(data.get("preferences"), 10),
            mood_signals=_clean_str_list(data.get("mood_signals"), 5),
        )

    def snapshot(self) -> dict:
        """Flat dict for the UI's "What Abide remembers" panel.
        Fields here are the ones the panel renders — keep it minimal
        and deterministic so the client doesn't have to interpret."""
        return {
            "name": self.name,
            "topics": list(self.mentioned_topics),
            "preferences": list(self.preferences),
            "mood": self.mood_signals[-1] if self.mood_signals else None,
        }

class Session:
    """Manages response pipeline as a concurrent task for barge-in support."""

    def __init__(self):
        self._response_task: asyncio.Task | None = None
        self._cancelled = False
        # Timestamp (monotonic) of the most recently sent TTS chunk.
        # Used for echo suppression: ignore VAD for POST_TTS_COOLDOWN_MS after.
        self.last_tts_send_ts: float | None = None

        # Client playback tracking. Set True when the browser sends
        # {"type":"playback_start"} (it has decoded + queued audio and
        # started an AudioBufferSourceNode) and False on playback_end.
        # Used by the barge-in gate in main.py to fire interrupts during
        # the window where the server's response task has finished but
        # the client is still playing buffered TTS audio through its
        # speakers. Without this signal, multi-sentence responses cannot
        # be interrupted after the last TTS chunk leaves the server.
        #
        # Exposed as a property below so assignment records a timestamp
        # and `is_audible` can clamp stale True values — defence against
        # a buggy client that fails to send playback_end.
        self._client_playing: bool = False
        self._client_playing_since: float | None = None

        # User context — accumulates facts about the user across the session.
        # On connect, main.py hydrates this from ./memory/<resident_id>.json
        # if a file exists (Phase E cross-session memory), so returning
        # users have their name / topics / preferences / mood-signals
        # restored. First-time visitors start with an empty UserContext.
        self.user_context: UserContext = UserContext()

        # Browser-generated per-device identifier for the memory file.
        # Set by main.py's config handler after validation against
        # app.memory._ID_RE. None until config arrives, and wiped on
        # Forget-me so subsequent fact-extraction saves don't resurrect
        # the deleted record.
        self.resident_id: str | None = None

        # Browser-reported timezone offset in minutes (JS
        # `getTimezoneOffset()`: UTC = local + offset, so offset=360 means
        # UTC-6 Central Daylight Time). Used by `time_of_day_context()` to
        # compute local hour so Claude's greetings and check-ins reference
        # the time of day naturally ("Good morning", "It's getting late").
        # Stays None until the first `config` WS message arrives; all
        # time-aware paths fall back to generic wording when None.
        self.tz_offset_minutes: int | None = None

        # Timestamp of last user speech (for proactive check-in timing)
        self.last_user_speech_ts: float = time.monotonic()

        # Engine/key refs for vision-reactive triggers. Set by main.py
        # in the config handler so _run_vision() can call start_response().
        self._engine_ref: ConversationEngine | None = None
        self._openai_key_ref: str | None = None

        # Cooldown for vision-reactive responses (prevents spam)
        self._last_vision_react_ts: float = 0.0

        # Queued reactive activity — set when a reactive gesture is detected
        # while Abide is busy talking. Consumed at the end of _run_response().
        self._pending_reactive_activity: str | None = None

        # Audio-events context from the most recently completed YAMNet
        # classification. Set by main.py's done-callback (fire-and-forget),
        # consumed and cleared by start_response() so it is injected into
        # the NEXT turn rather than blocking the current one.
        self._pending_audio_events_context: str = ""

        # Vision state
        self.vision_buffer: VisionBuffer = VisionBuffer()
        self._frame_task: asyncio.Task | None = None

        # Phase K — out-of-frame welfare check state.
        # `_consecutive_out_of_frame` counts successive vision cycles
        # that returned "Out of frame." as the activity. When it crosses
        # `_OUT_OF_FRAME_CHECKIN_THRESHOLD`, Abide fires a proactive
        # one-sentence check-in. `_last_out_of_frame_checkin_ts` gates
        # repeat nudges to no more than once per cooldown window.
        self._consecutive_out_of_frame: int = 0
        self._last_out_of_frame_checkin_ts: float = 0.0

        # Phase N — PTZ subject-follow controller. Discovers a
        # pan/tilt-capable camera at construction time (Logitech MeetUp
        # on Windows) via DirectShow; silent no-op elsewhere. On each
        # vision result with a non-null bbox we nudge the camera toward
        # the subject so it follows them around the room.
        self._ptz: PTZController = PTZController()
        # Phase U.2 — browser-local MediaPipe pose landmarks drive a
        # second, higher-frequency PTZ nudge source than the 2.4 s
        # GPT-4.1-mini vision cycle. Client emits `face_bbox` up to
        # ~5 Hz; we rate-limit server-side before touching DirectShow so
        # the property-set calls don't queue up. `_last_face_nudge_ts`
        # is the monotonic time of the most recent dispatched nudge.
        self._last_face_nudge_ts: float = 0.0
        # Phase U follow-up — cooldown timer for client-side pose-heuristic
        # fall alerts so a buggy/malicious client can't spam the red
        # banner + bump `fall_count`.
        self._last_client_fall_ts: float = 0.0
        # Fall-alert state: when a fall is detected, this holds the text
        # until it has been surfaced to Claude as urgent context for one
        # response turn. After that turn, cleared.
        self._pending_fall_alert: str | None = None

        # ── Telemetry (Langfuse) ──
        # `telemetry_client` is the shared Langfuse client (None if disabled).
        # `telemetry_session_id` is set by main.py on connect.
        # `_current_turn_trace` is the active turn's Langfuse trace handle,
        # created in main.py right after STT and passed in via start_response.
        self.telemetry_client = None
        self.telemetry_session_id: str | None = None
        self._current_turn_trace = None
        # Rolling session stats, flushed to Langfuse on disconnect.
        # `turn_latencies_ms` is the whole-turn metric (speech_end → last
        # TTS byte handed to WS). `ttfa_ms_samples` is the brief's real
        # SLA — speech_end → first audio byte on the wire (D85 note).
        # The per-stage lists (STT / Claude TTFT / TTS first-byte) let
        # main.py's session-summary finally block roll up P50/P95 per
        # stage so a slow session is diagnosable end-to-end, not just at
        # the aggregate level.
        self.stats = {
            "total_turns": 0,
            "completed_turns": 0,
            "barge_in_count": 0,
            "fall_count": 0,
            "vision_calls": 0,
            "turn_latencies_ms": [],
            "ttfa_ms_samples": [],
            "stt_ms_samples": [],
            "claude_ttft_ms_samples": [],
            "tts_first_byte_ms_samples": [],
        }
        # Per-turn state populated by start_response and consumed by
        # _run_response's finally block. Cleared at the end of every turn.
        self._current_turn_speech_end_ts: float | None = None
        self._current_turn_stt_ms: float | None = None

    @property
    def is_responding(self) -> bool:
        """True while the server-side response task is still producing
        content (streaming Claude tokens or synthesizing TTS)."""
        return self._response_task is not None and not self._response_task.done()

    @property
    def client_playing(self) -> bool:
        """Whether the browser most recently told us it is playing audio.
        Transitions are driven by playback_start / playback_end messages
        from the frontend (handled in main.py)."""
        return self._client_playing

    @client_playing.setter
    def client_playing(self, value: bool) -> None:
        # Record the monotonic timestamp when this flips True so
        # `is_audible` can clamp stale values from a buggy client.
        if value and not self._client_playing:
            self._client_playing_since = time.monotonic()
        elif not value:
            self._client_playing_since = None
        self._client_playing = bool(value)

    @property
    def is_audible(self) -> bool:
        """True if the user can currently hear Abide — either the server
        is still producing a response, OR the browser is still playing
        buffered TTS audio from a previous response. This is the correct
        gate for barge-in: we want to fire an interrupt any time there's
        sound coming out of the speakers, not only during the narrow
        window when the server's producer/consumer pipeline is active.

        Defence in depth: if `client_playing` has been True for longer
        than _CLIENT_PLAYING_STALENESS_S, we assume the client is buggy
        (failed to send playback_end) and force-clear the flag. Prevents
        a stuck state from making every subsequent user utterance look
        like a barge-in.
        """
        if self.is_responding:
            return True
        if self._client_playing:
            since = self._client_playing_since
            if since is not None and (time.monotonic() - since) > _CLIENT_PLAYING_STALENESS_S:
                log.warning(
                    "client_playing stuck True for >%.0fs — force-clearing (likely buggy client)",
                    _CLIENT_PLAYING_STALENESS_S,
                )
                self._client_playing = False
                self._client_playing_since = None
                return False
            return True
        return False

    def mark_tts_sent(self):
        self.last_tts_send_ts = time.monotonic()

    def _local_hour(self) -> int | None:
        """Compute the user's current local hour (0-23) from the server
        UTC clock + the browser-reported timezone offset. Returns None
        if the browser hasn't reported its offset yet."""
        if self.tz_offset_minutes is None:
            return None
        from datetime import datetime, timezone, timedelta
        # JS getTimezoneOffset(): UTC = local + offset_minutes →
        # local = UTC - offset_minutes.
        utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
        local = utc_now - timedelta(minutes=self.tz_offset_minutes)
        return local.hour

    def time_of_day_bucket(self) -> str | None:
        """Return one of {"morning", "afternoon", "evening", "night"} or
        None if the user's timezone hasn't been reported yet. Buckets
        chosen for natural-language greeting/check-in fit, not strict
        clock convention."""
        hour = self._local_hour()
        if hour is None:
            return None
        if 5 <= hour < 12:
            return "morning"
        if 12 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 21:
            return "evening"
        return "night"

    def time_of_day_context(self) -> str:
        """Short context string about the current local time, intended
        for injection into Claude's system prompt. Returns empty when
        the timezone is unknown so Claude falls back to generic wording.

        Kept as one concise line + instruction so it doesn't balloon
        the prompt: ~35 tokens. Claude is told to reference it naturally,
        not shoehorn it into every reply."""
        bucket = self.time_of_day_bucket()
        hour = self._local_hour()
        if bucket is None or hour is None:
            return ""
        h12 = hour % 12 or 12
        ampm = "AM" if hour < 12 else "PM"
        return (
            f"Local time where the user is: {bucket} (around {h12} {ampm}). "
            f"Reference the time of day naturally when it fits — in "
            f"greetings, welfare check-ins, or moments where a friend "
            f"would notice the hour. Do not force it into every reply."
        )

    async def cancel(self, ws: WebSocket):
        """Cancel the current response (if any) and tell the client to
        stop playing buffered audio. Works in two scenarios:

          1. Server is still producing (is_responding is True): set the
             cancellation flag so the producer/consumer pipeline stops
             cleanly, save any partial response to history.
          2. Server is done but client is still playing (client_playing
             is True): we have no task to cancel; we just send barge_in
             to the browser so it stops its Web Audio playback queue.

        In both cases we immediately clear client_playing so subsequent
        VAD events don't re-trigger this same cancel.
        """
        if not self.is_audible:
            return

        was_responding = self.is_responding
        self.stats["barge_in_count"] += 1
        if was_responding:
            log.info("Barge-in triggered — cancelling server response task")
            self._cancelled = True
            # Wait briefly for cooperative cancellation
            try:
                await asyncio.wait_for(asyncio.shield(self._response_task), timeout=0.2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                # If cooperative cancel didn't work fast enough, force it
                if self._response_task and not self._response_task.done():
                    self._response_task.cancel()
        else:
            log.info("Barge-in triggered — stopping client playback (server task already done)")

        # Client will receive this and call stopPlayback() which also
        # sends its own playback_end. Clearing the flag here is the
        # belt-and-suspenders guarantee in case that message races.
        self.client_playing = False
        await self._safe_send_json(ws, {"type": "barge_in"})

    async def say_canned(
        self,
        ws: WebSocket,
        engine: ConversationEngine,
        text: str,
        openai_key: str | None,
    ):
        """Speak a canned phrase — bypass Claude entirely, serve from
        TTS cache if possible. Used for the welcome greeting on connect
        and other fixed utterances. Also appends to conversation history
        so Claude knows what Abide has already said.
        """
        if not openai_key or ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await self._safe_send_json(ws, {"type": "status", "state": "speaking"})
            # Send the text to the transcript
            await self._safe_send_json(ws, {"type": "response_chunk", "text": text})
            # Synthesize (hits TTS cache for canned phrases)
            t0 = time.monotonic()
            audio = await synthesize(text, openai_key, t0)
            if self._cancelled or ws.client_state != WebSocketState.CONNECTED:
                return
            await self._safe_send_bytes(ws, audio)
            self.mark_tts_sent()
            # tts_done signals the frontend's playback worklet that no more
            # audio is coming for this utterance so it can drain and fire
            # playback_end. Must come before response_done.
            await self._safe_send_json(ws, {"type": "tts_done"})
            await self._safe_send_json(ws, {"type": "response_done"})
            # Record in history so Claude knows what was already said
            engine._history.append({"role": "assistant", "content": text})
            await self._safe_send_json(ws, {"type": "status", "state": "listening"})
            log.info("[CANNED] Said %r (%dB)", text[:50], len(audio))
        except Exception as e:
            # Surface the failure to the client so the UI doesn't get stuck
            # showing "speaking" forever. The transcript line we sent above
            # is left in place — the user at least sees what Abide meant to
            # say. Reset status to listening so the mic gate re-opens.
            log.error("say_canned failed: %s", e)
            try:
                await self._safe_send_json(ws, {"type": "response_done"})
                await self._safe_send_json(ws, {"type": "status", "state": "listening"})
            except Exception:
                pass

    def _record_stage_sample(self, key: str, value_ms: float | None) -> None:
        """Append a per-turn stage-latency sample to `self.stats[key]` and
        bound the list to `_MAX_TURN_LATENCY_SAMPLES`. Silent no-op when
        `value_ms` is None (stage didn't run or wasn't measured this turn)."""
        if value_ms is None:
            return
        lst = self.stats.setdefault(key, [])
        lst.append(round(float(value_ms), 1))
        if len(lst) > _MAX_TURN_LATENCY_SAMPLES:
            del lst[0:len(lst) - _MAX_TURN_LATENCY_SAMPLES]

    def start_response(
        self,
        ws: WebSocket,
        engine: ConversationEngine,
        text: str,
        openai_key: str | None,
        turn_trace=None,
        speech_end_ts: float | None = None,
        stt_latency_ms: float | None = None,
        audio_events_context: str = "",
    ):
        """Launch the response+TTS pipeline as a background task.

        `turn_trace` is an optional Langfuse trace handle created by main.py
        right after STT. If provided, Claude and TTS spans are attached to
        it and it is finalized in `_run_response`.

        `speech_end_ts` is the monotonic timestamp of the user's VAD
        speech_end event (from `AudioProcessor.last_speech_end_ts`). Used
        by `_run_response` to compute TTFA — the brief's "<1.5s to first
        audio after user finishes speaking" SLA. None for proactive /
        vision-reactive / welfare-check turns where there was no preceding
        user speech; TTFA is skipped for those turns.

        `stt_latency_ms` is the wall-clock time Groq Whisper took for
        this turn's transcription, measured in main.py. Passed through so
        the per-turn stage-breakdown in the session summary has STT
        latency alongside Claude TTFT and TTS first-byte.

        If a previous response is still running, it is cancelled first to
        prevent orphaned tasks and overlapping audio.
        """
        # Cancel any in-flight response before starting a new one.
        # This prevents orphaned tasks when concurrent callers (STT path,
        # check-in loop, vision-reactive trigger) race to start a response.
        if self._response_task is not None and not self._response_task.done():
            self._cancelled = True
            self._response_task.cancel()
            log.info("Previous response task cancelled before starting new one")
        self._cancelled = False
        self._current_turn_trace = turn_trace
        self._current_turn_speech_end_ts = speech_end_ts
        self._current_turn_stt_ms = stt_latency_ms
        self.stats["total_turns"] += 1
        vision_context = self.vision_buffer.as_context()
        # If there is a pending fall alert, prepend an urgent instruction
        # so Claude's next reply checks in about the fall. Consume the flag
        # so we only inject it once.
        if self._pending_fall_alert:
            urgent = (
                "URGENT SAFETY SIGNAL FROM CAMERA: " + self._pending_fall_alert +
                "\nYour FIRST sentence must gently check if the person is okay, "
                "e.g. 'I noticed you went down — are you alright?'. Stay calm, "
                "do not alarm them, and offer to call someone for help if they "
                "need it."
            )
            vision_context = (urgent + "\n\n" + vision_context).strip()
            self._pending_fall_alert = None
        user_context = self.user_context.as_prompt()
        time_context = self.time_of_day_context()
        self._response_task = asyncio.create_task(
            self._run_response(
                ws, engine, text, openai_key,
                vision_context, user_context, time_context,
                audio_events_context,
            )
        )

    def _is_reactive_change(self, result) -> bool:
        """Return True if the new scene is worth a proactive reaction.

        Two conditions must hold:
          1. The vision model flagged the scene as `noteworthy` — its own
             semantic judgment that this is the kind of event a friend
             would stop and remark on (waving, standing up, dancing,
             etc.) vs routine behavior (sitting, talking with gestures).
          2. The activity text has actually CHANGED since the previous
             observation. Without this, a sustained wave would re-trigger
             on every 2.4s vision cycle. The cooldown in _run_vision is
             a second layer of protection; this is belt-and-suspenders.

        NOTE: called AFTER vision_buffer.append(), so `result` is already
        the latest entry. We compare against the second-to-last entry to
        detect actual changes.
        """
        if not getattr(result, "noteworthy", False):
            return False
        entries = self.vision_buffer.entries
        if len(entries) >= 2:
            prev = entries[-2].result.activity.lower()
            if result.activity.lower() == prev:
                return False
        return True

    def process_frames(
        self,
        frames_b64: list[str],
        openai_key: str | None,
        ws: WebSocket,
    ):
        """Kick off a vision analysis for a short frame sequence.

        Frames are passed as pre-validated base64 strings (the browser
        already encoded them; main.py validates the b64 character class
        and size). Passing strings through avoids a decode/re-encode
        cycle in vision.py — about 20-40 ms per vision cycle.

        Fire-and-forget. Drops the batch if a previous analysis is still
        in flight — we don't queue, we don't want cost explosion if the
        API is slow.
        """
        if not openai_key or not frames_b64:
            return
        if self._frame_task is not None and not self._frame_task.done():
            # Previous analysis still in flight — drop this batch.
            return
        self._frame_task = asyncio.create_task(
            self._run_vision(frames_b64, openai_key, ws)
        )
        # `_run_vision` wraps its full body in try/except, so in the
        # current code nothing should escape — but a future refactor
        # might move work outside that wrapper. Defensive done-callback
        # surfaces any unhandled exception instead of letting Python's
        # GC print "Task exception was never retrieved" long after the
        # vision pipeline has moved on. Cheap insurance.
        self._frame_task.add_done_callback(_log_bg_exception("vision"))

    async def _run_vision(
        self,
        frames_b64: list[str],
        openai_key: str,
        ws: WebSocket,
    ):
        """Analyze a frame sequence and push the result to buffer + WS.

        Also detects fall events and raises both a WS alert and a pending
        urgent-context flag consumed by the next response turn.
        """
        try:
            prior = self.vision_buffer.recent_texts()
            with telemetry.Timer() as tmr:
                result = await analyze_frames(frames_b64, openai_key, prior)
            if not result or not result.activity:
                return
            self.vision_buffer.append(result)
            self.stats["vision_calls"] += 1

            # Telemetry: one standalone trace per vision call. Approximate
            # byte count from b64 length (a decoded jpeg is ~3/4 of its
            # base64 length) to avoid decoding just for a log number.
            total_bytes = sum(len(s) for s in frames_b64 if s) * 3 // 4
            telemetry.observe_vision(
                self.telemetry_client,
                self.telemetry_session_id or "unknown",
                num_frames=len(frames_b64),
                image_bytes=total_bytes,
                activity=result.activity,
                bbox=result.bbox,
                latency_ms=tmr.ms,
                is_fall=result.is_fall,
            )

            # Fall-alert path: stash for next turn + push a WS alert now.
            if result.is_fall:
                self._pending_fall_alert = (
                    result.activity.replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ").strip()
                )
                self.stats["fall_count"] += 1
                log.warning("FALL detected from vision: %s", result.activity)
                await self._safe_send_json(
                    ws,
                    {
                        "type": "alert",
                        "level": "fall",
                        "text": result.activity,
                    },
                )

            await self._safe_send_json(
                ws,
                {
                    "type": "scene",
                    "text": result.activity,
                    "bbox": result.bbox,
                    "fall": result.is_fall,
                },
            )

            # Vision-reactive trigger: react proactively when the vision
            # model flags the scene as `noteworthy` AND the activity has
            # actually changed since the last observation. Falls have
            # their own dedicated urgent-context path above.
            if (
                not result.is_fall
                and self._engine_ref is not None
                and self._is_reactive_change(result)
                and time.monotonic() - self._last_vision_react_ts >= _VISION_REACT_COOLDOWN_S
            ):
                silence = time.monotonic() - self.last_user_speech_ts
                if silence > _VISION_REACT_MIN_SILENCE_S:  # don't interrupt active conversation
                    if (
                        not self.is_responding
                        and not self.is_audible
                        and ws.client_state == WebSocketState.CONNECTED
                    ):
                        # Abide is free — react immediately
                        self._last_vision_react_ts = time.monotonic()
                        self._pending_reactive_activity = None
                        log.info(
                            "[VISION-REACT] Activity '%s' detected — triggering proactive response",
                            result.activity,
                        )
                        react_text = (
                            "[System: You just noticed the user doing something — "
                            f"'{result.activity}'. React to it naturally in 1 sentence. "
                            "Be warm and conversational, like noticing what a friend is doing.]"
                        )
                        self.start_response(
                            ws, self._engine_ref, react_text, self._openai_key_ref,
                        )
                    else:
                        # Abide is busy talking — queue for after response
                        self._pending_reactive_activity = result.activity
                        log.info(
                            "[VISION-REACT] Activity '%s' queued (Abide is busy)",
                            result.activity,
                        )

            # Phase K — out-of-frame welfare check. The frontend PTZ
            # subject-follow tries to keep the person centred, but if
            # the camera's mechanical range is exhausted (they walked
            # too far left/right) or they step out entirely, vision
            # reports "Out of frame." Once that persists past the
            # threshold, Abide speaks up with a gentle "are you still
            # there?" — the same mechanism the brief's demo video uses
            # ("I see you haven't moved in a while"). All the normal
            # guards apply: user must have been silent, Abide must not
            # be mid-response, and there's a cooldown so we don't chain
            # multiple nudges during one sustained absence.
            activity_lower = (result.activity or "").lower()
            if activity_lower.startswith("out of frame"):
                self._consecutive_out_of_frame += 1
            else:
                self._consecutive_out_of_frame = 0

            if (
                self._consecutive_out_of_frame >= _OUT_OF_FRAME_CHECKIN_THRESHOLD
                and self._engine_ref is not None
                and time.monotonic() - self._last_out_of_frame_checkin_ts >= _OUT_OF_FRAME_CHECKIN_COOLDOWN_S
                and time.monotonic() - self.last_user_speech_ts >= _VISION_REACT_MIN_SILENCE_S
                and not self.is_responding
                and not self.is_audible
                and ws.client_state == WebSocketState.CONNECTED
            ):
                self._last_out_of_frame_checkin_ts = time.monotonic()
                # Reset the counter so the same sustained absence doesn't
                # immediately re-trigger on the next vision cycle once
                # the cooldown expires — they have to come back into
                # frame at least briefly for the next window.
                self._consecutive_out_of_frame = 0
                log.info(
                    "[WELFARE] Out-of-frame for %d cycles — checking in",
                    _OUT_OF_FRAME_CHECKIN_THRESHOLD,
                )
                react_text = (
                    "[System: The user has been out of camera view for "
                    "about 10 seconds. Check in on them gently in ONE "
                    "short sentence — e.g., \"I can't see you right now "
                    "— are you still there?\". Keep it warm, not "
                    "alarmist. Do not mention the camera.]"
                )
                self.start_response(
                    ws, self._engine_ref, react_text, self._openai_key_ref,
                )

            # Phase N — PTZ subject-follow. Fire-and-forget off-loop
            # DirectShow call so the main event loop stays responsive.
            # `nudge_to_bbox` silently no-ops when the camera doesn't
            # support pan/tilt (non-MeetUp cameras, Firefox on Mac,
            # Windows without duvc-ctl, etc.).
            if result.bbox is not None and self._ptz.available:
                try:
                    await asyncio.to_thread(self._ptz.nudge_to_bbox, result.bbox)
                except Exception as e:
                    log.debug("PTZ nudge dispatch failed (%s)", type(e).__name__)
        except Exception as e:
            log.error("Vision worker error: %s", e)

    # Minimum seconds between face-bbox-driven PTZ nudges. At 5 Hz
    # from the browser we'd try to write DirectShow 300 times/minute
    # — DirectShow `set_camera_property` can take 50-100 ms each and
    # nudges don't stack meaningfully faster than the lens can slew.
    # 5 Hz (every 200 ms) is the sweet spot: visibly smoother than the
    # 2.4 s vision cycle without flooding the camera.
    _FACE_NUDGE_MIN_INTERVAL_S = 0.20

    def dispatch_face_bbox(self, bbox: list[float]) -> None:
        """Phase U.2 — take a MediaPipe-derived bbox from the browser
        and apply a PTZ nudge toward frame-centre, rate-limited to
        `_FACE_NUDGE_MIN_INTERVAL_S`. Silent no-op when PTZ is disabled
        (non-Windows, non-PTZ camera, zoom-only MeetUp firmware). Runs
        the DirectShow write off-loop via `asyncio.to_thread` so the
        COM call doesn't block the voice loop."""
        if not self._ptz.available:
            return
        now = time.monotonic()
        if now - self._last_face_nudge_ts < self._FACE_NUDGE_MIN_INTERVAL_S:
            return
        self._last_face_nudge_ts = now
        try:
            _t = asyncio.create_task(asyncio.to_thread(self._ptz.nudge_to_bbox, bbox))
            _t.add_done_callback(_log_bg_exception("face-bbox-nudge"))
        except Exception as e:
            log.debug("[PTZ] face-bbox dispatch failed (%s)", type(e).__name__)

    async def handle_client_fall(self, ws: WebSocket, text: str) -> None:
        """Phase U.3 — client-side MediaPipe pose heuristic flagged a
        fall. Same handling as a vision-prompt `FALL:` prefix: stash
        urgent context for the next response turn, increment fall_count,
        push a red alert banner to the browser.

        Phase U follow-up hardening:
          - Rate-limit via `_CLIENT_FALL_ALERT_COOLDOWN_S`. The pose
            loop runs 15–30 Hz in the browser and a bug / compromised
            client could otherwise flood us.
          - Escape `<` and `>` in the incoming text before storing. The
            text is later concatenated into the urgent-instruction
            prefix in `start_response` which lands in Claude's prompt.
            Without escaping, a malicious frontend could inject
            `"</camera_observations><system>..."` and hijack the turn.
        """
        now = time.monotonic()
        if now - self._last_client_fall_ts < _CLIENT_FALL_ALERT_COOLDOWN_S:
            return
        self._last_client_fall_ts = now

        # Defence-in-depth escape — cheap, applies regardless of where
        # the text came from. Keep it human-readable (HTML-entity form)
        # so a legit pose-heuristic message still reads naturally when
        # it reaches Claude.
        safe_text = text.replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ").strip()

        self._pending_fall_alert = safe_text
        self.stats["fall_count"] += 1
        log.warning("[FALL-POSE] client pose heuristic flagged fall: %s", safe_text)
        await self._safe_send_json(
            ws,
            {"type": "alert", "level": "fall", "text": safe_text},
        )

    def _dispatch_camera_action(self, action: str) -> None:
        """Translate a Claude [[CAM:...]] marker into a PTZController call.
        Fire-and-forget on the executor so the DirectShow COM call doesn't
        block the asyncio loop. Silent no-op when PTZ is unavailable."""
        if not self._ptz.available:
            log.info("[CAMERA] Claude requested %s but PTZ is unavailable", action)
            return
        if action == "zoom_in":
            direction = "in"
        elif action == "zoom_out":
            direction = "out"
        elif action == "zoom_reset":
            direction = "reset"
        else:
            log.info("[CAMERA] Unknown camera action %r", action)
            return
        log.info("[CAMERA] Dispatching zoom %s", direction)
        try:
            _t = asyncio.create_task(asyncio.to_thread(self._ptz.zoom, direction))
            _t.add_done_callback(_log_bg_exception("zoom-dispatch"))
        except Exception as e:
            log.debug("[CAMERA] dispatch failed (%s)", type(e).__name__)

    # ── Safe WebSocket send helpers ──
    async def _fire_queued_reaction(self, ws: WebSocket, activity: str):
        """Fire a queued vision-reactive response after a short delay."""
        try:
            await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            return
        if self.is_audible or ws.client_state != WebSocketState.CONNECTED:
            return
        if self._engine_ref is None:
            return
        self._last_vision_react_ts = time.monotonic()
        log.info(
            "[VISION-REACT] Firing queued activity '%s' after response completed",
            activity,
        )
        react_text = (
            "[System: While you were speaking, you noticed the user "
            f"was '{activity}'. Now that you've finished talking, "
            "react to it naturally in 1 sentence.]"
        )
        try:
            self.start_response(ws, self._engine_ref, react_text, self._openai_key_ref)
        except Exception as e:
            log.error("Queued vision-react failed: %s", e)

    async def _extract_user_facts(self, engine: ConversationEngine, ws=None):
        """Fire-and-forget task: extract user facts from the last exchange.

        On successful extraction, also (a) persists the updated
        UserContext to disk off-loop via the executor (Phase E), and
        (b) pushes a ``remember_snapshot`` WS message so the UI's
        "What Abide remembers" panel live-updates.
        """
        try:
            facts = await engine.extract_user_facts()
            if not facts:
                return
            self.user_context.update(facts)
            log.info("[CONTEXT] User context updated: %s", self.user_context.as_prompt()[:120])
            # Persist to ./memory/<resident_id>.json in the executor so
            # we don't block the event loop. Idempotent (full snapshot
            # every call); last-writer-wins on concurrent updates is
            # fine given the monotonic accumulation semantics.
            if self.resident_id:
                try:
                    loop = asyncio.get_running_loop()
                    _save_fut = loop.run_in_executor(
                        None,
                        save_user_context,
                        self.resident_id,
                        self.user_context.to_dict(),
                    )
                    # Surface disk-write failures (permission denied,
                    # disk full, path vanished) instead of letting them
                    # disappear into the executor. Wrapping in
                    # `asyncio.ensure_future` gives us a standard
                    # asyncio future that accepts add_done_callback
                    # uniformly across Python versions.
                    _save_fut.add_done_callback(
                        _log_bg_exception("memory-save")
                    )
                except RuntimeError:
                    # No running loop (shouldn't happen on this path) —
                    # fall back to sync write so we don't drop data.
                    save_user_context(self.resident_id, self.user_context.to_dict())
            # Push the updated snapshot to the UI memory panel.
            if ws is not None:
                await self._safe_send_json(
                    ws,
                    {"type": "remember_snapshot", "context": self.user_context.snapshot()},
                )
        except Exception as e:
            log.info("User fact extraction skipped (%s: %s)", type(e).__name__, e)

    # The response pipeline can outlive the WebSocket (TTS in flight when the
    # client disconnects). Without these guards, late sends raise
    # "Unexpected ASGI message 'websocket.send' after 'websocket.close'".

    @staticmethod
    async def _safe_send_json(ws: WebSocket, data: dict):
        if ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await ws.send_json(data)
        except Exception as e:
            log.debug("WS send_json failed (%s)", type(e).__name__)

    @staticmethod
    async def _safe_send_bytes(ws: WebSocket, data: bytes):
        if ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await ws.send_bytes(data)
        except Exception as e:
            log.debug("WS send_bytes failed (%s)", type(e).__name__)

    async def _run_response(
        self,
        ws: WebSocket,
        engine: ConversationEngine,
        text: str,
        openai_key: str | None,
        vision_context: str = "",
        user_context: str = "",
        time_context: str = "",
        audio_events_context: str = "",
    ):
        """Stream Claude + parallel TTS, checking for cancellation."""
        full_response: list[str] = []
        tts_queue: asyncio.Queue = asyncio.Queue(maxsize=_TTS_QUEUE_MAXSIZE)
        turn_trace = self._current_turn_trace
        t_turn_start = time.monotonic()
        # Phase U.3 follow-up #3 — TTFA gap diagnostic. When a user-speech
        # turn is starting, log the delta from speech_end to this point.
        # Previous instrumentation showed TTFA P50 ~5.6 s but STT + Claude
        # + TTS accounted for only ~2.9 s of it. The gap (~2.7 s) was
        # invisible. This line exposes the "speech_end → _run_response
        # started" slice so we can tell whether the delay is in main.py
        # pre-start_response plumbing (awaited transcribe + audio_events,
        # event-loop contention) or later in the pipeline. Skipped on
        # proactive / vision-reactive turns where speech_end isn't set.
        if self._current_turn_speech_end_ts is not None:
            lag_ms = (t_turn_start - self._current_turn_speech_end_ts) * 1000
            log.info(
                "[TIMING] speech_end \u2192 _run_response started = %.0fms", lag_ms,
            )
        # Stage-latency checkpoints used by the finally block to derive
        # TTFA + per-stage P50/P95 rollups. Shared mutably between the
        # producer (first-sentence-queued) and consumer (first-tts-ready
        # and first-bytes-sent) via dict so we don't need `nonlocal`
        # declarations in the nested functions.
        turn_ck = {
            "first_sentence_queued_ts": None,
            "first_tts_ready_ts": None,
            "first_bytes_sent_ts": None,
        }

        def _make_fill_task(sentence: str, ts: float):
            """Create a per-sentence asyncio.Queue + fill task using stream_sentence.

            The fill task (a background asyncio.Task) calls stream_sentence() and
            pushes each PCM chunk into chunk_q as it arrives. A None sentinel is
            always pushed when the generator exhausts (or on error / cancellation).
            This mirrors the old 'create_task(synthesize(...))' pattern but allows
            the consumer to forward each chunk to the WS immediately, cutting TTFA
            by the Ogg buffering delay (~300-600 ms on the first sentence).
            Subsequent sentences' fill tasks start in parallel while the consumer
            is streaming the current one — same overlap the old Task approach gave.
            """
            chunk_q: asyncio.Queue = asyncio.Queue()

            async def _fill(s=sentence, q=chunk_q, s_ts=ts):
                try:
                    async for pcm in stream_sentence(s, openai_key, s_ts):
                        if self._cancelled:
                            return
                        await q.put(pcm)
                except Exception as exc:
                    log.error("TTS stream error for %r: %s", s[:40], exc)
                finally:
                    q.put_nowait(None)  # sentinel — unbounded queue, never blocks

            return chunk_q, asyncio.create_task(_fill())

        async def producer():
            """Read Claude stream; launch a stream_sentence fill task per sentence."""
            from app.conversation import ConversationError, APIKeyError

            text_buf = ""
            # Phase R — camera-action side-channel. At most one dispatch
            # per turn; we reset on next start_response via the engine's
            # own reset in respond().
            camera_dispatched = False

            # Snapshot history length before respond() so we can roll back
            # the user message engine.respond() appends on a failed attempt.
            history_checkpoint = len(engine._history)

            try:
                await self._safe_send_json(ws, {"type": "status", "state": "thinking"})

                # Retry loop — up to 2 attempts for transient first-token stalls.
                # Only retries when no output was produced (full_response still
                # empty), meaning the timeout fired before any text_delta arrived.
                # Mid-stream stalls (partial output already sent) are NOT retried
                # because concatenating two partial responses produces incoherent
                # speech. APIKeyError is never retried (user must fix the key).
                # Barge-in (self._cancelled) exits cleanly without retry.
                for _attempt in range(2):
                    try:
                        async for chunk in engine.respond(
                            text,
                            vision_context=vision_context,
                            user_context=user_context,
                            time_context=time_context,
                            audio_events_context=audio_events_context,
                        ):
                            if self._cancelled:
                                log.info("Barge-in: stopping Claude stream")
                                break

                            # Phase R — if Claude emitted a [[CAM:...]] marker at
                            # the head of the response, the engine strips it from
                            # the stream and sets last_camera_action. Dispatch
                            # the PTZ action as early as possible so the lens
                            # motion overlaps with Claude's verbal ack.
                            if not camera_dispatched and engine.last_camera_action:
                                self._dispatch_camera_action(engine.last_camera_action)
                                camera_dispatched = True

                            await self._safe_send_json(
                                ws, {"type": "response_chunk", "text": chunk}
                            )
                            full_response.append(chunk)

                            # Only scan the newly-appended region (plus a 1-char
                            # overlap so a terminator at the tail of the prior
                            # chunk + leading whitespace in this chunk still
                            # matches).
                            scan_from = max(0, len(text_buf) - 1)
                            text_buf += chunk

                            match = _SENTENCE_RE.search(text_buf, scan_from)
                            while match:
                                sentence = text_buf[:match.start()].strip()
                                if sentence and openai_key and not self._cancelled:
                                    ts = time.monotonic()
                                    if (
                                        turn_ck["first_sentence_queued_ts"] is None
                                        and self._current_turn_speech_end_ts is not None
                                    ):
                                        pre_sentence_ms = (
                                            ts - self._current_turn_speech_end_ts
                                        ) * 1000
                                        log.info(
                                            "[TIMING] speech_end \u2192 first sentence boundary = %.0fms",
                                            pre_sentence_ms,
                                        )
                                    log.info("[TIMING] Sentence boundary: %r", sentence[:60])
                                    record_phrase(sentence)
                                    chunk_q, fill_task = _make_fill_task(sentence, ts)
                                    tts_queue.put_nowait((sentence, chunk_q, fill_task, ts))
                                    if turn_ck["first_sentence_queued_ts"] is None:
                                        turn_ck["first_sentence_queued_ts"] = ts
                                text_buf = text_buf[match.end():]
                                match = _SENTENCE_RE.search(text_buf)

                        # Final tail (text after the last sentence boundary or
                        # an un-terminated last sentence).
                        text_buf = text_buf.strip()
                        if text_buf and openai_key and not self._cancelled:
                            ts = time.monotonic()
                            if (
                                turn_ck["first_sentence_queued_ts"] is None
                                and self._current_turn_speech_end_ts is not None
                            ):
                                pre_sentence_ms = (
                                    ts - self._current_turn_speech_end_ts
                                ) * 1000
                                log.info(
                                    "[TIMING] speech_end \u2192 first sentence boundary = %.0fms (final tail)",
                                    pre_sentence_ms,
                                )
                            log.info("[TIMING] Final tail: %r", text_buf[:60])
                            record_phrase(text_buf)
                            chunk_q, fill_task = _make_fill_task(text_buf, ts)
                            tts_queue.put_nowait((text_buf, chunk_q, fill_task, ts))

                        # Telemetry: Claude generation with token usage. Done here
                        # (after the stream exits) so we have final usage numbers.
                        try:
                            full_system_prompt = engine.last_system_prompt
                            messages_input = {
                                "system": full_system_prompt,
                                "messages": engine.last_messages_snapshot,
                            }
                            telemetry.observe_claude(
                                turn_trace,
                                model=CLAUDE_MODEL,
                                messages=messages_input,  # type: ignore[arg-type]
                                response_text="".join(full_response),
                                input_tokens=engine.last_input_tokens,
                                output_tokens=engine.last_output_tokens,
                                latency_ms=engine.last_total_ms,
                                first_token_ms=engine.last_first_token_ms,
                            )
                        except Exception as e:
                            log.debug("Claude telemetry skipped: %s", e)

                    except ConversationError as e:
                        # Retry only on first-token stall with no output produced.
                        # APIKeyError subclasses ConversationError — never retry.
                        can_retry = (
                            not isinstance(e, APIKeyError)
                            and not self._cancelled
                            and not full_response
                            and _attempt < 1
                        )
                        if not can_retry:
                            raise
                        log.warning("[RETRY] Claude stall on attempt 1 — retrying in 1s: %s", e)
                        # Roll back the user message respond() appended before stalling.
                        del engine._history[history_checkpoint:]
                        await asyncio.sleep(1.0)
                        await self._safe_send_json(ws, {"type": "status", "state": "thinking"})
                    else:
                        break  # respond() completed — exit retry loop

            finally:
                # Sentinel — tells consumer no more items will arrive
                tts_queue.put_nowait(None)

        async def consumer():
            """Drain per-sentence PCM chunk queues in FIFO order, streaming
            each chunk to the WebSocket as it arrives from OpenAI.

            Each item in tts_queue is (sentence, chunk_q, fill_task, ts).
            chunk_q is fed by the fill_task (a background asyncio.Task calling
            stream_sentence). We forward chunks immediately so the browser's
            playback worklet can start playing the first sentence ~300-600 ms
            before the full sentence audio would have been available under the
            old buffered-opus approach.
            """
            first_tts = True
            while True:
                item = await tts_queue.get()
                if item is None:  # sentinel
                    return

                sentence, chunk_q, fill_task, ts = item

                if self._cancelled:
                    fill_task.cancel()
                    continue

                total_bytes = 0
                t_first_chunk: float | None = None

                # Drain all PCM chunks for this sentence. chunk_q.get() yields
                # either a bytes chunk or None (sentinel = sentence complete).
                while True:
                    try:
                        chunk = await asyncio.wait_for(chunk_q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        log.warning(
                            "[STALL] TTS chunk queue timed out for %r — skipping",
                            sentence[:40],
                        )
                        fill_task.cancel()
                        break

                    if chunk is None:  # sentinel — sentence fully streamed
                        break

                    if self._cancelled:
                        fill_task.cancel()
                        break

                    if not chunk:
                        continue

                    total_bytes += len(chunk)

                    # Stage checkpoint: first bytes ready — earlier than before
                    # because we no longer wait for the full sentence buffer.
                    if t_first_chunk is None:
                        t_first_chunk = time.monotonic()
                        if turn_ck["first_tts_ready_ts"] is None:
                            turn_ck["first_tts_ready_ts"] = t_first_chunk

                    if first_tts:
                        await self._safe_send_json(
                            ws, {"type": "status", "state": "speaking"}
                        )
                        first_tts = False

                    if turn_ck["first_bytes_sent_ts"] is None:
                        turn_ck["first_bytes_sent_ts"] = time.monotonic()

                    await self._safe_send_bytes(ws, chunk)
                    self.mark_tts_sent()

                # Per-sentence telemetry after all chunks delivered.
                if total_bytes > 0 and not self._cancelled:
                    tts_latency_ms = (time.monotonic() - ts) * 1000
                    telemetry.observe_tts(
                        turn_trace,
                        sentence=sentence,
                        audio_bytes=total_bytes,
                        latency_ms=tts_latency_ms,
                    )
                    log.info(
                        "[TIMING] TTS sentence done: %r %.0fms %dB",
                        sentence[:40], tts_latency_ms, total_bytes,
                    )

        try:
            await asyncio.gather(producer(), consumer())

            if not self._cancelled:
                await self._safe_send_json(ws, {"type": "response_done"})
                await self._safe_send_json(ws, {"type": "tts_done"})
            else:
                # Partial assistant text was already saved to history by
                # conversation.py's finally block (D35) when the stream
                # exited via _cancelled break. Do NOT call save_partial()
                # again here — that double-appends the same text to
                # engine._history. See D42 for the regression history.
                log.info(
                    "Barge-in: %d chars streamed before cancel (history saved by engine)",
                    len("".join(full_response)),
                )
                await self._safe_send_json(ws, {"type": "response_done"})

        except asyncio.CancelledError:
            # Hard cancellation — conversation.py's finally block still
            # runs on GeneratorExit / CancelledError propagation, so the
            # partial response is already in engine._history. No need to
            # save_partial() again here.
            _cancel_reason = "user-interrupted" if self._cancelled else "network-drop"
            log.info(
                "Hard cancel (%s): %d chars streamed (history saved by engine)",
                _cancel_reason, len("".join(full_response)),
            )
            await self._safe_send_json(ws, {"type": "response_done"})
        except Exception as e:
            # Log type + repr so the "empty message" pattern we saw at
            # session-shutdown is diagnosable. Some network / anyio
            # exception types stringify to "" (e.g. `ClosedResourceError`)
            # and the prior `"%s", e` line was emitting a blank tail.
            log.error("Response pipeline error: %s: %r", type(e).__name__, e)
            # Use the ConversationError's user-safe message when available
            # (our own typed exception with an already-sanitised string);
            # fall back to a generic message for any other exception type
            # so raw details never leak to the client. This lets the
            # Claude first-token-timeout path surface the friendly
            # "Give me a moment…" wording the engine defined.
            from app.conversation import ConversationError, APIKeyError
            if isinstance(e, APIKeyError):
                user_message = str(e)
                await self._safe_send_json(
                    ws,
                    {
                        "type": "error",
                        "message": user_message,
                        "open_settings": True,
                    },
                )
            elif isinstance(e, ConversationError):
                user_message = str(e)
                await self._safe_send_json(ws, {"type": "error", "message": user_message})
            else:
                user_message = "Something went wrong while I was responding. Let's try again."
                await self._safe_send_json(ws, {"type": "error", "message": user_message})
        finally:
            # Drain any TTS tasks still sitting in the queue. On the happy
            # path the consumer already processed everything up to the
            # sentinel, so the queue is empty and this loop is a no-op.
            # On the error / cancellation path (Claude stream error, hard
            # cancel during barge-in, network failure mid-turn) the
            # asyncio.gather above cancels the consumer before it drains
            # the queue — any TTS tasks enqueued but not yet awaited would
            # continue running in the background, completing an OpenAI TTS
            # API call whose audio never gets played. Cancelling them here
            # saves ~1-3 wasted requests per mid-stream error.
            while True:
                try:
                    leftover = tts_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if leftover is None:
                    continue  # sentinel; keep draining in case consumer died first
                _, _leftover_chunk_q, leftover_fill_task, _ = leftover
                if not leftover_fill_task.done():
                    leftover_fill_task.cancel()

            # End the turn trace with summary metadata + update session stats
            total_turn_ms = (time.monotonic() - t_turn_start) * 1000
            try:
                telemetry.end_turn_trace(
                    turn_trace,
                    response_text="".join(full_response),
                    was_interrupted=self._cancelled,
                    total_ms=total_turn_ms,
                )
            except Exception as e:
                # Programmer errors (wrong arg types, etc.) are WARNING so
                # they surface in dev; transient Langfuse network errors are
                # DEBUG since they're non-fatal and expected in offline use.
                if isinstance(e, (AttributeError, TypeError)):
                    log.warning("Telemetry bug in end_turn_trace: %s: %r", type(e).__name__, e)
                else:
                    log.debug("Turn trace end skipped (non-fatal): %s", type(e).__name__)
            if not self._cancelled:
                self.stats["completed_turns"] += 1
            # Bounded rolling window so a long session doesn't grow an
            # unbounded list (and so min/max/avg at disconnect stay O(k)).
            lat = self.stats["turn_latencies_ms"]
            lat.append(round(total_turn_ms, 1))
            if len(lat) > _MAX_TURN_LATENCY_SAMPLES:
                del lat[0:len(lat) - _MAX_TURN_LATENCY_SAMPLES]

            # Per-stage metrics — what actually lets us see WHERE latency
            # goes. Recorded alongside the whole-turn metric so main.py's
            # session-summary roll-up can emit P50/P95 per stage. See D85.
            self._record_stage_sample("stt_ms_samples", self._current_turn_stt_ms)
            self._record_stage_sample(
                "claude_ttft_ms_samples", engine.last_first_token_ms
            )
            tts_fb_ms = None
            if (
                turn_ck["first_sentence_queued_ts"] is not None
                and turn_ck["first_tts_ready_ts"] is not None
            ):
                tts_fb_ms = (
                    turn_ck["first_tts_ready_ts"]
                    - turn_ck["first_sentence_queued_ts"]
                ) * 1000
            self._record_stage_sample("tts_first_byte_ms_samples", tts_fb_ms)

            # TTFA — the brief's SLA metric. Only computable for turns
            # initiated by user speech (speech_end_ts populated). Proactive
            # check-ins and vision-reactive / welfare-check turns skip it.
            ttfa_ms = None
            if (
                self._current_turn_speech_end_ts is not None
                and turn_ck["first_bytes_sent_ts"] is not None
                and not self._cancelled
            ):
                ttfa_ms = (
                    turn_ck["first_bytes_sent_ts"]
                    - self._current_turn_speech_end_ts
                ) * 1000
                log.info(
                    "[TIMING] TTFA: %.0fms (speech_end \u2192 first audio byte out)",
                    ttfa_ms,
                )
            self._record_stage_sample("ttfa_ms_samples", ttfa_ms)

            # Clear per-turn state so a follow-up proactive turn doesn't
            # inherit stale values.
            self._current_turn_speech_end_ts = None
            self._current_turn_stt_ms = None
            self._current_turn_trace = None

            if not self._cancelled:
                await self._safe_send_json(ws, {"type": "status", "state": "listening"})

            # Fire-and-forget: extract user facts from the last exchange,
            # update the persistent UserContext, persist to disk if a
            # resident_id is known, and push the snapshot to the UI
            # memory panel. Non-blocking — the voice loop is already
            # back to "listening" above. The done-callback surfaces any
            # unhandled exception so fact-extraction failures don't get
            # swallowed for the rest of the session.
            _ext_task = asyncio.create_task(self._extract_user_facts(engine, ws))
            _ext_task.add_done_callback(_log_bg_exception("extract_user_facts"))

            # Consume any queued vision-reactive activity that was detected
            # while Abide was busy talking. Fire as a separate task with a
            # short delay so the client has time to finish playback. Using
            # create_task instead of await sleep() to avoid blocking the
            # event loop in the finally block.
            if (
                self._pending_reactive_activity
                and self._engine_ref is not None
                and not self._cancelled
                and ws.client_state == WebSocketState.CONNECTED
            ):
                react_activity = self._pending_reactive_activity
                self._pending_reactive_activity = None
                _react_task = asyncio.create_task(
                    self._fire_queued_reaction(ws, react_activity)
                )
                _react_task.add_done_callback(_log_bg_exception("queued_reaction"))
