@echo off
setlocal
cd /d "%~dp0"

echo ================================================
echo       Teacher AI Assistant - FREE LOCAL AI
echo ================================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11+ and try again.
  echo Download: https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist .venv (
  echo Creating Python virtual environment...
  py -m venv .venv
  if errorlevel 1 goto :error
)
call .venv\Scripts\activate
if errorlevel 1 goto :error

python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

if not exist .env copy .env.example .env >nul

where ollama >nul 2>nul
if errorlevel 1 (
  echo.
  echo Ollama is not installed.
  echo The website can still start, but AI generation will not work yet.
  echo Install Ollama from: https://ollama.com/download/windows
  echo Then run this file again and it will download the model automatically.
) else (
  echo.
  echo Checking local AI model...
  ollama list | findstr /I "qwen2.5:7b" >nul 2>nul
  if errorlevel 1 (
    echo Downloading qwen2.5:7b for the free local AI. This is a one-time download.
    ollama pull qwen2.5:7b
    if errorlevel 1 (
      echo.
      echo Model download failed. You can run: ollama pull qwen2.5:7b
    )
  )
  start "" /min ollama serve >nul 2>nul
)

echo.
echo.
echo Checking Windows Firewall for LAN access...
netsh advfirewall firewall add rule name="Teacher AI Assistant (TCP 5000)" dir=in action=allow protocol=TCP localport=5000 profile=private >nul 2>nul
if errorlevel 1 (
  echo Firewall rule could not be added automatically.
  echo If another device cannot connect, run run.bat as Administrator once,
  echo or allow Python through Windows Defender Firewall on Private networks.
) else (
  echo LAN access rule is ready on TCP port 5000.
)

echo Starting Teacher AI Assistant...
python app.py
if errorlevel 1 goto :error
pause
exit /b 0

:error
echo.
echo ================================================
echo Something went wrong while starting the app.
echo ================================================
pause
exit /b 1
