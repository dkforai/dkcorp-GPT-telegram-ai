"""Bounded PDF text extraction in an isolated, time-limited process. No OCR/AI."""
from __future__ import annotations

import json
import subprocess
import sys
from io import BytesIO

MAX_BOOK_BYTES = 20 * 1024 * 1024
MAX_BOOK_CHARS = 2_000_000
MAX_BOOK_PAGES = 1000


def extract_book(pdf: bytes) -> dict:
    if not pdf or len(pdf) > MAX_BOOK_BYTES or not pdf.startswith(b"%PDF-"):
        raise ValueError("Gunakan PDF valid maksimal 20 MB")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "app.learning_ingestion"], input=pdf,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=40,
        )
        if result.returncode:
            raise ValueError("PDF gagal diproses atau melebihi batas pemrosesan. Coba PDF teks yang lebih kecil.")
        data = json.loads(result.stdout)
        if "error" in data:
            raise ValueError(data["error"])
        return data
    except subprocess.TimeoutExpired:
        raise ValueError("Pemrosesan PDF melebihi 40 detik. Coba PDF teks yang lebih kecil.") from None


def _extract(pdf):
    from pypdf import PdfReader
    try:
        reader = PdfReader(BytesIO(pdf))
        if reader.is_encrypted:
            return {"error": "PDF terkunci. Unggah PDF tanpa password."}
        if len(reader.pages) > MAX_BOOK_PAGES:
            return {"error": "PDF maksimal 1.000 halaman. Tidak ada isi yang dipotong otomatis."}
        pages, empty, chars = [], [], 0
        for number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or "").replace("\x00", "").strip()
            if not text:
                empty.append(number)
            pages.append(text)
            chars += len(text)
            if chars > MAX_BOOK_CHARS:
                return {"error": "Teks buku melebihi 2 juta karakter. Tidak ada isi yang dipotong otomatis."}
        if not chars:
            return {"error": "PDF tidak memiliki teks terbaca. PDF scan memerlukan OCR sebelum diunggah."}
        return {"pages": pages, "report": {"pages": len(pages), "readable_pages": len(pages) - len(empty), "empty_pages": empty, "characters": chars}}
    except Exception:
        return {"error": "PDF rusak atau tidak dapat diekstraksi. Simpan ulang sebagai PDF teks."}


if __name__ == "__main__":
    # The worker cannot consume unbounded CPU/address space or log book contents.
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    payload = sys.stdin.buffer.read(MAX_BOOK_BYTES + 1)
    result = _extract(payload) if len(payload) <= MAX_BOOK_BYTES else {"error": "PDF terlalu besar"}
    sys.stdout.write(json.dumps(result))
