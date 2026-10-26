"""Entity resolution: name similarity and the merge bands.

similarity >= 0.85          -> "auto-merge" (0.85 exactly merges)
0.82 <= similarity < 0.85   -> "review"     (0.82 exactly queues for an operator)
similarity < 0.82           -> "new"

Similarity is 1 - levenshtein / max(len) over NORMALIZED names, counted in
Unicode code points.
"""

from __future__ import annotations

import math
from typing import Literal

from .ids import normalizeName

# Logical invariants: the spec's merge bands (World Model AGENTS.md section 7.1 and
# the conformance runner's boundary rulings).
AUTO_MERGE_AT = 0.85
REVIEW_LOWER = 0.82

Decision = Literal["auto-merge", "review", "new"]


def levenshtein(a: str, b: str) -> int:
    """Edit distance (insert, delete, substitute; each costs one)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        curr = [i]
        for j, cb in enumerate(b, start=1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def nameSimilarity(a: str, b: str) -> float:
    """1 - levenshtein / max(len) of the normalized names; two empty names are identical."""
    na, nb = normalizeName(a), normalizeName(b)
    longest = max(len(na), len(nb))
    if longest == 0:
        return 1.0
    return 1.0 - levenshtein(na, nb) / longest


def decide(
    similarity: float,
    autoMergeAt: float | None = AUTO_MERGE_AT,
    reviewLower: float = REVIEW_LOWER,
) -> Decision:
    """Band a similarity. `autoMergeAt=None` turns auto-merge off: the whole upper band
    goes to the review queue."""
    if isinstance(similarity, bool) or not isinstance(similarity, (int, float)):
        raise TypeError(f"decide needs a number, got {type(similarity).__name__}")
    if math.isnan(similarity):
        raise ValueError("decide: similarity is NaN")
    if autoMergeAt is not None and similarity >= autoMergeAt:
        return "auto-merge"
    if similarity >= reviewLower:
        return "review"
    return "new"
