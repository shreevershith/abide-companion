# Abide Companion

A real-time multimodal AI companion for elderly care. Abide listens, watches, and talks back — a voice conversation loop with always-on vision that describes the user's current activity, draws a bounding box around them in the live video, detects falls, and gently checks in when something looks wrong.

It runs on one machine as a native Python app. Double-click `start.bat` (or `start.sh`), the browser opens, enter three API keys, click **Start**, and talk. On Windows with a Logitech MeetUp, saying *"zoom in / out / reset"* moves the camera's optical zoom; MeetUp firmware does not expose mechanical pan/tilt — see [DESIGN-NOTES.md](DESIGN-NOTES.md) *The PTZ saga* for the story.

---

## Table of Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Stack](#stack)
- [Quick start](#quick-start)
- [Project layout](#project-layout)
- [How the voice loop works](#how-the-voice-loop-works)
- [Vision pipeline](#vision-pipeline)
- [Session summary and diary](#session-summary-and-diary)
- [Barge-in](#barge-in)
- [Whisper hallucination defences](#whisper-hallucination-defences)
- [Known limitations](#known-limitations)
- [Data privacy](#data-privacy)
- [Observability](#observability)
- [Configuration reference](#configuration-reference)
- [Further reading](#further-reading)

---

## What it does

- **Voice conversation** with barge-in — interrupt Abide mid-sentence and it stops within ~150 ms on MeetUp (~420 ms on a laptop).
- **Always-on vision** — 3-frame bursts every 3.6 s through GPT-4o-mini, with motion-aware prompting that distinguishes *dancing* from *waving*, *standing up* from *standing still*, *falling* from *lying down*.
- **Fall detection** — a `FALL:` prefix from the vision model raises a red alert banner and makes Abide's next reply open with a welfare check.
- **Out-of-frame welfare check** — after ~11 s of sustained absence, Abide gently asks *"I can't see you right now — are you still there?"*.
- **Proactive check-ins** — after 30 s of silence, Abide initiates conversation based on what it sees.
- **Vision-reactive responses** — the vision model itself emits a `noteworthy: bool` flag; proactive replies fire when it's true and the activity changed.
- **Personalised welcome greeting** — cached time-of-day variant, name-aware if cross-session memory has hydrated a name.
- **Cross-session memory** — per-browser `resident_id` UUID keys `./memory/<id>.json`. Name / topics / preferences / mood survive across Start→Stop. Conversation turns are deliberately not persisted. "Forget me" button wipes it.
- **On-request optical zoom** (Windows + MeetUp) — user says "zoom in / out / reset", Claude emits an inline `[[CAM:...]]` marker, server dispatches to DirectShow off-loop.
- **Session summary** — on Stop, a full-screen overlay shows duration, full transcript, activity log, and a Claude-powered analysis of what Abide got right vs wrong.
- **Live diary** — chronological event log with color-coded type badges, exportable to plain text.
- **Observability** — optional Langfuse v2 traces (per-turn + vision + session summary).

---

## Architecture

```
                     +---------------------+
                     |       Browser       |
                     |  frontend/index.html|
                     +----------+----------+
                                |
                                |  WebSocket
                                |     up:   PCM audio, JPEG frames (b64),
                                |           config / control (JSON)
                                |     down: opus audio, events + transcript
                                v
                     +----------+----------+
                     |   FastAPI server    |
                     |  (single process,   |
                     |   one WebSocket)    |
                     +----+-----------+----+
                          |           |
              +-----------+--+     +--+------------------+
              |  Voice loop  |     |  Vision pipeline    |
              |              |     |  (fire-and-forget)  |
              |  silero-vad  |     |                     |
              |  (local CPU) |     |  3-frame burst      |
              |              |     |  every 3.6 s        |
              |  Groq        |     |                     |
              |  Whisper STT |     |  GPT-4o-mini        |
              |              | <---+  activity / bbox /  |
              |  Anthropic   |     |  noteworthy flag    |
              |  Claude      |     +----------+----------+
              |  (streaming) |                |
              |              |                |  scene
              |  OpenAI TTS  |                |  description
              |  (parallel,  |                |  injected into
              |  per-        |                |  Claude's
              |  sentence)   |                |  system prompt
              +------+-------+                |
                     |                        v
                     |                 (Claude's next turn)
                     |
                     |  [[CAM:zoom_in]] marker in reply
                     v
              +----------------+
              | PTZController  |   Windows + MeetUp only
              | duvc-ctl  -->  |   optical zoom (no pan/tilt
              | DirectShow     |   on MeetUp firmware)
              +----------------+
```

All API calls (Claude, OpenAI, Groq) use persistent `httpx.AsyncClient`s with HTTP/2 multiplexing. Connection prewarm fires on WebSocket open so the user never pays a TLS handshake on their first turn.

---

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + single WebSocket endpoint |
| Frontend | Single `frontend/index.html` — no React, no build tools |
| VAD | silero-vad (local CPU, PyTorch) |
| STT | Groq Whisper (`whisper-large-v3`, `verbose_json`) |
| Conversation LLM | Anthropic Claude (`claude-sonnet-4-6`), streaming |
| TTS | OpenAI `tts-1` / `nova` voice, opus format |
| Vision | OpenAI `gpt-4o-mini`, JSON mode, multi-frame |
| Session analysis | Anthropic Claude (one-shot call at session end) |
| Telemetry | Langfuse v2 (optional, graceful no-op if missing) |
| Camera control | `duvc-ctl` (Windows DirectShow) — optical zoom on MeetUp |
| Packaging | Native Python 3.12+ in `.venv`, launched via `start.bat` / `start.sh` |

---

## Quick start

### End user

Follow [`README-SETUP.txt`](README-SETUP.txt). In short:

1. Install [Python 3.12+](https://www.python.org/downloads/) (check *"Add python.exe to PATH"* on Windows)
2. Double-click `start.bat` (Windows) or `start.sh` (Mac / Linux)
3. First run creates `.venv` and pip-installs dependencies (~3–5 min). Later runs start in seconds.
4. Browser opens at `http://localhost:8000`
5. Click the gear icon, paste your three API keys, click **Start**

### Developer / manual

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Mac / Linux

pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Always run via the venv's Python — launching uvicorn with system Python will silently disable `duvc-ctl` and zoom will do nothing. See [TROUBLESHOOTING.md #19](TROUBLESHOOTING.md) for the trap we hit.

### API keys

All three are entered in the browser UI and stored in `localStorage`. Nothing touches the server filesystem.

| Key | Provider | Used for |
|---|---|---|
| Groq | [console.groq.com](https://console.groq.com) | Whisper STT |
| Anthropic | [console.anthropic.com](https://console.anthropic.com) | Claude conversation + session analysis |
| OpenAI | [platform.openai.com](https://platform.openai.com) | TTS + vision |

Optional Langfuse keys go in a server-side `.env`; see [`.env.example`](.env.example).

---

## Project layout

```
abide-companion/
├── app/
│   ├── main.py             FastAPI, WebSocket, hot loop, /api/analyze
│   ├── session.py          Per-connection state, barge-in coordinator
│   ├── audio.py            silero-vad + Groq Whisper STT + hallucination filters
│   ├── conversation.py     Claude streaming via direct httpx (not SDK)
│   ├── tts.py              OpenAI TTS streaming via direct httpx
│   ├── vision.py           GPT-4o-mini multi-frame vision (JSON bbox output)
│   ├── ptz.py              DirectShow camera control via duvc-ctl (Windows)
│   ├── memory.py           Per-resident UserContext persistence (cross-session)
│   ├── tts_cache_store.py  Persistent phrase-frequency counter (auto-prewarm)
│   └── telemetry.py        Langfuse wrapper with graceful no-op
├── frontend/
│   └── index.html          Entire UI: conversation + diary + summary + overlay
├── start.bat               Windows launcher: .venv + pip + uvicorn + open browser
├── start.sh                Mac / Linux equivalent
├── requirements.txt
├── .env.example            Template for optional Langfuse env vars
├── README.md               This file
├── README-SETUP.txt        Plain-English setup guide for end users
├── DESIGN-NOTES.md         Development journal — what we built / tried / broke / fixed
├── TROUBLESHOOTING.md      Bug log with root causes and fixes
└── CLAUDE.md               Working notes for Claude Code sessions on this repo
```

---

## How the voice loop works

```
browser mic  (AudioWorklet, 48 kHz, 2048-sample chunks)
     |
     |   binary WebSocket frames
     v
server       downsample to 16 kHz, silero-vad (512-sample windows)
     |
     |   speech segment as WAV on speech_end
     v
Groq Whisper (STT, verbose_json, temperature=0, language=en)
     |
     |   confidence filter + hallucination blocklist
     v
user transcript
     |
     v
Anthropic Claude  (streamed, with vision + time + user context)
     |
     |   sentence-boundary detection
     v
OpenAI TTS   (one request per sentence, launched in parallel)
     |
     |   opus audio bytes, binary WebSocket frames
     v
browser      Web Audio API, sequential FIFO playback
```

**Key latency wins:**
- TTS fires on first sentence boundary, not after the full Claude response
- Parallel TTS via HTTP/2 multiplexing over one connection
- Connection prewarm on WebSocket open (HEAD to each API)
- silero-vad runs locally — no API call on the critical path
- Anthropic prompt caching (Phase O + R) — cached prefix `[system + prior turns]` kicks in from turn ~3–5

---

## Vision pipeline

Independent from the voice loop, fire-and-forget:

1. Browser captures one JPEG every 1.2 s; ring buffer holds last 3 frames.
2. Every 3rd capture, the 3-frame batch is sent to the server.
3. Server sends to GPT-4o-mini with a structured JSON prompt.
4. Response: `motion_cues` (chain-of-thought grounding), `activity` (≤10 words), `noteworthy` flag, bounding box.
5. Scene chip + canvas overlay render on the frontend.
6. Last 5 scene descriptions (with relative timestamps) get injected into Claude's system prompt.

The prompt teaches four motion scopes (WHOLE-BODY / LIMB / HAND-OBJECT / STATIC) and instructs the model to match the label's granularity to the scope of motion observed. When motion spans scopes, pick the largest visible. The `noteworthy` flag replaces an earlier hard-coded activity allowlist — the model decides whether a scene is the kind of event a friend would stop and react to.

**Fall detection.** A `FALL:` prefix on the activity text triggers a red pulsing alert banner that auto-hides after 20 s. Claude's next reply opens with a welfare question. Biased toward false positives.

---

## Session summary and diary

**Diary tab** (live during session): chronological event log with type badges — *You* (indigo) / *Abide* (teal) / *Vision* (amber) / *Alert* (rose). Live-updating. Exportable to plain text.

**Session summary** (on Stop): full-screen overlay with session duration, complete timestamped transcript, activity log, and a Claude-generated analysis of what Abide got right vs wrong (separate one-shot `POST /api/analyze` call).

---

## Barge-in

Abide can be interrupted mid-sentence. Multi-layer gate:

1. **300 ms post-TTS cooldown** — ignore VAD after each TTS chunk to kill trailing-echo blips.
2. **Sustained-speech gate** (150 ms on MeetUp / 400 ms on laptop) — single spikes don't qualify, real speech sustains.
3. **Loud-window count** (≥4 on MeetUp / ≥6 on laptop) — ≈128 ms of cumulative above-threshold audio in the same segment.
4. **Cooperative cancellation** — on barge-in, a flag checked between sentence boundaries stops the Claude stream; partial response is saved to history so Claude doesn't repeat itself.
5. **Client-side epoch counter** — drops any in-flight `decodeAudioData` callbacks from the cancelled response.

MeetUp's firmware-level Acoustic Echo Cancellation is what lets the gate be aggressive without false positives. Laptop deployments should revert to the 400 / 6 defaults.

---

## Whisper hallucination defences

Groq Whisper (whisper-large-v3) hallucinates on short / quiet / ambiguous audio — it emits phrases from its YouTube training data. Four layers of defence:

1. **RMS + length pre-filter** — reject segments <0.5 s or <0.015 RMS before they ever reach Whisper.
2. **Minimal STT prompt** — only disambiguates the assistant's name "Abide". No "hello" / "thank you" / "goodbye" (those would bias the decoder toward them — see [TROUBLESHOOTING.md #10](TROUBLESHOOTING.md)).
3. **`verbose_json` confidence filter** — drop segments where `no_speech_prob > 0.6` AND `avg_logprob < -1.0` (OpenAI's reference thresholds).
4. **Standalone blocklist** — drop transcripts that are exactly a known hallucination ("thank you", "bye", "Subtitles by Amara.org").

---

## Known limitations

- **Mechanical pan/tilt on MeetUp is not reachable.** Firmware 1.0.244 / 1.0.272 both probe as `Pan: ok=False / Tilt: ok=False` over UVC. Only optical zoom is exposed. Any on-device framing motion is Logitech RightSight digital cropping. See [DESIGN-NOTES.md](DESIGN-NOTES.md) *The PTZ saga*.
- **Barge-in is ~150 ms on MeetUp / ~420 ms on laptop** — the MeetUp number depends on its hardware AEC.
- **Vision bounding boxes are approximate** — GPT-4o-mini spatial grounding is "close" not "precise".
- **Fall detection is prototype-grade** — no emergency dispatch. Abide offers to call someone; it does not dial anyone.
- **Conversation history caps at 20 messages** — intentional, keeps token budget flat over long sessions.
- **English-only** — `language="en"` is passed to Whisper.
- **Single-session, localhost only** — no auth, no rate limiting, no TLS.
- **`p95_turn_latency_ms` is whole-turn, not TTFA** — real TTFA is sub-1 s on cache-warm paths. See DESIGN-NOTES *Observability and latency percentiles*.

---

## Data privacy

- Audio is processed in-memory only and immediately discarded.
- Video frames are sent to the vision API and not stored.
- Conversation history lives only in the server process for the WebSocket's lifetime.
- API keys live in the browser's `localStorage` and (optionally) the server's `.env` for Langfuse. Nothing else is persisted.
- Closing the tab ends the session; all derived data is gone.
- `UserContext` facts (name, topics, preferences, mood) persist to `./memory/<resident_id>.json`, gitignored, local-only. Wipe via the "Forget me" button.

---

## Observability

Langfuse v2 is optional. Set `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` in `.env` to enable. The server emits:

- One **turn trace** per user utterance with child spans for STT, Claude (with token usage + cache telemetry), and per-sentence TTS
- One **vision trace** per vision API call, tagged `vision` (and `fall` when applicable)
- One **session-summary trace** on disconnect with counters (turns, barge-ins, falls, vision calls, latency P50/P95, duration)
- One **connectivity-probe trace** on WebSocket connect

If keys are missing or Langfuse is unreachable, every telemetry call becomes a silent no-op. The voice loop never depends on telemetry.

---

## Configuration reference

### Browser (gear icon)

| Setting | Required | Notes |
|---|---|---|
| Groq API key | Yes | Whisper STT |
| Anthropic API key | Yes | Claude conversation + session analysis |
| OpenAI API key | Yes | TTS + vision |

### Server (`.env`, developer only)

| Variable | Notes |
|---|---|
| `LANGFUSE_PUBLIC_KEY` | Enables telemetry |
| `LANGFUSE_SECRET_KEY` | Enables telemetry |
| `LANGFUSE_HOST` | Defaults to `https://cloud.langfuse.com` |

### Key tunables (source-code constants)

| Constant | File | Default | Purpose |
|---|---|---|---|
| `SUSTAINED_SPEECH_MS` | `main.py` | 150 (MeetUp) / 400 (laptop) | Barge-in speech threshold |
| `BARGE_IN_MIN_LOUD_WINDOWS` | `main.py` | 4 (MeetUp) / 6 (laptop) | Barge-in loud-window count |
| `POST_TTS_COOLDOWN_MS` | `main.py` | 300 | Echo-suppression cooldown |
| `CHECK_IN_INTERVAL_S` | `main.py` | 30 | Proactive check-in silence threshold |
| `MAX_HISTORY` | `conversation.py` | 20 | Conversation window |
| `_OUT_OF_FRAME_CHECKIN_THRESHOLD` | `session.py` | 3 | Consecutive out-of-frame cycles before welfare check (~11 s) |

Full list in the source. Revert the barge-in constants to laptop defaults on deployments without hardware AEC.

---

## Further reading

- [`DESIGN-NOTES.md`](DESIGN-NOTES.md) — development journal: what we set out to build, what we tried, what broke, what we didn't ship and why
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — bug log with symptoms, root causes, and fixes
- [`README-SETUP.txt`](README-SETUP.txt) — end-user setup guide in plain English
- [`CLAUDE.md`](CLAUDE.md) — working notes for Claude Code sessions on this repo

---

## License

Proprietary. All rights reserved.
