"""Проверка: .env и один вызов DeepSeek для текстовой оценки."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        print("FAIL: нет .env в корне проекта")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip().lstrip("\ufeff"), v.strip().strip('"').strip("'")
        if k:
            os.environ[k] = v


def main() -> None:
    load_dotenv()
    key = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    model = (os.getenv("DEEPSEEK_MODEL") or "openrouter/free").strip()
    print("DEEPSEEK_API_KEY:", "задан" if key else "НЕТ")
    print("DEEPSEEK_MODEL:", model)

    from web_app.logic import _evaluate_text_answer_with_deepseek
    from web_app.models import Question, QuestionType, TheoryMaterial

    mat = TheoryMaterial(
        material_id="t",
        author="t",
        title="Тест",
        content="<p>2FA — двухфакторная аутентификация.</p>",
        images=[],
        order=1,
        tests=[],
    )
    q = Question(
        question_text="Как называется метод с паролем и дополнительным кодом?",
        question_type=QuestionType.TEXT_ANSWER,
        correct_answers=["2FA"],
        options=None,
        explanation="Имеется в виду 2FA.",
    )

    if not key:
        print("Пропуск вызова API (нет ключа)")
        return

    base = (os.getenv("DEEPSEEK_API_URL") or "https://api.deepseek.com").rstrip("/")
    ping_url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Ответь одним словом: ok"}],
        "temperature": 0,
    }
    req = request.Request(
        url=ping_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=45) as resp:
            print("DeepSeek ping HTTP:", resp.status)
    except error.HTTPError as e:
        print("DeepSeek ping HTTP_ERROR:", e.code, e.read().decode("utf-8", errors="replace")[:500])
    except OSError as e:
        print("DeepSeek ping ERROR:", type(e).__name__, e)

    result = _evaluate_text_answer_with_deepseek(mat, q, "Двухфакторная аутентификация")
    print("Оценка ответа (2FA vs полная форма):", result)


if __name__ == "__main__":
    main()
