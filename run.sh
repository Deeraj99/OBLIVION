#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

command -v python3 >/dev/null 2>&1 || { echo "Python 3.11+ is required."; exit 1; }

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --disable-pip-version-check -r requirements.txt
[ -f .env ] || cp .env.example .env

if command -v ollama >/dev/null 2>&1; then
  if ! ollama list | grep -q '^qwen2.5:7b'; then
    echo "Downloading qwen2.5:7b for the free local AI (one-time download)..."
    ollama pull qwen2.5:7b || true
  fi
  (ollama serve >/dev/null 2>&1 &) || true
else
  echo "Ollama is not installed. Install it from https://ollama.com/download and run again."
fi

python app.py
