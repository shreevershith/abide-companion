"""OpenAI TTS via direct httpx (bypasses SDK for Windows compatibility).

Uses a MODULE-LEVEL persistent httpx.AsyncClient. Creating a fresh client
per request was costing 400-800ms per TTS call on Windows (TCP+TLS handshake).
Never create a new client inside synthesize().
"""

import logging
import time
import httpx

log = logging.getLogger("abide.tts")

TTS_URL = "https://api.openai.com/v1/audio/speech"

# Module-level persistent client. Created on first use so module import stays cheap.
_client: httpx.AsyncClient | None = None


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

    If sentence_detected_ts (monotonic) is provided, logs the full timing breakdown:
      sentence boundary detected → API call start → first byte → last byte.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "tts-1",
        "voice": "nova",
        "input": text,
        "response_format": "opus",
    }

    t_call = time.monotonic()
    first_byte_ts: float | None = None
    chunks: list[bytes] = []

    client = _get_client()
    async with client.stream("POST", TTS_URL, headers=headers, json=payload) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            log.error("TTS error %d: %s", resp.status_code, body[:200])
            raise Exception(f"TTS API returned {resp.status_code}: {body[:200]!r}")

        async for chunk in resp.aiter_bytes():
            if first_byte_ts is None and chunk:
                first_byte_ts = time.monotonic()
            chunks.append(chunk)

    t_done = time.monotonic()
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
