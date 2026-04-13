Abide Companion — Setup
========================

--------------------------------------------------------------
 5 steps to run Abide
--------------------------------------------------------------

 1. Install Docker Desktop (one download, one installer).
      https://www.docker.com/products/docker-desktop/

 2. Open Docker Desktop and wait until the whale icon in the
    tray (top-right on Mac, bottom-right on Windows) is steady.

 3. Double-click:
        start.bat          (Windows)
      or
        start.sh           (Mac / Linux)

    The first run builds the container and takes 3-5 minutes.
    Later runs start in a few seconds.

 4. Your browser will open automatically at
        http://localhost:8000
    Click the small gear icon at the bottom-right and paste in
    your API keys for Groq, Anthropic, and OpenAI. They are
    saved in your browser and never leave this machine.

 5. Click the green "Start" button. Allow access to your
    microphone and camera when the browser asks. Speak to
    Abide — it will listen, see you, and respond by voice.

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

 Close the browser tab, then either quit Docker Desktop
 OR open the Abide folder and run:

        docker compose down

--------------------------------------------------------------
 Troubleshooting
--------------------------------------------------------------

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

 Abide says it is having trouble reaching its services.
    Check that your API keys in the gear-icon settings
    panel are correct, and that your internet is working.

 Something else weird.
    Open the Abide folder and run:
        docker compose logs -f
    The logs will show what went wrong.

--------------------------------------------------------------
