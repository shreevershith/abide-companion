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
Primary target: non-technical end users on Windows (with Docker Desktop).
Must work without developer involvement during first-run and daily use.

## Stack (non-negotiable)
- Backend: FastAPI (Python)
- Frontend: Single HTML/JS file — no React, no build tools, no npm
- STT: Groq Whisper API
- Vision: GPT-4o (frame sampling every 3.6s, 3 frames per call)
- Conversation: Claude API (claude-sonnet-4-20250514)
- TTS: OpenAI TTS (tts-1, nova voice, opus format)
- VAD: silero-vad (runs locally — deliberate latency optimization, no API call)
- Packaging: Docker + docker-compose + start.bat

## First-Run Experience (CRITICAL)
End users install and run with zero technical knowledge.
Acceptable steps:
1. Install Docker Desktop (one link, one installer)
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
- Barge-in: fires at ~420ms (gated on 400ms sustained speech + ≥6 loud VAD windows, ≈190ms cumulative above-threshold audio — matches post-hoc segment filter)
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
7. ✅ Docker packaging
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

## Never Do
- Use React, Vue, or any frontend framework
- Use <audio> element for TTS playback
- Wait for full Claude response before starting TTS
- Require terminal commands from the end user
- Add unnecessary dependencies outside Docker
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
├── docker-compose.yml
├── Dockerfile
├── start.bat
├── start.sh
├── requirements.txt
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
- No persistent session storage across sessions (in-memory only)
- English-only (language="en" passed to Whisper)
- Direct httpx everywhere due to Windows SDK issues
- Logitech MeetUp pan/tilt integration not built (hardware-specific, out of scope)

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
- Privacy notice displayed in UI

## Living Documents
- DESIGN-NOTES.md — source of truth for all architectural decisions
  Add a new Dxx entry whenever a significant decision is made or changed
- TROUBLESHOOTING.md — append-only bug log, newest entries at top
  Add an entry whenever a non-trivial bug is found and fixed
- CLAUDE.md — update immediately when any requirement changes
  Never let CLAUDE.md drift from actual implementation state
