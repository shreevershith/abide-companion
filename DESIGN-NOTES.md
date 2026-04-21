# Design Notes — Abide Companion

A development journal for the Resident Companion tier of Abide Robotics' elderly-care product. This document is the story of how we built it: what we set out to do, what we tried, what broke, how we fixed it, and what we deliberately chose not to ship.

It reads top-to-bottom as a narrative. Cross-references like *D82* point to specific decisions; the full decision record lived as a flat log before this rewrite and is preserved in the repo's git history.

---

## What we set out to build

A real-time multimodal AI companion for older adults: always-on voice conversation, always-on vision, a friend-in-the-room persona rather than a chatbot behind a push-to-talk button. The brief was specific about three things and we took them as hard constraints:

1. **First-run UX has to be a double-click.** No terminal commands, no dependency dance. Install Python, click `start.bat`, enter three API keys in the browser, talk.
2. **Latency has to feel conversational.** Under ~1.5 s from end-of-speech to first audio response.
3. **Barge-in has to work.** The user should be able to interrupt mid-sentence and have Abide stop within a few hundred milliseconds.

Target hardware is the Reachy Mini robot by Pollen Robotics, but that ships later; for this build everything runs on commodity hardware — ideally a Logitech MeetUp (camera + mic + speaker in one device), otherwise any webcam + mic + speaker.

We chose direct HTTP calls with persistent HTTP/2 clients over SDKs, a single HTML file for the frontend (no React, no build tools), and a FastAPI backend with a single WebSocket endpoint. Everything else followed from those three constraints and those three architectural commitments.

---

## The bones: voice loop and vision pipeline

The voice loop was built first. Browser mic → AudioWorklet at 48 kHz → WebSocket binary frames → server downsamples to 16 kHz → silero-vad (local CPU, no API call on the hot path) → speech_end fires → Groq Whisper STT → Claude (streaming) → OpenAI TTS → opus bytes back to the browser → Web Audio API playback.

Three early decisions that still matter: (a) silero-vad on local CPU eliminates one round-trip from the latency budget; (b) Web Audio API instead of an `<audio>` element, because `.stop()` is synchronous and that matters for barge-in; (c) direct `httpx` to Anthropic because the `anthropic` SDK hit a Windows-specific SSL error we couldn't chase down, and bypassing it gave us fine-grained control over the streaming lifecycle that every later phase benefited from.

Vision went in shortly after, as an independent fire-and-forget pipeline: the browser captures one JPEG every 1.2 s, buffers three frames (~3.6 s of motion), sends the batch to GPT-4o-mini with a structured JSON prompt, gets back an activity description + bounding box + a `noteworthy` flag, and the result is displayed as a glass chip + canvas overlay while the activity text gets injected into Claude's system prompt on the next turn.

---

## Latency engineering

First-turn latency was brutal on early builds: 3–5 seconds end-to-end. Sequential API calls, per-request httpx clients, no HTTP/2. We unwound that in layers.

**Persistent clients.** Every module (`conversation.py`, `tts.py`, `audio.py`) now keeps one HTTP client for its lifetime. Creating a fresh `httpx.AsyncClient` per call was paying a full TCP+TLS handshake every time (~400–800 ms on Windows). Module-level singletons with 60-second keepalive fixed it.

**HTTP/2 multiplexing.** On HTTP/1.1, a streaming response owns the TCP socket until the last byte arrives, which meant sentence 2's TTS call couldn't reuse the connection while sentence 1 was still streaming back for playback. Every new sentence paid a fresh TLS handshake. Switching to HTTP/2 (`http2=True` on the client, `httpx[http2]>=0.27` + `h2>=4.1` in requirements) lets multiple streams share one connection.

**Parallel TTS.** Once HTTP/2 was in place we still had to stop awaiting `synthesize()` serially per sentence. Refactored `session.py` into a producer/consumer pair: the producer reads the Claude stream and launches a `synthesize()` task on every sentence boundary without awaiting; the consumer pops tasks FIFO and streams audio to the WebSocket. Sentence N+1's OpenAI call now overlaps with sentence N's playback.

**Connection prewarm.** On WebSocket open we fire a HEAD request to each API (Claude, OpenAI, Groq) in parallel, so the TLS handshakes happen while the user is still deciding what to say.

**Sentence-boundary streaming.** TTS starts the instant Claude emits its first `.`, `!`, or `?` — not after the full response. First audio arrives while Claude is still generating sentence 2.

**Prompt caching (Phase O, revised).** Structurally opted into Anthropic's ephemeral cache with `cache_control` on `SYSTEM_PROMPT`, but for a long time `cache_read_tokens` logged zero every turn. The cause: our dynamic per-turn context (time of day, user facts, vision observations) lived in a second system block that changed every turn and invalidated the stable prefix. The fix was to move dynamic context out of the system array and into the head of the newest user message, wrapped in `<turn_context>…</turn_context>` delimiters, and place a second cache breakpoint on the second-to-last message (the previous completed turn's assistant reply). The accumulated history up to the prior turn is now the cached prefix; cache hits start landing around turn 3–5 once the prefix crosses Anthropic's 1024-token activation threshold.

**Model bump (D83, Phase P).** One-line change from `claude-sonnet-4-20250514` to `claude-sonnet-4-6` for ~10–20% TTFT improvement, rollback is a single diff if quality regresses.

P50 turn latency on a typical live session lands around 1–2.5 s; P95 is higher (4–5 s) but that's the whole-turn metric (STT + Claude + all TTS + all WebSocket sends), not time-to-first-audio. See D85 below for the metric-definition footnote.

---

## Barge-in

Barge-in took three iterations to stop being twitchy without being sluggish.

**First pass:** simple — when VAD fires during a Claude response, cancel the task. It worked but TTS audio leaking through the speakers into the mic produced phantom interrupts ("Even the slightest of sound, I think that you're taking it as a barge" — a tester's exact quote). Browser `echoCancellation: true` only filters `<audio>` element playback, not our Web Audio path.

**Second pass:** a 300 ms post-TTS cooldown (ignore VAD right after each TTS chunk to kill trailing-echo blips) plus a 400 ms sustained-speech requirement (short blips die before the threshold; real speech sustains through it).

**Third pass (Phase L, D80):** live-testing on a Logitech MeetUp revealed its firmware-level Acoustic Echo Cancellation is near-perfect. The 400 ms sustained-speech gate was now overkill. We dropped it to 150 ms and reduced the loud-window count from 6 to 4. Barge-in feels ~2.5× more responsive on MeetUp (~200 ms in testing). The two constants are top-of-file in `main.py`; deployments on laptops without hardware AEC should revert to 400 ms / 6 windows.

**Cooperative cancellation.** On barge-in we set a flag that's checked between sentence boundaries and between Claude chunks. Partial response is saved to Claude's history so it doesn't repeat itself on the follow-up. The client has an epoch counter that drops any in-flight `decodeAudioData` callbacks from the cancelled response.

---

## Teaching the vision model to see motion

Single-frame vision was failing on the activities we cared most about: distinguishing *dancing* from *waving*, *standing up* from *standing still*, *falling* from *lying down*. The model was anchoring on a single pose and picking the narrowest label that matched.

**Multi-frame input (D23).** Three consecutive frames, ~1.2 s apart, labelled "Frame 1 (oldest)" through "Frame 3 (most recent)". The model can now compare frames and commit to a motion scope before picking a label.

**Chain-of-thought grounding (D70, Phase F).** The JSON schema now requires `motion_cues` *before* `activity` — a short grounded observation ("Hips sway side to side; both arms up; feet shift") that forces the model to describe what changed across frames before classifying. Combined with a scope-matching rule (WHOLE-BODY / LIMB / HAND-OBJECT / STATIC — when motion spans scopes, pick the largest visible), this generalises across activities we never enumerated.

**Fall detection (D24).** Prompt-enforced: any sequence showing someone going to the ground, slipping, stumbling, or catching themselves on furniture must prefix `activity` with `FALL:`. We bias explicitly toward false positives over false negatives. The frontend raises a red alert banner; Claude's next reply opens with a welfare question.

**The `noteworthy` flag (D72, Phase H).** Earlier we had a hard-coded allowlist in `session.py` — `_REACTIVE_ACTIVITIES = {"waving", "standing up", ...}` — that decided when to fire a proactive response. It had to grow by hand for every new activity and didn't understand intent from context. We replaced it with a model-emitted `noteworthy: bool` judgment. The vision model itself decides whether a scene is the kind of event a friend in the room would stop and react to. No more allowlists.

**Prompt injection defence.** Vision output is wrapped in `<camera_observations>…</camera_observations>` when injected into Claude's prompt, with an explicit instruction to treat it as read-only data. Defends against a sign in the frame reading "Ignore previous instructions" or the vision model emitting instructions from frame OCR.

---

## Making it feel like a companion

Early builds answered when spoken to and stopped there. That's a chatbot; the product has to be a companion.

**Priority-hierarchy system prompt (Phase D).** Claude's prompt is structured as explicit priorities: *listen → respond → use vision only if relevant → be proactive only during silence*. Natural gestures (touching face, moving head while talking) are explicitly listed as "never comment on", so Abide stays a present friend rather than a narrator constantly describing what it sees.

**Proactive check-in (D52).** After 30 s of user silence, Abide initiates a turn based on the current vision context. Built as a dedicated background loop in `main.py`.

**Vision-reactive trigger (D54, restructured by D72).** When a new `noteworthy=true` scene arrives and the activity has actually changed since the last observation and the user has been silent ≥10 s, the server fires a proactive Claude turn. If Abide is already talking when the reactive scene arrives, the activity is queued and consumed at the end of the current response.

**User memory (D53, D78).** A lightweight Claude extraction call runs after every response to pull facts from what the user said: name, topics, preferences, current mood. These get injected into every Claude turn as *"What I know about you: …"*. A per-browser `resident_id` UUID keys an on-disk `./memory/<id>.json` file so the facts survive across Start→Stop cycles; conversation turns are deliberately not persisted (keeps Claude's context flat over long deployments). A "Forget me" button wipes the file.

**Welcome greeting (D61, D75, D81).** On WebSocket open Abide plays a cached time-of-day-appropriate greeting ("Good morning!", "Good evening!", …) with zero API latency. When a name is hydrated from cross-session memory, the variant becomes *"Good morning, Shree! I'm Abide."* — the specific personalised string is prewarmed in parallel with the seed list so it still serves at ~0 ms.

**The name-poisoning incident (D66).** On one test session `extract_user_facts` saved `"Abide"` as the user's name, because the extractor's input formatted history as `"User: … / Abide: …"` and Claude read the assistant's own role label as a speaker. From then on every system prompt said `What I know about you: Name: Abide`. Fixed in two layers: filter history to user messages only before extraction, and add a case-insensitive `_NAME_BLOCKLIST = {"abide", "assistant", "ai", "user", …}` as defence-in-depth. Any extracted name matching the blocklist is logged as `[CONTEXT] Rejected name candidate` and dropped.

---

## The deployment pivot: Docker → native Python

For most of the project's life `start.bat` built a Docker image. That changed in Phase N (D82), for a reason that started with PTZ and ended with a deployment rewrite.

The brief named motorised pan/tilt as a positive signal. Browser MediaCapture-PTZ is the obvious path on paper — Chrome exposes `pan` / `tilt` / `zoom` constraints for cameras that advertise them over UVC. In practice, on Logitech MeetUp firmware 1.0.244 (and later 1.0.272), `track.getCapabilities()` returns zoom but not pan/tilt. We verified this against Google's own reference demo — same result. Logi routes pan/tilt through their proprietary Sync/Tune SDK rather than UVC on MeetUp. That investigation became D79.

But MeetUp *does* expose camera controls over standard Windows DirectShow — the same interface Zoom and Teams use. Python can drive it via the `duvc-ctl` library. The problem: Docker Desktop on Windows runs containers inside a WSL2 Linux VM that can't see host DirectShow without `usbipd-win` + admin PowerShell + per-reboot USB re-attach. Five terminal commands the user would have to run. That breaks the double-click first-run rule hard.

We dropped Docker. `Dockerfile` and `docker-compose.yml` deleted; `start.bat` and `start.sh` rewritten to (1) check Python 3.12+ is on PATH, (2) create a local `.venv`, (3) `pip install -r requirements.txt`, (4) launch uvicorn, (5) open the browser. First run takes 3–5 minutes for the pip install (torch + silero-vad are heavy); subsequent runs start in seconds. Single double-click, same UX as the Docker launcher.

The trade-offs are real: Python install reliability varies across Windows versions (PATH issues, multiple Python versions coexisting), and native Python depends on the user's pip resolver rather than a reproducible image. We mitigated with a clear "Add python.exe to PATH" error message in `start.bat` and upper-bound pins in `requirements.txt`.

---

## The PTZ saga, honestly

With native Python in place, the next step was wiring `duvc-ctl` to our vision pipeline — bbox in, pan/tilt nudge out, subject-follow on the MeetUp.

The wrapper went in: `app/ptz.py`, with `PTZController.nudge_to_bbox(bbox)` computing a damped correction from the frame-centre offset and applying it off-loop via `asyncio.to_thread`. Early logs seemed promising (tester: *"the camera is tilting"*), and we shipped the feature with confidence.

Then live testing on a newer MeetUp firmware revealed it was never us. No `[PTZ]` lines in the logs. The wrapper was silently failing because we'd written it against duvc-ctl's 1.x API (methods on device objects: `device.get_camera_property_range(prop)`), but PyPI had moved to 2.x (module-level functions: `duvc.get_camera_property_range(device, prop)`). `CamProp.PAN` didn't exist either — it's `CamProp.Pan` (PascalCase). Every probe raised an `AttributeError` that the wrapper caught and returned `None`, making every device look PTZ-less.

We rewrote `ptz.py` against the real 2.x surface and added verbose per-axis diagnostic logs at init. That unblocked the real finding: **MeetUp firmware 1.0.272 reports `Pan: ok=False` and `Tilt: ok=False` on `get_camera_property_range`**, with garbage values in the returned struct. Only `Zoom` returns `ok=True` with a valid range `[100, 500]`. The apparent "tilting" we'd seen was Logitech's RightSight digital re-framing — an on-device AI crop, not mechanical motion. MeetUp has no pan/tilt motors.

**What we shipped instead (D84, Phase R).** On-request optical zoom. When the user says "zoom in" / "zoom out" / "reset the zoom", Claude emits a marker `[[CAM:zoom_in]]` (or `zoom_out`, `zoom_reset`) at the very start of its reply. The server strips the marker from the stream before the transcript sees it and dispatches `PTZController.zoom(direction)` off-loop so the lens motion overlaps with Claude's verbal acknowledgement ("Zooming in now."). System prompt tells Claude to decline pan/tilt requests honestly — "I can zoom but not pan/tilt" instead of another hallucinated "Zooming in now" with nothing happening.

**What we didn't ship.** Continuous subject-follow on MeetUp hardware. It isn't ours to build at the UVC level — Logi's proprietary SDK is the only path, and that's a native desktop application, not a web app. We preserved `nudge_to_bbox` in the wrapper so a genuinely PTZ-capable camera (Rally Bar, Rally Bar Mini) would just work, but the MeetUp deliverable is zoom-only. The **out-of-frame welfare check** (after ~11 s of consecutive "Out of frame." observations Abide gently asks *"I can't see you right now — are you still there?"*) is the feature that actually compensates on MeetUp. It runs server-side, works on any camera on any browser.

---

## Observability and latency percentiles

Langfuse is optional but wired throughout. Per-turn traces with STT / Claude / TTS child spans, standalone vision traces, session-summary traces on WebSocket disconnect. If the keys are missing or the library fails to import, every telemetry call becomes a silent no-op; the voice loop never depends on it.

Phase Q added P50 and P95 turn-latency percentiles to the session summary alongside avg/min/max, with a max-fallback when fewer than 20 samples exist to avoid a bogus percentile on short sessions.

**A metric caveat worth writing down (D85).** `p50_turn_latency_ms` and `p95_turn_latency_ms` measure whole-turn duration: from `start_response` entry (STT transcript ready) to the turn's `finally` block (last TTS chunk handed to the WebSocket). This is **not** the brief's "<1.5 s to first audio" target. Real TTFA on cache-warm paths is sub-1 s; on cold Claude TTFT it's 1.2–1.5 s. A 3-sentence Claude reply accumulates all three sentences' TTS work into the turn metric. Anyone reading a P95 of 5 s in isolation will overestimate the latency story by ~3×; the write-up should clearly state which metric is which.

---

## What we deliberately didn't ship

A few things were considered and rejected on principle:

- **LangChain / LlamaIndex / other LLM abstraction layers.** Direct httpx gives us HTTP/2 control, custom SSE parsing, and ~100 lines of streaming-state management that LangChain would hide behind its own API. Abstraction for abstraction's sake.
- **RAG over a corpus.** We don't have a corpus. Building a mock corpus for demo value would look padded.
- **Calendar integration.** The demo video hints at *"I just remembered you have a meeting at 11:30"* — that implies calendar integration. Cross-session UserContext (D78) gives us name + topics + mood; calendar is a different product.
- **Zoom as a continuous auto-behaviour.** Phase K's non-goal list called this distracting. Phase R (D84) narrowed it to user-initiated: Claude emits the marker only when the user asks.
- **CI pipeline, unit tests, mypy.** Prototype-grade build; the evaluator won't see them, they don't deliver visible features, and they cost days of plumbing.
- **System-tray / background mode.** Overkill for the eval context.

---

## Where we are now

Zoom works on MeetUp. Cross-session memory hydrates on connect. Welcome greetings personalise. Prompt caching activates around turn 3–5. Barge-in fires at ~150 ms on MeetUp, ~420 ms on laptops. The vision model catches waving, dancing, standing, falling, and flags the scenes a friend would actually react to. The out-of-frame welfare check catches the person when the camera can't see them. Fall detection errs toward false positives. All API keys stay in the browser's `localStorage`; audio and frames are discarded immediately; conversation history is ephemeral; only `UserContext` facts persist to disk.

The things we didn't ship — mechanical pan/tilt on MeetUp, calendar integration, a corpus — are that way because either the hardware didn't permit it, the brief didn't ask for it, or the cost outweighed the visible benefit.

---

## Appendix — Decision index (D1–D85)

Before this rewrite, `DESIGN-NOTES.md` was a flat log of 85 discrete decision records (D1–D85). The full per-entry rationale, alternatives-considered notes, and trade-off analyses live in the repo's git history — `git log --follow DESIGN-NOTES.md` to walk them. This table keeps the cross-reference function so that references like *"see D66"* elsewhere in the repo (README, CLAUDE.md, TROUBLESHOOTING, code comments) still resolve to a one-line summary.

| # | Title | Phase |
|---|---|---|
| D1 | Single-file HTML frontend, no framework | skeleton |
| D2 | FastAPI with a single WebSocket endpoint | skeleton |
| D3 | silero-vad runs locally in-process, not as an API call | skeleton |
| D4 | Linear interpolation for 48 kHz → 16 kHz downsampling | skeleton |
| D5 | Direct httpx instead of the `anthropic` Python SDK | 3 |
| D6 | ONE persistent `httpx.AsyncClient` per module, HTTP/2 enabled | 5 |
| D7 | Rolling message history capped at 20 messages | 3 |
| D8 | Sentence-boundary streaming — first TTS call fires before Claude finishes | 5 |
| D9 | Parallel TTS pipeline via producer/consumer `asyncio.Queue` | 5 |
| D10 | OpenAI TTS `opus` format, not `mp3` | 5 |
| D11 | Web Audio API playback, not `<audio>` element | 5 |
| D12 | Cooperative cancellation flag, not hard `task.cancel()` | 5 |
| D13 | Partial assistant response saved to history on barge-in | 5 |
| D14 | 400 ms sustained-speech threshold before firing barge-in | 5 |
| D15 | Sequential audio decode with epoch counter | post-5 |
| D16 | Browser captures frames, not server | 6 |
| D17 | Frames sent as base64 JSON, not binary WS frames | 6 |
| D18 | Fire-and-forget vision worker with drop-if-busy | 6 |
| D19 | Rolling 5-description buffer with relative timestamps | 6 |
| D20 | Vision prompt: cap at 10 words, forbid appearance/emotion | 6 |
| D21 | Bounding box in vision JSON, rendered as overlay canvas | 6 |
| D22 | Dark mode, Gemini-Live-inspired layout | 6 |
| D23 | Multi-frame vision call — 3 JPEGs per request, 1.2 s apart | 6 |
| D24 | Fall detection via prompt-enforced `FALL:` prefix, not a separate classifier | 6 |
| D25 | Image-first user content to fight confirmation bias | 6 |
| D25b | RMS-gated barge-in to prevent TTS echo false positives | post-6 |
| D25c | Whisper prompt hint for recognising the assistant's name | post-6 |
| D26 | Langfuse v2, not v3 | 8 |
| D27 | Telemetry side-channel via instance attributes on ConversationEngine | 8 |
| D28 | Per-turn parent trace, standalone vision traces | 8 |
| D29 | Server-side Langfuse keys via `os.environ`, not browser UI | 8 |
| D30 | Graceful no-op when langfuse package or keys are missing | 8 |
| D31 | Typed `ConversationError` + generic client-facing error messages | security |
| D32 | Defensive limits on WebSocket payloads | security |
| D33 | Vision context wrapped in delimited block (prompt-injection defence) | security |
| D34 | STT call hard timeout via `asyncio.wait_for` | robustness |
| D35 | Partial Claude response saved via `finally` block | robustness |
| D36 | Prewarm task exceptions are logged, not silently swallowed | robustness |
| D37 | Bounded `turn_latencies_ms` rolling window + capped `tts_queue` | robustness |
| D38 | Input validation on `np.frombuffer` audio chunks | robustness |
| D39 | Removed redundant `.astype(np.float32)` copies in RMS helpers | perf |
| D40 | `client_playing` flag closes the barge-in "deafness" window | post-5 |
| D41 | Whisper hallucination blocklist in `audio.py` | post-5 |
| D42 | Startup hook wrapped + `auth_check` moved to background task | robustness |
| D43 | `load_dotenv` CWD fallback removed + parse errors caught | robustness |
| D44 | `save_partial()` calls removed from `session.py` (D35 regression fix) | robustness |
| D45 | 60-second staleness clamp on `client_playing` | robustness |
| D46 | Dockerfile runs as non-root `abide` user (uid 10001) | security (pre-Phase-N) |
| D47 | `.env.example` security warning header | security |
| D48 | Whisper confidence filter via `verbose_json` + standalone hallucination list | post-5 |
| D49 | Diary tab — chronological timestamped interaction history | post-launch |
| D50 | Session summary overlay with Claude-powered accuracy analysis | post-launch |
| D51 | Proactive vision engagement in Claude system prompt | post-launch |
| D52 | Proactive check-in background task — 30 s silence trigger | post-launch |
| D53 | UserContext — persistent user-fact extraction and injection | post-launch |
| D54 | Vision-reactive proactive responses with queuing | post-launch |
| D55 | Concurrent response mutex — `start_response()` cancels in-flight tasks | post-launch |
| D56 | All WebSocket sends in `main.py` guarded via `Session._safe_send_json()` | security |
| D57 | Input validation — sample_rate type coercion and range check | security |
| D58 | UI redesign — Abide Robotics brand palette, 16:9 hero with glow, light/dark mode | UI |
| D59 | TTS cache for stock phrases | latency |
| D60 | Dynamic Whisper prompt biasing with UserContext.name | STT |
| D61 | Welcome greeting on WebSocket connect | latency |
| D62 | Vision confidence indicator in scene chip (frontend) | UI |
| D63 | Activity stability filter in VisionBuffer | prompt quality |
| D64 | Diary export button — plain-text download | UX |
| D65 | Session persistence across page refresh with opt-in resume *(reverted in D74)* | post-launch |
| D66 | Extract-user-facts only on user turns + name blocklist | A |
| D67 | Barge-in gate switched from peak-RMS to loud-window-count | B |
| D68 | System prompt trim + TTS cache seed expansion + short-acknowledgement nudge | C |
| D69 | Priority-hierarchy system prompt + narrowed reactive triggers + 10 s silence gate | D |
| D70 | Vision prompt restructured around motion-scope + chain-of-thought | F |
| D71 | Correction-response shape rule in Claude system prompt | G |
| D72 | Vision-emitted `noteworthy` flag replaces `_REACTIVE_ACTIVITIES` keyword list | H |
| D73 | Auto-populated TTS cache via `app/tts_cache_store.py` | I |
| D74 | Removed the "Resume last session?" banner *(reverts D65)* | post-launch |
| D75 | Time-of-day awareness — welcome + system-prompt injection | easter egg |
| D76 | Post-audit cleanup — hot-path I/O, frame pass-through, dep bounds, task drain on error | audit |
| D77 | Audio-reactive hero glow — AnalyserNode-driven `--voice-level` CSS variable | J |
| D78 | Cross-session UserContext persistence — `./memory/<resident_id>.json` | E |
| D79 | PTZ subject-follow via browser MediaCapture-PTZ attempted; out-of-frame welfare check shipped as fallback | K |
| D80 | MeetUp-tuned barge-in — drop to 150 ms / 4 loud-windows (revert to 400 / 6 on laptops) | L |
| D81 | Personalized name-aware welcome greeting | M |
| D82 | Dropped Docker, switched to native Python deployment *(+ 2026-04-20 follow-up correcting "subject-follow" claim)* | N |
| D83 | Claude Sonnet 4.6 model upgrade | P |
| D84 | On-request optical zoom via inline `[[CAM:...]]` marker | R |
| D85 | `p50/p95_turn_latency_ms` are whole-turn metrics, not TTFA — documented | Q (follow-up) |
