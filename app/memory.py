"""Per-resident UserContext persistence (Phase E).

Stores the ``UserContext`` dataclass (name, topics, preferences, mood) to
a plaintext JSON file keyed by a browser-generated ``resident_id`` UUID.
Conversation turns, audio, and video frames are NOT persisted — see
DESIGN-NOTES.md D78 for the persist-facts-not-history rationale.

Storage layout::

    <repo-root>/memory/<resident_id>.json

Security:
- ``resident_id`` is strictly validated against ``_ID_RE`` (hex + dashes,
  10-64 chars) before being used in a path, to prevent traversal
  attempts like ``../../etc/passwd``.
- After joining, the resolved path is checked to be inside the memory
  directory — belt-and-suspenders against symlink tricks.

Concurrency:
- Writes use atomic ``tmp + replace`` so a crash mid-write can't
  corrupt the file.
- Expected to be called from a worker thread (``loop.run_in_executor``)
  so the voice loop never blocks on disk I/O. Last-writer-wins on
  concurrent writes from the same process is fine — the state is
  idempotent (each save writes the full snapshot).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("abide.memory")

_MEMORY_DIR = Path(__file__).parent.parent / "memory"

# Allowed resident_id format: hex digits + dashes, 10-64 chars. Matches
# crypto.randomUUID() output (36 chars: 8-4-4-4-12) with headroom for
# future formats. Explicitly rejects ``/``, ``\``, ``..``, spaces, etc.
_ID_RE = re.compile(r"^[a-f0-9\-]{10,64}$")


def _safe_path(resident_id: str) -> Path | None:
    """Validate the resident_id and return the resolved JSON path inside
    the memory directory. Returns None if the id is malformed OR if the
    resolved path escapes _MEMORY_DIR (symlink / absolute path / ``..``
    defence). All public API functions in this module go through here."""
    if not isinstance(resident_id, str) or not _ID_RE.match(resident_id):
        log.warning("[MEMORY] Rejected invalid resident_id: %r", resident_id)
        return None
    try:
        candidate = (_MEMORY_DIR / f"{resident_id}.json").resolve()
        mem_root = _MEMORY_DIR.resolve()
    except OSError as e:
        log.warning("[MEMORY] Path resolution failed: %s", type(e).__name__)
        return None
    try:
        candidate.relative_to(mem_root)
    except ValueError:
        log.warning("[MEMORY] Path escape attempt blocked for id=%r", resident_id)
        return None
    return candidate


def load_user_context(resident_id: str) -> dict | None:
    """Return the JSON-deserialized dict for ``resident_id``, or None if
    no record exists / the id is invalid / the file is corrupt.

    Caller is responsible for converting the dict back into a
    ``UserContext`` via ``UserContext.from_dict``.
    """
    path = _safe_path(resident_id)
    if path is None:
        return None
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        log.warning("[MEMORY] File at %s is not a JSON object; ignoring", path)
        return None
    except (OSError, json.JSONDecodeError) as e:
        # Don't log the raw exception — the file's contents could leak
        # via the message. Log the exception class only.
        log.warning("[MEMORY] Load failed (%s) for id=%s", type(e).__name__, resident_id)
        return None


def save_user_context(resident_id: str, payload: dict) -> None:
    """Atomically write ``payload`` (already serializable) to
    ``memory/<resident_id>.json``. Silent no-op if the id is malformed."""
    path = _safe_path(resident_id)
    if path is None:
        return
    try:
        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(path)
    except OSError as e:
        # WARNING not DEBUG: a silent save failure means the user's name,
        # preferences, and mood are lost across sessions with no indication.
        # Common causes are disk-full and permission errors — both need to
        # be visible in the log so an operator can act.
        log.warning("[MEMORY] Save failed (%s) for id=%s", type(e).__name__, resident_id)


def delete_user_context(resident_id: str) -> bool:
    """Remove the stored file for ``resident_id``. Returns True whether
    the file existed or not (both are the "gone" state the caller wants
    after a Forget-me action). Returns False only if the id is invalid
    or an unexpected OSError occurs."""
    path = _safe_path(resident_id)
    if path is None:
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        log.warning("[MEMORY] Delete failed (%s) for id=%s", type(e).__name__, resident_id)
        return False
