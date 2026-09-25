"""Independent review: offline probes (no browser). Artifact integrity, allowlist edge cases, redaction, tokens."""

from __future__ import annotations

import dataclasses
import json

import pytest
from pydantic import ValidationError

from cua.evidence import EvidenceSink
from cua.policy import PolicyEngine, PolicyViolation
from cua.redactor import Redactor
from cua.store import ArtifactStore

from ..conftest import ROOT

GOLDEN = json.loads((ROOT / "artifacts" / "get_savings_balance" / "v1.json").read_text())


# ---------------------------------------------------------------- malformed artifacts


def _mutate(kind: str) -> str:
    body = json.loads(json.dumps(GOLDEN))
    if kind == "truncated_json":
        return json.dumps(body)[:200]
    if kind == "missing_success":
        body.pop("success")
    elif kind == "unknown_step_field":
        body["steps"][0]["javascript"] = "document.forms[0].submit()"
    elif kind == "literal_value_instead_of_input_ref":
        body["steps"][0]["value_from"] = "M1001"
    elif kind == "output_never_extracted":
        body["steps"] = [s for s in body["steps"] if s.get("output") != "currency"]
    elif kind == "ladder_out_of_order":
        body["steps"][2]["target"]["candidates"].insert(0, {"kind": "COORDINATES", "x": 1, "y": 1})
    return json.dumps(body)


@pytest.mark.parametrize("kind", ["truncated_json", "missing_success", "unknown_step_field",
                                  "literal_value_instead_of_input_ref", "output_never_extracted",
                                  "ladder_out_of_order"])
def test_malformed_artifact_on_disk_is_rejected_at_load(tmp_path, kind):
    store = ArtifactStore(tmp_path)
    p = tmp_path / "get_savings_balance" / "v1.json"
    p.parent.mkdir(parents=True)
    p.write_text(_mutate(kind))
    with pytest.raises((ValidationError, ValueError)):
        store.load("get_savings_balance", 1)


# ---------------------------------------------------------------- allowlist edge cases


@pytest.mark.xfail(strict=True, reason="FINDING: PolicyEngine.check_url fails open for URLs with no netloc "
                   "(policy.py:54-55), so file:// and javascript: URLs pass the allowlist")
@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:fetch('https://evil.example/x')"])
def test_non_http_schemes_are_not_on_the_allowlist(url):
    with pytest.raises(PolicyViolation):
        PolicyEngine().check_url(url)


@pytest.mark.xfail(strict=True, reason="FINDING: route_allowed is a raw string-prefix check (policy.py:48-50); "
                   "'/member/../admin/audit' passes as a /member/ route without normalisation")
def test_dot_segment_route_cannot_escape_an_allowed_prefix():
    assert not PolicyEngine().route_allowed("/member/../admin/audit")


@pytest.mark.parametrize("url", ["http://127.0.0.1.evil.example/member/search", "http://evil.example/member/search",
                                 "http://127.0.0.1:5055/admin/audit", "https://localhost/memberx"])
def test_host_and_route_lookalikes_are_refused(url):
    with pytest.raises(PolicyViolation):
        PolicyEngine().check_url(url)


# ---------------------------------------------------------------- approval tokens


def test_approval_token_forgery_and_cross_engine_reuse_rejected():
    pe = PolicyEngine()
    tok = pe.issue_token("run-1", "s7", "op-a")
    with pytest.raises(PolicyViolation, match="signature"):
        pe.consume_token(dataclasses.replace(tok, step_id="s8"), "run-1", "s8")  # re-bound without re-signing
    with pytest.raises(PolicyViolation, match="signature"):
        pe.consume_token(dataclasses.replace(tok, operator="op-b"), "run-1", "s7")
    other = PolicyEngine().issue_token("run-1", "s7", "op-a")  # minted by another engine (different key)
    with pytest.raises(PolicyViolation, match="signature"):
        pe.consume_token(other, "run-1", "s7")
    pe.consume_token(tok, "run-1", "s7")  # the genuine token still works once


# ---------------------------------------------------------------- redaction


def test_env_secrets_and_secret_keys_are_redacted_in_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("CORE_SERVICE_API_KEY", "sk-live-9f8e7d6c5b4a")
    sink = EvidenceSink(tmp_path, "r1", Redactor())
    sink.log("note", detail="upstream said key=sk-live-9f8e7d6c5b4a was rejected",
             headers={"Authorization": "Bearer abc", "x_session_token": "tok-123456"},
             nested=[{"password": "hunter2hunter2"}])
    text = (tmp_path / "r1" / "events.jsonl").read_text()
    for leaked in ("sk-live-9f8e7d6c5b4a", "tok-123456", "hunter2hunter2"):
        assert leaked not in text


def test_pii_shapes_on_a_legacy_page_are_redacted():
    page = ("Member Summary\nName:\tJane Doe\nSSN:\t123-45-6789\nDate of Birth:\t1984-03-12\n"
            "Email: jane.doe@bank.example\n10012346\tSavings\t$2,450.17")
    out = Redactor().text(page)
    for leaked in ("Jane Doe", "123-45-6789", "1984-03-12", "jane.doe@bank.example", "10012346", "2,450.17"):
        assert leaked not in out


@pytest.mark.xfail(strict=True, reason="FINDING (low): no card-number (PAN) pattern; a space-grouped PAN passes "
                   "through Redactor.text unchanged (redactor.py:19-28)")
def test_space_grouped_card_number_is_redacted():
    assert "4111 1111 1111 1111" not in Redactor().text("Card on file: 4111 1111 1111 1111")


@pytest.mark.xfail(strict=True, reason="FINDING (low): label-based redaction is case-sensitive (redactor.py:16); "
                   "an upper-case legacy label 'NAME:' leaks the member's name")
def test_upper_case_sensitive_label_is_redacted():
    assert "JANE DOE" not in Redactor().text("NAME:\tJANE DOE")
