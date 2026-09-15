@echo off
REM Build a standalone StreamGrab.exe — run this from inside the
REM streamgrab folder (the one containing pyproject.toml and this file).

echo Installing/upgrading build dependencies...
pip install --upgrade pyinstaller PyQt6 yt-dlp

echo.
echo Building StreamGrab.exe (this can take a minute or two)...
pyinstaller --name StreamGrab --windowed --onefile --clean StreamGrab.spec

echo.
echo Done. Your executable is at: dist\StreamGrab.exe
echo Note: ffmpeg must be installed separately for audio-only/MP3 downloads to work.
pause
