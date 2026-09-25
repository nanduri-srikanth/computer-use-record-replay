"""Outcome detectors and recovery condition scanning (backbone item 8).

Driven by the vendor product's detector pack (config/detectors/*.yaml), so
product knowledge lives in config, not in the engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..config import DetectorPack
from ..surface import Surface

Kind = Literal["BUSINESS", "FAILURE", "KNOWN_DIALOG", "UNKNOWN_DIALOG", "BLOCKER", "APP_ERROR"]


@dataclass
class Detection:
    kind: Kind
    code: str  # business code, failure reason, dialog name, or blocker code
    frame: str | None
    detail: str
    dismiss: dict | None = None

    @property
    def terminal(self) -> bool:
        """Business outcomes and hard failures end the run; everything else is handled or escalated."""
        return self.kind in ("BUSINESS", "FAILURE")


class Detectors:
    def __init__(self, pack: DetectorPack, surface: Surface):
        self.pack, self.surface = pack, surface

    def _texts(self) -> dict[str | None, str]:
        frames = {r.get("frame") for r in self.pack.business_outcomes + self.pack.failures + self.pack.app_errors
                  + self.pack.blockers + self.pack.known_dialogs}
        return {f: self.surface.frame_text(f) for f in frames}

    def scan(self) -> Detection | None:
        """Highest-priority condition currently on screen, or None."""
        texts = self._texts()
        for r in self.pack.business_outcomes:
            if r["text"] in texts[r.get("frame")]:
                return Detection("BUSINESS", r["code"], r.get("frame"), r["text"])
        for r in self.pack.failures:
            if r["text"] in texts[r.get("frame")]:
                return Detection("FAILURE", r["reason"], r.get("frame"), r["text"])
        for frame in {r.get("frame") for r in self.pack.known_dialogs} | {"main"}:
            dialog_text = self.surface.dialog_visible(frame, self.pack.dialog_selector)
            if dialog_text is None:
                continue
            for r in self.pack.known_dialogs:
                if r.get("frame") == frame and r["text"] in dialog_text:
                    return Detection("KNOWN_DIALOG", r["name"], frame, r["text"], r["dismiss"])
            return Detection("UNKNOWN_DIALOG", "UNKNOWN_DIALOG", frame, dialog_text.strip().splitlines()[0][:80])
        for r in self.pack.blockers:
            if r["text"] in texts[r.get("frame")]:
                return Detection("BLOCKER", r["code"], r.get("frame"), r["text"])
        for r in self.pack.app_errors:
            if r["text"] in texts[r.get("frame")]:
                return Detection("APP_ERROR", "APP_ERROR", r.get("frame"), r["text"])
        return None

    def fingerprint(self) -> dict[str, str] | None:
        fp = self.pack.fingerprint
        m = re.search(fp["pattern"], self.surface.frame_text(fp.get("frame")))
        return m.groupdict() if m else None
