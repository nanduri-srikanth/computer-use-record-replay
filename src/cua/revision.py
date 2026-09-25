"""Human-proposed revisions (D9): captured handoff events become a new DRAFT for review.

Events carry type and target only (never typed values). The revision is never
auto-approved; a reviewer decides whether the captured actions belong in the
procedure or in the product's detector pack.
"""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import Capability, Provenance
from .store import ArtifactStore


def propose_revision(store: ArtifactStore, name: str, base_version: int, run_dir: Path) -> Capability:
    events = [json.loads(line) for line in (Path(run_dir) / "events.jsonl").read_text().splitlines()]
    human = [e for e in events if e["kind"] == "human_event"]
    handoffs = [e for e in events if e["kind"] == "intervention_requested"]
    if not human:
        raise ValueError("run has no captured human actions to propose")
    lines = [f"handoff at {h['step']}: {h['reason']}" for h in handoffs]
    lines += [f"operator {e['type']} {e['role']} '{e['name'] or e['label']}'" for e in human]
    base = store.load(name, base_version)
    draft = base.model_copy(update={"provenance": Provenance(
        created_by="human-revision", parent_version=base_version, source_run_id=Path(run_dir).name,
        notes="; ".join(lines)[:1000])})
    return store.save_draft(draft)
