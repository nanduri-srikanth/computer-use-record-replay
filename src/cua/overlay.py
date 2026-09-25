"""Tenant overlay merge (D8). Deterministic; overlays can only relabel and override locators."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from .contracts import Capability, Checkpoint, TenantOverlay


class OverlayRejected(Exception):
    pass


def load_overlay(path: Path) -> TenantOverlay:
    try:
        return TenantOverlay.model_validate(json.loads(Path(path).read_text()))
    except (ValidationError, ValueError) as e:
        raise OverlayRejected(f"{path}: {e.__class__.__name__}: {str(e).splitlines()[0]}") from None


def _relabel(s: str | None, m: dict[str, str]) -> str | None:
    return m.get(s, s) if s is not None else None


def _cp(cp: Checkpoint | None, m: dict[str, str]) -> Checkpoint | None:
    return cp.model_copy(update={"text_present": [m.get(t, t) for t in cp.text_present]}) if cp else None


def merge(base: Capability, overlay: TenantOverlay | None) -> Capability:
    if overlay is None:
        return base
    if overlay.base_capability != base.name:
        raise OverlayRejected(f"overlay targets {overlay.base_capability}, not {base.name}")
    unknown = set(overlay.locator_overrides) - {s.id for s in base.steps}
    if unknown:
        raise OverlayRejected(f"overlay overrides unknown steps {sorted(unknown)}")
    m = overlay.label_map
    steps = []
    for s in base.steps:
        target = s.target
        if target is not None:
            cands = overlay.locator_overrides.get(s.id) or [
                c.model_copy(update={"name": _relabel(c.name, m), "label": _relabel(c.label, m),
                                     "anchor_text": _relabel(c.anchor_text, m)})
                for c in target.candidates]
            target = target.model_copy(update={"candidates": cands})
        steps.append(s.model_copy(update={"target": target, "pre": _cp(s.pre, m), "post": _cp(s.post, m)}))
    merged = base.model_copy(update={"steps": steps})
    return Capability.model_validate(merged.model_dump())  # re-validate: ladder order etc.
