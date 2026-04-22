"""VAD (silero-vad) + Groq Whisper STT pipeline."""

import asyncio
import io
import logging
import re
import time
import wave
import httpx
import numpy as np
import torch
from silero_vad import load_silero_vad, VADIterator
from groq import AsyncGroq

log = logging.getLogger("abide.audio")

# Hard upper bound on how long we'll wait for Groq Whisper to return a
# transcription. A transient network hiccup or Groq-side slowdown would
# otherwise freeze the main WebSocket loop (STT is called inline, not
# as a background task). 8s is generous for the audio sizes we send.
STT_TIMEOUT_S = 8.0

# Load model once at import time (cached by silero after first download)
_vad_model = load_silero_vad()

SAMPLE_RATE = 16000
VAD_CHUNK = 512  # samples per VAD window at 16kHz (32ms)

# Audio quality filter — rejects segments unlikely to contain real speech.
# Prevents Whisper hallucinations ("Thank you.", random foreign phrases) on
# short/quiet audio from breaths, clicks, chair creaks, etc.
MIN_SPEECH_SAMPLES = 8000    # 0.5 seconds at 16kHz
MIN_SPEECH_RMS = 0.015       # float32 PCM RMS threshold (background noise ~0.005, speech ~0.05+)


# Post-STT hallucination blocklist.
#
# Whisper (including whisper-large-v3 on Groq) is known to emit certain
# boilerplate phrases when the input audio is short, fragmented, or
# ambiguous. These phrases are memorized from the YouTube-heavy training
# set and are almost never genuine user speech. Common offenders:
#
#   - "Subtitles by the Amara.org community"      ← Amara subtitling credit
#   - "Thanks for watching"                        ← YouTuber outro
#   - "Please subscribe" / "Like and subscribe"   ← YouTuber outro
#   - "See you in the next video"                  ← YouTuber outro
#   - Bare musical note symbols (♪, ♫) on silent/near-silent audio
#
# We match these case-insensitively and drop the transcript entirely —
# downstream code already handles an empty transcript gracefully as
# "Empty transcript, skipping".
#
# This is deliberately a small, conservative list: only well-documented
# hallucination patterns, nothing that could plausibly be a real user
# utterance. If a user legitimately says "thanks for watching" to Abide
# it will be dropped, but that failure mode is far less bad than Abide
# apologizing to the user for subtitles they never mentioned.
_HALLUCINATION_PATTERNS = [
    r"\bamara\.?\s*org\b",
    r"\bsubtitle[sd]?\s+by\b",
    r"\bsubtitling\s+by\b",
    r"\bthanks?\s+for\s+watching\b",
    r"\bthank\s+you\s+for\s+watching\b",
    r"\bplease\s+subscribe\b",
    r"\blike\s+and\s+subscribe\b",
    r"\bdon'?t\s+forget\s+to\s+(like\s+(and\s+)?)?subscribe\b",
    r"\bsubscribe\s+to\s+(my|the)\s+channel\b",
    r"\bsee\s+you\s+(in\s+the\s+)?next\s+(video|time|episode)\b",
    # Bare musical symbols / decorative characters only
    r"^[\s\.,\-\u266a\u266b\u266c\u2669\u266d\u266e\u266f]+$",
]
_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE)

# Short-standalone hallucinations: phrases that are real words but, when
# emitted by Whisper as the *entire* transcript on near-silent audio, are
# almost always hallucinations. "Thank you." is the single most common —
# memorized from video outros. We only drop these when they are the whole
# utterance; "thank you for the reminder" and in-sentence uses pass through.
#
# Phase U.1 dropped `language="en"` so Whisper now auto-detects. The
# multilingual expansion below is the Phase U follow-up — Whisper's
# memorized YouTube outros exist in every language it was trained on,
# and a silent segment misdetected as Spanish / French / German / etc.
# would previously slip through untouched. Posterior-probability filter
# (`no_speech_prob` + `avg_logprob` at transcribe() below) catches most
# of them language-agnostically, but this lexical set is the belt-and-
# suspenders layer for the cases where confidence heuristics aren't
# decisive enough on their own.
_STANDALONE_HALLUCINATIONS = {
    # English — the original set, most-observed in live testing.
    "thank you", "thanks", "thank you.", "thanks.", "thank you!", "thanks!",
    "thank you so much", "thank you very much",
    "bye", "bye.", "goodbye", "goodbye.",
    "you", "you.", ".",
    "uh", "um", "hmm", "mm-hmm", "mmhmm",
    # Spanish
    "gracias", "gracias.", "muchas gracias", "adiós", "adios",
    "hola", "hola.", "sí", "si", "no.", "vale",
    # French
    "merci", "merci.", "merci beaucoup",
    "bonjour", "bonjour.", "au revoir", "salut",
    "oui", "non", "d'accord",
    # German
    "danke", "danke.", "danke schön", "danke schon",
    "tschüss", "tschuss", "hallo", "hallo.",
    "ja", "nein",
    # Italian
    "grazie", "grazie.", "grazie mille",
    "ciao", "ciao.", "prego",
    # Portuguese
    "obrigado", "obrigada", "obrigado.", "obrigada.",
    "olá", "ola", "adeus", "tchau",
    # Hindi / romanized
    "dhanyavaad", "namaste", "namaste.",
    # Japanese (romanized) / common memorized outro
    "arigato", "arigatou", "arigato gozaimasu",
    # Russian / Mandarin (romanized) — short hello/thanks commonly
    # hallucinated by Whisper on quiet multilingual training data
    "spasibo", "xie xie", "xiexie",
}


def _is_mixed_script(text: str) -> bool:
    """Return True if `text` mixes Latin + CJK/Cyrillic/Arabic scripts.

    Live sessions have repeatedly surfaced transcripts like
    ``'Que é我跟你講 not...'`` — Portuguese + Chinese + English glued
    together. A genuine multilingual user switches languages over
    TURNS, not mid-transcript; cross-script contamination inside a
    single utterance is Whisper fabricating tokens from its training
    prior. Flagging this is safe — the worst case is we drop a real
    utterance that happens to mix Chinese characters into English
    prose, which is very rare for spoken input.
    """
    has_latin = False
    has_cjk_or_other = False
    for ch in text:
        o = ord(ch)
        # ASCII Latin letters
        if 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A:
            has_latin = True
        # CJK Unified Ideographs, Hiragana, Katakana, Hangul, Cyrillic, Arabic
        elif (
            0x3040 <= o <= 0x30FF
            or 0x3400 <= o <= 0x4DBF
            or 0x4E00 <= o <= 0x9FFF
            or 0xAC00 <= o <= 0xD7AF
            or 0x0400 <= o <= 0x04FF
            or 0x0600 <= o <= 0x06FF
        ):
            has_cjk_or_other = True
        if has_latin and has_cjk_or_other:
            return True
    return False


# Minimum alphanumeric-character count for a transcript we forward to
# Claude. Whisper on very short audio (<0.7 s) or loud-but-silent
# segments routinely emits a single 2–4 letter "word" that is not what
# the user said — observed live: "Lift." (nothing said, lang=German),
# "Huh.", "Abide." (on the first packet of an unrelated segment). Real
# user single-word utterances that DO matter ("yes", "no", "hi", "hey",
# "why", "wait") are all ≥2 alpha chars so we use 2 as the floor after
# stripping punctuation. Combined with the VAD MIN_SPEECH_SAMPLES and
# MIN_SPEECH_RMS filters this closes the "<3 alpha-chars slip-through"
# hole without rejecting short real replies.
_MIN_TRANSCRIPT_ALPHA_CHARS = 2


def _is_hallucination(text: str) -> bool:
    """Return True if `text` matches a known Whisper hallucination pattern."""
    if not text:
        return False
    if _HALLUCINATION_RE.search(text):
        return True
    # Standalone match: strip trailing punctuation/whitespace and compare.
    if text.strip().lower() in _STANDALONE_HALLUCINATIONS:
        return True
    # Mixed-script hallucination (Latin + CJK/etc. in one transcript).
    if _is_mixed_script(text):
        return True
    # Very-short single-token transcripts on otherwise-passed audio —
    # the only real path to this point is if the VAD-RMS filter passed
    # something short. Count alphanumeric chars only (punctuation /
    # whitespace don't count). Applies only to single-token output so
    # "yes." and "no!" are preserved but "Q.", "A.", "x." are dropped.
    stripped = text.strip()
    if stripped and " " not in stripped:
        alpha_count = sum(1 for c in stripped if c.isalnum())
        if alpha_count < _MIN_TRANSCRIPT_ALPHA_CHARS:
            return True
    return False


class AudioProcessor:
    """Streaming VAD: feed PCM chunks, get back WAV bytes when speech ends."""

    def __init__(self):
        self.vad = VADIterator(
            _vad_model,
            sampling_rate=SAMPLE_RATE,
            threshold=0.5,
            min_silence_duration_ms=500,
        )
        self._collecting = False
        self._speech_buf: list[np.ndarray] = []
        self._remainder = np.array([], dtype=np.float32)
        self.last_speech_end_ts: float | None = None  # monotonic timestamp of last speech_end
        # Phase S.3 — raw 16 kHz float32 PCM of the most recently captured
        # speech segment, kept in parallel with the WAV bytes that go to
        # Whisper. Audio-event classification (`app/audio_events.py`) wants
        # the raw waveform, not a re-decoded WAV, and runs off this buffer.
        # Cleared after each read in main.py's speech-captured branch so
        # a stale segment doesn't get re-classified on an unrelated turn.
        self.last_speech_pcm: np.ndarray | None = None
        # Running max RMS over the current in-progress speech segment.
        # Kept for diagnostic logging; the barge-in gate uses the
        # loud-window counter below instead (see note).
        self._current_max_rms: float = 0.0
        # Count of VAD windows in the current segment whose RMS clears
        # MIN_SPEECH_RMS. Used as the barge-in gate so a single loud
        # spike (keypress, cough) cannot trigger a false interrupt —
        # matches the semantics of the post-hoc aggregate-RMS filter.
        # See D25b; previously the gate used peak-RMS, which let
        # momentary spikes through even when the full segment averaged
        # below the post-hoc threshold and got discarded anyway.
        self._loud_window_count: int = 0

    @property
    def is_speech(self) -> bool:
        return self._collecting

    @property
    def current_max_rms(self) -> float:
        """Max RMS observed in the current speech-in-progress segment.

        Zero when not collecting. Retained for diagnostic logging.
        """
        return self._current_max_rms

    @property
    def current_loud_window_count(self) -> int:
        """Number of windows in the current segment whose RMS cleared
        MIN_SPEECH_RMS. Zero when not collecting. Used by the barge-in
        gate in main.py to require SUSTAINED above-threshold audio.
        """
        return self._loud_window_count

    def feed(self, pcm_float32: np.ndarray, source_rate: int) -> bytes | None:
        """Feed raw PCM. Returns WAV bytes when a speech segment ends, else None."""
        # Downsample to 16 kHz via linear interpolation
        if source_rate != SAMPLE_RATE:
            n_out = int(len(pcm_float32) * SAMPLE_RATE / source_rate)
            if n_out == 0:
                return None
            pcm_16k = np.interp(
                np.linspace(0, len(pcm_float32) - 1, n_out),
                np.arange(len(pcm_float32)),
                pcm_float32,
            ).astype(np.float32)
        else:
            pcm_16k = pcm_float32

        # Prepend leftover samples from previous call
        if len(self._remainder) > 0:
            pcm_16k = np.concatenate([self._remainder, pcm_16k])

        result = None
        pos = 0

        while pos + VAD_CHUNK <= len(pcm_16k):
            window = pcm_16k[pos : pos + VAD_CHUNK]
            pos += VAD_CHUNK

            event = self.vad(torch.from_numpy(window))

            if event is not None:
                if "start" in event:
                    self._collecting = True
                    self._speech_buf = [window]
                    # Seed the running stats with this first window.
                    first_rms = _window_rms(window)
                    self._current_max_rms = first_rms
                    self._loud_window_count = 1 if first_rms >= MIN_SPEECH_RMS else 0
                elif "end" in event:
                    if self._collecting:
                        self._speech_buf.append(window)
                        audio = np.concatenate(self._speech_buf)
                        self._speech_buf = []
                        self._collecting = False
                        self._current_max_rms = 0.0
                        self._loud_window_count = 0
                        self.last_speech_end_ts = time.monotonic()
                        self.vad.reset_states()

                        # Quality filter: reject segments too short or too quiet
                        # to contain real speech. Prevents Whisper hallucinations.
                        n_samples = len(audio)
                        rms = float(np.sqrt(np.mean(audio ** 2)))
                        if n_samples < MIN_SPEECH_SAMPLES:
                            log.info(
                                "[FILTER] Rejected short segment: %d samples (%.2fs), RMS=%.4f",
                                n_samples, n_samples / SAMPLE_RATE, rms,
                            )
                        elif rms < MIN_SPEECH_RMS:
                            log.info(
                                "[FILTER] Rejected quiet segment: %d samples (%.2fs), RMS=%.4f",
                                n_samples, n_samples / SAMPLE_RATE, rms,
                            )
                        else:
                            log.info(
                                "[TIMING] speech_end detected (%d samples, %.2fs, RMS=%.4f)",
                                n_samples, n_samples / SAMPLE_RATE, rms,
                            )
                            result = _pcm_to_wav(audio)
                            # Phase S.3 — stash the raw PCM so the audio-event
                            # classifier can process it in parallel with STT.
                            self.last_speech_pcm = audio
            elif self._collecting:
                self._speech_buf.append(window)
                # Update the running max RMS and loud-window counter
                # while we're mid-segment.
                w_rms = _window_rms(window)
                if w_rms > self._current_max_rms:
                    self._current_max_rms = w_rms
                if w_rms >= MIN_SPEECH_RMS:
                    self._loud_window_count += 1

        self._remainder = pcm_16k[pos:]
        return result

    def reset(self):
        self._collecting = False
        self._speech_buf = []
        self._remainder = np.array([], dtype=np.float32)
        self._current_max_rms = 0.0
        self._loud_window_count = 0
        self.vad.reset_states()


def _window_rms(window: np.ndarray) -> float:
    """Compute RMS of a single float32 PCM window.

    Input is already float32 (from np.frombuffer in main.py) — no cast
    needed. Called per VAD window (~30 Hz during speech), so avoiding the
    unnecessary array copy is worth it.
    """
    return float(np.sqrt(np.mean(window ** 2)))


def _pcm_to_wav(pcm: np.ndarray) -> bytes:
    """Convert float32 PCM to WAV bytes (16-bit, 16 kHz, mono)."""
    buf = io.BytesIO()
    int16 = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(int16.tobytes())
    buf.seek(0)
    return buf.read()


# Persistent Groq client — created on first use, reused across all calls.
# Creating a new AsyncGroq per request pays TCP+TLS handshake each time.
_groq_client: AsyncGroq | None = None
_groq_client_key: str | None = None


def _get_groq_client(api_key: str) -> AsyncGroq:
    global _groq_client, _groq_client_key
    if _groq_client is None or _groq_client_key != api_key:
        _groq_client = AsyncGroq(api_key=api_key)
        _groq_client_key = api_key
        log.info("Groq client created (persistent)")
    return _groq_client


async def transcribe(
    wav_bytes: bytes,
    api_key: str,
    speech_end_ts: float | None = None,
    user_name: str | None = None,
) -> str:
    """Send WAV to Groq Whisper, return transcript text.

    `user_name`, if provided, is appended to the Whisper prompt so the
    decoder recognizes it as a proper noun. The name is extracted by
    UserContext when the user introduces themselves, and persists across
    the session. Dynamic prompt biasing — see D25c for the general rule
    (only bias on rare-word disambiguation, never conversational filler).
    """
    client = _get_groq_client(api_key)
    # Whisper accepts a `prompt` string as a biasing hint — not part of
    # the audio, just context used to weight the decoder's vocabulary.
    #
    # CRITICAL: every token in this prompt raises its output probability.
    # Earlier versions of this prompt included common conversational
    # phrases like "thank you", "goodbye", "hello" — which directly caused
    # Whisper to hallucinate "Thank you." on silent/ambiguous audio,
    # because it was the highest-prior phrase the decoder knew. DO NOT
    # add conversational filler here; keep it strictly to rare-word
    # disambiguation (the assistant's name "Abide" gets mapped to "abide"
    # the verb, "a bide", "abyd", or "bye" without a hint).
    stt_prompt = (
        "The assistant's name is Abide, spelled A-B-I-D-E. "
        "Users may address it as Abide, Hey Abide, or Abide companion."
    )
    if user_name:
        # Sanitize: collapse whitespace/newlines (prevent the extracted
        # name from breaking out of the intended sentence), cap length to
        # avoid prompt bloat, strip control chars.
        clean_name = " ".join(str(user_name).split())[:40]
        # Drop anything that isn't a normal name character.
        clean_name = "".join(c for c in clean_name if c.isalnum() or c in " -'.")
        if clean_name:
            stt_prompt += f" The user's name is {clean_name}."
    # response_format="verbose_json" gives us per-segment no_speech_prob
    # and avg_logprob, which are the standard Whisper hallucination
    # signals (same ones OpenAI's reference implementation uses to
    # suppress hallucinated segments). temperature=0 makes the decoder
    # deterministic and avoids sampling from low-probability paths that
    # produce most hallucinations.
    #
    # Phase U.1 — multi-language: dropped the `language="en"` lock so
    # Whisper auto-detects. The target resident for elderly care is
    # often a first-generation immigrant whose primary language isn't
    # English; hardcoding "en" excluded them entirely. The detected
    # language arrives in `result.language` and we log it. Claude 4.6
    # is multilingual so it naturally responds in whatever language
    # the transcript is in — no extra plumbing needed.
    #
    # Retry: one silent retry with 1 s gap on asyncio.TimeoutError.
    # Matches the Claude first-token (D97) and TTS first-byte patterns.
    # On a Groq-side stall the user doesn't need to re-speak; if both
    # attempts fail we drop the segment and return "" (handled upstream
    # as "Empty transcript, skipping").  t_start is inside the loop so
    # api_ms measures only the successful attempt.
    for _attempt in range(2):
        t_start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                client.audio.transcriptions.create(
                    file=("audio.wav", wav_bytes),
                    model="whisper-large-v3",
                    prompt=stt_prompt,
                    response_format="verbose_json",
                    temperature=0.0,
                ),
                timeout=STT_TIMEOUT_S,
            )
            break  # success — exit retry loop
        except (asyncio.TimeoutError, httpx.TransportError) as exc:
            if _attempt < 1:
                log.warning(
                    "[RETRY] Groq Whisper error on attempt 1 (%s) — retrying in 1s",
                    type(exc).__name__,
                )
                await asyncio.sleep(1.0)
                continue
            log.warning(
                "[STT] Groq Whisper failed after 2 attempts (%s) — dropping segment",
                type(exc).__name__,
            )
            return ""
    t_done = time.monotonic()
    api_ms = (t_done - t_start) * 1000

    detected_language = getattr(result, "language", None)
    if speech_end_ts is not None:
        total_ms = (t_done - speech_end_ts) * 1000
        log.info(
            "[TIMING] STT: speech_end \u2192 transcript = %.0fms (Groq API %.0fms, audio %d bytes, lang=%s)",
            total_ms, api_ms, len(wav_bytes), detected_language or "?",
        )
    else:
        log.info(
            "[TIMING] STT: Groq API %.0fms (audio %d bytes, lang=%s)",
            api_ms, len(wav_bytes), detected_language or "?",
        )

    transcript = (getattr(result, "text", "") or "").strip()

    # Confidence filter using Whisper's own uncertainty signals.
    #
    # verbose_json returns a `segments` list; each segment has:
    #   - no_speech_prob: P(segment is silence/non-speech)
    #   - avg_logprob:    mean log-probability of the emitted tokens
    #
    # OpenAI's reference implementation suppresses a segment when
    # no_speech_prob > 0.6 AND avg_logprob < -1.0. This is THE standard
    # filter for Whisper hallucinations — the model itself is telling us
    # it isn't confident. We apply it at the whole-transcript level: if
    # every segment fails the check, drop the transcript entirely.
    segments = getattr(result, "segments", None) or []
    if segments:
        kept = []
        for seg in segments:
            # Groq may return segments as dicts or pydantic models.
            no_speech = _seg_field(seg, "no_speech_prob", 0.0)
            avg_logprob = _seg_field(seg, "avg_logprob", 0.0)
            if no_speech > 0.6 and avg_logprob < -1.0:
                log.info(
                    "[FILTER] Low-confidence segment dropped: "
                    "no_speech_prob=%.2f avg_logprob=%.2f text=%r",
                    no_speech, avg_logprob, _seg_field(seg, "text", ""),
                )
                continue
            kept.append(_seg_field(seg, "text", ""))
        if not kept:
            log.info("[FILTER] All segments low-confidence, dropping transcript: %r", transcript)
            return ""
        transcript = " ".join(s.strip() for s in kept).strip()

    # Post-filter: drop well-known Whisper hallucinations before they
    # reach Claude. Returning "" routes through the existing "Empty
    # transcript, skipping" path in main.py — same effect as silence.
    if _is_hallucination(transcript):
        log.info("[FILTER] Rejected Whisper hallucination: %r", transcript)
        return ""

    return transcript


def _seg_field(seg, name: str, default):
    """Read a field from a Whisper segment whether it's a dict or a model."""
    if isinstance(seg, dict):
        return seg.get(name, default)
    return getattr(seg, name, default)
