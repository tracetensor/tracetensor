"""Harbor Hub / registry package reference parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RefType(str, Enum):
    TAG = "tag"
    REVISION = "revision"
    DIGEST = "digest"


@dataclass(frozen=True)
class PackageRef:
    org: str
    name: str
    ref: str = "latest"

    @property
    def qualified(self) -> str:
        return f"{self.org}/{self.name}@{self.ref}"

    @property
    def ref_type(self) -> RefType:
        if self.ref.isdigit():
            return RefType.REVISION
        if self.ref.startswith("sha256:"):
            return RefType.DIGEST
        return RefType.TAG


_PACKAGE_RE = re.compile(
    r"^(?:hub:)?(?P<org>[a-z0-9][a-z0-9.\-]*)/(?P<name>[a-z0-9][a-z0-9.\-]*)(?:@(?P<ref>[^@]+))?$",
    re.IGNORECASE,
)


def parse_package_ref(source: str) -> PackageRef:
    """Parse ``org/name`` or ``org/name@ref`` (optional ``hub:`` prefix)."""
    raw = source.strip()
    if not raw:
        raise ValueError("Package reference cannot be empty.")
    m = _PACKAGE_RE.match(raw)
    if not m:
        raise ValueError(
            f"Invalid package reference {source!r}. Expected org/name or org/name@latest."
        )
    return PackageRef(
        org=m.group("org"),
        name=m.group("name"),
        ref=m.group("ref") or "latest",
    )


def looks_like_package_ref(source: str) -> bool:
    try:
        parse_package_ref(source)
        return True
    except ValueError:
        return False
