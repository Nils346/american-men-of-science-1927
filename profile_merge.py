"""
profile_merge.py
================
Reconcile repeated extraction passes over the same directory page.

The vision model drops an entry now and then, and mangles the end of a surname
now and then, but rarely does either to the same scientist twice. Taking the
union of two passes therefore recovers most of what a single pass loses -- as
long as the two passes can be told apart from two genuinely different people,
which is the whole difficulty on a page carrying nine scientists called Beal.

Shared by extract_panel.py (building the panel) and qa_check.py (auditing it) so
both see identical profile sets. Deliberately free of project imports to keep
those two modules acyclic.
"""

from __future__ import annotations

import difflib
import re
from typing import Callable, Optional

MATCH_THRESHOLD = 0.85
INITIALS_MISMATCH_PENALTY = 0.05   # 'B(olla)' vs 'R(olla)' must still be able to pair
BIRTH_YEAR_BONUS = 0.10

_TITLE_RE = re.compile(r"\b(Dr|Prof|Mr|Mrs|Miss|Dean|Rev|Gen|Col|Capt|Sir)\.?\s*", re.I)
_ANNOTATION_RE = re.compile(r"\([^)]*\s[^)]*\)")   # '(Mrs. Warren Mack)', not '(eanette)'


def surname_of(profile: dict) -> str:
    return (profile.get("full_name") or "").split(",")[0].strip()


def _name_key(full_name: str) -> str:
    """Whole name, lowercased to letters only:
    'Behre, Dr. J(eanette) A(llen)' -> 'behrejeanetteallen'.

    Multi-word parentheticals are dropped: the directory appends things like
    '(Mrs. Warren Mack)' that one pass may record and another omit, and they
    otherwise swamp the comparison.
    """
    cleaned = _ANNOTATION_RE.sub("", full_name or "")
    return re.sub(r"[^a-z]", "", _TITLE_RE.sub("", cleaned).lower())


def _given_initials(full_name: str) -> str:
    """Initials of the given names, ignoring honorifics and expanded spellings.

    'Behre, Dr. J(eanette) A(llen)' -> 'ja'
    """
    name = full_name or ""
    given = name.split(",", 1)[1] if "," in name else ""
    given = _TITLE_RE.sub("", re.sub(r"\(.*?\)", "", given))
    return "".join(re.findall(r"([A-Za-z])\w*", given))[:2].lower()


def match_score(a: dict, b: dict) -> float:
    """How strongly two profiles from *different* passes look like one scientist.

    Names are compared fuzzily on purpose: the recurring OCR failure mangles the
    end of a surname ('Behre' -> 'Behr', 'Beer' -> 'Boer'), so two passes
    routinely disagree on the spelling of the same person. The comparison uses
    the full name rather than the surname alone, because a single page can carry
    nine different scientists called Beal.
    """
    ka, kb = _name_key(a.get("full_name", "")), _name_key(b.get("full_name", ""))
    if not ka or not kb:
        return 0.0
    score = difflib.SequenceMatcher(None, ka, kb).ratio()

    # Matching initials are strong evidence, but a mismatch cannot veto the
    # match: OCR corrupts initials too ('R(olla) Kent' read as 'B(olla) Kent').
    ia, ib = _given_initials(a.get("full_name", "")), _given_initials(b.get("full_name", ""))
    if ia and ib and ia != ib:
        score -= INITIALS_MISMATCH_PENALTY

    ya, yb = a.get("birth_year"), b.get("birth_year")
    if ya and ya == yb:
        score += BIRTH_YEAR_BONUS
    return score


def _richness(profile: dict) -> int:
    """Crude completeness score, used to pick between two versions of a record."""
    scalars = sum(1 for k, v in profile.items()
                  if not isinstance(v, (list, dict)) and v not in (None, "", False))
    return (scalars
            + len(profile.get("education") or [])
            + len(profile.get("employment") or [])
            + len(profile.get("minor_positions") or [])
            + len(profile.get("societies") or []))


def _pick_better(a: dict, b: dict, confirms: Optional[Callable[[str], int]]) -> dict:
    """Choose between two readings of one scientist.

    Prefers the spelling the PDF's own text layer confirms -- that is how a
    merge repairs 'Boer' back to 'Beer' -- and otherwise the fuller record.
    """
    if confirms is not None:
        ca, cb = confirms(surname_of(a)), confirms(surname_of(b))
        if ca and not cb:
            return a
        if cb and not ca:
            return b
    return a if _richness(a) >= _richness(b) else b


def merge_page_attempts(attempts: list[list[dict]],
                        confirms: Optional[Callable[[str], int]] = None) -> list[dict]:
    """Union the profiles from repeated passes over one page.

    `confirms(surname) -> count` reports how often the page actually prints that
    surname; pass qa_check.printed_count bound to the page text.

    Profiles within a single pass are never merged with each other: the model
    listed them as separate entries, so they are separate scientists even when
    the names look nearly identical. Matching across passes is one-to-one,
    best-scoring pair first.
    """
    if not attempts:
        return []

    merged = list(attempts[0])
    for attempt in attempts[1:]:
        pairs = sorted(
            ((match_score(existing, new), i, j)
             for i, existing in enumerate(merged)
             for j, new in enumerate(attempt)),
            key=lambda t: -t[0],
        )
        used_existing: set[int] = set()
        used_new: set[int] = set()
        for score, i, j in pairs:
            if score < MATCH_THRESHOLD:
                break
            if i in used_existing or j in used_new:
                continue
            used_existing.add(i)
            used_new.add(j)
            merged[i] = _pick_better(merged[i], attempt[j], confirms)
        merged.extend(p for j, p in enumerate(attempt) if j not in used_new)
    return merged
