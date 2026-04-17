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
from app.tts import synthesize
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

# Activities that should trigger an immediate proactive response when
# detected as a NEW activity (different from the previous observation).
# These are gestures or movements that clearly invite engagement —
# a companion in the room would notice and react immediately, not wait
# for the person to speak.
_REACTIVE_ACTIVITIES = frozenset({
    "waving", "wave", "thumbs up", "pointing", "beckoning",
    "standing up", "getting up", "picked up", "picking up",
    "holding up", "raised hand", "gesturing", "reaching",
    "dancing", "exercising", "stretching",
})

# Minimum seconds between vision-reactive responses. Without this,
# a sustained wave would trigger a response every ~3.6s (vision cycle).
_VISION_REACT_COOLDOWN_S = 15.0


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
            self.name = facts["name"]
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

        # User context — accumulates facts about the user across the session
        self.user_context: UserContext = UserContext()

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

        # Vision state
        self.vision_buffer: VisionBuffer = VisionBuffer()
        self._frame_task: asyncio.Task | None = None
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
        self.stats = {
            "total_turns": 0,
            "completed_turns": 0,
            "barge_in_count": 0,
            "fall_count": 0,
            "vision_calls": 0,
            "turn_latencies_ms": [],
        }

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

    def start_response(
        self,
        ws: WebSocket,
        engine: ConversationEngine,
        text: str,
        openai_key: str | None,
        turn_trace=None,
    ):
        """Launch the response+TTS pipeline as a background task.

        `turn_trace` is an optional Langfuse trace handle created by main.py
        right after STT. If provided, Claude and TTS spans are attached to
        it and it is finalized in `_run_response`.

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
        self._response_task = asyncio.create_task(
            self._run_response(ws, engine, text, openai_key, vision_context, user_context)
        )

    def _is_reactive_change(self, new_activity: str) -> bool:
        """Return True if the new activity is 'interesting' and different
        from the previous observation — worth triggering a proactive response.

        NOTE: This is called AFTER vision_buffer.append(), so the new
        activity is already the latest entry. We compare against the
        second-to-last entry to detect actual changes.
        """
        low = new_activity.lower()
        # Check if any reactive keyword appears in the activity text
        if not any(kw in low for kw in _REACTIVE_ACTIVITIES):
            return False
        # Compare against the PREVIOUS entry (second-to-last, since the
        # new activity was already appended to the buffer by _run_vision)
        entries = self.vision_buffer.entries
        if len(entries) >= 2:
            prev = entries[-2].result.activity.lower()
            if low == prev:
                return False
        return True

    def process_frames(
        self,
        jpegs: list[bytes],
        openai_key: str | None,
        ws: WebSocket,
    ):
        """Kick off a vision analysis for a short frame sequence.

        Fire-and-forget. Drops the batch if a previous analysis is still
        in flight — we don't queue, we don't want cost explosion if the
        API is slow.
        """
        if not openai_key or not jpegs:
            return
        if self._frame_task is not None and not self._frame_task.done():
            # Previous analysis still in flight — drop this batch.
            return
        self._frame_task = asyncio.create_task(
            self._run_vision(jpegs, openai_key, ws)
        )

    async def _run_vision(
        self,
        jpegs: list[bytes],
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
                result = await analyze_frames(jpegs, openai_key, prior)
            if not result or not result.activity:
                return
            self.vision_buffer.append(result)
            self.stats["vision_calls"] += 1

            # Telemetry: one standalone trace per vision call.
            total_bytes = sum(len(j) for j in jpegs if j)
            telemetry.observe_vision(
                self.telemetry_client,
                self.telemetry_session_id or "unknown",
                num_frames=len(jpegs),
                image_bytes=total_bytes,
                activity=result.activity,
                bbox=result.bbox,
                latency_ms=tmr.ms,
                is_fall=result.is_fall,
            )

            # Fall-alert path: stash for next turn + push a WS alert now.
            if result.is_fall:
                self._pending_fall_alert = result.activity
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

            # Vision-reactive trigger: if the activity is "interesting"
            # (waving, standing up, thumbs up, etc.), react proactively.
            # Falls have their own dedicated urgent-context path above.
            if (
                not result.is_fall
                and self._engine_ref is not None
                and self._is_reactive_change(result.activity)
                and time.monotonic() - self._last_vision_react_ts >= _VISION_REACT_COOLDOWN_S
            ):
                silence = time.monotonic() - self.last_user_speech_ts
                if silence > 3.0:  # don't interrupt active conversation
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
        except Exception as e:
            log.error("Vision worker error: %s", e)

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

    async def _extract_user_facts(self, engine: ConversationEngine):
        """Fire-and-forget task: extract user facts from the last exchange."""
        try:
            facts = await engine.extract_user_facts()
            if facts:
                self.user_context.update(facts)
                log.info("[CONTEXT] User context updated: %s", self.user_context.as_prompt()[:120])
        except Exception as e:
            log.debug("User fact extraction skipped: %s", e)

    # The response pipeline can outlive the WebSocket (TTS in flight when the
    # client disconnects). Without these guards, late sends raise
    # "Unexpected ASGI message 'websocket.send' after 'websocket.close'".

    @staticmethod
    async def _safe_send_json(ws: WebSocket, data: dict):
        if ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await ws.send_json(data)
        except Exception:
            pass

    @staticmethod
    async def _safe_send_bytes(ws: WebSocket, data: bytes):
        if ws.client_state != WebSocketState.CONNECTED:
            return
        try:
            await ws.send_bytes(data)
        except Exception:
            pass

    async def _run_response(
        self,
        ws: WebSocket,
        engine: ConversationEngine,
        text: str,
        openai_key: str | None,
        vision_context: str = "",
        user_context: str = "",
    ):
        """Stream Claude + parallel TTS, checking for cancellation."""
        full_response: list[str] = []
        tts_queue: asyncio.Queue = asyncio.Queue(maxsize=_TTS_QUEUE_MAXSIZE)
        turn_trace = self._current_turn_trace
        t_turn_start = time.monotonic()

        async def producer():
            """Read Claude stream; launch a synthesize task per sentence."""
            text_buf = ""
            try:
                await self._safe_send_json(ws, {"type": "status", "state": "thinking"})

                async for chunk in engine.respond(text, vision_context=vision_context, user_context=user_context):
                    if self._cancelled:
                        log.info("Barge-in: stopping Claude stream")
                        break

                    await self._safe_send_json(
                        ws, {"type": "response_chunk", "text": chunk}
                    )
                    full_response.append(chunk)
                    text_buf += chunk

                    sentences = _SENTENCE_RE.split(text_buf)
                    if len(sentences) > 1:
                        for sentence in sentences[:-1]:
                            sentence = sentence.strip()
                            if sentence and openai_key and not self._cancelled:
                                ts = time.monotonic()
                                log.info("[TIMING] Sentence boundary: %r", sentence[:60])
                                # Launch TTS as a concurrent task — do NOT await here.
                                # Next iteration of the Claude stream will start,
                                # and the TTS task will run in parallel.
                                task = asyncio.create_task(
                                    synthesize(sentence, openai_key, ts)
                                )
                                tts_queue.put_nowait((sentence, task, ts))
                        text_buf = sentences[-1]

                # Final tail (text after the last sentence boundary or an un-terminated last sentence)
                text_buf = text_buf.strip()
                if text_buf and openai_key and not self._cancelled:
                    ts = time.monotonic()
                    log.info("[TIMING] Final tail: %r", text_buf[:60])
                    task = asyncio.create_task(
                        synthesize(text_buf, openai_key, ts)
                    )
                    tts_queue.put_nowait((text_buf, task, ts))

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
            finally:
                # Sentinel — tells consumer no more items will arrive
                tts_queue.put_nowait(None)

        async def consumer():
            """Await TTS tasks in FIFO order; send their audio to the WebSocket."""
            first_tts = True
            while True:
                item = await tts_queue.get()
                if item is None:  # sentinel
                    return

                sentence, task, ts = item

                # If we've already been cancelled, drop the task and skip.
                if self._cancelled:
                    if not task.done():
                        task.cancel()
                    continue

                try:
                    audio = await task
                except asyncio.CancelledError:
                    continue
                except Exception as e:
                    log.error("TTS failed for %r: %s", sentence[:40], e)
                    continue

                # Telemetry: one span per TTS call. Latency is sentence-boundary
                # to the moment the audio bytes became available locally.
                tts_latency_ms = (time.monotonic() - ts) * 1000
                telemetry.observe_tts(
                    turn_trace,
                    sentence=sentence,
                    audio_bytes=len(audio),
                    latency_ms=tts_latency_ms,
                )

                # Check cancellation again after the await — user may have
                # barged in while we were waiting for OpenAI.
                if self._cancelled:
                    continue

                if first_tts:
                    await self._safe_send_json(
                        ws, {"type": "status", "state": "speaking"}
                    )
                    first_tts = False

                t_send = time.monotonic()
                await self._safe_send_bytes(ws, audio)
                self.mark_tts_sent()
                log.info(
                    "[TIMING] WS send: %.0fms (%d bytes)",
                    (time.monotonic() - t_send) * 1000,
                    len(audio),
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
            log.info(
                "Hard cancel: %d chars streamed (history saved by engine)",
                len("".join(full_response)),
            )
            await self._safe_send_json(ws, {"type": "response_done"})
        except Exception as e:
            log.error("Response pipeline error: %s", e)
            # Send a generic message to the client; never leak raw exception
            # details that may contain API fingerprints or stack fragments.
            await self._safe_send_json(
                ws,
                {
                    "type": "error",
                    "message": "Something went wrong while I was responding. Let's try again.",
                },
            )
        finally:
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
                log.debug("Turn trace end skipped: %s", e)
            if not self._cancelled:
                self.stats["completed_turns"] += 1
            # Bounded rolling window so a long session doesn't grow an
            # unbounded list (and so min/max/avg at disconnect stay O(k)).
            lat = self.stats["turn_latencies_ms"]
            lat.append(round(total_turn_ms, 1))
            if len(lat) > _MAX_TURN_LATENCY_SAMPLES:
                del lat[0:len(lat) - _MAX_TURN_LATENCY_SAMPLES]
            self._current_turn_trace = None

            if not self._cancelled:
                await self._safe_send_json(ws, {"type": "status", "state": "listening"})

            # Fire-and-forget: extract user facts from the last exchange
            # and update the persistent UserContext. Non-blocking — the
            # voice loop is already back to "listening" above.
            asyncio.create_task(self._extract_user_facts(engine))

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
                asyncio.create_task(
                    self._fire_queued_reaction(ws, react_activity)
                )
