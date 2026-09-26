"""Redaction of PII and secrets on every persistence path (backbone item 10).

Everything written to disk (artifacts, run events, evidence, intervention and
approval payloads) and everything sent to the discovery model passes through here.
"""

from __future__ import annotations

import os
import re
from typing import Any

# Values next to these labels are personal data even when no pattern can recognise them (names, addresses).
# Legacy table layouts put the label in the neighbouring cell, so this works without a clean DOM.
SENSITIVE_LABELS = ["Name:", "Member Name:", "Address:", "Phone:", "Email:", "Mother's Maiden Name:"]
# Case-insensitive: legacy screens print labels in capitals ("NAME:") as often as not.
_LABELLED = re.compile(r"(?mi)^((?:" + "|".join(re.escape(x[:-1]) for x in SENSITIVE_LABELS) + r")\s*:)[ \t]*\S[^\n]*$")
_SENSITIVE_LABELS_CF = {x.casefold() for x in SENSITIVE_LABELS}

# Order matters: longer, more specific patterns first.
_PATTERNS: list[tuple[str, re.Pattern[str], Any]] = [
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***-**-****"),
    # Card numbers (13-19 digits, often grouped by spaces or dashes); before account_no, which would miss the groups.
    ("card_no", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"), lambda m: "**** " + re.sub(r"\D", "", m.group(0))[-4:]),
    ("date", re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"), "****-**-**"),
    ("date_us", re.compile(r"\b\d{2}/\d{2}/\d{4}\b"), "**/**/****"),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "***@***"),
    ("account_no", re.compile(r"\b\d{8,12}\b"), lambda m: "****" + m.group(0)[-4:]),
    ("member_id", re.compile(r"\bM\d{4,}\b"), lambda m: "M**" + m.group(0)[-2:]),
    # Balances and amounts are regulated financial data: never persisted verbatim or shown to the model.
    ("money", re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?"), "$***"),
]

_SECRET_KEYS = re.compile(r"pass(word)?|secret|token|api[_-]?key|credential", re.I)


class Redactor:
    def __init__(self, secrets: list[str] | None = None):
        env_secrets = [v for k, v in os.environ.items() if _SECRET_KEYS.search(k) and len(v) >= 6]
        self._secrets = sorted({s for s in (secrets or []) + env_secrets if s}, key=len, reverse=True)

    def text(self, s: str) -> str:
        for secret in self._secrets:
            s = s.replace(secret, "[SECRET]")
        s = _LABELLED.sub(lambda m: f"{m.group(1)} [REDACTED]", s)
        for _, pat, repl in _PATTERNS:
            s = pat.sub(repl, s)
        return s

    def labelled(self, label: str, value: str) -> str:
        """A value shown next to a label: masked outright when the label marks personal data."""
        if label.strip().casefold() in _SENSITIVE_LABELS_CF:
            return "[REDACTED]"
        return self.text(value)

    def is_clean(self, s: str) -> bool:
        """True when the text carries nothing redactable (safe to store verbatim, e.g. as a checkpoint)."""
        return self.text(s) == s

    def value(self, v: Any, key: str = "") -> Any:
        if key and _SECRET_KEYS.search(key):
            return "[SECRET]"
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, dict):
            return {k: self.value(x, k) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [self.value(x) for x in v]
        return v

    @staticmethod
    def mask_patterns() -> list[re.Pattern[str]]:
        """Patterns whose on-screen matches are masked in screenshots."""
        return [p for name, p, _ in _PATTERNS if name in ("ssn", "card_no", "date", "date_us", "email", "account_no", "money",
                                                           "member_id")]
