@echo off
REM Build VoltCheck into a single portable exe: dist\VoltCheck.exe
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed ^
    --name VoltCheck ^
    --collect-all customtkinter ^
    --collect-all matplotlib ^
    battery_monitor.py
echo.
echo Done: dist\VoltCheck.exe
pause
