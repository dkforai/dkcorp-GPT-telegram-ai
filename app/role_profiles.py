from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommunicationProfile:
    profile_id: str
    label: str
    response_level: str
    focus: tuple[str, ...]
    default_structure: tuple[str, ...]
    avoid: tuple[str, ...]

    def as_prompt(self) -> str:
        lines = [
            f"Communication profile: {self.label} ({self.profile_id})",
            f"Level jawaban: {self.response_level}",
        ]
        if self.focus:
            lines.append("Fokus: " + "; ".join(self.focus))
        if self.default_structure:
            lines.append("Struktur default: " + " → ".join(self.default_structure))
        if self.avoid:
            lines.append("Hindari: " + "; ".join(self.avoid))
        lines.append(
            "Sesuaikan struktur dengan pertanyaan. Jangan memaksakan semua bagian jika tidak relevan."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class RoleProfiles:
    default: CommunicationProfile
    by_id: dict[str, CommunicationProfile]
    aliases: dict[str, CommunicationProfile]


def load_role_profiles(path: Path) -> RoleProfiles:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_profiles = raw.get("profiles")
    default_id = str(raw.get("default_profile", "default")).strip().casefold()
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError(f"{path} harus memiliki array 'profiles'")

    by_id: dict[str, CommunicationProfile] = {}
    aliases: dict[str, CommunicationProfile] = {}
    for item in raw_profiles:
        profile_id = str(item["id"]).strip().casefold()
        if not profile_id or profile_id in by_id:
            raise ValueError(f"Profile ID duplikat atau kosong di {path}")
        profile = CommunicationProfile(
            profile_id=profile_id,
            label=str(item.get("label", profile_id)).strip(),
            response_level=str(item.get("response_level", "balanced")).strip(),
            focus=_string_tuple(item.get("focus", [])),
            default_structure=_string_tuple(item.get("default_structure", [])),
            avoid=_string_tuple(item.get("avoid", [])),
        )
        by_id[profile_id] = profile
        for role in item.get("role_aliases", []):
            alias = str(role).strip().casefold()
            if alias:
                aliases[alias] = profile

    if default_id not in by_id:
        raise ValueError(f"Default profile '{default_id}' tidak ditemukan di {path}")
    return RoleProfiles(default=by_id[default_id], by_id=by_id, aliases=aliases)


def resolve_communication_profile(
    configured_profile: str, role: str, profiles: RoleProfiles
) -> CommunicationProfile:
    profile_id = configured_profile.strip().casefold()
    if profile_id:
        return profiles.by_id.get(profile_id, profiles.default)
    return profiles.aliases.get(role.strip().casefold(), profiles.default)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("Field profile harus berupa array")
    return tuple(str(item).strip() for item in value if str(item).strip())

