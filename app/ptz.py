"""DirectShow camera control for Logitech MeetUp (Phase N, revised Phase R).

Why this module exists
----------------------
The W3C MediaCapture-PTZ API in browsers only exposes the subset of UVC
controls Chrome chose to implement. On Logitech MeetUp (firmware 1.0.244
tested) `track.getCapabilities()` returns no pan/tilt caps — see D79 for
the investigation, including a cross-check against Google's reference
demo that showed the same result.

The actual camera-control surface on MeetUp is accessible via standard
Windows DirectShow `IKsPropertySet` / `KSPROPERTY_CAMERACONTROL_*` — the
same path Zoom and Teams use. The `duvc-ctl` Python library wraps that
surface so we can drive camera properties from our backend.

Empirical surface on MeetUp firmware 1.0.244 (via duvc-ctl 2.x):
  - Pan  : get_camera_property_range() returns ok=False (not exposed)
  - Tilt : get_camera_property_range() returns ok=False (not exposed)
  - Zoom : get_camera_property_range() returns ok=True, range [100, 500]
Motorised pan/tilt on MeetUp is gated behind Logitech's proprietary
Sync/Tune SDK; UVC reports they exist but returns garbage ranges when
queried, so we treat them as unsupported. Zoom is the one axis this
module actually drives. The on-request zoom feature (user speaks "zoom
in / out / reset"; Claude emits the `[[CAM:...]]` marker) is Phase R.

Because Docker Desktop on Windows runs in a WSL2 Linux VM that can't
see host DirectShow without admin-level USB/IP setup (which violates
the brief's double-click first-run constraint), we ship as native
Python instead — see D82.

Graceful degradation
--------------------
Every failure mode (non-Windows OS, duvc-ctl not installed, no DirectShow
cameras, no supported axes, COM errors) lands in the same "available=False"
state. The rest of the system (vision, conversation, out-of-frame welfare
check, everything else) works identically whether this module is active
or not.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("abide.ptz")


# Module-level import of duvc-ctl. On non-Windows we marker-skipped the
# install in requirements.txt so this just fails cleanly and PTZ silently
# disables. The error text is stashed so _init() can surface it in the
# per-session startup log — module-load time is before uvicorn has fully
# configured logging, so a log.info() here would often get dropped.
_DUVC_IMPORT_ERROR: str | None = None
try:
    import duvc_ctl as _duvc
    _DUVC_AVAILABLE = True
except Exception as e:
    _duvc = None
    _DUVC_AVAILABLE = False
    _DUVC_IMPORT_ERROR = f"{type(e).__name__}: {e}"


# Bbox-to-motion tuning for pan/tilt nudges.
#
# Phase S.1 retune: MeetUp firmware 1.0.272 turns out to expose pan/tilt
# after all (reversing the earlier D82 correction — kept under observation
# for stability). But MeetUp's range is narrow (pan ±25, tilt ±15), so
# the Phase N constants (fraction 0.20, damping 0.30 → effective 0.06)
# produced 1-unit nudges the user couldn't see.
#
# Phase S.3 follow-up retune: first live session with the S.1 values
# still felt sluggish ("the pan is a little slow, don't you think?" —
# user quote). Bumped `_DELTA_FRACTION` from 0.50 to 0.70 (effective
# motion 0.25 → 0.35 at frame edge). Kept damping at 0.50 so small
# bbox jitter from GPT-4o-mini/4.1-mini's loose spatial grounding
# doesn't cause oscillation.
#
# Phase S.3 follow-up #2 (dead-zone fix): the shared-dead-zone logic
# caused tilt to monotonically drift to max (+15 on MeetUp). When pan
# needed to move (|ox| > dead_zone), we proceeded to compute BOTH pan
# and tilt deltas — and for a seated user whose head sits slightly
# above frame centre (|oy| ≈ 0.05-0.10), the tilt delta consistently
# rounded to +1. Every pan nudge dragged tilt up by a unit; over ~30 s
# tilt was pinned at the rail. Fix: pan and tilt skip their nudge
# independently based on their own offset. Also widened the zone from
# 0.12 to 0.15 since the S.1 retune made the camera chase small jitter
# ("we keep moving with the camera" — user quote). For wider-range PTZ
# cameras (Rally Bar etc.) these values still compute sensible nudges.
_DEAD_ZONE = 0.15
_DELTA_FRACTION = 0.70
_DAMPING = 0.50

# Zoom step as a fraction of the zoom range per `zoom("in"|"out")` call.
# 0.25 picks a lens jump big enough the user notices on the first
# command, but small enough a second "zoom in" still has somewhere to go.
_ZOOM_STEP_FRACTION = 0.25

# Phase U.3 follow-up — soft cap on user-driven zoom-in. MeetUp's raw
# zoom range is [100, 500], but past ~200 the subject fills the frame
# so aggressively that the pose-bbox clips at the edges and a gentle
# head turn pushes the user out of view. Live session user feedback:
# "300 is too much zoom in." Cap at 200 so `[[CAM:zoom_in]]` stops
# advancing once it hits that ceiling; `zoom_reset` still returns to
# `zr.min` (widest) unchanged. Applies only to the in-session user
# experience — the hardware range is untouched so Logi Tune etc. can
# still zoom further if the user opens those tools outside Abide.
_ZOOM_USER_MAX = 200


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class PTZController:
    """Wraps duvc-ctl with graceful degradation.

    One instance per Session. Discovers a camera-control-capable device
    at construction time, caches whichever PTZ axes it exposes, and
    exposes `available`, `nudge_to_bbox(bbox)` (pan/tilt), `zoom(direction)`
    (optical zoom), and `center()`.

    Any axis not supported by the hardware no-ops cleanly on call;
    `available` reflects "at least one axis is driveable" so callers
    can cheaply skip dispatch when nothing is wired up.
    """

    def __init__(self) -> None:
        self._device = None
        self._pan_range = None
        self._tilt_range = None
        self._zoom_range = None
        self._init()
        # Phase S.3 follow-up: center the camera at session start, not
        # just at session end. Without this, a session inherits whatever
        # pan/tilt/zoom the previous session (or Logi Tune, or anyone
        # else) left on the device — observed in live testing as tilt
        # stuck at +15 (max up) across multiple sessions, with our small
        # nudges unable to bring it back. Safe on zoom-only cameras
        # thanks to the axis-aware guards in center().
        if self.available:
            self.center()

    def _init(self) -> None:
        if not _DUVC_AVAILABLE:
            log.info(
                "[PTZ] duvc-ctl unavailable at import (%s) — PTZ disabled",
                _DUVC_IMPORT_ERROR or "reason unknown",
            )
            return
        if sys.platform != "win32":
            log.info("[PTZ] non-Windows platform (%s) — PTZ disabled", sys.platform)
            return

        # Version + device inventory. Verbose on purpose: silent PTZ
        # disables are easy to miss and the feature only matters if we
        # can see why it's off when it is.
        version = getattr(_duvc, "__version__", "unknown")
        log.info("[PTZ] duvc-ctl version: %s", version)

        try:
            devices = _duvc.list_devices()
        except Exception as e:
            log.info("[PTZ] list_devices failed (%s: %s) — PTZ disabled", type(e).__name__, e)
            return
        log.info("[PTZ] list_devices returned %d entries", len(devices) if devices else 0)
        if not devices:
            log.info("[PTZ] no DirectShow cameras found — PTZ disabled")
            return

        # Walk the list and record whichever PTZ axes each device
        # actually exposes. Previously we required BOTH pan AND tilt,
        # which meant MeetUp — zoom-only on this firmware — never got
        # selected. Now: accept any device that exposes at least one
        # of {pan, tilt, zoom}, and let per-method guards no-op on
        # whatever's missing.
        for idx, device in enumerate(devices):
            name = _device_name(device)
            pan_range = self._probe_range(device, _duvc.CamProp.Pan)
            tilt_range = self._probe_range(device, _duvc.CamProp.Tilt)
            zoom_range = self._probe_range(device, _duvc.CamProp.Zoom)
            log.info(
                "[PTZ] device %d: %s | pan=%s tilt=%s zoom=%s",
                idx,
                name,
                self._fmt_range(pan_range),
                self._fmt_range(tilt_range),
                self._fmt_range(zoom_range),
            )
            if pan_range is None and tilt_range is None and zoom_range is None:
                continue
            # First device with any usable axis wins. On a mixed setup
            # (laptop webcam + MeetUp) only MeetUp reports a valid axis
            # range, so this naturally picks the right camera.
            self._device = device
            self._pan_range = pan_range
            self._tilt_range = tilt_range
            self._zoom_range = zoom_range
            axes = [a for a, r in (("pan", pan_range), ("tilt", tilt_range), ("zoom", zoom_range)) if r is not None]
            log.info("[PTZ] initialised on %s — axes: %s", name, ", ".join(axes))
            return

        log.info("[PTZ] no devices reported a usable pan/tilt/zoom axis — PTZ disabled")

    @staticmethod
    def _probe_range(device, prop):
        """Return a duvc `PropRange` for `prop` on `device`, or None
        when the device doesn't support it. In duvc-ctl 2.x,
        `get_camera_property_range` is a module-level function that
        returns a `(ok: bool, PropRange)` tuple; `ok=False` means the
        device doesn't expose that axis (the `PropRange` is garbage
        when ok is False)."""
        try:
            ok, r = _duvc.get_camera_property_range(device, prop)
        except Exception:
            return None
        if not ok:
            return None
        if not hasattr(r, "min") or not hasattr(r, "max"):
            return None
        if r.max <= r.min:
            return None
        return r

    @staticmethod
    def _fmt_range(r) -> str:
        if r is None:
            return "-"
        return "[%s,%s step=%s]" % (r.min, r.max, getattr(r, "step", "?"))

    @property
    def available(self) -> bool:
        return self._device is not None

    @property
    def axes_available(self) -> tuple[str, ...]:
        """Return the axis names this device exposes — subset of
        `("pan", "tilt", "zoom")`, in that stable order. Empty tuple
        when no device was selected (PTZ fully disabled). Used by the
        conversation engine to tell Claude what camera capabilities
        apply to the current session so it doesn't claim motion it
        can't deliver."""
        axes: list[str] = []
        if self._pan_range is not None:
            axes.append("pan")
        if self._tilt_range is not None:
            axes.append("tilt")
        if self._zoom_range is not None:
            axes.append("zoom")
        return tuple(axes)

    def nudge_to_bbox(self, bbox) -> None:
        """Apply a single damped pan/tilt correction so `bbox`'s centre
        drifts toward the frame centre. Sync call — caller should wrap
        in `asyncio.to_thread` to keep the event loop unblocked. Silent
        no-op when pan or tilt isn't supported (MeetUp).
        """
        if not self.available or self._pan_range is None or self._tilt_range is None:
            return
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            return
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        ox = cx - 0.5
        oy = cy - 0.5

        pan_span = self._pan_range.max - self._pan_range.min
        tilt_span = self._tilt_range.max - self._tilt_range.min

        # Axis-independent dead-zone: pan and tilt each skip their own
        # nudge when their own offset is inside the zone. Prevents tilt
        # from drifting to the rail when pan is doing all the work.
        if abs(ox) >= _DEAD_ZONE:
            pan_delta = int(round(ox * pan_span * _DELTA_FRACTION * _DAMPING))
        else:
            pan_delta = 0
        if abs(oy) >= _DEAD_ZONE:
            tilt_delta = int(round(-oy * tilt_span * _DELTA_FRACTION * _DAMPING))
        else:
            tilt_delta = 0

        pan_step = max(1, getattr(self._pan_range, "step", 1) or 1)
        tilt_step = max(1, getattr(self._tilt_range, "step", 1) or 1)
        pan_delta = (pan_delta // pan_step) * pan_step
        tilt_delta = (tilt_delta // tilt_step) * tilt_step

        if pan_delta == 0 and tilt_delta == 0:
            # Log at INFO so a "why didn't the camera move?" debug session
            # can tell us the code path ran but the computed step was
            # smaller than the device's minimum unit. Common on narrow-
            # range devices like MeetUp (pan ±25) when the subject is
            # near frame centre. Without this line the nudge is invisible
            # in both the log and the camera view.
            log.info(
                "[PTZ] nudge skip (step rounds to 0): bbox centre=(%.2f,%.2f) offset=(%.2f,%.2f)",
                cx, cy, ox, oy,
            )
            return

        cur_pan = self._read_property(_duvc.CamProp.Pan, default=0)
        cur_tilt = self._read_property(_duvc.CamProp.Tilt, default=0)
        new_pan = _clamp(cur_pan + pan_delta, self._pan_range.min, self._pan_range.max)
        new_tilt = _clamp(cur_tilt + tilt_delta, self._tilt_range.min, self._tilt_range.max)

        pan_ok = self._apply(_duvc.CamProp.Pan, new_pan)
        tilt_ok = self._apply(_duvc.CamProp.Tilt, new_tilt)
        if pan_ok or tilt_ok:
            log.info(
                "[PTZ] nudge: bbox centre=(%.2f,%.2f) offset=(%.2f,%.2f) "
                "pan %d→%d tilt %d→%d",
                cx, cy, ox, oy, cur_pan, new_pan, cur_tilt, new_tilt,
            )

    def zoom(self, direction: str) -> None:
        """Apply one step of optical zoom. `direction` is one of
        {"in", "out", "reset"}. Silent no-op when zoom isn't exposed.
        Called off-loop (asyncio.to_thread) from Session because the
        underlying DirectShow COM call can take tens of ms."""
        if not self.available or self._zoom_range is None:
            log.info("[PTZ] zoom %s requested but zoom unavailable", direction)
            return
        zr = self._zoom_range
        span = zr.max - zr.min
        if span <= 0:
            return
        step = max(1, int(round(span * _ZOOM_STEP_FRACTION)))

        current = self._read_property(_duvc.CamProp.Zoom, default=zr.min)

        # Hardware max stays zr.max; user-driven zoom_in is soft-capped
        # at _ZOOM_USER_MAX to keep the subject from clipping out of
        # frame. `out` and `reset` ignore the soft cap.
        user_cap = min(zr.max, _ZOOM_USER_MAX)

        if direction == "in":
            target = current + step
        elif direction == "out":
            target = current - step
        elif direction == "reset":
            target = zr.min  # widest / no-zoom on MeetUp
        else:
            log.info("[PTZ] zoom: unknown direction %r", direction)
            return

        if direction == "in":
            target = _clamp(target, zr.min, user_cap)
        else:
            target = _clamp(target, zr.min, zr.max)
        zoom_step = max(1, getattr(zr, "step", 1) or 1)
        target = (target // zoom_step) * zoom_step

        if target == current:
            log.info(
                "[PTZ] zoom %s: already at limit (%d, range=[%d,%d])",
                direction, current, zr.min, zr.max,
            )
            return

        if self._apply(_duvc.CamProp.Zoom, target):
            log.info(
                "[PTZ] zoom %s: %d → %d (range=[%d,%d] step=%d)",
                direction, current, target, zr.min, zr.max, step,
            )

    def center(self) -> None:
        """Return the camera to a neutral pose on session end. Resets
        whichever axes this device supports. Silent no-op otherwise."""
        if not self.available:
            return
        if self._pan_range is not None:
            self._apply(_duvc.CamProp.Pan, _clamp(0, self._pan_range.min, self._pan_range.max))
        if self._tilt_range is not None:
            self._apply(_duvc.CamProp.Tilt, _clamp(0, self._tilt_range.min, self._tilt_range.max))
        if self._zoom_range is not None:
            self._apply(_duvc.CamProp.Zoom, self._zoom_range.min)
        log.info("[PTZ] centred (reset supported axes to neutral)")

    def _read_property(self, prop, default: int) -> int:
        """Return the device's current value for `prop`, or `default`
        when the read fails or the device doesn't support the axis.
        duvc-ctl 2.x: `get_camera_property(device, prop)` is module-
        level and returns `(ok, PropSetting)` where PropSetting has
        `.value` and `.mode`."""
        try:
            ok, setting = _duvc.get_camera_property(self._device, prop)
        except Exception:
            return default
        if not ok:
            return default
        try:
            return int(getattr(setting, "value", default))
        except (TypeError, ValueError):
            return default

    def _apply(self, prop, value: int) -> bool:
        """Write `value` to `prop`. Returns True on success. In 2.x,
        `set_camera_property(device, prop, PropSetting)` takes a
        PropSetting value (value, mode) and returns a bool."""
        try:
            setting = _duvc.PropSetting(int(value), _duvc.CamMode.Manual)
            return bool(_duvc.set_camera_property(self._device, prop, setting))
        except Exception as e:
            log.debug("[PTZ] apply failed (%s: %s)", type(e).__name__, e)
            return False


def _device_name(device) -> str:
    """Best-effort readable label for a duvc Device object."""
    for attr in ("name", "friendly_name"):
        val = getattr(device, attr, None)
        if val:
            return str(val)
    return repr(device)
