Abide Companion — Setup
========================

--------------------------------------------------------------
 5 steps to run Abide
--------------------------------------------------------------

 1. Install Python 3.12 or newer (one-time, ~30 MB).
      https://www.python.org/downloads/
      During install, CHECK the box "Add python.exe to PATH".

 2. Plug in the Logitech MeetUp (or any USB webcam + mic).
    Aim the MeetUp at where you'll sit — its wide 120-degree
    lens gives plenty of room, but framing is better if you
    start pointed at your usual seat.

 3. Double-click:
        start.bat          (Windows)
      or
        start.sh           (Mac / Linux)

    The first run creates a Python virtual environment and
    installs dependencies — 3-5 minutes on a typical broadband
    connection. Later runs start in a few seconds.

 4. Your browser will open automatically at
        http://localhost:8000
    Click the small gear icon at the bottom-right and paste in
    your API keys for Groq, Anthropic, and OpenAI. They are
    saved in your browser and never leave this machine.

 5. Click the green "Start" button. Allow access to your
    microphone and camera when the browser asks. Speak to
    Abide — it will listen, watch, and respond by voice.

    On Windows with a Logitech MeetUp you can also say
    "zoom in", "zoom out", or "reset the zoom" to move the
    camera's optical zoom. MeetUp does not have mechanical
    pan or tilt (its 120-degree lens is fixed); if you ask
    for pan or tilt, Abide will say so honestly.

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

 If you step out of the camera's view for about 10 seconds,
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
    During Python install, check "Add python.exe to PATH".
    If you missed it, reinstall from python.org.

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
    MeetUp has a fixed 120-degree lens and no pan/tilt motors
    over its UVC interface — only optical zoom. Any on-device
    framing motion you see is Logitech RightSight digital
    cropping inside the camera, not Abide. If you need
    mechanical pan/tilt, a Logitech Rally Bar or Rally Bar
    Mini exposes those axes; the same Abide code path will
    drive them automatically.

 Abide says it is having trouble reaching its services.
    Check that your API keys in the gear-icon settings
    panel are correct, and that your internet is working.

 Something else weird.
    Check the "Abide Companion" console window for error
    messages — the full server log is visible there.

--------------------------------------------------------------
