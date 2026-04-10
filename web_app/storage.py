from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from threading import Lock
from typing import Dict, List

from .models import (
    AccessRequestStatus,
    ActivityEvent,
    ClassAssignment,
    ClassMembership,
    ClassMembershipRequest,
    Classroom,
    DialogNotificationState,
    Question,
    QuestionTiming,
    QuestionType,
    TestResult,
    TheoryMaterial,
    TopicAccessRequest,
    User,
    UserRole,
)

_WRITE_LOCK = Lock()

_LEGACY_MIGRATION_MARKER = ".migrated_from_legacy"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _migrate_legacy_data(project_root: Path, base: Path) -> None:
    marker = base / _LEGACY_MIGRATION_MARKER
    if marker.exists():
        return

    with _WRITE_LOCK:
        if marker.exists():
            return

        theory_dest = base / "theory_materials"
        theory_dest.mkdir(parents=True, exist_ok=True)

        legacy = project_root / "Новая папка"
        if legacy.is_dir():
            legacy_theory = legacy / "theory_materials"
            if legacy_theory.is_dir():
                for p in legacy_theory.iterdir():
                    dest = theory_dest / p.name
                    if dest.exists():
                        continue
                    if p.is_file():
                        shutil.copy2(p, dest)
                    elif p.is_dir():
                        shutil.copytree(p, dest, dirs_exist_ok=True)

            for name in (
                "users.json",
                "test_results.json",
                "access_requests.json",
                "achievements.json",
            ):
                src = legacy / name
                if src.is_file():
                    dest = base / name
                    if not dest.exists():
                        shutil.copy2(src, dest)

        static_tm = project_root / "web_app" / "static" / "theory_materials"
        if static_tm.is_dir():
            for p in static_tm.iterdir():
                if not p.is_file():
                    continue
                dest = theory_dest / p.name
                if not dest.exists():
                    shutil.copy2(p, dest)

        marker.write_text("1", encoding="utf-8")


def _base_path() -> Path:
    project_root = _project_root()
    override = (os.getenv("APP_DATA_DIR") or "").strip()
    if override:
        base = Path(override).expanduser()
        if not base.is_absolute():
            base = (project_root / base).resolve()
        else:
            base = base.resolve()
    else:
        base = (project_root / "data").resolve()

    base.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_data(project_root, base)
    return base


def _theory_dir() -> Path:
    theory = _base_path() / "theory_materials"
    theory.mkdir(parents=True, exist_ok=True)
    return theory


def get_data_root() -> Path:
    return _base_path()


def get_theory_materials_dir() -> Path:
    return _theory_dir()


def load_materials() -> List[TheoryMaterial]:
    materials_file = _theory_dir() / "materials.json"
    if not materials_file.exists():
        return []

    with materials_file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("materials", [])

    materials: List[TheoryMaterial] = []
    for m in raw:
        order_val = m["order"]
        title_val = m["title"]
        material_key = f"{order_val}:{title_val}"
        default_id = hashlib.md5(material_key.encode("utf-8")).hexdigest()
        tests: List[Question] = []
        for q in m.get("tests", []):
            tests.append(
                Question(
                    question_text=q["question_text"],
                    question_type=QuestionType(q["question_type"]),
                    correct_answers=q["correct_answers"],
                    options=q.get("options"),
                    explanation=q.get("explanation", ""),
                )
            )
        materials.append(
            TheoryMaterial(
                material_id=m.get("material_id", default_id),
                author=m.get("author", "admin"),
                title=m["title"],
                content=m["content"],
                images=m.get("images", []),
                order=order_val,
                video_url=m.get("video_url"),
                is_closed=bool(m.get("is_closed", False)),
                show_correct_answers=m.get("show_correct_answers", True),
                tests=tests,
            )
        )
    return materials


def save_materials(materials: List[TheoryMaterial]) -> None:
    materials_file = _theory_dir() / "materials.json"

    def question_to_dict(q: Question) -> dict:
        data = {
            "question_text": q.question_text,
            "question_type": q.question_type.value,
            "correct_answers": q.correct_answers,
        }
        if q.options is not None:
            data["options"] = q.options
        if q.explanation:
            data["explanation"] = q.explanation
        return data

    payload = {
        "materials": [
            {
                "material_id": m.material_id,
                "author": getattr(m, "author", "admin"),
                "title": m.title,
                "content": m.content,
                "images": m.images,
                "video_url": m.video_url,
                "order": m.order,
                "is_closed": bool(getattr(m, "is_closed", False)),
                "show_correct_answers": m.show_correct_answers,
                "tests": [question_to_dict(q) for q in m.tests],
            }
            for m in materials
        ]
    }

    with _WRITE_LOCK:
        with materials_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_users() -> List[User]:
    users_file = _base_path() / "users.json"
    if not users_file.exists():
        admin = User(
            username="admin",
            password_hash="admin",
            role=UserRole.TEACHER,
            first_name="Admin",
            last_name="",
            last_seen="",
        )
        save_users([admin])
        return [admin]

    with users_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    users: List[User] = []
    for u in data.get("users", []):
        users.append(
            User(
                username=u["username"],
                password_hash=u["password_hash"],
                role=UserRole(u.get("role", "student")),
                first_name=u.get("first_name", ""),
                last_name=u.get("last_name", ""),
                last_seen=u.get("last_seen", ""),
            )
        )
    return users


def save_users(users: List[User]) -> None:
    users_file = _base_path() / "users.json"
    payload = {
        "users": [
            {
                "username": u.username,
                "password_hash": u.password_hash,
                "role": u.role.value,
                "first_name": u.first_name,
                "last_name": u.last_name,
                "last_seen": u.last_seen,
            }
            for u in users
        ]
    }
    with _WRITE_LOCK:
        with users_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_test_results() -> List[TestResult]:
    results_file = _base_path() / "test_results.json"
    if not results_file.exists():
        return []

    with results_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    results: List[TestResult] = []
    for r in data.get("results", []):
        test_id = r.get("test_id", "legacy_test")
        result_id = r.get("result_id")
        if not result_id:
            seed = f'{r.get("student_username","")}:{r.get("material_title","")}:{test_id}:{r.get("date","")}'
            result_id = hashlib.md5(seed.encode("utf-8")).hexdigest()
        qt_raw = r.get("question_timings", [])
        timings: List[QuestionTiming] = []
        if isinstance(qt_raw, list):
            for t in qt_raw:
                try:
                    timings.append(
                        QuestionTiming(
                            question_index=int(t.get("question_index", 0)),
                            duration_sec=float(t.get("duration_sec", 0.0)),
                        )
                    )
                except Exception:
                    continue
        results.append(
            TestResult(
                result_id=str(result_id),
                student_username=str(r.get("student_username", "")),
                material_title=str(r.get("material_title", "")),
                test_id=str(test_id),
                date=str(r.get("date", "")),
                correct_answers=int(r.get("correct_answers", 0)),
                total_questions=int(r.get("total_questions", 0)),
                percentage=float(r.get("percentage", 0.0)),
                mistakes=list(r.get("mistakes", [])) if isinstance(r.get("mistakes", []), list) else [],
                details=list(r.get("details", [])) if isinstance(r.get("details", []), list) else [],
                result_messages=list(r.get("result_messages", []))
                if isinstance(r.get("result_messages", []), list)
                else [],
                assignment_id=str(r.get("assignment_id", "")),
                total_duration_sec=float(r.get("total_duration_sec", 0.0)),
                question_timings=timings,
            )
        )
    return results


def save_test_results(results: List[TestResult]) -> None:
    """Сохраняет результаты тестов в формате, совместимом с десктопной версией."""

    results_file = _base_path() / "test_results.json"
    payload = {
        "results": [
            {
                "result_id": r.result_id,
                "student_username": r.student_username,
                "material_title": r.material_title,
                "test_id": r.test_id,
                "date": r.date,
                "correct_answers": r.correct_answers,
                "total_questions": r.total_questions,
                "percentage": r.percentage,
                "mistakes": r.mistakes,
                "details": r.details,
                "result_messages": r.result_messages,
                "assignment_id": r.assignment_id,
                "total_duration_sec": r.total_duration_sec,
                "question_timings": [
                    {
                        "question_index": qt.question_index,
                        "duration_sec": qt.duration_sec,
                    }
                    for qt in (r.question_timings or [])
                ],
            }
            for r in results
        ]
    }

    with _WRITE_LOCK:
        with results_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _access_requests_file() -> Path:
    return _base_path() / "access_requests.json"


def _achievements_file() -> Path:
    return _base_path() / "achievements.json"


def load_achievements() -> Dict[str, List[str]]:
    """
    Достижения пользователей.

    Формат:
    {
      "users": {
        "student1": ["FIRST_TEST", "PERFECT_SCORE"]
      }
    }
    """
    file = _achievements_file()
    if not file.exists():
        return {}

    with file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    users = data.get("users", {})
    if not isinstance(users, dict):
        return {}

    out: Dict[str, List[str]] = {}
    for username, items in users.items():
        if isinstance(items, list):
            out[str(username)] = [str(x) for x in items]
    return out


def save_achievements(users_achievements: Dict[str, List[str]]) -> None:
    file = _achievements_file()
    payload = {"users": users_achievements}
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_access_requests() -> List[TopicAccessRequest]:
    file = _access_requests_file()
    if not file.exists():
        return []

    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("requests", [])

    items: List[TopicAccessRequest] = []
    for r in raw:
        try:
            status = AccessRequestStatus(r.get("status", "pending"))
        except ValueError:
            status = AccessRequestStatus.PENDING

        items.append(
            TopicAccessRequest(
                request_id=r["request_id"],
                material_id=r["material_id"],
                material_title=r.get("material_title", ""),
                student_username=r["student_username"],
                status=status,
                created_at=r.get("created_at", ""),
                decided_at=r.get("decided_at", ""),
                decided_by=r.get("decided_by", ""),
            )
        )
    return items


def save_access_requests(requests: List[TopicAccessRequest]) -> None:
    file = _access_requests_file()
    payload = {
        "requests": [
            {
                "request_id": r.request_id,
                "material_id": r.material_id,
                "material_title": r.material_title,
                "student_username": r.student_username,
                "status": r.status.value,
                "created_at": r.created_at,
                "decided_at": r.decided_at,
                "decided_by": r.decided_by,
            }
            for r in requests
        ]
    }

    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _classes_file() -> Path:
    return _base_path() / "classes.json"


def _class_requests_file() -> Path:
    return _base_path() / "class_requests.json"


def _class_memberships_file() -> Path:
    return _base_path() / "class_memberships.json"


def _assignments_file() -> Path:
    return _base_path() / "assignments.json"


def _activity_log_file() -> Path:
    return _base_path() / "activity_log.json"


def _dialog_notifications_file() -> Path:
    return _base_path() / "dialog_notifications.json"


def load_classes() -> List[Classroom]:
    file = _classes_file()
    if not file.exists():
        return []
    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("classes", [])
    out: List[Classroom] = []
    for c in raw:
        out.append(
            Classroom(
                class_id=c["class_id"],
                name=c["name"],
                owner_teacher_username=c["owner_teacher_username"],
                invite_token=c["invite_token"],
                created_at=c.get("created_at", ""),
                is_active=bool(c.get("is_active", True)),
            )
        )
    return out


def save_classes(items: List[Classroom]) -> None:
    file = _classes_file()
    payload = {
        "classes": [
            {
                "class_id": c.class_id,
                "name": c.name,
                "owner_teacher_username": c.owner_teacher_username,
                "invite_token": c.invite_token,
                "created_at": c.created_at,
                "is_active": c.is_active,
            }
            for c in items
        ]
    }
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_class_membership_requests() -> List[ClassMembershipRequest]:
    file = _class_requests_file()
    if not file.exists():
        return []
    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("requests", [])
    out: List[ClassMembershipRequest] = []
    for r in raw:
        try:
            status = AccessRequestStatus(r.get("status", "pending"))
        except ValueError:
            status = AccessRequestStatus.PENDING
        out.append(
            ClassMembershipRequest(
                request_id=r["request_id"],
                class_id=r["class_id"],
                student_username=r["student_username"],
                status=status,
                created_at=r.get("created_at", ""),
                decided_at=r.get("decided_at", ""),
                decided_by=r.get("decided_by", ""),
            )
        )
    return out


def save_class_membership_requests(items: List[ClassMembershipRequest]) -> None:
    file = _class_requests_file()
    payload = {
        "requests": [
            {
                "request_id": r.request_id,
                "class_id": r.class_id,
                "student_username": r.student_username,
                "status": r.status.value,
                "created_at": r.created_at,
                "decided_at": r.decided_at,
                "decided_by": r.decided_by,
            }
            for r in items
        ]
    }
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_class_memberships() -> List[ClassMembership]:
    file = _class_memberships_file()
    if not file.exists():
        return []
    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("memberships", [])
    out: List[ClassMembership] = []
    for m in raw:
        out.append(
            ClassMembership(
                class_id=m["class_id"],
                student_username=m["student_username"],
                joined_at=m.get("joined_at", ""),
            )
        )
    return out


def save_class_memberships(items: List[ClassMembership]) -> None:
    file = _class_memberships_file()
    payload = {
        "memberships": [
            {
                "class_id": m.class_id,
                "student_username": m.student_username,
                "joined_at": m.joined_at,
            }
            for m in items
        ]
    }
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_assignments() -> List[ClassAssignment]:
    file = _assignments_file()
    if not file.exists():
        return []
    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("assignments", [])
    out: List[ClassAssignment] = []
    for a in raw:
        out.append(
            ClassAssignment(
                assignment_id=a["assignment_id"],
                class_id=a["class_id"],
                material_id=a["material_id"],
                material_title=a.get("material_title", ""),
                assigned_by=a.get("assigned_by", ""),
                assigned_at=a.get("assigned_at", ""),
                due_at=a.get("due_at", ""),
                is_active=bool(a.get("is_active", True)),
            )
        )
    return out


def save_assignments(items: List[ClassAssignment]) -> None:
    file = _assignments_file()
    payload = {
        "assignments": [
            {
                "assignment_id": a.assignment_id,
                "class_id": a.class_id,
                "material_id": a.material_id,
                "material_title": a.material_title,
                "assigned_by": a.assigned_by,
                "assigned_at": a.assigned_at,
                "due_at": a.due_at,
                "is_active": a.is_active,
            }
            for a in items
        ]
    }
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_activity_log() -> List[ActivityEvent]:
    file = _activity_log_file()
    if not file.exists():
        return []
    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("events", [])
    out: List[ActivityEvent] = []
    for e in raw:
        out.append(
            ActivityEvent(
                event_id=e["event_id"],
                username=e["username"],
                event_type=e.get("event_type", ""),
                created_at=e.get("created_at", ""),
                meta=e.get("meta", {}) if isinstance(e.get("meta", {}), dict) else {},
            )
        )
    return out


def save_activity_log(items: List[ActivityEvent]) -> None:
    file = _activity_log_file()
    payload = {
        "events": [
            {
                "event_id": e.event_id,
                "username": e.username,
                "event_type": e.event_type,
                "created_at": e.created_at,
                "meta": e.meta,
            }
            for e in items
        ]
    }
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def load_dialog_notifications() -> List[DialogNotificationState]:
    file = _dialog_notifications_file()
    if not file.exists():
        return []
    with file.open("r", encoding="utf-8") as f:
        raw = json.load(f).get("notifications", [])
    out: List[DialogNotificationState] = []
    for n in raw:
        out.append(
            DialogNotificationState(
                result_id=n["result_id"],
                username=n["username"],
                last_read_at=n.get("last_read_at", ""),
            )
        )
    return out


def save_dialog_notifications(items: List[DialogNotificationState]) -> None:
    file = _dialog_notifications_file()
    payload = {
        "notifications": [
            {
                "result_id": n.result_id,
                "username": n.username,
                "last_read_at": n.last_read_at,
            }
            for n in items
        ]
    }
    with _WRITE_LOCK:
        with file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

