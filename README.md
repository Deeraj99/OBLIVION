# FacultyHub — Teacher AI Assistant

A free, local, single-user teacher management web app backed by **Ollama** (no OpenAI key, no per-request AI charges). It manages students, classes, projects, exam papers and uses local AI to:

- draft a "today's teaching summary" on the dashboard
- suggest exam questions by topic
- generate full exam papers from topics, structure and difficulty

The dashboard "Generate" button has been removed; the AI summary now runs automatically as a background job.

## Quick start (Windows)

```
run.bat
```

The script creates a virtualenv, installs dependencies, downloads `qwen2.5:7b` the first time, opens the firewall for LAN access, and starts the app at http://127.0.0.1:5000.

To open the app from another device on the same Wi-Fi, use the URL printed at startup (e.g. `http://192.168.1.42:5000`).

## Requirements

- Python 3.11+
- Ollama (download from https://ollama.com/download/windows)
- ~5 GB disk for the model

`requirements.txt`:

```
Flask>=3.1,<4
python-dotenv>=1.0,<2
requests>=2.32,<3
reportlab>=4.2,<5
```

## Running the test suite

```
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Tests use a temporary SQLite database and mock Ollama, so they do not require the model to be downloaded.

## Background jobs

All AI calls run through a thread-safe job manager (`services/job_manager.py`).

- Jobs are stored in the `ai_jobs` SQLite table.
- Concurrent jobs are capped (default 2) and duplicates are deduplicated by a key.
- States: `queued`, `running`, `completed`, `failed`, `cancelled`.
- The UI polls `/api/ai/jobs/<id>` every 1.5 s and never blocks the browser while Ollama is thinking.
- Completed jobs are cleaned up after 30 minutes.