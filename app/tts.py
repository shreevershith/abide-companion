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
from app.conversation import APIKeyError

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

# Mid-stream inter-chunk deadline. Once the first byte arrives (and the
# first-byte timeout is disabled via reschedule(None)), an OpenAI API
# throttle can dribble data at ~7 KB/s instead of the normal 100+ KB/s,
# causing an 11 s TTS delivery for a short sentence (observed in live
# session abide-350964a5617c). Without a per-chunk timeout there is
# nothing to abort a stalled-but-still-connected stream. 3 s is well
# above the normal inter-chunk gap at full speed (~50-100 ms) and still
# catches a throttled stream ~8 s before the old worst-case.
_TTS_CHUNK_TIMEOUT_S = 3.0

# Rate-based abort for slow-dribble throttle. The per-chunk timeout
# above only fires when a single gap between consecutive chunks exceeds
# 3 s. A different throttle pattern (chunks arriving every 1-2 s at
# 10-21 KB/s instead of 100+ KB/s) was observed in live session
# abide-ee7e5abb250e: "Ha, just waving hello then — what's up, Shree?"
# delivered 125,400 B at ~21 KB/s = 6,000 ms first→last, causing
# audible distortion (ring-buffer underrun). The chunk timeout never
# fired because each individual gap was <3 s.
# Fix: after a 1.5 s grace period (the ring buffer is well-stocked by
# then), if the overall delivery rate falls below 30 KB/s we abort. At
# normal speed (100-200 KB/s) a 1.5 s check yields ~150-300 KB received
# — well above any realistic sentence size — so this only triggers under
# genuine throttle.
_TTS_MIN_RATE_BPS = 30 * 1024   # 30 KB/s floor; normal is 100-200 KB/s
_TTS_RATE_CHECK_AFTER_S = 1.5   # grace period before rate enforcement

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
        "response_format": "pcm",
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
                if resp.status_code == 401:
                    raise APIKeyError(
                        "Please check your OpenAI API key in the settings panel (gear icon)."
                    )
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


async def stream_sentence(
    text: str,
    api_key: str,
    sentence_detected_ts: float | None = None,
):
    """Async generator: yields raw PCM chunks (16-bit LE, 24 kHz, mono) as they
    arrive from OpenAI TTS.

    Cache hit path: yields the cached bytes in one shot and returns.
    Live path: streams from OpenAI, yielding each ~4 KB chunk (~85 ms of audio)
    as it lands — first chunk arrives ~100-300 ms into the call, so the
    browser can start playing before synthesis is complete.

    Graceful degradation:
      - Emoji-only text → yields nothing (empty generator).
      - First-byte timeout → yields nothing (same as synthesize()).
      - APIKeyError → re-raised so session.py can surface it to the UI.
    """
    speakable = _strip_nonspeakable(text)
    if not speakable:
        log.info("[TTS] Skipping emoji-only sentence: %r", text[:40])
        return

    cache_key = _normalize_phrase(speakable)
    cached = _tts_cache.get(cache_key)
    if cached is not None:
        if sentence_detected_ts is not None:
            ms = (time.monotonic() - sentence_detected_ts) * 1000
            log.info("[TTS-CACHE] Hit '%s...' (sentence\u2192first=%.0fms, %dB)", text[:40], ms, len(cached))
        else:
            log.info("[TTS-CACHE] Hit '%s...' (%dB)", text[:40], len(cached))
        yield cached
        return

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "tts-1",
        "voice": "nova",
        "input": speakable,
        "response_format": "pcm",
    }

    t_call = time.monotonic()
    first_byte_ts: float | None = None
    total_bytes = 0

    client = _get_client()
    try:
        timeout_cm = asyncio.timeout(_TTS_FIRST_BYTE_TIMEOUT_S)
        async with timeout_cm:
            async with client.stream("POST", TTS_URL, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    log.error("TTS error %d: %s", resp.status_code, body[:200])
                    if resp.status_code == 401:
                        raise APIKeyError(
                            "Please check your OpenAI API key in the settings panel (gear icon)."
                        )
                    if resp.status_code == 429:
                        # Rate-limited by OpenAI. Skip this sentence cleanly
                        # instead of raising — the response pipeline continues
                        # with the next sentence, and the user hears a partial
                        # reply rather than a hard error.
                        log.warning("[TTS] Rate limited by OpenAI — skipping sentence %r", text[:40])
                        return
                    raise Exception(f"TTS API returned {resp.status_code}: {body[:200]!r}")

                _leftover = b""
                _aiter = resp.aiter_bytes().__aiter__()
                while True:
                    try:
                        if first_byte_ts is None:
                            # Waiting for first byte — first-byte timeout_cm covers this.
                            chunk = await _aiter.__anext__()
                        else:
                            # Streaming — apply inter-chunk deadline so a throttled
                            # OpenAI stream (observed at ~7 KB/s in live sessions)
                            # aborts after _TTS_CHUNK_TIMEOUT_S instead of dribbling
                            # for 11+ seconds.
                            chunk = await asyncio.wait_for(
                                _aiter.__anext__(), timeout=_TTS_CHUNK_TIMEOUT_S
                            )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        elapsed_ms = (time.monotonic() - first_byte_ts) * 1000
                        log.warning(
                            "[STALL] TTS mid-stream: no data for %.1fs after %.0fms "
                            "— aborting '%s...'",
                            _TTS_CHUNK_TIMEOUT_S, elapsed_ms, text[:40],
                        )
                        return
                    if not chunk:
                        continue
                    if first_byte_ts is None:
                        first_byte_ts = time.monotonic()
                        timeout_cm.reschedule(None)
                        if sentence_detected_ts is not None:
                            log.info(
                                "[TIMING] TTS first byte '%s...' | sentence\u2192first=%.0fms call\u2192first=%.0fms",
                                text[:40],
                                (first_byte_ts - sentence_detected_ts) * 1000,
                                (first_byte_ts - t_call) * 1000,
                            )
                    # Align to Int16 (2-byte) boundary. An odd-byte chunk causes
                    # new Int16Array(buf) to drop the stray byte, byte-swapping
                    # every subsequent sample in that chunk — sounds like static.
                    chunk = _leftover + chunk
                    _leftover = b""
                    if len(chunk) % 2 == 1:
                        _leftover = chunk[-1:]
                        chunk = chunk[:-1]
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > _MAX_TTS_BYTES:
                        log.warning(
                            "TTS response exceeded %d bytes (%d) — aborting stream",
                            _MAX_TTS_BYTES, total_bytes,
                        )
                        return
                    # Rate-based abort: per-chunk timeout catches complete
                    # stalls (gap > 3 s), but not slow-dribble (chunks
                    # arriving every 1-2 s at 10-21 KB/s). After the grace
                    # period, check the running average; abort if it's below
                    # the minimum floor.
                    _elapsed = time.monotonic() - first_byte_ts
                    if (
                        _elapsed >= _TTS_RATE_CHECK_AFTER_S
                        and (total_bytes / _elapsed) < _TTS_MIN_RATE_BPS
                    ):
                        _rate_kbs = total_bytes / _elapsed / 1024
                        log.warning(
                            "[STALL] TTS slow-dribble: %.0f KB/s after %.0fms "
                            "— aborting '%s...'",
                            _rate_kbs, _elapsed * 1000, text[:40],
                        )
                        return
                    yield chunk
                if _leftover:
                    yield _leftover + b"\x00"  # pad final stray byte to complete Int16
    except TimeoutError:
        log.warning(
            "[STALL] TTS first-byte deadline (%.1fs) tripped for %r — skipping",
            _TTS_FIRST_BYTE_TIMEOUT_S, text[:40],
        )
        return

    t_done = time.monotonic()
    total_api_ms = (t_done - t_call) * 1000
    first_to_done_ms = (t_done - first_byte_ts) * 1000 if first_byte_ts else -1
    if sentence_detected_ts is not None:
        log.info(
            "[TIMING] TTS done '%s...' | sentence\u2192call=%.0fms first\u2192last=%.0fms total=%.0fms size=%dB",
            text[:40],
            (t_call - sentence_detected_ts) * 1000,
            first_to_done_ms,
            total_api_ms,
            total_bytes,
        )
    else:
        log.info(
            "[TIMING] TTS done '%s...' | first\u2192last=%.0fms total=%.0fms size=%dB",
            text[:40], first_to_done_ms, total_api_ms, total_bytes,
        )


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
            "response_format": "pcm",
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
