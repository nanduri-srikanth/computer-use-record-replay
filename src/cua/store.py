"""ArtifactStore and lifecycle (D9): DRAFT -> APPROVED -> DEPRECATED, one way only.

Versions are immutable except for their status. A revision is a new version with
provenance pointing at its parent. Approving vN+1 deprecates the prior APPROVED version.
"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import ArtifactStatus, Capability
from .redactor import Redactor

_ALLOWED = {
    (ArtifactStatus.DRAFT, ArtifactStatus.APPROVED),
    (ArtifactStatus.APPROVED, ArtifactStatus.DEPRECATED),
}


class LifecycleError(Exception):
    pass


class ArtifactStore:
    def __init__(self, root: Path, redactor: Redactor | None = None):
        self.root = Path(root)
        self.redactor = redactor or Redactor()

    def _path(self, name: str, version: int) -> Path:
        return self.root / name / f"v{version}.json"

    def versions(self, name: str) -> list[int]:
        d = self.root / name
        return sorted(int(p.stem[1:]) for p in d.glob("v*.json")) if d.exists() else []

    def load(self, name: str, version: int) -> Capability:
        return Capability.model_validate_json(self._path(name, version).read_text())

    def load_raw(self, name: str, version: int) -> str:
        return self._path(name, version).read_text()

    def latest(self, name: str, status: ArtifactStatus | None = ArtifactStatus.APPROVED) -> Capability | None:
        for v in reversed(self.versions(name)):
            cap = self.load(name, v)
            if status is None or cap.status == status:
                return cap
        return None

    def _write(self, cap: Capability) -> None:
        body = cap.model_dump(mode="json", exclude_none=True)
        text = json.dumps(body, indent=2)
        if self.redactor.text(text) != text:
            raise LifecycleError("artifact contains redactable data (PII or secrets); refusing to store")
        p = self._path(cap.name, cap.version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")

    def save_draft(self, cap: Capability) -> Capability:
        """Store as a new DRAFT version (version number assigned here)."""
        version = (self.versions(cap.name) or [0])[-1] + 1
        draft = cap.model_copy(update={"version": version, "status": ArtifactStatus.DRAFT})
        self._write(Capability.model_validate(draft.model_dump()))
        return draft

    def _transition(self, name: str, version: int, to: ArtifactStatus) -> Capability:
        cap = self.load(name, version)
        if (cap.status, to) not in _ALLOWED:
            raise LifecycleError(f"{name} v{version}: {cap.status.value} -> {to.value} not allowed")
        updated = cap.model_copy(update={"status": to})
        self._write(updated)
        return updated

    def approve(self, name: str, version: int) -> Capability:
        prior = self.latest(name, ArtifactStatus.APPROVED)
        approved = self._transition(name, version, ArtifactStatus.APPROVED)
        if prior and prior.version != version:
            self._transition(name, prior.version, ArtifactStatus.DEPRECATED)
        return approved

    def deprecate(self, name: str, version: int) -> Capability:
        return self._transition(name, version, ArtifactStatus.DEPRECATED)

    def list_all(self) -> list[tuple[str, int, str]]:
        out = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()) if self.root.exists() else []:
            for v in self.versions(d.name):
                out.append((d.name, v, self.load(d.name, v).status.value))
        return out
