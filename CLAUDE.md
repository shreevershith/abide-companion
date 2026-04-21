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
- Vision: GPT-4o (frame sampling every 3.6s, 3 frames per call)
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
- Multi-frame: 3 JPEGs per call, 1.2s apart, sent every 3.6s
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
24. ⚠ PTZ via `duvc-ctl` — native-Python DirectShow access to camera controls. Empirically, MeetUp firmware 1.0.272 exposes only `Zoom` ([100, 500]); `Pan` and `Tilt` probe as `ok=False` on all MeetUp firmwares we've tested. The "subject-follow" ambition of Phase N.2 did not ship on this hardware — see D82 2026-04-20 follow-up. The working deliverable on MeetUp is on-request zoom (Phase R, item 28 below). `nudge_to_bbox` pan/tilt code path is preserved for cameras that do expose those axes (Rally Bar etc.) but is untested here. (Phase N.2, D82 + correction)
25. ✅ Anthropic prompt caching — `system` field is now an array of content blocks with `cache_control: ephemeral` on the static SYSTEM_PROMPT. Cache kicks in once the prefix crosses Anthropic's 1024-token threshold (usually turn 3-5). Verifiable via `cache_read_tokens` / `cache_creation_tokens` in the `[TIMING] Claude response complete` log line. (Phase O)
26. ✅ Claude Sonnet 4.6 — upgraded from `claude-sonnet-4-20250514` to `claude-sonnet-4-6`. One-line model-ID change for ~10-20% TTFT improvement. (Phase P, D83)
27. ✅ Latency percentiles in session summary — P50 and P95 (with max-fallback when <20 samples) now logged and pushed to Langfuse alongside avg/min/max. More evaluator-useful than avg alone. (Phase Q)
28. ✅ On-request optical zoom — user says "zoom in / out / reset"; Claude emits an inline `[[CAM:zoom_in]]` marker at the head of its reply; the server strips it from the transcript and dispatches `PTZController.zoom()` off-loop so the lens motion overlaps with the verbal ack. `PTZController` init relaxed to accept any single PTZ axis and logs per-axis probe results at INFO for diagnostics. (Phase R, D84)

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
- Conversation history caps at 20 messages
- Cross-session persistence is limited to `UserContext` facts (Phase E). Conversation turn history is deliberately ephemeral per-session; Claude starts with a clean slate each reconnect. Calendar/schedule integration (per Abide Robotics demo) is out of scope.
- English-only (language="en" passed to Whisper)
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
