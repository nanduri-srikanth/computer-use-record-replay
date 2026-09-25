"""Locator ladder (backbone item 3). Surface-agnostic.

Candidates are tried in ladder order. 0 matches: next candidate.
More than 1 match: stop immediately as AMBIGUOUS_TARGET, never guess.
COORDINATES is a locator only for targets that have no semantic candidate. When semantic
candidates exist and all of them miss, the page has drifted and a blind click at a stored
point would be a guess (it cannot see ambiguity or a moved column), so it is not tried.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import LocatorKind, TargetDescriptor
from .surface import Match, Surface


class AmbiguousTarget(Exception):
    def __init__(self, candidate_index: int, count: int, detail: str):
        super().__init__(detail)
        self.candidate_index, self.count, self.detail = candidate_index, count, detail


class TargetNotFound(Exception):
    pass


@dataclass
class Resolution:
    match: Match
    candidate_index: int
    tried: list[str]


def resolve_ladder(surface: Surface, target: TargetDescriptor) -> Resolution:
    tried: list[str] = []
    semantic = any(c.kind != LocatorKind.COORDINATES for c in target.candidates)
    for i, cand in enumerate(target.candidates):
        if semantic and cand.kind == LocatorKind.COORDINATES:
            tried.append("COORDINATES=not used after semantic drift")
            continue
        m = surface.resolve(target.frame, cand)
        tried.append(f"{cand.kind.value}={m.count}")
        if m.count == 1:
            return Resolution(match=m, candidate_index=i, tried=tried)
        if m.count > 1:
            raise AmbiguousTarget(i, m.count, f"{target.description}: {cand.kind.value} matched {m.count} elements")
    raise TargetNotFound(f"{target.description}: no candidate matched ({', '.join(tried)})")
