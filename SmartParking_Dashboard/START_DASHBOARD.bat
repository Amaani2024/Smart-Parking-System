@echo off
cd /d "%~dp0"
echo Starting dashboard on http://localhost:8001 ... keep this window open.
start "" http://localhost:8001
python -m http.server 8001 || py -m http.server 8001
pause
