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
- [Latency](#latency)
- [Security + robustness](#security--robustness)
- [Known limitations](#known-limitations)
- [Data privacy](#data-privacy)
- [Observability](#observability)
- [Configuration reference](#configuration-reference)
- [Further reading](#further-reading)

---

## What it does

- **Voice conversation** with barge-in — interrupt Abide mid-sentence and it stops within ~150 ms on MeetUp (~420 ms on a laptop).
- **Always-on vision** — 2-frame bursts every 2.4 s through GPT-4.1-mini, with motion-aware prompting that distinguishes *dancing* from *waving*, *standing up* from *standing still*, *falling* from *lying down*.
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
              |  (local CPU) |     |  2-frame burst      |
              |              |     |  every 2.4 s        |
              |  Groq        |     |                     |
              |  Whisper STT |     |  GPT-4.1-mini       |
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
| Vision | OpenAI `gpt-4.1-mini`, JSON mode, multi-frame |
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

1. Browser captures one JPEG every 1.2 s; ring buffer holds last 2 frames.
2. Every 2nd capture, the 2-frame batch is sent to the server.
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

Groq Whisper (whisper-large-v3) hallucinates on short / quiet / ambiguous audio — it emits phrases from its YouTube training data. Six layers of defence:

1. **RMS + length pre-filter** — reject segments <0.5 s or <0.015 RMS before they ever reach Whisper.
2. **Minimal STT prompt** — only disambiguates the assistant's name "Abide" (and the user's name once memory hydrates it). No "hello" / "thank you" / "goodbye" (those would bias the decoder toward them — see [TROUBLESHOOTING.md #10](TROUBLESHOOTING.md)).
3. **`verbose_json` confidence filter** — drop segments where `no_speech_prob > 0.6` AND `avg_logprob < -1.0` (OpenAI's reference thresholds).
4. **Standalone blocklist** — drop transcripts that are exactly a known hallucination. The list is multilingual after dropping `language="en"` lock (Phase U.1): English + Spanish + French + German + Italian + Portuguese + Hindi + romanised Japanese / Mandarin / Russian.
5. **Mixed-script rejection** — drop any transcript that mixes Latin with CJK / Cyrillic / Arabic in one utterance, e.g. `'Que é我跟你講 not...'`. Real multilingual speakers switch languages turn-over-turn, not mid-utterance; cross-script contamination is Whisper fabricating from its training prior.
6. **Single-token alphanumeric floor** — drop single-word transcripts with fewer than 2 alpha characters (catches `'.'`, `'x.'`, bare-punctuation fabrications) while preserving real one-word replies like `'yes'`, `'no'`, `'why'`.

---

## Latency

The brief asks for latency "low enough to feel interactive." Honest numbers from the latest 55-turn live session:

| Stage | P50 | P95 |
|---|---|---|
| STT (Groq Whisper) | 312 ms | 547 ms |
| Claude TTFT (first-token latency, cached prefix) | 1.5 s | 3.6 s |
| OpenAI TTS first byte | 1.4 s | 2.4 s |
| **TTFA** (speech_end → first audio byte out) | **4.5 s** | **6.1 s** |

**What this is bounded by.** Two cloud APIs — Claude Sonnet and OpenAI TTS — dominate the critical path. Their first-byte times are not user-tunable. Local STT (faster-whisper) and local TTS (Piper / Kokoro) would shave ~1.5–2 s off TTFA, but both introduce install-footprint cost that would violate the brief's "novice-runnable in 5 minutes, no multi-step setup" requirement. We chose predictable cloud-API latency over a latency win that compromises cold-start.

**What we *did* optimise:**

- **Prompt caching (Claude)** — `system` block + prior-turn assistant message carry `cache_control: ephemeral`. Cache activates from turn ~15+ on Sonnet 4.6's 2048-token threshold and saves ~400–800 ms per turn thereafter.
- **Parallel YAMNet + STT** — audio-event classification runs as a background `asyncio.Task` started before `await transcribe(...)`, awaited just before `start_response`. Removes ~200–280 ms of sequential drag.
- **YAMNet pre-load on connect** — the tflite interpreter loads in the connect-to-first-utterance window rather than inside the first user turn. Eliminates the 2.5 s turn-1 tax the `[TIMING] speech_end → _run_response started` anchor was exposing.
- **Persistent HTTP/2 clients + connection prewarm** — every API has a module-level `httpx.AsyncClient(http2=True)` and a connect-time HEAD/probe so the user never pays TLS handshake on turn 1.
- **Parallel TTS pipeline** — first TTS call fires at the first sentence boundary, not after the full Claude response. Sentence N+1 synthesises while sentence N plays.
- **Sentence-boundary streaming** — Claude streams; the server detects sentence endings and kicks TTS immediately.

**Non-latency wins in the same sweep:** four API timeouts with graceful fallback (Claude 15 s first-token deadline, TTS 10 s first-byte, STT 8 s, vision 8 s), all with user-safe error messages on trip. A `[STALL]` warning log captures any Claude turn that completes with zero output tokens and > 5 s of wall time — Anthropic-side stalls are observable now, not silent.

Full per-stage histograms are in the Langfuse session summary trace. `scripts/smoke_ttfa.py` is a standalone CLI that plays a canned WAV at the WebSocket and asserts TTFA < 1.5 s (currently fails — by design, to expose drift).

---

## Security + robustness

Abide runs on the user's own machine. It's single-tenant, localhost-only, not a service. Under that threat model, we've explicitly audited and hardened:

- **Loopback bind** — `start.bat` / `start.sh` bind uvicorn to `127.0.0.1`, not `0.0.0.0`. Neither the WebSocket endpoint nor `/api/analyze` is reachable from the LAN.
- **API key handling** — the three user-supplied keys (Groq / Anthropic / OpenAI) live only in browser `localStorage` and transit the WebSocket once per session. The server never writes them to disk or logs them in plaintext; the `Config received` line masks to `"set"/"missing"` only. Langfuse keys (developer-only, optional) sit in `.env` (gitignored).
- **Prompt-injection defences** — vision-model output, user-provided name, topics, and mood signals, and client-side `fall_alert` text all go through a defence-in-depth pipeline: `<camera_observations>` / `<audio_events>` / `<turn_context>` delimited blocks in Claude's prompt, with `&lt;` / `&gt;` escape on every user-controlled string before concatenation. A name-injection blocklist drops `"abide" / "assistant" / "ai" / "user" / "companion" / "robot"` before they reach the system prompt, and the fact-extraction call is instructed never to extract "Abide" as the user's name. The `[[CAM:...]]` camera-action marker is only ever set from Claude's own output via a strict regex + action allowlist; there is no input path that can inject one from user data.
- **Input validation on every WebSocket message type** — `frames` (JPEG b64 char class + size cap + 8-frame batch cap), `face_bbox` (exactly 4 floats in [0, 1], rejects bool-masquerading-as-int), `fall_alert` (string, truncated to 200 chars, then HTML-escaped), `config` (sample rate + timezone offset range-checked), audio PCM (multiple-of-4 byte check). A malformed message is dropped silently; nothing can crash the handler.
- **Path-traversal closed on `resident_id`** — the browser-generated identifier keying `./memory/<id>.json` is regex-validated against `^[a-f0-9\-]{10,64}$` before use, then `_safe_path` resolves and `relative_to`-checks the final path stays inside `./memory/`. Symlink-escape scenarios are closed.
- **Bounded growth everywhere** — conversation history, latency sample lists, TTS cache, vision buffer, phrase-frequency store, `UserContext` fields, `face_bbox` dispatch rate are all capped. No unbounded buffer that a long session or a buggy client could grow indefinitely.
- **Graceful degradation paths** — every external API has an explicit timeout with a user-safe error message on trip. Optional subsystems (`duvc-ctl`, `ai-edge-litert`, Langfuse) silent-no-op on import failure instead of crashing the process.
- **Background-task exception surfacing** — every `asyncio.create_task` + executor future in the codebase has an `add_done_callback` that logs unhandled exceptions at WARNING. No silent GC-time failures.

---

## Known limitations

- **Pan/tilt on MeetUp is conditional and stability-TBD.** Firmware 1.0.244 consistently probes as `Pan: ok=False / Tilt: ok=False`. Firmware 1.0.272 has been observed returning both `ok=True` (with `pan=[-25,25] / tilt=[-15,15]`) AND `ok=False` across different sessions — we don't yet have a clean explanation for the difference. Per-session capability detection (`PTZController.axes_available`) is now baked into Claude's system prompt so the assistant only promises what the current hardware actually supports. Zoom (`[100, 500]`) works on every MeetUp we've tested. See [DESIGN-NOTES.md](DESIGN-NOTES.md) *The PTZ saga* + *Phase S*.
- **Barge-in is ~150 ms on MeetUp / ~420 ms on laptop** — the MeetUp number depends on its hardware AEC.
- **Vision bounding boxes are approximate** — GPT-4o-mini spatial grounding is "close" not "precise".
- **Fall detection is prototype-grade** — no emergency dispatch. Abide offers to call someone; it does not dial anyone.
- **Conversation history caps at 60 messages** — about 30 turns. Bumped from 20 after a live session showed the sliding-window truncation was invalidating prompt-cache reads on every turn; 60 keeps the cached prefix stable across a realistic 20–30 min session.
- **Language handling** — Whisper now auto-detects (Phase U.1); Claude 4.6 is multilingual so replies come back in the detected language. English is the tested default; other languages work per Whisper's coverage.
- **Single-session, localhost only** — no auth, no rate limiting, no TLS.
- **`p95_turn_latency_ms` is whole-turn, not TTFA** — kept for backwards compat. The brief's `<1.5 s to first audio` SLA is tracked separately as `ttfa_p50_ms` / `ttfa_p95_ms` in the session summary, alongside per-stage P50/P95 for STT / Claude TTFT / TTS first-byte (D85 flagged the gap, D86 resolved it). `scripts/smoke_ttfa.py` asserts TTFA < 1.5 s against a canned WAV.

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
| `MAX_HISTORY` | `conversation.py` | 60 | Conversation window (≈30 turns; stable prompt-cache prefix) |
| `_OUT_OF_FRAME_CHECKIN_THRESHOLD` | `session.py` | 3 | Consecutive out-of-frame cycles before welfare check (~11 s) |

Full list in the source. Revert the barge-in constants to laptop defaults on deployments without hardware AEC.

---

## Further reading

- [`DESIGN-NOTES.md`](DESIGN-NOTES.md) — development journal: what we set out to build, what we tried, what broke, what we didn't ship and why
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) — bug log with symptoms, root causes, and fixes
- [`README-SETUP.txt`](README-SETUP.txt) — end-user setup guide in plain English
- [`CLAUDE.md`](CLAUDE.md) — working notes for Claude Code sessions on this repo
- [`scripts/smoke_ttfa.py`](scripts/smoke_ttfa.py) — CLI smoke test: plays a WAV at the WebSocket, asserts TTFA < 1.5 s

---

## License

Proprietary. All rights reserved.
