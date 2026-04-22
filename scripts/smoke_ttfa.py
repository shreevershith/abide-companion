"""TTFA smoke test for Abide Companion.

Plays a pre-recorded WAV at the server's WebSocket endpoint and measures
the time between the server acknowledging end-of-speech (status =
"processing") and the first audio byte arriving back on the wire. This
is the brief's <1.5 s "time to first audio after user finishes speaking"
SLA; see D85 for why this differs from — and is smaller than — the
existing `p95_turn_latency_ms` metric.

Exits 0 on pass, non-zero on fail.

Usage (three shells):

    # 1. Start the server:
    .venv\\Scripts\\python -m uvicorn app.main:app

    # 2. Export API keys in the shell you'll run the test from:
    set GROQ_API_KEY=...
    set ANTHROPIC_API_KEY=...
    set OPENAI_API_KEY=...

    # 3. Run with a short WAV containing real speech (record yourself
    #    saying "Hello Abide, how are you?" — 2 s is enough):
    .venv\\Scripts\\python scripts\\smoke_ttfa.py path\\to\\test.wav

Expected WAV format: 16-bit PCM, mono or stereo (left channel used), any
sample rate from 8000-192000 Hz (the server re-samples).

What the numbers mean:
  * "TTFA (processing -> first audio)" — server-side pipeline latency,
    excludes the VAD silence-tail window. This is the cleanest measure
    of "how fast does our pipeline turn STT-done into audio-out?".
  * "TTFA (last-audio-sent -> first audio)" — end-to-end from the test's
    own perspective; includes the VAD silence-detection window (~500 ms).
    Closer to what a real user perceives but noisier.

The pass/fail assertion is against the pipeline TTFA (strictest number).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
import wave

try:
    import numpy as np
    import websockets
except ImportError as e:
    print(f"ERROR: missing dependency ({e}). Activate the repo's .venv first:")
    print("    .venv\\Scripts\\activate")
    sys.exit(2)


WS_URL = os.environ.get("ABIDE_WS_URL", "ws://localhost:8000/ws")
TARGET_TTFA_MS = 1500  # brief's SLA
CHUNK_SAMPLES = 2048   # match the browser's AudioWorklet chunk size


def _load_wav(path: str) -> tuple[np.ndarray, int]:
    """Return (float32 mono samples in [-1, 1], sample_rate_hz)."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM WAV, got {sample_width * 8}-bit")

    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    if n_channels > 1:
        pcm_i16 = pcm_i16.reshape(-1, n_channels)[:, 0]  # left channel

    pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
    return pcm_f32, sr


async def run(wav_path: str) -> int:
    groq = os.environ.get("GROQ_API_KEY")
    anthropic = os.environ.get("ANTHROPIC_API_KEY")
    openai = os.environ.get("OPENAI_API_KEY")
    missing = [
        name for name, val in [
            ("GROQ_API_KEY", groq),
            ("ANTHROPIC_API_KEY", anthropic),
            ("OPENAI_API_KEY", openai),
        ] if not val
    ]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}")
        return 2

    if not os.path.exists(wav_path):
        print(f"ERROR: WAV not found: {wav_path}")
        return 2

    pcm, sr = _load_wav(wav_path)
    duration_s = len(pcm) / sr
    print(f"Loaded {wav_path}: {duration_s:.2f}s @ {sr} Hz, {len(pcm)} samples")

    try:
        async with websockets.connect(WS_URL, max_size=10_000_000) as ws:
            # Send config (field names must match main.py's config handler).
            # resident_id uses the fresh-id format the server's regex accepts.
            config = {
                "type": "config",
                "sample_rate": sr,
                "groq_api_key": groq,
                "anthropic_api_key": anthropic,
                "openai_api_key": openai,
                "timezone_offset_minutes": 0,
                "resident_id": f"smoketest-{uuid.uuid4().hex[:16]}",
            }
            await ws.send(json.dumps(config))
            print(f"Sent config ({WS_URL})")

            # Stream PCM as binary WS frames, real-time paced.
            t_stream_start = time.monotonic()
            for i in range(0, len(pcm), CHUNK_SAMPLES):
                chunk = pcm[i : i + CHUNK_SAMPLES]
                if len(chunk) < CHUNK_SAMPLES:
                    chunk = np.pad(chunk, (0, CHUNK_SAMPLES - len(chunk)))
                await ws.send(chunk.tobytes())
                # Pace to realtime so VAD sees a plausible signal.
                target_t = t_stream_start + (i + CHUNK_SAMPLES) / sr
                sleep_s = target_t - time.monotonic()
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
            # Append ~800 ms of silence so the VAD speech_end fires without
            # waiting for the next chunk boundary.
            silence = np.zeros(int(sr * 0.8), dtype=np.float32)
            for i in range(0, len(silence), CHUNK_SAMPLES):
                chunk = silence[i : i + CHUNK_SAMPLES]
                if len(chunk) < CHUNK_SAMPLES:
                    chunk = np.pad(chunk, (0, CHUNK_SAMPLES - len(chunk)))
                await ws.send(chunk.tobytes())
                await asyncio.sleep(CHUNK_SAMPLES / sr)
            t_audio_done = time.monotonic()
            print(
                f"Sent {duration_s:.2f}s speech + 0.8s silence in "
                f"{t_audio_done - t_stream_start:.2f}s realtime"
            )

            # Wait for status=processing, then first binary audio frame.
            t_processing: float | None = None
            t_first_audio: float | None = None
            t_transcript: float | None = None
            transcript_text: str | None = None
            try:
                while t_first_audio is None:
                    msg = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    if isinstance(msg, bytes):
                        t_first_audio = time.monotonic()
                    else:
                        try:
                            data = json.loads(msg)
                        except Exception:
                            continue
                        mtype = data.get("type")
                        if mtype == "status" and data.get("state") == "processing":
                            if t_processing is None:
                                t_processing = time.monotonic()
                                print(
                                    f"  status=processing at +"
                                    f"{(t_processing - t_audio_done) * 1000:.0f}ms"
                                )
                        elif mtype == "transcript" and t_transcript is None:
                            t_transcript = time.monotonic()
                            transcript_text = data.get("text", "")
                            print(
                                f"  transcript=%r at +%.0fms"
                                % (
                                    transcript_text[:80],
                                    (t_transcript - t_audio_done) * 1000,
                                )
                            )
            except asyncio.TimeoutError:
                print("TIMEOUT (15 s) waiting for first audio response")
                return 1

    except (websockets.exceptions.WebSocketException, OSError) as e:
        print(f"WebSocket error: {type(e).__name__}: {e}")
        print(f"Is the server running at {WS_URL}?")
        return 2

    # Results
    if t_processing is None:
        print(
            "FAIL: server never emitted status=processing. VAD probably "
            "didn't fire — record a WAV with clear speech and try again."
        )
        return 1

    ttfa_pipeline_ms = (t_first_audio - t_processing) * 1000
    ttfa_observed_ms = (t_first_audio - t_audio_done) * 1000

    print()
    print(f"TTFA (processing -> first audio):      {ttfa_pipeline_ms:7.0f} ms")
    print(f"TTFA (last-audio-sent -> first audio): {ttfa_observed_ms:7.0f} ms")
    print(f"Target:                                {TARGET_TTFA_MS:7d} ms")

    if ttfa_pipeline_ms > TARGET_TTFA_MS:
        print(
            f"\nFAIL: pipeline TTFA {ttfa_pipeline_ms:.0f} ms exceeds "
            f"{TARGET_TTFA_MS} ms target."
        )
        return 1

    print("\nPASS")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nUsage: python scripts/smoke_ttfa.py <wav_path>")
        return 2
    return asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    sys.exit(main())
