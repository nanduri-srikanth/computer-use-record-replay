"""DiscoveryAgent (D2; backbone item 11): the only component that talks to the LLM.

observe -> policy check -> LLM proposes -> action gateway -> act -> verify effect
-> record step -> goal met? The model proposes; deterministic code decides.

The model can target an element two ways: by an observation ref (from the element list)
or by screenshot coordinates (`click_point`). The coordinate path needs no DOM refs, which
is the path a surface without a clean DOM (legacy UI, desktop) would use.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import anthropic
import yaml
from pydantic import BaseModel, ConfigDict

from .. import metrics
from ..config import DetectorPack, Settings
from ..contracts import ActionType, Capability, FieldSpec, RiskTier, validate_fields
from ..evidence import EvidenceSink
from ..policy import PolicyEngine, PolicyViolation
from ..redactor import Redactor
from ..replay.detectors import Detectors
from ..session import OperatorConsole, SessionController
from ..store import ArtifactStore
from ..surface import ActionFailed, ElementInfo, Match, Observation, Surface
from .recorder import Recorder

DEFAULT_MODEL = "claude-opus-5"

SYSTEM = """You operate a legacy bank back-office web application in order to discover a repeatable procedure for a goal. \
A deterministic recorder turns each successful action into a step of a reusable automation, so prefer the shortest, \
most direct path through the UI.

Each turn you receive the current page: frames, headings, a list of elements (refs), redacted page text, and a masked \
screenshot. Act with exactly one tool call per turn. Target an element by its ref, or, when the element is not in the \
list, by its position in the screenshot with click_point (pixel coordinates of the full screenshot).

Rules:
- To type or choose a value, pass the input name (for example member_id); the system substitutes the real value. \
Never put literal values in tool calls.
- Read each required output with `extract`, pointing at the element that shows the value.
- Call `done` once every required output has been extracted.
- Personal data in page text is masked (for example M**01 or ****2346); that is expected.
- If you are blocked (a dialog you do not understand, repeated errors, no way forward), call `request_human`.
- You never have credentials. If the app asks you to sign in, call `request_human`; do not type into login fields.
- Never acknowledge or attest to a security, authorization, or compliance prompt yourself (for example "confirm you \
are authorised"): call `request_human`. Routine notices such as scheduled maintenance may be dismissed.
- If the page is still loading or content has not appeared yet, use `wait` rather than acting on unrelated elements.
- Irreversible actions (such as a final Confirm) pause for operator approval automatically; just propose them.
- Stay inside the application. There is no URL navigation tool."""

# One table drives both the tool schemas and the gateway: (tool, action, description, extra params)
_ACTION_TOOLS: list[tuple[str, ActionType, str, tuple[str, ...]]] = [
    ("click", ActionType.CLICK, "Click a link or button by ref.", ()),
    ("fill", ActionType.FILL, "Type an input value into a text field by ref.", ("input_name",)),
    ("select", ActionType.SELECT, "Choose an input value in a dropdown by ref.", ("input_name",)),
    ("extract", ActionType.EXTRACT, "Read a required output from the element (by ref) that displays it.",
     ("output_name",)),
]
_TOOL_ACTION = {name: action for name, action, _, _ in _ACTION_TOOLS} | {"click_point": ActionType.CLICK}


class DiscoverySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    goal: str
    vendor_product: str = "CoreOne Banking"
    start_route: str = "/"
    inputs: list[FieldSpec]
    outputs: list[FieldSpec]
    example_inputs: dict[str, str]

    @classmethod
    def load(cls, path: Path) -> "DiscoverySpec":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


@dataclass
class DiscoveryResult:
    status: str  # DRAFT_SAVED or STOPPED
    reason: str
    run_id: str
    turns: int
    capability: Capability | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


class _FrameState(NamedTuple):
    name: str | None
    headings: tuple[str, ...]
    dialog_open: bool
    route: str = ""


class _Stopped(Exception):
    pass


@dataclass
class _Run:
    """Per-discovery state."""

    run_id: str
    spec: DiscoverySpec
    ev: EvidenceSink
    session: SessionController
    recorder: Recorder
    obs: Observation | None = None
    extracted: dict[str, Any] = field(default_factory=dict)
    committed: bool = False  # an irreversible action has been performed in this run


class DiscoveryAgent:
    def __init__(self, surface: Surface, *, store: ArtifactStore, runs_dir: Path, redactor: Redactor,
                 policy: PolicyEngine | None = None, console: OperatorConsole | None = None,
                 settings: Settings | None = None, detectors: DetectorPack | None = None,
                 client: Any = None, model: str = DEFAULT_MODEL, use_fallbacks: bool = True):
        self.surface = surface
        self.store = store
        self.runs_dir = Path(runs_dir)
        self.redactor = redactor
        self.policy = policy or PolicyEngine()
        self.console = console
        self.settings = settings or Settings.load()
        self.det = Detectors(detectors or DetectorPack.load(), surface)
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.use_fallbacks = use_fallbacks

    # ================================================================ tools

    def _tools(self, spec: DiscoverySpec) -> list[dict[str, Any]]:
        def tool(name: str, desc: str, props: dict[str, Any]) -> dict[str, Any]:
            return {"name": name, "description": desc, "strict": True,
                    "input_schema": {"type": "object", "properties": props, "required": list(props),
                                     "additionalProperties": False}}
        why = {"type": "string", "description": "One short sentence: why this action moves toward the goal"}
        params = {
            "ref": {"type": "string", "description": "Element ref from the latest observation, e.g. main:e3"},
            "input_name": {"type": "string", "enum": [f.name for f in spec.inputs]},
            "output_name": {"type": "string", "enum": [f.name for f in spec.outputs]},
        }
        empty = {p for p in ("input_name", "output_name") if not params[p]["enum"]}  # an empty enum is invalid
        tools = [tool(name, desc, {"ref": params["ref"], **{p: params[p] for p in extra}, "reason": why})
                 for name, _, desc, extra in _ACTION_TOOLS if not empty & set(extra)]
        return tools + [
            tool("click_point", "Click whatever is at a screenshot position (pixels). Use when no ref fits.",
                 {"x": {"type": "number"}, "y": {"type": "number"}, "reason": why}),
            tool("wait", "Wait for the page to finish loading, then observe again. Not recorded as a step.",
                 {"seconds": {"type": "integer", "enum": [1, 2, 3, 5]}, "reason": why}),
            tool("done", "Declare the goal met after all outputs are extracted.", {"summary": why}),
            tool("request_human", "Ask a human operator to take over the live session.", {"reason": why}),
        ]

    # ================================================================ observation

    def _observe(self) -> tuple[Observation, list[dict[str, Any]]]:
        obs = self.surface.observe(with_screenshot=True, dialog_selector=self.det.pack.dialog_selector)
        r = self.redactor.text
        lines = ["Frames:"]
        for f in obs.frames:
            lines.append(f"- frame {f.name or 'top'}: route {f.route}; headings: {', '.join(map(r, f.headings)) or '(none)'}"
                         f"; dialog open: {'yes' if f.dialog_open else 'no'}")
        lines.append("Elements:")
        for e in obs.elements:
            desc = f"- {e.ref} {e.role}"
            if e.name:
                desc += f' "{r(e.name)}"'
            if e.label:
                desc += f' label="{r(e.label)}"'
            if e.text:
                desc += f' value="{self.redactor.labelled(e.label, e.text)}"'
            if e.row_cells and e.role != "cell":
                desc += " row=[" + " | ".join(r(c) for c in e.row_cells) + "]"
            lines.append(desc)
        for f in obs.frames:
            if f.name != "nav":
                lines.append(f"Page text ({f.name or 'top'}):\n{r(f.text)[:1500]}")
        content: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(lines)}]
        if obs.screenshot_png:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(obs.screenshot_png).decode()}})
        return obs, content

    @staticmethod
    def _frame_states(obs: Observation) -> list[_FrameState]:
        return [_FrameState(f.name, tuple(f.headings), f.dialog_open, f.route) for f in obs.frames]

    # ================================================================ main loop

    def discover(self, spec: DiscoverySpec, tenant: str) -> DiscoveryResult:
        run_id = f"disc-{datetime.now():%Y-%m-%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        ev = EvidenceSink(self.runs_dir, run_id, self.redactor)
        run = _Run(run_id=run_id, spec=spec, ev=ev,
                   session=SessionController(self.surface, self.console, self.policy, ev, self.redactor,
                                             self.settings, run_id),
                   recorder=Recorder(self.surface, self.redactor))
        turns = [0]
        try:
            result = self._discover(run, tenant, turns)
        except _Stopped as s:
            result = self._stop(run, str(s), turns[0])
        except Exception as e:  # noqa: BLE001 - every discovery ends with a logged, evidenced outcome
            result = self._stop(run, f"unexpected error: {type(e).__name__}: {e}", turns[0])
        finally:
            self.surface.set_act_guard(lambda: None)
        try:  # telemetry never breaks a run
            metrics.append(self.runs_dir / "metrics.jsonl",
                           metrics.derive_discovery(result, run.ev.events, spec.name, tenant))
        except Exception as e:  # noqa: BLE001
            run.ev.log("metrics_error", detail=f"{type(e).__name__}: {e}")
        return result

    def _stop(self, run: _Run, reason: str, turns: int) -> DiscoveryResult:
        try:
            ref = run.ev.capture(self.surface.snapshot(), "discovery-stopped")
        except Exception:  # noqa: BLE001 - evidence is best effort
            ref = None
        run.ev.log("discovery_stopped", reason=reason, turns=turns, evidence=ref)
        return DiscoveryResult("STOPPED", self.redactor.text(reason), run.run_id, turns, events=run.ev.events)

    def _discover(self, run: _Run, tenant: str, turns: list[int]) -> DiscoveryResult:
        spec, ev = run.spec, run.ev
        try:
            validate_fields(spec.inputs, {k: str(v) for k, v in spec.example_inputs.items()})
        except ValueError as e:
            raise _Stopped(f"example inputs invalid: {e}") from None

        ev.log("discovery_started", capability=spec.name, tenant=tenant, model=self.model)
        self.surface.goto(spec.start_route)
        from ..replay.engine import ReplayEngine  # shared wait helper; replay itself never imports discovery
        fp = ReplayEngine._await_value(self.det.fingerprint,
                                       self.settings.checkpoint_timeout + self.settings.slow_load_budget)
        if not fp or fp["tenant"] != tenant:
            raise _Stopped(f"fingerprint mismatch: {fp}")
        app_version = fp["version"]

        run.obs, content = self._observe()
        ev.log("observation", turn=0, text=content[0]["text"][:6000])
        intro = (f"Goal: {spec.goal}\nInputs available (use by name): {[f.name for f in spec.inputs]}\n"
                 f"Required outputs: {[f.name + ' (' + f.type + ')' for f in spec.outputs]}\n\nCurrent page:\n")
        content[0]["text"] = intro + content[0]["text"]
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        tools = self._tools(spec)
        started, no_progress = time.time(), 0

        while True:
            if turns[0] >= self.settings.discovery_max_steps:
                raise _Stopped("max steps reached")
            if time.time() - started > self.settings.discovery_timeout:
                raise _Stopped("discovery timeout")
            try:
                for f in run.obs.frames:
                    self.policy.check_url(f.url)
            except PolicyViolation as e:
                ev.log("policy_blocked", stage="page", rule=e.kind, detail=str(e))
                raise _Stopped(f"page off allowlist: {e}") from None
            turns[0] += 1
            t_call = time.time()
            resp = self._call(messages, tools)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                ev.log("model_usage", turn=turns[0], model=getattr(resp, "model", self.model), requested=self.model,
                       latency_s=round(time.time() - t_call, 2),
                       input_count=usage.input_tokens, output_count=usage.output_tokens,  # "*token*" keys get redacted
                       cache_read=getattr(usage, "cache_read_input_tokens", 0) or 0,
                       cache_write=getattr(usage, "cache_creation_input_tokens", 0) or 0)
            for fb in (b for b in resp.content if getattr(b, "type", "") == "fallback"):
                trig = getattr(fb, "trigger", None)
                ev.log("model_fallback", turn=turns[0], from_model=getattr(getattr(fb, "from_", None), "model", ""),
                       to_model=getattr(getattr(fb, "to", None), "model", ""),
                       category=getattr(trig, "category", None) or "")
            if resp.stop_reason == "refusal":
                raise _Stopped("model refused")
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not uses:
                messages.append({"role": "user", "content": "Use one of the tools to continue."})
                no_progress += 1
                continue
            tu = uses[0]
            t_tool = time.time()
            text, progress, done = self._gateway(run, tu.name, dict(tu.input))
            ev.log("tool", turn=turns[0], tool=tu.name, input=dict(tu.input), result=text, progress=progress,
                   seconds=round(time.time() - t_tool, 2))
            if done:
                success = run.recorder.checkpoint("main", self._main_headings(), with_label=True)
                cap = run.recorder.build(spec=spec, app_version=app_version, run_id=run.run_id, model=self.model,
                                         success=success)
                saved = self.store.save_draft(cap)
                ev.log("draft_saved", capability=saved.name, version=saved.version, steps=len(saved.steps))
                return DiscoveryResult("DRAFT_SAVED", "goal met", run.run_id, turns[0], saved, ev.events)
            no_progress = 0 if progress else no_progress + 1
            note = ""
            if no_progress >= self.settings.discovery_stuck_after or tu.name == "request_human":
                note = self._escalate(run, tu, turns[0], no_progress)
                no_progress = 0
            run.obs, obs_content = self._observe()
            ev.log("observation", turn=turns[0], text=obs_content[0]["text"][:6000])
            others = [{"type": "tool_result", "tool_use_id": u.id, "content": "Ignored: one tool call per turn.",
                       "is_error": True} for u in uses[1:]]
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tu.id, "content": text, "is_error": not progress}, *others,
                {"type": "text", "text": note + "Current page:\n" + obs_content[0]["text"]}, *obs_content[1:]]})

    def _escalate(self, run: _Run, tu: Any, turn: int, no_progress: int) -> str:
        reason = tu.input.get("reason") if tu.name == "request_human" else f"no progress for {no_progress} turns"
        if not run.session.can_escalate:
            raise _Stopped(f"stuck ({reason}) and no operator available")
        before = self._frame_states(self.surface.observe(dialog_selector=self.det.pack.dialog_selector))
        res = run.session.handoff(run.spec.name, f"turn-{turn}", f"STUCK: {reason}", lambda: "RETRY")
        if res.record.outcome != "RESUMED":
            raise _Stopped(f"handoff {res.record.outcome.lower()}")
        if res.record.human_events == 0:
            return "A human operator looked and handed control back without changing anything. "
        after = self._frame_states(self.surface.observe(dialog_selector=self.det.pack.dialog_selector))
        if self._only_closed_a_dialog(before, after):
            # Closing a dialog is never a recorded step (replay handles dialogs itself), so nothing is missing
            # from the recording: continue from here instead of restarting into the same dialog again.
            run.ev.log("human_closed_dialog", turn=turn)
            return ("A human operator handled the dialog on this page and handed control back. Continue from the "
                    "current page; do not request help for the same dialog again unless it reappears. ")
        # The human changed the page. Their actions are captured as event types only (no values), which is not
        # enough to replay them, so an artifact built from here would silently miss steps. Start over from the
        # entry point instead; the model now knows what unblocked the flow and its own steps get recorded.
        if run.committed:
            raise _Stopped("human took over after an irreversible action; artifact not saved (would be incomplete)")
        human = [e for e in run.session.human_events][-res.record.human_events:]
        summary = "; ".join(f"{e['type']} {e['role']} '{e['name'] or e['label']}'" for e in human)
        run.recorder.steps.clear()
        run.extracted.clear()
        self.surface.goto(run.spec.start_route)
        run.ev.log("discovery_restarted", reason="human changed the page during handoff", human_actions=summary)
        return (f"A human operator took over and did: {summary}. To keep the recording complete, the app was "
                "reset to its entry point and earlier steps were discarded. Redo the flow, repeating what the human "
                "did yourself where it is still needed. ")

    @staticmethod
    def _only_closed_a_dialog(before: list[_FrameState], after: list[_FrameState]) -> bool:
        pairs = {b.name: b for b in before}
        changed = [a for a in after if a.name in pairs and a != pairs[a.name]]
        return bool(changed) and all(
            pairs[a.name].dialog_open and not a.dialog_open and pairs[a.name].headings == a.headings
            and pairs[a.name].route == a.route for a in changed)

    def _main_headings(self) -> list[str]:
        return next((list(f.headings) for f in self.surface.observe(dialog_selector=self.det.pack.dialog_selector).frames if f.name == "main"), [])

    def _call(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        kwargs: dict[str, Any] = dict(model=self.model, max_tokens=16000, system=SYSTEM, tools=tools,
                                      messages=messages, thinking={"type": "adaptive"},
                                      tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                                      cache_control={"type": "ephemeral"})  # history is append-only: cache it
        if self.use_fallbacks:
            return self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                    **kwargs)
        return self.client.messages.create(**kwargs)

    # ================================================================ action gateway

    def _target(self, run: _Run, name: str, args: dict[str, Any]) -> tuple[ElementInfo, Match] | str:
        """Resolve the model's target to an element, or return an error message for the model."""
        if name == "click_point":
            found = self.surface.element_at(float(args["x"]), float(args["y"]))
            return found or f"Nothing actionable at ({args['x']}, {args['y']})."
        el = next((e for e in run.obs.elements if e.ref == args["ref"]), None)
        if el is None:
            return f"Unknown ref {args['ref']}; use a ref from the latest observation."
        match = self.surface.resolve_ref(el.ref)
        return (el, match) if match.count == 1 else "That element is no longer on the page."

    def _gateway(self, run: _Run, name: str, args: dict[str, Any]) -> tuple[str, bool, bool]:
        """Returns (result text for the model, made progress, goal met)."""
        spec = run.spec
        if name == "request_human":
            return "Requesting a human operator.", False, False
        if name == "wait":  # bounded by the enum and by stuck detection; replay handles slow loads itself
            time.sleep(int(args.get("seconds", 1)))
            return f"Waited {int(args.get('seconds', 1))}s.", False, False
        if name == "done":
            missing = [f.name for f in spec.outputs if f.name not in run.extracted]
            if missing:
                return f"Not done: outputs not yet extracted: {missing}", False, False
            return "Goal met.", True, True
        action = _TOOL_ACTION[name]
        target = self._target(run, name, args)
        if isinstance(target, str):
            return target, False, False
        el, match = target
        try:
            self.policy.check_action(action)
            self.policy.check_url(self.surface.frame_url(el.frame))
            if action == ActionType.CLICK and match.target_url:  # where the click would lead, before clicking
                self.policy.check_url(match.target_url)
        except PolicyViolation as e:
            run.ev.log("policy_blocked", stage="gateway", rule=e.kind, tool=name, detail=str(e))
            return f"Blocked by policy: {e}", False, False
        risk = self.policy.classify(action, match.frame_route, match.text, match.submits_post)
        if risk == RiskTier.IRREVERSIBLE:
            self._approve(run, el)

        pre_view = next((f for f in run.obs.frames if f.name == el.frame), None)
        pre = run.recorder.checkpoint(el.frame, pre_view.headings if pre_view else [])
        cands = run.recorder.candidates_for(el)  # before acting, while the element is still on the page
        if action == ActionType.EXTRACT:
            return self._extract(run, el, match, args["output_name"], risk, pre, cands)

        value_from = value = None
        if action in (ActionType.FILL, ActionType.SELECT):
            value_from = f"inputs.{args['input_name']}"
            value = str(spec.example_inputs[args["input_name"]])
        before = self._frame_states(run.obs)
        try:
            self.surface.act(action, match, value)
        except ActionFailed as e:
            return f"The action failed: {e}", False, False
        if risk == RiskTier.IRREVERSIBLE:
            run.committed = True
        if action in (ActionType.FILL, ActionType.SELECT):
            ok, post = self.surface.read_value(match) == value, None
        else:
            ok, post = self._await_change(run, el.frame, before)
        if not ok:
            return "The action had no visible effect.", False, False
        if self._dismissed_dialog(el.frame, before):
            run.ev.log("dialog_dismissed_not_recorded", target=run.recorder.describe(el))
            return "Dialog dismissed. Not recorded as a step: replay handles known dialogs itself.", True, False
        run.recorder.record(action=action, el=el, description=args.get("reason", name), risk=risk, pre=pre,
                            post=post, value_from=value_from, candidates=cands)
        return f"{name} succeeded.", True, False

    def _approve(self, run: _Run, el: ElementInfo) -> None:
        if not run.session.can_escalate:
            raise _Stopped("irreversible action proposed and no operator available for approval")
        step_id = f"discovery:{el.ref}"
        d = run.session.request_approval(run.spec.name, step_id, {"capability": run.spec.name,
                                                                  "action": run.recorder.describe(el),
                                                                  **run.spec.example_inputs})
        if d.outcome != "APPROVED" or d.token is None:
            raise _Stopped(f"irreversible action not approved ({d.outcome.lower()})")
        self.policy.consume_token(d.token, run.run_id, step_id)

    def _extract(self, run: _Run, el: ElementInfo, match: Match, output: str, risk: RiskTier, pre: Any,
                 cands: list) -> tuple[str, bool, bool]:
        raw = self.surface.act(ActionType.EXTRACT, match)
        field_spec = next(f for f in run.spec.outputs if f.name == output)
        try:
            field_spec.coerce(raw)
        except ValueError as e:
            return f"Extracted value does not fit {output} ({field_spec.type}): {e}", False, False
        run.extracted[output] = raw
        run.recorder.record(action=ActionType.EXTRACT, el=el, description=f"Read {output}", risk=risk, pre=pre,
                            post=None, output=output, candidates=cands)
        shown = "[sensitive]" if field_spec.sensitive else self.redactor.text(str(raw))
        return f"Extracted {output} = {shown}", True, False

    def _dismissed_dialog(self, frame: str | None, before: list[_FrameState]) -> bool:
        was = next((b for b in before if b.name == frame), None)
        now = next((f for f in self._frame_states(self.surface.observe(dialog_selector=self.det.pack.dialog_selector)) if f.name == frame), None)
        return bool(was and now and was.dialog_open and not now.dialog_open and was.headings == now.headings)

    def _await_change(self, run: _Run, frame: str | None, before: list[_FrameState]) -> tuple[bool, Any]:
        end = time.time() + self.settings.checkpoint_timeout + self.settings.slow_load_budget
        while time.time() < end:
            if self._frame_states(self.surface.observe(dialog_selector=self.det.pack.dialog_selector)) != before:
                time.sleep(0.2)  # let the new page settle
                fv = next((f for f in self.surface.observe(dialog_selector=self.det.pack.dialog_selector).frames if f.name == frame), None)
                return True, run.recorder.checkpoint(frame, fv.headings if fv else [])
            time.sleep(0.15)
        return False, None
