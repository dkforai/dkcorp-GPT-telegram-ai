from __future__ import annotations

import html
import re
from dataclasses import dataclass


COPY_TEXT_START = "[[COPY_TEXT]]"
COPY_TEXT_END = "[[/COPY_TEXT]]"

TELEGRAM_OUTPUT_CONTRACT = f"""Format teknis output untuk Telegram:
- Jawaban biasa, analisis, pertanyaan, pilihan akun, dan tips tetap boleh terformat:
  **teks** untuk judul/penekanan, *teks* untuk italic, `teks` untuk command,
  pagar kode untuk kode, - untuk bullet, > untuk kutipan, dan [label](https://alamat).
- Jangan menulis HTML mentah, tabel Markdown, atau format bertumpuk/nested.
- KHUSUS hasil tulisan siap disalin/diposting (misalnya naskah Threads, caption
  Instagram, copy iklan, atau draft pesan), bungkus ISINYA SAJA dengan penanda
  {COPY_TEXT_START} dan {COPY_TEXT_END}, masing-masing pada baris tersendiri.
- Di dalam penanda, tulis teks polos tanpa bold, italic, >, spoiler, pagar kode,
  tanda kutip pembungkus, label pilihan, atau instruksi lanjutan. Pertahankan
  paragraf, hashtag, emoji, dan tanda baca konten. Tulis URL lengkap apa adanya.
- Setiap alternatif naskah memakai blok siap salin sendiri. Judul/nomor pilihan,
  penjelasan, tips, dan pertanyaan lanjutan diletakkan DI LUAR blok tersebut.
- Penanda hanya untuk sistem pengiriman, bukan bagian naskah. Jangan gunakan
  penanda pada jawaban biasa atau penjelasan tentang Threads/caption. Tentukan
  dari tujuan isi jawaban, bukan semata nama module atau adanya kata Threads.
- Aturan ini berlaku di General maupun module mana pun. Untuk sapaan/pilihan
  akun di Threads generator, tetap gunakan format biasa. Jangan mengubah substansi
  bisnis atau aturan keamanan demi format."""


@dataclass(frozen=True)
class TelegramResponsePart:
    text: str
    copyable: bool = False


def prepare_response_parts(text: str, max_chars: int) -> list[TelegramResponsePart]:
    """Keep ordinary Markdown, isolating only explicitly marked copy-ready text.

    Markers are recognized on standalone lines outside ordinary fenced code.
    An unclosed copy block stays plain through EOF; stray/nested protocol markers
    are removed. The visible-history character budget is shared across all parts.
    """
    raw_parts: list[TelegramResponsePart] = []
    buffer: list[str] = []
    copyable = False
    fence: str | None = None

    def flush() -> None:
        if buffer:
            raw_parts.append(TelegramResponsePart("".join(buffer), copyable))
            buffer.clear()

    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines(keepends=True):
        stripped = line.strip()
        if fence is not None:
            buffer.append(line)
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = None
            continue
        if not copyable:
            opening_fence = re.match(r"(`{3,}|~{3,})", stripped)
            if opening_fence:
                fence = opening_fence[0]
                buffer.append(line)
                continue
        if stripped in {COPY_TEXT_START, COPY_TEXT_END}:
            flush()
            copyable = stripped == COPY_TEXT_START
        else:
            buffer.append(line)
    flush()

    parts: list[TelegramResponsePart] = []
    remaining = max_chars
    for part in raw_parts:
        body = to_telegram_plain_text(part.text) if part.copyable else part.text.strip("\n")
        if not body.strip():
            continue
        if parts:
            remaining -= 2  # Separator used when saving visible text to history.
        if remaining <= 0:
            break
        body = body[:remaining]
        if body.strip():
            parts.append(TelegramResponsePart(body, part.copyable))
        remaining -= len(body)
    return parts


def to_telegram_plain_text(text: str) -> str:
    """Remove the legacy Markdown subset without interpreting HTML.

    This is a best-effort cleanup, not a general Markdown parser. Code, URLs,
    literal escapes and content punctuation are preserved. The caller must send
    with parse_mode=None; no Telegram formatting entities are generated here.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    placeholders: dict[str, str] = {}
    prefix = "\x00plain"
    while prefix in normalized:
        prefix += "_"

    def stash(rendered: str) -> str:
        token = f"{prefix}{len(placeholders)}\x00"
        placeholders[token] = rendered
        return token

    def replace_code_block(match: re.Match[str]) -> str:
        content = match.group(1).strip("\n")
        return stash(content)

    normalized = re.sub(
        r"(?m)^[ \t]*```[^\n]*\n(.*?)^[ \t]*```[ \t]*$",
        replace_code_block,
        normalized,
        flags=re.DOTALL,
    )

    def replace_inline_code(match: re.Match[str]) -> str:
        return stash(match.group(1))

    normalized = re.sub(r"(?<!\\)`([^`\n]+)(?<!\\)`", replace_inline_code, normalized)
    normalized = re.sub(r"\\([\\`*_{}\[\]()#+.!>|~-])", lambda m: stash(m[1]), normalized)

    def replace_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        return stash(url) if label == url else f"{label} ({stash(url)})"

    normalized = re.sub(
        r"!?\[([^\]\n]+)\]\((https?://(?:[^\s()]|\([^\s()]*\))+)\)",
        replace_link,
        normalized,
    )

    normalized = re.sub(r"https?://[^\s<>]+", lambda m: stash(m[0]), normalized)
    rendered = re.sub(r"(?m)^[ \t]*(?:>[ \t]+)+|^[ \t]*>+[ \t]*$", "", normalized)
    rendered = re.sub(r"(?m)^#{1,6}[ \t]+", "", rendered)
    rendered = re.sub(r"(?m)^[ \t]*([*_-])(?:[ \t]*\1){2,}[ \t]*$", "", rendered)
    rendered = re.sub(r"(?m)^([ \t]*)\*[ \t]+", r"\1- ", rendered)
    # Repeated passes handle combinations such as **bold and *italic***.
    for _ in range(3):
        previous = rendered
        for marker in ("**", "__", "~~", "||", "*", "_"):
            escaped = re.escape(marker)
            rendered = re.sub(
                rf"(?<![\w{escaped}]){escaped}(?=\S)([^\n]+?)(?<=\S){escaped}(?![\w{escaped}])",
                r"\1", rendered,
            )
        if rendered == previous:
            break

    for token, replacement in reversed(placeholders.items()):
        rendered = rendered.replace(token, replacement)
    return rendered.strip("\n")


def markdown_to_telegram_html(text: str) -> str:
    """Convert a deliberately small Markdown subset to safe Telegram HTML.

    All source HTML is escaped first. Only tags created by this renderer can
    reach Telegram, preventing model output from injecting unsupported tags.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    placeholders: dict[str, str] = {}

    def stash(rendered: str) -> str:
        token = f"\ue000{len(placeholders)}\ue001"
        placeholders[token] = rendered
        return token

    def replace_code_block(match: re.Match[str]) -> str:
        content = match.group(1).strip("\n")
        return stash(f"<pre>{html.escape(content, quote=False)}</pre>")

    normalized = re.sub(
        r"```(?:[A-Za-z0-9_+.-]+\n)?(.*?)```",
        replace_code_block,
        normalized,
        flags=re.DOTALL,
    )

    def replace_inline_code(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1), quote=False)}</code>")

    normalized = re.sub(r"`([^`\n]+)`", replace_inline_code, normalized)

    def replace_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        url = html.escape(match.group(2), quote=True)
        return stash(f'<a href="{url}">{label}</a>')

    normalized = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
        replace_link,
        normalized,
    )

    rendered = html.escape(normalized, quote=False)
    rendered = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", rendered)
    rendered = re.sub(r"(?m)^[ \t]*[-*][ \t]+", "• ", rendered)
    rendered = re.sub(r"(?m)^&gt;[ \t]?(.+)$", r"<blockquote>\1</blockquote>", rendered)
    rendered = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", rendered)
    rendered = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", rendered)
    rendered = re.sub(r"\|\|([^|\n]+)\|\|", r"<tg-spoiler>\1</tg-spoiler>", rendered)

    for token, replacement in placeholders.items():
        rendered = rendered.replace(token, replacement)
    return rendered.strip()
