"""Recorder: turns verified discovery actions into artifact steps.

For each target it builds ranked locator candidates and keeps only those that
resolve to exactly one element at record time, so replay's ambiguity rule is
meaningful. Checkpoints and anchors use static page text only; anything the
Redactor would change (PII, ids, amounts) is never stored.
"""

from __future__ import annotations

import re
from typing import Any

from ..contracts import (ActionType, Capability, Checkpoint, LocatorCandidate, LocatorKind, Provenance,
                         RiskTier, Step, TargetDescriptor)
from ..redactor import Redactor
from ..surface import ElementInfo, Surface

_ROLES = {"button", "link", "textbox", "combobox", "checkbox", "radio"}


class Recorder:
    def __init__(self, surface: Surface, redactor: Redactor):
        self.surface = surface
        self.redactor = redactor
        self.steps: list[Step] = []

    def is_static(self, s: str | None) -> bool:
        return bool(s) and len(s) <= 60 and not re.search(r"\d", s) and self.redactor.is_clean(s)

    def _unique(self, frame: str | None, c: LocatorCandidate) -> bool:
        return self.surface.resolve(frame, c).count == 1

    def candidates_for(self, el: ElementInfo) -> list[LocatorCandidate]:
        out: list[LocatorCandidate] = []
        if el.role in _ROLES and self.is_static(el.name):
            c = LocatorCandidate(kind=LocatorKind.ROLE_NAME, role=el.role, name=el.name)
            if self._unique(el.frame, c):
                out.append(c)
        if self.is_static(el.label) and el.tag in ("input", "select", "textarea", "td"):
            c = LocatorCandidate(kind=LocatorKind.LABEL_PROXIMITY, label=el.label, element=el.tag)
            if self._unique(el.frame, c):
                out.append(c)
        if el.column and el.row_cells:
            for i, cell in enumerate(el.row_cells, start=1):
                if i == el.column or not self.is_static(cell):
                    continue
                c = LocatorCandidate(kind=LocatorKind.TABLE_ANCHOR, anchor_text=cell, column=el.column, element=el.tag)
                if self._unique(el.frame, c):
                    out.append(c)
                    break
        if el.tag != "select":  # a coordinate click cannot choose an option, so it is no fallback for SELECT
            out.append(LocatorCandidate(kind=LocatorKind.COORDINATES, x=round(el.x, 1), y=round(el.y, 1)))
        if not out:
            raise ValueError(f"no stable locator for {self.describe(el)}")
        return out

    def checkpoint(self, frame: str | None, headings: list[str], with_label: bool = False) -> Checkpoint | None:
        """Static heading text of the frame; with_label also adds a static field label (a stronger success condition)."""
        texts = [h for h in headings if self.is_static(h)][:1]
        if with_label and texts:
            labels = [e.label for e in self.surface.observe().elements
                      if e.frame == frame and e.role == "cell" and self.is_static(e.label)]
            texts += labels[:1]
        return Checkpoint(frame=frame, text_present=texts) if texts else None

    def describe(self, el: ElementInfo) -> str:
        what = el.name or el.label or el.role
        return self.redactor.text(f"{el.role} '{what}'")[:80]

    def record(self, *, action: ActionType, el: ElementInfo, description: str, risk: RiskTier,
               pre: Checkpoint | None, post: Checkpoint | None, value_from: str | None = None,
               output: str | None = None, candidates: list[LocatorCandidate] | None = None) -> Step:
        """candidates must be built before the action runs, while the element is still on the page."""
        if output:  # re-extracting an output replaces the earlier step
            self.steps = [s for s in self.steps if s.output != output]
        step = Step(id=f"s{len(self.steps) + 1}", action=action, description=self.redactor.text(description)[:120],
                    target=TargetDescriptor(frame=el.frame, description=self.describe(el),
                                            candidates=candidates or self.candidates_for(el)),
                    value_from=value_from, output=output, risk=risk, pre=pre, post=post)
        self.steps.append(step)
        return step

    def build(self, *, spec: Any, app_version: str, run_id: str, model: str, success: Checkpoint | None) -> Capability:
        """spec is a DiscoverySpec (name, goal, vendor_product, start_route, inputs, outputs)."""
        if success is None:
            raise ValueError("no static text on the final page to use as the success condition")
        steps = [s.model_copy(update={"id": f"s{i}"}) for i, s in enumerate(self.steps, start=1)]
        return Capability(name=spec.name, version=1, goal=spec.goal, vendor_product=spec.vendor_product,
                          compatible_versions=[app_version], start_route=spec.start_route, inputs=spec.inputs,
                          outputs=spec.outputs, steps=steps, success=success,
                          provenance=Provenance(created_by="discovery", source_run_id=run_id, model=model))
