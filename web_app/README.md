# Инфотест (web‑версия)

Веб‑приложение реализует учебно‑тестовую логику на стеке Flask + HTML + Bootstrap.

## Структура

- `web_app/app.py` — запуск Flask‑приложения, HTTP‑маршруты.
- `web_app/models.py` — модели данных (повторяют `models.py` из десктопной версии).
- `web_app/storage.py` — работа с файлами в **`data/`** (по умолчанию):
  `data/users.json`, `data/theory_materials/materials.json`, `data/test_results.json`,
  `data/access_requests.json`, `data/achievements.json`,
  `data/classes.json`, `data/class_requests.json`, `data/class_memberships.json`,
  `data/assignments.json`, `data/activity_log.json`, `data/dialog_notifications.json`,
  загрузки изображений в `data/theory_materials/`.
  Корень задаётся переменной `APP_DATA_DIR`. Однократная миграция: если в корне проекта есть папка
  `Новая папка` (старый десктопный формат) или остатки `web_app/static/theory_materials`, недостающие
  файлы копируются в `data/` без перезаписи уже существующих.
- `web_app/logic.py` — бизнес‑логика:
  - авторизация и регистрация (`LoginScreen`, `RegisterScreen`);
  - выбор материалов по уровню (`TheoryScreen.update_topics_list`, `load_current_material`);
  - проверка теста и формирование результатов (`TestScreen.submit_test`);
  - фильтрация результатов (`TestResultsScreen`).
- `web_app/templates/*.html` — HTML‑шаблоны (вместо PyQt5‑виджетов):
  - `login.html`, `register.html`, `theory.html`, `test.html`, `test_result.html`, `results.html`.
- `web_app/static/style.css` — базовые стили поверх Bootstrap.

## Ключевые соответствия логики

- **Пользователи и роли**
  - Класс `User` и enum `UserRole` перенесены в `web_app/models.py`.
  - Загрузка/сохранение пользователей (`MainWindow.load_users/save_users`) реализованы в `web_app/storage.py`.
  - Авторизация и проверка пароля (`LoginScreen.login`) реализованы в `web_app/logic.authenticate()`
    и маршруте `/login` в `app.py`.
  - Регистрация и проверка сложности пароля (`RegisterScreen.register`, `check_password_strength`)
    перенесены в `logic.evaluate_password_strength()` и маршрут `/register`.

- **Материалы и уровни**
  - `MaterialLevel`, структура материала и JSON‑формат сохранены как в исходном `models.py`.
  - Логика фильтрации по уровню и сортировка по `order`
    (`TheoryScreen.change_level`, `update_topics_list`) реализована в
    `logic.load_materials_by_level()` и маршруте `/theory`.
  - Загрузка контента, изображений и видео (`TheoryScreen.load_current_material`)
    реализована в `theory.html`:
    - текст (`material.content`) рендерится как HTML;
    - изображения лежат в `data/theory_materials/`, раздаются по URL `/static/theory_materials/…`;
    - `video_url` выводится как ссылка.

- **Тестирование**
  - Структура вопросов и типов (`Question`, `QuestionType`) — в `web_app/models.py`.
  - Алгоритм проверки ответов и подсчёта процента (`TestScreen.submit_test`) полностью
    перенесён в `web_app/logic.compute_test_result()`:
    - один вариант ответа — попадание в `correct_answers`;
    - несколько вариантов — сравнение множеств;
    - текстовый ответ — проверка через DeepSeek (нужен `DEEPSEEK_API_KEY`);
      без ключа или при сбое API ответ считается неверным.
  - Формирование `test_id` по содержимому вопросов (md5) сохранено.
  - Результаты сохраняются через `storage.save_test_results()` в том же формате.
  - Веб‑интерфейс теста (`test.html`) показывает все вопросы сразу
    (вместо переключения `prev/next`), но логика проверки идентична.

- **Результаты тестов**
  - Загрузка/сохранение результатов (`MainWindow.load_test_results/save_test_results`)
    реализованы в `web_app/storage.py`.
  - Фильтрация по имени ученика и материалу (`TestResultsScreen.filter_results`)
    перенесена в маршрут `/results`.

- **Классы и задания**
  - Учитель создаёт класс (`/classes/new`) и получает invite‑ссылку.
  - Ученик переходит по ссылке `/classes/join/<token>` и отправляет заявку.
  - Учитель принимает/отклоняет заявки в `/classes/<class_id>/requests`.
  - Учитель назначает тесты классу в `/classes/<class_id>/assignments/new`.
  - Ученик видит задания в `/classes` и `/assignments/my`.

- **Таймеры по вопросам**
  - На странице теста (`test.html`) собирается время по каждому вопросу.
  - В `TestResult` сохраняются:
    - `total_duration_sec` — общее время попытки;
    - `question_timings` и `details[].duration_sec` — время по вопросам.
  - Учитель видит это время в разборе результата.

- **AI‑пояснения для развёрнутых ответов**
  - Для `text_answer` используется проверка через DeepSeek/OpenRouter.
  - Пояснение ИИ (`details[].ai_reason`) показывается:
    - учителю — всегда;
    - ученику — только когда у материала включён показ правильных ответов.

- **Уведомления диалогов**
  - В списке `/dialogs` диалоги сортируются по последнему сообщению.
  - Непрочитанные диалоги подсвечиваются.
  - При открытии результата/чата диалог помечается как прочитанный.

## Запуск

1. Установите зависимости (из корня проекта `проект 2`):

   ```bash
   pip install -r requirements.txt
   ```

2. Данные веб‑версии хранятся в **`data/`** в корне проекта. При необходимости путь задаётся в `.env`
   через `APP_DATA_DIR`.

3. Запустите веб‑приложение:

   ```bash
   python -m web_app.app
   ```

   Либо из корня проекта: `python run_app.py`.

### Проверка текстовых ответов через OpenRouter (free)

1. Скопируйте `.env.example` в `.env` в корне проекта.
2. Укажите ключ OpenRouter (формат `sk-or-...`) из [OpenRouter](https://openrouter.ai/keys):

   ```env
   DEEPSEEK_API_KEY=ваш_ключ
   DEEPSEEK_API_URL=https://openrouter.ai/api/v1
   DEEPSEEK_MODEL=openrouter/free
   ```

Без ключа текстовые вопросы при сдаче теста будут засчитываться как неверные
(проверка только через API).

4. Откройте в браузере:

   - `http://127.0.0.1:5000/` — вход в систему (`admin` / `admin` по умолчанию).

## Примечания

- Веб‑версия фокусируется на просмотре материалов, прохождении тестов и работе
  с результатами. Полное редактирование материалов и тестов (как в диалогах
  `MaterialEditDialog` и `QuestionEditDialog`) может быть добавлено отдельно,
  но основная учебно‑тестирующая логика полностью перенесена.

