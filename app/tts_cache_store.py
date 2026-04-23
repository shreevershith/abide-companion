"""Persistent frequency counter for Claude's spoken sentences.

Replaces the hand-curated ``TTS_CACHE_PHRASES`` seed list with runtime
learning: every sentence Claude completes during a session is recorded
here. Sentences observed at least ``_MIN_COUNT_TO_PREWARM`` times are
returned by ``learned_phrases()`` and prewarmed at the next session
start, so the TTS cache naturally tracks Abide's actual voice instead
of us guessing.

Storage: a single JSON file at ``<repo>/tts_cache/phrase_counts.json``,
keyed by the same normalization the TTS module uses for cache keys
(lowercased, whitespace-collapsed). Atomic write via temp + replace so
a crash mid-save can't corrupt the file. File size bounded to
``_MAX_STORED`` entries — rarely-seen phrases fall off the bottom.

Not per-resident. Abide's voice is Abide's voice regardless of who is
in the room; per-resident caching would just split frequency counts
and waste prewarm budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger("abide.tts_cache_store")

_CACHE_DIR = Path(__file__).parent.parent / "tts_cache"
_STORE_PATH = _CACHE_DIR / "phrase_counts.json"

# Tunables.
_MAX_STORED = 200              # cap the on-disk file so it stays small
_MIN_CHARS = 4                 # skip "Hmm.", single words
_MAX_CHARS = 90                # skip long sentences unlikely to recur exactly
_MIN_COUNT_TO_PREWARM = 2      # must be heard at least N times before prewarming


# Module-level state. In-memory mirror of the JSON file.
# Key: `_norm_key(sentence)`; value: {"text": original-case sentence, "count": int}.
# Keying on the normalized form means "Hello there!" and "hello there!"
# accumulate on the same entry; we keep the most recent original-case
# text so prewarming uses the natural casing.
_entries: dict[str, dict] = {}
_loaded = False


def _norm_key(text: str) -> str:
    """Same normalization as ``tts._normalize_phrase``. Kept as a local
    helper to avoid a circular import."""
    return " ".join(text.lower().strip().split())


def _ensure_loaded() -> None:
    """Lazy one-time load of the JSON store. Sets _loaded first so a
    corrupt-file error doesn't cause every record_phrase() call to
    re-attempt the failing load."""
    global _entries, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if _STORE_PATH.exists():
            with _STORE_PATH.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cleaned: dict[str, dict] = {}
                for k, v in raw.items():
                    if not isinstance(k, str):
                        continue
                    if isinstance(v, dict):
                        text = str(v.get("text", k)).strip()
                        try:
                            count = int(v.get("count", 0))
                        except (TypeError, ValueError):
                            continue
                    elif isinstance(v, (int, float)):
                        # Back-compat: earlier format was {key: int}.
                        text, count = k, int(v)
                    else:
                        continue
                    if count > 0 and text:
                        cleaned[k] = {"text": text, "count": count}
                _entries = cleaned
                log.info(
                    "[TTS-STORE] Loaded %d phrase counts from %s",
                    len(_entries), _STORE_PATH,
                )
    except Exception as e:
        # Don't surface the raw exception — file may contain partial contents
        # we don't want to echo. Log the type only.
        log.warning("[TTS-STORE] Load failed (%s) — starting empty", type(e).__name__)
        _entries = {}


def record_phrase(sentence: str) -> None:
    """Increment the frequency count for a completed sentence and persist.

    Called at each sentence boundary in the Claude→TTS producer. Short
    (<4 char) or long (>90 char) sentences are skipped — they're
    unlikely to hit exact-string cache lookups. Persistence is sync
    but writes a small JSON file, so wall-clock impact is negligible
    on the voice loop (<1ms typical).
    """
    sentence = (sentence or "").strip()
    if not (_MIN_CHARS <= len(sentence) <= _MAX_CHARS):
        return
    _ensure_loaded()
    key = _norm_key(sentence)
    entry = _entries.get(key)
    if entry is None:
        _entries[key] = {"text": sentence, "count": 1}
    else:
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["text"] = sentence  # keep the most recently observed casing
    # The dict update above is sync (fast, GIL-atomic). The disk write
    # is handed to the default executor so it doesn't block the voice
    # loop — on every sentence boundary this used to stall the producer
    # coroutine for ~2-10 ms. Falls back to sync write when no loop is
    # running (e.g., unit tests, CLI usage).
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _save()
        return
    loop.run_in_executor(None, _save)


def _save() -> None:
    """Atomic write of the current state. Keeps only the top ``_MAX_STORED``
    entries by count so the file doesn't grow without bound on a
    long-running deployment.

    Runs either inline (no event loop) or in the default asyncio executor
    (off the voice loop). In the executor case the producer coroutine
    may be mutating ``_entries`` concurrently; we snapshot once via
    ``dict(_entries)`` — that call is atomic under the GIL — so the
    sort + write iterates over a frozen copy and can't raise
    "dict changed size during iteration"."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_src = dict(_entries)
        items = sorted(
            snapshot_src.items(),
            key=lambda kv: kv[1].get("count", 0),
            reverse=True,
        )[:_MAX_STORED]
        snapshot = dict(items)
        tmp = _STORE_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        tmp.replace(_STORE_PATH)
    except Exception as e:
        # WARNING not DEBUG: a silent save failure means the phrase-frequency
        # store stops persisting and the next session's prewarm list regresses
        # to the seed list only. Disk-full and permission errors need to be
        # visible so an operator can act.
        log.warning("[TTS-STORE] save failed: %s", type(e).__name__)


def learned_phrases(limit: int = 40) -> list[str]:
    """Return original-case phrases heard at least ``_MIN_COUNT_TO_PREWARM``
    times across all prior sessions, ordered by count descending.

    The caller merges this with any hard-coded baseline seed list and
    passes the combined result to ``tts.prewarm_cache()``.
    """
    _ensure_loaded()
    eligible = [
        (v.get("text", k), int(v.get("count", 0)))
        for k, v in _entries.items()
        if int(v.get("count", 0)) >= _MIN_COUNT_TO_PREWARM
    ]
    eligible.sort(key=lambda tc: tc[1], reverse=True)
    return [t for t, _ in eligible[:limit]]


def stats() -> dict:
    """Diagnostic snapshot of the store state."""
    _ensure_loaded()
    return {
        "total_entries": len(_entries),
        "eligible_for_prewarm": sum(
            1 for v in _entries.values()
            if int(v.get("count", 0)) >= _MIN_COUNT_TO_PREWARM
        ),
        "path": str(_STORE_PATH),
    }
