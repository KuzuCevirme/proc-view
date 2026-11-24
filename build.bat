@echo off
rem Builds dist\ProcView.exe (single file, no Python needed on the target PC)
cd /d "%~dp0"
python -m pip install -q -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name ProcView ^
  --icon assets\procview.ico --add-data "web;web" procview.py || goto :err
echo.
echo Done: dist\ProcView.exe
exit /b 0
:err
echo Build failed.
exit /b 1
