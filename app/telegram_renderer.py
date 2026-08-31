from __future__ import annotations

import html
import re


TELEGRAM_MARKUP_CONTRACT = """Format teknis output untuk Telegram:
- Gunakan **teks** untuk judul, keputusan, atau penekanan kuat.
- Gunakan *teks* untuk penekanan ringan.
- Gunakan `teks` untuk command, pilihan, angka input, atau istilah teknis pendek.
- Gunakan ``` untuk blok kode bila benar-benar diperlukan.
- Gunakan baris yang diawali - untuk bullet.
- Link boleh ditulis sebagai [label](https://alamat).
- Jangan menulis HTML mentah.
- Jangan memakai tabel Markdown atau format bertumpuk/nested.
- Jangan bergantung pada heading # karena tampilan akhir memakai format Telegram."""


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
