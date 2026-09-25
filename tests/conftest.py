"""Shared fixtures: mock bank server, browser surface, replay engine factory, fault profiles."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from cua.config import Settings
from cua.contracts import Capability
from cua.redactor import Redactor
from cua.replay.engine import ReplayEngine
from cua.store import ArtifactStore
from cua.surface.playwright_surface import PlaywrightSurface
from mockbank import data
from mockbank.app import MockBankServer

ROOT = Path(__file__).resolve().parent.parent

# Fault profiles F1 to F10 (docs/BUILD_MAP.md section 2).
FAULTS: dict[str, dict] = {
    "F1": {},
    "F2": {"interstitial": True},
    "F3": {"unknown_dialog": True},
    "F4": {"slow_ms": 1800},  # within slow_load_budget below
    "F5": {"slow_ms": 6000},  # beyond it
    "F6": {"transient_500": 1},
    "F7": {"persistent_500": True},
    # counted after the per-test reset: /, /nav, /member/search, /member/summary, then /member/account is 5th
    "F8": {"session_expire_at": 5},
}

FAST = dict(checkpoint_timeout=1.0, slow_load_budget=3.5, retry_budget=3, action_timeout=2.0,
            claim_timeout=1.0, hold_timeout=5.0, checkpoint_retries=1, approval_timeout=1.0)

CANARIES = [m.ssn for m in data.seed_members().values()] + \
           [m.dob for m in data.seed_members().values()] + \
           list(data.seed_members()) + [data.OPERATOR_PASSWORD, "2,450.17", "2450.17"] + \
           [m.name for m in data.seed_members().values()]


@pytest.fixture(scope="session")
def server():
    srv = MockBankServer().start()
    yield srv
    srv.stop()


@pytest.fixture
def reset(server):
    def _reset(tenant: str = "tenant_a", fault: str = "F1", **extra) -> None:
        body = json.dumps({"tenant": tenant, "faults": {**FAULTS[fault], **extra}}).encode()
        urllib.request.urlopen(urllib.request.Request(f"{server.base_url}/__admin/reset", body,
                                                      {"Content-Type": "application/json"}))
    _reset()
    return _reset


@pytest.fixture
def app_state(server):
    def _state() -> dict:
        return json.loads(urllib.request.urlopen(f"{server.base_url}/__admin/state").read())
    return _state


@pytest.fixture
def redactor():
    return Redactor(secrets=[data.OPERATOR_PASSWORD])


@pytest.fixture
def surface(server, redactor):
    s = PlaywrightSurface(server.base_url, redactor=redactor, action_timeout=FAST["action_timeout"]).start()
    s.authenticate(data.OPERATOR_USERNAME, data.OPERATOR_PASSWORD)
    yield s
    s.close()


@pytest.fixture(scope="session")
def runs_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("runs")


@pytest.fixture
def golden():
    store = ArtifactStore(ROOT / "artifacts")

    def _load(name: str) -> Capability:
        return store.load(name, 1)
    return _load


@pytest.fixture
def engine(surface, redactor, runs_dir):
    def _make(console=None, tenants=None, **settings) -> ReplayEngine:
        return ReplayEngine(surface, runs_dir=runs_dir, redactor=redactor, console=console, tenants=tenants,
                            settings=Settings.load(**{**FAST, **settings}), overlay_root=ROOT)
    return _make
