"""GPT-4o-mini vision pipeline for activity description + bounding box.

Same pattern as conversation.py / tts.py:
- Module-level persistent httpx.AsyncClient (HTTP/2) to avoid per-call
  TLS handshake overhead (~400-800ms on Windows).
- Direct httpx, not the openai SDK, for Windows SSL consistency.

VisionBuffer holds the rolling last 3 scene descriptions so they can be
injected into Claude's system prompt per turn (short-term temporal context).
Only the activity text is used for Claude — bounding boxes are purely for
the UI overlay.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("abide.vision")

CHAT_URL = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"
BUFFER_SIZE = 5  # rolling buffer of last N descriptions (with timestamps for temporal awareness)

VISION_SYSTEM_PROMPT = """\
You are a vision assistant for an elderly care companion. You will receive \
a SHORT SEQUENCE of consecutive frames (usually 3) captured about 1 second \
apart. Analyze the motion across the frames and describe what the person \
is doing, paying close attention to whether they are moving or stationary.

CRITICAL: Use the whole sequence to decide the action. If the person's \
position or pose changes between frames, describe the MOTION (dancing, \
walking, waving, reaching, falling, getting up, sitting down). If nothing \
moves, describe the static posture and any visible hand action.

Required JSON shape:
{
  "activity": "short phrase, max 10 words",
  "bbox": [x1, y1, x2, y2]  // normalized 0-1 coords of the person in the LAST frame, or null
}

Rules for "activity":
- At most 10 words. One short concrete phrase.
- Prefer motion verbs when motion is visible: walking, dancing, waving, \
reaching, sitting down, standing up, bending over, stretching, exercising, \
falling, getting up from the floor.
- If no motion is visible across the frames, describe the static state: \
sitting, standing, lying down, holding a cup, holding up fingers, reading, \
typing, eating, folding a towel, putting on a shirt, brushing teeth.
- SAFETY: if the sequence shows someone going down to the ground, lying on \
the floor, collapsing, or unable to get up, the activity MUST start with \
"FALL:" (e.g. "FALL: lying on the floor after falling."). This is an \
urgent signal and must not be downplayed.
- Do NOT describe clothing, hair, appearance, or the background.
- Do NOT guess emotions, mood, or medical conditions beyond the fall flag.
- Do NOT start with "Still" unless the sequence genuinely shows the exact \
same posture with no change.
- If no person is visible in any frame, activity is "Out of frame." and \
bbox is null.

Rules for "bbox" (ALWAYS computed from the LAST frame in the sequence):
- Coordinates are NORMALIZED to [0, 1] relative to the image. (0,0) is \
the top-left, (1,1) is the bottom-right.
- [x1, y1] is the top-left corner, [x2, y2] is the bottom-right corner.
- x1 < x2 and y1 < y2 always.
- Wrap the person's visible body tightly in the LAST frame.
- Recompute the bbox for every call — do not copy any previous bbox.
- If no person is visible, return null.

Examples of valid responses:
{"activity": "Sitting, looking at the camera.", "bbox": [0.22, 0.08, 0.80, 0.96]}
{"activity": "Holding up three fingers.", "bbox": [0.18, 0.05, 0.82, 0.88]}
{"activity": "Waving hand above head.", "bbox": [0.20, 0.02, 0.78, 0.90]}
{"activity": "Dancing, arms raised.", "bbox": [0.15, 0.02, 0.85, 0.98]}
{"activity": "Walking across the room.", "bbox": [0.30, 0.10, 0.70, 0.95]}
{"activity": "FALL: collapsed to the floor.", "bbox": [0.10, 0.55, 0.90, 0.98]}
{"activity": "Out of frame.", "bbox": null}

Return ONLY the JSON object, nothing else.
"""

# Fall-alert keywords checked server-side on the returned activity string.
FALL_KEYWORDS = ("fall:", "fallen", "collapsed", "lying on the floor",
                 "on the ground", "on the floor")

# Module-level persistent client.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=60.0),
            http2=True,
        )
        log.info("Vision httpx client created (persistent HTTP/2)")
    return _client


async def prewarm(api_key: str):
    """Warm up the OpenAI TLS connection for vision calls."""
    t0 = time.monotonic()
    client = _get_client()
    try:
        await client.head(
            CHAT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        elapsed = (time.monotonic() - t0) * 1000
        log.info("[TIMING] Vision prewarm: %.0fms", elapsed)
    except Exception as e:
        log.warning("Vision prewarm failed (non-fatal): %s", e)


async def aclose():
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        _client = None


@dataclass
class SceneResult:
    """One vision observation: activity text + optional normalized bbox."""

    activity: str
    bbox: list[float] | None = None  # [x1, y1, x2, y2] in [0, 1]

    @property
    def is_fall(self) -> bool:
        """True if the activity text indicates a fall-related event."""
        if not self.activity:
            return False
        low = self.activity.lower()
        return any(kw in low for kw in FALL_KEYWORDS)


@dataclass
class SceneEntry:
    ts: float
    result: SceneResult


@dataclass
class VisionBuffer:
    """Rolling buffer of the last N scene observations with stability tracking."""

    entries: list[SceneEntry] = field(default_factory=list)
    # Stability: count of consecutive identical activity descriptions.
    # Used to suppress redundant "still sitting" injections into Claude's
    # system prompt. See D61.
    _consecutive_count: int = 0
    # Monotonic timestamp of the first occurrence of the current stable
    # activity. When this is None, nothing has stabilized yet.
    _stable_since: float | None = None
    # Monotonic timestamp of the last as_context() inject while stable —
    # prevents re-injecting before STABLE_REMIND_S elapses.
    _last_stable_inject_ts: float = 0.0

    def append(self, result: SceneResult):
        if not result or not result.activity:
            return
        now = time.monotonic()
        prev_activity = self.entries[-1].result.activity if self.entries else None
        self.entries.append(SceneEntry(ts=now, result=result))
        if len(self.entries) > BUFFER_SIZE:
            self.entries = self.entries[-BUFFER_SIZE:]

        # Stability accounting
        if prev_activity is not None and result.activity == prev_activity:
            self._consecutive_count += 1
            if self._consecutive_count >= 3 and self._stable_since is None:
                self._stable_since = now
        else:
            self._consecutive_count = 1
            self._stable_since = None
            # Reset the reminder clock on any activity change so the
            # next injection after stabilization is immediate.
            self._last_stable_inject_ts = 0.0

    @property
    def latest(self) -> str:
        return self.entries[-1].result.activity if self.entries else ""

    @property
    def is_stable(self) -> bool:
        """True if the latest activity has been identical for 3+ consecutive
        observations. Used by Session/main to decide whether to inject
        vision context as 'new' information to Claude."""
        return self._consecutive_count >= 3 and self._stable_since is not None

    def recent_texts(self) -> list[str]:
        return [e.result.activity for e in self.entries]

    # Seconds after which a stable activity gets re-injected as a reminder.
    # Below this, as_context() returns "" to avoid Claude repeating itself.
    STABLE_REMIND_S = 30.0

    def as_context(self) -> str:
        """Format the buffer for injection into Claude's system prompt.

        Only the activity string is passed to Claude — bboxes are UI-only.
        Returns empty string if there's no history yet, or if the activity
        has been stable (same 3+ times) for less than STABLE_REMIND_S seconds
        (suppresses redundant "still sitting" commentary). Includes relative
        timestamps so Claude has temporal awareness of activity changes.
        """
        if not self.entries:
            return ""

        now = time.monotonic()
        # Stability suppression: if the activity hasn't changed recently
        # AND we already injected less than STABLE_REMIND_S seconds ago,
        # skip injection so Claude stops saying "I still see you sitting".
        if self.is_stable:
            since_last_inject = now - self._last_stable_inject_ts
            if since_last_inject < self.STABLE_REMIND_S:
                return ""
            # Fall through and inject — this is the periodic reminder.
            self._last_stable_inject_ts = now
        else:
            # Activity just changed (or still settling) — always inject
            # and mark the time in case it stabilizes next.
            self._last_stable_inject_ts = now

        lines = ["Recent activity history (most recent last):"]
        for e in self.entries:
            ago = now - e.ts
            if ago < 10:
                label = "just now"
            elif ago < 60:
                label = f"{int(ago)}s ago"
            elif ago < 3600:
                mins = int(ago // 60)
                label = f"{mins} min ago"
            else:
                label = f"{int(ago // 3600)}h ago"
            lines.append(f"- {label}: {e.result.activity}")
        if self.is_stable:
            lines.append("(Note: this activity has been stable for a while.)")
        return "\n".join(lines)

    def clear(self):
        self.entries.clear()


def _validate_bbox(raw) -> list[float] | None:
    """Return a cleaned [x1, y1, x2, y2] in [0,1], or None if invalid."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        vals = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    # Clamp to [0, 1]
    vals = [max(0.0, min(1.0, v)) for v in vals]
    x1, y1, x2, y2 = vals
    if x1 >= x2 or y1 >= y2:
        return None
    return vals


async def analyze_frames(
    jpegs: list[bytes],
    api_key: str,
    prior: list[str] | None = None,
) -> SceneResult:
    """Send a SHORT sequence of consecutive JPEG frames (~1s apart) to
    GPT-4o-mini and return a SceneResult describing the motion across them.

    `jpegs` should be 2-4 frames ordered oldest → newest.
    `prior` is ignored for activity (we have proper motion context now) but
    kept in the signature for back-compat.

    On any error, returns SceneResult(activity="", bbox=None) — callers
    should treat empty activity as "no update".
    """
    empty = SceneResult(activity="", bbox=None)
    if not jpegs:
        return empty

    # Build user content: all frames in order, then an instruction.
    user_content: list[dict] = []
    n = len(jpegs)
    for i, jpeg in enumerate(jpegs):
        if not jpeg:
            continue
        b64 = base64.b64encode(jpeg).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"
        if n == 1:
            caption = "Frame (only this one):"
        elif i == 0:
            caption = f"Frame 1 of {n} (oldest):"
        elif i == n - 1:
            caption = f"Frame {i+1} of {n} (most recent):"
        else:
            caption = f"Frame {i+1} of {n}:"
        user_content.append({"type": "text", "text": caption})
        user_content.append(
            {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}}
        )
    user_content.append({
        "type": "text",
        "text": (
            "Describe what the person is DOING across this sequence. If they "
            "are moving between frames, use a motion verb. Compute the bbox "
            "from the MOST RECENT frame. Return the JSON object."
        ),
    })

    payload = {
        "model": MODEL,
        "max_tokens": 120,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.monotonic()
    client = _get_client()
    try:
        resp = await client.post(CHAT_URL, headers=headers, json=payload)
    except Exception as e:
        log.error("Vision request failed: %s", e)
        return empty

    elapsed_ms = (time.monotonic() - t0) * 1000

    if resp.status_code != 200:
        log.error("Vision API %d: %s", resp.status_code, resp.text[:200])
        return empty

    try:
        data = resp.json()
        raw_content = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("Vision response envelope parse failed: %s", e)
        return empty

    # Try JSON parse; fall back to treating raw as activity text if the
    # model ignored the json_object response_format for any reason.
    activity: str = ""
    bbox: list[float] | None = None
    try:
        parsed = json.loads(raw_content)
        activity = str(parsed.get("activity", "")).strip()
        bbox = _validate_bbox(parsed.get("bbox"))
    except json.JSONDecodeError:
        log.warning("Vision JSON decode failed; using raw text. Raw=%r", raw_content[:120])
        activity = raw_content.split("\n", 1)[0].strip()

    total_bytes = sum(len(j) for j in jpegs if j)
    log.info(
        "[TIMING] Vision: %.0fms | n=%d in=%dB activity=%r bbox=%s",
        elapsed_ms,
        n,
        total_bytes,
        activity[:80],
        bbox,
    )
    return SceneResult(activity=activity, bbox=bbox)


# Back-compat shim so any remaining caller using the old single-frame name
# still works. Delegates to analyze_frames with a one-element list.
async def analyze_frame(
    jpeg_bytes: bytes,
    api_key: str,
    prior: list[str] | None = None,
) -> SceneResult:
    return await analyze_frames([jpeg_bytes], api_key, prior)
