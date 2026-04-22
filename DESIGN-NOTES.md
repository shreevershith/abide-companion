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

Vision went in shortly after, as an independent fire-and-forget pipeline: the browser captures one JPEG every 1.2 s, buffers two frames (~2.4 s of motion; was 3 frames / 3.6 s before the Phase S.3 follow-up retune), sends the batch to GPT-4.1-mini with a structured JSON prompt, gets back an activity description + bounding box + a `noteworthy` flag, and the result is displayed as a glass chip + canvas overlay while the activity text gets injected into Claude's system prompt on the next turn.

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

**The metric-definition caveat (D85).** `p50_turn_latency_ms` and `p95_turn_latency_ms` measure **whole-turn** duration: from `start_response` entry (STT transcript ready) to the turn's `finally` block (last TTS chunk handed to the WebSocket). This is **not** the brief's "<1.5 s to first audio" target. A 3-sentence Claude reply accumulates all three sentences' TTS work into the turn metric, so P95 looks ~3× larger than the user actually experiences.

**The fix (D86).** Rather than rename the existing metric and break any Langfuse dashboards that reference it, we added four new stage-level sample lists to `Session.stats` — `ttfa_ms_samples`, `stt_ms_samples`, `claude_ttft_ms_samples`, `tts_first_byte_ms_samples` — and roll up P50/P95 for each into the session summary. `ttfa_p50_ms` / `ttfa_p95_ms` is the brief's real SLA number: `speech_end` (VAD fires in `audio.py`) → first audio byte leaving the server (first `_safe_send_bytes` in `session.py`'s consumer). The whole-turn metric is kept unchanged. Each turn also logs `[TIMING] TTFA: Xms` for per-turn visibility. A standalone smoke test (`scripts/smoke_ttfa.py`) plays a pre-recorded WAV at the WebSocket, times `status=processing` → first binary frame, and asserts < 1.5 s — catches latency regressions before they ship.

**Vision model swap (D87).** Switched `vision.py`'s MODEL from `gpt-4o-mini` to `gpt-4.1-mini`. Per OpenAI's release notes, same/better quality at ~50% lower latency and ~83% lower cost on short-burst multi-frame calls. Rollback is a one-line revert if the eval pass shows activity/bbox quality regression.

**One last dumb bug (TROUBLESHOOTING #22).** `/api/analyze` still hardcoded `claude-sonnet-4-20250514` even though Phase P had moved `conversation.py` to `claude-sonnet-4-6`. Anthropic sunsets the old model on 2026-06-15 — silent deprecation bomb. Fixed by importing `MODEL` from `app.conversation` into `main.py` so both call sites track one constant.

---

## Phase S — corrections, retunes, and a new sensory channel

Phase S exists because the first post-Phase-R live session surfaced three things worth revisiting: PTZ reality on MeetUp reversed, prompt caching still stuck at zero, and a product question about coughing that opened a real new direction.

**Pan/tilt isn't dead after all (S.1).** A second live probe of MeetUp firmware 1.0.272 reported `pan=[-25,25]` and `tilt=[-15,15]` with `ok=True` — the opposite of what the earlier probe showed (and the opposite of what D82's 2026-04-20 follow-up + D84 now claim). Nudges fired correctly during the session (`pan 0→1 tilt 0→0` three times) but the deltas were too small to be visually perceptible: our `_DELTA_FRACTION * _DAMPING = 0.06` on a range of ±25 rounds to 1-unit steps. Retuned both multipliers to `0.50 * 0.50 = 0.25`, so a mid-frame offset now moves the lens ~6 units per step instead of 1. Whether pan/tilt is *reliably* exposed across sessions and firmware revisions is a live open question — we've now seen both outcomes; the docs call it out as observed-but-conditional.

The other half of S.1 was honesty: SYSTEM_PROMPT used to tell Claude *"you can zoom but not pan/tilt"*, which was always wrong when pan/tilt hardware was present. Replaced the categorical claim with a reference to a per-session "Session camera capabilities" note that `main.py` now appends to the system prompt once at connect time, based on `PTZController.axes_available`. The appended text is session-invariant (capabilities don't change mid-session) so it stays inside the cacheable prefix rather than fragmenting the cache.

**Prompt caching, for real this time (S.2).** D86 had the right structure (static system, per-turn context moved to the user message, breakpoint on `messages[-2]`) but cited the wrong threshold. Claude Sonnet 4.6's minimum cacheable prefix is 2048 tokens, not 1024. Our typical prefix maxes out around 1500–1800 tokens at the 20-message history cap, so we never crossed threshold — every turn logged `cache_read=0`. See TROUBLESHOOTING #23. Also: prompt caching is GA now; the beta header we were sending is no longer required, removed for cleanliness. The cache *will* activate in longer sessions with repeated fact-extraction and vision injection building up the prefix, just not turn-after-turn the way we'd hoped.

**Audio-event scaffold (S.3).** The live session included the user coughing and saying *"I'm surprised you did not catch it."* Whisper drops non-speech sounds, so that was by design — but an elderly-care companion arguably should notice coughs. Shipped `app/audio_events.py` with the full interface + plumbing (Session, Conversation, SYSTEM_PROMPT all know how to handle an `<audio_events>` block alongside `<camera_observations>`). Current classifier is a stub returning `[]`; a follow-up session will swap in Google's YAMNet (521-class AudioSet classifier, ~4 MB ONNX) via `onnxruntime` — which is already installed as a transitive dep. The module docstring lays out the integration recipe. Today the pipeline runs end-to-end with zero detections; the moment a real classifier slots in, coughs / sneezes / gasps start surfacing to Claude.

The `/api/analyze` endpoint also got a long-overdue model fix — it was still hardcoded to `claude-sonnet-4-20250514` (scheduled to sunset 2026-06-15) despite Phase P moving `conversation.py` to `claude-sonnet-4-6`. See TROUBLESHOOTING #22.

**Phase S.4 — streaming TTS, evaluated and deferred.** Motivation was TTFA: measured P50 is 3.3 s, target is <1.5 s; the dominant contributor is `tts_first_byte_ms` at ~1.8 s. On inspection the metric name is itself a little misleading — our consumer `await`s the full audio buffer before sending, so the delay between *"first sentence detected"* and *"first audio byte hits the wire"* includes the entire TTS stream. True streaming could shave 150–500 ms off. The cost is real though: (a) OpenAI's `opus` format returns one logical container and Chrome's `decodeAudioData` needs a complete file before playback — so streaming opus chunks to the client doesn't actually speed up audible playback; (b) true streaming requires switching to `pcm` response format, routing raw samples through an AudioWorkletNode on the client, and dropping the current opus compression (fine on localhost, but a full frontend/server refactor across `tts.py`, `session.py`, and `frontend/index.html` — ~150-200 LOC with new failure modes at chunk boundaries); (c) the savings don't actually get us under the brief's 1.5 s target anyway (best-case TTFA with streaming + everything else is ~2.1 s given Claude TTFT and STT floors). Conclusion: not worth the refactor risk right now. If we revisit, the win is real — just not the headline win it first looked like.

**Phase U — multi-language STT + browser-local pose intelligence (2026-04-21).** After shipping Phase T we asked what else would genuinely make Abide more intelligent without speculative scope creep. Three items passed the cost/benefit check; all three shipped in one pass.

**U.1 — multi-language Whisper auto-detect (one-line code change, zero deps).** `audio.py:transcribe()` used to hardcode `language="en"` on the Groq Whisper call. For an elderly-care companion that's a blocking accessibility limitation — the target resident is often a first-generation immigrant whose primary language isn't English. Dropped the lock and let Whisper auto-detect. Logged the detected language per turn (`[TIMING] STT: ... lang=es`). Claude 4.6 is natively multilingual so it responds in whatever language the transcript is in — no extra plumbing needed. One known residual: the existing hallucination blocklist (`_STANDALONE_HALLUCINATIONS`) matches English phrases only, so non-English false positives bypass it; acceptable tradeoff for now, add per-language patterns if they appear in live testing.

**U.2 — browser-local MediaPipe PoseLandmarker driving smooth PTZ.** Pre-Phase-U, PTZ nudges were bottlenecked by the 2.4-s vision cycle — we only got a fresh bbox from GPT-4.1-mini every 2.4 seconds, so the lens could only move that often. MediaPipe's PoseLandmarker (lite float16, ~3 MB WASM + model) runs locally in the browser at 15-30 fps and gives 33 body keypoints per frame. Derive a min-max bbox from the visible landmarks, emit over WS as a `face_bbox` message (client-side throttled to 5 Hz), and rate-limit again server-side before touching DirectShow. Net effect: pan/tilt updates go from 0.42 Hz to 5 Hz — visibly smoother tracking without touching the DirectShow write rate. Chose MediaPipe over server-side face detection because (a) browser-local keeps the CPU load off the voice loop's thread pool, (b) WASM works on every modern browser on every OS including Mac Intel (no Python wheel dependency like `ai-edge-litert` has on Mac Intel), and (c) we get PoseLandmarker's full skeleton for free, which U.3 uses.

**U.3 — pose-based fall detection as a second signal.** The existing `FALL:` prompt-prefix path (D24) works but is best-effort and we've never stress-tested it against real falls. MediaPipe pose landmarks give us a second signal to compose with it: a simple heuristic looks at the vertical relationship between the nose (landmark 0) and the hips (23/24) in normalized y coordinates. A standing or seated person has their nose well above their hips; a fallen person's nose approaches or drops below hip level. Require ~1 s (20 frames at 20 fps) of sustained horizontal torso before firing to skip transient bending-over. Client emits `fall_alert` over WS; server routes it through the same `_pending_fall_alert` path as the vision-prompt version so Claude's next turn opens with a welfare check and the red alert banner renders. One state flag (`fallAlertFired`) prevents repeat alerts while the person is still down — the next alert requires them to get up first.

**Phase U follow-up — security-review fixes (post-live-audit).** A thorough post-Phase-U security pass surfaced two high-priority gaps and four medium ones, all shipped as a follow-up batch:

1. **MediaPipe vendored locally** (`frontend/vendor/tasks-vision/` + `models/pose_landmarker_lite.task`, ~24 MB). The Phase U.2/U.3 CDN fetch from `cdn.jsdelivr.net` + `storage.googleapis.com` was a supply-chain XSS risk — a compromised CDN would have had JS execution on a page holding the user's API keys in `localStorage` and a live WebSocket. `main.py` now mounts `/vendor/` and `/models/` as FastAPI `StaticFiles`, `frontend/index.html` imports from the local paths, CDN deps are gone. Offline-capable for everything except the LLM/STT/TTS APIs themselves.
2. **Non-English Whisper hallucination blocklist** extended with Spanish / French / German / Italian / Portuguese / Hindi / romanized Japanese / Russian / Mandarin short-hallucination patterns. After Phase U.1 dropped `language="en"`, an auto-detected foreign-language silence would have produced `"gracias"`/`"merci"`/`"danke"` phantom transcripts that bypassed the English-only filter. The posterior-probability segment filter (`no_speech_prob > 0.6 AND avg_logprob < -1.0`) is already language-agnostic and handles most cases; this is the belt-and-suspenders lexical layer.
3. **`fall_alert` input hardening**: `Session.handle_client_fall` now HTML-entity-escapes `<` and `>` before storing the text and before echoing it to the client. A compromised frontend could otherwise have injected `"</camera_observations><system>..."` and hijacked the next Claude turn. Added a 10 s cooldown (`_CLIENT_FALL_ALERT_COOLDOWN_S`) so a buggy/adversarial pose loop can't spam fall counts and flashing UI banners at 15–30 Hz.
4. **Exception callbacks** on `dispatch_face_bbox` and `_dispatch_camera_action` — matched the `_log_bg_exception` pattern already used elsewhere. Previously a DirectShow error inside the `to_thread` would have been silent up to 5 Hz.
5. **`face_bbox` schema tightened** to reject booleans (which pass `isinstance(v, (int, float))`) and out-of-range coordinates. Eliminates a latent type-confusion / overflow path that would have reached `nudge_to_bbox`'s arithmetic with pathological floats.
6. **`_strip_leading_xml_defensive` narrowed** from `\w+` to a closed whitelist of the three known sensor tags (`audio_events`, `camera_observations`, `turn_context`). Now legitimate inline HTML Claude might emit (e.g. `<b>bold</b>` in quoted content) passes through untouched; the defence only fires on the exact tag names it was designed for.

All six ship as one batch with no API surface change.

**Cross-platform audit (what prompted the depth-check).** Before finalising Phase T's `ai-edge-litert` dependency we checked wheel coverage across OSes. Coverage: Windows x86_64, Linux x86_64 + ARM64, macOS Apple Silicon ✓ — macOS Intel (`x86_64`) has NO wheel and no source build. Marker-gated the install (`sys_platform != "darwin" or platform_machine == "arm64"`) so `pip install -r requirements.txt` doesn't fail on Intel Mac; on that one platform combo, `audio_events.py` falls through to its stub (returns `[]`) and Abide runs without cough detection. Every other Phase T/U feature is cross-platform because MediaPipe runs in the browser (WASM) and the fallback paths are explicit.

**Phase T — YAMNet cough detection for real (2026-04-21).** Phase S.3 shipped the `audio_events.py` scaffold with a stub classifier that always returned `[]`. Phase T swaps in real YAMNet: `models/yamnet.tflite` (~4 MB, Google's 521-class AudioSet classifier) loaded via `ai-edge-litert` (Google's desktop TFLite runtime, works on Windows unlike `tflite-runtime`). Input is a fixed 0.975-s window (15 600 samples @ 16 kHz), so longer speech segments get run on overlapping windows with 0.5-s hop and max-pooled per class. Inference is ~280 ms per window on CPU, dispatched via `asyncio.to_thread` so the voice loop never waits on it. Only a curated set of seven AudioSet classes surfaces to Claude — Cough, Sneeze, Throat clearing, Gasp, Wheeze, Snoring, Crying — deliberately skipping Breathing (too common, would fire on every exhale) and Sniff (allergies / cold → routine). Confidence threshold 0.30 picks recall > precision: we'd rather Claude see a speculative *"maybe a cough"* and ask gently than miss a real health signal. Graceful degradation on every failure mode — missing model file, `ai-edge-litert` not installed, tflite error inside invoke — lands in `return []` and the voice loop is unaffected. The hardened SYSTEM_PROMPT from Phase S.3 follow-up (which explicitly forbids Claude from emitting the `<audio_events>` tag itself) still governs how Claude uses incoming events: brief acknowledgement for health-relevant ones, silence otherwise, never fabrication.

**Phase S.3 follow-up #3 — PTZ dead-zone fix (2026-04-21).** Second live session after the S.1 pan speedup revealed a drift bug: tilt monotonically climbed to the +15 rail within ~30 s and stuck there. Cause was the shared dead-zone in `PTZController.nudge_to_bbox` — if `|ox|` was outside the dead zone (pan needed), both pan AND tilt deltas got computed, and for a seated user whose head sits slightly above frame-centre the tilt delta consistently rounded to +1 per cycle. Fix: pan and tilt each check their own offset against the dead zone independently. Also bumped the dead zone from 0.12 → 0.15 in response to user feedback that the S.1 retune made the camera chase small jitter ("we keep moving with the camera").

**Phase S.3 follow-up — hardening + tuning (2026-04-21 live-session feedback).** One live session surfaced five real issues: (1) Claude mirrored the `<audio_events>...</audio_events>` tag schema into its own reply and TTS spoke it out loud — hardened `SYSTEM_PROMPT` with an explicit *"never emit these tags"* block + added a defence-in-depth stream-parser strip for any leading `<tag>...</tag>` pairs; (2) the user noticed PTZ pan was sluggish ("the pan is a little slow, don't you think?") — bumped `_DELTA_FRACTION` 0.50 → 0.70 so the effective per-step motion on MeetUp's ±25 pan range goes from ~6 units to ~9 units; (3) the out-of-frame welfare check fired at ~11 s of absence which felt long — shortened the vision cycle from 3.6 s (3 frames × 3 captures) to 2.4 s (2 frames × 2 captures), which drops the 3-consecutive-cycles threshold to ~7 s without sacrificing the false-fire protection the confirmation gate provides; (4) tilt was observed stuck at ±max across sessions (the camera inherited whatever pose the previous session left) — `PTZController.center()` now runs on session *start* in addition to session end; (5) Claude kept ending replies with 😄 / 👋 which TTS synthesised as ~2 KB of gibberish — added an emoji/pictograph strip in `tts.py` that also skips the API call entirely for emoji-only sentences. Vision calls cost ~33 % more now (same frames per minute, but more frequent calls), traded for a noticeably more attentive out-of-frame response. The shorter cycle also reduces the ceiling on pan-follow responsiveness from 3.6 s/step to 2.4 s/step.

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

## Appendix — Decision index (D1–D99)

Before this rewrite, `DESIGN-NOTES.md` was a flat log of discrete decision records. The full per-entry rationale, alternatives-considered notes, and trade-off analyses live in the repo's git history — `git log --follow DESIGN-NOTES.md` to walk them. This table keeps the cross-reference function so that references like *"see D66"* elsewhere in the repo (README, CLAUDE.md, TROUBLESHOOTING, code comments) still resolve to a one-line summary.

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
| D85 | `p50/p95_turn_latency_ms` are whole-turn metrics, not TTFA — *flagged, resolved in D86* | Q (follow-up) |
| D86 | TTFA + per-stage latency percentiles (stt / claude_ttft / tts_first_byte) + `scripts/smoke_ttfa.py` smoke test; *note: original "turn 3-5 cache activation" prediction was wrong — Sonnet 4.6 threshold is 2048 tokens, see S.2 / TROUBLESHOOTING #23* | Q.2 |
| D87 | Vision model `gpt-4o-mini` → `gpt-4.1-mini` (~50% lower latency, same/better quality) | Q.2 |
| D88 | PTZ retune (delta/damp 0.20/0.30 → 0.50/0.50) + `axes_available` + per-session capability note appended to SYSTEM_PROMPT via `ConversationEngine.system_prompt_override`; pan/tilt observed as `ok=True` on MeetUp 1.0.272, reversal from D82/D84 is *conditional/stability-TBD* | S.1 |
| D89 | Audio-event classifier scaffold (`app/audio_events.py`) + plumbing through Session → Conversation → `<audio_events>` ambient block + SYSTEM_PROMPT NON-SPEECH AUDIO section; stub returns `[]` — upgraded to real YAMNet in D90 | S.3 |
| D90 | YAMNet TFLite classifier slotted into `app/audio_events.py` via `ai-edge-litert`; 7 curated health-relevant AudioSet tags (Cough, Sneeze, Throat clearing, Gasp, Wheeze, Snoring, Crying); ~280 ms/window CPU inference in `asyncio.to_thread` | T |
| D91 | Multi-language Whisper auto-detect — dropped `language="en"` from the Groq Whisper call in `audio.py:transcribe()`; detected language logged per turn; Claude 4.6 multilingual so replies match user's language with no extra plumbing | U.1 |
| D92 | Browser-local MediaPipe PoseLandmarker for smooth PTZ — pose landmarks → min-max bbox → `face_bbox` WS → `Session.dispatch_face_bbox` → 5 Hz rate-limited `PTZController.nudge_to_bbox`; lifts pan/tilt update rate from 0.42 Hz (GPT-4.1-mini vision cycle) to 5 Hz | U.2 |
| D93 | Pose-based fall heuristic — nose y ≥ hip y sustained ~1 s → `fall_alert` WS → `Session.handle_client_fall` → same `_pending_fall_alert` path as the vision-prompt `FALL:` prefix; runs as a second signal composed with D24, not a replacement | U.3 |
| D94 | Log-driven fixes from live session `abide-585f1dec5ee2` (51 turns, 549 s): (a) fall-pose rule changed from "nose within 0.08 above hip" to "nose clearly below hip by 0.05" sustained 45 frames (~1.5 s) — killed 5 false fires on seated-leaning-forward; (b) SYSTEM_PROMPT zoom trigger narrowed to literal zoom words, explicitly rejecting visibility questions — stopped spurious `zoom_in` on "can you see my face?"; (c) `_ZOOM_USER_MAX = 200` soft cap on user-driven zoom-in only (reset/out untouched) per user feedback "300 is too much"; (d) `MAX_HISTORY 20 → 60` so the prompt-cache prefix stops sliding as history truncates — root cause of `cache_read=0` across all 51 turns. Receive-side `face_bbox` rate log line in `main.py` every 100 msgs to diagnose the concurrent TTFA regression (P50 ~3300 → ~4968 ms on long sessions) | U.3 (follow-up) |
| D95 | Live session `abide-621b915bf3e3` (96 turns, 719 s) follow-up: (a) **pose-based fall heuristic disabled entirely** — D94's tightened rule still fired 3× on standing-at-desk-bent-over-laptop (head dips below hip in image-y, no ankle landmarks visible to disambiguate); vision-prompt `FALL:` prefix is the sole fall path now. (b) **Fall banner CSS rewrite for light-mode legibility** — saturated crimson bg + white text on both themes (old light-pink palette disappeared on `--bg: #f8f5ef`). (c) **Pose bbox narrowed to landmarks 0–12** (face + shoulders) so camera stops chasing hands across a keyboard — user quote: "you're tracking my hands, you're acting like an AC moving left to right." (d) **SYSTEM_PROMPT user-perspective left/right rule** — camera observations are camera-frame coordinates, but Claude must translate to the user's own left/right when speaking, and avoid left/right when ambiguous. Prompt-cache activation confirmed: `cache_read` reached 2100+ tokens from ~turn 15 onward, validating D94's `MAX_HISTORY` fix | U.3 (follow-up #2) |
| D96 | Six-change latency + noise-floor bundle from the same 96-turn session, aimed at the user's *"you'll have to work on the latency of Abide"* complaint: (a) **audio-event classifier parallelised with STT** — YAMNet now runs as a background `asyncio.Task` started before `await transcribe`, awaited just before `start_response`; removes ~200–280 ms of TTFA drag since YAMNet typically completes alongside Groq Whisper. (b) **New TTFA anchors** — `speech_end → _run_response started` + `speech_end → first sentence boundary` diagnostic log lines so the ~2.7 s unaccounted gap (TTFA P50 5.6 s vs STT+Claude+TTS sum ~2.9 s) becomes visible on the next run. (c) **Vision `noteworthy` rewrite** — requires both "clear state transition" AND "high confidence not mid-activity motion"; typing, reaching, scratching, posture shifts, head turns now explicitly FALSE; target < 5 % of frames TRUE over a session. Fixes the over-eager proactive reactions observed in live logs. (d) **Whisper filter**: mixed-script detection (Latin + CJK/Cyrillic/Arabic in the same transcript) + single-token alphanumeric floor — catches `'Que é我跟你講 not...'`-style fabrications without over-rejecting real one-word replies. (e) **Vision timeout** `asyncio.wait_for(..., 8.0)` — one 11.25 s outlier seen in live session; 8 s lets us drop the batch and recover on the next 2.4 s cycle. (f) **Claude `max_tokens` 300 → 180** — live output averaged 15–30 tokens peaking at 51; 180 is a tighter cap that complements SYSTEM_PROMPT's brevity instruction without clipping multi-sentence replies | U.3 (follow-up #3) |
| D97 | Stall-resilience bundle after live session `abide-63d2e9245567` saw three back-to-back Anthropic-side stalls (Claude responses with `in=None out=None cache_read=None`, total_ms 4141 / 12203 / 17328, never received `message_start`). Silence read as "Abide is dead" to the user. Six changes: (a) **Claude first-token deadline** via `asyncio.timeout(15.0)` wrapped around the stream setup in `conversation.py`; `timeout_cm.reschedule(None)` on first `text_delta` disables the deadline for the rest of the response. On trip: `[STALL]` WARNING log + `ConversationError("Give me a moment — trouble reaching my services, try again.")`. (b) **TTS first-byte deadline** — same pattern, 10 s cap in `tts.py`; timeout returns `b""` so the sentence is skipped cleanly. (c) **Post-hoc stall detector** — any Claude call ending with zero output tokens and `total_ms > 5000` is logged as `[STALL] Claude turn produced zero output in Xms`, making these cases greppable across sessions. (d) **`ConversationError` message routing** — Session's catch-all used to hardcode a generic "Something went wrong"; now type-checks for `ConversationError` and passes its user-safe message through to the client. (e) **YAMNet `_MAX_WINDOWS` 5 → 3** — classifier now scans only the first 1.5 s of audio (was 3.5 s); kills the 2+ s TTFA drag the D96 anchor exposed on long utterances. Cough/sneeze/gasp events reliably live in the first window (they interrupt speech). (f) **Frontend "Still thinking…" hint** — `setStatus()` now schedules a 3 s timer on entry to thinking/processing; if the state is still active at timeout, the pill label softens so silence doesn't imply death. Cleared on any transition | U.3 (follow-up #4) |
| D98 | YAMNet pre-load on connect. Live session `abide-a1265a3e45be` confirmed a consistent turn-1 `speech_end → _run_response started = 2532 ms` tax (subsequent turns: 1100-1900 ms) caused by `Interpreter.__init__` + `allocate_tensors` lazy-running inside the first `classify_segment()` call. `audio_events.prewarm()` new public coroutine wraps `_load_once()` in `asyncio.to_thread`; main.py's connect-time prewarm fan-out fires it alongside Claude / TTS / Vision prewarms. Eager load moves the cost out of the user-visible TTFA window into the connect-to-first-utterance window. No API key required (local model), runs unconditionally. Chose NOT to ship streaming TTS chunks to WS (listed as an option in #26): frontend `decodeAudioData` requires complete Ogg-opus containers so chunked WS delivery doesn't enable progressive playback — the ~100-200 ms savings would require switching OpenAI TTS `response_format` to `pcm` + AudioWorklet pipeline, which is a major architecture change incompatible with the "small polish" framing. Documented as a considered tradeoff in the demo write-up — "chose predictable cloud-API latency over an architecturally expensive streaming pipeline that would compromise cold-start simplicity." | U.3 (follow-up #5) |
| D99 | Pre-demo security + robustness sweep (8 items from parallel security / performance / error-handling reviews). **Security:** (a) `<>` escape symmetry on the vision path — `SceneResult.activity` and `.motion_cues` pass through `&lt;`/`&gt;` replacement before injection into `<camera_observations>`, closing the tag-injection gap if the vision model ever mirrored a closing-tag lookalike; (b) uvicorn now binds to `127.0.0.1` in `start.bat`/`start.sh` (was `0.0.0.0`), so neither the WebSocket endpoint nor the `/api/analyze` anonymous-Anthropic-proxy surface is reachable from the LAN. **Diagnosability:** (c) the shutdown-race `Response pipeline error:` log now prints `type(e).__name__: repr(e)` instead of a raw `%s` that rendered empty for `anyio.ClosedResourceError` etc. **Robustness:** (d)–(g) `add_done_callback` on `checkin_task`, `audio_events_task`, `_frame_task`, and the `save_user_context` executor future — surfaces schedule-side / executor exceptions instead of Python's GC-time generic warning; (h) replaced the discard-only lambda on the Langfuse probe task with `_log_prewarm_exception` for consistency with the rest of the prewarm fan-out. None touch the happy path; all make the failure-mode surface observable | U.3 (follow-up #6) |
