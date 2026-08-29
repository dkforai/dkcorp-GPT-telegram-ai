from __future__ import annotations

from pathlib import Path


def load_knowledge(directory: Path, max_chars: int) -> str:
    if max_chars <= 0 or not directory.exists():
        return ""

    sections: list[str] = []
    used = 0
    for path in sorted(directory.rglob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        section = f"## Sumber: {path.relative_to(directory)}\n\n{text}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        sections.append(section[:remaining])
        used += len(sections[-1])
    return "\n\n---\n\n".join(sections)

