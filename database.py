"""SQLite database layer for FacultyHub / Teacher AI Assistant.

Designed to be simple, robust, and safe against the most common local bugs:

* Foreign keys are enabled on every new connection.
* Cascade deletes are declared at the schema level.
* Indexes target the queries actually used by the app.
* Every write helper commits on success and rolls back on failure.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("facultyhub.db")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("DB_PATH_OVERRIDE") or (BASE_DIR / "database" / "teacher.db"))
DB_PATH.parent.mkdir(exist_ok=True)


SCHEMA = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS teacher (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL,
    school TEXT DEFAULT '',
    department TEXT DEFAULT '',
    email TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    roll_number TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    whatsapp_number TEXT DEFAULT '',
    semester TEXT NOT NULL,
    department TEXT NOT NULL,
    section TEXT DEFAULT '',
    email TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    semester TEXT NOT NULL,
    department TEXT NOT NULL,
    section TEXT DEFAULT '',
    class_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT DEFAULT '',
    room TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    topic TEXT DEFAULT '',
    description TEXT DEFAULT '',
    semester TEXT NOT NULL,
    department TEXT NOT NULL,
    section TEXT DEFAULT '',
    deadline TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Active'
);

CREATE TABLE IF NOT EXISTS project_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pending',
    submitted_at TEXT,
    submission_note TEXT DEFAULT '',
    initial_reminder_sent INTEGER NOT NULL DEFAULT 0,
    deadline_reminder_sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, student_id),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(student_id) REFERENCES students(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS exam_papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subject TEXT DEFAULT '',
    semester TEXT DEFAULT '',
    department TEXT DEFAULT '',
    duration INTEGER DEFAULT 90,
    total_marks INTEGER DEFAULT 0,
    instructions TEXT DEFAULT '',
    structure_prompt TEXT DEFAULT '',
    difficulty_json TEXT DEFAULT '{}',
    topic_distribution_json TEXT DEFAULT '{}',
    reference_name TEXT DEFAULT '',
    reference_analysis TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'Draft',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exam_questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER NOT NULL,
    position INTEGER NOT NULL DEFAULT 1,
    section TEXT DEFAULT '',
    question_text TEXT NOT NULL,
    question_type TEXT DEFAULT 'Short Answer',
    topic TEXT DEFAULT '',
    marks INTEGER NOT NULL DEFAULT 1,
    difficulty TEXT DEFAULT 'Medium',
    answer_key TEXT DEFAULT '',
    FOREIGN KEY(paper_id) REFERENCES exam_papers(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS ai_jobs (
    id TEXT PRIMARY KEY,
    job_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    progress TEXT DEFAULT '',
    payload TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_students_sem_dept ON students(semester, department);
CREATE INDEX IF NOT EXISTS idx_students_roll ON students(roll_number);
CREATE INDEX IF NOT EXISTS idx_projects_deadline ON projects(deadline);
CREATE INDEX IF NOT EXISTS idx_projects_sem_dept ON projects(semester, department);
CREATE INDEX IF NOT EXISTS idx_classes_date ON classes(class_date);
CREATE INDEX IF NOT EXISTS idx_classes_date_time ON classes(class_date, start_time);
CREATE INDEX IF NOT EXISTS idx_ps_project ON project_students(project_id);
CREATE INDEX IF NOT EXISTS idx_ps_student ON project_students(student_id);
CREATE INDEX IF NOT EXISTS idx_exam_questions_paper ON exam_questions(paper_id, position);
CREATE INDEX IF NOT EXISTS idx_ai_jobs_status ON ai_jobs(status, created_at);
"""


def get_db() -> sqlite3.Connection:
    """Return a new SQLite connection with safe defaults."""
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


class Transaction:
    """Context manager that wraps a transaction with explicit commit/rollback."""

    def __init__(self) -> None:
        self.conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = get_db()
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
        finally:
            self.conn.close()


def init_db() -> None:
    with Transaction() as db:
        db.executescript(SCHEMA)
        seed_data(db)


def seed_data(db: sqlite3.Connection) -> None:
    if db.execute("SELECT COUNT(*) FROM departments").fetchone()[0] == 0:
        db.executemany(
            "INSERT INTO departments(name) VALUES (?)",
            [("Computer Science",), ("Electronics",), ("Mechanical",), ("Civil",)],
        )

    if db.execute("SELECT COUNT(*) FROM teacher").fetchone()[0] == 0:
        db.execute(
            "INSERT INTO teacher(id,name,school,department,email,created_at) VALUES (1,?,?,?,?,?)",
            (
                "Dr. Anjali Rao",
                "AISAT",
                "Computer Science",
                "teacher@example.com",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    if db.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0:
        names = [
            "Rahul Kumar",
            "Anjali S",
            "Arun P",
            "Meera P",
            "Vishnu R",
            "Fathima K",
            "Nikhil V",
            "Diya Mathew",
            "Akhil Raj",
            "Sneha Nair",
            "Joel Thomas",
            "Neha Menon",
        ]
        rows = []
        for i, n in enumerate(names, 1):
            rows.append(
                (
                    n,
                    f"CS3-{i:02d}",
                    f"+91 90000{i:05d}",
                    f"+9190000{i:05d}",
                    "Semester 3",
                    "Computer Science",
                    "A",
                    "",
                    datetime.now().isoformat(timespec="seconds"),
                )
            )
        db.executemany(
            "INSERT INTO students(name,roll_number,phone,whatsapp_number,semester,department,section,email,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )

    if db.execute("SELECT COUNT(*) FROM classes").fetchone()[0] == 0:
        today = date.today()
        rows = [
            ("Data Structures", "Semester 3", "Computer Science", "A", (today + timedelta(days=0)).isoformat(), "10:00", "11:00", "Lab 2", "Binary Search Trees", "Insertion, deletion and traversal"),
            ("Database Management", "Semester 3", "Computer Science", "A", (today + timedelta(days=0)).isoformat(), "13:00", "14:00", "Room 302", "Normalization and Functional Dependencies", "1NF, 2NF, 3NF and BCNF"),
            ("Computer Networks", "Semester 5", "Computer Science", "B", (today + timedelta(days=1)).isoformat(), "09:00", "10:00", "Room 210", "Transport Layer", "TCP, UDP and flow control"),
            ("Digital Electronics", "Semester 2", "Electronics", "A", (today + timedelta(days=2)).isoformat(), "11:00", "12:00", "Room 105", "Flip-Flops", "SR, JK, D and T flip-flops"),
        ]
        db.executemany(
            "INSERT INTO classes(subject,semester,department,section,class_date,start_time,end_time,room,topic,notes,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [r + (datetime.now().isoformat(timespec="seconds"),) for r in rows],
        )

    if db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
        deadline = (date.today() + timedelta(days=10)).isoformat()
        deadline2 = (date.today() + timedelta(days=4)).isoformat()
        pids = []
        for proj in [
            ("Library Management System", "Build a database-driven library system", "Create schema, UI and CRUD operations.", "Semester 3", "Computer Science", "A", deadline),
            ("Network Traffic Analysis", "Analyze packet captures", "Submit a short report with graphs and findings.", "Semester 5", "Computer Science", "B", deadline2),
        ]:
            cur = db.execute(
                "INSERT INTO projects(name,topic,description,semester,department,section,deadline,created_at,status) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                proj + (datetime.now().isoformat(timespec="seconds"), "Active"),
            )
            pids.append(cur.lastrowid)
        s_ids = [r["id"] for r in db.execute("SELECT id FROM students WHERE semester='Semester 3' ORDER BY id LIMIT 12").fetchall()]
        for sid in s_ids:
            db.execute(
                "INSERT OR IGNORE INTO project_students(project_id,student_id,status) VALUES (?,?,?)",
                (pids[0], sid, "Pending"),
            )
        s_ids2 = [r["id"] for r in db.execute("SELECT id FROM students WHERE semester='Semester 5'").fetchall()]
        for sid in s_ids2:
            db.execute(
                "INSERT OR IGNORE INTO project_students(project_id,student_id,status) VALUES (?,?,?)",
                (pids[1], sid, "Pending"),
            )


def rows_to_dict(rows: Iterable[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def expire_projects() -> int:
    """Move past-deadline projects from Active to History. Returns rows updated."""
    today = date.today().isoformat()
    with Transaction() as db:
        cur = db.execute(
            "UPDATE projects SET status='History' WHERE deadline < ? AND status='Active'",
            (today,),
        )
    return cur.rowcount or 0