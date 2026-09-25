"""Writes the hand-authored golden artifacts and overlays (validated through the contracts)."""
import json
from pathlib import Path

from cua.contracts import Capability, TenantOverlay
from cua.store import ArtifactStore

ROOT = Path(__file__).resolve().parent.parent
M = "main"


def cp(*texts):
    return {"frame": M, "text_present": list(texts)}


def tgt(desc, *cands):
    return {"frame": M, "description": desc, "candidates": list(cands)}


member_steps = [
    {"id": "s1", "action": "FILL", "description": "Enter member id", "value_from": "inputs.member_id",
     "risk": "REVERSIBLE_WRITE", "pre": cp("Member Search"),
     "target": tgt("Member ID field", {"kind": "LABEL_PROXIMITY", "label": "Member ID:", "element": "input"})},
    {"id": "s2", "action": "CLICK", "description": "Search for the member", "pre": cp("Member Search"),
     "post": cp("Member Summary"),
     "target": tgt("Search button", {"kind": "ROLE_NAME", "role": "button", "name": "Search"})},
]

balance = {
    "name": "get_savings_balance", "version": 1, "status": "APPROVED",
    "goal": "Look up a member's savings account balance and currency.",
    "vendor_product": "CoreOne Banking", "compatible_versions": ["4.2.1"], "start_route": "/",
    "inputs": [{"name": "member_id", "type": "string", "pattern": r"^M\d{4}$"}],
    "outputs": [{"name": "balance", "type": "decimal", "sensitive": True},
                {"name": "currency", "type": "string", "pattern": "^[A-Z]{3}$"}],
    "steps": member_steps + [
        {"id": "s3", "action": "CLICK", "description": "Open the savings account", "pre": cp("Member Summary"),
         "post": cp("Account Detail"),
         "target": tgt("View link on the Savings row",
                       {"kind": "TABLE_ANCHOR", "anchor_text": "Savings", "column": 4, "element": "a"})},
        {"id": "s4", "action": "EXTRACT", "description": "Read available balance", "output": "balance",
         "pre": cp("Account Detail"),
         "target": tgt("Available balance", {"kind": "LABEL_PROXIMITY", "label": "Available Balance:", "element": "td"})},
        {"id": "s5", "action": "EXTRACT", "description": "Read currency", "output": "currency",
         "pre": cp("Account Detail"),
         "target": tgt("Currency", {"kind": "LABEL_PROXIMITY", "label": "Currency:", "element": "td"})},
    ],
    "success": cp("Account Detail", "Available Balance:"),
    "provenance": {"created_by": "hand-authored", "created_at": "2026-09-24T00:00:00+00:00",
                   "notes": "Golden reference for replay tests; discovery output is compared against it."},
}

subaccount = {
    "name": "open_sub_account", "version": 1, "status": "APPROVED",
    "goal": "Open a new sub-account for a member with an initial deposit and return the confirmation number.",
    "vendor_product": "CoreOne Banking", "compatible_versions": ["4.2.1"], "start_route": "/",
    "inputs": [{"name": "member_id", "type": "string", "pattern": r"^M\d{4}$"},
               {"name": "account_type", "type": "enum", "enum": ["SAVINGS", "MONEY_MARKET", "CHRISTMAS_CLUB"]},
               {"name": "initial_deposit", "type": "decimal", "minimum": "0.01"}],
    "outputs": [{"name": "confirmation_number", "type": "string", "pattern": r"^CNF-\d{8}$"}],
    "steps": member_steps + [
        {"id": "s3", "action": "CLICK", "description": "Start opening a sub-account", "pre": cp("Member Summary"),
         "post": cp("Open Sub-Account", "Initial Deposit:"),
         "target": tgt("Open Sub-Account button", {"kind": "ROLE_NAME", "role": "button", "name": "Open Sub-Account"})},
        {"id": "s4", "action": "SELECT", "description": "Choose account type", "value_from": "inputs.account_type",
         "risk": "REVERSIBLE_WRITE", "pre": cp("Initial Deposit:"),
         "target": tgt("Account type list", {"kind": "LABEL_PROXIMITY", "label": "Account Type:", "element": "select"})},
        {"id": "s5", "action": "FILL", "description": "Enter initial deposit", "value_from": "inputs.initial_deposit",
         "risk": "REVERSIBLE_WRITE", "pre": cp("Initial Deposit:"),
         "target": tgt("Initial deposit field", {"kind": "LABEL_PROXIMITY", "label": "Initial Deposit:", "element": "input"})},
        {"id": "s6", "action": "CLICK", "description": "Continue to review", "pre": cp("Initial Deposit:"),
         "post": cp("Review Sub-Account"),
         "target": tgt("Continue button", {"kind": "ROLE_NAME", "role": "button", "name": "Continue"})},
        {"id": "s7", "action": "CLICK", "description": "Confirm and open the account", "risk": "IRREVERSIBLE",
         "pre": cp("Review Sub-Account"), "post": cp("Sub-Account Opened"),
         "target": tgt("Confirm button", {"kind": "ROLE_NAME", "role": "button", "name": "Confirm"})},
        {"id": "s8", "action": "EXTRACT", "description": "Read confirmation number", "output": "confirmation_number",
         "pre": cp("Sub-Account Opened"),
         "target": tgt("Confirmation number", {"kind": "LABEL_PROXIMITY", "label": "Confirmation Number:", "element": "td"})},
    ],
    "success": cp("Sub-Account Opened", "Confirmation Number:"),
    "provenance": {"created_by": "hand-authored", "created_at": "2026-09-24T00:00:00+00:00",
                   "notes": "Golden reference for replay tests."},
}

overlay_b = {
    "tenant": "tenant_b", "base_capability": "get_savings_balance",
    "label_map": {"Member ID:": "Member #:", "Search": "Find"},
    "locator_overrides": {"s3": [{"kind": "TABLE_ANCHOR", "anchor_text": "Share Savings", "column": 4, "element": "a"}]},
}

for body in (balance, subaccount):
    cap = Capability.model_validate(body)
    p = ROOT / "artifacts" / cap.name / "v1.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cap.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
    ArtifactStore(ROOT / "artifacts")._record_approval(cap)  # the goldens are reviewed as written: record the approval
    print("wrote", p.relative_to(ROOT))
TenantOverlay.model_validate(overlay_b)
(ROOT / "overlays" / "tenant_b.json").write_text(json.dumps(overlay_b, indent=2) + "\n")
print("wrote overlays/tenant_b.json")
