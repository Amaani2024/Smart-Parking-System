
ZENVA WEB VOICE ASSISTANT - FINAL
================================

This version runs fully through the browser.

The microphone is handled by Chrome/Edge using the browser Web Speech API.
The Python server does not use speech_recognition, PyAudio or win32com.

1. INSTALL
----------

Open Command Prompt in this folder:

    python -m pip install -r requirements.txt

2. RUN
------

    python app.py

3. OPEN
-------

Use Google Chrome or Microsoft Edge:

    http://127.0.0.1:5000

Do not double-click index.html.
Do not open it as file:///...
Open it through the Python Flask server.

4. ALLOW MICROPHONE
-------------------

When you press "Start speaking", the browser asks for microphone permission.

Select Allow.

If you blocked it:

Chrome / Edge:
- Click the lock or tune icon beside the address.
- Open Site permissions.
- Set Microphone to Allow.
- Refresh the page.

Windows:
- Settings
- Privacy & security
- Microphone
- Turn on Microphone access
- Turn on Let desktop apps access your microphone

5. IMPORTANT
------------

Browser speech recognition normally needs internet access.
Use Chrome or Edge. Firefox may not support this voice input API.

6. FIREBASE LOGIC
-----------------

IR sensor readings control physical occupancy:
IR1 = A-01
IR2 = A-02
IR3 = A-03
IR4 = A-04

An active booking reserves only a physically empty slot.
Old paid, exited, completed or cancelled bookings do not overwrite live IR data.
