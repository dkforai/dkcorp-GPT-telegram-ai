from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.config import Settings, load_settings
from app.credentials import PROVIDERS, encryption_is_ready
from app.database import AIRuntimeProfile, AdminUser, Database, Membership, User
from app.document_ingestion import MAX_UPLOAD_BYTES, extract_uploaded_document
from app.providers import runtime_profile_is_configured
from app.model_catalog import CATALOG_MESSAGES, discover_models
from app.role_profiles import load_role_profiles, profile_id_for_role
from app.user_import import MAX_USER_IMPORT_BYTES, UserImportValidationError, read_user_import


logger = logging.getLogger(__name__)
SESSION_COOKIE = "dk_admin_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60


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
    # SDK DEBUG output can contain request bodies or headers. This also applies
    # to admin-only uvicorn startup, which does not pass through app.main.
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    database.ensure_primary_admin(settings.admin_username, settings.admin_password)
    root = settings.project_root.resolve()

    def admin_template_context(request: Request) -> dict[str, object]:
        identity = _admin_identity(request, settings)
        return {
            "current_admin_name": identity.display_name if identity else "",
            "current_admin_role": identity.role if identity else "",
            "is_super_admin": bool(identity and identity.role == "super_admin"),
        }

    templates = Jinja2Templates(
        directory=str(root / "templates"),
        context_processors=[admin_template_context],
    )
    limiter = LoginLimiter()
    ai_test_limiter = LoginLimiter(max_attempts=3, window_seconds=60)
    ai_tests_running: set[str] = set()
    role_profiles = load_role_profiles(settings.role_profiles_file)
    database.ensure_communication_styles(role_profiles)
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
    app.state.database = database
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(root / "static" / "admin")),
        name="admin-static",
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        identity = _admin_identity(request, settings)
        super_only = (
            request.url.path.startswith("/admin/runtime-profiles")
            or request.url.path.startswith("/admin/settings/ai")
            or request.url.path.startswith("/admin/settings/admins")
            or request.url.path.startswith("/admin/settings/communication")
        )
        if identity and super_only and identity.role != "super_admin":
            response = HTMLResponse(
                "Halaman ini hanya dapat diakses Super Admin.", status_code=403
            )
        else:
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
        admin = database.authenticate_admin(username, password)
        if admin is None:
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
            _create_session_token(settings, admin.username),
            max_age=SESSION_MAX_AGE,
            httponly=True,
            secure=settings.admin_cookie_secure,
            samesite="lax",
            path="/admin",
        )
        database.write_admin_event(
            admin.display_name, "admin.login", "admin_user", admin.username
        )
        return response

    @app.get("/admin/logout")
    async def logout(request: Request) -> RedirectResponse:
        identity = _admin_identity(request, settings)
        if identity:
            database.write_admin_event(
                identity.display_name, "admin.logout", "admin_user", identity.username
            )
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
                "", name, actor=_admin_actor(request, settings), active=active
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
                company_id, name, actor=_admin_actor(request, settings)
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
                company_id, target == "1", actor=_admin_actor(request, settings)
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

    def user_import_page(request: Request, *, values=None, error="", errors=None, report=None, status_code=200):
        return templates.TemplateResponse(
            request=request,
            name="admin/user_import.html",
            context={
                "active_page": "users",
                "admin_username": settings.admin_username,
                "csrf_token": _csrf_token(request, settings),
                "role_options": _role_options(),
                "profile_options": profile_options,
                "values": values or {"role_level": "staff", "communication_profile": "staff", "active": "0"},
                "error": error, "errors": errors or [], "report": report,
            },
            status_code=status_code,
        )

    @app.get("/admin/users/import", response_class=HTMLResponse)
    async def new_user_import(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return user_import_page(request)

    @app.post("/admin/users/import", response_class=HTMLResponse)
    async def import_users(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        # Bound the actual stream, not just Content-Length, before multipart
        # parsing can spool an arbitrarily large upload to disk.
        limited_request = await _bounded_import_request(request)
        async with limited_request.form(max_files=1, max_fields=8, max_part_size=8192) as form:
            if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
                return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
            values = {key: str(form.get(key, default)) for key, default in (
                ("role_level", "staff"), ("active", "0"),
            )}
            values["communication_profile"] = profile_id_for_role(values["role_level"])
            try:
                if values["active"] not in {"0", "1"}:
                    raise ValueError("Status whitelist tidak valid.")
                upload = form.get("file")
                if not isinstance(upload, UploadFile) or not upload.filename:
                    raise ValueError("Pilih file Excel .xls atau .xlsx terlebih dahulu.")
                data = await upload.read(MAX_USER_IMPORT_BYTES + 1)
                rows = await run_in_threadpool(read_user_import, upload.filename, data)
                report = await run_in_threadpool(
                    database.import_new_users, rows,
                    role_level=values["role_level"],
                    communication_profile=values["communication_profile"],
                    active=values["active"] == "1", actor=_admin_actor(request, settings),
                    source_filename=upload.filename,
                    source_sha256=hashlib.sha256(data).hexdigest(),
                )
            except UserImportValidationError as exc:
                return user_import_page(request, values=values, error=str(exc), errors=exc.errors, status_code=400)
            except ValueError as exc:
                return user_import_page(request, values=values, error=str(exc), status_code=400)
        return user_import_page(request, values=values, report=report)

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
                actor=_admin_actor(request, settings),
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
                telegram_id, str(form.get("name", "")), actor=_admin_actor(request, settings)
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
                telegram_id, target == "1", actor=_admin_actor(request, settings)
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
                actor=_admin_actor(request, settings),
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
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
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
                actor=_admin_actor(request, settings),
                preserve_legacy_context=True,
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
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _user_detail_redirect(telegram_id, error=str(exc))
        status = "diaktifkan" if membership.active else "dinonaktifkan"
        return _user_detail_redirect(
            telegram_id,
            notice=f"Membership {membership.company_name} berhasil {status}",
        )

    @app.post(
        "/admin/users/{telegram_id}/memberships/{company_id}/modules"
    )
    async def update_membership_module_access(
        request: Request, telegram_id: int, company_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.set_membership_module_access(
                telegram_id,
                company_id,
                list(form.getlist("module_ids")),
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _membership_edit_redirect(
                telegram_id, company_id, error=str(exc)
            )
        return _membership_edit_redirect(
            telegram_id,
            company_id,
            notice="Akses module berhasil diperbarui",
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
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _instruction_redirect(company_id, error=str(exc))
        if str(form.get("submit_action", "")) == "preview":
            return RedirectResponse(
                f"/admin/instructions/{company_id}/preview", status_code=303
            )
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
                company_id, actor=_admin_actor(request, settings)
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
                company_id, version_id, actor=_admin_actor(request, settings)
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
                actor=_admin_actor(request, settings),
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
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _knowledge_document_redirect(
                company_id, document_key, error=str(exc)
            )
        if str(form.get("submit_action", "")) == "preview":
            return RedirectResponse(
                f"/admin/knowledge/{company_id}/{document_key}/preview",
                status_code=303,
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
                company_id, document_key, actor=_admin_actor(request, settings)
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
                actor=_admin_actor(request, settings),
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
                actor=_admin_actor(request, settings),
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

    @app.get("/admin/modules", response_class=HTMLResponse)
    async def modules(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/modules.html",
            context={
                "active_page": "modules",
                "admin_username": settings.admin_username,
                "modules": database.list_modules_admin(),
                "runtime_profiles": _runtime_profile_views(database),
                "csrf_token": _csrf_token(request, settings),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get("/admin/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        return _login_redirect(request, settings) or RedirectResponse("/admin/settings/ai", status_code=303)

    @app.get("/admin/settings/ai", response_class=HTMLResponse)
    async def ai_settings(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request, name="admin/ai_settings.html", context={
                "active_page": "settings", "admin_username": settings.admin_username,
                "csrf_token": _csrf_token(request, settings),
                "runtime_profiles": _runtime_profile_views(database),
                "encryption_ready": encryption_is_ready(),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.get("/admin/runtime-profiles/new", response_class=HTMLResponse)
    async def new_runtime_profile(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/runtime_profile_form.html",
            context=_runtime_profile_form_context(request, settings, mode="create"),
        )

    @app.post("/admin/runtime-profiles", response_class=HTMLResponse)
    async def create_runtime_profile(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await _credential_form(request)
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        values = _runtime_profile_form_values(form)
        try:
            profile = database.create_ai_connection(
                values["provider"], str(form.get("api_key", "")), _admin_actor(request, settings),
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/runtime_profile_form.html",
                context=_runtime_profile_form_context(
                    request,
                    settings,
                    mode="create",
                    values=values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _ai_settings_redirect(notice=f"{profile.label} aktif. Klik Tes & ambil model sebelum memilih model di Module.")

    @app.get(
        "/admin/runtime-profiles/{profile_id}/edit",
        response_class=HTMLResponse,
    )
    async def edit_runtime_profile(request: Request, profile_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        try:
            profile = database.get_ai_runtime_profile(profile_id)
        except ValueError:
            profile = None
        if profile is None:
            return HTMLResponse("Credential profile tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/runtime_profile_form.html",
            context=_runtime_profile_form_context(
                request,
                settings,
                mode="edit",
                profile=profile,
            ),
        )

    @app.post(
        "/admin/runtime-profiles/{profile_id}", response_class=HTMLResponse
    )
    async def update_runtime_profile(request: Request, profile_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        try:
            current_profile = database.get_ai_runtime_profile(profile_id)
        except ValueError:
            current_profile = None
        if current_profile is None:
            return HTMLResponse("AI tidak ditemukan.", status_code=404)
        form = await _credential_form(request)
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        values = _runtime_profile_form_values(form)
        try:
            if values["provider"] != current_profile.provider:
                raise ValueError("Provider koneksi lama tidak dapat diganti. Tambahkan koneksi provider baru.")
            if not str(form.get("api_key", "")).strip():
                return _ai_settings_redirect(notice="Key dan daftar model sebelumnya dipertahankan.")
            database.update_ai_runtime_profile(
                profile_id,
                current_profile.label,
                current_profile.provider,
                current_profile.api_key_env,
                current_profile.model,
                "" if str(form.get("api_key", "")).strip() else current_profile.base_url,
                actor=_admin_actor(request, settings),
                api_key=str(form.get("api_key", "")) or None,
            )
        except ValueError as exc:
            profile = database.get_ai_runtime_profile(profile_id)
            return templates.TemplateResponse(
                request=request,
                name="admin/runtime_profile_form.html",
                context=_runtime_profile_form_context(
                    request,
                    settings,
                    mode="edit",
                    profile=profile,
                    values=values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _ai_settings_redirect(notice="Key berhasil disimpan. Klik Tes & ambil model untuk memperbarui daftar model.")

    @app.post("/admin/runtime-profiles/{profile_id}/status")
    async def update_runtime_profile_status(request: Request, profile_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await _credential_form(request)
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        target = str(form.get("active", ""))
        if target not in {"0", "1"}:
            return _ai_settings_redirect(error="Status credential profile tidak valid")
        try:
            profile = database.set_ai_runtime_profile_active(
                profile_id, target == "1", actor=_admin_actor(request, settings)
            )
        except ValueError as exc:
            return _ai_settings_redirect(error=str(exc))
        status = "diaktifkan" if profile.active else "dinonaktifkan"
        return _ai_settings_redirect(
            notice=f"Credential profile {profile.label} berhasil {status}"
        )

    @app.post("/admin/runtime-profiles/{profile_id}/test")
    async def probe_runtime_profile(request: Request, profile_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await _credential_form(request)
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            profile = database.get_ai_runtime_profile(profile_id)
        except ValueError:
            profile = None
        if profile is None:
            return HTMLResponse("AI tidak ditemukan.", status_code=404)
        if not profile.active:
            return _ai_settings_redirect(error="Aktifkan provider sebelum menjalankan tes.")
        if (profile_id in ai_tests_running or len(ai_tests_running) >= 2
                or ai_test_limiter.blocked(profile_id) or ai_test_limiter.blocked("__all__")):
            return HTMLResponse("Tes sedang berjalan atau batas tes tercapai. Coba lagi satu menit lagi.", status_code=429)
        ai_test_limiter.failure(profile_id)
        ai_test_limiter.failure("__all__")
        ai_tests_running.add(profile_id)
        try:
            result = await discover_models(profile)
            status = result.status
            recorded = database.record_model_catalog(profile, result, _admin_actor(request, settings))
        finally:
            ai_tests_running.discard(profile_id)
        if not recorded:
            return _ai_settings_redirect(error="Konfigurasi berubah selama tes. Ulangi tes untuk konfigurasi terbaru.")
        message = f"{profile.label}: {CATALOG_MESSAGES[status]}"
        if status == "success":
            message += f" {len(result.models)} model, {sum(m.selectable for m in result.models)} mendukung adapter bot."
        return _ai_settings_redirect(notice=message) if status == "success" else _ai_settings_redirect(error=message)

    @app.get("/admin/modules/new", response_class=HTMLResponse)
    async def new_module(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        companies = _active_company_options(database)
        selected_company = str(request.query_params.get("company_id", ""))
        return templates.TemplateResponse(
            request=request,
            name="admin/module_form.html",
            context=_module_form_context(
                request,
                settings,
                companies,
                database,
                values={"company_id": selected_company},
            ),
        )

    @app.post("/admin/modules", response_class=HTMLResponse)
    async def create_module(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        values = _module_form_values(form)
        try:
            module = database.create_module(
                values["company_id"],
                "",
                values["name"],
                values["description"],
                actor=_admin_actor(request, settings),
                ai_runtime_profile_id=values["ai_runtime_profile_id"],
                backup_ai_runtime_profile_id=values["backup_ai_runtime_profile_id"],
                ai_model=values["ai_model"],
                backup_ai_model=values["backup_ai_model"],
                active=values["active"] == "1",
                short_code=values["short_code"],
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/module_form.html",
                context=_module_form_context(
                    request,
                    settings,
                    _active_company_options(database),
                    database,
                    values=values,
                    error=str(exc),
                ),
                status_code=400,
            )
        return _module_redirect(
            module.company_id,
            module.module_id,
            notice="Module berhasil dibuat. Isi dan publish playbook berikutnya.",
        )

    @app.get(
        "/admin/modules/{company_id}/{module_id}", response_class=HTMLResponse
    )
    async def edit_module(request: Request, company_id: str, module_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        state = database.get_module_playbook_admin(company_id, module_id)
        if state is None:
            return HTMLResponse("Module tidak ditemukan.", status_code=404)
        return templates.TemplateResponse(
            request=request,
            name="admin/module_editor.html",
            context=_module_editor_context(
                request,
                settings,
                database,
                state,
                notice=request.query_params.get("notice", ""),
                error=request.query_params.get("error", ""),
            ),
        )

    @app.post("/admin/modules/{company_id}/{module_id}")
    async def update_module(
        request: Request, company_id: str, module_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            values = _module_form_values(form)
            database.update_module(
                company_id,
                module_id,
                form.get("name", ""),
                form.get("description", ""),
                actor=_admin_actor(request, settings),
                ai_runtime_profile_id=values["ai_runtime_profile_id"],
                backup_ai_runtime_profile_id=(values["backup_ai_runtime_profile_id"]
                    if "backup_ai_selection" in form or "backup_ai_runtime_profile_id" in form else None),
                ai_model=values["ai_model"],
                backup_ai_model=values["backup_ai_model"],
                short_code=values["short_code"] if "short_code" in form else None,
            )
        except ValueError as exc:
            return _module_redirect(company_id, module_id, error=str(exc))
        return _module_redirect(
            company_id, module_id, notice="Identitas module berhasil diperbarui"
        )

    @app.post("/admin/modules/{company_id}/{module_id}/draft")
    async def save_module_draft(
        request: Request, company_id: str, module_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.save_module_playbook_draft(
                company_id,
                module_id,
                form.get("content", ""),
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _module_redirect(company_id, module_id, error=str(exc))
        if str(form.get("submit_action", "")) == "preview":
            return RedirectResponse(
                f"/admin/modules/{company_id}/{module_id}/preview",
                status_code=303,
            )
        return _module_redirect(
            company_id, module_id, notice="Draft playbook berhasil disimpan"
        )

    @app.get(
        "/admin/modules/{company_id}/{module_id}/preview",
        response_class=HTMLResponse,
    )
    async def preview_module(
        request: Request, company_id: str, module_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        state = database.get_module_playbook_admin(company_id, module_id)
        if state is None:
            return HTMLResponse("Module tidak ditemukan.", status_code=404)
        if state.get("draft_content") is None:
            return _module_redirect(
                company_id, module_id, error="Simpan draft sebelum preview"
            )
        return templates.TemplateResponse(
            request=request,
            name="admin/module_preview.html",
            context={
                "active_page": "modules",
                "admin_username": settings.admin_username,
                "csrf_token": _csrf_token(request, settings),
                "state": state,
            },
        )

    @app.post("/admin/modules/{company_id}/{module_id}/publish")
    async def publish_module(
        request: Request, company_id: str, module_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            version = database.publish_module_playbook(
                company_id, module_id, actor=_admin_actor(request, settings)
            )
        except ValueError as exc:
            return _module_redirect(company_id, module_id, error=str(exc))
        return _module_redirect(
            company_id,
            module_id,
            notice=f"Playbook versi {version} berhasil dipublikasikan",
        )

    @app.post("/admin/modules/{company_id}/{module_id}/status")
    async def update_module_status(
        request: Request, company_id: str, module_id: str
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        target = str(form.get("active", ""))
        if target not in {"0", "1"}:
            return _modules_redirect(error="Status module tidak valid")
        try:
            module = database.set_module_active(
                company_id,
                module_id,
                target == "1",
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _modules_redirect(error=str(exc))
        status = "diaktifkan" if module.active else "dinonaktifkan"
        return _modules_redirect(notice=f"{module.name} berhasil {status}")

    @app.post(
        "/admin/modules/{company_id}/{module_id}/versions/{version_id}/restore"
    )
    async def restore_module_version(
        request: Request,
        company_id: str,
        module_id: str,
        version_id: int,
    ):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            return HTMLResponse("Permintaan tidak valid. Muat ulang halaman.", status_code=403)
        try:
            database.restore_module_playbook_version_to_draft(
                company_id,
                module_id,
                version_id,
                actor=_admin_actor(request, settings),
            )
        except ValueError as exc:
            return _module_redirect(company_id, module_id, error=str(exc))
        return _module_redirect(
            company_id,
            module_id,
            notice="Versi playbook dipulihkan ke draft. Preview sebelum publish.",
        )

    @app.get("/admin/activity", response_class=HTMLResponse)
    async def activity(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        category = str(request.query_params.get("category", "all")).strip()
        categories = {
            "all": "Semua",
            "admin": "Login & Admin",
            "company": "Company",
            "user": "User",
            "membership": "Membership",
            "instruction": "Instruction",
            "knowledge": "Knowledge",
            "module": "Modules",
            "credential": "API Credentials",
        }
        if category not in categories:
            category = "all"
        events = [
            _activity_event_view(row)
            for row in database.list_admin_audit_events(limit=500)
        ]
        if category != "all":
            events = [event for event in events if event["category"] == category]
        events = events[:100]
        return templates.TemplateResponse(
            request=request,
            name="admin/activity.html",
            context={
                "active_page": "activity",
                "admin_username": settings.admin_username,
                "categories": categories,
                "selected_category": category,
                "events": events,
            },
        )

    @app.get("/admin/settings/admins", response_class=HTMLResponse)
    async def admin_users(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        return templates.TemplateResponse(
            request=request,
            name="admin/admin_users.html",
            context={
                "active_page": "settings",
                "admins": database.list_admin_users(),
                "csrf_token": _csrf_token(request, settings),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.post("/admin/settings/admins")
    async def create_admin_user(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            raise HTTPException(403, "Permintaan tidak valid. Muat ulang halaman.")
        try:
            database.create_admin_user(
                str(form.get("username", "")),
                str(form.get("display_name", "")),
                str(form.get("password", "")),
                str(form.get("role", "admin_operator")),
                _admin_actor(request, settings),
            )
        except ValueError as exc:
            return RedirectResponse(
                "/admin/settings/admins?" + urlencode({"error": str(exc)}), 303
            )
        return RedirectResponse(
            "/admin/settings/admins?notice=Akun+admin+berhasil+dibuat", 303
        )

    @app.post("/admin/settings/admins/{username}/status")
    async def admin_user_status(request: Request, username: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            raise HTTPException(403, "Permintaan tidak valid. Muat ulang halaman.")
        target = str(form.get("active", "")) == "1"
        try:
            database.set_admin_user_active(
                username, target, _admin_actor(request, settings)
            )
        except ValueError as exc:
            return RedirectResponse(
                "/admin/settings/admins?" + urlencode({"error": str(exc)}), 303
            )
        return RedirectResponse(
            "/admin/settings/admins?notice=Status+admin+berhasil+diubah", 303
        )

    @app.get("/admin/settings/communication", response_class=HTMLResponse)
    async def communication_styles(request: Request):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        styles = []
        for row in database.list_communication_styles():
            styles.append({
                **row,
                "focus": "\n".join(json.loads(str(row["focus_json"]))),
                "structure": "\n".join(json.loads(str(row["structure_json"]))),
                "avoid": "\n".join(json.loads(str(row["avoid_json"]))),
            })
        return templates.TemplateResponse(
            request=request,
            name="admin/communication_styles.html",
            context={
                "active_page": "settings", "styles": styles,
                "csrf_token": _csrf_token(request, settings),
                "notice": request.query_params.get("notice", ""),
                "error": request.query_params.get("error", ""),
            },
        )

    @app.post("/admin/settings/communication/{profile_id}")
    async def update_communication_style(request: Request, profile_id: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        form = await request.form()
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            raise HTTPException(403, "Permintaan tidak valid. Muat ulang halaman.")
        try:
            database.update_communication_style(
                profile_id, str(form.get("response_level", "")),
                str(form.get("focus", "")), str(form.get("structure", "")),
                str(form.get("avoid", "")), _admin_actor(request, settings),
            )
        except ValueError as exc:
            return RedirectResponse(
                "/admin/settings/communication?" + urlencode({"error": str(exc)}), 303
            )
        return RedirectResponse(
            "/admin/settings/communication?notice=Gaya+komunikasi+berhasil+disimpan", 303
        )

    from app.shared_admin import register_shared_routes
    register_shared_routes(app, settings, database, templates)

    @app.get("/admin/{section}", response_class=HTMLResponse)
    async def placeholder(request: Request, section: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        labels = {
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


def _create_session_token(settings: Settings, username: str | None = None) -> str:
    issued_at = str(int(time.time()))
    payload = f"{username or settings.admin_username}:{issued_at}"
    signature = hmac.new(
        settings.admin_session_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{payload}:{signature}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _session_username(request: Request, settings: Settings) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return ""
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        username, issued_at_text, signature = raw.split(":", 2)
        issued_at = int(issued_at_text)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return ""
    age = int(time.time()) - issued_at
    if age < 0 or age > SESSION_MAX_AGE:
        return ""
    payload = f"{username}:{issued_at_text}"
    expected = hmac.new(
        settings.admin_session_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    return username if hmac.compare_digest(signature, expected) else ""


def _admin_identity(request: Request, settings: Settings) -> AdminUser | None:
    username = _session_username(request, settings)
    if not username:
        return None
    database = getattr(request.app.state, "database", None)
    identity = database.get_admin_user(username) if database else None
    return identity if identity and identity.active else None


def _admin_actor(request: Request, settings: Settings) -> str:
    identity = _admin_identity(request, settings)
    return identity.display_name if identity else settings.admin_username


def _is_authenticated(request: Request, settings: Settings) -> bool:
    return _admin_identity(request, settings) is not None


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


def _module_form_values(form) -> dict[str, str | None]:
    primary, _, model = str(form.get("ai_selection", "")).partition("|")
    backup, _, backup_model = str(form.get("backup_ai_selection", "")).partition("|")
    return {
        "company_id": str(form.get("company_id", "")).strip(),
        "name": str(form.get("name", "")).strip(),
        "description": str(form.get("description", "")).strip(),
        "short_code": str(form.get("short_code", "")).strip(),
        "ai_runtime_profile_id": primary.strip() if "ai_selection" in form else str(form.get("ai_runtime_profile_id", "")).strip(),
        "backup_ai_runtime_profile_id": backup.strip() if "backup_ai_selection" in form else str(form.get("backup_ai_runtime_profile_id", "")).strip(),
        "ai_model": model if "ai_selection" in form else None,
        "backup_ai_model": backup_model if "backup_ai_selection" in form else None,
        "ai_selection": str(form.get("ai_selection", form.get("ai_runtime_profile_id", ""))),
        "backup_ai_selection": str(form.get("backup_ai_selection", form.get("backup_ai_runtime_profile_id", ""))),
        "active": "1" if form.get("active") == "1" else "0",
    }


def _module_form_context(
    request: Request,
    settings: Settings,
    companies: list[dict[str, object]],
    database: Database,
    *,
    values: dict[str, str] | None = None,
    error: str = "",
) -> dict[str, object]:
    defaults = {
        "company_id": str(companies[0]["company_id"]) if companies else "",
        "name": "",
        "description": "",
        "ai_runtime_profile_id": "",
        "backup_ai_runtime_profile_id": "",
        "ai_selection": "",
        "backup_ai_selection": "",
        "short_code": "",
        "active": "1",
    }
    defaults.update({key: value for key, value in (values or {}).items() if value})
    return {
        "active_page": "modules",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "companies": companies,
        "runtime_profiles": _module_model_options(database),
        "values": defaults,
        "error": error,
    }


def _module_editor_context(
    request: Request,
    settings: Settings,
    database: Database,
    state: dict[str, object],
    *,
    notice: str = "",
    error: str = "",
) -> dict[str, object]:
    draft_content = str(state.get("draft_content") or "")
    published_content = str(state.get("published_content") or "")
    return {
        "active_page": "modules",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "state": state,
        "runtime_profiles": _module_model_options(database),
        "ai_selection": _model_selection_value(state["ai_runtime_profile_id"], state["selected_ai_model"]),
        "backup_ai_selection": _model_selection_value(state["backup_ai_runtime_profile_id"], state["selected_backup_ai_model"]),
        "draft_content": draft_content,
        "versions": database.list_module_playbook_versions(
            str(state["company_id"]), str(state["module_id"])
        ),
        "has_unpublished_changes": (
            state.get("published_version_id") is None
            or draft_content != published_content
        ),
        "notice": notice,
        "error": error,
    }


def _runtime_profile_views(
    database: Database, *, active_only: bool = False
) -> list[dict[str, object]]:
    profiles = database.list_ai_runtime_profiles()
    if active_only:
        profiles = [profile for profile in profiles if profile.active]
    return [
        {
            "profile_id": profile.profile_id,
            "label": profile.label,
            "provider": profile.provider,
            "api_key_env": profile.api_key_env,
            "model": profile.model,
            "base_url": profile.base_url,
            "active": profile.active,
            "configured": runtime_profile_is_configured(profile),
            "provider_label": PROVIDERS.get(profile.provider, (profile.provider, ""))[0],
            "encrypted": bool(profile.api_key_ciphertext),
            "models": database.list_ai_models(profile.profile_id),
            "catalog_at": profile.catalog_updated_at,
            "test_message": ("Tes model lama; katalog belum diambil" if profile.last_test_status == "success"
                             and not profile.catalog_updated_at else CATALOG_MESSAGES.get(profile.last_test_status, "Belum diuji")),
            "test_status": profile.last_test_status,
            "test_at": profile.last_test_at,
        }
        for profile in profiles
    ]


def _runtime_profile_form_values(form) -> dict[str, str]:
    return {
        "provider": str(form.get("provider", "openai")).strip().casefold(),
    }


def _model_selection_value(profile_id: object, model: object) -> str:
    if not profile_id:
        return ""
    return f"{profile_id}|{model}" if model else str(profile_id)


def _module_model_options(database: Database) -> list[dict[str, object]]:
    options = []
    for profile in database.list_ai_runtime_profiles():
        if not profile.active:
            continue
        label = PROVIDERS.get(profile.provider, (profile.provider, ""))[0]
        configured = runtime_profile_is_configured(profile)
        # A legacy selection remains explicit, including after a catalog refresh.
        if profile.model:
            options.append({"value": profile.profile_id, "label": profile.label,
                            "model": profile.model, "disabled": False,
                            "reason": "Konfigurasi lama", "profile_id": profile.profile_id})
        for model in database.list_ai_models(profile.profile_id):
            options.append({"value": _model_selection_value(profile.profile_id, model["model_id"]),
                            "label": label, "model": model["model_id"],
                            "disabled": not model["selectable"] or not configured,
                            "reason": model["reason"] if configured else "Key belum siap",
                            "profile_id": profile.profile_id})
    return options


async def _credential_form(request: Request):
    # Bound the complete body before parsing; never persist or echo this body.
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 16_384:
            raise HTTPException(status_code=413, detail="Form AI terlalu besar.")
    request._body = bytes(data)
    form = await request.form(max_files=0, max_fields=12)
    if len(form.multi_items()) != len(form):
        raise HTTPException(status_code=400, detail="Field form ganda tidak diizinkan.")
    return form


def _runtime_profile_form_context(
    request: Request,
    settings: Settings,
    *,
    mode: str,
    profile: AIRuntimeProfile | None = None,
    values: dict[str, str] | None = None,
    error: str = "",
) -> dict[str, object]:
    defaults = {
        "label": profile.label if profile else "",
        "provider": profile.provider if profile else "openai",
        "api_key_env": profile.api_key_env if profile else "",
        "model": profile.model if profile else "",
        "base_url": profile.base_url if profile else "",
        "active": "1" if profile is None or profile.active else "0",
    }
    defaults.update(values or {})
    return {
        "active_page": "settings",
        "admin_username": settings.admin_username,
        "csrf_token": _csrf_token(request, settings),
        "mode": mode,
        "profile": profile,
        "values": defaults,
        "configured": runtime_profile_is_configured(profile) if profile else False,
        "encryption_ready": encryption_is_ready(),
        "providers": PROVIDERS,
        "error": error,
    }


def _ai_settings_redirect(notice: str = "", *, error: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in {"notice": notice, "error": error}.items() if value})
    return RedirectResponse("/admin/settings/ai" + (f"?{query}" if query else ""), status_code=303)


def _modules_redirect(
    notice: str = "", *, error: str = ""
) -> RedirectResponse:
    values = {
        key: value
        for key, value in {"notice": notice, "error": error}.items()
        if value
    }
    suffix = f"?{urlencode(values)}" if values else ""
    return RedirectResponse(f"/admin/modules{suffix}", status_code=303)


def _module_redirect(
    company_id: str,
    module_id: str,
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
        f"/admin/modules/{company_id}/{module_id}{suffix}", status_code=303
    )


_ACTIVITY_ACTION_LABELS = {
    "admin.login": "login",
    "admin.logout": "logout",
    "admin_user.created": "membuat akun admin",
    "admin_user.activated": "mengaktifkan akun admin",
    "admin_user.deactivated": "menonaktifkan akun admin",
    "communication_style.updated": "memperbarui gaya komunikasi",
    "shared_module.saved": "menyimpan draft modul bersama",
    "shared_module.published": "menerbitkan modul bersama",
    "learning.source_processed": "memproses PDF Learning",
    "learning.saved": "menyimpan draft buku Learning",
    "learning.published": "menerbitkan buku Learning",
    "ai_models.refreshed": "Katalog model AI diperiksa",
    "ai_runtime_profile.tested": "Koneksi AI diuji",
    "users.imported": "Import user baru",
    "company.created": "Company dibuat",
    "company.updated": "Company diperbarui",
    "company.activated": "Company diaktifkan",
    "company.deactivated": "Company dinonaktifkan",
    "user.created": "User dibuat",
    "user.updated": "User diperbarui",
    "user.activated": "User diaktifkan",
    "user.deactivated": "User dinonaktifkan",
    "membership.created": "Membership dibuat",
    "membership.updated": "Membership diperbarui",
    "membership.activated": "Membership diaktifkan",
    "membership.deactivated": "Membership dinonaktifkan",
    "company_instruction.draft_saved": "Draft instruction disimpan",
    "company_instruction.published": "Instruction dipublikasikan",
    "company_instruction.version_restored_to_draft": "Versi instruction dipulihkan",
    "knowledge_document.created": "Knowledge dibuat",
    "knowledge_document.draft_saved": "Draft knowledge disimpan",
    "knowledge_document.published": "Knowledge dipublikasikan",
    "knowledge_document.activated": "Knowledge diaktifkan",
    "knowledge_document.deactivated": "Knowledge dinonaktifkan",
    "knowledge_document.version_restored_to_draft": "Versi knowledge dipulihkan",
    "module.created": "Module dibuat",
    "module.updated": "Module diperbarui",
    "module.activated": "Module diaktifkan",
    "module.deactivated": "Module dinonaktifkan",
    "module_playbook.draft_saved": "Draft playbook disimpan",
    "module_playbook.published": "Playbook dipublikasikan",
    "module_playbook.version_restored_to_draft": "Versi playbook dipulihkan",
    "module_access.updated": "Akses module diperbarui",
    "ai_runtime_profile.created": "API credential dibuat",
    "ai_runtime_profile.updated": "API credential diperbarui",
    "ai_runtime_profile.activated": "API credential diaktifkan",
    "ai_runtime_profile.deactivated": "API credential dinonaktifkan",
}

_ACTIVITY_DETAIL_LABELS = {
    "secret_source": "Sumber key",
    "key_replaced": "API key diganti",
    "test_status": "Hasil tes koneksi",
    "created_users": "User baru",
    "created_memberships": "Membership baru",
    "skipped_rows": "Baris ID sudah terdaftar",
    "duplicate_rows": "Baris duplikat dalam file",
    "active": "Aktif",
    "character_count": "Karakter",
    "communication_profile": "Profil komunikasi",
    "company_id": "Company",
    "content_sha256": "Checksum konten",
    "division": "Divisi",
    "has_custom_instruction": "Custom instruction",
    "is_default": "Default",
    "job_title": "Jabatan",
    "name": "Nama",
    "name_after": "Nama baru",
    "name_before": "Nama sebelumnya",
    "role_level": "Role",
    "source_filename": "File sumber",
    "source_media_type": "Media type",
    "source_sha256": "Checksum file",
    "source_size_bytes": "Ukuran file",
    "source_version_number": "Versi sumber",
    "title": "Judul",
    "version_number": "Versi",
    "module_count": "Jumlah module",
    "module_ids": "Module",
    "short_code": "Kode singkat",
    "short_code_before": "Kode singkat sebelumnya",
    "short_code_after": "Kode singkat baru",
    "ai_runtime_profile_id": "AI utama",
    "ai_runtime_profile_before": "AI utama sebelumnya",
    "ai_runtime_profile_after": "AI utama baru",
    "backup_ai_runtime_profile_id": "AI cadangan",
    "backup_ai_runtime_profile_before": "AI cadangan sebelumnya",
    "backup_ai_runtime_profile_after": "AI cadangan baru",
    "api_key_env": "Environment key",
    "api_key_env_before": "Environment key sebelumnya",
    "api_key_env_after": "Environment key baru",
    "provider": "Provider",
    "provider_before": "Provider sebelumnya",
    "provider_after": "Provider baru",
    "model": "Model",
    "model_before": "Model sebelumnya",
    "model_after": "Model baru",
}


def _activity_event_view(row: dict[str, object]) -> dict[str, object]:
    action = str(row.get("action", ""))
    entity_type = str(row.get("entity_type", ""))
    category = {
        "admin": "admin",
        "admin_user": "admin",
        "company": "company",
        "user": "user",
        "membership": "membership",
        "company_instruction": "instruction",
        "knowledge_document": "knowledge",
        "module": "module",
        "module_playbook": "module",
        "module_access": "module",
        "shared_module": "module",
        "learning_book": "module",
        "learning_source": "module",
        "communication_style": "admin",
        "ai_runtime_profile": "credential",
    }.get(entity_type, "all")
    try:
        raw_details = json.loads(str(row.get("details_json", "{}")))
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_details = {}
    if not isinstance(raw_details, dict):
        raw_details = {}
    details = [
        {
            "label": _ACTIVITY_DETAIL_LABELS[str(key)],
            "value": _activity_detail_value(str(key), value),
        }
        for key, value in raw_details.items()
        if str(key) in _ACTIVITY_DETAIL_LABELS
    ]
    return {
        "action": action,
        "label": _ACTIVITY_ACTION_LABELS.get(
            action, action.replace("_", " ").replace(".", " · ").title()
        ),
        "category": category,
        "entity_id": str(row.get("entity_id", "")),
        "actor": str(row.get("actor", "")),
        "created_at": _format_activity_time(row.get("created_at", "")),
        "details": [],
        "summary": (
            _ACTIVITY_ACTION_LABELS.get(
                action, action.replace("_", " ").replace(".", " · ").title()
            )
            + (
                f" · {str(row.get('entity_id', ''))}"
                if action not in {"admin.login", "admin.logout"}
                and str(row.get("entity_id", ""))
                else ""
            )
        ),
    }


def _activity_detail_value(key: str, value: object) -> str:
    if isinstance(value, bool):
        return "Ya" if value else "Tidak"
    if key == "source_size_bytes":
        try:
            return f"{int(value) / 1024:.1f} KB"
        except (TypeError, ValueError):
            return str(value)
    if key.endswith("sha256"):
        checksum = str(value)
        return f"{checksum[:12]}…" if len(checksum) > 12 else checksum
    if key == "module_ids" and isinstance(value, list):
        return ", ".join(str(item) for item in value) or "Tidak ada"
    return str(value)


def _format_activity_time(value: object) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        wib = timezone(timedelta(hours=7))
        return timestamp.astimezone(wib).strftime("%d/%m/%Y %H:%M WIB")
    except ValueError:
        return str(value)


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
    role_level = str(form.get("role_level", "staff")).strip().casefold()
    return {
        "company_id": company_id or str(form.get("company_id", "")).strip(),
        "job_title": str(form.get("job_title", "")).strip(),
        "division": "",
        "role_level": role_level,
        "communication_profile": profile_id_for_role(role_level),
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
    notice: str = "",
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
        "module_access": (
            database.list_membership_module_access_admin(
                user.telegram_id, membership.company_id
            )
            if mode == "edit" and membership
            else []
        ),
        "notice": notice,
        "error": error,
    }


async def _bounded_import_request(request: Request) -> Request:
    limit = MAX_USER_IMPORT_BYTES + 64 * 1024
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="Upload terlalu besar. File Excel maksimal 5 MB.")
        body.extend(chunk)
    payload = bytes(body)
    del body

    async def receive():
        nonlocal payload
        current, payload = payload, b""
        return {"type": "http.request", "body": current, "more_body": False}

    return Request(request.scope, receive=receive)


def _role_options() -> list[dict[str, str]]:
    return [
        {"id": "owner", "label": "Owner / Board"},
        {"id": "gm", "label": "Executive / GM"},
        {"id": "manager", "label": "Manager / Head"},
        {"id": "supervisor", "label": "Supervisor / Coordinator"},
        {"id": "staff", "label": "Staff / Operational"},
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


def _membership_edit_redirect(
    telegram_id: int,
    company_id: str,
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
        f"/admin/users/{telegram_id}/memberships/{company_id}/edit{suffix}",
        status_code=303,
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
