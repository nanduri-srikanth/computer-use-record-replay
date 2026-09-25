"""Loads policy, tenants, detector packs, and budgets from config/."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .contracts import TenantConfig

ROOT = Path(os.environ.get("CUA_ROOT", Path(__file__).resolve().parents[2]))


def _load(rel: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / rel).read_text())


@dataclass(frozen=True)
class Settings:
    checkpoint_timeout: float = 2.0
    slow_load_budget: float = 6.0
    retry_budget: int = 3
    action_timeout: float = 3.0
    claim_timeout: float = 300.0
    hold_timeout: float = 900.0
    checkpoint_retries: int = 2
    approval_timeout: float = 300.0
    discovery_max_steps: int = 30
    discovery_timeout: float = 600.0
    discovery_stuck_after: int = 3

    @classmethod
    def load(cls, **overrides: Any) -> "Settings":
        return replace(cls(**_load("config/settings.yaml")), **overrides)


@dataclass(frozen=True)
class PolicyConfig:
    allowed_hosts: list[str]
    allowed_routes: list[str]
    allowed_actions: list[str]
    write_actions: list[str]
    irreversible_controls: list[dict[str, str]]
    irreversible_keywords: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "PolicyConfig":
        d = _load("config/policy.yaml")
        rr = d["risk_rules"]
        return cls(d["allowed_hosts"], d["allowed_routes"], d["allowed_actions"],
                   rr["write_actions"], rr["irreversible_controls"], rr.get("irreversible_keywords", []))


@dataclass(frozen=True)
class DetectorPack:
    fingerprint: dict[str, str]
    dialog_selector: str
    business_outcomes: list[dict[str, str]]
    failures: list[dict[str, str]]
    known_dialogs: list[dict[str, Any]]
    app_errors: list[dict[str, str]]
    blockers: list[dict[str, str]] = field(default_factory=list)
    native_dialogs: list[dict[str, Any]] = field(default_factory=list)  # browser alert/confirm: {text, accept}

    @classmethod
    def load(cls, product: str = "coreone") -> "DetectorPack":
        d = _load(f"config/detectors/{product}.yaml")
        return cls(d["fingerprint"], d["dialog_selector"], d["business_outcomes"], d["failures"],
                   d["recoverable"]["known_dialogs"], d["recoverable"]["app_errors"], d.get("blockers", []),
                   d["recoverable"].get("native_dialogs", []))


def load_tenants() -> dict[str, TenantConfig]:
    d = _load("config/tenants.yaml")["tenants"]
    return {k: TenantConfig(tenant=k, **v) for k, v in d.items()}
