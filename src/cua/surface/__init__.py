"""The Surface seam (backbone item 2).

DiscoveryAgent, ReplayEngine, and SessionController depend only on this
interface. PlaywrightSurface is the only module that imports Playwright; a
desktop (UIA/AX) Surface would implement the same protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..contracts import ActionType, LocatorCandidate


@dataclass
class ElementInfo:
    """One actionable or readable element as seen by observe()."""

    ref: str  # observation-scoped handle, only valid until the next observe()
    frame: str | None
    tag: str
    role: str
    name: str
    label: str
    column: int | None
    row_cells: list[str]
    x: float
    y: float
    text: str = ""  # readable value (for label: value cells)


@dataclass
class FrameView:
    name: str | None
    url: str
    route: str
    headings: list[str]
    text: str
    dialog_open: bool


@dataclass
class Observation:
    frames: list[FrameView]
    elements: list[ElementInfo]
    screenshot_png: bytes | None = None


@dataclass
class Snapshot:
    png: bytes
    text: str


@dataclass
class Match:
    count: int
    handle: Any = None  # opaque, Surface-specific
    text: str = ""  # visible text or accessible name of the single match
    frame_route: str = ""
    submits_post: bool = False  # clicking it submits a POST form: a state-changing request
    target_url: str = ""  # where a click would navigate (link href or form action), checked before clicking


@dataclass
class NativeDialog:
    """A browser-native alert/confirm/prompt. The Surface must answer it synchronously."""

    kind: str
    message: str
    accepted: bool


@dataclass
class HumanEvent:
    type: str  # click, change, submit
    frame: str | None
    tag: str
    role: str
    name: str
    label: str


class ControlTokenHeld(Exception):
    """Raised when automation tries to act while it does not hold the control token."""


class ActionFailed(Exception):
    """The control was resolved but the action did not take (obscured, detached, not editable)."""


class Surface(Protocol):
    # observe
    def observe(self, with_screenshot: bool = False, dialog_selector: str = ".modal") -> Observation: ...
    def frame_text(self, frame: str | None) -> str: ...
    def frame_url(self, frame: str | None) -> str: ...
    def dialog_visible(self, frame: str | None, selector: str) -> str | None: ...

    # resolve
    def resolve(self, frame: str | None, candidate: LocatorCandidate) -> Match: ...
    def resolve_ref(self, ref: str) -> Match: ...
    def element_at(self, x: float, y: float) -> tuple[ElementInfo, Match] | None: ...

    # act (guarded by the control token)
    def goto(self, route: str) -> None: ...
    def act(self, action: ActionType, match: Match, value: str | None = None) -> str | None: ...
    def read_value(self, match: Match) -> str: ...
    def click_role(self, frame: str | None, role: str, name: str) -> None: ...
    def reload(self, frame: str | None) -> None: ...

    # snapshot
    def snapshot(self) -> Snapshot: ...

    # control token + human event capture
    def set_act_guard(self, guard: Callable[[], None]) -> None: ...
    def set_event_sink(self, sink: Callable[[HumanEvent], None] | None) -> None: ...
    def human_page(self) -> Any: ...
    def pump(self, ms: int = 200) -> None: ...

    # native dialogs
    def set_dialog_policy(self, accept: Callable[[str, str], bool]) -> None: ...
    def pop_native_dialogs(self) -> list[NativeDialog]: ...
