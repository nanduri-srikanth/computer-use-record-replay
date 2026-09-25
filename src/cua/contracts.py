"""Typed contracts shared by every component (backbone item 1).

Artifacts reference abstract target descriptors only, never Playwright objects,
so any Surface implementation can replay them.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 2

Verdict = Literal["NEXT", "RETRY"]  # after a recovery or handoff: advance, or run the same step again
INPUT_REF = r"^inputs\.[a-z][a-z0-9_]*$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- enums


class RiskTier(str, Enum):
    READ = "READ"
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"
    IRREVERSIBLE = "IRREVERSIBLE"

    @property
    def rank(self) -> int:
        return list(RiskTier).index(self)


class ArtifactStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    DEPRECATED = "DEPRECATED"


class ActionType(str, Enum):
    CLICK = "CLICK"
    FILL = "FILL"
    SELECT = "SELECT"
    EXTRACT = "EXTRACT"
    NAVIGATE = "NAVIGATE"  # exists so the policy can refuse it; not in the default allowlist


class LocatorKind(str, Enum):
    ROLE_NAME = "ROLE_NAME"
    LABEL_PROXIMITY = "LABEL_PROXIMITY"
    TABLE_ANCHOR = "TABLE_ANCHOR"
    COORDINATES = "COORDINATES"


LADDER_ORDER = [LocatorKind.ROLE_NAME, LocatorKind.LABEL_PROXIMITY, LocatorKind.TABLE_ANCHOR, LocatorKind.COORDINATES]


class FailureReason(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ARTIFACT_INVALID = "ARTIFACT_INVALID"
    ARTIFACT_NOT_APPROVED = "ARTIFACT_NOT_APPROVED"
    PERMISSION_DENIED = "PERMISSION_DENIED"  # the application refused (operator not entitled)
    POLICY_VIOLATION = "POLICY_VIOLATION"  # our allowlist or approval rules refused
    DRIFT_DETECTED = "DRIFT_DETECTED"
    OVERLAY_REJECTED = "OVERLAY_REJECTED"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    ACTION_FAILED = "ACTION_FAILED"  # the control was found but the action did not take (obscured, truncated input)
    SUCCESS_CONDITION_UNMET = "SUCCESS_CONDITION_UNMET"
    APP_ERROR = "APP_ERROR"
    HANDOFF_FAILED = "HANDOFF_FAILED"
    UNRECOVERABLE_BLOCKER = "UNRECOVERABLE_BLOCKER"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    UNEXPECTED_ERROR = "UNEXPECTED_ERROR"  # anything unanticipated; still a structured FAILURE with evidence


class BusinessCode(str, Enum):
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    DECLINED_BY_OPERATOR = "DECLINED_BY_OPERATOR"


# ---------------------------------------------------------------- targets


class LocatorCandidate(Strict):
    kind: LocatorKind
    role: str | None = None  # ROLE_NAME
    name: str | None = None  # ROLE_NAME
    label: str | None = None  # LABEL_PROXIMITY: text of the neighbouring label cell
    anchor_text: str | None = None  # TABLE_ANCHOR: static text of a cell in the same row
    column: int | None = None  # TABLE_ANCHOR: 1-based cell index of the target
    element: str | None = None  # LABEL_PROXIMITY / TABLE_ANCHOR: tag of the target (input, select, a)
    x: float | None = None  # COORDINATES
    y: float | None = None

    @model_validator(mode="after")
    def _fields_for_kind(self) -> "LocatorCandidate":
        need = {
            LocatorKind.ROLE_NAME: ("role", "name"),
            LocatorKind.LABEL_PROXIMITY: ("label", "element"),
            LocatorKind.TABLE_ANCHOR: ("anchor_text", "column", "element"),
            LocatorKind.COORDINATES: ("x", "y"),
        }[self.kind]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.kind.value} candidate missing {missing}")
        return self


class TargetDescriptor(Strict):
    frame: str | None = None  # frame name; None = top document
    description: str
    candidates: list[LocatorCandidate] = Field(min_length=1)

    @model_validator(mode="after")
    def _ladder_order(self) -> "TargetDescriptor":
        ranks = [LADDER_ORDER.index(c.kind) for c in self.candidates]
        if ranks != sorted(ranks):
            raise ValueError("candidates must follow ladder order: role+name, label, table anchor, coordinates")
        return self


class Checkpoint(Strict):
    frame: str | None = None
    text_present: list[str] = Field(min_length=1)


# ---------------------------------------------------------------- IO specs


class FieldSpec(Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: Literal["string", "decimal", "enum"]
    pattern: str | None = None
    enum: list[str] | None = None
    minimum: Decimal | None = None
    sensitive: bool = False  # returned to the caller, but masked in anything persisted (result.json, evidence)

    @model_validator(mode="after")
    def _enum_has_values(self) -> "FieldSpec":
        if self.type == "enum" and not self.enum:
            raise ValueError(f"{self.name}: enum fields need a non-empty enum list")
        return self

    def coerce(self, raw: Any) -> Any:
        """Validate and normalise one value; raises ValueError on mismatch."""
        if raw is None:
            raise ValueError(f"{self.name} is required")
        if self.type == "decimal":
            try:
                v = Decimal(str(raw).replace("$", "").replace(",", "").strip())
            except InvalidOperation:
                raise ValueError(f"{self.name} is not a decimal") from None
            if self.minimum is not None and v < self.minimum:
                raise ValueError(f"{self.name} below minimum {self.minimum}")
            return v
        s = str(raw).strip()
        if self.type == "enum" and s not in (self.enum or []):
            raise ValueError(f"{self.name} must be one of {self.enum}")
        if self.pattern and not re.fullmatch(self.pattern, s):
            raise ValueError(f"{self.name} does not match {self.pattern}")
        return s


def validate_fields(specs: list[FieldSpec], values: dict[str, Any]) -> dict[str, Any]:
    unknown = set(values) - {s.name for s in specs}
    if unknown:
        raise ValueError(f"unknown fields {sorted(unknown)}")
    return {s.name: s.coerce(values.get(s.name)) for s in specs}


# ---------------------------------------------------------------- artifact


class Step(Strict):
    id: str
    action: ActionType
    description: str
    target: TargetDescriptor | None = None  # None only for NAVIGATE
    value_from: str | None = Field(default=None, pattern=INPUT_REF)  # FILL/SELECT only; never a literal value
    output: str | None = None  # EXTRACT: output field name
    url: str | None = None  # NAVIGATE only
    risk: RiskTier = RiskTier.READ
    pre: Checkpoint | None = None
    post: Checkpoint | None = None

    @property
    def input_name(self) -> str | None:
        return self.value_from.split(".", 1)[1] if self.value_from else None

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        writes = self.action in (ActionType.FILL, ActionType.SELECT)
        if writes and not self.value_from:
            raise ValueError(f"step {self.id}: {self.action.value} needs value_from='inputs.<name>'")
        if not writes and self.value_from:
            raise ValueError(f"step {self.id}: value_from is only valid on FILL or SELECT")
        if self.action == ActionType.EXTRACT and not self.output:
            raise ValueError(f"step {self.id}: EXTRACT needs output")
        if self.action != ActionType.NAVIGATE and self.target is None:
            raise ValueError(f"step {self.id}: target required")
        return self


class Provenance(Strict):
    created_by: Literal["discovery", "hand-authored", "human-revision"]
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    source_run_id: str | None = None
    parent_version: int | None = None
    model: str | None = None
    notes: str | None = None


class Capability(Strict):
    schema_version: int = SCHEMA_VERSION
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    status: ArtifactStatus = ArtifactStatus.DRAFT
    goal: str
    vendor_product: str
    compatible_versions: list[str] = Field(min_length=1)  # app versions this artifact was recorded against
    start_route: str = "/"
    inputs: list[FieldSpec]
    outputs: list[FieldSpec]
    steps: list[Step] = Field(min_length=1)
    success: Checkpoint  # capability-level success condition, verified after the last step
    provenance: Provenance

    @model_validator(mode="after")
    def _refs(self) -> "Capability":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate step ids")
        in_names = {f.name for f in self.inputs}
        out_names = {f.name for f in self.outputs}
        for s in self.steps:
            if s.input_name and s.input_name not in in_names:
                raise ValueError(f"step {s.id} references unknown input {s.value_from}")
            if s.output and s.output not in out_names:
                raise ValueError(f"step {s.id} extracts unknown output {s.output}")
        extracted = {s.output for s in self.steps if s.output}
        if extracted != out_names:
            raise ValueError(f"outputs {sorted(out_names - extracted)} never extracted")
        return self

    def content_hash(self) -> str:
        """Hash of everything except lifecycle status, so status changes keep identity."""
        body = self.model_dump(mode="json", exclude={"status"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


class TenantOverlay(Strict):
    """Per-tenant adjustments. Deliberately cannot express steps, risk tiers, or permissions."""

    tenant: str
    base_capability: str
    label_map: dict[str, str] = Field(default_factory=dict)
    locator_overrides: dict[str, list[LocatorCandidate]] = Field(default_factory=dict)  # step id -> candidates


class TenantConfig(Strict):
    tenant: str
    expected_version: str
    overlays: dict[str, str] = Field(default_factory=dict)  # capability -> overlay path


# ---------------------------------------------------------------- results


class RecoveryRecord(Strict):
    step: str
    kind: Literal["KNOWN_DIALOG", "NATIVE_DIALOG", "SLOW_LOAD", "TRANSIENT_APP_ERROR", "ACTION_RETRY"]
    detail: str


class _Result(Strict):
    # Recoverable conditions handled inside replay. Part of every result so the caller sees them,
    # but they never change the bucket.
    recoveries: list[RecoveryRecord] = Field(default_factory=list)


class HandoffRecord(Strict):
    step: str
    reason: str
    outcome: Literal["RESUMED", "FAILED", "CANCELLED"]
    operator: str | None = None
    transitions: list[str] = Field(default_factory=list)
    human_events: int = 0


class Success(_Result):
    bucket: Literal["SUCCESS"] = "SUCCESS"
    outputs: dict[str, str]


class BusinessOutcome(_Result):
    bucket: Literal["BUSINESS_OUTCOME"] = "BUSINESS_OUTCOME"
    code: BusinessCode
    step: str | None = None
    detail: str = ""


class Escalated(_Result):
    bucket: Literal["ESCALATED"] = "ESCALATED"
    reason: str
    handoffs: list[HandoffRecord]
    outputs: dict[str, str] | None = None  # present when the run completed after a resume
    business_code: BusinessCode | None = None


class Failure(_Result):
    bucket: Literal["FAILURE"] = "FAILURE"
    reason: FailureReason
    step: str | None
    expected: str
    observed: str
    evidence_ref: str | None = None
    handoffs: list[HandoffRecord] = Field(default_factory=list)  # e.g. a handoff nobody claimed


RunResult = Annotated[Union[Success, BusinessOutcome, Escalated, Failure], Field(discriminator="bucket")]


class RunReport(Strict):
    run_id: str
    capability: str
    version: int
    tenant: str
    inputs_redacted: dict[str, str]
    result: RunResult
    steps_executed: list[str] = Field(default_factory=list)
    ui_actions: int = 0
    started_at: str
    ended_at: str
