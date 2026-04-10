from __future__ import annotations

import os
import hashlib
import uuid
import json
from pathlib import Path
from typing import List, Optional

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename
import docx

from .logic import (
    authenticate,
    create_class_assignment,
    create_classroom,
    change_password,
    decide_class_join_request,
    compute_test_result,
    get_assignments_for_class,
    get_assignments_for_student,
    get_class_by_id,
    get_class_by_invite_token,
    get_class_member_usernames,
    get_class_requests_for_teacher,
    get_class_statistics,
    get_student_classes,
    get_teacher_classes,
    is_dialog_unread,
    mark_dialog_read,
    submit_class_join_request,
    decide_access_request,
    evaluate_password_strength,
    get_access_request_for_student,
    get_student_dashboard_stats,
    get_material_by_id,
    get_achievements_detail,
    get_user_progress,
    get_results_for_user,
    load_all_materials_ordered,
    register_user,
    submit_access_request,
    student_has_access,
    teacher_can_access_material,
    list_access_requests_for_teacher,
    update_user_last_seen,
    update_user_profile,
    user_exists,
    append_result_message,
    user_can_access_result_chat,
    get_user_display_name,
)
from .models import AccessRequestStatus, QuestionType, UserRole, TheoryMaterial, Question, QuestionTiming
from . import storage


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return

    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        if not key:
            continue
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        os.environ[key] = value


def create_app() -> Flask:
    app = Flask(__name__)
    project_root = Path(__file__).resolve().parents[1]
    _load_env_file(project_root / ".env")
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")

    @app.template_filter("user_display_name")
    def _user_display_name_filter(uname: str) -> str:
        return get_user_display_name(uname or "")

    theory_media_dir = storage.get_theory_materials_dir()
    theory_media_dir.mkdir(parents=True, exist_ok=True)
    images_dir = theory_media_dir

    @app.route("/static/theory_materials/<path:filename>")
    def serve_theory_materials_files(filename: str):
        return send_from_directory(str(theory_media_dir), filename)

    def _current_user():
        username = session.get("username")
        role = session.get("role")
        if not username or not role:
            return None
        return {"username": username, "role": UserRole(role)}

    def _require_login():
        if not _current_user():
            flash("Пожалуйста, войдите в систему.", "warning")
            return redirect(url_for("login"))
        return None

    def _default_home_redirect():
        user = _current_user()
        assert user is not None
        if user["role"] == UserRole.TEACHER:
            return redirect(url_for("teacher_profile"))
        return redirect(url_for("tests_catalog"))

    @app.route("/")
    def index():
        if _current_user():
            return _default_home_redirect()
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()

            if not username or not password:
                flash("Заполните все поля.", "danger")
            else:
                user = authenticate(username, password)
                if user:
                    session["username"] = user.username
                    session["role"] = user.role.value
                    update_user_last_seen(user.username)
                    flash("Успешный вход.", "success")
                    return _default_home_redirect()
                else:
                    flash("Неверное имя пользователя или пароль.", "danger")

        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Вы вышли из системы.", "info")
        return redirect(url_for("login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        password_info: Optional[dict] = None

        if request.method == "POST":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            role_str = request.form.get("role", "student")

            password_info = evaluate_password_strength(password)

            if not first_name or not last_name or not username or not password or not confirm_password:
                flash("Заполните все поля.", "danger")
            elif password != confirm_password:
                flash("Пароли не совпадают.", "danger")
            elif int(password_info["strength"]) < 3:
                flash("Пароль слишком слабый. Усильте его по рекомендациям.", "danger")
            elif user_exists(username):
                flash("Пользователь с таким именем уже существует.", "danger")
            else:
                role = UserRole.TEACHER if role_str == "teacher" else UserRole.STUDENT
                register_user(username, password, role, first_name=first_name, last_name=last_name)
                flash("Регистрация успешно завершена. Теперь можно войти.", "success")
                return redirect(url_for("login"))

        return render_template("register.html", password_info=password_info)

    @app.route("/theory")
    def theory():
        if (resp := _require_login()) is not None:
            return resp
        return _default_home_redirect()

    @app.route("/tests")
    def tests_catalog():
        user = _current_user()

        q = request.args.get("q", "").strip().lower()
        materials = list(load_all_materials_ordered())
        if q:
            materials = [m for m in materials if q in m.title.lower()]

        materials.sort(key=lambda m: (m.order, m.title))

        if user and user["role"] == UserRole.TEACHER:
            uname = user["username"]  # type: ignore[index]
            materials = [m for m in materials if teacher_can_access_material(m, uname)]

        access_map = {}
        if user and user["role"] == UserRole.STUDENT:
            username = user["username"]  # type: ignore[index]
            for m in materials:
                locked = bool(getattr(m, "is_closed", False)) and not student_has_access(username, m)
                req = get_access_request_for_student(username, m.material_id)
                access_map[m.material_id] = {
                    "locked": locked,
                    "status": (req.status.value if req else ""),
                }

        return render_template(
            "tests_catalog.html",
            materials=materials,
            q=request.args.get("q", ""),
            access_map=access_map,
        )

    @app.route("/topics")
    def topics_overview():
        if (resp := _require_login()) is not None:
            return resp

        q = request.args.get("q", "").strip().lower()
        pick_for_class = request.args.get("pick_for_class", "").strip()
        materials = [m for m in load_all_materials_ordered() if not getattr(m, "is_closed", False)]
        if q:
            materials = [m for m in materials if q in m.title.lower()]
        materials.sort(key=lambda m: (m.order, m.title))

        return render_template(
            "topics_overview.html",
            materials=materials,
            q=request.args.get("q", ""),
            pick_for_class=pick_for_class,
        )

    @app.route("/classes")
    def classes_home():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None

        if user["role"] == UserRole.TEACHER:
            classes = get_teacher_classes(user["username"])  # type: ignore[index]
            pending = get_class_requests_for_teacher(user["username"])  # type: ignore[index]
            return render_template(
                "classes_teacher.html",
                classes=classes,
                pending_count=len([r for r in pending if r.status == AccessRequestStatus.PENDING]),
            )

        classes = get_student_classes(user["username"])  # type: ignore[index]
        assignments = get_assignments_for_student(user["username"])  # type: ignore[index]
        return render_template(
            "classes_student.html",
            classes=classes,
            assignments=assignments,
        )

    @app.route("/classes/new", methods=["GET", "POST"])
    def classes_new():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Только учитель может создавать класс.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Введите название класса.", "danger")
            else:
                cls = create_classroom(user["username"], name)  # type: ignore[index]
                flash("Класс создан.", "success")
                return redirect(url_for("class_view", class_id=cls.class_id))
        return render_template("class_new.html")

    @app.route("/classes/join/<token>", methods=["GET", "POST"])
    def classes_join(token: str):
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.STUDENT:
            flash("Вступать в класс могут только ученики.", "danger")
            return _default_home_redirect()
        cls = get_class_by_invite_token(token)
        if not cls:
            flash("Ссылка-приглашение недействительна.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            req = submit_class_join_request(user["username"], cls.class_id)  # type: ignore[index]
            if req.status == AccessRequestStatus.APPROVED:
                flash("Вы уже состоите в этом классе.", "success")
            else:
                flash("Заявка отправлена учителю.", "success")
            return redirect(url_for("classes_home"))

        return render_template("class_join.html", classroom=cls)

    @app.route("/classes/<class_id>")
    def class_view(class_id: str):
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        cls = get_class_by_id(class_id)
        if not cls:
            flash("Класс не найден.", "danger")
            return redirect(url_for("classes_home"))

        if user["role"] == UserRole.TEACHER:
            if cls.owner_teacher_username != user["username"]:
                flash("Нет доступа к классу.", "danger")
                return redirect(url_for("classes_home"))
            stats = get_class_statistics(class_id)
            assignments = get_assignments_for_class(class_id)
            invite_link = url_for("classes_join", token=cls.invite_token, _external=True)
            return render_template(
                "class_view_teacher.html",
                classroom=cls,
                assignments=assignments,
                stats=stats,
                invite_link=invite_link,
            )

        student_classes = {c.class_id for c in get_student_classes(user["username"])}  # type: ignore[index]
        if class_id not in student_classes:
            flash("Вы не состоите в этом классе.", "danger")
            return redirect(url_for("classes_home"))
        assignments = [a for a in get_assignments_for_student(user["username"]) if a.class_id == class_id]  # type: ignore[index]
        return render_template("class_view_student.html", classroom=cls, assignments=assignments)

    @app.route("/classes/<class_id>/requests", methods=["GET", "POST"])
    def class_requests(class_id: str):
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Раздел доступен только учителю.", "danger")
            return _default_home_redirect()
        cls = get_class_by_id(class_id)
        if not cls or cls.owner_teacher_username != user["username"]:
            flash("Класс не найден или нет прав.", "danger")
            return redirect(url_for("classes_home"))

        if request.method == "POST":
            req_id = request.form.get("request_id", "")
            action = request.form.get("action", "")
            updated = decide_class_join_request(
                req_id,
                user["username"],  # type: ignore[index]
                approve=(action == "approve"),
            )
            if updated:
                flash("Решение сохранено.", "success")
            else:
                flash("Не удалось обработать заявку.", "danger")
            return redirect(url_for("class_requests", class_id=class_id))

        reqs = [r for r in get_class_requests_for_teacher(user["username"]) if r.class_id == class_id]  # type: ignore[index]
        return render_template("class_requests.html", classroom=cls, requests=reqs)

    @app.route("/classes/<class_id>/assignments/new", methods=["GET", "POST"])
    def class_assignment_new(class_id: str):
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Раздел доступен только учителю.", "danger")
            return _default_home_redirect()
        cls = get_class_by_id(class_id)
        if not cls or cls.owner_teacher_username != user["username"]:
            flash("Класс не найден или нет прав.", "danger")
            return redirect(url_for("classes_home"))

        materials = [m for m in storage.load_materials() if (m.tests and len(m.tests) > 0)]
        preselected_material_id = request.args.get("material_id", "").strip()
        if request.method == "POST":
            material_id = request.form.get("material_id", "").strip()
            due_at = request.form.get("due_at", "").strip()
            assignment = create_class_assignment(
                user["username"],  # type: ignore[index]
                class_id,
                material_id,
                due_at=due_at,
            )
            if assignment:
                flash("Задание выдано классу.", "success")
                return redirect(url_for("class_view", class_id=class_id))
            flash("Не удалось создать задание.", "danger")
        return render_template(
            "class_assignment_new.html",
            classroom=cls,
            materials=materials,
            preselected_material_id=preselected_material_id,
        )

    @app.route("/classes/<class_id>/assignments/<assignment_id>/results")
    def class_assignment_results(class_id: str, assignment_id: str):
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Раздел доступен только учителю.", "danger")
            return _default_home_redirect()

        cls = get_class_by_id(class_id)
        if not cls or cls.owner_teacher_username != user["username"]:
            flash("Класс не найден или нет прав.", "danger")
            return redirect(url_for("classes_home"))

        assignments = get_assignments_for_class(class_id)
        assignment = next((a for a in assignments if a.assignment_id == assignment_id), None)
        if not assignment:
            flash("Задание не найдено в этом классе.", "warning")
            return redirect(url_for("class_view", class_id=class_id))

        members = set(get_class_member_usernames(class_id))
        results = storage.load_test_results()
        assignment_results = [
            r
            for r in results
            if r.assignment_id == assignment_id and r.student_username in members
        ]
        assignment_results.sort(key=lambda r: r.date, reverse=True)

        return render_template(
            "class_assignment_results.html",
            classroom=cls,
            assignment=assignment,
            results=assignment_results,
            get_user_display_name=get_user_display_name,
        )

    @app.route("/assignments/my")
    def my_assignments():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.STUDENT:
            flash("Раздел доступен только ученику.", "warning")
            return _default_home_redirect()
        assignments = get_assignments_for_student(user["username"])  # type: ignore[index]
        return render_template("my_assignments.html", assignments=assignments)

    @app.route("/materials/<material_id>/view")
    def material_view(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None

        try:
            material = get_material_by_id(material_id)
        except Exception:
            flash("Материал не найден.", "danger")
            return _default_home_redirect()

        if user["role"] == UserRole.STUDENT:
            if not student_has_access(user["username"], material):  # type: ignore[index]
                flash("Нет доступа к этому материалу.", "warning")
                return _default_home_redirect()
        elif user["role"] == UserRole.TEACHER:
            if not teacher_can_access_material(material, user["username"]):  # type: ignore[index]
                flash("Закрытые материалы других учителей недоступны для просмотра.", "warning")
                return _default_home_redirect()
        else:
            flash("Роль не поддерживается.", "danger")
            return _default_home_redirect()

        is_owner = (
            getattr(material, "author", "admin") == user["username"]  # type: ignore[index]
            or user["username"] == "admin"  # type: ignore[index]
        )

        return render_template(
            "material_view.html",
            material=material,
            material_id=material_id,
            is_owner=is_owner,
        )

    @app.route("/materials/<material_id>/request_access", methods=["POST"])
    def request_access(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.STUDENT:
            flash("Подавать заявки могут только ученики.", "danger")
            return _default_home_redirect()

        all_materials = load_all_materials_ordered()
        material = next((m for m in all_materials if m.material_id == material_id), None)
        if not material:
            flash("Материал не найден.", "danger")
            return _default_home_redirect()

        if not getattr(material, "is_closed", False):
            flash("Эта тема не закрытая — доступ уже открыт.", "info")
            return _default_home_redirect()

        req = submit_access_request(user["username"], material)  # type: ignore[index]
        if req.status == AccessRequestStatus.APPROVED:
            flash("Доступ уже подтверждён.", "success")
        else:
            flash("Заявка отправлена учителю.", "success")
        return _default_home_redirect()

    @app.route("/materials/new", methods=["GET", "POST"])
    def material_new():
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Добавлять материалы может только учитель.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            images_raw = request.form.get("images", "")
            video_url = request.form.get("video_url", "").strip() or None
            show_correct = request.form.get("show_correct_answers") == "1"
            is_closed = request.form.get("is_closed") == "1"

            if not title or not content:
                flash("Название и содержимое материала обязательны.", "danger")
            else:
                all_materials = storage.load_materials()
                next_order = (max([m.order for m in all_materials], default=0) + 1) if all_materials else 1

                images = [
                    line.strip()
                    for line in images_raw.splitlines()
                    if line.strip()
                ]

                material_key = f"{next_order}:{title}"
                material_id = hashlib.md5(material_key.encode("utf-8")).hexdigest()

                new_material = TheoryMaterial(
                    material_id=material_id,
                    author=user["username"],  # type: ignore[index]
                    title=title,
                    content=content,
                    images=images,
                    order=next_order,
                    video_url=video_url,
                    is_closed=is_closed,
                    show_correct_answers=show_correct,
                )

                all_materials.append(new_material)
                storage.save_materials(all_materials)

                flash("Материал успешно добавлен.", "success")
                return redirect(url_for("teacher_profile"))

        return render_template("material_form.html", mode="new", material=None)

    @app.route("/materials/<material_id>/edit", methods=["GET", "POST"])
    def material_edit(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Редактировать материалы может только учитель.", "danger")
            return _default_home_redirect()

        all_materials = storage.load_materials()
        try:
            current_material = next(m for m in all_materials if m.material_id == material_id)
            global_idx = all_materials.index(current_material)
        except StopIteration:
            flash("Материал не найден.", "danger")
            return _default_home_redirect()
        if (
            getattr(current_material, "author", "admin") != user["username"]  # type: ignore[index]
            and user["username"] != "admin"  # type: ignore[index]
        ):
            flash("Редактировать материал может только автор.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            title = request.form.get("title", "").strip()
            content = request.form.get("content", "").strip()
            images_raw = request.form.get("images", "")
            video_url = request.form.get("video_url", "").strip() or None
            show_correct = request.form.get("show_correct_answers") == "1"
            is_closed = request.form.get("is_closed") == "1"

            if not title or not content:
                flash("Название и содержимое материала обязательны.", "danger")
            else:
                images = [
                    line.strip()
                    for line in images_raw.splitlines()
                    if line.strip()
                ]

                current_material.title = title
                current_material.content = content
                current_material.images = images
                current_material.video_url = video_url
                current_material.show_correct_answers = show_correct
                current_material.is_closed = is_closed

                all_materials[global_idx] = current_material
                storage.save_materials(all_materials)

                flash("Материал успешно обновлён.", "success")
                return _default_home_redirect()

        return render_template(
            "material_form.html",
            mode="edit",
            material=current_material,
            material_id=material_id,
        )

    @app.route("/materials/upload_word", methods=["GET", "POST"])
    def material_upload_word():
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Импортировать материалы из Word может только учитель.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            file = request.files.get("file")
            title = request.form.get("title", "").strip()
            level_str = request.form.get("level", "")

            if not file or file.filename == "":
                flash("Выберите файл Word (.docx).", "danger")
                return redirect(request.url)

            if not file.filename.lower().endswith(".docx"):
                flash("Поддерживаются только файлы формата .docx.", "danger")
                return redirect(request.url)

            try:
                document = docx.Document(file)
            except Exception as e:
                flash(f"Не удалось прочитать файл Word: {e}", "danger")
                return redirect(request.url)

            paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
            html_content = "".join(f"<p>{p}</p>" for p in paragraphs) or "Без содержания"

            image_names: List[str] = []
            for rel in document.part._rels.values():
                if getattr(rel, "target_mode", "Internal") != "Internal":
                    continue
                try:
                    target = rel.target_part
                except ValueError:
                    continue
                content_type = getattr(target, "content_type", "")
                if content_type.startswith("image/"):
                    ext = content_type.split("/")[-1]
                    safe_base = secure_filename(os.path.splitext(file.filename)[0]) or "material"
                    filename = f"{safe_base}_{len(image_names)+1}.{ext}"
                    out_path = images_dir / filename
                    with out_path.open("wb") as img_file:
                        img_file.write(target.blob)
                    image_names.append(filename)

            if not title:
                title = os.path.splitext(file.filename)[0]

            all_materials = storage.load_materials()
            next_order = (max([m.order for m in all_materials], default=0) + 1) if all_materials else 1

            new_material = TheoryMaterial(
                material_id=hashlib.md5(f"{next_order}:{title}".encode("utf-8")).hexdigest(),
                author=user["username"],  # type: ignore[index]
                title=title,
                content=html_content,
                images=image_names,
                order=next_order,
                video_url=None,
            )

            all_materials.append(new_material)
            storage.save_materials(all_materials)

            flash("Материал успешно импортирован из Word.", "success")
            return redirect(url_for("teacher_profile"))

        return render_template("upload_word.html")

    @app.route("/materials/upload_editor_image", methods=["POST"])
    def material_upload_editor_image():
        if not _current_user():
            return jsonify({"ok": False, "error": "Требуется вход"}), 401
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            return jsonify({"ok": False, "error": "Доступно только учителям"}), 403

        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"ok": False, "error": "Файл не выбран"}), 400

        mimetype = (f.mimetype or "").lower()
        if not mimetype.startswith("image/"):
            return jsonify({"ok": False, "error": "Нужен файл изображения"}), 400

        orig = secure_filename(f.filename)
        if not orig:
            return jsonify({"ok": False, "error": "Некорректное имя файла"}), 400

        ext = Path(orig).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            return jsonify({"ok": False, "error": "Допустимы: PNG, JPEG, GIF, WebP"}), 400

        unique_name = f"{uuid.uuid4().hex}{ext}"
        out_path = images_dir / unique_name
        f.save(str(out_path))

        url = url_for("static", filename=f"theory_materials/{unique_name}")
        return jsonify({"ok": True, "url": url})

    @app.route("/materials/<material_id>/questions")
    def questions_list(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Редактировать вопросы может только учитель.", "danger")
            return _default_home_redirect()

        all_materials = storage.load_materials()
        material = next((m for m in all_materials if m.material_id == material_id), None)
        if not material:
            flash("Материал не найден.", "danger")
            return _default_home_redirect()
        global_idx = all_materials.index(material)
        if (
            getattr(material, "author", "admin") != user["username"]  # type: ignore[index]
            and user["username"] != "admin"  # type: ignore[index]
        ):
            flash("Редактировать вопросы может только автор материала.", "danger")
            return _default_home_redirect()

        return render_template(
            "questions_list.html",
            material=material,
            material_id=material_id,
            global_idx=global_idx,
        )

    def _get_material_by_id(material_id: str):
        all_materials = storage.load_materials()
        for i, m in enumerate(all_materials):
            if m.material_id == material_id:
                return all_materials, i, m
        raise IndexError("Материал не найден")

    def _parse_question_form():
        text = request.form.get("question_text", "").strip()
        qtype_str = request.form.get("question_type", "single_choice")
        options_raw = request.form.get("options", "")
        correct_raw = request.form.get("correct_answers", "")
        explanation = request.form.get("explanation", "").strip()

        if not text:
            raise ValueError("Введите текст вопроса.")

        try:
            qtype = QuestionType(qtype_str)
        except ValueError:
            qtype = QuestionType.SINGLE_CHOICE

        options: Optional[List[str]] = None
        if qtype in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
            options_list = [
                line.strip() for line in options_raw.splitlines() if line.strip()
            ]
            if not options_list:
                raise ValueError("Добавьте хотя бы один вариант ответа.")
            options = options_list
        else:
            options_list = []

        correct_answers = [
            line.strip() for line in correct_raw.splitlines() if line.strip()
        ]
        if not correct_answers:
            raise ValueError("Добавьте хотя бы один правильный ответ.")

        if qtype in (QuestionType.SINGLE_CHOICE, QuestionType.MULTIPLE_CHOICE):
            for ans in correct_answers:
                if ans not in options_list:
                    raise ValueError(
                        f"Правильный ответ «{ans}» отсутствует среди вариантов."
                    )

        if qtype == QuestionType.SINGLE_CHOICE and len(correct_answers) != 1:
            raise ValueError(
                "Для вопроса с одним вариантом ответа должен быть ровно один правильный ответ."
            )

        if qtype == QuestionType.MULTIPLE_CHOICE and len(correct_answers) < 2:
            raise ValueError(
                "Для вопроса с несколькими вариантами ответа нужно выбрать минимум два правильных варианта."
            )

        return text, qtype, options, correct_answers, explanation

    @app.route("/materials/<material_id>/questions/new", methods=["GET", "POST"])
    def question_new(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Редактировать вопросы может только учитель.", "danger")
            return _default_home_redirect()

        try:
            all_materials, global_idx, material = _get_material_by_id(material_id)
        except IndexError as e:
            flash(str(e), "danger")
            return _default_home_redirect()

        if (
            getattr(material, "author", "admin") != user["username"]  # type: ignore[index]
            and user["username"] != "admin"  # type: ignore[index]
        ):
            flash("Добавлять вопросы может только автор материала.", "danger")
            return redirect(url_for("questions_list", material_id=material_id))

        if request.method == "POST":
            try:
                text, qtype, options, correct_answers, explanation = _parse_question_form()
            except ValueError as e:
                flash(str(e), "danger")
            else:
                material.tests.append(
                    Question(
                        question_text=text,
                        question_type=qtype,
                        correct_answers=correct_answers,
                        options=options,
                        explanation=explanation,
                    )
                )
                all_materials[global_idx] = material
                storage.save_materials(all_materials)
                flash("Вопрос добавлен.", "success")
                return redirect(
                    url_for("questions_list", material_id=material_id)
                )

        return render_template(
            "question_form.html",
            mode="new",
            material_id=material_id,
            question=None,
        )

    @app.route(
        "/materials/<material_id>/questions/<int:q_index>/edit",
        methods=["GET", "POST"],
    )
    def question_edit(material_id: str, q_index: int):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Редактировать вопросы может только учитель.", "danger")
            return _default_home_redirect()

        try:
            all_materials, global_idx, material = _get_material_by_id(material_id)
        except IndexError as e:
            flash(str(e), "danger")
            return _default_home_redirect()
        
        if (
            getattr(material, "author", "admin") != user["username"]  # type: ignore[index]
            and user["username"] != "admin"  # type: ignore[index]
        ):
            flash("Редактировать вопросы может только автор материала.", "danger")
            return redirect(url_for("questions_list", material_id=material_id))

        if q_index < 0 or q_index >= len(material.tests):
            flash("Вопрос не найден.", "danger")
            return redirect(url_for("questions_list", material_id=material_id))

        question = material.tests[q_index]

        if request.method == "POST":
            try:
                text, qtype, options, correct_answers, explanation = _parse_question_form()
            except ValueError as e:
                flash(str(e), "danger")
            else:
                question.question_text = text
                question.question_type = qtype
                question.options = options
                question.correct_answers = correct_answers
                question.explanation = explanation

                material.tests[q_index] = question
                all_materials[global_idx] = material
                storage.save_materials(all_materials)
                flash("Вопрос обновлён.", "success")
                return redirect(
                    url_for("questions_list", material_id=material_id)
                )

        options_text = ""
        if question.options:
            options_text = "\n".join(question.options)
        correct_text = "\n".join(question.correct_answers)

        return render_template(
            "question_form.html",
            mode="edit",
            material_id=material_id,
            question=question,
            options_text=options_text,
            correct_text=correct_text,
            q_index=q_index,
        )

    @app.route(
        "/materials/<material_id>/questions/<int:q_index>/delete",
        methods=["POST"],
    )
    def question_delete(material_id: str, q_index: int):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Редактировать вопросы может только учитель.", "danger")
            return _default_home_redirect()

        try:
            all_materials, global_idx, material = _get_material_by_id(material_id)
        except IndexError as e:
            flash(str(e), "danger")
            return _default_home_redirect()
        
        if (
            getattr(material, "author", "admin") != user["username"]  # type: ignore[index]
            and user["username"] != "admin"  # type: ignore[index]
        ):
            flash("Удалять вопросы может только автор материала.", "danger")
            return redirect(url_for("questions_list", material_id=material_id))

        if 0 <= q_index < len(material.tests):
            material.tests.pop(q_index)
            all_materials[global_idx] = material
            storage.save_materials(all_materials)
            flash("Вопрос удалён.", "success")

        return redirect(url_for("questions_list", material_id=material_id))

    @app.route(
        "/test/<material_id>",
        methods=["GET", "POST"],
    )
    def test(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        try:
            material = get_material_by_id(material_id)
        except Exception:
            flash("Материал не найден.", "danger")
            return _default_home_redirect()

        user = _current_user()
        assert user is not None
        if user["role"] == UserRole.STUDENT:
            if not student_has_access(user["username"], material):  # type: ignore[index]
                flash("Это закрытая тема. Сначала подайте заявку и дождитесь подтверждения учителя.", "warning")
                return _default_home_redirect()
        elif user["role"] == UserRole.TEACHER:
            if not teacher_can_access_material(material, user["username"]):  # type: ignore[index]
                flash("Проходить тест по чужому закрытому материалу нельзя.", "warning")
                return _default_home_redirect()
        else:
            flash("Роль не поддерживается.", "danger")
            return _default_home_redirect()

        if not material.tests:
            flash("У этого материала нет тестовых вопросов.", "warning")
            return _default_home_redirect()

        assignment_id = request.args.get("assignment_id", "").strip()

        if request.method == "POST":
            user_answers: List[object] = []
            for i, q in enumerate(material.tests):
                field_name = f"q{i}"
                if q.question_type == QuestionType.MULTIPLE_CHOICE:
                    selected = request.form.getlist(field_name)
                    user_answers.append(selected)
                else:
                    user_answers.append(request.form.get(field_name, "").strip())

            timings_json = request.form.get("question_timings", "").strip()
            parsed_timings: List[QuestionTiming] = []
            if timings_json:
                try:
                    raw = json.loads(timings_json)
                    if isinstance(raw, list):
                        for item in raw:
                            parsed_timings.append(
                                QuestionTiming(
                                    question_index=int(item.get("question_index", 0)),
                                    duration_sec=float(item.get("duration_sec", 0.0)),
                                )
                            )
                except Exception:
                    parsed_timings = []

            user = _current_user()
            result = compute_test_result(
                material=material,
                user_answers=user_answers,
                student_username=user["username"],  # type: ignore[index]
                question_timings=parsed_timings,
                assignment_id=assignment_id,
            )
            teacher_u = getattr(material, "author", "admin")
            return render_template(
                "test_result.html",
                result=result,
                show_correct_answers=bool(getattr(material, "show_correct_answers", True)),
                chat_teacher_username=teacher_u,
                chat_teacher_display_name=get_user_display_name(teacher_u),
                chat_student_display_name=get_user_display_name(result.student_username),
            )

        return render_template(
            "test.html",
            material=material,
            material_id=material_id,
            assignment_id=assignment_id,
        )

    @app.route("/test/<material_id>/preview")
    def test_preview(material_id: str):
        if (resp := _require_login()) is not None:
            return resp

        try:
            material = get_material_by_id(material_id)
        except Exception:
            flash("Материал не найден.", "danger")
            return _default_home_redirect()

        user = _current_user()
        assert user is not None
        if user["role"] == UserRole.STUDENT:
            if not student_has_access(user["username"], material):  # type: ignore[index]
                flash("Это закрытая тема. Сначала подайте заявку и дождитесь подтверждения учителя.", "warning")
                return _default_home_redirect()
        elif user["role"] == UserRole.TEACHER:
            if not teacher_can_access_material(material, user["username"]):  # type: ignore[index]
                flash("Просматривать тест по чужому закрытому материалу нельзя.", "warning")
                return _default_home_redirect()
        else:
            flash("Роль не поддерживается.", "danger")
            return _default_home_redirect()

        if not material.tests:
            flash("У этого материала нет тестовых вопросов.", "warning")
            return _default_home_redirect()

        pick_for_class = request.args.get("pick_for_class", "").strip()
        q = request.args.get("q", "").strip()
        return render_template(
            "test_preview.html",
            material=material,
            material_id=material_id,
            pick_for_class=pick_for_class,
            q=q,
        )

    @app.route("/results")
    def results():
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        is_teacher = user["role"] == UserRole.TEACHER

        results_list = get_results_for_user(
            username=user["username"],  # type: ignore[index]
            is_teacher=is_teacher,
        )

        search = request.args.get("search", "").lower()
        if search:
            results_list = [
                r
                for r in results_list
                if search in r.student_username.lower()
                or search in r.material_title.lower()
            ]

        return render_template(
            "results.html",
            results=results_list,
            is_teacher=is_teacher,
            search=search,
        )

    @app.route("/dialogs")
    def all_dialogs():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] not in (UserRole.STUDENT, UserRole.TEACHER):
            flash("Раздел недоступен.", "danger")
            return _default_home_redirect()

        is_teacher = user["role"] == UserRole.TEACHER
        results_list = get_results_for_user(
            user["username"],  # type: ignore[index]
            is_teacher,
        )
        rows: List[dict] = []
        for r in results_list:
            msgs = list(r.result_messages or [])
            if not msgs:
                continue
            last = msgs[-1]
            t = (last.get("text") or "").strip()
            preview = t if len(t) <= 120 else (t[:120] + "…")
            last_message_at = str(last.get("created_at") or "")
            rows.append(
                {
                    "result": r,
                    "student_display_name": get_user_display_name(r.student_username),
                    "msg_count": len(msgs),
                    "last_preview": preview,
                    "last_message_at": last_message_at,
                    "is_unread": is_dialog_unread(r, user["username"]),  # type: ignore[index]
                }
            )

        rows.sort(
            key=lambda row: row["last_message_at"] or row["result"].date,
            reverse=True,
        )

        return render_template(
            "dialogs.html",
            rows=rows,
            is_teacher=is_teacher,
        )

    @app.route("/statistics")
    def statistics():
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.STUDENT:
            flash("Статистика доступна только ученикам.", "warning")
            return _default_home_redirect()

        stats = get_student_dashboard_stats(user["username"])  # type: ignore[index]
        achievement_details = get_achievements_detail(user["username"])  # type: ignore[index]
        return render_template(
            "statistics.html",
            stats=stats,
            achievement_details=achievement_details,
        )

    @app.route("/dashboard")
    def dashboard_redirect():
        return redirect(url_for("statistics"))

    @app.route("/api/user_progress")
    def api_user_progress():
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.STUDENT:
            return jsonify([])

        return jsonify(get_user_progress(user["username"]))  # type: ignore[index]

    def _get_user_full(username: str):
        for u in storage.load_users():
            if u.username == username:
                return u
        return None

    @app.route("/student/profile")
    def student_profile():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.STUDENT:
            flash("Страница доступна только ученикам.", "danger")
            return _default_home_redirect()

        u = _get_user_full(user["username"])  # type: ignore[index]
        stats = get_student_dashboard_stats(user["username"])  # type: ignore[index]
        return render_template("student_profile.html", u=u, stats=stats)

    @app.route("/teacher/profile")
    def teacher_profile():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Страница доступна только учителям.", "danger")
            return _default_home_redirect()

        u = _get_user_full(user["username"])  # type: ignore[index]
        materials = [m for m in storage.load_materials() if getattr(m, "author", "admin") == user["username"]]  # type: ignore[index]
        return render_template(
            "teacher_profile.html",
            u=u,
            materials=materials,
        )

    @app.route("/profile/edit", methods=["GET", "POST"])
    def profile_edit():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        u = _get_user_full(user["username"])  # type: ignore[index]
        if not u:
            flash("Пользователь не найден.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            if not first_name or not last_name:
                flash("Заполните имя и фамилию.", "danger")
            else:
                update_user_profile(u.username, first_name, last_name)
                flash("Профиль обновлён.", "success")
                return redirect(url_for("student_profile" if user["role"] == UserRole.STUDENT else "teacher_profile"))

        return render_template("profile_edit.html", u=u)

    @app.route("/profile/password", methods=["GET", "POST"])
    def profile_password():
        if (resp := _require_login()) is not None:
            return resp
        user = _current_user()
        assert user is not None
        if request.method == "POST":
            old_pw = request.form.get("old_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")
            if not old_pw or not new_pw or not confirm_pw:
                flash("Заполните все поля.", "danger")
            elif new_pw != confirm_pw:
                flash("Пароли не совпадают.", "danger")
            else:
                info = evaluate_password_strength(new_pw)
                if int(info["strength"]) < 3:
                    flash("Новый пароль слишком слабый.", "danger")
                elif not change_password(user["username"], old_pw, new_pw):  # type: ignore[index]
                    flash("Старый пароль неверен.", "danger")
                else:
                    flash("Пароль обновлён.", "success")
                    return redirect(url_for("student_profile" if user["role"] == UserRole.STUDENT else "teacher_profile"))

        return render_template("change_password.html")

    @app.route("/results/<result_id>")
    def result_view(result_id: str):
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None

        all_results = storage.load_test_results()
        result = next((r for r in all_results if r.result_id == result_id), None)
        if not result:
            flash("Результат не найден.", "danger")
            return redirect(url_for("results"))

        if user["role"] == UserRole.STUDENT and result.student_username != user["username"]:  # type: ignore[index]
            flash("Нет доступа к этому результату.", "danger")
            return redirect(url_for("results"))

        show_correct_answers = True
        material = next(
            (m for m in storage.load_materials() if m.title == result.material_title),
            None,
        )
        teacher_u = "admin"
        if material is not None:
            show_correct_answers = bool(getattr(material, "show_correct_answers", True))
            teacher_u = getattr(material, "author", "admin")

        mark_dialog_read(result.result_id, user["username"])  # type: ignore[index]

        return render_template(
            "result_view.html",
            result=result,
            show_correct_answers=show_correct_answers,
            chat_teacher_username=teacher_u,
            chat_teacher_display_name=get_user_display_name(teacher_u),
            chat_student_display_name=get_user_display_name(result.student_username),
        )

    @app.route("/results/<result_id>/messages", methods=["GET", "POST"])
    def api_result_messages(result_id: str):
        user = _current_user()
        if not user:
            return jsonify({"ok": False, "error": "Требуется вход в систему."}), 401

        all_results = storage.load_test_results()
        result = next((r for r in all_results if r.result_id == result_id), None)
        if not result:
            return jsonify({"ok": False, "error": "Результат не найден."}), 404
        if not user_can_access_result_chat(
            user["username"],  # type: ignore[index]
            user["role"],  # type: ignore[index]
            result,
        ):
            return jsonify({"ok": False, "error": "Нет доступа к этой переписке."}), 403

        if request.method == "GET":
            mark_dialog_read(result_id, user["username"])  # type: ignore[index]
            raw = list(result.result_messages or [])
            enriched = []
            for m in raw:
                d = dict(m)
                if not (d.get("author_display_name") or "").strip():
                    d["author_display_name"] = get_user_display_name(
                        str(d.get("author_username", ""))
                    )
                enriched.append(d)
            return jsonify({"ok": True, "messages": enriched})

        payload = request.get_json(silent=True) or {}
        text = payload.get("text", "")
        ok, err, msg = append_result_message(
            result_id,
            user["username"],  # type: ignore[index]
            user["role"],  # type: ignore[index]
            text,
        )
        if not ok:
            return jsonify({"ok": False, "error": err}), 400
        return jsonify({"ok": True, "message": msg})

    @app.route("/access_requests", methods=["GET", "POST"])
    def access_requests():
        if (resp := _require_login()) is not None:
            return resp

        user = _current_user()
        assert user is not None
        if user["role"] != UserRole.TEACHER:
            flash("Раздел доступен только учителю.", "danger")
            return _default_home_redirect()

        if request.method == "POST":
            request_id = request.form.get("request_id", "")
            action = request.form.get("action", "")
            if not request_id or action not in ("approve", "reject"):
                flash("Некорректный запрос.", "danger")
                return redirect(url_for("access_requests"))

            updated = decide_access_request(
                request_id=request_id,
                decided_by=user["username"],  # type: ignore[index]
                approve=(action == "approve"),
            )
            if updated:
                flash("Решение сохранено.", "success")
            else:
                flash("Заявка не найдена или у вас нет прав на эту тему.", "danger")
            return redirect(url_for("access_requests"))

        items = list_access_requests_for_teacher(user["username"])  # type: ignore[index]
        return render_template("access_requests.html", requests=items)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=5000, debug=True)

