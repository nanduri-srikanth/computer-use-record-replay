"""Locator ladder (backbone item 3). Surface-agnostic.

Candidates are tried in ladder order. 0 matches: next candidate.
More than 1 match: stop immediately as AMBIGUOUS_TARGET, never guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import TargetDescriptor
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
    for i, cand in enumerate(target.candidates):
        m = surface.resolve(target.frame, cand)
        tried.append(f"{cand.kind.value}={m.count}")
        if m.count == 1:
            return Resolution(match=m, candidate_index=i, tried=tried)
        if m.count > 1:
            raise AmbiguousTarget(i, m.count, f"{target.description}: {cand.kind.value} matched {m.count} elements")
    raise TargetNotFound(f"{target.description}: no candidate matched ({', '.join(tried)})")
