"""A scripted stand-in for the Anthropic client, so the discovery loop is testable offline.

Each intent looks at the latest observation text and returns one tool call. Refs are
found by matching element lines, exactly as a model would read them.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Block:
    type: str
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    text: str = ""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Response:
    content: list[Block]
    stop_reason: str = "tool_use"
    id: str = "msg_scripted"
    usage: Usage = field(default_factory=Usage)


Intent = Callable[[str], tuple[str, dict]]


def ref(obs: str, *needles: str) -> str:
    for line in obs.splitlines():
        if line.startswith("- ") and all(n in line for n in needles):
            return line.split()[1]
    raise AssertionError(f"no element with {needles} in observation:\n{obs}")


def latest_observation(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] != "user":
            continue
        content = m["content"]
        if isinstance(content, str):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and "Elements:" in block["text"]:
                return block["text"]
    raise AssertionError("no observation found")


class ScriptedLLM:
    def __init__(self, intents: list[Intent]):
        self.intents = list(intents)
        self.calls: list[dict[str, Any]] = []
        self._ids = itertools.count(1)
        self.beta = self
        self.messages = self

    def create(self, **kwargs: Any) -> Response:
        self.calls.append(kwargs)
        if not self.intents:
            return Response([Block(type="text", text="I am finished.")], stop_reason="end_turn")
        name, args = self.intents.pop(0)(latest_observation(kwargs["messages"]))
        return Response([Block(type="tool_use", id=f"toolu_{next(self._ids)}", name=name, input=args)])


def tool(name: str, needles: tuple[str, ...] = (), **args: str) -> Intent:
    def intent(obs: str) -> tuple[str, dict]:
        a = dict(args)
        if needles:
            a["ref"] = ref(obs, *needles)
        a.setdefault("reason", f"{name} step")
        if name == "done":
            a = {"summary": "all outputs extracted"}
        if name == "request_human":
            a = {"reason": args.get("reason", "blocked")}
        return name, a
    return intent


SAVINGS_SCRIPT = [
    tool("fill", ('label="Member ID:"',), input_name="member_id"),
    tool("click", ('button "Search"',)),
    tool("click", ('link "View"', "| Savings |")),
    tool("extract", ('label="Available Balance:"',), output_name="balance"),
    tool("extract", ('label="Currency:"',), output_name="currency"),
    tool("done"),
]

SUBACCOUNT_SCRIPT = [
    tool("fill", ('label="Member ID:"',), input_name="member_id"),
    tool("click", ('button "Search"',)),
    tool("click", ('button "Open Sub-Account"',)),
    tool("select", ('label="Account Type:"',), input_name="account_type"),
    tool("fill", ('label="Initial Deposit:"',), input_name="initial_deposit"),
    tool("click", ('button "Continue"',)),
    tool("click", ('button "Confirm"',)),
    tool("extract", ('label="Confirmation Number:"',), output_name="confirmation_number"),
    tool("done"),
]


def strip_refs(text: str) -> str:
    return re.sub(r"\b\w+:e\d+\b", "REF", text)
