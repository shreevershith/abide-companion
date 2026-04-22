"""OpenAI TTS via direct httpx (bypasses SDK for Windows compatibility).

Uses a MODULE-LEVEL persistent httpx.AsyncClient. Creating a fresh client
per request was costing 400-800ms per TTS call on Windows (TCP+TLS handshake).
Never create a new client inside synthesize().
"""

import asyncio
import logging
import re
import time
import httpx

log = logging.getLogger("abide.tts")

# Phase S.3 follow-up — strip pictographs / emoji before sending text
# to OpenAI TTS. Claude likes to close sentences with 😄 👋 — TTS
# synthesises ~2 KB of gibberish (or just silence) for those characters
# and we observed up to 6.5 s of wasted call time on an emoji-only
# sentence. The emoji stays in the transcript/diary where it reads
# fine; it just doesn't go to the audio stream. Unicode ranges cover
# miscellaneous symbols + pictographs + dingbats + supplemental pairs.
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # supplemental symbols & pictographs (incl. 1F600 emoticons)
    "\U00002600-\U000027BF"  # dingbats, misc symbols
    "\U0001F000-\U0001F1FF"  # mahjong, domino, regional indicators
    "\u200D\uFE0F"             # zero-width joiner and variation selector-16 (emoji modifiers)
    "]+",
    flags=re.UNICODE,
)


def _strip_nonspeakable(text: str) -> str:
    """Remove emoji / pictograph characters from text before TTS. Safe
    for normal prose (doesn't touch ASCII, Latin, CJK, punctuation).
    Collapses runs of trailing whitespace left behind so e.g.
    ``"Pretty good! 😄"`` becomes ``"Pretty good!"``, not
    ``"Pretty good! "``."""
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    # Normalise collapsed whitespace runs without touching content.
    return re.sub(r"[ \t]+", " ", cleaned).strip()

TTS_URL = "https://api.openai.com/v1/audio/speech"

# Module-level persistent client. Created on first use so module import stays cheap.
_client: httpx.AsyncClient | None = None

# Hard cap on a single TTS response's audio size. Sentences Claude emits
# are 2-3 short sentences, ~5-50 KB of opus each. A response an order of
# magnitude larger means the stream misbehaved (malformed Accept header,
# MITM, bogus OpenAI response) — drop it rather than OOM the process
# trying to concat it. 4 MB allows ~3-4 min of opus with generous
# headroom.
_MAX_TTS_BYTES = 4 * 1024 * 1024

# Phase U.3 follow-up #4 — first-byte deadline for OpenAI TTS. The shared
# httpx.AsyncClient has timeout=15s end-to-end, but that lets a hung
# request sit for 15 seconds of silence on the speakers before failing.
# 10 seconds is well above observed p95 first-byte (~1.5-2 s) so genuine
# slow-but-working responses still complete; past that is an API stall
# and we fail the sentence gracefully rather than block the consumer.
_TTS_FIRST_BYTE_TIMEOUT_S = 10.0

# TTS cache: pre-generated opus audio for frequently-used stock phrases.
# Populated by prewarm_cache() at session start. synthesize() checks this
# first and serves the cached bytes instantly (0ms), bypassing the OpenAI
# API call (~1000-2000ms). Keyed by _normalize_phrase(text).
_tts_cache: dict[str, bytes] = {}


def _normalize_phrase(text: str) -> str:
    """Normalize a phrase for cache lookup: lowercased, stripped, collapsed whitespace."""
    return " ".join(text.lower().strip().split())


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        # HTTP/2 for multiplexed streams — see conversation.py for the
        # full explanation. Without this, each sentence TTS call pays a
        # fresh TLS handshake while the previous sentence's stream is
        # still holding the HTTP/1.1 socket.
        _client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=60.0),
            http2=True,
        )
        log.info("TTS httpx client created (persistent HTTP/2)")
    return _client


async def prewarm(api_key: str):
    """Warm up the OpenAI TLS connection before the first real TTS call.

    Uses a HEAD to the TTS endpoint — returns an error instantly but
    establishes the connection for subsequent reuse.
    """
    import time as _time
    t0 = _time.monotonic()
    client = _get_client()
    try:
        await client.head(
            TTS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        elapsed = (_time.monotonic() - t0) * 1000
        log.info("[TIMING] TTS prewarm: %.0fms", elapsed)
    except Exception as e:
        log.warning("TTS prewarm failed (non-fatal): %s", e)


async def aclose():
    """Close the module-level client. Call on app shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


async def synthesize(text: str, api_key: str, sentence_detected_ts: float | None = None) -> bytes:
    """Convert text to speech using OpenAI TTS. Returns raw opus audio bytes.

    If the phrase is in the pre-generated cache (see prewarm_cache), the
    cached bytes are returned instantly — no API call. Otherwise, streams
    from OpenAI TTS and logs timing.

    If sentence_detected_ts (monotonic) is provided, logs the full timing breakdown:
      sentence boundary detected → API call start → first byte → last byte.
    """
    # Strip emoji / pictograph characters before anything else. If
    # what remains is empty or whitespace, the "sentence" was emoji-
    # only — skip synthesis entirely and return silent audio. The
    # consumer sends zero bytes to the WS, which is a no-op on the
    # client side. Without this, OpenAI charges us for ~2 KB of
    # gibberish audio (sometimes up to 6.5 s wall-clock for a single
    # 😄 observed in a live session).
    speakable = _strip_nonspeakable(text)
    if not speakable:
        log.info("[TTS] Skipping emoji-only sentence: %r", text[:40])
        return b""

    # Cache hit path — instant return, bypass OpenAI entirely.
    # Key is normalised on the speakable form, so "That's good. 😄" and
    # "That's good." share a cache entry — same audio either way.
    cache_key = _normalize_phrase(speakable)
    cached = _tts_cache.get(cache_key)
    if cached is not None:
        if sentence_detected_ts is not None:
            sentence_to_first_ms = (time.monotonic() - sentence_detected_ts) * 1000
            log.info(
                "[TTS-CACHE] Hit for '%s...' (sentence\u2192first=%.0fms, %dB)",
                text[:40], sentence_to_first_ms, len(cached),
            )
        else:
            log.info("[TTS-CACHE] Hit for '%s...' (%dB)", text[:40], len(cached))
        return cached

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "tts-1",
        "voice": "nova",
        "input": speakable,
        "response_format": "opus",
    }

    t_call = time.monotonic()
    first_byte_ts: float | None = None
    chunks: list[bytes] = []

    client = _get_client()
    try:
        # First-byte deadline around the stream setup + initial chunk
        # arrival. Once audio starts flowing we call `.reschedule(None)`
        # to disable the deadline for the rest of the response.
        timeout_cm = asyncio.timeout(_TTS_FIRST_BYTE_TIMEOUT_S)
        async with timeout_cm:
          async with client.stream("POST", TTS_URL, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                log.error("TTS error %d: %s", resp.status_code, body[:200])
                raise Exception(f"TTS API returned {resp.status_code}: {body[:200]!r}")

            total_bytes = 0
            aborted = False
            async for chunk in resp.aiter_bytes():
                if first_byte_ts is None and chunk:
                    first_byte_ts = time.monotonic()
                    # Audio started flowing — disable the deadline so
                    # long responses can complete uninterrupted.
                    timeout_cm.reschedule(None)
                total_bytes += len(chunk)
                if total_bytes > _MAX_TTS_BYTES:
                    log.warning(
                        "TTS response exceeded %d bytes (%d) — aborting stream",
                        _MAX_TTS_BYTES, total_bytes,
                    )
                    aborted = True
                    break
                chunks.append(chunk)
    except TimeoutError:
        log.warning(
            "[STALL] TTS first-byte deadline (%.1fs) tripped for %r — skipping this sentence",
            _TTS_FIRST_BYTE_TIMEOUT_S, text[:40],
        )
        return b""

    t_done = time.monotonic()
    if aborted:
        raise Exception(f"TTS response exceeded size cap ({_MAX_TTS_BYTES} bytes)")
    audio = b"".join(chunks)

    call_to_first_ms = (first_byte_ts - t_call) * 1000 if first_byte_ts else -1
    first_to_done_ms = (t_done - first_byte_ts) * 1000 if first_byte_ts else -1
    total_api_ms = (t_done - t_call) * 1000

    if sentence_detected_ts is not None:
        sentence_to_call_ms = (t_call - sentence_detected_ts) * 1000
        sentence_to_first_ms = (first_byte_ts - sentence_detected_ts) * 1000 if first_byte_ts else -1
        log.info(
            "[TIMING] TTS '%s...' | sentence\u2192call=%.0fms call\u2192first_byte=%.0fms "
            "first\u2192last=%.0fms total_api=%.0fms sentence\u2192first=%.0fms size=%dB",
            text[:40],
            sentence_to_call_ms,
            call_to_first_ms,
            first_to_done_ms,
            total_api_ms,
            sentence_to_first_ms,
            len(audio),
        )
    else:
        log.info(
            "[TIMING] TTS '%s...' | call\u2192first_byte=%.0fms first\u2192last=%.0fms "
            "total_api=%.0fms size=%dB",
            text[:40],
            call_to_first_ms,
            first_to_done_ms,
            total_api_ms,
            len(audio),
        )

    return audio


# Hard cap on the cache size. The cache is module-level and persists
# for the server process lifetime; this guard prevents unbounded growth
# if someone ever passes a large phrase list to prewarm_cache().
_TTS_CACHE_MAX_ENTRIES = 64


async def _prewarm_single(phrase: str, api_key: str) -> None:
    """Prewarm a single cache entry. Runs concurrently with siblings."""
    # Apply the same emoji strip + normalise as `synthesize()` so the
    # cached audio matches what a later synthesis lookup will key on.
    speakable = _strip_nonspeakable(phrase)
    if not speakable:
        return
    key = _normalize_phrase(speakable)
    if key in _tts_cache:
        return
    if len(_tts_cache) >= _TTS_CACHE_MAX_ENTRIES:
        log.warning("[TTS-CACHE] Hit max size %d, skipping %r", _TTS_CACHE_MAX_ENTRIES, phrase[:40])
        return
    try:
        t0 = time.monotonic()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "tts-1",
            "voice": "nova",
            "input": speakable,
            "response_format": "opus",
        }
        client = _get_client()
        chunks: list[bytes] = []
        async with client.stream("POST", TTS_URL, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                log.warning(
                    "[TTS-CACHE] Prewarm failed for %r: HTTP %d %s",
                    phrase, resp.status_code, body[:100],
                )
                return
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
        audio = b"".join(chunks)
        _tts_cache[key] = audio
        elapsed_ms = (time.monotonic() - t0) * 1000
        log.info(
            "[TTS-CACHE] Cached '%s...' in %.0fms (%dB)",
            phrase[:40], elapsed_ms, len(audio),
        )
    except Exception as e:
        log.warning("[TTS-CACHE] Prewarm error for %r: %s", phrase, e)


async def prewarm_cache(phrases: list[str], api_key: str) -> None:
    """Pre-generate TTS audio for stock phrases at session start.

    All phrases are synthesized IN PARALLEL via asyncio.gather so total
    wall-clock is ~one API call instead of N. Critical because the welcome
    greeting waits ~1.2s for cache to populate; serial prewarming (5 × 1s)
    would guarantee a cache miss and force a fallback API call for the
    greeting.

    Fired as a fire-and-forget task from main.py. Failures are logged but
    non-fatal — synthesize() falls back to the API path on cache miss.
    Cache persists for the lifetime of the server process (module-level),
    bounded by _TTS_CACHE_MAX_ENTRIES.
    """
    import asyncio
    if not phrases:
        return
    await asyncio.gather(
        *(_prewarm_single(p, api_key) for p in phrases),
        return_exceptions=True,  # don't let one failure cancel siblings
    )
