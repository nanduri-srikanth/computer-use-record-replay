"""EvidenceSink: per-run event log, masked screenshots, and result. Everything is redacted."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .redactor import Redactor
from .surface import Snapshot


class EvidenceSink:
    def __init__(self, root: Path, run_id: str, redactor: Redactor):
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.redactor = redactor
        self._n = 0
        self.events: list[dict[str, Any]] = []

    def log(self, kind: str, **fields: Any) -> None:
        ev = {"ts": round(time.time(), 3), "kind": kind, **self.redactor.value(fields)}
        self.events.append(ev)
        with (self.dir / "events.jsonl").open("a") as f:
            f.write(json.dumps(ev, default=str) + "\n")

    def capture(self, snap: Snapshot, label: str) -> str:
        """Store a masked screenshot plus redacted page text; returns the evidence reference."""
        self._n += 1
        stem = f"evidence-{self._n:03d}-{label}"
        if snap.png:
            (self.dir / f"{stem}.png").write_bytes(snap.png)
        (self.dir / f"{stem}.txt").write_text(self.redactor.text(snap.text))
        return f"{self.run_id}/{stem}"

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(obj, indent=2, default=str))
        return p
