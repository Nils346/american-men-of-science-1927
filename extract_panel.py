"""
extract_panel.py
================
Production pipeline for extracting structured scientist biographies from the
"American Men of Science" 4th Edition (1927) directory PDF using the Anthropic
Claude vision API, and transforming them into a long-format Scientist-Year-Event
panel CSV for econometric analysis (Stata / R).

Pipeline phases
---------------
1. PDF chunking with a "Focus + Look-Ahead" 2-page window (PyMuPDF page images).
2. Claude API extraction validated against a strict Pydantic data contract.
3. Fault tolerance: exponential-backoff retries, truncation detection, debug
   dumps, failed-page logging, and JSONL checkpointing with automatic resume.
4. Flattening of the nested JSON records into scientist_mobility_panel.csv.

Usage examples (PowerShell)
---------------------------
    # Verify everything on a tiny window first (focus pages 15-16 of the PDF):
    python extract_panel.py --test-run --pages 15 16

    # Full run over the whole listing (resumes automatically if interrupted):
    python extract_panel.py --pages 14 1123

    # Rebuild the panel CSV from the existing checkpoint without calling the API:
    python extract_panel.py --panel-only

    # Render the images that would be sent, without spending API tokens:
    python extract_panel.py --pages 15 16 --dry-run
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import fitz  # PyMuPDF
import pandas as pd
from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    RateLimitError,
)
from pydantic import BaseModel, Field, ValidationError, field_validator
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

DEFAULT_PDF = "American Men of Science_4th edition_1927.pdf"
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_DPI = 150            # ~1100 x 1580 px per page: near Claude's optimal 1568px long edge
DEFAULT_MAX_TOKENS = 32000   # dense pages can hold 25+ profiles
DEFAULT_START_PAGE = 14      # 1-based PDF page where the scientist listing begins
DEFAULT_END_PAGE = 1123      # 1-based PDF page where the listing ends
DIRECTORY_YEAR = 1927        # edition year: current (italic) positions run up to here

CHECKPOINT_FILE = "extraction_checkpoint.jsonl"
FAILED_PAGES_LOG = "failed_pages.log"
DEBUG_DIR = "debug"
PANEL_CSV = "scientist_mobility_panel.csv"          # balanced scientist-year panel
EVENTS_CSV = "scientist_events_long.csv"            # one row per dated event (audit)
SUMMARY_CSV = "scientist_summary.csv"               # one row per scientist (invariants)
RAW_PROFILES_JSON = "scientists_raw.json"

logger = logging.getLogger("extract_panel")


# ---------------------------------------------------------------------------
# Pydantic data contract
# ---------------------------------------------------------------------------

class DegreeRecord(BaseModel):
    """One earned degree, in chronological order of appearance in the entry."""
    degree_type: str
    institution: Optional[str] = None
    year: Optional[int] = None

    @field_validator("year", mode="before")
    @classmethod
    def _coerce_year(cls, v):
        return _coerce_optional_int(v)


class EmploymentRecord(BaseModel):
    """One career position, in chronological order of appearance in the entry."""
    position_title: str
    institution_org: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    is_current_position: bool = False

    @field_validator("start_year", "end_year", mode="before")
    @classmethod
    def _coerce_years(cls, v):
        return _coerce_optional_int(v)


class MinorPositionRecord(BaseModel):
    """A parallel / temporary / minor appointment held alongside the main career
    (fellowships, lectureships, summer or military or committee/editorial roles)."""
    position_title: str
    institution_org: Optional[str] = None
    start_year: Optional[int] = None
    end_year: Optional[int] = None

    @field_validator("start_year", "end_year", mode="before")
    @classmethod
    def _coerce_years(cls, v):
        return _coerce_optional_int(v)


class ScientistProfile(BaseModel):
    """One complete biographical entry from the directory."""
    full_name: str
    titles: Optional[str] = None
    mailing_address: str = ""
    mailing_city: Optional[str] = None
    mailing_state: Optional[str] = None
    mailing_country: Optional[str] = None
    star_status: bool = False
    department: str = ""
    birth_place: Optional[str] = None
    birth_city: Optional[str] = None
    birth_state: Optional[str] = None
    birth_country: Optional[str] = None
    birth_date: Optional[str] = None
    birth_year: Optional[int] = None
    education: List[DegreeRecord] = Field(default_factory=list)
    employment: List[EmploymentRecord] = Field(default_factory=list)
    minor_positions: Optional[List[MinorPositionRecord]] = None
    societies: Optional[List[str]] = None
    research_accomplished: Optional[str] = None
    research_in_progress: Optional[str] = None

    @field_validator("birth_year", mode="before")
    @classmethod
    def _coerce_year(cls, v):
        return _coerce_optional_int(v)

    @field_validator("minor_positions", mode="before")
    @classmethod
    def _coerce_minor_positions(cls, v):
        # Tolerate the model returning bare strings or loosely-keyed dicts.
        if v is None:
            return None
        if isinstance(v, (str, dict)):
            v = [v]
        out = []
        for item in v:
            if isinstance(item, MinorPositionRecord):
                out.append(item)
            elif isinstance(item, dict):
                out.append(item)
            elif item is not None:
                out.append({"position_title": str(item)})
        return out or None

    @field_validator("societies", mode="before")
    @classmethod
    def _coerce_str_list(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            out = []
            for item in v:
                if isinstance(item, dict):
                    out.append("; ".join(str(x) for x in item.values() if x))
                elif item is not None:
                    out.append(str(item))
            return out
        return v


class PageExtractionContainer(BaseModel):
    """Top-level object the LLM must return for every focus page."""
    focus_page_number: int
    scientists: List[ScientistProfile] = Field(default_factory=list)


def _coerce_optional_int(v):
    """Tolerate strings like '1898', "'98", 'ca. 1912', or empty values."""
    if v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"(\d{4})", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})", s)
    if m:
        # Bare 2-digit year the model failed to expand: assume 18xx/19xx split at 27
        yy = int(m.group(1))
        return 1900 + yy if yy <= 27 else 1800 + yy
    return None


# ---------------------------------------------------------------------------
# Prompt engineering
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert archivist extracting biographical entries from scanned pages of
"American Men of Science" (4th Edition, 1927), a dense two-column biographical
directory. You return ONLY a single valid JSON object -- no prose, no markdown
fences, no commentary.

# INPUT
You receive images of TWO consecutive PDF pages:
- Image 1 = the FOCUS PAGE.
- Image 2 = the LOOK-AHEAD PAGE (the very next page; may be absent for the last page).

# EXTRACTION BOUNDARY RULE (critical -- prevents duplicates and data loss)
- Extract ONLY the scientists whose entries BEGIN on the FOCUS PAGE. An entry
  begins with a bold, left-indented surname paragraph (e.g. "Abbott, Dr. C(harles) H(arlan),").
- If an entry begins on the focus page and continues onto the look-ahead page,
  use the look-ahead page text to COMPLETE that record fully.
- If text at the top of the focus page belongs to an entry that began on a PREVIOUS
  page (i.e. the column starts mid-sentence with no bold name), IGNORE it entirely --
  it was already captured when the previous page was processed.
- Read the page in column order: finish the entire left column top-to-bottom, then
  the right column. Entries also flow from the bottom of the left column to the top
  of the right column, and from the bottom-right of the focus page to the top-left
  of the look-ahead page.

# ENTRY LAYOUT (fields appear in this exact order within each paragraph)
1. full_name: bold, format "Last, First (Middle/Omitted names in parentheses)".
   Keep the parentheses exactly as printed. Do NOT include titles in full_name.
2. titles: honorifics like "Dr.", "Prof.", "Gen." printed inside the bold name
   string (e.g. "Abbott, Dr. C(harles)..."); null if absent.
3. mailing_address: everything between the name and the italicized department
   (street, institution, city, state), as one string. Then ALSO parse its
   geography:
   - mailing_city: the city/town (e.g. "Redlands").
   - mailing_state: the US state or Canadian province, kept as printed
     ("Calif", "N. Y", "S. Dak", "Que"); null if the location is outside the
     US/Canada.
   - mailing_country: infer the country -- "USA" for US states, "Canada" for
     Canadian provinces (Que, Ont, B.C, etc.), or the actual country for foreign
     addresses ("Peru", "England"). Default to "USA" when a US state is present.
4. star_status: true ONLY if an asterisk (*) is printed directly BEFORE the
   italicized department of investigation. Otherwise false. Stars mark the top
   1,000 scientists; the vast majority of entries have NO star.
5. department: the scientific field printed in italics right after the address
   (e.g. "Zoology.", "Physiological chemistry."). Strip the trailing period.
6. Birth data: place then date, e.g. "Antrim, N. H, March 1, 89." ->
   birth_place   = "Antrim, N.H." (the raw place string as printed),
   birth_city    = "Antrim",
   birth_state   = "N.H" (US state / Canadian province as printed; null if the
                   birthplace is in another country, where the token after the
                   city is actually a country -- e.g. "Liverpool, Eng" has NO state),
   birth_country = "USA" (infer: "USA" for US states, "Canada" for Canadian
                   provinces, else the country, e.g. "England" for "Eng",
                   "Peru", "Germany" for "Ger"),
   birth_date    = "March 1, 1889" (the FULL calendar date -- month name, day,
                   and 4-digit year; if the day or month is missing give what is
                   printed; null if only a year is given),
   birth_year    = 1889 (expand 2-digit years: these scientists were born in the
                   19th or very early 20th century).
7. education: earned or honorary DEGREES only, in chronological order, e.g.
   "A.B, Brown, 13, A.M, 14, Ph.D, 18." A degree_type is a letter abbreviation
   such as A.B, B.S, A.M, M.S, Ph.D, Sc.D, M.D, LL.B, C.E, D.Sc (optionally
   prefixed "hon." for honorary). NEVER put an employment position (Instr., prof,
   asst, biol, etc.) or its institution in education -- those belong only in
   employment. The words 'college'/'university' are omitted in print; keep
   institution names as printed. When consecutive degrees omit the institution,
   it is the SAME institution as the previous degree -- fill it in. Expand 2-digit
   years to 4 digits using the birth year as anchor (a degree year must be
   >= birth_year + ~15 and <= 1927).
8. employment: the MAIN CAREER CHAIN. This is the single semicolon-separated
   sequence of positions that runs from the earliest job up to and INCLUDING the
   italicized current 1927 position, e.g.:
     "Instr. zool, Wash. Col, 14-15; biol, Haverford, 16-17; asst, Yale, 19;
      instr. zool, Mass. Col, 19-22; prof, Redlands, 22-"
   EVERY link in that chain is an employment record, IN ORDER -- including links
   whose position title is abbreviated to a bare field word ("biol", "zool",
   "path") because of the carry-forward convention below. Do NOT pull a middle
   link (like "biol, Haverford, 16-17") out into minor_positions; if it sits
   inside the main semicolon chain of dated jobs, it is employment.
   CRITICAL carry-forward convention of this directory: when the same position is
   held successively at different institutions, the position title is NOT repeated
   (carry it forward from the previous record); when different positions are held
   at the same institution, the institution is NOT repeated (carry it forward).
   Fill in the omitted value from the previous record so every employment record
   has both a position_title and an institution_org.
   RECONSTRUCT abbreviated titles per this convention: when a link is only a bare
   SUBJECT/FIELD word (e.g. "biol", "zool", "chem", "path", "bot") with no rank,
   prepend the RANK carried forward from the previous link so the title is
   complete. Example chain "Instr. zool, Wash. Col, 14-15; biol, Haverford, 16-17"
   -> second record position_title = "Instr. biol" (the rank "Instr." carries; the
   subject changes to biology). But when a link states its OWN rank ("asst",
   "assoc", "prof", "dir", "lecturer"), use that rank as printed and do NOT carry
   the old one. Never invent a rank that is not implied by the chain.
   Parse date ranges into start_year / end_year (expand 2-digit years; "19-" or a
   single year with no end often means ongoing -- leave end_year null if
   open-ended). The position printed in ITALICS is the scientist's CURRENT 1927
   position: set is_current_position = true for that record only (usually the last
   link; at most one or two records). NEVER place research subjects, societies, or
   honors in employment.
9. minor_positions: appointments that are NOT part of the main career chain --
   they typically appear AFTER the italic current position, or are clearly of a
   different kind: military service (e.g. "Second lieut, Sanit. C, 18"),
   fellowships, summer/visiting/acting appointments, lectureships given elsewhere,
   committee or editorial roles, delegate/officer roles. Test: if removing it does
   NOT break the chronological chain of primary jobs, it is a minor position.
   Return each as an OBJECT with position_title, institution_org (null if none),
   start_year, end_year (expand 2-digit years; leave end_year null if a single
   year or open-ended). Null if none.
10. societies: memberships in scientific societies, kept as the printed
    abbreviations (e.g. "A.A.", "Soc. Mammal", "F.A.A."), one string per society.
11. Research: the final block lists research SUBJECTS/TOPICS (e.g. "Ecology; light
    reactions of land isopods"). Topics BEFORE the dash (-) -> research_accomplished;
    topics AFTER the dash -> research_in_progress. If there is no dash, put everything
    in research_accomplished and set research_in_progress to null. CRITICAL: research
    subjects are descriptive phrases, NOT institutions or job titles -- never let them
    leak into employment.institution_org, employment.position_title, or minor_positions.

# OCR / TYPOGRAPHY GUIDANCE
- This is an old scanned book: expect broken kerning and stray spaces around
  punctuation ("A.B ,  Brown , 13"). Normalize spacing sensibly.
- Two-digit years are shorthand: '98 -> 1898, 13 -> 1913, 26 -> 1926. Use the
  scientist's own chronology (birth -> degrees -> jobs, all <= 1927) to expand them.
- "(sec'y-treas, ed, 'Bul')" style parentheticals inside employment describe roles;
  keep them attached to the relevant record's position_title.
- The old italic typeface confuses s/z. ALWAYS read "sool" as "zool" (zoology)
  and "soolog" as "zoolog": e.g. "instr. sool" -> "instr. zool". There is no word
  "sool"; give the sensible reading. Watch for the same s/z slip in other
  field words when the context is a science subject.

# OUTPUT FORMAT
Return exactly one JSON object matching this schema (no extra keys, no markdown):
{
  "focus_page_number": <int, the PDF page number given in the user message>,
  "scientists": [
    {
      "full_name": "...", "titles": "..." | null, "mailing_address": "...",
      "mailing_city": "..." | null, "mailing_state": "..." | null, "mailing_country": "..." | null,
      "star_status": true|false, "department": "...",
      "birth_place": "..." | null, "birth_city": "..." | null, "birth_state": "..." | null,
      "birth_country": "..." | null, "birth_date": "..." | null, "birth_year": <int> | null,
      "education": [{"degree_type": "...", "institution": "...", "year": <int>|null}],
      "employment": [{"position_title": "...", "institution_org": "...",
                      "start_year": <int>|null, "end_year": <int>|null,
                      "is_current_position": true|false}],
      "minor_positions": [{"position_title": "...", "institution_org": "..."|null,
                           "start_year": <int>|null, "end_year": <int>|null}] | null,
      "societies": ["..."] | null,
      "research_accomplished": "..." | null,
      "research_in_progress": "..." | null
    }
  ]
}
If NO new entry begins on the focus page (e.g. a blank or front-matter page),
return {"focus_page_number": <int>, "scientists": []}.
"""


def build_user_content(focus_page_1based: int, focus_png: bytes,
                       lookahead_png: Optional[bytes]) -> list:
    """Assemble the multimodal user message for one Focus + Look-Ahead window."""
    content = [
        {
            "type": "text",
            "text": (
                f"FOCUS PAGE = PDF page {focus_page_1based} (Image 1). "
                + (f"LOOK-AHEAD PAGE = PDF page {focus_page_1based + 1} (Image 2). "
                   if lookahead_png else "There is NO look-ahead page (last page). ")
                + f"Extract every scientist whose entry BEGINS on PDF page "
                  f"{focus_page_1based}, completing any entry that spills onto the "
                  f"look-ahead page. Set focus_page_number = {focus_page_1based}. "
                  f"Return ONLY the JSON object."
            ),
        },
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(focus_png).decode("ascii"),
            },
        },
    ]
    if lookahead_png:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(lookahead_png).decode("ascii"),
            },
        })
    return content


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def render_page_png(doc: fitz.Document, page_index_0based: int, dpi: int) -> bytes:
    """Render a single PDF page to PNG bytes at the given DPI."""
    page = doc[page_index_0based]
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


# ---------------------------------------------------------------------------
# Anthropic API interaction
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """Retry on rate limits (429), server errors (5xx), timeouts, and drops."""
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError) and exc.status_code >= 500:
        return True
    return False


class TruncatedResponseError(Exception):
    """Raised when the model hit max_tokens before completing the JSON."""
    def __init__(self, raw_text: str):
        super().__init__("Response truncated at max_tokens")
        self.raw_text = raw_text


class ExtractionClient:
    """Thin wrapper around the Anthropic client with retries and token accounting."""

    def __init__(self, api_key: str, model: str, max_tokens: int):
        self.client = Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # Newer Sonnet models enable adaptive thinking by default; we disable it
        # for deterministic, token-efficient JSON output. If the target model
        # rejects the parameter we silently drop it (see _call).
        self._thinking_supported = True

    @retry(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=2, min=2, max=90),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _call(self, content: list):
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                # Cache the long system prompt across the hundreds of page calls.
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": content}],
        )
        if self._thinking_supported:
            kwargs["thinking"] = {"type": "disabled"}
        try:
            return self._stream_message(kwargs)
        except BadRequestError as e:
            if self._thinking_supported and "thinking" in str(e).lower():
                logger.warning("Model rejected 'thinking' parameter; retrying without it.")
                self._thinking_supported = False
                kwargs.pop("thinking", None)
                return self._stream_message(kwargs)
            raise

    def _stream_message(self, kwargs: dict):
        # The SDK requires streaming for requests whose max_tokens implies a
        # potential runtime over 10 minutes; stream and return the final message.
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def extract_page(self, focus_page_1based: int, focus_png: bytes,
                     lookahead_png: Optional[bytes]) -> tuple[str, dict]:
        """Call the API for one window; return (raw_text, usage_dict).

        Raises TruncatedResponseError if the output hit the token wall.
        """
        content = build_user_content(focus_page_1based, focus_png, lookahead_png)
        response = self._call(content)

        usage = {
            "input_tokens": getattr(response.usage, "input_tokens", 0),
            "output_tokens": getattr(response.usage, "output_tokens", 0),
        }
        self.total_input_tokens += usage["input_tokens"]
        self.total_output_tokens += usage["output_tokens"]

        raw_text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        if response.stop_reason == "max_tokens":
            raise TruncatedResponseError(raw_text)
        return raw_text, usage


def parse_llm_json(raw_text: str) -> PageExtractionContainer:
    """Parse the model output into the Pydantic contract, tolerating fences/prose."""
    text = raw_text.strip()
    # Strip accidental markdown fences.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Isolate the outermost JSON object in case of stray prose.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("No JSON object found in response", text, 0)
    payload = json.loads(text[start:end + 1])
    return PageExtractionContainer.model_validate(payload)


# ---------------------------------------------------------------------------
# Checkpointing & failure bookkeeping
# ---------------------------------------------------------------------------

def load_completed_pages(checkpoint_path: Path) -> set[int]:
    """Return the set of focus pages already successfully extracted."""
    completed: set[int] = set()
    if not checkpoint_path.exists():
        return completed
    with checkpoint_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if rec.get("status") == "ok":
                    completed.add(int(rec["focus_page_number"]))
            except (json.JSONDecodeError, KeyError, ValueError):
                logger.warning("Skipping unreadable checkpoint line: %.80s", line)
    return completed


def append_checkpoint(checkpoint_path: Path, record: dict) -> None:
    """Append one JSON line and flush to disk so a crash cannot lose it."""
    with checkpoint_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def log_failed_page(failed_log_path: Path, page: int, reason: str) -> None:
    with failed_log_path.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}\tpage={page}\t{reason}\n")


def save_debug_artifact(debug_dir: Path, page: int, raw_text: str, label: str) -> Path:
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"page_{page:04d}_{label}.txt"
    path.write_text(raw_text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Extraction loop
# ---------------------------------------------------------------------------

def run_extraction(args, base_dir: Path) -> None:
    checkpoint_path = base_dir / CHECKPOINT_FILE
    failed_log_path = base_dir / FAILED_PAGES_LOG
    debug_dir = base_dir / DEBUG_DIR

    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = base_dir / pdf_path
    if not pdf_path.exists():
        logger.error("PDF not found: %s", pdf_path)
        sys.exit(1)

    doc = fitz.open(pdf_path)
    logger.info("Opened PDF '%s' (%d pages).", pdf_path.name, doc.page_count)

    start_page, end_page = args.pages
    start_page = max(1, start_page)
    end_page = min(doc.page_count, end_page)
    pages_to_process = list(range(start_page, end_page + 1))

    if args.test_run:
        pages_to_process = pages_to_process[:2]
        logger.info("TEST RUN: restricted to focus pages %s.", pages_to_process)

    completed = load_completed_pages(checkpoint_path)
    if completed:
        logger.info("Checkpoint found: %d pages already completed; they will be skipped.",
                    len(completed))
    remaining = [p for p in pages_to_process if p not in completed]
    logger.info("Processing %d focus page(s): %d..%d (%d skipped as done).",
                len(remaining), start_page, end_page, len(pages_to_process) - len(remaining))

    if args.dry_run:
        for page in remaining[:3]:
            png = render_page_png(doc, page - 1, args.dpi)
            out = debug_dir / f"dryrun_page_{page:04d}.png"
            debug_dir.mkdir(parents=True, exist_ok=True)
            out.write_bytes(png)
            logger.info("DRY RUN: rendered focus page %d -> %s (%.0f KB).",
                        page, out, len(png) / 1024)
        logger.info("DRY RUN complete; no API calls made.")
        return

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("No API key. Pass --api-key or set the ANTHROPIC_API_KEY "
                     "environment variable.")
        sys.exit(1)

    client = ExtractionClient(api_key=api_key, model=args.model,
                              max_tokens=args.max_tokens)

    n_ok, n_failed, n_scientists = 0, 0, 0
    t0 = time.time()

    for i, page in enumerate(remaining, start=1):
        focus_idx = page - 1                      # 0-based index of focus page
        lookahead_idx = page if page < doc.page_count else None

        logger.info("[%d/%d] Rendering window: focus page %d%s ...",
                    i, len(remaining), page,
                    f" + look-ahead page {page + 1}" if lookahead_idx is not None else
                    " (no look-ahead: last page)")
        focus_png = render_page_png(doc, focus_idx, args.dpi)
        lookahead_png = (render_page_png(doc, lookahead_idx, args.dpi)
                         if lookahead_idx is not None else None)

        raw_text = ""
        try:
            raw_text, usage = client.extract_page(page, focus_png, lookahead_png)
            container = parse_llm_json(raw_text)
            container.focus_page_number = page    # trust our loop, not the model

            record = {
                "focus_page_number": page,
                "status": "ok",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "n_scientists": len(container.scientists),
                "usage": usage,
                "scientists": [s.model_dump() for s in container.scientists],
            }
            append_checkpoint(checkpoint_path, record)
            n_ok += 1
            n_scientists += len(container.scientists)
            logger.info(
                "[%d/%d] Page %d OK: %d scientists | tokens in=%d out=%d "
                "(cumulative in=%d out=%d) | checkpoint written.",
                i, len(remaining), page, len(container.scientists),
                usage["input_tokens"], usage["output_tokens"],
                client.total_input_tokens, client.total_output_tokens,
            )

        except TruncatedResponseError as e:
            n_failed += 1
            path = save_debug_artifact(debug_dir, page, e.raw_text, "truncated")
            log_failed_page(failed_log_path, page, "output truncated at max_tokens")
            append_checkpoint(checkpoint_path, {
                "focus_page_number": page, "status": "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": "truncated",
            })
            logger.error("Page %d FAILED: output truncated at max_tokens "
                         "(raw saved to %s). Consider raising --max-tokens.", page, path)

        except (json.JSONDecodeError, ValidationError) as e:
            n_failed += 1
            path = save_debug_artifact(debug_dir, page, raw_text, "parse_error")
            log_failed_page(failed_log_path, page, f"parse/validation error: {e!r:.200}")
            append_checkpoint(checkpoint_path, {
                "focus_page_number": page, "status": "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": f"parse_error: {type(e).__name__}",
            })
            logger.error("Page %d FAILED: response did not match the schema "
                         "(%s). Raw output saved to %s.", page, type(e).__name__, path)

        except (RateLimitError, APIConnectionError, APITimeoutError, APIStatusError) as e:
            # All 5 retry attempts exhausted -- fail this window gracefully.
            n_failed += 1
            log_failed_page(failed_log_path, page, f"api_error after retries: {e!r:.200}")
            append_checkpoint(checkpoint_path, {
                "focus_page_number": page, "status": "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": f"api_error: {type(e).__name__}",
            })
            logger.error("Page %d FAILED after retries: %s. Continuing with next page.",
                         page, e)

    doc.close()
    elapsed = time.time() - t0
    logger.info("=" * 70)
    logger.info("Extraction finished in %.1f min: %d pages OK, %d failed, "
                "%d scientist profiles extracted.",
                elapsed / 60, n_ok, n_failed, n_scientists)
    logger.info("Total token consumption: input=%d, output=%d.",
                client.total_input_tokens, client.total_output_tokens)
    if n_failed:
        logger.warning("Failed pages are listed in '%s'. Re-run the same command "
                       "to retry them (successful pages are skipped automatically).",
                       FAILED_PAGES_LOG)


# ---------------------------------------------------------------------------
# Panel transformation
# ---------------------------------------------------------------------------

_TAIL_PLACE_RE = re.compile(r"([A-Za-z][A-Za-z .'&/-]*,\s*[A-Za-z][A-Za-z. ]*?)\.?\s*$")


def split_place(place: str) -> tuple[Optional[str], Optional[str]]:
    """Split a 'City, State/Country' string into (city, state) on the LAST comma.

    Birth places and address tails are printed 'City, State' (e.g. "Antrim, N.H.",
    "Liverpool, Eng."). If there is no comma we treat the whole thing as the city."""
    if not place:
        return None, None
    s = place.strip().rstrip(".")
    if "," in s:
        city, state = s.rsplit(",", 1)
        return (city.strip() or None), (state.strip() or None)
    return (s or None), None


def extract_mailing_place(mailing_address: str) -> tuple[Optional[str], Optional[str]]:
    """Grab the trailing 'City, State' from a full mailing address and split it."""
    if not mailing_address:
        return None, None
    m = _TAIL_PLACE_RE.search(mailing_address.strip())
    tail = m.group(1) if m else mailing_address
    return split_place(tail)


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def format_birth_date(raw: Optional[str], birth_year: Optional[int]) -> Optional[str]:
    """Normalise a birth date to DD.MM.YYYY.

    Accepts strings like "March 1, 1889", "Dec. 3, 1888", "1889". Missing
    components are zero-filled ("00"). Returns None if nothing usable is found."""
    text = (raw or "").strip()
    day = month = year = None

    m = re.search(r"([A-Za-z]{3,9})", text)
    if m:
        month = _MONTHS.get(m.group(1)[:4].lower().rstrip(".")) or \
                _MONTHS.get(m.group(1)[:3].lower())

    m = re.search(r"\b(\d{4})\b", text)
    if m:
        year = int(m.group(1))
    # Day: a 1-2 digit number that is not the 4-digit year.
    for m in re.finditer(r"\b(\d{1,2})\b", text):
        val = int(m.group(1))
        if 1 <= val <= 31:
            day = val
            break

    if year is None:
        year = birth_year
    if year is None:
        return None
    return f"{(day or 0):02d}.{(month or 0):02d}.{year:04d}"


def _first(*values):
    """Return the first non-empty value (treats '' and None as empty)."""
    for v in values:
        if v is not None and str(v).strip() != "":
            return v
    return None


# Deterministic OCR safety net for the italic s/z confusion in this typeface.
# "sool" is never a real word here; it is always "zool" (zoology). Applied to the
# derived CSVs only -- scientists_raw.json keeps the model's original text.
_OCR_FIXES = [
    (re.compile(r"\bsool\b"), "zool"),
    (re.compile(r"\bSool\b"), "Zool"),
    (re.compile(r"\bsoolog"), "zoolog"),
    (re.compile(r"\bSoolog"), "Zoolog"),
]


def normalize_ocr(text):
    """Fix known, unambiguous OCR slips in a string field (returns input as-is
    for non-strings/None)."""
    if not isinstance(text, str) or not text:
        return text
    for pat, rep in _OCR_FIXES:
        text = pat.sub(rep, text)
    return text


def pipe_join(*chunks) -> Optional[str]:
    """Join multi-value text with ' | ' for spreadsheet friendliness.

    Semicolons are Excel's list separator in many locales (e.g. German), so a ';'
    inside a cell breaks Text-to-Columns. We therefore use '|' as the ONLY in-cell
    multi-value delimiter: each chunk is split on any existing ';', trimmed, and the
    pieces are re-joined with ' | '. Returns None if nothing remains."""
    parts: list[str] = []
    for chunk in chunks:
        if chunk is None:
            continue
        for piece in str(chunk).split(";"):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return " | ".join(parts) if parts else None


def _join_distinct(values) -> str:
    """Join non-empty, de-duplicated strings with ' | ' (preserves first order)."""
    seen: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v not in seen:
            seen.append(v)
    return " | ".join(seen)


def confirmed_years(start: Optional[int], end: Optional[int],
                    is_current: bool, directory_year: int = DIRECTORY_YEAR) -> list[int]:
    """Return the years for which a spell is CONFIRMED (never interpolated).

    - Explicit range (start & end): every year in [start, end].
    - Current 1927 position with only a start: [start .. directory_year] (the
      italic position is confirmed ongoing up to the edition year).
    - Non-current position with only a start: [start] alone -- we do NOT assume
      the person stayed until the next station.
    - Only an end year: [end] alone.
    """
    if start is not None and end is not None:
        if end < start:
            start, end = end, start
        return list(range(start, end + 1))
    if start is not None:
        return list(range(start, directory_year + 1)) if is_current else [start]
    if end is not None:
        return [end]
    return []


def _infer_country(state: Optional[str]) -> Optional[str]:
    """Fallback country inference from a US-state-looking token (used only when
    the model did not supply a country)."""
    if not state:
        return None
    s = state.strip().rstrip(".").lower()
    canada = {"que", "ont", "b.c", "bc", "man", "sask", "alta", "n.s", "n.b", "p.e.i"}
    if s in canada or "can" in s:
        return "Canada"
    # A short "X.Y" abbreviation or a US-state-ish word: assume USA.
    return "USA"


def profile_invariants(p: dict) -> dict:
    """Time-invariant attributes shared by every row of one scientist.

    Geography prefers the model's structured city/state/country fields and falls
    back to splitting the raw place/address strings when they are missing."""
    raw_name = re.sub(r"\s+", " ", (p.get("full_name") or "").strip())
    last_name, first_name = split_name(raw_name)
    name = clean_display_name(raw_name)

    fb_birth_city, fb_birth_state = split_place(p.get("birth_place") or "")
    birth_city = _first(p.get("birth_city"), fb_birth_city)
    birth_state = _first(p.get("birth_state"), fb_birth_state)
    birth_country = _first(p.get("birth_country"), _infer_country(birth_state))

    fb_mail_city, fb_mail_state = extract_mailing_place(p.get("mailing_address") or "")
    mail_city = _first(p.get("mailing_city"), fb_mail_city)
    mail_state = _first(p.get("mailing_state"), fb_mail_state)
    mail_country = _first(p.get("mailing_country"), _infer_country(mail_state))

    # Prefer the model's title; otherwise recover one left inside the name string.
    title = p.get("titles")
    if not title:
        m = _HONORIFIC_RE.match(raw_name.split(",", 1)[1] if "," in raw_name else raw_name)
        title = m.group(0).strip() if m else None

    # Single combined research field (accomplished + in-progress, no separation),
    # pipe-delimited so Excel Text-to-Columns never trips over an in-cell ';'.
    research = pipe_join(p.get("research_accomplished"), p.get("research_in_progress"))

    return {
        "last_name": last_name,
        "first_name": first_name,
        "scientist_name": name,
        "title": title,
        "star_status": 1 if p.get("star_status") else 0,
        "primary_department": (p.get("department") or "").strip().rstrip("."),
        "research": research,
        "birth_year": p.get("birth_year"),
        "birth_date": format_birth_date(p.get("birth_date"), p.get("birth_year")),
        "birth_city": birth_city,
        "birth_state": birth_state,
        "birth_country": birth_country,
        "mailing_city": mail_city,
        "mailing_state": mail_state,
        "mailing_country": mail_country,
        "source_pdf_page": p.get("_source_page"),
    }


_HONORIFIC_RE = re.compile(
    r"^\s*(Dr|Prof|Gen|Col|Maj|Capt|Lieut|Lt|Rev|Hon|Sir|Mr|Mrs|Miss|Ms|"
    r"Judge|Sen|Gov|Pres|Adm|Cmdr|Bp|Rt\.?\s*Rev)\.?\s+",
    re.IGNORECASE,
)


def strip_honorific(text: str) -> str:
    """Remove a leading honorific (Dr., Prof., ...) that the model sometimes
    leaves inside the name string despite instructions to keep it out."""
    prev = None
    s = text or ""
    # Strip repeatedly in case of stacked titles ("Prof. Dr. ...").
    while prev != s:
        prev = s
        s = _HONORIFIC_RE.sub("", s)
    return s.strip()


def split_name(full_name: str) -> tuple[str, str]:
    """Split "Last, First (Middle/Omitted)" into (last_name, first_name).

    The directory prints every entry surname-first, so the FIRST comma is the
    last/first boundary. Any remaining commas (rare) stay with the first name.
    A leading honorific in the first-name slot (e.g. "Dr.") is stripped, since it
    is captured separately in the title column. Names with no comma (mononyms)
    return ("", full_name)."""
    name = re.sub(r"\s+", " ", (full_name or "").strip())
    if "," in name:
        last, first = name.split(",", 1)
        return last.strip(), strip_honorific(first.strip())
    return "", strip_honorific(name)


def clean_display_name(full_name: str) -> str:
    """Rebuild a title-free 'Last, First' display name."""
    last, first = split_name(full_name)
    if last and first:
        return f"{last}, {first}"
    return first or last or (full_name or "").strip()


def load_profiles_from_checkpoint(checkpoint_path: Path) -> list[dict]:
    """Read every successful checkpoint line; keep the LATEST record per page."""
    by_page: dict[int, dict] = {}
    with checkpoint_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") == "ok":
                by_page[int(rec["focus_page_number"])] = rec

    profiles: list[dict] = []
    for page in sorted(by_page):
        for s in by_page[page].get("scientists", []):
            s["_source_page"] = page
            profiles.append(s)
    return profiles


def _dedupe_records(records: list[dict], keys: tuple) -> list[dict]:
    """Drop records that are identical on the given keys, preserving order."""
    seen = set()
    out = []
    for r in records:
        sig = tuple((r.get(k) or "").strip() if isinstance(r.get(k), str) else r.get(k)
                    for k in keys)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(r)
    return out


def build_panel(profiles: list[dict]) -> pd.DataFrame:
    """Build a BALANCED scientist-year panel with one row per scientist per year
    (birth year .. 1927 edition year).

    Activity is filled ONLY where the directory confirms it (a dated degree, a
    dated employment spell/range, or a dated parallel position); gaps between
    separate confirmed stations are left blank (never interpolated).

    When MORE THAN ONE position covers the same year -- because two spells overlap
    (e.g. a one-year "asst, Yale, 19" alongside "instr, Mass. Col, 19-22") or
    because a career transition happens mid-year (a spell ending in 1922 and the
    next beginning in 1922) -- each concurrent position is written to its OWN
    numbered column (position_1/institution_1, position_2/institution_2, ...)
    rather than being merged into a single cell. The number of slots is set to the
    maximum concurrency observed in the data, so nothing is lost and every column
    holds a single value (panel-ready). Degrees and parallel positions are slotted
    the same way. For primary positions, the italic current-1927 role is placed in
    slot 1 whenever it is active; otherwise slots are ordered by start year.
    """
    # -- Pass 1: accumulate per-scientist, per-year records; track max concurrency.
    parsed = []
    max_deg = max_prim = max_par = 1
    for p in profiles:
        inv = profile_invariants(p)
        birth_year = p.get("birth_year")
        events: dict[int, dict] = defaultdict(lambda: {"deg": [], "prim": [], "par": []})

        for deg in p.get("education") or []:
            y = deg.get("year")
            if y is None:
                continue
            events[y]["deg"].append({
                "role": normalize_ocr(deg.get("degree_type") or ""),
                "inst": normalize_ocr(deg.get("institution") or ""),
            })

        for job in p.get("employment") or []:
            is_cur = bool(job.get("is_current_position"))
            start = job.get("start_year")
            for y in confirmed_years(start, job.get("end_year"), is_cur):
                events[y]["prim"].append({
                    "role": normalize_ocr(job.get("position_title") or ""),
                    "inst": normalize_ocr(job.get("institution_org") or ""),
                    "is_cur": is_cur,
                    "start": start if start is not None else 0,
                })

        for mp in p.get("minor_positions") or []:
            if not isinstance(mp, dict):
                continue
            start = mp.get("start_year")
            for y in confirmed_years(start, mp.get("end_year"), False):
                events[y]["par"].append({
                    "role": normalize_ocr(mp.get("position_title") or ""),
                    "inst": normalize_ocr(mp.get("institution_org") or ""),
                    "start": start if start is not None else 0,
                })

        # De-duplicate within each year and order deterministically.
        for y, ev in events.items():
            ev["deg"] = _dedupe_records(ev["deg"], ("role", "inst"))
            ev["prim"] = sorted(
                _dedupe_records(ev["prim"], ("role", "inst")),
                key=lambda r: (0 if r["is_cur"] else 1, r["start"], r["inst"]),
            )
            ev["par"] = sorted(
                _dedupe_records(ev["par"], ("role", "inst")),
                key=lambda r: (r["start"], r["inst"], r["role"]),
            )
            max_deg = max(max_deg, len(ev["deg"]))
            max_prim = max(max_prim, len(ev["prim"]))
            max_par = max(max_par, len(ev["par"]))

        parsed.append((inv, birth_year, events))

    # -- Slot column names (fixed for the whole dataset).
    deg_cols = [(f"degree_earned_{i}", f"degree_institution_{i}") for i in range(1, max_deg + 1)]
    prim_cols = [(f"position_{i}", f"institution_{i}") for i in range(1, max_prim + 1)]
    par_cols = [(f"parallel_position_{i}", f"parallel_institution_{i}") for i in range(1, max_par + 1)]

    def blank_slots() -> dict:
        d = {}
        for rc, ic in deg_cols + prim_cols + par_cols:
            d[rc] = ""
            d[ic] = ""
        return d

    # -- Pass 2: emit balanced rows.
    rows = []
    for inv, birth_year, events in parsed:
        dated = [y for y in events if y is not None]
        if birth_year is not None:
            start_span = birth_year
        elif dated:
            start_span = min(dated)
        else:
            rows.append({**inv, "year": None, "age": None, **blank_slots(),
                         "is_current_1927_role": 0, "activity_confirmed": 0})
            continue
        end_span = max(DIRECTORY_YEAR, max(dated)) if dated else DIRECTORY_YEAR

        for y in range(start_span, end_span + 1):
            ev = events.get(y)
            row = {
                **inv,
                "year": y,
                "age": (y - birth_year) if birth_year is not None else None,
                **blank_slots(),
                "is_current_1927_role": 0,
                "activity_confirmed": 0,
            }
            if ev:
                for (rc, ic), rec in zip(deg_cols, ev["deg"]):
                    row[rc], row[ic] = rec["role"], rec["inst"]
                for (rc, ic), rec in zip(prim_cols, ev["prim"]):
                    row[rc], row[ic] = rec["role"], rec["inst"]
                    if rec["is_cur"]:
                        row["is_current_1927_role"] = 1
                for (rc, ic), rec in zip(par_cols, ev["par"]):
                    row[rc], row[ic] = rec["role"], rec["inst"]
                row["activity_confirmed"] = int(bool(ev["deg"] or ev["prim"] or ev["par"]))
            rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    col_order = (
        ["year", "first_name", "last_name", "scientist_name", "title",
         "age", "activity_confirmed"]
        + [c for pair in deg_cols for c in pair]
        + [c for pair in prim_cols for c in pair]
        + ["is_current_1927_role"]
        + [c for pair in par_cols for c in pair]
        + ["birth_year", "birth_date", "birth_city", "birth_state", "birth_country",
           "star_status", "primary_department",
           "mailing_city", "mailing_state", "mailing_country",
           "research", "source_pdf_page"]
    )
    df = df.reindex(columns=col_order)

    for col in ("birth_year", "year", "age"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in ("star_status", "activity_confirmed", "is_current_1927_role"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

    df = df.sort_values(
        ["last_name", "first_name", "year"],
        na_position="last", kind="stable",
    ).reset_index(drop=True)
    return df


def build_events_long(profiles: list[dict]) -> pd.DataFrame:
    """One row per DATED event (degree / employment spell / parallel position).

    This is the lossless audit table: it preserves explicit start_year/end_year
    ranges (not expanded), so nothing about spell duration is lost."""
    rows = []
    for p in profiles:
        inv = profile_invariants(p)

        for deg in p.get("education") or []:
            rows.append({
                **inv, "record_type": "Education",
                "start_year": deg.get("year"), "end_year": deg.get("year"),
                "institution_organization": normalize_ocr(deg.get("institution")),
                "role_or_degree": normalize_ocr(deg.get("degree_type")),
                "is_current_1927_role": 0,
            })
        for job in p.get("employment") or []:
            rows.append({
                **inv, "record_type": "Employment",
                "start_year": job.get("start_year"), "end_year": job.get("end_year"),
                "institution_organization": normalize_ocr(job.get("institution_org")),
                "role_or_degree": normalize_ocr(job.get("position_title")),
                "is_current_1927_role": 1 if job.get("is_current_position") else 0,
            })
        for mp in p.get("minor_positions") or []:
            if not isinstance(mp, dict):
                continue
            rows.append({
                **inv, "record_type": "MinorPosition",
                "start_year": mp.get("start_year"), "end_year": mp.get("end_year"),
                "institution_organization": normalize_ocr(mp.get("institution_org")),
                "role_or_degree": normalize_ocr(mp.get("position_title")),
                "is_current_1927_role": 0,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    col_order = [
        "first_name", "last_name", "scientist_name", "title",
        "record_type", "start_year", "end_year",
        "institution_organization", "role_or_degree", "is_current_1927_role",
        "birth_year", "birth_date", "birth_city", "birth_state", "birth_country",
        "star_status", "primary_department",
        "mailing_city", "mailing_state", "mailing_country", "source_pdf_page",
    ]
    df = df.reindex(columns=col_order)
    for col in ("birth_year", "start_year", "end_year"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in ("star_status", "is_current_1927_role"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    df = df.sort_values(
        ["last_name", "first_name", "start_year", "record_type"],
        na_position="last", kind="stable",
    ).reset_index(drop=True)
    return df


def build_summary(profiles: list[dict]) -> pd.DataFrame:
    """One row per scientist: invariants plus research subjects and societies
    (which are not year-specific), so those fields are preserved and queryable."""
    rows = []
    for p in profiles:
        inv = profile_invariants(p)
        societies = p.get("societies") or []
        rows.append({
            **inv,
            "n_degrees": len(p.get("education") or []),
            "n_positions": len(p.get("employment") or []),
            "n_parallel_positions": len(p.get("minor_positions") or []),
            "societies": pipe_join(*societies),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["birth_year"] = pd.to_numeric(df["birth_year"], errors="coerce").astype("Int64")
    df["star_status"] = df["star_status"].astype(int)
    df = df.sort_values(["last_name", "first_name"], kind="stable").reset_index(drop=True)
    return df


def run_panel_transformation(base_dir: Path) -> None:
    checkpoint_path = base_dir / CHECKPOINT_FILE
    if not checkpoint_path.exists():
        logger.error("No checkpoint file at %s -- run the extraction first.", checkpoint_path)
        sys.exit(1)

    profiles = load_profiles_from_checkpoint(checkpoint_path)
    logger.info("Loaded %d scientist profiles from the checkpoint.", len(profiles))
    if not profiles:
        logger.warning("Nothing to transform; skipping panel build.")
        return

    raw_json_path = base_dir / RAW_PROFILES_JSON
    raw_json_path.write_text(
        json.dumps(profiles, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Raw nested profiles archived to %s.", raw_json_path.name)

    # 1) Balanced scientist-year panel (main econometric deliverable).
    panel = build_panel(profiles)
    panel_path = base_dir / PANEL_CSV
    panel.to_csv(panel_path, index=False, encoding="utf-8-sig")
    n_scientists = panel["scientist_name"].nunique() if not panel.empty else 0
    n_starred = (panel.loc[panel["star_status"] == 1, "scientist_name"].nunique()
                 if not panel.empty else 0)
    n_confirmed = int(panel["activity_confirmed"].sum()) if not panel.empty else 0
    logger.info("Balanced panel written: %s (%d scientist-year rows, %d scientists, "
                "%d starred, %d rows with confirmed activity).",
                panel_path.name, len(panel), n_scientists, n_starred, n_confirmed)

    # 2) Lossless event-level long table (audit of spells/ranges).
    events = build_events_long(profiles)
    events_path = base_dir / EVENTS_CSV
    events.to_csv(events_path, index=False, encoding="utf-8-sig")
    logger.info("Event-level audit table written: %s (%d dated events).",
                events_path.name, len(events))

    # 3) One-row-per-scientist summary (research subjects, societies, counts).
    summary = build_summary(profiles)
    summary_path = base_dir / SUMMARY_CSV
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    logger.info("Scientist summary written: %s (%d scientists).",
                summary_path.name, len(summary))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract 'American Men of Science' (1927) biographies into a "
                    "long-format panel CSV via the Anthropic Claude vision API.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pdf", default=DEFAULT_PDF,
                        help="Path to the directory PDF.")
    parser.add_argument("--pages", nargs=2, type=int, metavar=("START", "END"),
                        default=[DEFAULT_START_PAGE, DEFAULT_END_PAGE],
                        help="Inclusive 1-based PDF page range of FOCUS pages.")
    parser.add_argument("--test-run", action="store_true",
                        help="Process only the first two focus pages of the range "
                             "to verify the pipeline end-to-end.")
    parser.add_argument("--api-key", default=None,
                        help="Anthropic API key (defaults to $ANTHROPIC_API_KEY).")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Anthropic model ID.")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help="Rendering resolution for page images.")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help="Max output tokens per API call.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render sample page images without calling the API.")
    parser.add_argument("--panel-only", action="store_true",
                        help="Skip extraction; rebuild the panel CSV from the "
                             "existing checkpoint.")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore and delete the existing checkpoint (start over).")
    return parser


def configure_logging(base_dir: Path) -> None:
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                            datefmt="%H:%M:%S")
    logger.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    file_handler = logging.FileHandler(base_dir / "pipeline.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)


def main() -> None:
    args = build_arg_parser().parse_args()
    base_dir = Path(__file__).resolve().parent
    configure_logging(base_dir)

    logger.info("=" * 70)
    logger.info("American Men of Science (1927) extraction pipeline starting.")

    if args.fresh:
        for name in (CHECKPOINT_FILE, FAILED_PAGES_LOG):
            path = base_dir / name
            if path.exists():
                path.unlink()
                logger.info("--fresh: removed %s.", name)

    if not args.panel_only:
        run_extraction(args, base_dir)

    if not args.dry_run:
        run_panel_transformation(base_dir)

    logger.info("Pipeline complete.")


if __name__ == "__main__":
    main()
