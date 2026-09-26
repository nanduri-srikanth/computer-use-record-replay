"""ArtifactStore and lifecycle (D9): DRAFT -> APPROVED -> DEPRECATED, one way only.

Versions are immutable except for their status. A revision is a new version with
provenance pointing at its parent. Approving vN+1 deprecates the prior APPROVED version.

Approval is bound to content: approve() records the version's content hash in
<name>/approvals.json, and load() refuses an APPROVED version whose ledger entry is missing
(status flipped on disk) or whose content no longer matches the approved hash (edited after
review). Only approve() writes the ledger. This is tamper-evident, not tamper-proof: someone who
can rewrite both files can forge an approval, so production signs ledger entries (KMS) instead.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .contracts import SCHEMA_VERSION, ArtifactStatus, Capability
from .redactor import Redactor

_ALLOWED = {
    (ArtifactStatus.DRAFT, ArtifactStatus.APPROVED),
    (ArtifactStatus.APPROVED, ArtifactStatus.DEPRECATED),
}


class LifecycleError(Exception):
    pass


class IntegrityError(LifecycleError):
    """An APPROVED artifact on disk is not the content that was approved."""


class NotApproved(IntegrityError):
    """Status says APPROVED but no approval was ever recorded for this version."""


class ArtifactStore:
    def __init__(self, root: Path, redactor: Redactor | None = None):
        self.root = Path(root)
        self.redactor = redactor or Redactor()

    def _path(self, name: str, version: int) -> Path:
        return self.root / name / f"v{version}.json"

    def versions(self, name: str) -> list[int]:
        d = self.root / name
        return sorted(int(p.stem[1:]) for p in d.glob("v*.json")) if d.exists() else []

    def _ledger_path(self, name: str) -> Path:
        return self.root / name / "approvals.json"

    def approvals(self, name: str) -> dict[str, dict]:
        p = self._ledger_path(name)
        return json.loads(p.read_text()) if p.exists() else {}

    def _record_approval(self, cap: Capability) -> None:
        ledger = self.approvals(cap.name)
        ledger[f"v{cap.version}"] = {"content_hash": cap.content_hash(),
                                     "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self._ledger_path(cap.name).write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")

    def verify(self, cap: Capability) -> None:
        """Raises unless an APPROVED artifact matches its recorded approval. Other statuses need no approval."""
        if cap.status != ArtifactStatus.APPROVED:
            return
        entry = self.approvals(cap.name).get(f"v{cap.version}")
        if entry is None:
            raise NotApproved(f"{cap.name} v{cap.version} says APPROVED but has no recorded approval")
        if entry["content_hash"] != cap.content_hash():
            raise IntegrityError(f"{cap.name} v{cap.version} changed after approval "
                                 f"(approved {entry['content_hash']}, now {cap.content_hash()})")

    def load(self, name: str, version: int) -> Capability:
        p = self._path(name, version)
        if not p.exists():
            raise LifecycleError(f"{name} v{version} not found")
        raw = json.loads(p.read_text())
        if raw.get("schema_version") != SCHEMA_VERSION:  # say so plainly rather than fail as a hash or field mismatch
            raise LifecycleError(f"{name} v{version} is schema v{raw.get('schema_version')}, this build reads "
                                 f"v{SCHEMA_VERSION}; re-record or migrate it")
        cap = Capability.model_validate(raw)
        self.verify(cap)
        return cap

    def load_raw(self, name: str, version: int) -> str:
        return self._path(name, version).read_text()

    def latest(self, name: str, status: ArtifactStatus | None = ArtifactStatus.APPROVED) -> Capability | None:
        for v in reversed(self.versions(name)):
            try:
                cap = self.load(name, v)
            except NotApproved:  # never approved, whatever its status field says: not a candidate
                continue
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
        cap = self.load(name, version)  # verified: a tampered APPROVED version cannot be re-approved or deprecated
        if (cap.status, to) not in _ALLOWED:
            raise LifecycleError(f"{name} v{version}: {cap.status.value} -> {to.value} not allowed")
        updated = cap.model_copy(update={"status": to})
        if to == ArtifactStatus.APPROVED:
            self._record_approval(updated)
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
                try:
                    status = self.load(d.name, v).status.value
                except IntegrityError as e:
                    status = f"INVALID ({type(e).__name__})"
                out.append((d.name, v, status))
        return out
