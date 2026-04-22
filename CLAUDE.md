# Abide Companion — AI Elderly Care Product

## Product Context
Abide Robotics (abide-robotics.com) builds a three-tier care product:

1. **Resident Companion** — an in-room robotic companion for older adults.
   Voice-first, 24/7, supports daily routine (medications, meals, hydration).
   Target hardware: Reachy Mini by Pollen Robotics.
2. **Caretaker Mobile App** — alert inbox + context portal for care staff.
3. **Facility Dashboard** — live overview + longitudinal trend data.

**This codebase implements tier 1 — the Resident Companion.** It runs on
commodity hardware (Logitech MeetUp, or any webcam + mic + speaker) today
and is designed to port cleanly to Reachy Mini when the hardware ships.
Tiers 2 and 3 are out of scope for this build; integration points
(Langfuse session traces, fall-alert events) are in place so a future
caretaker app can subscribe to them.

Mission: *to care for those who can't care for themselves.*

## Project Context
Real-time multimodal AI companion for elderly care (Abide Robotics).
Primary target: non-technical end users on Windows (native Python 3.12+).
Must work without developer involvement during first-run and daily use.

## Stack (non-negotiable)
- Backend: FastAPI (Python)
- Frontend: Single HTML/JS file — no React, no build tools, no npm
- STT: Groq Whisper API
- Vision: GPT-4.1-mini (frame sampling every 2.4s, 2 frames per call; was 3/3 before S.3 follow-up tuning; was gpt-4o-mini before D87)
- Conversation: Claude API (claude-sonnet-4-20250514)
- TTS: OpenAI TTS (tts-1, nova voice, opus format)
- VAD: silero-vad (runs locally — deliberate latency optimization, no API call)
- Packaging: native Python 3.12+ in a local `.venv`, launched via `start.bat` / `start.sh` (one double-click, first-run creates venv + pip installs). Docker was dropped in Phase N (D82) because its WSL2 backend on Windows can't reach DirectShow, which blocks MeetUp pan/tilt control.

## First-Run Experience (CRITICAL)
End users install and run with zero technical knowledge.
Acceptable steps:
1. Install Python 3.12+ (one link, one installer)
2. Double-click start.bat
3. Browser opens automatically
4. Enter API keys in browser UI
5. Click Start

NOT acceptable:
- Any terminal commands after initial setup
- Installing dependencies manually
- Multiple steps beyond the above

## API Keys
- Groq, Anthropic, OpenAI keys: entered in browser UI, stored in localStorage
- Langfuse keys: stored server-side in .env (developer-only, never shown in UI)
- End user supplies exactly 3 keys: Groq, Anthropic, OpenAI

## Hard Requirements
- Barge-in: fires at ~150ms on Logitech MeetUp (gated on 150ms sustained speech + ≥4 loud VAD windows, ≈128ms cumulative above-threshold audio). Tuned for MeetUp's hardware AEC which strips echo at the device level. On a laptop mic+speaker (no hardware AEC), revert to 400ms / 6 windows — see DESIGN-NOTES.md D80
- Use Web Audio API for playback (NOT <audio> element)
- TTS starts on first sentence boundary, not after full Claude response
- Latency target: <1.5s to first audio after user finishes speaking
- HTTP/2 persistent clients everywhere — never create per-request clients
- System must run stable for 10-15 min continuously without degradation
- Graceful degradation: partial speech, user out of frame, noisy audio must not crash

## UI Requirements
- Real-time transcript of conversation (always visible)
- System interpretation of current user activity (bounding box overlay + scene chip)
- Status indicator: listening / thinking / speaking
- Correction flow: system confirms interpretation, user can say "no that's wrong"
- Small talk capability
- Abide Robotics brand UI — 16:9 rounded rectangle hero with amber glow, warm palette, light/dark mode toggle
- Playfair Display (serif) headings + Inter (sans-serif) body to match abide-robotics.com
- Privacy notice displayed in UI
- Fall detection alert banner (red, auto-hides after 20s)

## Proactive Behavior (COMPLETE — D51, D52, D53, D54)
- System prompt positions Abide as a live companion robot, not a chatbot
- 30-second proactive check-in: if user is silent, Abide initiates based on vision
- UserContext persistence: extracts name, topics, preferences, mood from conversation
- User facts injected into every Claude turn ("What I know about you: ...")
- Vision activity buffer: last 5 observations with relative timestamps
- Vision-reactive trigger: the vision model emits a `noteworthy` boolean alongside each scene; Session fires a proactive turn when `noteworthy` is true AND the activity text changed since the last observation AND the user has been silent ≥10s. No hard-coded activity allowlist — the model decides semantically (D54)
Implementation: `CHECK_IN_INTERVAL_S` + `_proactive_checkin_loop()` in `app/main.py`,
`UserContext` dataclass + `_extract_user_facts()` in `app/session.py`,
`extract_user_facts()` in `app/conversation.py`.

## Session Summary (COMPLETE — D50)
When user clicks Stop, a full-screen glass-morphism overlay shows:
- Session duration (start time — end time)
- Complete conversation transcript with timestamps
- Activity log with timestamps (all vision observations + fall alerts)
- Claude-generated analysis of what Abide got right vs. wrong
Implementation: `showSessionSummary()` + `fetchAnalysis()` in `frontend/index.html`,
`POST /api/analyze` endpoint in `app/main.py`.

## Diary View (COMPLETE — D49)
Tab alongside "Conversation" in the transcript panel showing:
- Full timestamped interaction history (user, assistant, vision, alerts)
- Color-coded type badges
- Live-updating during session, scrollable after session ends
- Cleared on next Start; exportable to .txt. Refresh ends the session (no restore)
Implementation: `diaryEntries[]` array + `renderDiaryEntry()` + `switchTab()` in `frontend/index.html`.

## Vision Requirements
- Multi-frame: 2 JPEGs per call, 1.2s apart, sent every 2.4s (was 3/3 / 3.6s before S.3 follow-up tuning)
- Output schema emits `motion_cues` (chain-of-thought grounding) → `activity` → `noteworthy` → `bbox`. Model must reason about observed frame differences before classifying.
- Scope-matching label rule (WHOLE-BODY / LIMB / HAND-OBJECT / STATIC) — label granularity must match motion scope. When motion spans scopes, pick the largest visible. Generalizes across activities we don't enumerate (dance, fall, slip, jump, lift-a-leg, wave, reach, bend, stretch, eat, drink).
- `noteworthy: bool` is the vision model's own judgment on whether the scene is worth a proactive reaction. Replaces the old `_REACTIVE_ACTIVITIES` keyword allowlist in `session.py`.
- Bounding box overlay rendered on canvas (computed from the LAST frame)
- Rolling buffer of last 5 scene descriptions with relative timestamps
- Fall detection: `FALL:` prefix → red alert banner + urgent Claude context. Covers near-falls (slipping, stumbling, catching themselves on furniture) — err on the side of flagging.
- Prompt injection defense: vision context wrapped in <camera_observations> block

## Module Status
1. ✅ Skeleton + audio loopback
2. ✅ STT pipeline (silero-vad + Groq Whisper)
3. ✅ Conversation engine (Claude streaming)
4. ✅ TTS pipeline (OpenAI, sentence-boundary, parallel producer/consumer)
5. ✅ Barge-in (cooperative cancellation, loud-window-count gated, client_playing flag)
6. ✅ Vision pipeline (multi-frame, bbox, fall detection)
7. ✅ Native Python packaging (was Docker until Phase N — see D82)
8. ✅ Langfuse observability
9. ✅ Session summary screen
10. ✅ Diary view
11. ✅ TTS cache for stock phrases (D59)
12. ✅ Whisper name biasing via UserContext (D60)
13. ✅ Welcome greeting on connect (D61)
14. ✅ Vision confidence indicator (D62)
15. ✅ Activity stability filter in VisionBuffer (D63)
16. ✅ Diary export button (D64)
17. ✅ Auto-populated TTS cache — runtime-learned phrase frequencies replace the hand-curated seed list (Phase I)
18. ✅ Time-of-day awareness — browser-reported timezone drives 4-variant welcome + system-prompt injection in every Claude turn (D75, easter egg)
19. ✅ Cross-session UserContext persistence — per-device `resident_id` UUID keys `./memory/<id>.json`; name/topics/preferences/mood hydrated on connect, saved after each fact-extraction. Conversation history stays ephemeral. "Forget me" button in the gear drawer wipes (Phase E, D78)
20. ✅ Out-of-frame welfare check — Abide verbally checks in after ~11s of sustained absence. Works on every browser / every camera. PTZ subject-follow was attempted via W3C MediaCapture-PTZ but removed after empirical confirmation that Logitech doesn't expose pan/tilt over UVC on any MeetUp firmware (verified against Google's reference demo — MeetUp reports zoom but not pan/tilt). Motion tracking on MeetUp is only possible via Logitech's proprietary Sync/Tune SDK, inaccessible from a browser (Phase K, D79)
21. ✅ MeetUp-tuned barge-in — hardware AEC on the MeetUp allows dropping the sustained-speech gate from 400ms→150ms and loud-window requirement from 6→4. Barge-in feels ~2.5× more responsive (~200ms in live testing). Revert to laptop values (400/6) for deployments without hardware AEC (Phase L, D80)
22. ✅ Personalized welcome greeting — when UserContext has hydrated a name on connect, the welcome picks a name-aware variant ("Good morning, Shree! I'm Abide. How are you today?"). The specific greeting is prewarmed in parallel with the other seed phrases so it still serves at ~0ms. First-time users (no name) hear the generic greeting unchanged (Phase M, D81)
23. ✅ Native Python deployment — dropped Docker in favour of `.venv` + `pip install` + `uvicorn`, all triggered by one double-click on `start.bat`. Enables Phase N.2 below. See D82 for rationale (Phase N.1, D82)
24. ⚠ PTZ via `duvc-ctl` — native-Python DirectShow access to camera controls. Earlier probes of MeetUp firmware 1.0.272 returned `Pan: ok=False / Tilt: ok=False`; a later live session on the same firmware returned `Pan: ok=True [-25, 25] / Tilt: ok=True [-15, 15] / Zoom: ok=True [100, 500]` and `nudge_to_bbox` fired real pan nudges. **Pan/tilt availability on MeetUp is conditional — stability across sessions/firmware revs is still under observation.** Per-session capabilities are now detected at connect via `PTZController.axes_available` and baked into Claude's system prompt so the assistant only claims motion the current hardware actually supports. On-request zoom (item 28) still works reliably on every MeetUp. (Phase N.2 + S.1, D82 + D88)
25. ✅ Anthropic prompt caching — `system` field is now an array of content blocks with `cache_control: ephemeral` on the static SYSTEM_PROMPT. Cache kicks in once the prefix crosses Anthropic's 1024-token threshold (usually turn 3-5). Verifiable via `cache_read_tokens` / `cache_creation_tokens` in the `[TIMING] Claude response complete` log line. (Phase O)
26. ✅ Claude Sonnet 4.6 — upgraded from `claude-sonnet-4-20250514` to `claude-sonnet-4-6`. One-line model-ID change for ~10-20% TTFT improvement. (Phase P, D83)
27. ✅ Latency percentiles in session summary — P50 and P95 (with max-fallback when <20 samples) now logged and pushed to Langfuse alongside avg/min/max. More evaluator-useful than avg alone. (Phase Q)
28. ✅ On-request optical zoom — user says "zoom in / out / reset"; Claude emits an inline `[[CAM:zoom_in]]` marker at the head of its reply; the server strips it from the transcript and dispatches `PTZController.zoom()` off-loop so the lens motion overlaps with the verbal ack. `PTZController` init relaxed to accept any single PTZ axis and logs per-axis probe results at INFO for diagnostics. (Phase R, D84)
29. ✅ TTFA + per-stage latency percentiles — Session.stats grew four new sample lists (`ttfa_ms_samples`, `stt_ms_samples`, `claude_ttft_ms_samples`, `tts_first_byte_ms_samples`); session-summary log + Langfuse now carry P50/P95 per stage alongside the existing whole-turn metric. Each turn logs `[TIMING] TTFA: Xms`. `scripts/smoke_ttfa.py` is a standalone CLI that plays a WAV at the WebSocket and asserts TTFA < 1.5s. Resolves the metric-definition gap documented in D85. (Phase Q.2, D86)
30. ✅ Vision model bump — `gpt-4o-mini` → `gpt-4.1-mini` in `app/vision.py`. Same/better quality at ~50% lower latency and ~83% lower cost on short-burst multi-frame calls per OpenAI's release notes. One-line revert if activity/bbox quality regresses. (Phase Q.2, D87)
31. ✅ PTZ retune + per-session capability injection — `_DELTA_FRACTION` / `_DAMPING` bumped from 0.20 / 0.30 to 0.50 / 0.50 so MeetUp's tiny ±25 pan range produces visible motion. `PTZController.axes_available` reports what axes this hardware actually exposes; `main.py` appends a per-session "Session camera capabilities" note to `SYSTEM_PROMPT` via `ConversationEngine.system_prompt_override` so Claude doesn't claim motion it can't deliver. Pan/tilt on MeetUp observed working on firmware 1.0.272 — stability across sessions/firmware still under observation. (Phase S.1, D88)
32. ✅ Audio-event classifier — `app/audio_events.py` now runs real **YAMNet** (Google's 521-class AudioSet classifier) via `ai-edge-litert` on `models/yamnet.tflite` (~4 MB, lazy-loaded on first call). Curated surface of seven health-relevant tags (Cough, Sneeze, Throat clearing, Gasp, Wheeze, Snoring, Crying) — the full 521-class ontology is filtered down so Claude doesn't see every breath. Overlapping 0.975-s windows with 0.5-s hop, per-class max-pool; runs in `asyncio.to_thread` so the ~280 ms inference doesn't touch the voice loop. Plumbed through Session / Conversation / SYSTEM_PROMPT's NON-SPEECH AUDIO block (upgrade from the Phase S.3 stub). Falls back cleanly to `[]` if model files are missing or `ai-edge-litert` isn't installed. (Phase T, D90; upgrades Phase S.3 / D89)
33. ✅ Prompt-cache threshold correction — Sonnet 4.6 requires 2048 tokens for activation (not 1024, which was the Sonnet-4.5-and-older number we'd been citing). Docs + code comments corrected; beta header removed since prompt caching is GA. Cache activates later than D86 predicted — around turn 15+ for typical sessions rather than turn 3-5. See TROUBLESHOOTING #23. (Phase S.2)
34. ✅ Multi-language Whisper auto-detect — dropped `language="en"` from Groq Whisper call in `app/audio.py`. The model now detects the user's language per-turn and logs it (`[TIMING] STT: ... lang=es`). Claude 4.6 is multilingual so replies come back in the detected language without extra plumbing. Opens Abide to non-English-speaking elderly users who couldn't use the tool before. (Phase U.1, D91)
35. ✅ MediaPipe pose landmarks in browser — Phase U.2 + U.3. `frontend/index.html` loads MediaPipe Tasks Vision (PoseLandmarker, lite float16, ~3 MB) lazily from CDN when vision starts; runs ~15-30 fps locally on CPU via WASM. Two consumers: (a) min-max bbox over visible landmarks → `face_bbox` WS messages at up to 5 Hz → `Session.dispatch_face_bbox` → rate-limited `PTZController.nudge_to_bbox` at max 5 Hz, so PTZ feels smooth instead of stepping every 2.4 s; (b) fall heuristic (nose y ≥ hip y sustained ~1 s) → `fall_alert` WS message → `Session.handle_client_fall` → same `_pending_fall_alert` path as the vision-prompt `FALL:` prefix, so Claude's next reply opens with a welfare check. Graceful degradation: if the CDN blocks or WASM fails, `poseLandmarker` stays null and the rest of Abide runs unchanged against the GPT-4.1-mini bbox cycle. (Phase U.2 + U.3, D92 + D93)
36. ✅ Log-driven fixes from live session `abide-585f1dec5ee2` — four targeted changes after the 51-turn live session: (a) **fall-pose heuristic tightened** — nose must be clearly BELOW hip (`FALL_NOSE_BELOW_HIP = 0.05`) and sustained 1.5 s (`FALL_THRESHOLD_FRAMES = 45`), fixing 5 false positives the user hit while leaning over a table; (b) **zoom-marker trigger narrowed** — SYSTEM_PROMPT explicitly lists literal zoom words as the only valid triggers and calls out visibility questions ("can you see my face?") as non-triggers, preventing the `zoom_in` emitted on "are you able to see my face?"; (c) **zoom soft-cap at 200** — `_ZOOM_USER_MAX` keeps `[[CAM:zoom_in]]` from pushing past the comfortable frame crop ("300 is too much zoom," user quote); (d) **MAX_HISTORY 20 → 60** — cache prefix stops shifting on every turn as the sliding window truncates old messages, which was why `cache_read=0` on all 51 turns of the live session. Bonus: `face_bbox` receive-rate log line every 100 messages in `main.py` to diagnose the TTFA regression (P50 ~3300 ms → ~4968 ms). (Phase U.3 follow-up, D94)
37. ✅ Follow-up from live session `abide-621b915bf3e3` — four more fixes: (a) **pose-based fall heuristic disabled** — the tightened threshold from D94 still false-fired 3× on standing-at-desk-bent-over-laptop where nose dips below hip with no ankle landmarks visible to disambiguate; vision-model `FALL:` prefix continues to handle genuine falls. (b) **Fall banner made legible on light mode** — old palette (`#ffd8df` text on `rgba(255,100,120,0.18)` bg) was invisible against `--bg: #f8f5ef`; now saturated crimson bg + white text on both themes. (c) **Pose bbox restricted to face + shoulders (landmarks 0-12)** — previous whole-body bbox meant every hand gesture shifted the centroid and PTZ tracked hands not face (live-session user quote: "you're tracking my hands" / "acting like an AC moving left to right"); face/shoulder anchor is stable against keyboard typing and table gestures. (d) **SYSTEM_PROMPT: user-perspective left/right convention** — camera observations use camera-frame coordinates but spoken replies now use the user's own left/right, with guidance to avoid left/right language entirely when ambiguous ("my right is abide's left and my left is abide's right" — user quote). Cache working (`cache_read=2100+` observed from turn ~15 onward as predicted). (Phase U.3 follow-up #2, D95)
38. ✅ Latency + noise-floor bundle from the same session — six changes aimed at the user's "you'll have to work on the latency of Abide" complaint + the chatty/noisy overall feel: (a) **audio-event classifier runs in parallel with Whisper STT** — previously sequential (`await transcribe` → `await classify_segment` → `start_response`); now YAMNet is kicked off as a background task the moment the speech PCM is pulled, and awaited right before start_response. Saves ~200-280 ms of TTFA on every turn. (b) **New instrumentation to expose the ~2.7 s unaccounted TTFA gap** — `[TIMING] speech_end → _run_response started` + `[TIMING] speech_end → first sentence boundary` so future sessions show which slice hides the missing time. (c) **Vision `noteworthy` rule rewritten** — requires both "clear state transition" AND "high confidence it isn't motion inside an ongoing activity"; explicitly lists typing/reaching/posture-shifts/scratching/head-turns as `noteworthy=false`; target is <5% of frames TRUE. Curbs the "over-eager proactive reactions" problem (every arm raise triggered Claude). (d) **Whisper filter: mixed-script rejection + single-token alphanumeric floor** — catches `'Que é我跟你講 not...'` (Portuguese + Chinese + English fabrication) and single-char garbage while leaving real one-word replies (`'yes'`, `'no'`, `'why'`) intact. (e) **Explicit 8 s vision timeout** via `asyncio.wait_for` — one 11 s spike observed in live logs; 8 s lets us fail-fast and resume on next 2.4 s vision cycle. (f) **Claude `max_tokens` 300 → 180** — live session output_tokens averaged 15-30 with a 51 peak (rhyme); 180 is a tighter cap that pressures brevity alongside the SYSTEM_PROMPT instruction. (Phase U.3 follow-up #3, D96)
39. ✅ Stall-resilience bundle after live session `abide-63d2e9245567` saw three Anthropic-side stalls (4 s / 12 s / 17 s with `in=None out=None`, never received `message_start`) that read as "Abide is dead" to the user: (a) **Claude first-token deadline** — `asyncio.timeout(15.0)` around the stream setup + first-token wait in `conversation.py`; once first text_delta arrives `timeout_cm.reschedule(None)` disables the deadline so the rest of the response runs unrestricted. On timeout: WARNING log + `ConversationError` with user-safe message "Give me a moment — trouble reaching my services, try again." (b) **TTS first-byte deadline** — same pattern, 10 s cap in `tts.py`; timeout returns empty bytes so the consumer skips the sentence cleanly. (c) **Stall detector** — any Claude response ending with `out_tokens=None` and `total_ms > 5000` is now logged at WARNING with `[STALL]` prefix so these cases are greppable across sessions. (d) **`ConversationError` message now passes through to the UI** — previously Session's except clause hardcoded a generic "Something went wrong" string, hiding the specific friendly message. (e) **YAMNet windows capped at 3** (was 5) — classifier now scans only the first 1.5 s of audio per utterance instead of up to 3.5 s; trims 1-2 s of TTFA drag on longer utterances since the `speech_end → _run_response started` anchor (shipped in D96) showed YAMNet processing time was overflowing STT on long inputs. Cough/sneeze/gasp events reliably live in the first window anyway (coughs interrupt, they don't happen mid-sentence). (f) **Frontend "Still thinking…" escalation** — after 3 s in the thinking/processing state with no audio, the status pill label softens so silence doesn't read as "dead". Cleared on any state transition. (Phase U.3 follow-up #4, D97)
40. ✅ **YAMNet pre-load on connect** — `audio_events.prewarm()` new public coroutine wraps `_load_once()` in `asyncio.to_thread`; fired from main.py's connect-time prewarm fan-out alongside Claude / TTS / Vision prewarms. Prior behaviour: tflite interpreter lazy-loaded inside the first `classify_segment()` call, paying ~600-900 ms of model+tensor init inside the TTFA window of turn 1. Live session `abide-a1265a3e45be` first-turn `speech_end → _run_response started = 2532 ms` confirmed the hypothesis; subsequent turns were 1100-1900 ms. Eager load in the connect-to-first-utterance window moves the cost out of the user-visible critical path. No API key required (model is local). Unconditionally runs even when `openai_key` is missing. Conscious decision to NOT ship streaming TTS-chunks-to-WS: frontend uses `decodeAudioData` which requires complete Ogg-opus containers, so streaming chunks to the client doesn't enable progressive playback — the ~100-200 ms savings would require switching `response_format` to `pcm` + AudioWorklet progressive playback, which is a major architecture change incompatible with the "small polish" framing for the demo milestone. Documented in write-up as a considered tradeoff. (Phase U.3 follow-up #5, D98)
41. ✅ **Pre-demo security + robustness sweep** — eight findings from a parallel security / performance / error-handling review, all shipped: (a) `<>` escape symmetry on the vision path — `SceneResult.activity` and `.motion_cues` now pass through the same `&lt;`/`&gt;` replacement that `handle_client_fall` has always used, closing the `</camera_observations>` injection gap if the vision model ever emitted a closing-tag lookalike. (b) `start.bat` / `start.sh` bind uvicorn to `127.0.0.1` (was `0.0.0.0`) so the WebSocket + `/api/analyze` endpoints aren't exposed to the LAN — Abide runs on the user's own machine, loopback is all we need. (c) The `ERROR:abide.session:Response pipeline error:` empty-string log at session shutdown now logs `"%s: %r", type(e).__name__, e` so the type is visible even when `str(e) == ""` (e.g. `anyio.ClosedResourceError`). (d)–(g) `add_done_callback` on four background tasks that previously had none: `checkin_task`, `audio_events_task`, `_frame_task`, and the executor future for `save_user_context`. Surfaces disk-full / permission / schedule-side exceptions that would otherwise disappear at GC time with Python's generic "Task exception was never retrieved" message. (h) Replaced the discard-only lambda on the Langfuse connectivity-probe task with `_log_prewarm_exception("Langfuse probe")` for consistency with the rest of the prewarm fan-out. None of these change the happy path; all make failure modes visible. (Phase U.3 follow-up #6, D99)

## Never Do
- Use React, Vue, or any frontend framework
- Use <audio> element for TTS playback
- Wait for full Claude response before starting TTS
- Require terminal commands from the end user
- Add unnecessary dependencies outside the requirements.txt (every added dep is a failure mode on first-run pip install)
- Create httpx/Groq/Anthropic clients per-request
- Put user speech examples in Whisper prompt (causes hallucinations)
- Send raw exception messages to the client (security risk)
- Call start_response() without checking/cancelling existing task (race condition)
- Use bare ws.send_json() in main.py — always use Session._safe_send_json()

## Verification Checklist
- [ ] Barge-in: speak mid-response, system stops within ~420ms
- [ ] First-run: follow README as a non-technical user, zero errors
- [ ] Stability: run 10-15 min continuously, no crashes
- [ ] Corrections: say "no that's wrong" → handled gracefully
- [ ] Partial input: mumble or speak out of frame → no crash
- [ ] Activity detection: sit, stand, wave, fold → Claude describes it
- [ ] Fall detection: lie down → red alert banner appears
- [ ] Session summary: click Stop → summary screen shows
- [ ] Diary view: timestamped log visible and scrollable
- [ ] Langfuse: traces visible in dashboard after session

## File Structure
abide-companion/
├── CLAUDE.md
├── DESIGN-NOTES.md
├── TROUBLESHOOTING.md
├── start.bat            # Windows launcher — creates .venv, pip install, uvicorn, opens browser
├── start.sh             # Mac/Linux equivalent
├── requirements.txt     # Python deps incl. duvc-ctl on Windows for PTZ
├── README-SETUP.txt
├── .env                 # Langfuse keys only (developer-only)
├── .env.example         # Template with security warning header
├── app/
│   ├── main.py          # FastAPI app, WebSocket, serves frontend
│   ├── session.py       # Per-connection state, barge-in coordinator
│   ├── audio.py         # silero-vad + Groq Whisper STT
│   ├── vision.py        # GPT-4o multi-frame analysis
│   ├── conversation.py  # Claude API streaming (direct httpx, no SDK)
│   ├── tts.py           # OpenAI TTS streaming (opus, parallel)
│   ├── tts_cache_store.py # Runtime-learned phrase frequency store (auto-prewarm list)
│   ├── memory.py        # Cross-session UserContext persistence (Phase E)
│   ├── ptz.py           # DirectShow PTZ subject-follow via duvc-ctl (Phase N, Windows-only)
│   └── telemetry.py     # Langfuse tracing (graceful no-op if missing)
├── frontend/
│   └── index.html       # Single-file UI
└── tests/
    ├── test_audio.py
    ├── test_vision.py
    ├── test_conversation.py
    └── test_tts.py

## Key Dependencies
fastapi>=0.110
uvicorn[standard]>=0.27
websockets>=12.0
httpx[http2]>=0.27
h2>=4.1
anthropic>=0.40
openai>=1.30
groq>=0.5
torch>=2.0
silero-vad>=4.0
numpy>=1.26
langfuse>=2.60,<3.0
python-dotenv>=1.0

## Known Limitations
- Barge-in fires at ~420ms not 100ms (echo suppression tradeoff)
- Vision bbox coordinates approximate, not surgical
- Fall detection is best-effort, no emergency dispatch
- Conversation history caps at 60 messages (~30 turns — bumped from 20 in Phase U.3 follow-up so the prompt-cache prefix stays stable for typical 20-30 min sessions)
- Cross-session persistence is limited to `UserContext` facts (Phase E). Conversation turn history is deliberately ephemeral per-session; Claude starts with a clean slate each reconnect. Calendar/schedule integration (per Abide Robotics demo) is out of scope.
- Multi-language STT — Whisper auto-detects language (Phase U.1); Claude 4.6 replies in the detected language. English is the primary tested path
- Direct httpx everywhere due to Windows SDK issues
- Logitech MeetUp motorized pan/tilt control: **architecturally inaccessible from a web app**. Logitech exposes zoom via UVC but gates pan/tilt behind their proprietary Sync/Tune SDK on all MeetUp firmware (confirmed via Google's official MediaCapture-PTZ reference demo against MeetUp firmware 1.0.244 — zoom slider active, pan/tilt reported unsupported). The MediaCapture-PTZ frontend code was shipped and then removed after empirical confirmation; the out-of-frame welfare check (backend, camera-agnostic) remains as the effective Phase K feature on MeetUp. A native desktop app using Logi SDK is the only path for motion tracking on this hardware

## Observability
- Langfuse v2 (pinned <3.0)
- Per-turn traces: STT span, Claude generation, per-sentence TTS spans
- Standalone vision traces tagged "vision"
- Session summary trace on WebSocket disconnect
- Graceful no-op if Langfuse keys missing or package unavailable

## Data Privacy
- No audio stored anywhere
- Video frames analyzed and immediately discarded
- Conversation history in-memory only, cleared on session end
- `UserContext` (name, topics, preferences, mood signals) persists on-device across sessions at `./memory/<resident_id>.json`, keyed by a browser-generated UUID. Plaintext, gitignored, local only — no cloud sync. User wipes via "Forget me" button in the gear drawer (Phase E)
- Privacy notice displayed in UI

## Living Documents
- DESIGN-NOTES.md — source of truth for all architectural decisions
  Add a new Dxx entry whenever a significant decision is made or changed
- TROUBLESHOOTING.md — append-only bug log, newest entries at top
  Add an entry whenever a non-trivial bug is found and fixed
- CLAUDE.md — update immediately when any requirement changes
  Never let CLAUDE.md drift from actual implementation state
