"""Free local AI service backed by Ollama.

Design goals for stability on a normal Windows PC:

* No OpenAI dependency. Ollama is the only supported backend.
* All long operations are bounded by a hard timeout and a `num_predict` cap so
  the model can never run away on the CPU.
* JSON parsing is robust against surrounding prose and broken brackets.
* Errors carry enough context for the UI to render a useful message instead
  of a generic "Something went wrong".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("facultyhub.ai")

DEFAULT_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))
DEFAULT_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "1200"))
MAX_PROMPT_CHARS = int(os.getenv("OLLAMA_MAX_PROMPT_CHARS", "12000"))


class AIServiceError(RuntimeError):
    """Raised when the AI service cannot produce a result.

    The `code` attribute lets the UI translate the failure into a useful
    message without exposing internal details to the teacher.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class AIConfig:
    model: str
    base_url: str
    timeout: int
    num_predict: int


class AIService:
    """Thin wrapper around the Ollama HTTP API.

    Every public method returns plain Python data (lists / dicts / strings).
    All failures are converted to :class:`AIServiceError` with a `code` so
    callers (and the UI) can render appropriate text.
    """

    def __init__(self, config: AIConfig | None = None) -> None:
        if config is None:
            config = AIConfig(
                model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b").strip(),
                base_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
                timeout=int(os.getenv("OLLAMA_TIMEOUT", str(DEFAULT_TIMEOUT))),
                num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", str(DEFAULT_NUM_PREDICT))),
            )
        self.config = config

    # ----- public properties -------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.config.base_url and self.config.model)

    @property
    def model(self) -> str:
        return self.config.model

    def status(self) -> dict[str, Any]:
        """Return setup status without ever raising."""
        result: dict[str, Any] = {
            "provider": "Ollama",
            "configured": self.configured,
            "running": False,
            "model": self.config.model,
        }
        if not self.configured:
            result["message"] = "Local AI is not configured. Set AI_PROVIDER=ollama and OLLAMA_MODEL in .env."
            return result
        try:
            r = requests.get(f"{self.config.base_url}/api/tags", timeout=2)
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
            result["running"] = True
            installed = self.config.model in models or any(
                x.split(":")[0] == self.config.model.split(":")[0] for x in models
            )
            result["model_installed"] = installed
            result["message"] = (
                "Ollama and the selected model are ready."
                if installed
                else f"Ollama is running, but {self.config.model} is not downloaded yet."
            )
        except requests.RequestException:
            result["message"] = "Ollama is not running. Start Ollama and try again."
        except Exception as exc:  # pragma: no cover - very defensive
            log.exception("Unexpected error while checking Ollama status")
            result["message"] = f"Could not contact Ollama: {exc}"
        return result

    # ----- low level ---------------------------------------------------------
    def _call(self, prompt: str, system: str = "", json_mode: bool = False) -> str:
        """Make a single Ollama generate call."""
        if not self.configured:
            raise AIServiceError("not_configured", "Local AI is not configured.")
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS]
            log.warning("Prompt truncated to %d chars", MAX_PROMPT_CHARS)

        payload: dict[str, Any] = {
            "model": self.config.model,
            "system": system or "You are an expert academic assistant for college teachers. Return concise, accurate, teacher-ready content.",
            "prompt": prompt,
            "stream": False,
            "keep_alive": "5m",
            "options": {
                "temperature": 0.2,
                "num_predict": self.config.num_predict,
                "num_ctx": 4096,
            },
        }
        if json_mode:
            payload["format"] = "json"

        try:
            response = requests.post(
                f"{self.config.base_url}/api/generate",
                json=payload,
                timeout=self.config.timeout,
            )
        except requests.Timeout:
            raise AIServiceError(
                "timeout",
                f"AI generation timed out after {self.config.timeout} seconds. Try a shorter prompt.",
            )
        except requests.ConnectionError:
            raise AIServiceError(
                "connection",
                "Could not connect to Ollama. Make sure Ollama is running on "
                f"{self.config.base_url}.",
            )
        except requests.RequestException as exc:
            raise AIServiceError("network", f"Network error contacting Ollama: {exc}")

        if not response.ok:
            text = response.text[:500] if response.text else ""
            log.error("Ollama HTTP %s: %s", response.status_code, text)
            if response.status_code == 404:
                raise AIServiceError(
                    "model_missing",
                    f"The model '{self.config.model}' is not installed. Run: ollama pull {self.config.model}",
                )
            raise AIServiceError("ollama_error", f"Ollama request failed ({response.status_code}): {text}")

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise AIServiceError("bad_response", f"Ollama returned non-JSON envelope: {exc}")

        text = str(data.get("response", "")).strip()
        if not text:
            raise AIServiceError("empty", "Ollama returned an empty response. Please retry.")
        return text

    def _json(self, prompt: str, system: str) -> Any:
        """Ask for JSON and try hard to recover from messy output."""
        text = self._call(prompt, system=system, json_mode=True)
        cleaned = text.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Try to find the first balanced JSON object/array.
        starts = [cleaned.find("["), cleaned.find("{")]
        starts = [s for s in starts if s >= 0]
        if starts:
            start = min(starts)
            for end_char in ("]", "}"):
                end = cleaned.rfind(end_char)
                if end > start:
                    snippet = cleaned[start : end + 1]
                    try:
                        return json.loads(snippet)
                    except json.JSONDecodeError:
                        continue
        raise AIServiceError(
            "bad_json",
            "The local AI returned invalid JSON. Please retry or shorten your prompt.",
        )

    # ----- higher level helpers used by the API ------------------------------
    def generate_questions(
        self,
        topic: str,
        marks: int,
        difficulty: str,
        qtype: str,
        count: int = 4,
        context: str = "",
    ) -> list[dict]:
        if not topic:
            raise AIServiceError("invalid_input", "Enter a topic before generating questions.")
        count = max(1, min(10, int(count or 4)))
        marks = max(1, int(marks or 2))
        prompt = (
            f"Generate {count} distinct university/college exam questions.\n"
            f"Topic: {topic}\n"
            f"Marks per question: {marks}\n"
            f"Difficulty: {difficulty}\n"
            f"Question type: {qtype}\n"
            f"Additional context: {context or 'None'}\n"
            'Return ONLY a JSON object exactly like: '
            '{"questions":[{"question":"...","marks":0,"difficulty":"Easy|Medium|Hard","type":"...","topic":"...","answer_key":"..."}]}\n'
            "Use the requested marks and difficulty. Do not copy a known exam question."
        )
        data = self._json(prompt, "You create high-quality original college exam questions. Respect the requested marks, type, topic and difficulty. Never copy a known exam question.")
        if isinstance(data, dict):
            return data.get("questions", []) or []
        if isinstance(data, list):
            return data
        return []

    def analyze_previous_paper(self, filename: str, file_bytes: bytes) -> dict[str, Any]:
        # Free local setup intentionally avoids OCR/document-parsing dependencies.
        size = len(file_bytes)
        return {
            "summary": f"Uploaded reference: {filename} ({size // 1024} KB). Treated as structural reference only.",
            "sections": ["Reference paper uploaded"],
            "question_types_found": [],
            "mark_distribution_notes": "Preserve the visible section and mark pattern without copying questions.",
            "difficulty_estimate_notes": "Preserve overall difficulty balance without copying original questions.",
            "topics_covered": [],
            "style_notes": "Match section numbering, concise academic wording and instruction style; do not reuse original questions.",
        }

    def generate_exam_paper(self, data: dict[str, Any]) -> dict[str, Any]:
        topics = data.get("topics") or []
        if not topics:
            raise AIServiceError("invalid_input", "Add at least one topic before generating the paper.")
        prompt = (
            "Create a complete university/college exam paper.\n"
            f"Department: {data.get('department')}\n"
            f"Semester: {data.get('semester')}\n"
            f"Subject: {data.get('subject')}\n"
            f"Topics: {', '.join(topics)}\n"
            f"Total marks: {data.get('total_marks')}\n"
            f"Duration: {data.get('duration')} minutes\n"
            f"Structure instructions: {data.get('structure_prompt')}\n"
            f"Overall paper difficulty: {data.get('difficulty', 'Medium')}\n"
            f"Topic distribution: {json.dumps(data.get('topic_distribution', {}), ensure_ascii=False)}\n"
            f"Previous-paper analysis: {json.dumps(data.get('reference_analysis', {}), ensure_ascii=False)}\n"
            "Return ONLY a JSON object with keys: instructions, questions.\n"
            "questions must be an array of objects with: section, question_text, question_type, topic, marks, difficulty, answer_key, position.\n"
            "Set the difficulty of the entire paper to the requested overall level. Do not vary difficulty across sections unless the question types demand it. Follow the requested structure as closely as mathematically possible."
        )
        return self._json(
            prompt,
            "You are an experienced university examiner and assessment designer. Produce original, balanced, syllabus-aligned exam questions. Do not copy previous paper questions.",
        )

    def generate_summary(self, classes: list[dict]) -> str:
        if not classes:
            return "No classes are scheduled for today."
        prompt = (
            "Create a concise teacher-facing summary of today's classes and topics. "
            "Mention the subject, time, and key teaching topic for each class.\n"
            f"{json.dumps(classes, ensure_ascii=False)}"
        )
        return self._call(prompt, json_mode=False)