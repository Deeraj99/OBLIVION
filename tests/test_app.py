"""Automated tests for FacultyHub.

These tests run the Flask app in-process against a temporary SQLite
database. Ollama is mocked so the suite does not require a live model
on the test machine.

Run with:
    python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _setup_test_environment():
    tmp = Path(tempfile.mkdtemp(prefix="facultyhub_test_"))
    db_path = tmp / "teacher.db"
    os.environ["DB_PATH_OVERRIDE"] = str(db_path)
    os.environ["OLLAMA_URL"] = "http://127.0.0.1:1"  # never reached; we mock
    os.environ["OLLAMA_MODEL"] = "test-model"
    os.environ["OLLAMA_TIMEOUT"] = "5"
    return tmp


_setup_test_environment()

from database import DB_PATH, init_db  # noqa: E402
from services.ai_service import AIService  # noqa: E402
from services.job_manager import JobManager  # noqa: E402


# Make sure the DB is fresh before any app modules load.
if DB_PATH.exists():
    DB_PATH.unlink()
init_db()


class _AppFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patches = []
        # Mock the AI service so tests do not hit Ollama.
        cls.ai_patcher = mock.patch("app.ai", autospec=True)
        cls.mock_ai = cls.ai_patcher.start()
        cls.mock_ai.configured = True
        cls.mock_ai.status.return_value = {
            "provider": "Ollama",
            "configured": True,
            "running": True,
            "model": "test-model",
            "model_installed": True,
            "message": "ready",
        }
        cls.mock_ai.generate_questions.return_value = [
            {"question": "What is a binary tree?", "marks": 5, "difficulty": "Medium", "type": "Short Answer", "topic": "Trees", "answer_key": "A tree where each node has up to two children."},
            {"question": "Define BST.", "marks": 5, "difficulty": "Medium", "type": "Short Answer", "topic": "Trees", "answer_key": "Binary search tree."},
        ]
        cls.mock_ai.generate_exam_paper.return_value = {
            "instructions": "Answer all questions.",
            "questions": [
                {"section": "A", "question_text": "Define a tree.", "question_type": "Short Answer", "topic": "Trees", "marks": 5, "difficulty": "Medium", "answer_key": "Recursive structure.", "position": 1},
                {"section": "A", "question_text": "What is a BST?", "question_type": "Short Answer", "topic": "Trees", "marks": 5, "difficulty": "Medium", "answer_key": "Ordered tree.", "position": 2},
            ],
        }
        cls.mock_ai.generate_summary.return_value = "Today's teaching will cover trees."
        cls.mock_ai.analyze_previous_paper.return_value = {"summary": "ref", "style_notes": "n/a"}

        # Import app after patching
        from app import app as flask_app
        cls.flask_app = flask_app
        cls.client = flask_app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.ai_patcher.stop()


class SmokeTests(_AppFixture):
    def test_index_loads(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"FacultyHub", r.data)

    def test_favicon(self):
        r = self.client.get("/favicon.ico")
        self.assertEqual(r.status_code, 200)

    def test_meta(self):
        r = self.client.get("/api/meta")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("Computer Science", body["departments"])
        self.assertTrue(body["semesters"])

    def test_dashboard(self):
        r = self.client.get("/api/dashboard")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertIn("stats", body)
        self.assertIn("today_classes", body)


class StudentCrudTests(_AppFixture):
    def setUp(self):
        r = self.client.post("/api/students", json={
            "name": "Test Student", "semester": "Semester 3", "department": "Computer Science",
            "roll_number": "T-001", "phone": "123", "whatsapp_number": "123", "section": "A", "email": "t@x.com",
        })
        self.assertEqual(r.status_code, 200, r.data)
        self.student_id = r.get_json()["id"]

    def test_create_student(self):
        r = self.client.get("/api/students")
        self.assertEqual(r.status_code, 200)
        names = [s["name"] for s in r.get_json()]
        self.assertIn("Test Student", names)

    def test_create_student_missing_required(self):
        r = self.client.post("/api/students", json={"name": ""})
        self.assertEqual(r.status_code, 400)

    def test_edit_student(self):
        r = self.client.put(f"/api/students/{self.student_id}", json={
            "name": "Renamed Student", "semester": "Semester 5", "department": "Computer Science",
            "roll_number": "T-001", "phone": "", "whatsapp_number": "", "section": "B", "email": "",
        })
        self.assertEqual(r.status_code, 200)
        r = self.client.get("/api/students")
        names = [s["name"] for s in r.get_json()]
        self.assertIn("Renamed Student", names)

    def test_search_and_filter(self):
        r = self.client.get("/api/students?semester=Semester%205")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), 1)
        r = self.client.get("/api/students?q=Renamed")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), 1)

    def test_delete_student(self):
        r = self.client.delete(f"/api/students/{self.student_id}")
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/students/{self.student_id}")
        self.assertEqual(r.status_code, 404)


class ProjectCrudTests(_AppFixture):
    def setUp(self):
        # Need at least one student
        self.client.post("/api/students", json={
            "name": "Project Student", "semester": "Semester 3", "department": "Computer Science",
            "roll_number": "P-001", "phone": "", "whatsapp_number": "", "section": "A", "email": "",
        })
        s = self.client.get("/api/students").get_json()
        self.student_id = s[0]["id"]

    def test_create_project_minimum(self):
        r = self.client.post("/api/projects", json={
            "name": "Mini Project", "semester": "Semester 3", "department": "Computer Science",
            "deadline": "2099-01-01",
        })
        self.assertEqual(r.status_code, 200, r.data)
        pid = r.get_json()["id"]
        r = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["project"]["name"], "Mini Project")

    def test_create_project_missing_required(self):
        # empty payload
        r = self.client.post("/api/projects", json={})
        self.assertEqual(r.status_code, 400)
        # missing deadline
        r = self.client.post("/api/projects", json={
            "name": "X", "semester": "Semester 3", "department": "Computer Science",
        })
        self.assertEqual(r.status_code, 400)
        # empty deadline string
        r = self.client.post("/api/projects", json={
            "name": "X", "semester": "Semester 3", "department": "Computer Science", "deadline": "   ",
        })
        self.assertEqual(r.status_code, 400)

    def test_create_project_with_students(self):
        r = self.client.post("/api/projects", json={
            "name": "With Students", "semester": "Semester 3", "department": "Computer Science",
            "deadline": "2099-01-01", "student_ids": [self.student_id],
        })
        self.assertEqual(r.status_code, 200, r.data)
        pid = r.get_json()["id"]
        r = self.client.get(f"/api/projects/{pid}")
        body = r.get_json()
        self.assertEqual(len(body["students"]), 1)
        self.assertEqual(body["students"][0]["student_id"], self.student_id)

    def test_edit_project_changes_students(self):
        r = self.client.post("/api/projects", json={
            "name": "Editable", "semester": "Semester 3", "department": "Computer Science",
            "deadline": "2099-01-01", "student_ids": [self.student_id],
        })
        pid = r.get_json()["id"]
        r = self.client.put(f"/api/projects/{pid}", json={
            "name": "Editable", "topic": "updated", "description": "", "semester": "Semester 3",
            "department": "Computer Science", "section": "", "deadline": "2099-01-02", "status": "Active",
            "student_ids": [],
        })
        self.assertEqual(r.status_code, 200, r.data)
        r = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(len(r.get_json()["students"]), 0)

    def test_delete_project(self):
        r = self.client.post("/api/projects", json={
            "name": "ToDelete", "semester": "Semester 3", "department": "Computer Science",
            "deadline": "2099-01-01",
        })
        pid = r.get_json()["id"]
        r = self.client.delete(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 404)
        r = self.client.delete(f"/api/projects/{pid}")
        self.assertEqual(r.status_code, 404)


class ClassCrudTests(_AppFixture):
    def test_class_crud(self):
        r = self.client.post("/api/classes", json={
            "subject": "Math", "semester": "Semester 3", "department": "Computer Science",
            "class_date": "2099-01-01", "start_time": "09:00", "end_time": "10:00",
            "section": "", "room": "", "topic": "", "notes": "",
        })
        self.assertEqual(r.status_code, 200, r.data)
        cid = r.get_json()["id"]
        r = self.client.put(f"/api/classes/{cid}", json={
            "subject": "Math 2", "semester": "Semester 3", "department": "Computer Science",
            "class_date": "2099-01-01", "start_time": "09:00", "end_time": "10:00",
            "section": "", "room": "", "topic": "", "notes": "",
        })
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/classes/{cid}")
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/classes/{cid}")
        self.assertEqual(r.status_code, 404)


class ExamCrudTests(_AppFixture):
    def test_exam_crud(self):
        payload = {
            "name": "Mid Term", "subject": "DS", "semester": "Semester 3",
            "department": "Computer Science", "duration": 90, "total_marks": 50,
            "instructions": "All", "structure_prompt": "n/a",
            "difficulty": "Medium", "topic_distribution": {},
            "questions": [{"section": "A", "question_text": "Q1", "question_type": "Short Answer", "topic": "DS", "marks": 5, "difficulty": "Medium", "answer_key": "a", "position": 1}],
        }
        r = self.client.post("/api/exams", json=payload)
        self.assertEqual(r.status_code, 200, r.data)
        pid = r.get_json()["id"]
        r = self.client.get(f"/api/exams/{pid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["questions"]), 1)
        r = self.client.put(f"/api/exams/{pid}", json=payload)
        self.assertEqual(r.status_code, 200)
        # PDF
        r = self.client.get(f"/api/exams/{pid}/pdf")
        self.assertEqual(r.status_code, 200)
        # delete
        r = self.client.delete(f"/api/exams/{pid}")
        self.assertEqual(r.status_code, 200)
        r = self.client.delete(f"/api/exams/{pid}")
        self.assertEqual(r.status_code, 404)


class JobManagerTests(unittest.TestCase):
    def test_job_lifecycle(self):
        from database import get_db
        mgr = JobManager()
        ran = {}

        def work(job):
            ran["ok"] = True
            return {"hello": "world"}

        snap = mgr.start("test", work)
        self.assertIn(snap["status"], ("queued", "running"))
        # poll up to a few seconds for completion
        import time
        for _ in range(20):
            j = mgr.get(snap["id"])
            if j and j["status"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(ran.get("ok"), True)
        j = mgr.get(snap["id"])
        self.assertEqual(j["status"], "completed")
        self.assertEqual(j["result"], {"hello": "world"})

    def test_duplicate_job_returns_existing(self):
        mgr = JobManager()
        counter = {"n": 0}

        def work(job):
            counter["n"] += 1
            import time; time.sleep(0.2)
            return {"ok": True}

        s1 = mgr.start("dup", work, dedupe_key="k")
        s2 = mgr.start("dup", work, dedupe_key="k")
        # Second call should be flagged as duplicate.
        self.assertTrue(s2.get("duplicate") or s2["id"] == s1["id"])


class AIEndpointTests(_AppFixture):
    def test_ai_status(self):
        r = self.client.get("/api/ai/status")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["configured"])

    def test_ai_questions_returns_job_id(self):
        r = self.client.post("/api/ai/questions", json={"topic": "Trees", "marks": 5, "difficulty": "Medium", "question_type": "Short Answer", "count": 2})
        self.assertEqual(r.status_code, 200, r.data)
        body = r.get_json()
        self.assertIn("id", body)
        self.assertIn(body["status"], ("queued", "running", "completed"))

    def test_ai_questions_missing_topic(self):
        r = self.client.post("/api/ai/questions", json={"topic": ""})
        self.assertEqual(r.status_code, 400)

    def test_ai_exam_returns_job_id(self):
        r = self.client.post("/api/ai/exam", json={
            "name": "X", "subject": "DS", "semester": "Semester 3", "department": "Computer Science",
            "duration": 90, "total_marks": 50, "topics": ["Trees"], "difficulty": "Medium",
        })
        self.assertEqual(r.status_code, 200, r.data)

    def test_ai_exam_missing_topic(self):
        r = self.client.post("/api/ai/exam", json={"topics": []})
        self.assertEqual(r.status_code, 400)


class ValidationTests(_AppFixture):
    def test_project_validation_error_includes_field(self):
        r = self.client.post("/api/projects", json={"semester": "Semester 3"})
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn("error", body)
        # The error message should hint which field is missing.
        self.assertTrue(any(field in body["error"] for field in ("name", "department", "deadline")))

    def test_exam_validation(self):
        r = self.client.post("/api/exams", json={"name": ""})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()