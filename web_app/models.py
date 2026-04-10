from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class AccessRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class QuestionType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    MULTIPLE_CHOICE = "multiple_choice"
    TEXT_ANSWER = "text_answer"


class UserRole(str, Enum):
    STUDENT = "student"
    TEACHER = "teacher"


@dataclass
class Question:
    question_text: str
    question_type: QuestionType
    correct_answers: List[str]
    options: Optional[List[str]] = None
    explanation: str = ""


@dataclass
class User:
    username: str
    password_hash: str
    role: UserRole
    first_name: str = ""
    last_name: str = ""
    last_seen: str = ""


@dataclass
class TheoryMaterial:
    material_id: str
    author: str
    title: str
    content: str
    images: List[str]
    order: int
    video_url: Optional[str] = None
    is_closed: bool = False
    show_correct_answers: bool = True
    tests: List[Question] = field(default_factory=list)


@dataclass
class TestResult:
    result_id: str
    student_username: str
    material_title: str
    test_id: str
    date: str
    correct_answers: int
    total_questions: int
    percentage: float
    mistakes: List[str]
    details: List[Dict[str, Any]] = field(default_factory=list)
    result_messages: List[Dict[str, Any]] = field(default_factory=list)
    assignment_id: str = ""
    total_duration_sec: float = 0.0
    question_timings: List["QuestionTiming"] = field(default_factory=list)


@dataclass
class TopicAccessRequest:
    request_id: str
    material_id: str
    material_title: str
    student_username: str
    status: AccessRequestStatus
    created_at: str
    decided_at: str = ""
    decided_by: str = ""


@dataclass
class QuestionTiming:
    question_index: int
    duration_sec: float


@dataclass
class Classroom:
    class_id: str
    name: str
    owner_teacher_username: str
    invite_token: str
    created_at: str
    is_active: bool = True


@dataclass
class ClassMembershipRequest:
    request_id: str
    class_id: str
    student_username: str
    status: AccessRequestStatus
    created_at: str
    decided_at: str = ""
    decided_by: str = ""


@dataclass
class ClassMembership:
    class_id: str
    student_username: str
    joined_at: str


@dataclass
class ClassAssignment:
    assignment_id: str
    class_id: str
    material_id: str
    material_title: str
    assigned_by: str
    assigned_at: str
    due_at: str = ""
    is_active: bool = True


@dataclass
class ActivityEvent:
    event_id: str
    username: str
    event_type: str
    created_at: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DialogNotificationState:
    result_id: str
    username: str
    last_read_at: str = ""

