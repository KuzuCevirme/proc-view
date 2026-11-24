@echo off
python -m pip show psutil >nul 2>&1 || python -m pip install -q -r "%~dp0requirements.txt"
start "" pythonw "%~dp0procview.py"
