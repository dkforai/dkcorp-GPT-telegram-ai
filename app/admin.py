from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, load_settings
from app.database import Database, Membership, User
from app.document_ingestion import MAX_UPLOAD_BYTES, extract_uploaded_document
from app.role_profiles import load_role_profiles


logger = logging.getLogger(__name__)
SESSION_COOKIE = "dk_admin_session"
SESSION_MAX_AGE = 8 * 60 * 60


class LoginLimiter:
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, client_id: str) -> bool:
        now = time.time()
        with self._lock:
            attempts = self._attempts[client_id]
            while attempts and now - attempts[0] > self.window_seconds:
                attempts.popleft()
            return len(attempts) >= self.max_attempts

    def failure(self, client_id: str) -> None:
        with self._lock:
            self._attempts[client_id].append(time.time())

    def reset(self, client_id: str) -> None:
        with self._lock:
            self._attempts.pop(client_id, None)


def create_admin_app(settings: Settings, database: Database) -> FastAPI:
    root = settings.project_root.resolve()
    templates = Jinja2Templates(directory=str(root / "templates"))
    limiter = LoginLimiter()
    role_profiles = load_role_profiles(settings.role_profiles_file)
    profile_options = [
        {"id": profile.profile_id, "label": profile.label}
        for profile in role_profiles.by_id.values()
    ]
    app = FastAPI(
        title="DK Corp AI Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(root / "static" / "admin")),
        name="admin-static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/", include_in_schema=False)
    async def root_redirect() -> RedirectResponse:
        return RedirectResponse("/admin", status_code=303)

    @app.get("/admin/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _is_authenticated(request, settings):
            return RedirectResponse("/admin", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            context={"error": ""},
        )

    @app.post("/admin/login", response_class=HTMLResponse)
    async def login(request: Request):
        client_id = request.client.host if request.client else "unknown"
        if limiter.blocked(client_id):
            return templates.TemplateResponse(
                request=request,
                name="admin/login.html",
                context={
                    "error": "Terlalu banyak percobaan. Tunggu 5 menit.",
                },
                status_code=429,
            )

        form = await request.form()
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        username_ok = hmac.compare_digest(username, settings.admin_username)
        password_ok = hmac.compare_digest(password, settings.admin_password)
        if not (username_ok and password_ok):
            limiter.failure(client_id)
            return templates.TemplateResponse(
                request=request,
                name="admin/login.html",
                context={"error": "Username atau password tidak valid."},
                status_code=401,
            )

        limiter.reset(client_id)
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            _create_session_token(settings),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=settings.admin_cookie_secure,
            samesite="lax",
            path="/admin",
        )
        return response

    @app.get("/admin/logout")
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/admin/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/admin")
        return response

    @app.get("/admin", response_class=HTMLResponse)
    async def dashboard(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/dashboard.html",
            context={
                "active_page": "dashboard",
                "stats": database.get_admin_dashboard_stats(),
                "companies": database.list_companies_admin()[:5],
                "admin_username": settings.admin_username,
            },
        )

    @app.get("/admin/companies", response_class=HTMLResponse)
    async def companies(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        rows = database.list_companies_admin()
        knowledge_summary = {
            str(item["company_id"]): item
            for item in database.list_company_knowledge_summary_admin()
        }
        for row in rows:
            summary = knowledge_summary.get(str(row["company_id"]), {})
            if int(summary.get("published_document_count", 0)):
                row["knowledge_count"] = int(
                    summary.get("live_document_count", 0)
                )
                row["knowledge_source"] = "Database live"
            else:
                row["knowledge_count"] = _markdown_count(
                    root, str(row.get("knowledge_dir", ""))
                )
                row["knowledge_source"] = "File transisi"
        return templates.TemplateResponse(
            request=request,
            name="admin/companies.html",
            context={
                "active_page": "companies",
                "companies": rows,
                "admin_username": settings.admin_username,
                "csrf_token": _csrf_token(request, settings),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get("/admin/companies/new", response_class=HTMLResponse)
    async def new_company(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/company_form.html",
            context=_company_form_context(
                request,
                settings,
                mode="create",
                company_id="",
                name="",
                active=True,
            ),
        )

    @app.post("/admin/companies", response_class=HTMLResponse)
    async def create_company(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        name = str(form.get("name", ""))
        active = form.get("active") == "1"
        try:
            database.create_company(
                "", name, actor=settings.admin_username, active=active
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/company_form.html",
                context=_company_form_context(
                    request,
                    settings,
                    mode="create",
                    company_id="",
                    name=name,
                    active=active,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _companies_redirect("Company berhasil ditambahkan")

    @app.get("/admin/companies/{company_id}/edit", response_class=HTMLResponse)
    async def edit_company(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        company = database.get_company_admin(company_id)
        if company is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/company_form.html",
            context=_company_form_context(
                request,
                settings,
                mode="edit",
                company_id=company.company_id,
                name=company.name,
                active=company.active,
                profile_file=company.profile_file,
                instruction_file=company.instruction_file,
                knowledge_dir=company.knowledge_dir,
            ),
        )

    @app.post("/admin/companies/{company_id}", response_class=HTMLResponse)
    async def update_company(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        company = database.get_company_admin(company_id)
        if company is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        name = str(form.get("name", ""))
        try:
            updated = database.update_company(
                company_id, name, actor=settings.admin_username
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/company_form.html",
                context=_company_form_context(
                    request,
                    settings,
                    mode="edit",
                    company_id=company.company_id,
                    name=name,
                    active=company.active,
                    profile_file=company.profile_file,
                    instruction_file=company.instruction_file,
                    knowledge_dir=company.knowledge_dir,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _companies_redirect(f"{updated.name} berhasil diperbarui")

    @app.post("/admin/companies/{company_id}/status")
    async def update_company_status(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        target = str(form.get("active", ""))
        if target not in {"0", "1"}:
            return _companies_redirect(error="Status company tidak valid")
        try:
            company = database.set_company_active(
                company_id, target == "1", actor=settings.admin_username
            )
        except ValueError as exc:
            return _companies_redirect(error=str(exc))
        status = "diaktifkan" if company.active else "dinonaktifkan"
        return _companies_redirect(f"{company.name} berhasil {status}")

    @app.get("/admin/users", response_class=HTMLResponse)
    async def users(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/users.html",
            context={
                "active_page": "users",
                "users": database.list_users_admin(),
                "admin_username": settings.admin_username,
                "csrf_token": _csrf_token(request, settings),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get("/admin/users/new", response_class=HTMLResponse)
    async def new_user(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/user_form.html",
            context=_user_form_context(
                request,
                settings,
                database,
                profile_options,
                mode="create",
            ),
        )

    @app.post("/admin/users", response_class=HTMLResponse)
    async def create_user(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        values = _user_form_values(form)
        try:
            user = database.create_user_with_membership(
                values["telegram_id"],
                values["name"],
                values["company_id"],
                values["job_title"],
                values["division"],
                values["role_level"],
                values["communication_profile"],
                values["custom_instruction"],
                actor=settings.admin_username,
                active=values["active"] == "1",
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/user_form.html",
                context=_user_form_context(
                    request,
                    settings,
                    database,
                    profile_options,
                    mode="create",
                    values=values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _users_redirect(f"{user.name} berhasil ditambahkan")

    @app.get("/admin/users/{telegram_id}/edit", response_class=HTMLResponse)
    async def edit_user(request: Request, telegram_id: int):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        user = database.get_user_admin(telegram_id)
        if user is None:
            return HTMLResponse("User tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/user_form.html",
            context=_user_form_context(
                request,
                settings,
                database,
                profile_options,
                mode="edit",
                user=user,
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post("/admin/users/{telegram_id}", response_class=HTMLResponse)
    async def update_user(request: Request, telegram_id: int):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        user = database.get_user_admin(telegram_id)
        if user is None:
            return HTMLResponse("User tidak ditemukan.", status_code=404)
        try:
            updated = database.update_user(
                telegram_id, str(form.get("name", "")), actor=settings.admin_username
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/user_form.html",
                context=_user_form_context(
                    request,
                    settings,
                    database,
                    profile_options,
                    mode="edit",
                    user=user,
                    values={"name": str(form.get("name", ""))},
                    error=str(exc),
                ),
                status_code=400,
            )
        return _user_detail_redirect(
            telegram_id, notice=f"{updated.name} berhasil diperbarui"
        )

    @app.post("/admin/users/{telegram_id}/status")
    async def update_user_status(request: Request, telegram_id: int):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        target = str(form.get("active", ""))
        if target not in {"0", "1"}:
            return _users_redirect(error="Status user tidak valid")
        try:
            user = database.set_user_active(
                telegram_id, target == "1", actor=settings.admin_username
            )
        except ValueError as exc:
            return _users_redirect(error=str(exc))
        status = "diaktifkan" if user.active else "dinonaktifkan"
        return _users_redirect(f"{user.name} berhasil {status}")

    @app.get(
        "/admin/users/{telegram_id}/memberships/new", response_class=HTMLResponse
    )
    async def new_membership(request: Request, telegram_id: int):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        user = database.get_user_admin(telegram_id)
        if user is None:
            return HTMLResponse("User tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/membership_form.html",
            context=_membership_form_context(
                request,
                settings,
                database,
                profile_options,
                user=user,
                mode="create",
            ),
        )

    @app.post("/admin/users/{telegram_id}/memberships", response_class=HTMLResponse)
    async def create_membership(request: Request, telegram_id: int):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        user = database.get_user_admin(telegram_id)
        if user is None:
            return HTMLResponse("User tidak ditemukan.", status_code=404)
        values = _membership_form_values(form)
        try:
            membership = database.create_membership(
                telegram_id,
                values["company_id"],
                values["job_title"],
                values["division"],
                values["role_level"],
                values["communication_profile"],
                values["custom_instruction"],
                is_default=values["is_default"] == "1",
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/membership_form.html",
                context=_membership_form_context(
                    request,
                    settings,
                    database,
                    profile_options,
                    user=user,
                    mode="create",
                    values=values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _user_detail_redirect(
            telegram_id,
            notice=f"Membership {membership.company_name} berhasil ditambahkan",
        )

    @app.get(
        "/admin/users/{telegram_id}/memberships/{company_id}/edit",
        response_class=HTMLResponse,
    )
    async def edit_membership(
        request: Request, telegram_id: int, company_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        user = database.get_user_admin(telegram_id)
        membership = database.get_membership_admin(telegram_id, company_id)
        if user is None or membership is None:
            return HTMLResponse("Membership tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/membership_form.html",
            context=_membership_form_context(
                request,
                settings,
                database,
                profile_options,
                user=user,
                mode="edit",
                membership=membership,
            ),
        )

    @app.post(
        "/admin/users/{telegram_id}/memberships/{company_id}",
        response_class=HTMLResponse,
    )
    async def update_membership(
        request: Request, telegram_id: int, company_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        user = database.get_user_admin(telegram_id)
        membership = database.get_membership_admin(telegram_id, company_id)
        if user is None or membership is None:
            return HTMLResponse("Membership tidak ditemukan.", status_code=404)
        values = _membership_form_values(form, company_id=company_id)
        try:
            updated = database.update_membership(
                telegram_id,
                company_id,
                values["job_title"],
                values["division"],
                values["role_level"],
                values["communication_profile"],
                values["custom_instruction"],
                is_default=values["is_default"] == "1",
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/membership_form.html",
                context=_membership_form_context(
                    request,
                    settings,
                    database,
                    profile_options,
                    user=user,
                    mode="edit",
                    membership=membership,
                    values=values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _user_detail_redirect(
            telegram_id,
            notice=f"Membership {updated.company_name} berhasil diperbarui",
        )

    @app.post(
        "/admin/users/{telegram_id}/memberships/{company_id}/status"
    )
    async def update_membership_status(
        request: Request, telegram_id: int, company_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        target = str(form.get("active", ""))
        if target not in {"0", "1"}:
            return _user_detail_redirect(
                telegram_id, error="Status membership tidak valid"
            )
        try:
            membership = database.set_membership_active(
                telegram_id,
                company_id,
                target == "1",
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return _user_detail_redirect(telegram_id, error=str(exc))
        status = "diaktifkan" if membership.active else "dinonaktifkan"
        return _user_detail_redirect(
            telegram_id,
            notice=f"Membership {membership.company_name} berhasil {status}",
        )

    @app.get("/admin/instructions", response_class=HTMLResponse)
    async def instructions(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        rows = database.list_company_instructions_admin()
        for row in rows:
            row["has_legacy_instruction"] = bool(
                _read_scoped_text(
                    root,
                    str(row.get("instruction_file", "")),
                    max_chars=settings.knowledge_max_chars,
                )
            )
        return templates.TemplateResponse(
            request=request,
            name="admin/instructions.html",
            context={
                "active_page": "instructions",
                "instructions": rows,
                "admin_username": settings.admin_username,
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get(
        "/admin/instructions/{company_id}", response_class=HTMLResponse
    )
    async def edit_instruction(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        state = database.get_company_instruction_admin(company_id)
        if state is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        company = database.get_company_admin(company_id)
        if company is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        legacy_content = _read_scoped_text(
            root,
            company.instruction_file,
            max_chars=settings.knowledge_max_chars,
        )
        if state["draft_content"] is not None:
            draft_content = str(state["draft_content"])
            draft_source = "Draft database"
        elif state["published_content"] is not None:
            draft_content = str(state["published_content"])
            draft_source = "Versi terbit"
        elif legacy_content:
            draft_content = legacy_content
            draft_source = "File transisi"
        else:
            draft_content = ""
            draft_source = "Belum ada"
        return templates.TemplateResponse(
            request=request,
            name="admin/instruction_editor.html",
            context=_instruction_context(
                request,
                settings,
                database,
                state,
                draft_content=draft_content,
                draft_source=draft_source,
                legacy_content=legacy_content,
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post(
        "/admin/instructions/{company_id}/draft", response_class=HTMLResponse
    )
    async def save_instruction_draft(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.save_company_instruction_draft(
                company_id,
                form.get("content", ""),
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return _instruction_redirect(company_id, error=str(exc))
        return _instruction_redirect(company_id, notice="Draft berhasil disimpan")

    @app.get(
        "/admin/instructions/{company_id}/preview", response_class=HTMLResponse
    )
    async def preview_instruction(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        state = database.get_company_instruction_admin(company_id)
        if state is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        if state["draft_content"] is None:
            return _instruction_redirect(
                company_id, error="Simpan draft sebelum membuka preview"
            )
        return templates.TemplateResponse(
            request=request,
            name="admin/instruction_preview.html",
            context=_instruction_context(
                request,
                settings,
                database,
                state,
                draft_content=str(state["draft_content"]),
                draft_source="Draft database",
                legacy_content="",
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post("/admin/instructions/{company_id}/publish")
    async def publish_instruction(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            version = database.publish_company_instruction(
                company_id, actor=settings.admin_username
            )
        except ValueError as exc:
            return _instruction_redirect(company_id, error=str(exc))
        return _instruction_redirect(
            company_id, notice=f"Instruction versi {version} berhasil dipublikasikan"
        )

    @app.post(
        "/admin/instructions/{company_id}/versions/{version_id}/restore"
    )
    async def restore_instruction_version(
        request: Request, company_id: str, version_id: int
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.restore_company_instruction_version_to_draft(
                company_id, version_id, actor=settings.admin_username
            )
        except ValueError as exc:
            return _instruction_redirect(company_id, error=str(exc))
        return _instruction_redirect(
            company_id,
            notice="Versi lama dipulihkan sebagai draft. Preview sebelum publish.",
        )

    @app.get("/admin/knowledge", response_class=HTMLResponse)
    async def knowledge_registry(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        rows = database.list_company_knowledge_summary_admin()
        for row in rows:
            row["legacy_document_count"] = _markdown_count(
                root, str(row.get("knowledge_dir", ""))
            )
        return templates.TemplateResponse(
            request=request,
            name="admin/knowledge.html",
            context={
                "active_page": "knowledge",
                "companies": rows,
                "admin_username": settings.admin_username,
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get("/admin/knowledge/{company_id}", response_class=HTMLResponse)
    async def knowledge_documents(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        company = database.get_company_admin(company_id)
        if company is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        summary = next(
            (
                item
                for item in database.list_company_knowledge_summary_admin()
                if item["company_id"] == company.company_id
            ),
            {},
        )
        return templates.TemplateResponse(
            request=request,
            name="admin/knowledge_documents.html",
            context={
                "active_page": "knowledge",
                "admin_username": settings.admin_username,
                "csrf_token": _csrf_token(request, settings),
                "company": company,
                "summary": summary,
                "documents": database.list_knowledge_documents_admin(company_id),
                "legacy_document_count": _markdown_count(
                    root, company.knowledge_dir
                ),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get(
        "/admin/knowledge/{company_id}/new", response_class=HTMLResponse
    )
    async def new_knowledge_document(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        company = database.get_company_admin(company_id)
        if company is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/knowledge_form.html",
            context=_knowledge_form_context(request, settings, company),
        )

    @app.post("/admin/knowledge/{company_id}", response_class=HTMLResponse)
    async def create_knowledge_document(request: Request, company_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        company = database.get_company_admin(company_id)
        if company is None:
            return HTMLResponse("Company tidak ditemukan.", status_code=404)
        values = {
            "title": str(form.get("title", "")).strip(),
            "content": str(form.get("content", "")).strip(),
        }
        try:
            upload = form.get("source_file")
            upload_name = str(getattr(upload, "filename", "") or "").strip()
            source_metadata: dict[str, object] = {}
            if upload_name:
                if values["content"]:
                    raise ValueError(
                        "Pilih salah satu: isi teks langsung atau upload file"
                    )
                file_bytes = await upload.read(MAX_UPLOAD_BYTES + 1)
                extracted = extract_uploaded_document(
                    upload_name,
                    getattr(upload, "content_type", ""),
                    file_bytes,
                )
                values["content"] = extracted.text
                source_metadata = {
                    "source_filename": extracted.filename,
                    "source_media_type": extracted.media_type,
                    "source_size_bytes": extracted.size_bytes,
                    "source_sha256": extracted.sha256,
                }
            document = database.create_knowledge_document(
                company_id,
                "",
                values["title"],
                values["content"],
                actor=settings.admin_username,
                **source_metadata,
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/knowledge_form.html",
                context=_knowledge_form_context(
                    request, settings, company, values=values, error=str(exc)
                ),
                status_code=400,
            )
        return _knowledge_document_redirect(
            company_id,
            str(document["document_key"]),
            notice="Draft knowledge berhasil dibuat",
        )

    @app.get(
        "/admin/knowledge/{company_id}/{document_key}/preview",
        response_class=HTMLResponse,
    )
    async def preview_knowledge_document(
        request: Request, company_id: str, document_key: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        document = database.get_knowledge_document_admin(company_id, document_key)
        if document is None:
            return HTMLResponse("Knowledge document tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/knowledge_preview.html",
            context=_knowledge_document_context(
                request, settings, database, document
            ),
        )

    @app.get(
        "/admin/knowledge/{company_id}/{document_key}",
        response_class=HTMLResponse,
    )
    async def edit_knowledge_document(
        request: Request, company_id: str, document_key: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        document = database.get_knowledge_document_admin(company_id, document_key)
        if document is None:
            return HTMLResponse("Knowledge document tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/knowledge_editor.html",
            context=_knowledge_document_context(
                request,
                settings,
                database,
                document,
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post("/admin/knowledge/{company_id}/{document_key}/draft")
    async def save_knowledge_document_draft(
        request: Request, company_id: str, document_key: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.save_knowledge_document_draft(
                company_id,
                document_key,
                form.get("title", ""),
                form.get("content", ""),
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return _knowledge_document_redirect(
                company_id, document_key, error=str(exc)
            )
        return _knowledge_document_redirect(
            company_id, document_key, notice="Draft knowledge berhasil disimpan"
        )

    @app.post("/admin/knowledge/{company_id}/{document_key}/publish")
    async def publish_knowledge_document(
        request: Request, company_id: str, document_key: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            version = database.publish_knowledge_document(
                company_id, document_key, actor=settings.admin_username
            )
        except ValueError as exc:
            return _knowledge_document_redirect(
                company_id, document_key, error=str(exc)
            )
        return _knowledge_document_redirect(
            company_id,
            document_key,
            notice=f"Knowledge versi {version} berhasil dipublikasikan",
        )

    @app.post("/admin/knowledge/{company_id}/{document_key}/status")
    async def update_knowledge_document_status(
        request: Request, company_id: str, document_key: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        target = str(form.get("active", ""))
        if target not in {"0", "1"}:
            return _knowledge_company_redirect(
                company_id, error="Status knowledge tidak valid"
            )
        try:
            database.set_knowledge_document_active(
                company_id,
                document_key,
                target == "1",
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return _knowledge_company_redirect(company_id, error=str(exc))
        status = "diaktifkan" if target == "1" else "dinonaktifkan"
        return _knowledge_company_redirect(
            company_id, notice=f"Knowledge berhasil {status}"
        )

    @app.post(
        "/admin/knowledge/{company_id}/{document_key}/versions/{version_id}/restore"
    )
    async def restore_knowledge_document_version(
        request: Request,
        company_id: str,
        document_key: str,
        version_id: int,
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.restore_knowledge_document_version_to_draft(
                company_id,
                document_key,
                version_id,
                actor=settings.admin_username,
            )
        except ValueError as exc:
            return _knowledge_document_redirect(
                company_id, document_key, error=str(exc)
            )
        return _knowledge_document_redirect(
            company_id,
            document_key,
            notice="Versi lama dipulihkan sebagai draft. Preview sebelum publish.",
        )

    @app.get("/admin/{section}", response_class=HTMLResponse)
    async def placeholder(request: Request, section: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        labels = {
            "modules": "Modules",
            "activity": "Activity",
        }
        label = labels.get(section)
        if label is None:
            return templates.TemplateResponse(
                request=request,
                name="admin/not_found.html",
                context={
                    "active_page": "",
                    "admin_username": settings.admin_username,
                },
                status_code=404,
            )
        return templates.TemplateResponse(
            request=request,
            name="admin/placeholder.html",
            context={
                "active_page": section,
                "title": label,
                "admin_username": settings.admin_username,
            },
        )

    return app


def start_admin_server(settings: Settings, database: Database) -> threading.Thread | None:
    if not settings.admin_enabled:
        logger.warning(
            "Admin web nonaktif. Isi ADMIN_USERNAME, ADMIN_PASSWORD, dan "
            "ADMIN_SESSION_SECRET untuk mengaktifkan."
        )
        return None

    app = create_admin_app(settings, database)
    config = uvicorn.Config(
        app,
        host=settings.admin_host,
        port=settings.admin_port,
        log_level=settings.log_level.casefold(),
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        name="dk-admin-web",
        daemon=True,
    )
    thread.start()
    logger.info("Admin web aktif pada port %d", settings.admin_port)
    return thread


def create_admin_app_from_env() -> FastAPI:
    settings = load_settings()
    if not settings.admin_enabled:
        raise RuntimeError("Admin web belum diaktifkan melalui environment variables")
    database = Database(settings.database_path)
    database.initialize()
    database.bootstrap_companies(settings.companies_file)
    database.bootstrap_users(settings.users_file)
    return create_admin_app(settings, database)


def _create_session_token(settings: Settings) -> str:
    issued_at = str(int(time.time()))
    payload = f"{settings.admin_username}:{issued_at}"
    signature = hmac.new(
        settings.admin_session_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{payload}:{signature}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _is_authenticated(request: Request, settings: Settings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, issued_at_text, signature = raw.split(":", 2)
        issued_at = int(issued_at_text)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    if username != settings.admin_username:
        return False
    age = int(time.time()) - issued_at
    if age < 0 or age > SESSION_MAX_AGE:
        return False
    payload = f"{username}:{issued_at_text}"
    expected = hmac.new(
        settings.admin_session_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _login_redirect(request: Request, settings: Settings) -> RedirectResponse | None:
    if _is_authenticated(request, settings):
        return None
    return RedirectResponse("/admin/login", status_code=303)


def _csrf_token(request: Request, settings: Settings) -> str:
    session = request.cookies.get(SESSION_COOKIE, "")
    if not session:
        return ""
    return hmac.new(
        settings.admin_session_secret.encode(),
        f"csrf:{session}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _valid_csrf(value: str, request: Request, settings: Settings) -> bool:
    expected = _csrf_token(request, settings)
    return bool(expected) and hmac.compare_digest(value, expected)


def _company_form_context(
    request: Request,
    settings: Settings,
    *,
    mode: str,
    company_id: str,
    name: str,
    active: bool,
    profile_file: str = "",
    instruction_file: str = "",
    knowledge_dir: str = "",
    error: str = "",
) -> dict[str, object]:
    return {
        "active_page": "companies",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "mode": mode,
        "company_id": company_id,
        "name": name,
        "active": active,
        "profile_file": profile_file,
        "instruction_file": instruction_file,
        "knowledge_dir": knowledge_dir,
        "error": error,
    }


def _companies_redirect(
    notice: str = "", *, error: str = ""
) -> RedirectResponse:
    values = {key: value for key, value in {"notice": notice, "error": error}.items() if value}
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(f"/admin/companies{suffix}", status_code=303)


def _instruction_context(
    request: Request,
    settings: Settings,
    database: Database,
    state: dict[str, object],
    *,
    draft_content: str,
    draft_source: str,
    legacy_content: str,
    notice: str = "",
    error: str = "",
) -> dict[str, object]:
    published_content = state.get("published_content")
    return {
        "active_page": "instructions",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "state": state,
        "draft_content": draft_content,
        "draft_source": draft_source,
        "legacy_content": legacy_content,
        "has_unpublished_changes": (
            state.get("draft_content") is not None
            and (
                published_content is None
                or str(state.get("draft_content")) != str(published_content)
            )
        ),
        "versions": database.list_company_instruction_versions(
            str(state["company_id"])
        ),
        "notice": notice,
        "error": error,
    }


def _instruction_redirect(
    company_id: str, notice: str = "", *, error: str = ""
) -> RedirectResponse:
    values = {
        key: value
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(
        f"/admin/instructions/{company_id}{suffix}", status_code=303
    )


def _knowledge_form_context(
    request: Request,
    settings: Settings,
    company,
    *,
    values: dict[str, str] | None = None,
    error: str = "",
) -> dict[str, object]:
    defaults = {"title": "", "content": ""}
    defaults.update(values or {})
    return {
        "active_page": "knowledge",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "company": company,
        "values": defaults,
        "error": error,
    }


def _knowledge_document_context(
    request: Request,
    settings: Settings,
    database: Database,
    document: dict[str, object],
    *,
    notice: str = "",
    error: str = "",
) -> dict[str, object]:
    return {
        "active_page": "knowledge",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "document": document,
        "versions": database.list_knowledge_document_versions(
            str(document["company_id"]), str(document["document_key"])
        ),
        "has_unpublished_changes": (
            document.get("published_version_id") is None
            or str(document.get("title", ""))
            != str(document.get("published_title", ""))
            or str(document.get("draft_content", ""))
            != str(document.get("published_content", ""))
        ),
        "notice": notice,
        "error": error,
    }


def _knowledge_company_redirect(
    company_id: str, notice: str = "", *, error: str = ""
) -> RedirectResponse:
    values = {
        key: value
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(
        f"/admin/knowledge/{company_id}{suffix}", status_code=303
    )


def _knowledge_document_redirect(
    company_id: str,
    document_key: str,
    notice: str = "",
    *,
    error: str = "",
) -> RedirectResponse:
    values = {
        key: value
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(
        f"/admin/knowledge/{company_id}/{document_key}{suffix}",
        status_code=303,
    )


def _user_form_values(form) -> dict[str, str]:
    values = _membership_form_values(form)
    values.update(
        {
            "telegram_id": str(form.get("telegram_id", "")).strip(),
            "name": str(form.get("name", "")).strip(),
            "active": "1" if form.get("active") == "1" else "0",
        }
    )
    return values


def _membership_form_values(form, *, company_id: str = "") -> dict[str, str]:
    return {
        "company_id": company_id or str(form.get("company_id", "")).strip(),
        "job_title": str(form.get("job_title", "")).strip(),
        "division": str(form.get("division", "")).strip(),
        "role_level": str(form.get("role_level", "staff")).strip(),
        "communication_profile": str(
            form.get("communication_profile", "staff")
        ).strip(),
        "custom_instruction": str(form.get("custom_instruction", "")).strip(),
        "is_default": "1" if form.get("is_default") == "1" else "0",
    }


def _active_company_options(database: Database) -> list[dict[str, object]]:
    return [row for row in database.list_companies_admin() if bool(row["active"])]


def _user_form_context(
    request: Request,
    settings: Settings,
    database: Database,
    profile_options: list[dict[str, str]],
    *,
    mode: str,
    user: User | None = None,
    values: dict[str, str] | None = None,
    notice: str = "",
    error: str = "",
) -> dict[str, object]:
    companies = _active_company_options(database)
    defaults = {
        "telegram_id": str(user.telegram_id) if user else "",
        "name": user.name if user else "",
        "active": "1" if user is None or user.active else "0",
        "company_id": str(companies[0]["company_id"]) if companies else "",
        "job_title": "",
        "division": "",
        "role_level": "staff",
        "communication_profile": "staff",
        "custom_instruction": "",
        "is_default": "1",
    }
    defaults.update(values or {})
    return {
        "active_page": "users",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "mode": mode,
        "user": user,
        "values": defaults,
        "companies": companies,
        "memberships": (
            database.list_memberships_admin(user.telegram_id) if user else []
        ),
        "profile_options": profile_options,
        "role_options": _role_options(),
        "notice": notice,
        "error": error,
    }


def _membership_form_context(
    request: Request,
    settings: Settings,
    database: Database,
    profile_options: list[dict[str, str]],
    *,
    user: User,
    mode: str,
    membership: Membership | None = None,
    values: dict[str, str] | None = None,
    error: str = "",
) -> dict[str, object]:
    existing_ids = {
        item.company_id for item in database.list_memberships_admin(user.telegram_id)
    }
    companies = [
        row
        for row in _active_company_options(database)
        if mode == "edit" or str(row["company_id"]) not in existing_ids
    ]
    defaults = {
        "company_id": membership.company_id if membership else (
            str(companies[0]["company_id"]) if companies else ""
        ),
        "job_title": membership.job_title if membership else "",
        "division": membership.division if membership else "",
        "role_level": membership.role_level if membership else "staff",
        "communication_profile": (
            membership.communication_profile if membership else "staff"
        ),
        "custom_instruction": membership.custom_instruction if membership else "",
        "is_default": "1" if membership and membership.is_default else "0",
    }
    defaults.update(values or {})
    return {
        "active_page": "users",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "user": user,
        "mode": mode,
        "membership": membership,
        "values": defaults,
        "companies": companies,
        "profile_options": profile_options,
        "role_options": _role_options(),
        "error": error,
    }


def _role_options() -> list[dict[str, str]]:
    return [
        {"id": "gm", "label": "GM / Executive"},
        {"id": "manager", "label": "Manager"},
        {"id": "staff", "label": "Staff"},
    ]


def _users_redirect(notice: str = "", *, error: str = "") -> RedirectResponse:
    values = {
        key: value
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(f"/admin/users{suffix}", status_code=303)


def _user_detail_redirect(
    telegram_id: int, notice: str = "", *, error: str = ""
) -> RedirectResponse:
    values = {
        key: value
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(
        f"/admin/users/{telegram_id}/edit{suffix}", status_code=303
    )


def _markdown_count(root: Path, configured_path: str) -> int:
    if not configured_path:
        return 0
    path = (root / configured_path).resolve()
    if path != root and root not in path.parents:
        return 0
    if not path.is_dir():
        return 0
    return sum(1 for item in path.rglob("*.md") if item.is_file())


def _read_scoped_text(root: Path, configured_path: str, max_chars: int) -> str:
    if not configured_path or max_chars <= 0:
        return ""
    path = (root / configured_path).resolve()
    if path == root or root not in path.parents or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()[:max_chars]
