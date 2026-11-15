@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
echo.
echo Open http://127.0.0.1:5000 in Chrome or Edge
python app.py
pause
