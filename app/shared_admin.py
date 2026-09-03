"""Admin routes for global modules and scheduled books. All mutations use CSRF."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import PurePath

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.learning_ingestion import MAX_BOOK_BYTES, extract_book
from app.shared_modules import SharedStore, WIB, now, display_time


def register_shared_routes(app, settings, database, templates):
    from app.admin import _login_redirect, _csrf_token, _valid_csrf, _module_model_options
    store = SharedStore(database)
    processing = asyncio.Semaphore(1)

    def page(request, name, active, **context):
        status = context.pop("status_code", 200)
        return templates.TemplateResponse(request=request, name=f"admin/{name}.html", status_code=status, context={
            "admin_username": settings.admin_username, "csrf_token": _csrf_token(request, settings),
            "active_page": active, "error": "", "notice": request.query_params.get("notice", ""),
            **context,
        })

    async def form_data(request, upload=False):
        total = bytearray()
        limit = MAX_BOOK_BYTES + 300_000 if upload else 200_000
        async for chunk in request.stream():
            total.extend(chunk)
            if len(total) > limit:
                raise HTTPException(413, "Form atau file terlalu besar")
        request._body = bytes(total)
        form = await request.form(max_files=1 if upload else 0, max_fields=20, max_part_size=200_000)
        if len(form.multi_items()) != len(form):
            await form.close()
            raise HTTPException(400, "Field ganda tidak diizinkan")
        if not _valid_csrf(str(form.get("csrf_token", "")), request, settings):
            await form.close()
            raise HTTPException(403, "Permintaan tidak valid. Muat ulang halaman.")
        return form

    def module_editor(request, row=None, values=None, error="", status_code=200):
        learning = row and row["kind"] == "learning"
        defaults = {"name": "", "description": "", "instruction": "", "context_mode": "independent", "ai_selection": "", "backup_ai_selection": "", "short_code": "", "active": "1"}
        if row:
            defaults.update(json.loads(row["draft_json"]))
            defaults.update(short_code=row["short_code"], active="1" if row["active"] else "0")
        defaults.update(values or {})
        return page(request, "shared_module_form", "learning" if learning else "shared", row=row, values=defaults,
                    learning=learning, runtime_profiles=_module_model_options(database), error=error, status_code=status_code)

    @app.get("/admin/shared-modules")
    async def modules(request: Request):
        if redirect := _login_redirect(request, settings):
            return redirect
        rows = [{**r, **json.loads(r["draft_json"])} for r in store.modules()]
        return page(request, "shared_modules", "shared", modules=rows)

    @app.get("/admin/shared-modules/new")
    async def new_module(request: Request):
        if redirect := _login_redirect(request, settings):
            return redirect
        return module_editor(request)

    @app.get("/admin/shared-modules/{module_id}")
    async def edit_module(request: Request, module_id: str):
        if redirect := _login_redirect(request, settings):
            return redirect
        row = store.module(module_id)
        if not row:
            raise HTTPException(404)
        return module_editor(request, row)

    @app.post("/admin/shared-modules/{module_id}")
    async def save_module(request: Request, module_id: str):
        if redirect := _login_redirect(request, settings):
            return redirect
        form = await form_data(request)
        try:
            values = {key: str(value) for key, value in form.items()}
            values.setdefault("active", "0")
            row = store.module(module_id) if module_id != "new" else None
            if module_id != "new" and not row:
                raise HTTPException(404)
            if values.get("action") not in {"draft", "publish"}:
                raise ValueError("Pilih simpan draft atau publish")
            saved = store.save_module(None if module_id == "new" else module_id, values, settings.admin_username,
                                      publish=values["action"] == "publish", expected_revision=values.get("revision"))
            return RedirectResponse(f"/admin/shared-modules/{saved}?notice=Berhasil+disimpan", 303)
        except ValueError as exc:
            return module_editor(request, row, values, str(exc), 400)
        finally:
            await form.close()

    @app.get("/admin/learning")
    async def learning(request: Request):
        if redirect := _login_redirect(request, settings):
            return redirect
        rows = []
        for row in store.books():
            draft = json.loads(row["draft_json"])
            live = json.loads(row["live_json"]) if row["live_json"] else None
            state = "Nonaktif" if not row["active"] else "Draft"
            if live and row["active"]:
                state = "Terjadwal" if now() < live["start_utc"] else "Berakhir" if now() >= live["end_utc"] else "Aktif"
            shown = live or draft
            rows.append({**row, **draft, "state": state, "start_label": display_time(shown["start_utc"]), "end_label": display_time(shown["end_utc"])})
        return page(request, "learning", "learning", books=rows, module=store.module("learning"))

    def book_editor(request, row=None, values=None, error="", status_code=200):
        today = datetime.now(WIB).date()
        defaults = {"title": "", "description": "", "instruction": "", "source_id": "", "reviewed": False,
                    "starts_at": f"{today}T00:00", "ends_at": f"{today + timedelta(days=7)}T00:00", "active": "1"}
        if row:
            defaults.update(json.loads(row["draft_json"]))
            defaults["active"] = "1" if row["active"] else "0"
        defaults.update(values or {})
        source = store.source(defaults["source_id"]) if defaults["source_id"] else None
        report = json.loads(source["report_json"]) if source else None
        return page(request, "learning_book", "learning", row=row, values=defaults, source=source, report=report, error=error, status_code=status_code)

    @app.get("/admin/learning/books/new")
    async def new_book(request: Request):
        if redirect := _login_redirect(request, settings):
            return redirect
        return book_editor(request)

    @app.get("/admin/learning/books/{book_id}")
    async def edit_book(request: Request, book_id: str):
        if redirect := _login_redirect(request, settings):
            return redirect
        row = store.book(book_id)
        if not row:
            raise HTTPException(404)
        return book_editor(request, row)

    @app.post("/admin/learning/books/{book_id}")
    async def save_book(request: Request, book_id: str):
        if redirect := _login_redirect(request, settings):
            return redirect
        form = await form_data(request, upload=True)
        row = store.book(book_id) if book_id != "new" else None
        if book_id != "new" and not row:
            await form.close()
            raise HTTPException(404)
        values = {key: str(value) for key, value in form.items() if not isinstance(value, UploadFile)}
        values.setdefault("active", "0")
        values.setdefault("reviewed", "0")
        try:
            if values.get("action") not in {"draft", "publish"}:
                raise ValueError("Pilih simpan draft atau publish")
            file = form.get("file")
            if isinstance(file, UploadFile) and file.filename:
                if not file.filename.lower().endswith(".pdf"):
                    raise ValueError("Learning menerima file PDF")
                if values["action"] == "publish":
                    raise ValueError("PDF baru harus diproses dan ditinjau dulu. Gunakan Simpan draft & proses PDF.")
                if processing.locked():
                    raise ValueError("Ada PDF sedang diproses. Coba lagi setelah pemrosesan selesai.")
                async with processing:
                    pdf = await file.read(MAX_BOOK_BYTES + 1)
                    source_id = hashlib.sha256(pdf).hexdigest()
                    existing = store.source(source_id)
                    if not existing:
                        extracted = await run_in_threadpool(extract_book, pdf)
                        source_id = await run_in_threadpool(store.save_source, PurePath(file.filename).name, pdf, extracted, settings.admin_username)
                    values["source_id"] = source_id
                    values["reviewed"] = "0"
                    if not values.get("title", "").strip():
                        values["title"] = PurePath(file.filename).stem[:150]
            saved = store.save_book(None if book_id == "new" else book_id, values, settings.admin_username,
                                   publish=values["action"] == "publish", expected_revision=values.get("revision"))
            return RedirectResponse(f"/admin/learning/books/{saved}?notice=Materi+berhasil+disimpan", 303)
        except ValueError as exc:
            values["reviewed"] = values.get("reviewed") == "1"
            return book_editor(request, row, values, str(exc), 400)
        finally:
            await form.close()

    @app.get("/admin/learning/sources/{source_id}")
    async def view_source(request: Request, source_id: str, page_number: int = 1):
        if redirect := _login_redirect(request, settings):
            return redirect
        source = store.source(source_id)
        if not source:
            raise HTTPException(404)
        pages = json.loads(source["pages_json"])
        if not 1 <= page_number <= len(pages):
            raise HTTPException(404)
        return page(request, "learning_source", "learning", source=source, page_number=page_number, count=len(pages), text=pages[page_number-1])
