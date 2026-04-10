import hashlib
import html
import json
import logging
import os
import re
from urllib import error, request
from datetime import datetime
from typing import Any, Dict, List, Tuple

from werkzeug.security import check_password_hash, generate_password_hash

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
from . import storage

logger = logging.getLogger(__name__)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _mask_api_key(value: str) -> str:
    if not value:
        return "<empty>"
    trimmed = value.strip()
    if len(trimmed) <= 10:
        return "*" * len(trimmed)
    return f"{trimmed[:8]}...{trimmed[-6:]}"


def _parse_dt(value: str) -> datetime | None:
    s = (value or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _strip_html_tags(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_for_compare(text: str) -> str:
    s = (text or "").lower().strip().replace("ё", "е")
    s = re.sub(r"[^0-9a-zа-я\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _levenshtein_limited(a: str, b: str, limit: int = 2) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        row_min = cur[0]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            cur.append(min(
                prev[j] + 1,
                cur[j - 1] + 1,
                prev[j - 1] + cost,
            ))
            row_min = min(row_min, cur[j])
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _should_veto_ai_true(user_answer: str, correct_answers: List[str]) -> bool:
    """
    Блокирует очевидно ложное "верно" от ИИ для коротких мусорных ответов.
    Длинные развернутые ответы этим фильтром почти не затрагиваются.
    """
    nu = _normalize_for_compare(user_answer)
    if not nu:
        return True
    if len(nu) > 6:
        return False

    profanity_markers = ("соси", "хуй", "пизд", "еб", "нах")
    if any(m in nu for m in profanity_markers):
        return True

    for c in correct_answers:
        nc = _normalize_for_compare(c)
        if not nc:
            continue
        if nu == nc:
            return False
        if len(nu) >= 3 and (nu in nc or nc in nu):
            return False
        if _levenshtein_limited(nu, nc, limit=1) <= 1:
            return False

    return True


def _extract_json_object(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    fragment = text[start : end + 1]
    try:
        parsed = json.loads(fragment)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _evaluate_text_answer_with_deepseek(
    material: TheoryMaterial, question: Question, user_answer: str
) -> tuple[bool, str] | None:
    api_key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    logger.warning("DeepSeek key fingerprint: %s", _mask_api_key(api_key))
    if not api_key:
        return None

    model = (os.getenv("DEEPSEEK_MODEL") or "deepseek/deepseek-chat-v3-0324:free").strip()
    base = (os.getenv("DEEPSEEK_API_URL") or "https://api.deepseek.com").rstrip("/")
    url = f"{base}/chat/completions"
    material_text = _strip_html_tags(getattr(material, "content", "") or "")
    explanation = (getattr(question, "explanation", "") or "").strip()
    correct_answers = [a.strip() for a in question.correct_answers if a and a.strip()]

    prompt = (
        "Ты проверяешь ответ ученика в учебной системе.\n"
        "Оцени по смыслу, опираясь на теорию и эталонные ответы.\n\n"
        "Считай ответ ПРАВИЛЬНЫМ, если по сути это тот же ответ, что и эталон, в том числе:\n"
        "- синонимы и перефразирование;\n"
        "- расшифровки и аббревиатуры (например, '2FA' и полная формулировка про двухфакторную аутентификацию);\n"
        "- разный порядок слов, язык ответа (RU/EN) при том же значении;\n"
        "- разный регистр букв;\n"
        "- разные ударения/дефисы/пробелы в терминах, если смысл не меняется;\n"
        "- варианты транслитерации имени понятия (например, заимствованных IT-терминов);\n"
        "- для коротких ответов (одно слово или короткий термин до ~4–5 слов): "
        "мелкие орфографические опечатки (лишняя/пропущенная буква, очевидная перестановка "
        "звуков в одном слове), если однозначно имеется в виду тот же термин, что в эталоне "
        "(пример: эталон «Рансомвер», ответ «рансомавер» — про то же malicious ПО с выкупом).\n\n"
        "Считай НЕПРАВИЛЬНЫМ, если ответ про другое понятие, с фактической ошибкой по теме, "
        "пустой, избегает ответа или не покрывает суть вопроса.\n"
        "Если эталон — список допустимых вариантов, достаточно попадания в любой из них "
        "(с учётом правил выше).\n\n"
        "ДАНО:\n"
        f"ТЕОРЕТИЧЕСКИЙ МАТЕРИАЛ:\n{material_text}\n\n"
        f"ВОПРОС:\n{question.question_text}\n\n"
        f"ПОЯСНЕНИЕ:\n{explanation or '—'}\n\n"
        f"ЭТАЛОННЫЕ ОТВЕТЫ:\n{'; '.join(correct_answers) if correct_answers else '—'}\n\n"
        f"ОТВЕТ УЧЕНИКА:\n{user_answer}\n\n"
        "Ответь ТОЛЬКО JSON (без markdown):\n"
        '{"is_correct": true/false, "reason": "кратко, до 140 символов"}'
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        choices = data.get("choices", [])
        if not choices or not isinstance(choices[0], dict):
            logger.warning("DeepSeek: empty or invalid choices in response")
            return None
        msg = choices[0].get("message") or {}
        text_out = str(msg.get("content", "")).strip()
        obj = _extract_json_object(text_out)
        if not obj:
            logger.warning("DeepSeek: model response is not JSON object: %s", text_out[:300])
            return None
        is_correct = bool(obj.get("is_correct", False))
        reason = str(obj.get("reason", "")).strip()
        return is_correct, reason
    except error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except OSError:
            detail = "<failed to read HTTPError body>"
        logger.warning("DeepSeek HTTPError %s: %s", e.code, detail)
        return None
    except (error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError) as e:
        logger.warning("DeepSeek request failed: %s", e)
        return None


class AchievementId:
    FIRST_TEST = "FIRST_TEST"
    PERFECT_SCORE = "PERFECT_SCORE"
    FIVE_TESTS_COMPLETED = "FIVE_TESTS_COMPLETED"
    TEN_TESTS = "TEN_TESTS"
    THREE_TOPICS = "THREE_TOPICS"
    FIVE_TOPICS = "FIVE_TOPICS"
    FIVE_SCORES_80 = "FIVE_SCORES_80"
    TRIPLE_90 = "TRIPLE_90"
    SCORE_95_ONCE = "SCORE_95_ONCE"
    HUNDRED_QUESTIONS = "HUNDRED_QUESTIONS"


def get_user_achievements(username: str) -> List[str]:
    all_ach = storage.load_achievements()
    return all_ach.get(username, [])


def get_achievements_detail(username: str) -> List[Dict[str, Any]]:
    maybe_award_achievements(username)
    earned = set(get_user_achievements(username))
    results = [r for r in storage.load_test_results() if r.student_username == username]
    n = len(results)
    scores = [float(r.percentage) for r in results]
    best = max(scores) if scores else 0.0
    has_perfect = any(r.percentage >= 100.0 for r in results)
    unique_topics = len({r.test_id for r in results})
    count_80 = sum(1 for r in results if r.percentage >= 80.0)
    count_90 = sum(1 for r in results if r.percentage >= 90.0)
    has_95 = any(r.percentage >= 95.0 for r in results)
    questions_total = sum(r.total_questions for r in results)

    details: List[Dict[str, Any]] = []

    ft_done = n >= 1
    details.append(
        {
            "id": AchievementId.FIRST_TEST,
            "title": "Первый тест",
            "description": "Пройдите хотя бы один тест.",
            "unlocked": AchievementId.FIRST_TEST in earned,
            "current": 1 if ft_done else 0,
            "target": 1,
            "progress_percent": 100.0 if ft_done else 0.0,
            "variant": "primary",
        }
    )

    details.append(
        {
            "id": AchievementId.THREE_TOPICS,
            "title": "Три темы",
            "description": "Пройдите тесты по трём разным материалам.",
            "unlocked": AchievementId.THREE_TOPICS in earned,
            "current": min(3, unique_topics),
            "target": 3,
            "progress_percent": min(100.0, (min(3, unique_topics) / 3.0) * 100.0),
            "variant": "info",
        }
    )

    details.append(
        {
            "id": AchievementId.FIVE_TOPICS,
            "title": "Пять тем",
            "description": "Пройдите тесты по пяти разным материалам.",
            "unlocked": AchievementId.FIVE_TOPICS in earned,
            "current": min(5, unique_topics),
            "target": 5,
            "progress_percent": min(100.0, (min(5, unique_topics) / 5.0) * 100.0),
            "variant": "info",
        }
    )

    cur5 = min(n, 5)
    details.append(
        {
            "id": AchievementId.FIVE_TESTS_COMPLETED,
            "title": "5 тестов пройдено",
            "description": "Завершите 5 попыток (каждое прохождение считается).",
            "unlocked": AchievementId.FIVE_TESTS_COMPLETED in earned,
            "current": cur5,
            "target": 5,
            "progress_percent": min(100.0, (cur5 / 5.0) * 100.0),
            "variant": "primary",
        }
    )

    cur10 = min(n, 10)
    details.append(
        {
            "id": AchievementId.TEN_TESTS,
            "title": "10 попыток",
            "description": "Пройдите тесты 10 раз.",
            "unlocked": AchievementId.TEN_TESTS in earned,
            "current": cur10,
            "target": 10,
            "progress_percent": min(100.0, (cur10 / 10.0) * 100.0),
            "variant": "warning",
        }
    )

    c80 = min(5, count_80)
    details.append(
        {
            "id": AchievementId.FIVE_SCORES_80,
            "title": "Пятёрка восьмёрок",
            "description": "5 раз наберите не менее 80% за попытку.",
            "unlocked": AchievementId.FIVE_SCORES_80 in earned,
            "current": c80,
            "target": 5,
            "progress_percent": min(100.0, (c80 / 5.0) * 100.0),
            "variant": "danger",
        }
    )

    c90 = min(3, count_90)
    details.append(
        {
            "id": AchievementId.TRIPLE_90,
            "title": "Три девятки",
            "description": "3 раза наберите не менее 90% за попытку.",
            "unlocked": AchievementId.TRIPLE_90 in earned,
            "current": c90,
            "target": 3,
            "progress_percent": min(100.0, (c90 / 3.0) * 100.0),
            "variant": "success",
        }
    )

    details.append(
        {
            "id": AchievementId.SCORE_95_ONCE,
            "title": "Почти идеально",
            "description": "Хотя бы раз наберите от 95% за попытку.",
            "unlocked": AchievementId.SCORE_95_ONCE in earned,
            "current": 1 if has_95 else 0,
            "target": 1,
            "progress_percent": 100.0 if has_95 else min(100.0, (best / 95.0) * 100.0),
            "variant": "warning",
        }
    )

    details.append(
        {
            "id": AchievementId.PERFECT_SCORE,
            "title": "100% результат",
            "description": "Наберите 100% хотя бы на одном тесте.",
            "unlocked": AchievementId.PERFECT_SCORE in earned,
            "current": 1 if has_perfect else 0,
            "target": 1,
            "progress_percent": 100.0 if has_perfect else min(100.0, best),
            "variant": "success",
        }
    )

    q_cur = min(100, questions_total)
    details.append(
        {
            "id": AchievementId.HUNDRED_QUESTIONS,
            "title": "Сотня вопросов",
            "description": "Ответьте в сумме на 100 вопросов во всех тестах.",
            "unlocked": AchievementId.HUNDRED_QUESTIONS in earned,
            "current": q_cur,
            "target": 100,
            "progress_percent": min(100.0, questions_total),
            "variant": "primary",
        }
    )

    return details


def award_achievement(username: str, achievement_id: str) -> bool:
    all_ach = storage.load_achievements()
    current = set(all_ach.get(username, []))
    if achievement_id in current:
        return False
    current.add(achievement_id)
    all_ach[username] = sorted(current)
    storage.save_achievements(all_ach)
    return True


def maybe_award_achievements(username: str) -> List[str]:
    """
    Выдаёт достижения на основе текущих результатов пользователя.
    Возвращает список достижений, которые были выданы в этом вызове.
    """
    users = storage.load_users()
    u = next((x for x in users if x.username == username), None)
    if u and u.role != UserRole.STUDENT:
        return []

    results = storage.load_test_results()
    user_results = [r for r in results if r.student_username == username]

    awarded: List[str] = []
    unique_topics = len({r.test_id for r in user_results})
    count_80 = sum(1 for r in user_results if r.percentage >= 80.0)
    count_90 = sum(1 for r in user_results if r.percentage >= 90.0)
    questions_total = sum(r.total_questions for r in user_results)

    def _try(aid: str, condition: bool) -> None:
        if condition and award_achievement(username, aid):
            awarded.append(aid)

    _try(AchievementId.FIRST_TEST, len(user_results) >= 1)
    _try(AchievementId.THREE_TOPICS, unique_topics >= 3)
    _try(AchievementId.FIVE_TOPICS, unique_topics >= 5)
    _try(AchievementId.FIVE_TESTS_COMPLETED, len(user_results) >= 5)
    _try(AchievementId.TEN_TESTS, len(user_results) >= 10)
    _try(AchievementId.FIVE_SCORES_80, count_80 >= 5)
    _try(AchievementId.TRIPLE_90, count_90 >= 3)
    _try(AchievementId.SCORE_95_ONCE, any(r.percentage >= 95.0 for r in user_results))
    _try(AchievementId.PERFECT_SCORE, any(r.percentage >= 100.0 for r in user_results))
    _try(AchievementId.HUNDRED_QUESTIONS, questions_total >= 100)

    return awarded


def get_student_dashboard_stats(username: str) -> Dict[str, object]:
    results = storage.load_test_results()
    user_results = [r for r in results if r.student_username == username]

    total_attempts = len(user_results)
    scores = [float(r.percentage) for r in user_results]
    avg_score = (sum(scores) / total_attempts) if total_attempts else 0.0
    best_score = max(scores) if scores else 0.0

    last5 = sorted(user_results, key=lambda r: r.date, reverse=True)[:5]

    return {
        "total_attempts": total_attempts,
        "average_score": avg_score,
        "best_score": best_score,
        "last5": last5,
    }


def get_user_progress(username: str) -> List[Dict[str, object]]:
    results = storage.load_test_results()
    user_results = [r for r in results if r.student_username == username]
    user_results.sort(key=lambda r: r.date)
    return [{"date": r.date, "score": float(r.percentage)} for r in user_results]


def get_access_request_for_student(
    student_username: str, material_id: str
) -> TopicAccessRequest | None:
    for r in storage.load_access_requests():
        if r.student_username == student_username and r.material_id == material_id:
            return r
    return None


def student_has_access(student_username: str, material: TheoryMaterial) -> bool:
    if not getattr(material, "is_closed", False):
        return True
    req = get_access_request_for_student(student_username, material.material_id)
    return bool(req and req.status == AccessRequestStatus.APPROVED)


def teacher_can_access_material(material: TheoryMaterial, teacher_username: str) -> bool:
    """
    Учитель может смотреть/проходить тест по чужому открытому материалу.
    Закрытые темы других авторов недоступны; свои материалы (в т.ч. закрытые) — доступны.
    """
    if teacher_username == "admin":
        return True
    if getattr(material, "author", "admin") == teacher_username:
        return True
    if not getattr(material, "is_closed", False):
        return True
    return False


def submit_access_request(student_username: str, material: TheoryMaterial) -> TopicAccessRequest:
    existing = get_access_request_for_student(student_username, material.material_id)
    if existing and existing.status in (
        AccessRequestStatus.PENDING,
        AccessRequestStatus.APPROVED,
    ):
        return existing

    requests = storage.load_access_requests()
    request_id_seed = f"{student_username}:{material.material_id}:{_now_str()}"
    request_id = hashlib.md5(request_id_seed.encode("utf-8")).hexdigest()

    new_req = TopicAccessRequest(
        request_id=request_id,
        material_id=material.material_id,
        material_title=material.title,
        student_username=student_username,
        status=AccessRequestStatus.PENDING,
        created_at=_now_str(),
    )
    requests.append(new_req)
    storage.save_access_requests(requests)
    return new_req


def list_access_requests() -> List[TopicAccessRequest]:
    items = storage.load_access_requests()
    items.sort(key=lambda r: (0 if r.status == AccessRequestStatus.PENDING else 1, r.created_at), reverse=False)
    return items


def list_access_requests_for_teacher(teacher_username: str) -> List[TopicAccessRequest]:
    materials = {m.material_id: getattr(m, "author", "admin") for m in storage.load_materials()}
    items = [
        r
        for r in storage.load_access_requests()
        if materials.get(r.material_id) == teacher_username
        or teacher_username == "admin"
    ]
    items.sort(
        key=lambda r: (0 if r.status == AccessRequestStatus.PENDING else 1, r.created_at),
        reverse=False,
    )
    return items


def decide_access_request(
    request_id: str, decided_by: str, approve: bool
) -> TopicAccessRequest | None:
    requests = storage.load_access_requests()
    for i, r in enumerate(requests):
        if r.request_id == request_id:
            # Security: only author of the material (or admin) can decide.
            try:
                material = next(
                    m
                    for m in storage.load_materials()
                    if m.material_id == r.material_id
                )
            except StopIteration:
                return None
            author = getattr(material, "author", "admin")
            if decided_by != author and decided_by != "admin":
                return None

            r.status = AccessRequestStatus.APPROVED if approve else AccessRequestStatus.REJECTED
            r.decided_at = _now_str()
            r.decided_by = decided_by
            requests[i] = r
            storage.save_access_requests(requests)
            return r
    return None


def authenticate(username: str, password: str) -> User | None:
    users = storage.load_users()
    for i, user in enumerate(users):
        if user.username != username:
            continue

        stored = user.password_hash or ""
        ok = False
        if stored.startswith("pbkdf2:") or stored.startswith("scrypt:"):
            ok = check_password_hash(stored, password)
        else:
            ok = stored == password
            if ok:
                users[i].password_hash = generate_password_hash(password)
                storage.save_users(users)

        if ok:
            return users[i]
    return None


def user_exists(username: str) -> bool:
    return any(u.username == username for u in storage.load_users())


def register_user(
    username: str,
    password: str,
    role: UserRole,
    first_name: str = "",
    last_name: str = "",
) -> User:
    users = storage.load_users()
    new_user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role,
        first_name=first_name,
        last_name=last_name,
        last_seen="",
    )
    users.append(new_user)
    storage.save_users(users)
    return new_user


def update_user_last_seen(username: str) -> None:
    users = storage.load_users()
    for i, u in enumerate(users):
        if u.username == username:
            users[i].last_seen = _now_str()
            storage.save_users(users)
            log_activity_event(username, "login")
            return


def update_user_profile(username: str, first_name: str, last_name: str) -> None:
    users = storage.load_users()
    for i, u in enumerate(users):
        if u.username == username:
            users[i].first_name = first_name
            users[i].last_name = last_name
            storage.save_users(users)
            return


def change_password(username: str, old_password: str, new_password: str) -> bool:
    users = storage.load_users()
    for i, u in enumerate(users):
        if u.username != username:
            continue

        stored = u.password_hash or ""
        ok_old = False
        if stored.startswith("pbkdf2:") or stored.startswith("scrypt:"):
            ok_old = check_password_hash(stored, old_password)
        else:
            ok_old = stored == old_password

        if not ok_old:
            return False

        users[i].password_hash = generate_password_hash(new_password)
        storage.save_users(users)
        return True

    return False


def load_all_materials_ordered() -> List[TheoryMaterial]:
    """
    Возвращает все материалы сразу, независимо от уровня.
    Используется для отображения общего списка тем на главном экране.
    Порядок: по полю order, затем по title.
    """

    all_materials = storage.load_materials()
    all_materials.sort(key=lambda m: (m.order, m.title))
    return all_materials


def get_material_by_index(index: int) -> TheoryMaterial:
    materials = load_all_materials_ordered()
    if index < 0 or index >= len(materials):
        raise IndexError("Материал не найден")
    return materials[index]


def get_material_by_id(material_id: str) -> TheoryMaterial:
    materials = load_all_materials_ordered()
    for m in materials:
        if m.material_id == material_id:
            return m
    raise IndexError("Материал не найден")


def compute_test_result(
    material: TheoryMaterial,
    user_answers: List[str | List[str] | None],
    student_username: str,
    question_timings: List[QuestionTiming] | None = None,
    assignment_id: str = "",
) -> TestResult:
    """
    Проверяет ответы и формирует результат:
    - для single_choice: ответ должен быть в correct_answers;
    - для multiple_choice: множества выбранных и правильных ответов должны совпасть;
    - для text_answer: оценка только через DeepSeek; при ошибке API или без ключа
      ответ считается неверным.
    """

    questions = material.tests
    total_questions = len(questions)
    correct_answers = 0
    mistakes: List[str] = []
    details: List[Dict[str, object]] = []
    timings_map = {t.question_index: t.duration_sec for t in (question_timings or [])}

    for i, question in enumerate(questions):
        user_answer = user_answers[i] if i < len(user_answers) else None
        is_correct = False

        if question.question_type == QuestionType.SINGLE_CHOICE:
            is_correct = bool(
                isinstance(user_answer, str)
                and user_answer in question.correct_answers
            )
            if not is_correct:
                shown_answer = (
                    user_answer if isinstance(user_answer, str) and user_answer else "ответ не дан"
                )
                msg_lines = [
                    f"Вопрос {i + 1}: {question.question_text}",
                    f"Ваш ответ: {shown_answer}",
                ]
                if material.show_correct_answers:
                    msg_lines.append(
                        f"Правильный ответ: {', '.join(question.correct_answers)}"
                    )
                mistakes.append("\n".join(msg_lines))

        elif question.question_type == QuestionType.MULTIPLE_CHOICE:
            if isinstance(user_answer, list):
                user_set = set(user_answer)
                correct_set = set(question.correct_answers)
                is_correct = user_set == correct_set
                if not is_correct:
                    shown_answers = ", ".join(user_answer) if user_answer else "ответы не даны"
                    msg_lines = [
                        f"Вопрос {i + 1}: {question.question_text}",
                        f"Ваши ответы: {shown_answers}",
                    ]
                    if material.show_correct_answers:
                        msg_lines.append(
                            f"Правильные ответы: {', '.join(question.correct_answers)}"
                        )
                    mistakes.append("\n".join(msg_lines))
            else:
                is_correct = False
                msg_lines = [
                    f"Вопрос {i + 1}: {question.question_text}",
                    "Ваши ответы: ответы не даны",
                ]
                if material.show_correct_answers:
                    msg_lines.append(
                        f"Правильные ответы: {', '.join(question.correct_answers)}"
                    )
                mistakes.append("\n".join(msg_lines))

        else:  # TEXT_ANSWER
            ai_reason = ""
            if isinstance(user_answer, str) and user_answer.strip():
                normalized_user_answer = user_answer.strip()
                ai_check = _evaluate_text_answer_with_deepseek(
                    material=material,
                    question=question,
                    user_answer=normalized_user_answer,
                )
                if ai_check is not None:
                    is_correct, ai_reason = ai_check
                    if is_correct and _should_veto_ai_true(normalized_user_answer, question.correct_answers):
                        is_correct = False
                        ai_reason = (
                            "Ответ не засчитан: совпадение с эталоном не подтверждено "
                            "дополнительной проверкой."
                        )
                else:
                    is_correct = False
                    ai_reason = (
                        "Проверка через DeepSeek недоступна "
                        "(нет ключа DEEPSEEK_API_KEY, ошибка сети или ответа API)."
                    )
            else:
                is_correct = False

            if not is_correct:
                shown_answer = (
                    user_answer if isinstance(user_answer, str) and user_answer else "ответ не дан"
                )
                msg_lines = [
                    f"Вопрос {i + 1}: {question.question_text}",
                    f"Ваш ответ: {shown_answer}",
                ]
                if material.show_correct_answers:
                    msg_lines.append(
                        f"Правильный ответ: {', '.join(question.correct_answers)}"
                    )
                if ai_reason:
                    msg_lines.append(f"Оценка ИИ: {ai_reason}")
                mistakes.append("\n".join(msg_lines))

        if is_correct:
            correct_answers += 1

        details.append(
            {
                "question_index": i,
                "question_text": question.question_text,
                "question_type": question.question_type.value,
                "options": question.options or [],
                "user_answer": user_answer if user_answer is not None else "",
                "correct_answers": question.correct_answers,
                "is_correct": is_correct,
                "explanation": getattr(question, "explanation", ""),
                "ai_reason": ai_reason if question.question_type == QuestionType.TEXT_ANSWER else "",
                "duration_sec": float(timings_map.get(i, 0.0)),
            }
        )

    percentage = (correct_answers / total_questions) * 100 if total_questions else 0.0

    test_content = str(
        [(q.question_text, tuple(q.correct_answers)) for q in questions]
    )
    test_id = hashlib.md5(test_content.encode("utf-8")).hexdigest()

    result_id_seed = f"{student_username}:{material.title}:{test_id}:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    result_id = hashlib.md5(result_id_seed.encode("utf-8")).hexdigest()

    result = TestResult(
        result_id=result_id,
        student_username=student_username,
        material_title=material.title,
        test_id=test_id,
        date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        correct_answers=correct_answers,
        total_questions=total_questions,
        percentage=percentage,
        mistakes=mistakes,
        details=details,
        assignment_id=assignment_id,
        total_duration_sec=sum(timings_map.values()),
        question_timings=(question_timings or []),
    )

    results = storage.load_test_results()
    results.append(result)
    storage.save_test_results(results)

    maybe_award_achievements(student_username)

    return result


def get_results_for_user(username: str, is_teacher: bool) -> List[TestResult]:
    results = storage.load_test_results()
    if is_teacher:
        filtered = list(results)
    else:
        filtered = [r for r in results if r.student_username == username]
    return sorted(filtered, key=lambda r: r.date, reverse=True)


def get_user_display_name(username: str) -> str:
    if not (username or "").strip():
        return ""
    uname = username.strip()
    for u in storage.load_users():
        if u.username == uname:
            full = f"{(u.first_name or '').strip()} {(u.last_name or '').strip()}".strip()
            return full if full else uname
    return uname


def user_can_access_result_chat(username: str, role: UserRole, result: TestResult) -> bool:
    if role == UserRole.STUDENT:
        return result.student_username == username
    if role == UserRole.TEACHER:
        return True
    return False


def append_result_message(
    result_id: str,
    author_username: str,
    author_role: UserRole,
    text: str,
) -> tuple[bool, str, Dict[str, Any] | None]:
    """
    Добавляет сообщение в переписку по результату.
    Возвращает (успех, сообщение_об_ошибке, новое_сообщение_или_None).
    """
    text = (text or "").strip()
    if not text:
        return False, "Введите текст сообщения.", None
    if len(text) > 2000:
        return False, "Сообщение не длиннее 2000 символов.", None
    if author_role not in (UserRole.STUDENT, UserRole.TEACHER):
        return False, "Недопустимая роль.", None

    role_str = "student" if author_role == UserRole.STUDENT else "teacher"
    results = storage.load_test_results()
    for i, r in enumerate(results):
        if r.result_id != result_id:
            continue
        msgs = list(r.result_messages or [])
        entry: Dict[str, Any] = {
            "author_role": role_str,
            "author_username": author_username,
            "author_display_name": get_user_display_name(author_username),
            "text": text,
            "created_at": _now_str(),
        }
        msgs.append(entry)
        r.result_messages = msgs
        results[i] = r
        storage.save_test_results(results)
        return True, "", entry
    return False, "Результат не найден.", None


def log_activity_event(username: str, event_type: str, meta: Dict[str, Any] | None = None) -> None:
    events = storage.load_activity_log()
    seed = f"{username}:{event_type}:{_now_str()}:{len(events)}"
    event_id = hashlib.md5(seed.encode("utf-8")).hexdigest()
    events.append(
        ActivityEvent(
            event_id=event_id,
            username=username,
            event_type=event_type,
            created_at=_now_str(),
            meta=(meta or {}),
        )
    )
    storage.save_activity_log(events)


def create_classroom(teacher_username: str, name: str) -> Classroom:
    classes = storage.load_classes()
    seed = f"{teacher_username}:{name}:{_now_str()}"
    class_id = hashlib.md5(seed.encode("utf-8")).hexdigest()
    token = hashlib.md5(f"{class_id}:invite".encode("utf-8")).hexdigest()[:20]
    room = Classroom(
        class_id=class_id,
        name=name,
        owner_teacher_username=teacher_username,
        invite_token=token,
        created_at=_now_str(),
        is_active=True,
    )
    classes.append(room)
    storage.save_classes(classes)
    log_activity_event(teacher_username, "class_created", {"class_id": class_id})
    return room


def get_class_by_id(class_id: str) -> Classroom | None:
    return next((c for c in storage.load_classes() if c.class_id == class_id), None)


def get_class_by_invite_token(token: str) -> Classroom | None:
    return next((c for c in storage.load_classes() if c.invite_token == token), None)


def get_teacher_classes(teacher_username: str) -> List[Classroom]:
    return [c for c in storage.load_classes() if c.owner_teacher_username == teacher_username]


def get_student_classes(student_username: str) -> List[Classroom]:
    memberships = storage.load_class_memberships()
    ids = {m.class_id for m in memberships if m.student_username == student_username}
    return [c for c in storage.load_classes() if c.class_id in ids]


def get_class_members(class_id: str) -> List[ClassMembership]:
    return [m for m in storage.load_class_memberships() if m.class_id == class_id]


def get_class_member_usernames(class_id: str) -> List[str]:
    return [m.student_username for m in get_class_members(class_id)]


def submit_class_join_request(student_username: str, class_id: str) -> ClassMembershipRequest:
    requests = storage.load_class_membership_requests()
    memberships = storage.load_class_memberships()
    if any(m.class_id == class_id and m.student_username == student_username for m in memberships):
        return ClassMembershipRequest(
            request_id="already_member",
            class_id=class_id,
            student_username=student_username,
            status=AccessRequestStatus.APPROVED,
            created_at=_now_str(),
        )
    for r in requests:
        if r.class_id == class_id and r.student_username == student_username and r.status == AccessRequestStatus.PENDING:
            return r
    seed = f"{class_id}:{student_username}:{_now_str()}"
    req = ClassMembershipRequest(
        request_id=hashlib.md5(seed.encode("utf-8")).hexdigest(),
        class_id=class_id,
        student_username=student_username,
        status=AccessRequestStatus.PENDING,
        created_at=_now_str(),
    )
    requests.append(req)
    storage.save_class_membership_requests(requests)
    log_activity_event(student_username, "class_join_request", {"class_id": class_id})
    return req


def get_class_requests_for_teacher(teacher_username: str) -> List[ClassMembershipRequest]:
    teacher_class_ids = {c.class_id for c in get_teacher_classes(teacher_username)}
    reqs = [r for r in storage.load_class_membership_requests() if r.class_id in teacher_class_ids]
    reqs.sort(key=lambda r: (0 if r.status == AccessRequestStatus.PENDING else 1, r.created_at))
    return reqs


def decide_class_join_request(
    request_id: str, teacher_username: str, approve: bool
) -> ClassMembershipRequest | None:
    reqs = storage.load_class_membership_requests()
    classes = storage.load_classes()
    idx = next((i for i, r in enumerate(reqs) if r.request_id == request_id), -1)
    if idx < 0:
        return None
    req = reqs[idx]
    cls = next((c for c in classes if c.class_id == req.class_id), None)
    if cls is None or cls.owner_teacher_username != teacher_username:
        return None
    req.status = AccessRequestStatus.APPROVED if approve else AccessRequestStatus.REJECTED
    req.decided_at = _now_str()
    req.decided_by = teacher_username
    reqs[idx] = req
    storage.save_class_membership_requests(reqs)
    if approve:
        memberships = storage.load_class_memberships()
        if not any(m.class_id == req.class_id and m.student_username == req.student_username for m in memberships):
            memberships.append(
                ClassMembership(
                    class_id=req.class_id,
                    student_username=req.student_username,
                    joined_at=_now_str(),
                )
            )
            storage.save_class_memberships(memberships)
    log_activity_event(
        teacher_username,
        "class_join_request_decided",
        {"request_id": request_id, "approve": approve},
    )
    return req


def create_class_assignment(
    teacher_username: str,
    class_id: str,
    material_id: str,
    due_at: str = "",
) -> ClassAssignment | None:
    cls = get_class_by_id(class_id)
    if cls is None or cls.owner_teacher_username != teacher_username:
        return None
    material = get_material_by_id(material_id)
    assignments = storage.load_assignments()
    seed = f"{class_id}:{material_id}:{_now_str()}"
    assignment = ClassAssignment(
        assignment_id=hashlib.md5(seed.encode("utf-8")).hexdigest(),
        class_id=class_id,
        material_id=material_id,
        material_title=material.title,
        assigned_by=teacher_username,
        assigned_at=_now_str(),
        due_at=due_at,
        is_active=True,
    )
    assignments.append(assignment)
    storage.save_assignments(assignments)
    log_activity_event(
        teacher_username,
        "class_assignment_created",
        {"class_id": class_id, "material_id": material_id},
    )
    return assignment


def get_assignments_for_class(class_id: str) -> List[ClassAssignment]:
    items = [a for a in storage.load_assignments() if a.class_id == class_id]
    items.sort(key=lambda a: a.assigned_at, reverse=True)
    return items


def get_assignments_for_student(student_username: str) -> List[ClassAssignment]:
    memberships = storage.load_class_memberships()
    class_ids = {m.class_id for m in memberships if m.student_username == student_username}
    items = [a for a in storage.load_assignments() if a.class_id in class_ids and a.is_active]
    items.sort(key=lambda a: a.assigned_at, reverse=True)
    return items


def get_class_statistics(class_id: str) -> Dict[str, Any]:
    members = get_class_member_usernames(class_id)
    assignments = get_assignments_for_class(class_id)
    results = storage.load_test_results()
    users = {u.username: u for u in storage.load_users()}
    class_assignment_ids = {a.assignment_id for a in assignments}

    class_results = [
        r
        for r in results
        if r.student_username in members and bool(r.assignment_id) and r.assignment_id in class_assignment_ids
    ]

    per_test: List[Dict[str, Any]] = []
    for a in assignments:
        assignment_results = [r for r in class_results if r.assignment_id == a.assignment_id]
        if not assignment_results:
            avg = 0.0
        else:
            avg = sum(float(r.percentage) for r in assignment_results) / len(assignment_results)
        per_test.append(
            {
                "assignment": a,
                "attempts": len(assignment_results),
                "average_score": round(avg, 2),
            }
        )

    member_activity: List[Dict[str, Any]] = []
    for uname in members:
        ur = [r for r in class_results if r.student_username == uname]
        last_seen = getattr(users.get(uname), "last_seen", "")
        member_activity.append(
            {
                "username": uname,
                "display_name": get_user_display_name(uname),
                "last_seen": last_seen,
                "attempts": len(ur),
                "avg_score": round((sum(float(r.percentage) for r in ur) / len(ur)), 2) if ur else 0.0,
            }
        )
    member_activity.sort(key=lambda x: x["display_name"])

    return {"per_test": per_test, "member_activity": member_activity}


def mark_dialog_read(result_id: str, username: str) -> None:
    items = storage.load_dialog_notifications()
    idx = next((i for i, n in enumerate(items) if n.result_id == result_id and n.username == username), -1)
    now = _now_str()
    results = storage.load_test_results()
    result = next((r for r in results if r.result_id == result_id), None)
    candidate_ts = now
    if result and result.result_messages:
        last_msg_at = str((result.result_messages[-1] or {}).get("created_at", "")).strip()
        last_dt = _parse_dt(last_msg_at)
        now_dt = _parse_dt(now)
        if last_dt and now_dt:
            candidate_ts = last_msg_at if last_dt > now_dt else now
        elif last_msg_at:
            candidate_ts = last_msg_at
    if idx >= 0:
        items[idx].last_read_at = candidate_ts
    else:
        items.append(DialogNotificationState(result_id=result_id, username=username, last_read_at=candidate_ts))
    storage.save_dialog_notifications(items)


def is_dialog_unread(result: TestResult, username: str) -> bool:
    messages = list(result.result_messages or [])
    if not messages:
        return False
    last_message = messages[-1]
    if str(last_message.get("author_username", "")) == username:
        return False
    last_msg_at = str(last_message.get("created_at", ""))
    items = storage.load_dialog_notifications()
    state = next((n for n in items if n.result_id == result.result_id and n.username == username), None)
    last_read = state.last_read_at if state else ""
    last_msg_dt = _parse_dt(last_msg_at)
    last_read_dt = _parse_dt(last_read)
    if last_msg_dt and last_read_dt:
        return last_msg_dt > last_read_dt
    return (last_msg_at or "") > (last_read or "")


def evaluate_password_strength(password: str) -> Dict[str, str]:
    strength = 0
    feedback: List[str] = []

    common_passwords = [
        "123",
        "1234",
        "12345",
        "123456",
        "1234567",
        "12345678",
        "123456789",
        "qwerty",
        "qwertyui",
        "qwertyuiop",
        "password",
        "admin",
        "administrator",
        "abc",
        "abcd",
        "abcde",
        "abcdef",
        "111",
        "222",
        "333",
        "444",
        "555",
        "666",
        "777",
        "888",
        "999",
        "000",
        "111111",
        "222222",
        "333333",
        "444444",
        "555555",
        "666666",
        "777777",
        "888888",
        "999999",
        "000000",
    ]

    password_lower = password.lower()
    for common in common_passwords:
        if common in password_lower:
            feedback.append(
                f"Пароль содержит распространенную комбинацию '{common}'"
            )
            strength = 0
            break

    sequences = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"]
    for seq in sequences:
        for i in range(len(seq) - 2):
            if seq[i : i + 3] in password_lower:
                feedback.append(
                    f"Пароль содержит последовательность клавиш '{seq[i:i+3]}'"
                )
                strength = 0
                break

    if len(password) < 8:
        feedback.append("Минимальная длина пароля - 8 символов")
    else:
        strength += 1

    if not any(ch.isdigit() for ch in password):
        feedback.append("Добавьте хотя бы одну цифру")
    else:
        strength += 1

    if not any(ch.islower() for ch in password):
        feedback.append("Добавьте хотя бы одну строчную букву")
    else:
        strength += 1

    if not any(ch.isupper() for ch in password):
        feedback.append("Добавьте хотя бы одну заглавную букву")
    else:
        strength += 1

    special_chars = "!@#$%^&*()_+-=[]{}|;:,.<>?"
    if not any(ch in special_chars for ch in password):
        feedback.append(
            "Добавьте хотя бы один специальный символ (!@#$%^&*()_+-=[]{}|;:,.<>?)"
        )
    else:
        strength += 1

    if strength == 0:
        color = "red"
        message = "Очень слабый пароль"
    elif strength == 1:
        color = "red"
        message = "Слабый пароль"
    elif strength == 2:
        color = "orange"
        message = "Средний пароль"
    elif strength == 3:
        color = "yellow"
        message = "Хороший пароль"
    elif strength == 4:
        color = "lightgreen"
        message = "Сильный пароль"
    else:
        color = "green"
        message = "Очень сильный пароль"

    return {
        "color": color,
        "message": message,
        "details": "\n".join(feedback),
        "strength": str(strength),
    }


