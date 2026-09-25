"""Shared helpers for the independent review tests. Uses only the project's public APIs."""

from __future__ import annotations

import json
from pathlib import Path

from cua.contracts import (ActionType, Capability, Checkpoint, LocatorCandidate, LocatorKind, Provenance, Step,
                           TargetDescriptor)

from ..conftest import ROOT

EVIDENCE_V4 = ROOT / "evidence" / "artifacts" / "get_savings_balance" / "v4.json"


def evidence_artifact() -> Capability:
    """A real, live-discovered, APPROVED artifact from /evidence (it carries COORDINATES fallbacks)."""
    return Capability.model_validate_json(EVIDENCE_V4.read_text())


def replace_candidates(cap: Capability, step_id: str, candidates: list[LocatorCandidate], **step_updates) -> Capability:
    steps = []
    for s in cap.steps:
        if s.id == step_id:
            s = s.model_copy(update={"target": s.target.model_copy(update={"candidates": candidates}), **step_updates})
        steps.append(s)
    return Capability.model_validate(cap.model_copy(update={"steps": steps}).model_dump())


def coords(x: float, y: float) -> LocatorCandidate:
    return LocatorCandidate(kind=LocatorKind.COORDINATES, x=round(x, 1), y=round(y, 1))


def open_main(surface, base_url: str, path: str) -> None:
    """Load the frameset, then point the main frame at a page (used only to measure element positions)."""
    surface.goto("/")
    main = surface.page.frame(name="main")
    main.goto(base_url + path, wait_until="load")


def element(surface, frame: str, *, role: str | None = None, name: str | None = None, label: str | None = None,
            index: int = 0):
    els = [e for e in surface.observe().elements if e.frame == frame
           and (role is None or e.role == role) and (name is None or e.name == name)
           and (label is None or e.label == label)]
    assert len(els) > index, f"element not found: {role} {name} {label} in {frame}"
    return els[index]


def probe_capability(steps: list[Step], success: Checkpoint, **kw) -> Capability:
    return Capability(name="probe", version=1, status="APPROVED", goal="independent probe",
                      vendor_product="CoreOne Banking", compatible_versions=["4.2.1"], inputs=kw.get("inputs", []),
                      outputs=kw.get("outputs", []), steps=steps, success=success,
                      provenance=Provenance(created_by="hand-authored"))


def click_step(step_id: str, frame: str, candidates: list[LocatorCandidate], post: Checkpoint | None = None) -> Step:
    return Step(id=step_id, action=ActionType.CLICK, description="probe click",
                target=TargetDescriptor(frame=frame, description="probe target", candidates=candidates), post=post)


def events(runs_dir: Path, run_id: str) -> list[dict]:
    return [json.loads(line) for line in (Path(runs_dir) / run_id / "events.jsonl").read_text().splitlines()]


def run_dir_text(runs_dir: Path, run_id: str) -> str:
    d = Path(runs_dir) / run_id
    return "\n".join(p.read_text(errors="ignore") for p in d.iterdir() if p.suffix in (".json", ".jsonl", ".txt"))
