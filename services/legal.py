"""
End User Licence Agreement helpers.

The current EULA lives in legal/eula.txt. The version is a hash of that file,
so any edit of the text is treated as a new version and users must accept again.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_EULA_PATH = Path(__file__).resolve().parent.parent / "legal" / "eula.txt"


def current_eula_text() -> str:
    if not _EULA_PATH.is_file():
        raise FileNotFoundError(f"EULA file missing at {_EULA_PATH}")
    return _EULA_PATH.read_text(encoding="utf-8").strip()


def current_eula_version() -> str:
    return hashlib.sha256(current_eula_text().encode("utf-8")).hexdigest()[:16]


def has_accepted_current_eula(user: dict | None) -> bool:
    if not user:
        return False
    accepted = user.get("eula_accepted_version")
    if not accepted:
        return False
    return accepted == current_eula_version()
