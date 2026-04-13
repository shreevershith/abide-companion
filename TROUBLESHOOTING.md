# Troubleshooting Log

A running record of bugs, root causes, and fixes encountered while building Abide Companion. Newest entries at the top.

---

## 12. Vision-reactive trigger lost when Abide is busy talking (Post-Phase-7)

**Symptom**: User waved at the camera for ~12 seconds. Vision correctly detected "Waving hand." 4 times in a row. But no `[VISION-REACT]` log entry appeared and Abide never reacted. The user complained "You're not responding" and Abide apologized for a "delay or connection issue."

**Root cause**: Two bugs compounding:

1. **`_is_reactive_change()` always returned False** — it compared the new activity against `self.vision_buffer.latest`, but `vision_buffer.append(result)` had already been called before the check, so latest WAS the new activity. Every activity looked like a "duplicate" of itself. Fixed by comparing against `entries[-2]` (the entry before the just-appended one).

2. **No queuing when busy** — even after fixing the comparison, the trigger was blocked by `not self.is_responding and not self.is_audible` guards because Abide was playing TTS from a previous response. By the time the audio finished, the user had stopped waving and subsequent vision frames showed "Sitting, looking at the camera." The reactive opportunity was permanently lost.

**Fix**: Added `_pending_reactive_activity` field to Session. When a reactive gesture is detected but Abide is busy, the activity is queued. At the end of `_run_response()`'s `finally` block, the queued activity is consumed — after a 1-second delay and a final `is_audible` check, a new proactive response fires mentioning what Abide noticed while it was talking.

**Files touched**: `app/session.py` — `_is_reactive_change()` comparison fix, `_pending_reactive_activity` field, queuing in `_run_vision()`, consumption in `_run_response()`.

---

## 11. Security/performance audit: race conditions, unguarded sends, per-request client (Post-Phase-7)

**Symptom**: During a code review audit, three categories of issues were identified:

1. **Race condition in `start_response()`**: Three concurrent callers (STT path, check-in loop, vision-reactive trigger) could call `start_response()` simultaneously, orphaning the first task and causing overlapping audio output.

2. **Unguarded WebSocket sends**: 11 direct `ws.send_json()` calls in `main.py` could crash with "Cannot call send once a close message has been sent" if the client disconnected mid-operation. Already seen in testing (Troubleshooting #10 logs).

3. **Per-request httpx client in `/api/analyze`**: The session analysis endpoint created a fresh `httpx.AsyncClient` per call, paying ~300-500ms TCP+TLS handshake each time.

**Fixes (D55, D56, D57)**:

1. `start_response()` now cancels any in-flight `_response_task` before starting a new one. Sets `_cancelled = True`, calls `task.cancel()`, then resets for the new task. User speech always wins over check-in/vision-reactive responses.

2. All 11 `ws.send_json()` calls in `main.py` replaced with `Session._safe_send_json(ws, ...)` which checks `ws.client_state` before sending and silently swallows exceptions on closed connections.

3. `/api/analyze` now uses a module-level `_analyze_client` (persistent HTTP/2) instead of per-request creation. Client is closed in `_on_shutdown()`.

4. `sample_rate` from client config validated as int with range check (8000-192000 Hz), defaults to 48000 on invalid input.

5. `_proactive_checkin_loop()` now wraps `start_response()` in try/except to prevent silent loop death on unexpected errors.

**Files touched**: `app/main.py`, `app/session.py`

---

## 10. STT prompt was seeding the very hallucinations it was meant to prevent (Phase 7+)

**Symptom**: Even with the blocklist from Fix #8 and D41 in place, phantom `"Thank you"` transcripts were still reaching Claude — "I get random Thank you all of a sudden" during normal use. The blocklist only caught the well-documented YouTube outros (`"Thanks for watching"`, `"Subtitles by Amara.org"`, etc.), not a bare `"Thank you."`.

**Root cause**: `transcribe()` in `app/audio.py` was passing a Whisper `prompt` that included, verbatim:
```
"Common conversation: hello, hi, good morning, good evening, how are you,
 I feel, I think, I need, thank you, goodbye, please, yes, no, can you help me."
```
Whisper's `prompt` parameter is a **decoder bias** — every token in it has its output probability raised. The prompt was originally added (D25c) so the decoder wouldn't map the assistant's name "Abide" onto "bye". Somewhere along the way it grew to include a grab-bag of "common conversation" phrases for the user side too. That grab-bag included exactly the strings Whisper is already known to hallucinate on near-silent audio, and we were amplifying them on every single call.

This is the Whisper equivalent of writing `logit_bias={" thank you": +5}` and being surprised the model keeps saying "thank you".

**Fix**: Three layered changes in `app/audio.py`, strongest lever first:

1. **Strip the prompt back to only what disambiguates rare words.** The new `STT_PROMPT` is two sentences, names "Abide" and `A-B-I-D-E`, and nothing else. No "hello", no "thank you", no "goodbye". The original D25c purpose (recognising the assistant's name) is preserved.

2. **Switch to `response_format="verbose_json"` + confidence filter.** Each returned segment now carries `no_speech_prob` and `avg_logprob`. Drop the segment when `no_speech_prob > 0.6 AND avg_logprob < -1.0` — the exact threshold OpenAI's reference Whisper implementation uses to suppress hallucinated segments. If every segment fails the check, the whole transcript is dropped. Whisper itself tells us when it's bluffing; we just have to ask.

3. **Also set `temperature=0.0` and `language="en"`.** Deterministic decoding (no sampling into low-probability paths that produce most hallucinations) and no language-detection round-trip (which is another source of spurious output on silence).

4. **Standalone-phrase blocklist** added alongside the existing regex blocklist: if the *entire* transcript (after stripping punctuation + lowercasing) is one of `{"thank you", "thanks", "bye", "you", "uh", "hmm", ...}`, it's dropped. `"thank you for the reminder"` and other in-sentence uses still pass through.

**Related design notes**: D25c (updated to document the prompt-hygiene rule), D48 (new — Whisper confidence filter). This supersedes Fix #8's RMS-only approach in the specific case of short, non-silent hallucinations.

**Meta-lesson**: the `prompt` field on Whisper is not a place for example user speech. Treat it like a logit-bias list — every token you put there, you're asking for more of.

---

## 9. Sequential TTS calls + WS send-after-close (Phase 5)

**Symptom**: Even after HTTP/2 + prewarm (Fix #7), total turn latency stayed at 3-5 seconds because every sentence's TTS was being awaited sequentially. A 3-sentence response paid ~1.5s × 3 = 4.5s in serial TTS calls. Separately, the server was logging `Unexpected ASGI message 'websocket.send', after sending 'websocket.close' or response already completed` when TTS finished after a client disconnect.

**Root cause**:
1. `session.py` awaited `synthesize()` inline for each sentence before moving to the next. HTTP/2 multiplexing was enabled but gave us nothing — we were already serialized at the application level.
2. All `ws.send_json()` / `ws.send_bytes()` calls assumed the socket was still open. In-flight TTS tasks could complete after the WebSocket closed, raising ASGI protocol errors.

**Fix**: Two-part refactor of `app/session.py`:
1. **Producer/consumer with parallel TTS**: `_run_response()` now runs two concurrent sub-tasks via `asyncio.gather()`:
   - **Producer**: reads the Claude stream, and on each sentence boundary launches `synthesize()` as an independent `asyncio.create_task()` (no await). Pushes `(sentence, task)` onto an `asyncio.Queue`.
   - **Consumer**: pops from the queue, awaits each task in FIFO order, and sends the audio bytes to the WebSocket.

   Because TTS tasks are launched without awaiting, sentence N+1's OpenAI call starts while sentence N's audio is still streaming back. HTTP/2 finally multiplexes over a single connection. Total TTS latency for a 3-sentence response drops from ~4.5s to ~1.6s (dominated by the slowest single call).

2. **Safe WebSocket helpers**: New `_safe_send_json()` / `_safe_send_bytes()` static methods on `Session`. Both check `ws.client_state == WebSocketState.CONNECTED` before sending and silently swallow any exception. All outbound WebSocket writes in `_run_response()` now go through these helpers, so late TTS completions on a closed socket are silently dropped instead of crashing the task.

**Barge-in preserved**: Both producer and consumer check `self._cancelled` at every await point. On barge-in:
- Producer stops iterating Claude chunks and stops creating new TTS tasks.
- Consumer cancels any pending TTS tasks in its queue (`task.cancel()`) and drops any completed-but-unsent audio.
- Partial response is still saved to history so Claude doesn't repeat itself.

**What HTTP/2 actually buys us now**: Parallel TTS tasks reuse one TCP connection via H2 stream multiplexing. Without parallel calls, HTTP/2 was dead weight over HTTP/1.1 keep-alive.

---

## 8. Whisper "Thank you." hallucination on short/quiet audio (Phase 5)

**Symptom**: Server logs showed phantom transcripts — `Thank you.`, `Наржу.` (Russian), `Entah kalau abaid.` (Indonesian) — for audio segments the user never spoke. These phantom turns triggered full Claude + TTS cycles, wasting latency budget and producing confused apologetic responses.

**Root cause**: Whisper (both OpenAI and Groq's versions) defaults to common phrases from its training data when given very short or very quiet audio. It was trained heavily on YouTube transcripts, so "Thank you for watching" is a fallback. Ambient noise (breaths, clicks, chair creaks, the HVAC) was making it to Groq because silero-vad correctly identified them as "sound present" but they weren't actually speech.

Looking at byte sizes in the logs confirmed it: phantom transcripts came from 18-58KB segments (0.5-1.8s of mostly-silent audio), while legitimate speech was 70-200KB.

**Fix**: Quality filter in `app/audio.py`, applied when VAD fires `speech_end` but before converting to WAV:
```python
MIN_SPEECH_SAMPLES = 8000    # 0.5 seconds at 16kHz
MIN_SPEECH_RMS = 0.015       # float32 PCM RMS threshold
```

Rejected segments log `[FILTER] Rejected short/quiet segment: N samples (Xs), RMS=Y` and return `None` from `feed()`. `main.py` already treats a `None` return as "no speech", so no further code changes needed.

**Calibration**: 0.5s minimum cuts clicks and coughs without touching "Hi" (~0.4s) or "Bye" (~0.5s — barely passes, acceptable). 0.015 RMS is roughly 3x the measured background-noise floor (~0.005) and well below real speech (~0.04-0.08). Values verified against real conversation logs — zero false rejections, zero phantom transcripts.

---

## 7. HTTP/1.1 streaming + head-of-line blocking = 1500ms first-byte (Phase 5)

**Symptom**: Even after switching to persistent httpx clients (Fix #5), Claude first-token latency was still 969–1531ms and OpenAI TTS first-byte was still 719–1797ms. The pattern was clear: the **first** API call of a turn was always slow, while calls *within* the same response were faster — suggesting connection reuse was partially working but handshakes were still happening.

**Evidence ruling out the network**:
```
curl.exe -w "dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} ttfb=%{time_starttransfer}" ...

api.anthropic.com:  connect=22-53ms   tls=62-91ms    ttfb=155-238ms
api.openai.com:     connect=26-59ms   tls=57-98ms    ttfb=140-176ms
api.groq.com:       connect=24-64ms   tls=57-140ms   ttfb=115-197ms
```

Raw network from California → all three APIs is healthy (<250ms). Our code was adding ~1300ms of pure overhead.

**Root cause**: httpx was configured with `http2=False`. On HTTP/1.1, a **streaming** response holds the TCP socket for its entire lifetime (until the last byte is read). When `session.py` calls `synthesize()` for sentence 2 while sentence 1's response is still being streamed for playback, httpx can't reuse the existing connection — it has to open a new TCP + TLS connection from scratch (~500-1000ms penalty every time).

Groq wasn't affected because its transcription endpoint returns a single non-streaming JSON response — the socket frees up immediately, so keepalive works.

**Fix**: Two-part:
1. **Enable HTTP/2** in `conversation.py` and `tts.py`: `httpx.AsyncClient(http2=True, ...)`. HTTP/2 multiplexes multiple streams over one connection — no head-of-line blocking, no extra handshakes. Added `httpx[http2]>=0.27` and `h2>=4.1` to `requirements.txt`.
2. **Pre-warm connections on WebSocket config**: `ConversationEngine.prewarm()` and `tts.prewarm()` fire a HEAD request to each API as soon as the client connects. Both run in parallel via `asyncio.create_task(asyncio.gather(...))`. The TLS handshake happens while the user is still deciding what to say, not during their first turn.

**Expected improvement**: Claude first-token 969ms → ~300-500ms. TTS first-byte 719-1797ms → ~200-400ms. Total turn latency 3-4s → ~1-1.5s.

**Why `http2=False` was there in the first place**: I added it in Fix #5 thinking it might interact badly with httpx's SSE streaming (we parse `data:` lines manually). It doesn't — HTTP/2 streams work identically to HTTP/1.1 streams from the application's perspective. It was an unnecessary precaution that cost us 1000ms per call.

---

## 6. Echo feedback triggering phantom barge-ins (Phase 5)

**Symptom**: During a test conversation, Abide would cut itself off mid-sentence even though the user said nothing. Server logs showed repeated `Barge-in triggered — cancelling response` events between TTS sentences. User observation: "Even the slightest of sound, I think that you're taking it as a barge."

**Root cause**: TTS audio played through speakers → leaked into the microphone → silero-vad detected it as "speech" → triggered the barge-in logic in `main.py`. Browser `echoCancellation: true` only filters echo from `<audio>` elements, not our Web Audio API playback path.

**Fix**: Two-part echo suppression in `app/main.py`:
1. **300ms post-TTS cooldown** — after every TTS chunk is sent (tracked via `session.last_tts_send_ts`), ignore all audio/VAD events for 300ms. This kills the trailing-echo blip that happens at the end of each TTS sentence.
2. **400ms sustained-speech requirement** — when VAD first detects speech during a response, don't fire barge-in immediately. Start a timer. Only fire if speech is still active 400ms later. Short echo blips die out before reaching this threshold; real human speech sustains through it.

Tunables are top-of-file constants in `main.py`: `POST_TTS_COOLDOWN_MS`, `SUSTAINED_SPEECH_MS`.

**Files touched**: `app/main.py`, `app/session.py` (added `last_tts_send_ts` + `mark_tts_sent()`).

---

## 5. TTS latency 1.3-2.8s per sentence — per-request httpx clients (Phase 5)

**Symptom**: Server timing logs showed OpenAI TTS first-byte latency of 1282-2797ms per sentence. Normal `tts-1` is 200-500ms. Same pattern hit Claude calls. Full turns were running 3-4 seconds end-to-end, blowing past the <1500ms requirement in CLAUDE.md.

**Root cause**: `tts.py`, `conversation.py`, and `audio.py` were each creating a brand-new client (`httpx.AsyncClient()` or `AsyncGroq()`) **on every single call**. Every request paid a full TCP + TLS handshake from scratch. On Windows, that's 300-500ms of handshake overhead per request. Stacked across STT + Claude + multiple TTS calls per turn, it added 1-2 seconds of pure dead time.

**Fix**: Persistent clients everywhere:
- `app/conversation.py` — one `httpx.AsyncClient` lives on each `ConversationEngine` instance (lifetime = WebSocket session). Explicit `aclose()` called from `main.py` finally block.
- `app/tts.py` — module-level singleton `httpx.AsyncClient`, lazy-initialized. `keepalive_expiry=60s`.
- `app/audio.py` — module-level `AsyncGroq` singleton, re-created only if the API key changes.

**Never do this again**: Added to CLAUDE.md under `## Never Do`:
> Never create httpx/Groq/Anthropic clients per-request — always use persistent module-level or session-level clients.

**Expected improvement**: -400 to -800ms per TTS call, -200 to -400ms per Claude call.

---

## 4. VAD cutting off speech too early (Phase 5)

**Symptom**: User couldn't pause naturally between sentences without the system prematurely marking speech as "ended" and sending to transcription. Resulted in fragmented, multi-turn transcripts for single thoughts.

**First fix attempt**: Bumped `min_silence_duration_ms` from 300ms → 700ms in `app/audio.py`. Worked, but introduced a different complaint: noticeable lag between finishing speech and seeing the transcript.

**Second iteration**: Dialed back to 500ms. Sweet spot between "cuts me off mid-thought" and "takes forever to respond".

**Also added**: Timing instrumentation. `audio.py` now captures `last_speech_end_ts` on every `speech_end` event. `transcribe()` logs:
```
[TIMING] STT: speech_end → transcript = 840ms (Groq API 620ms, audio 48000 bytes)
```
This lets us see if slowness is Groq itself vs. local processing overhead.

---

## 3. Anthropic SDK "Connection error" on Windows (Phase 3)

**Symptom**: After transcript arrived, UI showed `Claude error: Connection error` and hung on "Thinking...". The anthropic SDK retried twice then gave up. Meanwhile Groq calls worked fine on the same machine, so it wasn't a general network issue.

**Root cause**: Unknown low-level SSL/proxy issue specific to the `anthropic` Python SDK on Windows. Not worth debugging further.

**Fix**: Bypassed the SDK entirely. Rewrote `app/conversation.py` to POST directly to `https://api.anthropic.com/v1/messages` with `httpx.AsyncClient` and parse the SSE `data:` stream manually. Handles `content_block_delta` → `text_delta` events and `error` events.

**Bonus**: We now have complete control over the streaming lifecycle, which made Phase 5 barge-in cancellation much cleaner (we check a flag between chunks instead of wrestling with SDK internals). Also made Fix #5 (persistent httpx client) trivial to apply.

**Kept `anthropic>=0.40` in requirements.txt** just in case a future phase needs anthropic's typed models for something else.

---

## 2. WebSocket send/recv race on first message (Phase 2)

**Symptom**: Occasionally on the very first audio chunk after Start, the config JSON hadn't been processed yet and the server errored with "Groq API key not set".

**Root cause**: Frontend was sending the first PCM chunk immediately on `workletNode.port.onmessage` firing, but the config message was still in flight. On fast machines the ordering could race.

**Fix**: In `frontend/index.html`, connect `workletNode` to the source **inside** `ws.onopen`, after `ws.send(config)`. This guarantees the config hits the server before any binary frames.

---

## 1. AudioWorklet chunk size mismatch (Phase 1)

**Symptom**: Audio loopback worked but silero-vad behaved erratically — missed the start of speech, fired spurious `end` events.

**Root cause**: `silero-vad` expects exactly 512-sample windows at 16kHz. My initial implementation fed whatever chunk size the AudioWorklet produced (varies by browser/OS), then tried to resample after. This meant VAD was being called with mismatched window sizes.

**Fix**: Two-stage pipeline in `app/audio.py`:
1. Linear-interpolation downsample from source rate (48kHz typically) to 16kHz.
2. Slice the 16kHz stream into exactly 512-sample windows. Any leftover samples at the end of a `feed()` call go into `self._remainder` and get prepended to the next call.

Also pinned `CHUNK_SIZE = 2048` on the frontend to give the worklet a stable, predictable buffer size.

---

## Open items / known limitations

- **Echo suppression is heuristic**, not acoustic. If the evaluator's environment is unusually reverberant or uses loud speakers, the 300ms cooldown + 400ms sustained threshold may still let some phantom barge-ins through. Proper fix would be hardware AEC (Logitech MeetUp has it built in) or WebRTC's `RTCAudioProcessor`. Out of scope for prototype.
- **Barge-in partial history** saves whatever Claude had streamed before the cut-off, but only full sentences were actually spoken aloud. Claude's history may therefore include text the user never heard. Acceptable tradeoff for now — keeps Claude from repeating itself, and the "unheard" portion is usually just the last half-sentence.
- **`verbose_json` + confidence filter now in place** (Fix #10). Previously listed as an open item; superseded. The same response also carries per-segment `language`, so the Phase 7 language-detection TODO is closed too.
- **Session summary analysis depends on Anthropic API availability.** If the API call fails at session end, the rest of the summary (duration, transcript, activity log) still renders — only the "Right / Wrong" section shows a fallback message. Non-blocking by design.
- **Diary tab accumulates many entries in long sessions** (~one vision entry every 3.6s = ~250 entries in a 15-minute session). Scrollable and performant at this scale, but a production system would benefit from debouncing identical consecutive observations.

---

## Completed items (previously open)

- ~~Session summary screen~~ — Implemented (D50). Full-screen overlay on Stop with duration, transcript, activity log, and Claude-powered accuracy analysis.
- ~~Diary view~~ — Implemented (D49). Live-updating tab in the transcript panel with color-coded chronological event log.
- ~~Groq Whisper verbose_json~~ — Implemented (D48, Fix #10). Confidence filter + standalone blocklist active.
- ~~Demo video~~ — Still pending.
- ~~Proactive check-in~~ — Implemented (D52). 30-second silence trigger with vision-based initiation.
- ~~UserContext persistence~~ — Implemented (D53). Lightweight extraction after each response.
- ~~Vision-reactive trigger~~ — Implemented (D54). Waving, thumbs up, standing up etc. trigger immediate response.
- ~~Race condition in start_response()~~ — Fixed (D55, Fix #11). Cancels in-flight task before starting new one.
- ~~Unguarded WebSocket sends~~ — Fixed (D56, Fix #11). All sends use Session._safe_send_json().
- ~~Per-request client in /api/analyze~~ — Fixed (Fix #11). Module-level persistent HTTP/2 client.
- ~~Demo video~~ — Still pending.
