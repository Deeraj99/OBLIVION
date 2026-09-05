"""FacultyHub / Teacher AI Assistant — Flask backend.

Major changes from the original "fixed_project" version:

* Dashboard's manual "Generate Summary" button has been removed. The summary
  is requested as soon as today's classes load and is rendered through the
  shared background job manager; the dashboard remains usable while Ollama
  thinks.
* All long-running AI calls (``/api/ai/exam``, ``/api/ai/questions``,
  ``/api/ai/summary``, ``/api/ai/analyze-paper``) run through the job
  manager. The HTTP endpoints immediately return a ``job_id``; the UI polls
  ``/api/ai/jobs/<id>`` to pick up the result.
* Project creation now validates required fields but tolerates ``None`` /
  whitespace strings coming from the front-end, and project updates correctly
  refresh ``project_students`` so editing does not silently deselect people.
* Clear logging, structured error messages and proper HTTP status codes.
* No OpenAI dependencies anywhere.
"""

from __future__ import annotations

import json as _json
import logging
import os
import socket
import threading
import time
import webbrowser
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, render_template, request, send_file
from dotenv import load_dotenv
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from database import (
    Transaction,
    expire_projects as db_expire_projects,
    get_db,
    init_db,
    rows_to_dict,
)
from services.ai_service import AIService, AIServiceError
from services.job_manager import get_job_manager
from services.whatsapp_service import WhatsAppService

# ---------------------------------------------------------------------------
# Logging setup (also used by the services via named loggers).
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("facultyhub.app")

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "teacher-ai-local-secret")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB uploads
ai = AIService()
wa = WhatsAppService()
jobs = get_job_manager()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def local_ip() -> str:
    """Best-effort discovery of the LAN-facing IPv4 address."""
    candidates: list[str] = []
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM):
            addr = item[4][0]
            if addr and not addr.startswith("127."):
                candidates.append(addr)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1, no real traffic
        addr = s.getsockname()[0]
        s.close()
        if addr and not addr.startswith("127."):
            candidates.insert(0, addr)
    except Exception:
        pass
    return candidates[0] if candidates else "127.0.0.1"


def json_rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def require_fields(payload: dict, fields: list[str]) -> str | None:
    """Return the first missing-field label, or None if every field is set."""
    for f in fields:
        value = payload.get(f)
        if value is None:
            return f
        if isinstance(value, str) and not value.strip():
            return f
    return None


def _normalize(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------------------
# Background workers (non-blocking)
# ---------------------------------------------------------------------------
def reminder_worker() -> None:
    while True:
        try:
            db_expire_projects()
            now = datetime.now()
            today = now.date()
            tomorrow = today + timedelta(days=1)
            with Transaction() as db:
                rows = db.execute(
                    """
                    SELECT ps.id, ps.project_id, ps.student_id, ps.deadline_reminder_sent,
                           p.name, p.topic, p.deadline,
                           s.name AS student_name, s.whatsapp_number
                    FROM project_students ps
                    JOIN projects p ON p.id = ps.project_id
                    JOIN students s ON s.id = ps.student_id
                    WHERE p.status = 'Active'
                    """
                ).fetchall()
                for r in rows:
                    deadline = date.fromisoformat(r["deadline"])
                    if deadline == tomorrow and not r["deadline_reminder_sent"]:
                        msg = (
                            f"Reminder, {r['student_name']}:\n\n"
                            f"Your project '{r['name']}' is due tomorrow.\n"
                            f"Topic: {r['topic'] or 'See project details'}\n"
                            f"Deadline: {r['deadline']}\n\n"
                            "Please submit on time."
                        )
                        result = wa.send_text(r["whatsapp_number"], msg)
                        if result.get("sent") or result.get("dry_run"):
                            db.execute(
                                "UPDATE project_students SET deadline_reminder_sent=1 WHERE id=?",
                                (r["id"],),
                            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Reminder worker error: %s", exc)
        time.sleep(1800)


def cleanup_worker() -> None:
    while True:
        try:
            removed = jobs.cleanup()
            if removed:
                log.info("Cleaned %d finished AI jobs", removed)
        except Exception as exc:  # noqa: BLE001
            log.warning("Cleanup worker error: %s", exc)
        time.sleep(300)


# ---------------------------------------------------------------------------
# Routes — dashboard / settings / metadata
# ---------------------------------------------------------------------------
@app.get("/favicon.ico")
def favicon():
    # Inline 1x1 transparent PNG so the browser stops logging missing-icon errors.
    import base64
    data = base64.b64decode(
        b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    from flask import Response
    return Response(data, mimetype="image/png")


@app.get("/")
def index():
    db_expire_projects()
    return render_template(
        "app.html",
        local_ip=local_ip(),
        ai_configured=ai.configured,
        whatsapp_configured=wa.configured,
    )


@app.get("/api/dashboard")
def dashboard():
    db_expire_projects()
    today_iso = date.today().isoformat()
    with get_db() as db:
        classes = db.execute(
            "SELECT * FROM classes WHERE class_date>=? ORDER BY class_date, start_time LIMIT 8",
            (today_iso,),
        ).fetchall()
        today_classes = db.execute(
            "SELECT * FROM classes WHERE class_date=? ORDER BY start_time",
            (today_iso,),
        ).fetchall()
        active = db.execute(
            "SELECT * FROM projects WHERE status='Active' ORDER BY deadline LIMIT 8"
        ).fetchall()
        stats = {
            "classes": db.execute(
                "SELECT COUNT(*) FROM classes WHERE class_date>=?", (today_iso,)
            ).fetchone()[0],
            "projects": db.execute(
                "SELECT COUNT(*) FROM projects WHERE status='Active'"
            ).fetchone()[0],
            "pending": db.execute(
                "SELECT COUNT(*) FROM project_students ps JOIN projects p ON p.id=ps.project_id "
                "WHERE p.status='Active' AND ps.status='Pending'"
            ).fetchone()[0],
            "due_week": db.execute(
                "SELECT COUNT(*) FROM projects WHERE status='Active' "
                "AND deadline BETWEEN ? AND ?",
                (today_iso, (date.today() + timedelta(days=7)).isoformat()),
            ).fetchone()[0],
        }
    return jsonify(
        {
            "classes": json_rows(classes),
            "today_classes": json_rows(today_classes),
            "active_projects": json_rows(active),
            "stats": stats,
        }
    )


@app.get("/api/settings")
def settings_get():
    with get_db() as db:
        teacher = dict(db.execute("SELECT * FROM teacher WHERE id=1").fetchone())
    return jsonify(
        teacher=teacher,
        ai_configured=ai.configured,
        whatsapp_configured=wa.configured,
        local_ip=local_ip(),
    )


@app.put("/api/settings")
def settings_update():
    d = request.get_json() or {}
    fields = ["name", "school", "department", "email"]
    values = [_normalize(d.get(f)) for f in fields]
    if not values[0]:
        return jsonify(error="Teacher name is required."), 400
    with Transaction() as db:
        db.execute(
            "UPDATE teacher SET " + ",".join(f"{x}=?" for x in fields) + " WHERE id=1",
            values,
        )
    return jsonify(ok=True)


@app.get("/api/meta")
def meta():
    with get_db() as db:
        deps = db.execute("SELECT name FROM departments ORDER BY name").fetchall()
        sems = db.execute(
            "SELECT DISTINCT semester FROM students "
            "UNION SELECT DISTINCT semester FROM classes "
            "UNION SELECT DISTINCT semester FROM projects "
            "ORDER BY semester"
        ).fetchall()
    return jsonify(
        departments=[d["name"] for d in deps],
        semesters=[s["semester"] for s in sems if s["semester"]],
    )


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------
@app.get("/api/classes")
def classes_list():
    with get_db() as db:
        rows = db.execute("SELECT * FROM classes ORDER BY class_date, start_time").fetchall()
    return jsonify(json_rows(rows))


@app.post("/api/classes")
def classes_create():
    d = request.get_json() or {}
    missing = require_fields(d, ["subject", "semester", "department", "class_date", "start_time"])
    if missing:
        return (
            jsonify(error="Subject, semester, department, date and start time are required."),
            400,
        )
    fields = ["subject", "semester", "department", "section", "class_date", "start_time",
              "end_time", "room", "topic", "notes"]
    with Transaction() as db:
        cur = db.execute(
            "INSERT INTO classes(" + ",".join(fields) + ",created_at) VALUES ("
            + ",".join("?" for _ in fields) + ",?)",
            [_normalize(d.get(f)) for f in fields] + [datetime.now().isoformat(timespec="seconds")],
        )
    return jsonify(id=cur.lastrowid)


@app.put("/api/classes/<int:cid>")
def classes_update(cid):
    d = request.get_json() or {}
    fields = ["subject", "semester", "department", "section", "class_date", "start_time",
              "end_time", "room", "topic", "notes"]
    with Transaction() as db:
        db.execute(
            "UPDATE classes SET " + ",".join(f"{x}=?" for x in fields) + " WHERE id=?",
            [_normalize(d.get(f)) for f in fields] + [cid],
        )
    return jsonify(ok=True)


@app.delete("/api/classes/<int:cid>")
def classes_delete(cid):
    with Transaction() as db:
        cur = db.execute("DELETE FROM classes WHERE id=?", (cid,))
    if cur.rowcount == 0:
        return jsonify(error="Class not found."), 404
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------
@app.get("/api/students")
def students_list():
    q = request.args.get("q", "").strip()
    sem = request.args.get("semester", "").strip()
    dept = request.args.get("department", "").strip()
    section = request.args.get("section", "").strip()
    sql = "SELECT * FROM students WHERE 1=1"
    params: list = []
    if q:
        sql += " AND (name LIKE ? OR roll_number LIKE ? OR phone LIKE ? OR whatsapp_number LIKE ?)"
        params += [f"%{q}%"] * 4
    if sem:
        sql += " AND semester=?"
        params.append(sem)
    if dept:
        sql += " AND department=?"
        params.append(dept)
    if section:
        sql += " AND section=?"
        params.append(section)
    sql += " ORDER BY semester, department, section, name"
    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return jsonify(json_rows(rows))


@app.post("/api/students")
def students_create():
    d = request.get_json() or {}
    missing = require_fields(d, ["name", "semester", "department"])
    if missing:
        return jsonify(error="Name, semester and department are required."), 400
    fields = ["name", "roll_number", "phone", "whatsapp_number", "semester", "department", "section", "email"]
    with Transaction() as db:
        cur = db.execute(
            "INSERT INTO students(" + ",".join(fields) + ",created_at) VALUES ("
            + ",".join("?" for _ in fields) + ",?)",
            [_normalize(d.get(f)) for f in fields] + [datetime.now().isoformat(timespec="seconds")],
        )
    return jsonify(id=cur.lastrowid)


@app.put("/api/students/<int:sid>")
def students_update(sid):
    d = request.get_json() or {}
    missing = require_fields(d, ["name", "semester", "department"])
    if missing:
        return jsonify(error="Name, semester and department are required."), 400
    fields = ["name", "roll_number", "phone", "whatsapp_number", "semester", "department", "section", "email"]
    with Transaction() as db:
        db.execute(
            "UPDATE students SET " + ",".join(f"{x}=?" for x in fields) + " WHERE id=?",
            [_normalize(d.get(f)) for f in fields] + [sid],
        )
    return jsonify(ok=True)


@app.delete("/api/students/<int:sid>")
def students_delete(sid):
    with Transaction() as db:
        cur = db.execute("DELETE FROM students WHERE id=?", (sid,))
    if cur.rowcount == 0:
        return jsonify(error="Student not found."), 404
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def _send_initial_messages(pid: int) -> None:
    with Transaction() as db:
        rs = db.execute(
            """
            SELECT ps.id, s.name, s.whatsapp_number,
                   p.name AS project_name, p.topic, p.deadline
            FROM project_students ps
            JOIN students s ON s.id = ps.student_id
            JOIN projects p ON p.id = ps.project_id
            WHERE ps.project_id=?
            """,
            (pid,),
        ).fetchall()
        for r in rs:
            msg = (
                f"Hello {r['name']},\n\n"
                f"Your new project is:\nProject: {r['project_name']}\n"
                f"Topic: {r['topic'] or 'See project details'}\n"
                f"Deadline: {r['deadline']}\n\n"
                "Please make sure to submit before the deadline."
            )
            result = wa.send_text(r["whatsapp_number"], msg)
            if result.get("sent") or result.get("dry_run"):
                db.execute(
                    "UPDATE project_students SET initial_reminder_sent=1 WHERE id=?",
                    (r["id"],),
                )


def _project_list_payload():
    q = request.args.get("q", "").strip()
    sem = request.args.get("semester", "").strip()
    dept = request.args.get("department", "").strip()
    status = request.args.get("status", "").strip()
    sql = (
        "SELECT p.*, "
        "COUNT(ps.id) AS student_count, "
        "SUM(CASE WHEN ps.status='Submitted' THEN 1 ELSE 0 END) AS submitted_count, "
        "SUM(CASE WHEN ps.status='Late Submission' THEN 1 ELSE 0 END) AS late_count, "
        "SUM(CASE WHEN ps.status='Pending' THEN 1 ELSE 0 END) AS pending_count "
        "FROM projects p LEFT JOIN project_students ps ON ps.project_id=p.id WHERE 1=1"
    )
    params: list = []
    if q:
        sql += (
            " AND (p.name LIKE ? OR p.topic LIKE ? OR p.semester LIKE ? OR p.department LIKE ? "
            "OR EXISTS(SELECT 1 FROM project_students x JOIN students sx ON sx.id=x.student_id "
            "WHERE x.project_id=p.id AND (sx.name LIKE ? OR sx.roll_number LIKE ?)))"
        )
        params += [f"%{q}%"] * 6
    if sem:
        sql += " AND p.semester=?"
        params.append(sem)
    if dept:
        sql += " AND p.department=?"
        params.append(dept)
    if status:
        sql += " AND p.status=?"
        params.append(status)
    sql += " GROUP BY p.id ORDER BY p.status ASC, p.deadline ASC"
    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return json_rows(rows)


@app.get("/api/projects")
def projects_list():
    db_expire_projects()
    return jsonify(_project_list_payload())


@app.get("/api/projects/<int:pid>")
def project_detail(pid):
    with get_db() as db:
        p = db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify(error="Project not found."), 404
        students = db.execute(
            """
            SELECT ps.*, s.name, s.roll_number, s.whatsapp_number, s.semester, s.department
            FROM project_students ps JOIN students s ON s.id=ps.student_id
            WHERE ps.project_id=? ORDER BY s.name
            """,
            (pid,),
        ).fetchall()
    return jsonify(project=dict(p), students=json_rows(students))


@app.post("/api/projects")
def project_create():
    d = request.get_json() or {}
    missing = require_fields(d, ["name", "semester", "department", "deadline"])
    if missing:
        log.info("Project create rejected: missing %r in payload=%s", missing, d)
        return (
            jsonify(
                error="Project name, semester, department and deadline are required. "
                f"Missing field: {missing}."
            ),
            400,
        )
    student_ids = d.get("student_ids") or []
    with Transaction() as db:
        cur = db.execute(
            """
            INSERT INTO projects(name,topic,description,semester,department,section,deadline,created_at,status)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                _normalize(d["name"]),
                _normalize(d.get("topic")),
                _normalize(d.get("description")),
                _normalize(d["semester"]),
                _normalize(d["department"]),
                _normalize(d.get("section")),
                _normalize(d["deadline"]),
                datetime.now().isoformat(timespec="seconds"),
                "Active",
            ),
        )
        pid = cur.lastrowid
        for sid in student_ids:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO project_students(project_id,student_id,status) VALUES (?,?,?)",
                    (pid, int(sid), "Pending"),
                )
            except (TypeError, ValueError):
                continue
    # Send WhatsApp messages after the transaction has committed so partial
    # failures don't roll back the project itself.
    try:
        _send_initial_messages(pid)
    except Exception as exc:  # noqa: BLE001
        log.warning("WhatsApp initial reminder failed for project %s: %s", pid, exc)
    return jsonify(id=pid)


@app.put("/api/projects/<int:pid>")
def project_update(pid):
    d = request.get_json() or {}
    fields = ["name", "topic", "description", "semester", "department", "section", "deadline", "status"]
    values = [_normalize(d.get(f)) for f in fields]
    student_ids = d.get("student_ids")
    with Transaction() as db:
        exists = db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone()
        if not exists:
            return jsonify(error="Project not found."), 404
        db.execute(
            "UPDATE projects SET " + ",".join(f"{x}=?" for x in fields) + " WHERE id=?",
            values + [pid],
        )
        if student_ids is not None:
            db.execute("DELETE FROM project_students WHERE project_id=?", (pid,))
            for sid in student_ids:
                try:
                    db.execute(
                        "INSERT INTO project_students(project_id,student_id,status) VALUES (?,?, 'Pending')",
                        (pid, int(sid)),
                    )
                except (TypeError, ValueError):
                    continue
    return jsonify(ok=True)


@app.post("/api/projects/<int:pid>/submission")
def project_submission(pid):
    d = request.get_json() or {}
    sid = d.get("student_id")
    if sid is None:
        return jsonify(error="student_id is required."), 400
    try:
        sid_int = int(sid)
    except (TypeError, ValueError):
        return jsonify(error="student_id must be an integer."), 400
    status = _normalize(d.get("status")) or "Pending"
    if status not in {"Pending", "Submitted", "Late Submission"}:
        return jsonify(error="Invalid submission status."), 400
    submitted_at = (
        _normalize(d.get("submitted_at"))
        or (datetime.now().isoformat(timespec="minutes") if status != "Pending" else "")
    )
    with Transaction() as db:
        exists = db.execute(
            "SELECT 1 FROM project_students WHERE project_id=? AND student_id=?",
            (pid, sid_int),
        ).fetchone()
        if not exists:
            return jsonify(error="This student is not assigned to the project."), 404
        db.execute(
            "UPDATE project_students SET status=?, submitted_at=?, submission_note=? "
            "WHERE project_id=? AND student_id=?",
            (status, submitted_at or None, _normalize(d.get("note")), pid, sid_int),
        )
    return jsonify(ok=True)


@app.delete("/api/projects/<int:pid>")
def project_delete(pid):
    with Transaction() as db:
        cur = db.execute("DELETE FROM projects WHERE id=?", (pid,))
    if cur.rowcount == 0:
        return jsonify(error="Project not found."), 404
    return jsonify(ok=True)


@app.post("/api/semester/<path:semester>/delete")
def semester_delete(semester):
    with Transaction() as db:
        db.execute(
            "DELETE FROM project_students WHERE project_id IN (SELECT id FROM projects WHERE semester=?)",
            (semester,),
        )
        db.execute("DELETE FROM projects WHERE semester=?", (semester,))
        db.execute("DELETE FROM students WHERE semester=?", (semester,))
        db.execute("DELETE FROM classes WHERE semester=?", (semester,))
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Exams
# ---------------------------------------------------------------------------
def normalize_exam_difficulty(value):
    if isinstance(value, dict):
        try:
            e, m, h = (float(value.get("easy", 0)), float(value.get("medium", 0)), float(value.get("hard", 0)))
            return "Easy" if e >= m and e >= h else ("Medium" if m >= h else "Hard")
        except Exception:
            return "Medium"
    value = str(value or "Medium")
    return value if value in {"Easy", "Medium", "Hard"} else "Medium"


def _exam_row_to_dict(row):
    d = dict(row)
    d["difficulty"] = normalize_exam_difficulty(_json.loads(d.pop("difficulty_json") or '"Medium"'))
    d["topic_distribution"] = _json.loads(d.pop("topic_distribution_json") or "{}")
    return d


@app.get("/api/exams")
def exams_list():
    with get_db() as db:
        rows = db.execute("SELECT * FROM exam_papers ORDER BY updated_at DESC").fetchall()
    return jsonify([_exam_row_to_dict(r) for r in rows])


@app.post("/api/exams")
def exam_create():
    d = request.get_json() or {}
    missing = require_fields(d, ["name"])
    if missing:
        return jsonify(error="Exam paper name is required."), 400
    now = datetime.now().isoformat(timespec="seconds")
    fields = [
        "name", "subject", "semester", "department", "duration", "total_marks",
        "instructions", "structure_prompt", "reference_name", "reference_analysis",
    ]
    vals = [d.get(x, "") for x in fields]
    with Transaction() as db:
        cur = db.execute(
            """
            INSERT INTO exam_papers(
                name, subject, semester, department, duration, total_marks,
                instructions, structure_prompt, reference_name, reference_analysis,
                status, created_at, updated_at, difficulty_json, topic_distribution_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            vals
            + [
                "Draft",
                now,
                now,
                _json.dumps(normalize_exam_difficulty(d.get("difficulty", "Medium"))),
                _json.dumps(d.get("topic_distribution", {})),
            ],
        )
        pid = cur.lastrowid
        for q in d.get("questions", []) or []:
            db.execute(
                """
                INSERT INTO exam_questions(paper_id,position,section,question_text,question_type,
                                            topic,marks,difficulty,answer_key)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    pid,
                    int(q.get("position", 1)),
                    q.get("section", ""),
                    q.get("question_text", ""),
                    q.get("question_type", "Short Answer"),
                    q.get("topic", ""),
                    int(q.get("marks", 1) or 1),
                    q.get("difficulty", "Medium"),
                    q.get("answer_key", ""),
                ),
            )
    return jsonify(id=pid)


@app.get("/api/exams/<int:pid>")
def exam_detail(pid):
    with get_db() as db:
        p = db.execute("SELECT * FROM exam_papers WHERE id=?", (pid,)).fetchone()
        if not p:
            return jsonify(error="Exam not found."), 404
        qs = db.execute(
            "SELECT * FROM exam_questions WHERE paper_id=? ORDER BY position", (pid,)
        ).fetchall()
    out = _exam_row_to_dict(p)
    out["questions"] = json_rows(qs)
    return jsonify(out)


@app.put("/api/exams/<int:pid>")
def exam_update(pid):
    d = request.get_json() or {}
    now = datetime.now().isoformat(timespec="seconds")
    fields = [
        "name", "subject", "semester", "department", "duration", "total_marks",
        "instructions", "structure_prompt", "reference_name", "reference_analysis", "status",
    ]
    with Transaction() as db:
        exists = db.execute("SELECT 1 FROM exam_papers WHERE id=?", (pid,)).fetchone()
        if not exists:
            return jsonify(error="Exam not found."), 404
        db.execute(
            "UPDATE exam_papers SET "
            + ",".join(f"{x}=?" for x in fields)
            + ",difficulty_json=?,topic_distribution_json=?,updated_at=? WHERE id=?",
            [d.get(x, "") for x in fields]
            + [
                _json.dumps(normalize_exam_difficulty(d.get("difficulty", "Medium"))),
                _json.dumps(d.get("topic_distribution", {})),
                now,
                pid,
            ],
        )
        db.execute("DELETE FROM exam_questions WHERE paper_id=?", (pid,))
        for q in d.get("questions", []) or []:
            db.execute(
                """
                INSERT INTO exam_questions(paper_id,position,section,question_text,question_type,
                                            topic,marks,difficulty,answer_key)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    pid,
                    int(q.get("position", 1)),
                    q.get("section", ""),
                    q.get("question_text", ""),
                    q.get("question_type", "Short Answer"),
                    q.get("topic", ""),
                    int(q.get("marks", 1) or 1),
                    q.get("difficulty", "Medium"),
                    q.get("answer_key", ""),
                ),
            )
    return jsonify(ok=True)


@app.delete("/api/exams/<int:pid>")
def exam_delete(pid):
    with Transaction() as db:
        cur = db.execute("DELETE FROM exam_papers WHERE id=?", (pid,))
    if cur.rowcount == 0:
        return jsonify(error="Exam not found."), 404
    return jsonify(ok=True)


@app.get("/api/exams/<int:pid>/pdf")
def exam_pdf(pid):
    with get_db() as db:
        p = db.execute("SELECT * FROM exam_papers WHERE id=?", (pid,)).fetchone()
        qs = db.execute(
            "SELECT * FROM exam_questions WHERE paper_id=? ORDER BY position", (pid,)
        ).fetchall()
    if not p:
        abort(404)
    p = dict(p)
    qs = json_rows(qs)
    out = BASE_DIR / "database" / f"exam_{pid}.pdf"
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "title",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        alignment=TA_CENTER,
        fontSize=15,
        leading=18,
    )
    center = ParagraphStyle(
        "center", parent=styles["BodyText"], alignment=TA_CENTER, fontSize=9
    )
    qstyle = ParagraphStyle(
        "q", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=7
    )
    doc = SimpleDocTemplate(
        str(out),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    flow = [
        Paragraph(str(p["name"]), title),
        Paragraph(f"{p['department']} · {p['semester']} · {p['subject']}", center),
        Paragraph(
            f"Duration: {p['duration']} minutes · Maximum Marks: {p['total_marks']}",
            center,
        ),
        Spacer(1, 8),
    ]
    if p["instructions"]:
        flow += [
            Paragraph("<b>Instructions</b>", styles["Heading4"]),
            Paragraph(str(p["instructions"]), styles["BodyText"]),
            Spacer(1, 6),
        ]
    current_section = None
    for q in qs:
        if q.get("section") != current_section and q.get("section"):
            current_section = q["section"]
            flow.append(Paragraph(f"<b>{current_section}</b>", styles["Heading3"]))
        text = (
            f"<b>Q{q['position']}.</b> {q['question_text']} "
            f"<font size='8'>[{q['marks']} marks]</font>"
        )
        flow.append(KeepTogether([Paragraph(text, qstyle)]))
    doc.build(flow)
    return send_file(
        out,
        as_attachment=True,
        download_name=f"{str(p['name']).replace(' ', '_')}.pdf",
        mimetype="application/pdf",
    )


# ---------------------------------------------------------------------------
# AI endpoints (job-manager based)
# ---------------------------------------------------------------------------
@app.get("/api/ai/status")
def ai_status():
    return jsonify(ai.status())


def _job_payload_dict(snapshot):
    return {
        "id": snapshot.get("id"),
        "status": snapshot.get("status"),
        "kind": snapshot.get("kind"),
        "progress": snapshot.get("progress"),
        "error": snapshot.get("error"),
        "result": snapshot.get("result"),
        "duplicate": snapshot.get("duplicate", False),
    }


def _ai_error(code: str, message: str, status_code: int = 400):
    return jsonify(error=message, code=code), status_code


@app.post("/api/ai/questions")
def ai_questions_create():
    d = request.get_json() or {}
    topic = _normalize(d.get("topic"))
    if not topic:
        return _ai_error("invalid_input", "Enter a topic before generating questions.")

    dedupe = f"questions:{topic}|{d.get('marks')}|{d.get('difficulty')}|{d.get('question_type')}|{d.get('count')}"

    def work(job):
        job.update_progress("Generating questions with local AI…")
        try:
            return {"questions": ai.generate_questions(
                topic,
                d.get("marks", 2),
                d.get("difficulty", "Medium"),
                d.get("question_type", "Short Answer"),
                d.get("count", 4),
                d.get("context", ""),
            )}
        except AIServiceError as exc:
            raise RuntimeError(f"{exc.code}: {exc}") from exc

    snapshot = jobs.start("questions", work, dedupe_key=dedupe, payload=d)
    return jsonify(_job_payload_dict(snapshot))


@app.post("/api/ai/exam")
def ai_exam_create():
    d = request.get_json() or {}
    topics = d.get("topics") or []
    if not topics:
        return _ai_error("invalid_input", "Add at least one topic before generating the paper.")
    missing = require_fields(d, ["name", "subject", "semester", "department", "duration", "total_marks"])
    if missing:
        return _ai_error(
            "invalid_input",
            f"Paper setup is incomplete: '{missing}' is required.",
        )
    dedupe = f"exam:{d.get('semester')}|{d.get('department')}|{','.join(sorted(topics))}|{d.get('total_marks')}|{d.get('difficulty')}"

    def work(job):
        job.update_progress("Generating exam paper with local AI…")
        try:
            return ai.generate_exam_paper(d)
        except AIServiceError as exc:
            raise RuntimeError(f"{exc.code}: {exc}") from exc

    snapshot = jobs.start("exam", work, dedupe_key=dedupe, payload=d)
    return jsonify(_job_payload_dict(snapshot))


@app.post("/api/ai/summary")
def ai_summary_create():
    d = request.get_json() or {}
    classes = d.get("classes") or []

    def work(job):
        job.update_progress("Drafting today's teaching summary…")
        return {"summary": ai.generate_summary(classes)}

    snapshot = jobs.start("summary", work, dedupe_key="summary:".join([c.get("id", "") for c in classes]) or "summary:daily")
    return jsonify(_job_payload_dict(snapshot))


@app.post("/api/ai/analyze-paper")
def ai_analyze_paper():
    f = request.files.get("file")
    if not f:
        return _ai_error("invalid_input", "No file uploaded.")
    file_bytes = f.read()
    if not file_bytes:
        return _ai_error("invalid_input", "Uploaded file is empty.")

    def work(job):
        job.update_progress("Recording reference paper details…")
        return ai.analyze_previous_paper(f.filename or "reference.pdf", file_bytes)

    snapshot = jobs.start("analyze", work, dedupe_key=f"analyze:{f.filename}-{len(file_bytes)}")
    return jsonify(_job_payload_dict(snapshot))


@app.get("/api/ai/jobs/<job_id>")
def ai_job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Job not found."), 404
    return jsonify(_job_payload_dict(job))


@app.post("/api/ai/jobs/<job_id>/cancel")
def ai_job_cancel(job_id):
    job = jobs.cancel(job_id)
    return jsonify(_job_payload_dict(job))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def start_browser(url: str) -> None:
    time.sleep(1.4)
    try:
        webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open browser automatically: %s", exc)


if __name__ == "__main__":
    init_db()
    db_expire_projects()
    ip = local_ip()
    port = int(os.getenv("PORT", "5000"))
    print("\nTeacher AI Assistant")
    print(f"Local:   http://127.0.0.1:{port}")
    print(f"Wi-Fi:   http://{ip}:{port}")
    print("AI:      " + ("ready" if ai.configured else "not configured (set OLLAMA_MODEL in .env)"))
    print("WhatsApp:" + (" enabled" if wa.configured else " disabled (dry-run)"))
    print()
    threading.Thread(target=reminder_worker, daemon=True, name="reminder").start()
    threading.Thread(target=cleanup_worker, daemon=True, name="cleanup").start()
    threading.Thread(target=start_browser, args=(f"http://127.0.0.1:{port}",), daemon=True, name="browser").start()
    # use_reloader=False avoids spawning two Flask workers when run from run.bat
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)