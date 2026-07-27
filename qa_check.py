"""
qa_check.py
===========
Offline quality checks for an `extraction_checkpoint.jsonl` produced by
extract_panel.py. Makes no API calls and costs nothing, so it can be run over the
full extraction as often as you like.

The pipeline's dangerous failure mode is a *silent* one: the model occasionally
drops an entry, or corrupts a surname, and reports success either way. Nothing
lands in failed_pages.log. These checks exist to surface those cases.

The strongest signal is the PDF's own OCR text layer. It is too fragmented to
extract structured data from, but it is an independent reading of the same page,
so disagreement between it and the model is a reliable review trigger:

  * a surname the model returned that the text layer has never heard of is
    usually a model OCR slip  (e.g. model "Boer" vs printed "Beer");
  * a surname the text layer sees more often than the model returned it is
    usually a dropped entry   (e.g. "Behre" printed 3x, returned 0x).

Usage
-----
    python qa_check.py                       # check everything, write qa_findings.csv
    python qa_check.py --pages 75 84         # restrict to a page range
    python qa_check.py --severity error      # only near-certain problems
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from profile_merge import merge_page_attempts, surname_of

CHECKPOINT_FILE = "extraction_checkpoint.jsonl"
DEFAULT_PDF = "American Men of Science_4th edition_1927.pdf"
FINDINGS_CSV = "qa_findings.csv"
DIRECTORY_YEAR = 1927
MIN_AGE_AT_MILESTONE = 15   # nobody earns a degree or holds a post before this

ERROR, WARN = "error", "warn"

# Entry starts in the printed text: a surname followed either by an honorific or
# by an initial in parentheses ("Behre,Prof." / "Behre,C(harles)"). Applied to the
# text layer after all whitespace is stripped, since the OCR spacing is chaotic.
# The surname is deliberately capital-then-lowercase only: the OCR frequently
# glues the running header or a hyphenated word onto the front of a name
# ("Ento-Beams", "ANMENOFSCIENCEBechtel") and this recovers just the real surname.
ENTRY_START_PATTERNS = [
    re.compile(r"([A-Z][a-z'\-]{1,20}),(?:Dr|Prof|Mrs|Miss|Mr|Dean|Rev|Gen|Col|Capt|Sir)\."),
    re.compile(r"([A-Z][a-z'\-]{1,20}),[A-Z]\("),
]

RUNNING_HEADER = re.compile(r"AMERICANMENOFSCIENCE", re.I)

POSITION_WORD = re.compile(
    r"^(instr|prof|asst|assoc|dir|lect|fellow|res|dean|head|supt|chief|curator)\b", re.I)


class Finding:
    __slots__ = ("severity", "code", "page", "scientist", "detail")

    def __init__(self, severity, code, page, scientist, detail=""):
        self.severity = severity
        self.code = code
        self.page = page
        self.scientist = scientist
        self.detail = detail


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_pages(path: Path, doc: Optional[fitz.Document] = None) -> dict[int, list[dict]]:
    """Focus page -> its profiles, unioned across every pass, exactly as
    extract_panel builds the panel. Auditing the last pass instead would report
    on data the CSVs never contained."""
    attempts: dict[int, list[list[dict]]] = defaultdict(list)
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("status") == "ok":
                attempts[int(rec["focus_page_number"])].append(rec.get("scientists", []))

    pages: dict[int, list[dict]] = {}
    for page, passes in attempts.items():
        confirms = None
        if doc is not None:
            text = squashed_text(doc, page)
            if text:
                confirms = lambda s, t=text: printed_count(t, s)   # noqa: E731
        pages[page] = merge_page_attempts(passes, confirms)
    return pages


def squashed_text(doc: fitz.Document, page_1based: int) -> str:
    text = re.sub(r"\s+", "", doc[page_1based - 1].get_text())
    return RUNNING_HEADER.sub("", text)


def entry_start_candidates(text: str) -> set[str]:
    """Surnames the text layer thinks start an entry on this page."""
    found: set[str] = set()
    for pattern in ENTRY_START_PATTERNS:
        found.update(pattern.findall(text))
    return found


def printed_count(text: str, surname: str) -> int:
    return len(re.findall(re.escape(surname) + ",", text))


def find_omissions(text: str, returned: Counter, window: tuple[str, str]
                   ) -> list[tuple[str, int, int]]:
    """Surnames the page prints more often than the extraction returned them.

    Returns (surname, n_printed, n_returned). Candidates are restricted to the
    alphabetical window so OCR debris -- glued running headers, hyphenated line
    breaks -- does not register as a missing scientist.
    """
    if not text:
        return []
    lo, hi = window
    out: list[tuple[str, int, int]] = []

    for surname in sorted(entry_start_candidates(text)):
        if not lo <= surname.lower() <= hi:
            continue
        n_printed = printed_count(text, surname)
        n_returned = returned.get(surname, 0)
        if n_returned == 0:
            # The model may have returned the same entry under a misread
            # surname; credit the closest spelling it did return.
            close = difflib.get_close_matches(surname, list(returned), n=1, cutoff=0.75)
            if close:
                n_returned = returned[close[0]]
        if n_printed > n_returned:
            out.append((surname, n_printed, n_returned))
    return out


def alphabetical_window(pages: dict[int, list[dict]], page: int) -> tuple[str, str]:
    """Surname range a missed entry on this page could plausibly fall into.

    Bounded by the neighbouring pages rather than by this page's own first and
    last surname, so an entry dropped from the very top or bottom still counts.
    """
    own = sorted((surname_of(p).lower() for p in pages[page] if surname_of(p)))
    lo = own[0] if own else ""
    hi = own[-1] if own else "zzzz"

    before = [p for p in pages if p < page]
    after = [p for p in pages if p > page]
    if before:
        prev = [surname_of(p).lower() for p in pages[max(before)] if surname_of(p)]
        if prev:
            lo = min(lo, prev[-1])
    if after:
        nxt = [surname_of(p).lower() for p in pages[min(after)] if surname_of(p)]
        if nxt:
            hi = max(hi, nxt[0])
    return lo, hi


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_against_text_layer(page: int, profiles: list[dict], text: str,
                             window: tuple[str, str], out: list[Finding]) -> None:
    """Compare the model's surnames with the page's own OCR text layer."""
    if not text:
        out.append(Finding(WARN, "no_text_layer", page, "",
                           "page has no OCR text layer; cross-check skipped"))
        return

    returned = Counter(surname_of(p) for p in profiles if surname_of(p))

    def printed(surname: str) -> int:
        return printed_count(text, surname)

    # Surnames the model invented, and entry starts it never returned.
    unprinted = sorted(s for s in returned if printed(s) == 0)

    lo, hi = window
    candidates = entry_start_candidates(text)
    # The directory is alphabetical, so a real entry on this page has to sort
    # inside the window its neighbours define. Anything else is OCR debris.
    unreturned = sorted(c for c in candidates - set(returned) if lo <= c.lower() <= hi)

    # Pair the two lists up: a near-match on both sides is one misread surname,
    # not an invention plus an omission.
    for wrong in list(unprinted):
        close = difflib.get_close_matches(wrong, unreturned, n=1, cutoff=0.75)
        if close:
            right = close[0]
            unprinted.remove(wrong)
            unreturned.remove(right)
            out.append(Finding(ERROR, "surname_misread", page, right,
                               "model returned '%s' but the page prints '%s'"
                               % (wrong, right)))
            # A misread can hide an omission: three printed "Behre" answered by
            # two mangled "Behr" is still one scientist short.
            shortfall = printed(right) - returned[wrong]
            if shortfall > 0:
                out.append(Finding(ERROR, "entry_possibly_missed", page, right,
                                   "'%s,' appears %d time(s) on the page but only %d "
                                   "profile(s) came back (as '%s')"
                                   % (right, printed(right), returned[wrong], wrong)))

    for surname in unprinted:
        hint = difflib.get_close_matches(surname, sorted(candidates), n=1, cutoff=0.7)
        out.append(Finding(ERROR, "surname_not_printed", page, surname,
                           "model returned '%s' but the page never prints '%s,'%s"
                           % (surname, surname,
                              " -- did it mean '%s'?" % hint[0] if hint else "")))

    for surname in unreturned:
        out.append(Finding(ERROR, "entry_possibly_missed", page, surname,
                           "the page prints an entry for '%s' (%dx) but no profile "
                           "with that surname came back" % (surname, printed(surname))))

    for surname, n_returned in sorted(returned.items()):
        n_printed = printed(surname)
        if n_printed > n_returned:
            out.append(Finding(WARN, "surname_undercounted", page, surname,
                               "'%s,' appears %d time(s) on the page but %d "
                               "entr(y/ies) came back" % (surname, n_printed, n_returned)))


def check_ordering(pages: dict[int, list[dict]], out: list[Finding]) -> None:
    """The directory is strictly alphabetical.

    NOTE: this catches out-of-order entries, overlaps between consecutive pages,
    and cross-page duplicates. It CANNOT detect a dropped entry -- a gap in the
    alphabet is indistinguishable from two genuinely adjacent surnames. Use the
    text-layer cross-check for omissions.
    """
    ordered = sorted(pages)
    prev_last: Optional[tuple[int, str]] = None

    for page in ordered:
        names = [surname_of(p) for p in pages[page] if surname_of(p)]
        if not names:
            out.append(Finding(ERROR, "empty_page", page, "", "no profiles returned"))
            continue

        for a, b in zip(names, names[1:]):
            if b.lower() < a.lower():
                out.append(Finding(WARN, "out_of_order", page, b,
                                   "'%s' follows '%s' but sorts before it" % (b, a)))

        if prev_last and names[0].lower() < prev_last[1].lower():
            out.append(Finding(ERROR, "page_overlap", page, names[0],
                               "page starts at '%s', before page %d ended at '%s' "
                               "-- possible duplicated entries"
                               % (names[0], prev_last[0], prev_last[1])))
        prev_last = (page, names[-1])

    seen: dict[str, list[int]] = defaultdict(list)
    for page in ordered:
        for p in pages[page]:
            seen[(p.get("full_name") or "").strip()].append(page)
    for name, hits in sorted(seen.items()):
        if len(hits) > 1:
            out.append(Finding(ERROR, "duplicate_scientist", hits[-1], name,
                               "appears on pages %s" % (hits,)))


def check_page_yield(pages: dict[int, list[dict]], out: list[Finding]) -> None:
    """A page returning far fewer entries than its neighbours is suspicious."""
    counts = {p: len(v) for p, v in pages.items()}
    if len(counts) < 5:
        return
    median = statistics.median(counts.values())
    cutoff = max(1.0, median * 0.6)
    for page, n in sorted(counts.items()):
        if n < cutoff:
            out.append(Finding(WARN, "low_entry_count", page, "",
                               "%d entries vs a median of %.0f across the run"
                               % (n, median)))


def check_profile(page: int, p: dict, out: list[Finding]) -> None:
    name = p.get("full_name") or "(unnamed)"
    birth = p.get("birth_year")

    if not birth:
        out.append(Finding(WARN, "no_birth_year", page, name, ""))
    elif not 1800 <= birth <= 1915:
        out.append(Finding(ERROR, "birth_year_implausible", page, name, str(birth)))

    if not p.get("department"):
        out.append(Finding(WARN, "no_department", page, name, ""))
    if not p.get("education"):
        out.append(Finding(WARN, "no_education", page, name, ""))
    if not p.get("employment"):
        out.append(Finding(WARN, "no_employment", page, name, ""))

    # Chronology. Both confirmed birth-year misreads in the 10-page sample
    # (67 read as 87, 56 as 76) surfaced here as degrees earned in infancy.
    for d in p.get("education") or []:
        y = d.get("year")
        label = "%s %s" % (d.get("degree_type"), d.get("institution") or "")
        if y and not 1800 <= y <= DIRECTORY_YEAR:
            out.append(Finding(ERROR, "year_out_of_range", page, name,
                               "degree %s in %s" % (label.strip(), y)))
        if y and birth and y < birth + MIN_AGE_AT_MILESTONE:
            out.append(Finding(ERROR, "milestone_before_adulthood", page, name,
                               "degree %s in %s but born %s -- check the birth year"
                               % (label.strip(), y, birth)))
        if POSITION_WORD.match((d.get("degree_type") or "")):
            out.append(Finding(ERROR, "position_in_education", page, name,
                               "degree_type=%r looks like a job title"
                               % d.get("degree_type")))

    n_current = 0
    for e in p.get("employment") or []:
        s, en = e.get("start_year"), e.get("end_year")
        title = e.get("position_title") or ""
        if e.get("is_current_position"):
            n_current += 1
        for y in (s, en):
            if y and not 1800 <= y <= DIRECTORY_YEAR:
                out.append(Finding(ERROR, "year_out_of_range", page, name,
                                   "employment %s year=%s" % (title, y)))
        if s and en and en < s:
            out.append(Finding(ERROR, "end_before_start", page, name,
                               "%s %s-%s" % (title, s, en)))
        if s and birth and s < birth + MIN_AGE_AT_MILESTONE:
            out.append(Finding(ERROR, "milestone_before_adulthood", page, name,
                               "position '%s' in %s but born %s -- check the birth year"
                               % (title, s, birth)))

    if n_current == 0:
        out.append(Finding(WARN, "no_current_position", page, name,
                           "no italic 1927 role: panel years to 1927 will be blank"))
    elif n_current > 2:
        out.append(Finding(WARN, "many_current_positions", page, name,
                           "%d positions marked current" % n_current))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(findings: list[Finding], pages: dict[int, list[dict]], severity: str) -> int:
    wanted = [f for f in findings if severity == "all" or f.severity == severity]

    by_code: dict[str, list[Finding]] = defaultdict(list)
    for f in wanted:
        by_code[f.code].append(f)

    n_profiles = sum(len(v) for v in pages.values())
    print("=" * 78)
    print("QA over %d pages / %d profiles" % (len(pages), n_profiles))
    print("=" * 78)

    if not wanted:
        print("\nNo findings at severity '%s'." % severity)
        return 0

    for code in sorted(by_code, key=lambda c: (by_code[c][0].severity != ERROR, -len(by_code[c]))):
        rows = by_code[code]
        print("\n[%s] %s  (%d)" % (rows[0].severity.upper(), code, len(rows)))
        for f in rows[:8]:
            print("    p%-5s %-34s %s" % (f.page, f.scientist[:34], f.detail))
        if len(rows) > 8:
            print("    ... and %d more (see %s)" % (len(rows) - 8, FINDINGS_CSV))

    suspect = sorted({f.page for f in findings if f.severity == ERROR
                      and f.code in ("entry_possibly_missed", "page_overlap",
                                     "empty_page", "duplicate_scientist")})
    print()
    print("=" * 78)
    print("Pages worth re-extracting (%d): %s" % (len(suspect), suspect or "none"))
    print("  python extract_panel.py --pages N N   (delete the page's checkpoint line first)")
    return len(suspect)


def write_csv(findings: list[Finding], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["severity", "code", "page", "scientist", "detail"])
        for x in sorted(findings, key=lambda x: (x.page, x.code)):
            w.writerow([x.severity, x.code, x.page, x.scientist, x.detail])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--checkpoint", default=CHECKPOINT_FILE)
    ap.add_argument("--pdf", default=DEFAULT_PDF)
    ap.add_argument("--pages", nargs=2, type=int, metavar=("START", "END"),
                    help="Restrict the check to this inclusive focus-page range.")
    ap.add_argument("--severity", default="all", choices=["all", "error", "warn"])
    ap.add_argument("--no-text-layer", action="store_true",
                    help="Skip the PDF cross-check (faster, much weaker).")
    ap.add_argument("--out", default=FINDINGS_CSV)
    args = ap.parse_args()

    base = Path(__file__).resolve().parent
    ckpt = Path(args.checkpoint)
    if not ckpt.is_absolute():
        ckpt = base / ckpt
    if not ckpt.exists():
        print("No checkpoint at %s -- run the extraction first." % ckpt)
        sys.exit(1)

    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = base / pdf
    doc = None
    if not args.no_text_layer:
        if pdf.exists():
            doc = fitz.open(pdf)
        else:
            print("PDF not found at %s -- skipping the text-layer cross-check.\n" % pdf)

    pages = load_pages(ckpt, doc)
    if args.pages:
        lo, hi = args.pages
        pages = {p: v for p, v in pages.items() if lo <= p <= hi}
    if not pages:
        print("No successfully extracted pages in that range.")
        sys.exit(1)

    findings: list[Finding] = []

    if doc is not None:
        for page in sorted(pages):
            check_against_text_layer(page, pages[page], squashed_text(doc, page),
                                     alphabetical_window(pages, page), findings)
        doc.close()

    check_ordering(pages, findings)
    check_page_yield(pages, findings)
    for page in sorted(pages):
        for profile in pages[page]:
            check_profile(page, profile, findings)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = base / out_path
    write_csv(findings, out_path)

    n_suspect = report(findings, pages, args.severity)
    print("Full findings written to %s" % out_path.name)
    sys.exit(1 if n_suspect else 0)


if __name__ == "__main__":
    main()
