# Abide Companion

A real-time multimodal AI companion for elderly care. Abide listens, watches,
and talks back — a voice conversation loop with always-on vision that can
describe the person's current activity, draw a bounding box around them in
the live video, detect falls, and gently check in when something looks wrong.

It runs on one machine with Docker Desktop. You double-click `start.bat`
(or `start.sh`), the browser opens, you click Start, and you can talk to it.

---

## Table of Contents

- [What It Does](#what-it-does)
- [Architecture](#architecture)
- [Stack](#stack)
- [Quick Start](#quick-start)
- [Project Layout](#project-layout)
- [How the Voice Loop Works](#how-the-voice-loop-works)
- [Vision Pipeline](#vision-pipeline)
- [Session Summary and Diary](#session-summary-and-diary)
- [Barge-In (Interruption Handling)](#barge-in-interruption-handling)
- [Whisper Hallucination Defenses](#whisper-hallucination-defenses)
- [Key Design Decisions](#key-design-decisions)
- [Known Limitations](#known-limitations)
- [Data Privacy](#data-privacy)
- [Observability (Langfuse)](#observability-langfuse)
- [Configuration Reference](#configuration-reference)
- [Further Reading](#further-reading)

---

## What It Does

- **Voice conversation** — continuous microphone capture, voice-activity
  detection (local CPU), speech-to-text, a conversational LLM, text-to-speech,
  and barge-in (interrupt Abide mid-sentence and it stops within ~420 ms).
- **Live scene understanding** — webcam feed sampled every ~3.6 seconds
  in 3-frame bursts so the model can see motion. Abide can distinguish
  sitting from standing, walking from dancing, folding clothes from
  putting on a shirt. The current activity is displayed as a glass chip
  overlaid on the video, with a bounding box drawn around the person.
- **Fall detection + welfare check-in** — if the vision model sees
  someone going down or lying on the floor, a red alert banner appears
  and Abide's next reply opens with a gentle welfare question instead
  of continuing its previous train of thought.
- **Session summary** — when the user clicks Stop, a full-screen overlay
  shows: session duration, complete timestamped transcript, activity log,
  and a Claude-generated analysis of what Abide got right vs. wrong.
- **Live diary** — a tab in the transcript panel showing all events
  (speech, replies, vision observations, alerts) in chronological order
  with timestamps, live-updating during the session.
- **Proactive check-ins** — if the user is silent for 30 seconds, Abide
  initiates conversation based on what it sees on camera. It's a companion
  in the room, not a chatbot waiting for input.
- **User memory within session** — Abide extracts and remembers the user's
  name, topics discussed, preferences, and mood signals. These are injected
  into every Claude turn so responses feel personal ("John, I noticed you're
  back in your chair — how was your walk?").
- **Low latency** — target is under 1.5 seconds from end-of-speech to
  first audio response. Achieved via persistent HTTP/2 clients, streaming
  LLM to streaming TTS on sentence boundaries, and parallel TTS
  synthesis while the LLM is still generating.
- **Observability** — optional Langfuse integration with per-turn
  traces, standalone vision traces, and a session-summary trace on
  disconnect.

---

## Architecture

```
                          +-----------+
                          |  Browser  |
                          |  (Single  |
                          |   HTML    |
                          |   file)   |
                          +-----+-----+
                                |
                         WebSocket (binary PCM up,
                         opus audio down, JSON both ways,
                         base64 JPEG frames up)
                                |
                   +------------+------------+
                   |    FastAPI Server       |
                   |    (single process)     |
                   |                         |
           +-------+-------+       +--------+--------+
           | Voice Loop    |       | Vision Pipeline  |
           |               |       | (fire-and-forget)|
           | 1. silero-vad |       |                  |
           |    (local CPU)|       | 3-frame burst    |
           | 2. Groq       |       |    every 3.6s    |
           |    Whisper STT|       |       |          |
           | 3. Claude     |       |  GPT-4o-mini     |
           |    (streaming)|       |  (JSON bbox)     |
           | 4. OpenAI TTS |       +--------+---------+
           |    (parallel, |                |
           |    per-sentence|        Scene description
           +-------+-------+        injected into Claude's
                   |                 system prompt per turn
            Audio bytes back
            to browser via WS
```

All API calls use a single persistent `httpx.AsyncClient` with HTTP/2
per module. Connection prewarm fires on WebSocket open so the user
never pays a TLS handshake on their first turn.

---

## Stack

| Layer             | Choice                                              |
|-------------------|-----------------------------------------------------|
| Backend           | FastAPI + single WebSocket endpoint                 |
| Frontend          | Single `frontend/index.html` — no React, no build tools |
| VAD               | silero-vad (local CPU inference via PyTorch)         |
| STT               | Groq Whisper API (`whisper-large-v3`, `verbose_json`) |
| Conversation LLM  | Anthropic Claude (`claude-sonnet-4-20250514`), streaming |
| TTS               | OpenAI `tts-1` / `nova` voice, opus format          |
| Vision            | OpenAI `gpt-4o-mini`, JSON-mode, multi-frame        |
| Session Analysis  | Anthropic Claude (one-shot call at session end)      |
| Telemetry         | Langfuse v2 (optional, graceful no-op if missing)    |
| Packaging         | Docker + docker-compose + `start.bat` / `start.sh`  |

---

## Quick Start

### For a non-technical evaluator

Follow `README-SETUP.txt`. In short:

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
2. Double-click `start.bat` (Windows) or `start.sh` (Mac/Linux)
3. Browser opens at `http://localhost:8000`
4. Click the gear icon, paste your 3 API keys (Groq, Anthropic, OpenAI)
5. Click **Start** — allow mic + camera access when prompted

### For a developer who wants to iterate without Docker

```bash
# Python 3.12+ recommended
python -m venv .venv
.venv\Scripts\activate              # Windows
# source .venv/bin/activate         # Mac/Linux

pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Then open `http://localhost:8000` and click the gear icon to enter
your API keys.

### API keys needed

| Key | Provider | Purpose | Get it at |
|-----|----------|---------|-----------|
| Groq | Groq | Speech-to-text (Whisper) | https://console.groq.com |
| Anthropic | Anthropic | Conversation (Claude) + session analysis | https://console.anthropic.com |
| OpenAI | OpenAI | TTS (text-to-speech) + Vision (GPT-4o-mini) | https://platform.openai.com |

All three are entered in the browser UI and stored in `localStorage`.
They never touch the server's filesystem. The only server-side keys
are the optional Langfuse ones in `.env`.

---

## Project Layout

```
abide-companion/
|-- app/
|   |-- main.py           FastAPI app, WebSocket, hot loop, /api/analyze endpoint
|   |-- audio.py          silero-vad + Groq Whisper STT + hallucination filters
|   |-- session.py        Per-connection state, barge-in coordinator, response task
|   |-- conversation.py   Claude streaming via direct httpx (not SDK)
|   |-- tts.py            OpenAI TTS streaming via direct httpx
|   |-- vision.py         GPT-4o-mini multi-frame vision with JSON bbox output
|   `-- telemetry.py      Langfuse wrapper with graceful no-op
|-- frontend/
|   `-- index.html        Entire UI in one file: HTML + CSS + JS
|                          - Conversation tab (real-time transcript)
|                          - Diary tab (chronological event log)
|                          - Session summary overlay (on Stop)
|                          - Video overlay with bounding box
|                          - Fall alert banner
|-- Dockerfile            Python 3.12-slim + CPU torch + torchaudio + silero-vad
|-- docker-compose.yml    Single service, .env mounted as env_file
|-- start.bat             Windows launcher: docker compose up + open browser
|-- start.sh              Mac / Linux equivalent
|-- .env.example          Template for Langfuse env vars (developer only)
|-- requirements.txt      Python dependencies
|-- README.md             This file
|-- README-SETUP.txt      Plain-English setup guide for the evaluator
|-- DESIGN-NOTES.md       57 architectural decisions with context and trade-offs
|-- CLAUDE.md             Developer guidance for Claude Code sessions
`-- TROUBLESHOOTING.md    10 bugs, root causes, and fixes
```

---

## How the Voice Loop Works

```
browser mic (AudioWorklet, 48 kHz, 2048-sample chunks)
    --- binary WebSocket frames --->
server: downsample to 16 kHz, silero-vad (512-sample windows)
    --- speech segment as WAV (on speech_end) --->
Groq Whisper (STT, verbose_json, temperature=0, language=en)
    --- confidence filter + hallucination blocklist --->
    --- user transcript text --->
Anthropic Claude (streamed, with vision context in system prompt)
    --- sentence-boundary detection --->
OpenAI TTS (one request per sentence, launched in parallel)
    --- opus audio bytes --->
    --- binary WebSocket frames --->
browser: decode + sequential FIFO playback via Web Audio API
```

**Key latency optimizations:**
- TTS fires on first sentence boundary, not after full Claude response
- Parallel TTS requests via HTTP/2 multiplexing over one connection
- Connection prewarm on WebSocket open (HEAD to each API)
- silero-vad runs locally — no API call on the critical path

---

## Vision Pipeline

The vision system runs independently from the voice loop as a
fire-and-forget background task:

1. Browser captures one JPEG frame every 1.2 seconds
2. Ring buffer holds last 3 frames (~3.6 seconds of motion)
3. Every 3rd capture, the full 3-frame batch is sent to the server
4. Server sends the batch to GPT-4o-mini with a structured prompt
5. Response includes: activity description (max 10 words) + bounding box
6. Result displayed on the frontend: scene chip + canvas overlay
7. Last 5 scene descriptions with relative timestamps injected into Claude's system prompt

**Activities detected:** sitting, standing, walking, waving, dancing,
reaching, bending, stretching, folding clothes, putting on a shirt,
holding a cup, reading, typing, exercising, falling, lying down

**Fall detection:** Vision prompt enforces a `FALL:` prefix when a fall
is observed. The frontend shows a red pulsing alert banner that
auto-hides after 20 seconds. Claude's next reply opens with a welfare
check-in question.

---

## Session Summary and Diary

### Diary Tab (live, during session)

A tab alongside "Conversation" in the transcript panel. Shows ALL events
in chronological order:

- **You** (indigo) — user speech transcripts
- **Abide** (teal) — assistant responses
- **Vision** (amber) — camera observations
- **Alert/Fall** (rose) — fall alerts and warnings

Each entry has a timestamp (HH:MM:SS) and a color-coded type badge.
Live-updating — entries appear as they happen.

### Session Summary (on Stop)

When the user clicks Stop, a full-screen overlay displays:

1. **Duration** — start time, end time, total length
2. **Conversation Transcript** — all user + assistant messages with timestamps
3. **Activity Log** — all vision observations + fall alerts with timestamps
4. **What Abide Got Right / Wrong** — a Claude-powered analysis that reviews
   the conversation for: correct interpretations (user confirmed or didn't
   correct), incorrect interpretations (user said "no", "that's wrong",
   etc.), and ambiguous cases

The analysis is generated by a one-shot Claude API call via
`POST /api/analyze`. The rest of the summary renders immediately;
the analysis shows a spinner while loading.

---

## Barge-In (Interruption Handling)

Abide can be interrupted mid-sentence. The system uses a multi-layer gate:

1. **POST_TTS_COOLDOWN_MS (300 ms)** — ignore VAD after each TTS chunk
   to suppress echo blips
2. **SUSTAINED_SPEECH_MS (400 ms)** — require continuous speech before
   firing, not a single spike
3. **BARGE_IN_MIN_RMS (0.015)** — require real voice-level energy,
   rejecting TTS echo (~0.005 RMS) leaking through the mic
4. **Cooperative cancellation** — on barge-in, the server sets a flag
   checked between sentence boundaries; partial response is saved to
   history so Claude doesn't repeat itself
5. **Client-side epoch counter** — drops any in-flight `decodeAudioData`
   callbacks from the cancelled response

Total barge-in latency: ~420 ms from user speech onset to Abide going
silent. The tradeoff is documented in D14 — lowering it further requires
acoustic echo cancellation (AEC) hardware or DSP, not just timing gates.

---

## Whisper Hallucination Defenses

Groq Whisper (whisper-large-v3) hallucinates on short/quiet/ambiguous
audio — it emits phrases from its YouTube training data. The system
has four layers of defense (see D41, D48, Troubleshooting #8, #10):

1. **RMS + length pre-filter** — reject segments < 0.5s or < 0.015 RMS
   before they ever reach Whisper
2. **Minimal STT prompt** — only disambiguates the rare name "Abide",
   never lists common phrases (which bias the decoder toward them)
3. **`verbose_json` confidence filter** — drop segments where
   `no_speech_prob > 0.6 AND avg_logprob < -1.0` (OpenAI's reference
   thresholds)
4. **Regex + standalone blocklist** — catch known hallucination patterns
   ("Subtitles by Amara.org") and bare phrases ("thank you", "hmm")

---

## Key Design Decisions

These are documented in full with alternatives and trade-offs in
`DESIGN-NOTES.md` (57 entries across 8+ phases). Highlights:

- **Single-file frontend, no framework** (D1) — build tools are a cold-start
  liability for a zero-config evaluator experience.
- **Direct httpx instead of SDKs** (D5) — bypasses Windows SSL flakiness;
  HTTP/2 client reuse cuts ~500 ms off first-token latency.
- **silero-vad on local CPU** (D3) — no API round-trip on the critical path.
- **Sentence-boundary streaming TTS** (D8) — first audio starts playing
  while Claude is still generating sentence 2.
- **Web Audio API, not `<audio>`** (D11) — `.stop()` is synchronous,
  which matters for barge-in.
- **Cooperative cancellation flag** (D12) — clean partial-response
  preservation on barge-in so Claude doesn't repeat itself.
- **Multi-frame vision** (D23) — 3 consecutive frames gives the model
  motion signal (walking vs standing, falling vs lying down).
- **`FALL:` prefix convention** (D24) — prompt-enforced, not a separate
  classifier; biased toward false positives over false negatives.
- **Whisper confidence filter** (D48) — `verbose_json` + `no_speech_prob`
  thresholds suppress hallucinations at the model confidence level.
- **Session summary with Claude analysis** (D50) — one-shot Claude call
  at session end reviews conversation accuracy.
- **Live diary tab** (D49) — chronological event log mixing speech,
  replies, and vision observations with color-coded type badges.
- **Proactive vision engagement** (D51) — Claude is instructed to always
  comment on what it sees, not wait to be asked. Activity changes between
  turns are noticed and remarked on naturally.
- **Proactive check-in** (D52) — 30-second silence trigger fires a
  system-initiated Claude turn based on vision context. Abide doesn't
  wait for the user to speak first.
- **UserContext persistence** (D53) — lightweight extraction call after each
  response extracts user facts (name, topics, preferences, mood) and
  injects them into every subsequent Claude turn.
- **Vision-reactive triggers** (D54) — waving, thumbs up, standing up etc.
  trigger an immediate Claude response within ~4 seconds, not waiting
  for the 30-second timer or user speech.
- **Concurrent response mutex** (D55) — `start_response()` cancels any
  in-flight task before starting a new one, preventing orphaned tasks.
- **Safe WebSocket sends** (D56) — all 11 `ws.send_json()` calls in
  `main.py` replaced with `Session._safe_send_json()` to prevent crashes
  on WebSocket close race conditions.
- **Temporal activity context** (D19) — last 5 scene descriptions with
  relative timestamps ("2 min ago: sitting", "just now: standing up")
  give Claude awareness of what the user has been doing over time.

---

## Known Limitations

Full list in `DESIGN-NOTES.md`. Highlights:

- **Barge-in latency ~420 ms** — the RMS + sustained-speech gate prevents
  TTS echo from triggering phantom interrupts. Lowering it needs proper AEC.
- **Vision bounding boxes are approximate** — GPT-4o-mini spatial grounding
  is "close" not "precise."
- **Fall detection is prototype-grade** — no emergency dispatch, no SLA,
  best-effort. Abide offers to call someone; it does not dial anyone.
- **Conversation history caps at 20 messages** — intentional forgetting
  so the token budget stays flat over long sessions.
- **No persistence** — closing the tab ends the session. This matches
  the project's data-privacy posture.
- **English-only** — `language="en"` is passed to Whisper; other languages
  were not tested.
- **Single-session, localhost only** — no auth, no rate limiting, no TLS.

---

## Data Privacy

- Audio is processed in-memory only and immediately discarded.
- Video frames are sent to the vision API and not stored anywhere.
- Conversation history lives only in the server process's memory for
  the duration of the WebSocket connection.
- API keys live in the browser's `localStorage` and the server's
  `.env` (Langfuse only). They are not written anywhere else.
- If the browser tab closes, the entire conversation and all derived
  data is gone.
- The privacy notice is displayed in the UI.

---

## Observability (Langfuse)

Langfuse is supported but optional. Set `LANGFUSE_PUBLIC_KEY` and
`LANGFUSE_SECRET_KEY` in `.env` to enable. The server emits:

- One **turn trace** per user utterance with child spans for STT, the
  Claude generation (with token usage), and one TTS span per sentence
- One **vision trace** per vision API call, tagged `vision` (and `fall`
  if a fall was detected)
- One **session-summary trace** at WebSocket disconnect with counters
  (turns, barge-ins, falls, vision calls, latency stats, duration)
- One **connectivity-probe trace** on WebSocket connect for end-to-end
  verification

If keys are missing or Langfuse is unreachable, the server logs
`Langfuse: disabled (no keys)` and every telemetry call becomes a
silent no-op. The voice loop never depends on telemetry.

---

## Configuration Reference

### Browser-side (gear icon in UI)

| Setting | Required | Notes |
|---------|----------|-------|
| Groq API Key | Yes | For Whisper STT |
| Anthropic API Key | Yes | For Claude conversation + session analysis |
| OpenAI API Key | Yes | For TTS + vision |

### Server-side (.env file, developer only)

| Variable | Required | Notes |
|----------|----------|-------|
| `LANGFUSE_PUBLIC_KEY` | No | Enables telemetry traces |
| `LANGFUSE_SECRET_KEY` | No | Enables telemetry traces |
| `LANGFUSE_HOST` | No | Defaults to `https://cloud.langfuse.com` |

### Tunables (constants in source code)

| Constant | File | Default | Purpose |
|----------|------|---------|---------|
| `POST_TTS_COOLDOWN_MS` | `main.py` | 300 | Echo suppression cooldown (ms) |
| `SUSTAINED_SPEECH_MS` | `main.py` | 400 | Barge-in speech threshold (ms) |
| `BARGE_IN_MIN_RMS` | `main.py` | 0.015 | Barge-in loudness threshold |
| `MIN_SPEECH_SAMPLES` | `audio.py` | 8000 | Min segment length (0.5s at 16kHz) |
| `MIN_SPEECH_RMS` | `audio.py` | 0.015 | Min segment loudness |
| `MAX_HISTORY` | `conversation.py` | 20 | Conversation window (messages) |
| `CHECK_IN_INTERVAL_S` | `main.py` | 30 | Proactive check-in silence threshold (s) |
| `_VISION_REACT_COOLDOWN_S` | `session.py` | 15.0 | Min seconds between vision-reactive responses |
| `CAPTURE_INTERVAL_MS` | `index.html` | 1200 | Vision frame capture interval |
| `SEND_EVERY_N` | `index.html` | 3 | Frames before sending to vision |

---

## Stopping

Click Stop in the browser to end the session and see the summary.
To shut down the server: close the browser tab, then quit Docker
Desktop, or run `docker compose down` in this folder.

---

## License

Prototype / research code. No license granted.

---

## Further Reading

- `README-SETUP.txt` — one-page setup guide for the end user
- `DESIGN-NOTES.md` — 57 architectural decisions with context and
  trade-offs across 8+ development phases
- `TROUBLESHOOTING.md` — 10 bugs, root causes, and fixes
- `CLAUDE.md` — developer instructions for Claude Code sessions
