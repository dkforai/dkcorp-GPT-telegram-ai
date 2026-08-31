from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.database import Company
from app.knowledge import load_knowledge


@dataclass(frozen=True)
class CompanyContent:
    profile: str
    instruction: str
    knowledge: str


def load_company_content(
    company: Company, project_root: Path, max_chars: int
) -> CompanyContent:
    root = project_root.resolve()
    profile = _read_scoped_file(root, company.profile_file, max_chars=10_000)
    instruction = _read_scoped_file(
        root, company.instruction_file, max_chars=max_chars
    )
    knowledge_dir = _scoped_path(root, company.knowledge_dir)
    knowledge = (
        load_knowledge(knowledge_dir, max_chars)
        if knowledge_dir is not None
        else ""
    )
    return CompanyContent(
        profile=profile,
        instruction=instruction,
        knowledge=knowledge,
    )


def _read_scoped_file(root: Path, configured_path: str, max_chars: int) -> str:
    path = _scoped_path(root, configured_path)
    if path is None or not path.is_file() or max_chars <= 0:
        return ""
    return path.read_text(encoding="utf-8").strip()[:max_chars]


def _scoped_path(root: Path, configured_path: str) -> Path | None:
    if not configured_path:
        return None
    candidate = (root / configured_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path company berada di luar project root: {configured_path}")
    return candidate
