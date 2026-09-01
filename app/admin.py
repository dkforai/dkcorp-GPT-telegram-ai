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
from app.database import Database


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
        for row in rows:
            row["knowledge_count"] = _markdown_count(
                root, str(row.get("knowledge_dir", ""))
            )
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
        company_id = str(form.get("company_id", ""))
        name = str(form.get("name", ""))
        active = form.get("active") == "1"
        try:
            database.create_company(
                company_id, name, actor=settings.admin_username, active=active
            )
        except ValueError as exc:
            return templates.TemplateResponse(
                request=request,
                name="admin/company_form.html",
                context=_company_form_context(
                    request,
                    settings,
                    mode="create",
                    company_id=company_id,
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
            },
        )

    @app.get("/admin/{section}", response_class=HTMLResponse)
    async def placeholder(request: Request, section: str):
        redirect = _login_redirect(request, settings)
        if redirect:
            return redirect
        labels = {
            "knowledge": "Knowledge",
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


def _markdown_count(root: Path, configured_path: str) -> int:
    if not configured_path:
        return 0
    path = (root / configured_path).resolve()
    if path != root and root not in path.parents:
        return 0
    if not path.is_dir():
        return 0
    return sum(1 for item in path.rglob("*.md") if item.is_file())
