"""Seed data for the mock core banking app.

Each member exists to drive one path through replay (see docs/BUILD_MAP.md, section 2).
The SSN and DOB values are redaction canaries: they must never appear in logs,
artifacts, or evidence written by the automation system.
"""

from dataclasses import dataclass, field
from decimal import Decimal

OPERATOR_USERNAME = "teller1"
OPERATOR_PASSWORD = "Tr0ub4dor&3"

PRODUCT = "CoreOne Banking"


@dataclass
class Account:
    number: str
    type: str  # canonical type key: CHECKING, SAVINGS, MONEY_MARKET, CHRISTMAS_CLUB
    balance: Decimal
    currency: str = "USD"


@dataclass
class Member:
    member_id: str
    name: str
    ssn: str
    dob: str
    accounts: list[Account] = field(default_factory=list)
    restricted: bool = False  # operator is not entitled to view this member
    frozen: bool = False  # app rejects opening sub-accounts


def seed_members() -> dict[str, Member]:
    members = [
        Member("M1001", "Jane Doe", "123-45-6789", "1984-03-12", [
            Account("10012345", "CHECKING", Decimal("812.40")),
            Account("10012346", "SAVINGS", Decimal("2450.17")),
        ]),
        Member("M1002", "Omar Haddad", "234-56-7890", "1979-11-02", [
            Account("10022345", "SAVINGS", Decimal("150.00")),
            Account("10022346", "SAVINGS", Decimal("9800.25")),
        ]),
        Member("M1003", "Li Wei", "345-67-8901", "1991-07-21", [
            Account("10032345", "CHECKING", Decimal("45.10")),
            Account("10032346", "SAVINGS", Decimal("0.00")),
        ]),
        Member("M1004", "Grace Okafor", "456-78-9012", "1968-01-30", [
            Account("10042345", "SAVINGS", Decimal("1234567.89")),
        ]),
        Member("M1005", "Victor Stone", "567-89-0123", "1975-05-05", [
            Account("10052345", "SAVINGS", Decimal("300.00")),
        ], restricted=True),
        Member("M1006", "Ana Ruiz", "678-90-1234", "1988-09-09", [
            Account("10062345", "SAVINGS", Decimal("75.00")),
        ], frozen=True),
    ]
    return {m.member_id: m for m in members}


# Per-tenant presentation. The base artifact targets tenant_a labels; tenant_b
# relabels controls (needs an overlay); tenant_c runs a newer app version (drift).
TENANTS: dict[str, dict] = {
    "tenant_a": {
        "version": "4.2.1",
        "member_label": "Member ID:",
        "search_button": "Search",
        "type_labels": {"CHECKING": "Checking", "SAVINGS": "Savings",
                        "MONEY_MARKET": "Money Market", "CHRISTMAS_CLUB": "Christmas Club"},
    },
    "tenant_b": {
        "version": "4.2.1",
        "member_label": "Member #:",
        "search_button": "Find",
        "type_labels": {"CHECKING": "Share Draft", "SAVINGS": "Share Savings",
                        "MONEY_MARKET": "Money Market", "CHRISTMAS_CLUB": "Christmas Club"},
    },
    "tenant_c": {
        "version": "4.3.0",
        "member_label": "Member ID:",
        "search_button": "Search",
        "type_labels": {"CHECKING": "Checking", "SAVINGS": "Savings",
                        "MONEY_MARKET": "Money Market", "CHRISTMAS_CLUB": "Christmas Club"},
    },
}

MIN_OPENING_DEPOSIT = Decimal("25.00")
