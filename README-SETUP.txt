Abide Companion — Setup
========================

--------------------------------------------------------------
 4 steps to run Abide
--------------------------------------------------------------

 1. Plug in the Logitech MeetUp (or any USB webcam + mic).
    Aim the MeetUp at where you'll sit — its wide 120-degree
    lens gives plenty of room, but framing is better if you
    start pointed at your usual seat.

 2. Double-click the launcher for your system:
        start.bat          (Windows)
        start.command      (macOS)
        start.sh           (Linux — run from terminal: bash start.sh)

    IMPORTANT for macOS users: the file is named start.command,
    not start.sh, because Finder treats .sh as text and will open
    it in TextEdit. Use start.command to double-click.

    The first time you double-click start.command on macOS, you
    may see a dialog: "start.command can't be opened because it
    is from an unidentified developer." This is Apple's standard
    warning for any unsigned third-party app. To bypass it once:

        Right-click start.command -> Open -> click Open on
        the confirmation dialog that appears.

    After that, every future launch is a normal silent double-
    click. You only do this once per download.

    If Python 3.12 is already installed, the launcher uses it.
    If not, the launcher will install it for you automatically:

      - On Windows: a per-user Python 3.12 install runs silently
        in the background (no admin prompt, no UAC, no PATH
        checkbox to click). Adds about 30 seconds to first-run.

      - On macOS: the official Apple Installer opens. Click
        through the Continue buttons and enter your admin
        password once when asked. The launcher then continues.

      - On Linux: if Python 3.12 isn't already installed, the
        launcher prints one command for your distribution and
        exits. Run it, then double-click start.sh again.

    After Python is available, the first run creates a virtual
    environment and installs dependencies — 3-5 minutes on a
    typical broadband connection. Later runs start in seconds.

 3. Your browser will open automatically at
        http://localhost:8000
    Click the small gear icon at the bottom-right and paste in
    your API keys for Groq, Anthropic, and OpenAI. They are
    saved in your browser and never leave this machine.

 4. Click the green "Start" button. Allow access to your
    microphone and camera when the browser asks. Speak to
    Abide — it will listen, watch, and respond by voice.

    On Windows with a Logitech MeetUp you can also say
    "zoom in", "zoom out", or "reset the zoom" to move the
    camera's optical zoom. Pan/tilt availability is detected
    per session and can vary by firmware/session conditions;
    Abide will only claim capabilities that probe as available.

--------------------------------------------------------------
 During a session
--------------------------------------------------------------

 While Abide is running you will see two tabs at the top of
 the conversation panel:

    Conversation  — shows what you and Abide are saying
    Diary         — shows a live timestamped log of everything:
                    your speech, Abide's replies, AND what the
                    camera sees (vision observations, fall alerts)

 You can switch between tabs at any time. Click Hide to
 collapse the panel, Show to bring it back.

 If you step out of the camera's view for about 11 seconds,
 Abide will gently check in — "I can't see you right now,
 are you still there?". It's designed to notice when someone
 leaves the frame unexpectedly.

--------------------------------------------------------------
 When you click Stop
--------------------------------------------------------------

 A full-screen Session Summary will appear with:

    - Duration       — how long the session lasted
    - Transcript     — complete conversation with timestamps
    - Activity Log   — every vision observation with timestamps
    - Right / Wrong  — a Claude-generated review of what Abide
                       interpreted correctly vs. what you corrected

 Click the X or anywhere outside the summary to close it.
 The Diary tab remains available with all entries even after
 the summary is dismissed. Entries are cleared when you
 click Start again.

--------------------------------------------------------------
 How to stop the server
--------------------------------------------------------------

 Close the separate "Abide Companion" console window that
 appeared when you launched. Or press Ctrl+C inside it.

--------------------------------------------------------------
 Why Python instead of Docker
--------------------------------------------------------------

 Earlier versions shipped as a Docker container. We switched
 to native Python because controlling the camera (optical zoom
 on the MeetUp) requires direct access to Windows DirectShow,
 which Docker Desktop on Windows cannot reach without admin-
 level USB configuration steps that violate the brief's
 "double-click, zero terminal commands" first-run rule.
 Native Python keeps the launcher a single double-click and
 has a smaller installer (Python 3.12 is ~30 MB vs Docker
 Desktop's ~500 MB plus WSL2 setup). See DESIGN-NOTES.md
 (sections "The deployment pivot" and "The PTZ saga") for
 the full story.

--------------------------------------------------------------
 Troubleshooting
--------------------------------------------------------------

 "python is not recognised" or similar on Windows.
    The launcher tries to auto-install Python 3.12 on first
    run. If that failed (for example: no internet), install
    Python manually from https://www.python.org/downloads/
    and re-run start.bat. You do NOT need to tick "Add to
    PATH" — start.bat will find a per-user install at
    %LocalAppData%\Programs\Python\Python312\ automatically.

 Auto-install of Python failed.
    Check your internet connection and re-run start.bat —
    the download is attempted fresh each time Python is not
    found. If you are behind a proxy or firewall that blocks
    python.org, download the installer manually from
    https://www.python.org/downloads/ and run it yourself;
    start.bat will pick up the resulting install on the
    next launch.

 macOS: "start.command can't be opened" / "unidentified
 developer" on first launch.
    This is Apple's Gatekeeper warning for any unsigned
    third-party app. Right-click start.command in Finder,
    choose Open, then click Open on the confirmation
    dialog. Subsequent launches are silent. You only do
    this once. Bypassing Gatekeeper permanently requires
    a paid Apple Developer ID, which Abide doesn't have
    (and which wouldn't make the first-run any smoother
    than the right-click-Open workflow above).

 macOS: double-clicking start.sh opens TextEdit instead
 of running it.
    You clicked the wrong file. macOS Finder treats .sh
    as plain text. On macOS, double-click start.command
    instead — Finder recognizes .command and opens it
    in Terminal.

 The browser did not open.
    Open it yourself and go to http://localhost:8000

 "Port 8000 is already in use."
    Another program is using that port. Close it, or on
    Windows run:
        netstat -ano | findstr :8000
    and close the program that owns the process ID shown.

 Microphone or camera not working.
    Click the lock icon in the address bar and allow
    microphone and camera for http://localhost:8000.
    On Windows, also check that another app (Windows Camera,
    Logi Tune, Teams, Zoom) isn't holding the camera —
    only one process can open it at a time.

 Zoom commands do nothing.
    This works on Windows with a Logitech MeetUp only. Check
    the "Abide Companion" console window for a line like:
        [PTZ] initialised on Logitech MeetUp — axes: zoom
    If instead you see:
        [PTZ] duvc-ctl unavailable at import (...)
    you are running with the wrong Python. Use start.bat —
    do not run uvicorn manually unless you have activated
    the project's .venv first.
    If you see:
        [PTZ] no devices reported a usable pan/tilt/zoom axis
    your camera does not expose zoom over DirectShow; the
    rest of Abide still works normally.

 Camera is not panning or tilting.
    Pan/tilt on MeetUp is conditional and may probe differently
    across sessions/firmware. Check the startup axes line in
    the "Abide Companion" console. If it reports only `zoom`,
    Abide will use zoom-only behavior in that session.
    If it reports `pan`, `tilt`, and/or `zoom`, those axes can
    be used. Any on-device reframing motion you see may still
    be Logitech RightSight digital cropping.

 Abide says it is having trouble reaching its services.
    Check that your API keys in the gear-icon settings
    panel are correct, and that your internet is working.

 Something else weird.
    Check the "Abide Companion" console window for error
    messages — the full server log is visible there.

--------------------------------------------------------------
