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
You are a vision assistant for an elderly-care companion robot. You will \
receive a SHORT SEQUENCE of consecutive frames (usually 3) captured about \
1 second apart, labeled "Frame 1 (oldest)" through "Frame N (most recent)". \
Your job is to describe what the person is doing across the sequence.

HOW TO REASON:
First, compare the frames and identify what changed between them — which \
body parts moved, in what direction. Write this as `motion_cues` in the \
output. Then, pick the `activity` label that matches the SCOPE of the \
motion you just described. Grounding the label in observed cues prevents \
defaulting to a narrow single-pose label when the whole body is moving.

Required JSON shape (emit fields in this order):
{
  "motion_cues": "<=15 words — which body parts changed across frames, and how",
  "activity": "<=10 words — one concrete phrase whose scope matches the cues",
  "noteworthy": true | false,   // see NOTEWORTHY rule below
  "bbox": [x1, y1, x2, y2]   // normalized 0-1 coords of the person in the LAST frame, or null
}

NOTEWORTHY (the flag that decides whether the robot should proactively \
remark on what it just saw):
- Set `noteworthy` to TRUE only when this activity is the kind of event \
a friend sitting in the room would stop and react to — a new, intentional \
moment worth acknowledging. Examples of TRUE: waving, beginning to dance, \
standing up, sitting down, picking up an object, leaving the room, \
starting to eat, stretching. Falls are always noteworthy (set TRUE even \
though the FALL: prefix already flags them).
- Set `noteworthy` to FALSE for: sitting still, talking while making \
natural hand or head gestures, continuing an activity already in \
progress, small postural shifts, looking around. These are background \
— a friend would keep listening, not interrupt.
- Default to FALSE. When genuinely unsure, choose FALSE. Most frames \
should be FALSE; TRUE is reserved for clear, intentional events.

MATCH LABEL SCOPE TO MOTION SCOPE:
- WHOLE-BODY motion (legs + torso move together, or the person changes \
position in the room): dancing, walking, running, jumping, standing up, \
sitting down, bending over, turning, exercising, getting up from the floor, \
falling, slipping.
- LIMB motion (one arm or leg moves, body is otherwise still): waving, \
reaching, pointing, raising a hand, lifting a leg, stretching an arm.
- HAND/OBJECT action (hand manipulating something specific): holding a cup, \
eating, drinking, typing, folding a towel, picking something up, putting \
on a shirt, brushing teeth.
- STATIC posture (nothing changes across the frames): sitting, standing, \
lying down.

If motion spans multiple scopes, choose the LARGEST scope visible. Example: \
if the legs AND the hands are moving, this is WHOLE-BODY (e.g. dancing), \
not LIMB (e.g. waving). When torn between a broader and a narrower label, \
prefer the broader one.

SAFETY (highest priority, overrides everything above):
If the sequence shows someone going down to the ground, lying on the floor, \
collapsing, losing balance, slipping, stumbling, catching themselves on \
furniture, or appearing unable to get up, the `activity` MUST start with \
"FALL:" (e.g. "FALL: slipped and caught themselves."). Err on the side of \
flagging near-falls — a false alarm is far better than a missed fall.

OTHER RULES:
- Do NOT describe clothing, hair, appearance, or the background.
- Do NOT guess emotions, mood, or medical conditions beyond the FALL flag.
- Do NOT start with "Still" unless the frames show the exact same posture \
with zero change.
- If no person is visible in any frame: `motion_cues` = "No person visible", \
`activity` = "Out of frame.", `bbox` = null.

BBOX (always computed from the LAST frame):
- Normalized [0, 1]. [x1, y1] top-left, [x2, y2] bottom-right. x1 < x2 and \
y1 < y2 always. Wrap the person's visible body tightly. Recompute every \
call. If no person visible, return null.

EXAMPLES (showing motion_cues → activity → noteworthy alignment):
{"motion_cues": "Hips sway side to side; both arms up; feet shift", "activity": "Dancing with raised arms.", "noteworthy": true, "bbox": [0.15, 0.02, 0.85, 0.98]}
{"motion_cues": "Right hand moves left-right above head; body still", "activity": "Waving hand above head.", "noteworthy": true, "bbox": [0.20, 0.02, 0.78, 0.90]}
{"motion_cues": "Both feet leave ground briefly; body compresses then extends", "activity": "Jumping in place.", "noteworthy": true, "bbox": [0.25, 0.00, 0.75, 1.00]}
{"motion_cues": "Right knee rises toward chest; left leg grounded", "activity": "Lifting right leg to knee height.", "noteworthy": true, "bbox": [0.20, 0.05, 0.80, 0.95]}
{"motion_cues": "Right arm extends up and forward beyond head", "activity": "Reaching for shelf above.", "noteworthy": true, "bbox": [0.22, 0.00, 0.78, 0.92]}
{"motion_cues": "Torso tips forward, hands approach floor", "activity": "Bending over.", "noteworthy": true, "bbox": [0.20, 0.20, 0.80, 1.00]}
{"motion_cues": "Arms extend overhead, torso lengthens", "activity": "Stretching arms overhead.", "noteworthy": true, "bbox": [0.18, 0.00, 0.82, 0.98]}
{"motion_cues": "Hand moves to mouth holding a cup; head tilts back", "activity": "Drinking from a cup.", "noteworthy": true, "bbox": [0.22, 0.08, 0.80, 0.96]}
{"motion_cues": "Body orientation shifts; feet cover ground across frames", "activity": "Walking across the room.", "noteworthy": true, "bbox": [0.30, 0.10, 0.70, 0.95]}
{"motion_cues": "Knees bend, torso lowers onto seat", "activity": "Sitting down.", "noteworthy": true, "bbox": [0.22, 0.20, 0.78, 0.98]}
{"motion_cues": "No change across frames; seated, hands resting", "activity": "Sitting still.", "noteworthy": false, "bbox": [0.22, 0.08, 0.80, 0.96]}
{"motion_cues": "Hand touches cheek briefly while speaking; body still", "activity": "Talking with hand at face.", "noteworthy": false, "bbox": [0.22, 0.08, 0.80, 0.96]}
{"motion_cues": "Small head turns while speaking; shoulders still", "activity": "Talking and looking around.", "noteworthy": false, "bbox": [0.22, 0.08, 0.80, 0.96]}
{"motion_cues": "Sudden drop; body goes horizontal on floor", "activity": "FALL: collapsed to the floor.", "noteworthy": true, "bbox": [0.10, 0.55, 0.90, 0.98]}
{"motion_cues": "Foot slips, body tilts, hand grabs furniture", "activity": "FALL: slipped and caught themselves.", "noteworthy": true, "bbox": [0.15, 0.40, 0.85, 0.98]}
{"motion_cues": "No person visible", "activity": "Out of frame.", "noteworthy": false, "bbox": null}

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
    """One vision observation: activity text + optional normalized bbox.

    `noteworthy` is the model's own judgment on whether the activity is
    the kind of event a companion-in-the-room would proactively remark
    on (waving, standing up, dancing, etc.) versus routine/ongoing
    behavior (sitting, talking with natural gestures). Replaces the old
    hard-coded keyword list in session.py: the vision model now decides
    based on semantic understanding of the scene.
    """

    activity: str
    bbox: list[float] | None = None  # [x1, y1, x2, y2] in [0, 1]
    noteworthy: bool = False

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
        # Trim BEFORE append so the list never briefly exceeds BUFFER_SIZE +
        # 1, even for one statement. Previously we appended then re-sliced,
        # which allocated a new list each time past the cap and held the
        # over-sized entry for the gap between append and reassign. Cheap
        # polish; matters more if BUFFER_SIZE ever grows.
        if len(self.entries) >= BUFFER_SIZE:
            del self.entries[0:len(self.entries) - BUFFER_SIZE + 1]
        self.entries.append(SceneEntry(ts=now, result=result))

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
    frames_b64: list[str],
    api_key: str,
    prior: list[str] | None = None,
) -> SceneResult:
    """Send a SHORT sequence of consecutive JPEG frames (~1s apart) to
    GPT-4o-mini and return a SceneResult describing the motion across them.

    `frames_b64` is a list of 2-4 pre-encoded base64 JPEG strings ordered
    oldest → newest. Keeping them base64 end-to-end avoids a decode-then-
    re-encode cycle on the hot path (saves ~20-40 ms per vision cycle).
    main.py validates the base64 character class + size cap before
    forwarding.
    `prior` is ignored for activity (we have proper motion context now) but
    kept in the signature for back-compat.

    On any error, returns SceneResult(activity="", bbox=None) — callers
    should treat empty activity as "no update".
    """
    empty = SceneResult(activity="", bbox=None)
    if not frames_b64:
        return empty

    # Build user content: all frames in order, then an instruction.
    user_content: list[dict] = []
    n = len(frames_b64)
    for i, b64 in enumerate(frames_b64):
        if not b64:
            continue
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
    motion_cues: str = ""  # CoT grounding field — diagnostic only, not passed on
    noteworthy: bool = False
    try:
        parsed = json.loads(raw_content)
        activity = str(parsed.get("activity", "")).strip()
        bbox = _validate_bbox(parsed.get("bbox"))
        # motion_cues is optional: older prompt versions don't request it,
        # and the model may occasionally skip it. Absence is not an error.
        motion_cues = str(parsed.get("motion_cues", "")).strip()
        # noteworthy: accept explicit bool or common string variants.
        # Default False (don't over-react when the field is missing).
        raw_nw = parsed.get("noteworthy", False)
        if isinstance(raw_nw, bool):
            noteworthy = raw_nw
        elif isinstance(raw_nw, str):
            noteworthy = raw_nw.strip().lower() in ("true", "yes", "1")
        else:
            noteworthy = bool(raw_nw)
    except json.JSONDecodeError:
        log.warning("Vision JSON decode failed; using raw text. Raw=%r", raw_content[:120])
        activity = raw_content.split("\n", 1)[0].strip()

    # Approximate decoded byte count from base64 length (decoded size ≈
    # 3/4 of base64 length). Used only for diagnostic logging, not for
    # any decision that needs exact bytes.
    total_bytes = sum(len(s) for s in frames_b64 if s) * 3 // 4
    log.info(
        "[TIMING] Vision: %.0fms | n=%d in=%dB activity=%r cues=%r noteworthy=%s bbox=%s",
        elapsed_ms,
        n,
        total_bytes,
        activity[:80],
        motion_cues[:80],
        noteworthy,
        bbox,
    )
    return SceneResult(activity=activity, bbox=bbox, noteworthy=noteworthy)


# Back-compat shim so any remaining caller using the old single-frame name
# still works. Delegates to analyze_frames with a one-element list.
async def analyze_frame(
    jpeg_b64: str,
    api_key: str,
    prior: list[str] | None = None,
) -> SceneResult:
    return await analyze_frames([jpeg_b64], api_key, prior)
