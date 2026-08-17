# American Men of Science (1927) → Scientist-Year Panel

**For a new agent / new machine:** read [HANDOFF.md](HANDOFF.md) first.
It is the project memory (decisions, full-run stats, QA, what to copy).
Do not re-extract the book.

A complete transcription of the *American Men of Science* 4th edition (1927)
into a scientist-year panel for mobility and migration research. Nothing is
interpolated or guessed; unverifiable values are removed, not invented.

## The dataset

**Current build** (Batch API, 15 Aug 2026): 1,110 pages (PDF 14–1123),
**13,003 scientists**, 1,405 starred, **606,903** scientist-year rows,
**109,652** dated events. Extraction cost **$103.84**.

Institution locations cover **79% of career events** (3,565 of 17,840 unique
strings). Each located row is tagged:

| `source` | Meaning |
| --- | --- |
| **mailing address** | Taken from a scientist's own printed 1927 address that names this institution |
| **AI** | Classify-then-locate draft — not yet hand-verified |
| **hand** | Typed in by a person |

Ambiguous tours (`Berlin and Vienna`) and non-places (societies, journals)
stay blank on purpose.

Built into `release/` by `python build_release.py`. Every table is in **book
order** (PDF page, then surname, then year). Excel files freeze Page +
Scientist so scrolling a career feels like turning the page.

| File | What it is |
| --- | --- |
| **`scientists_1927.xlsx` / `.csv`** | One row per scientist. Open this first. |
| **`scientist_year_panel_1927.xlsx` / `.csv`** | **Main analysis file.** One row per scientist per year (birth–1927). Activity only where the book confirms it. |
| **`career_events_1927.xlsx` / `.csv`** | One row per dated degree or position, with printed year spans and locations. |
| **`institution_locations_1927.xlsx` / `.csv`** | Institution → city/state/country, with `source`. Join on the institution string. |
| **`codebook.xlsx`** | Overview, counts, and a dictionary of every column. |

CSVs are for Stata / R / Python. Excel is the copy you hand someone to browse.
The working bridge (`institution_locations.csv`, tracked in git) is where
further location work accumulates. Rows whose `notes` start with `auto:` are
mailing-address proposals; `ai:` are model drafts.

## Replicate from scratch

The book is already extracted. Rebuild the panel and the release for free
from `extraction_checkpoint.jsonl`. Only run steps 1–2 if you truly need a
new transcription (costs ~$104 and a live OpenAI key).

```powershell
# 0. Setup
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."          # extraction only
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # optional location --propose only
#    put the PDF here as "American Men of Science_4th edition_1927.pdf"
#    (https://babel.hathitrust.org/cgi/pt?id=mdp.39015039431948)

# 1. Extract everything through the Batch API (half price, ~$110, <24 h)
python batch_extract.py submit --pages 14 1123
python batch_extract.py harvest --wait

# 2. Build the working CSVs + hand-review workbook
python extract_panel.py --panel-only

# 3. Quality report (free, no API calls); re-extract any pages it lists
python qa_check.py

# 4. Institution locations
python merge_institution_locations.py --export     # prefill from mailing addresses
python merge_institution_locations.py --propose    # optional AI draft of leftovers
python merge_institution_locations.py --apply      # merge onto events + tag source

# 5. Spot-check red/amber rows in review_workbook.xlsx

# 6. Publishable CSVs + formatted Excel
python build_release.py
```

Every extraction step is idempotent: results append to
`extraction_checkpoint.jsonl`, completed pages are skipped, and all CSVs
rebuild from the checkpoint (`--panel-only`) at no cost.

## Repository map

| File | Purpose |
| --- | --- |
| `extract_panel.py` | Prompt + schema, page rendering, live extraction, panel build |
| `batch_extract.py` | Same extraction through OpenAI's Batch API (submit / status / harvest) |
| `qa_check.py` | Offline quality checks against the PDF's OCR text layer; birth-data repair |
| `profile_merge.py` | Merging of repeated extraction passes over one page (default unused) |
| `review_workbook.py` | Hand-verification Excel with QA flags |
| `merge_institution_locations.py` | Institution→location bridge: export, address prefill, AI propose, apply |
| `build_release.py` | Publishable CSVs + formatted Excel + codebook |
| `EDITIONS.md` / `DIRECTORY_REFERENCE.md` | Source PDFs; 1927 volume structure and field notes |

## How it works

1. **Focus + look-ahead pages.** Each request sends page N and N+1 as images;
   only entries beginning on N are extracted, completed from N+1 if they
   spill over.
2. **Roster before transcription.** The model must first list every entry's
   full bold heading — unique within a page — which stops it collapsing runs
   of repeated surnames (the dominant silent-omission mode).
3. **OCR spelling cross-check.** The prompt carries the entry headings as the
   PDF's own text layer reads them. The vision model normalises rare surnames
   toward familiar ones; the OCR never does. The list is authoritative for
   spelling only — the image decides what exists.
4. **Strict schema.** Responses are constrained by a JSON Schema (Structured
   Outputs) and re-validated with Pydantic.
5. **Subtractive repair.** Birth data with no trace in the entry's own printed
   text is nulled at panel build. Repair removes, never writes; OCR/model
   disputes are kept and flagged for eyeballing.
6. **Evidence-based locations.** Birthplaces and mailing addresses are parsed
   from the entries themselves. Career institutions are located only by
   (a) a printed mailing address that names the institution, (b) a tagged AI
   draft after a classify-then-locate check, or (c) hand-coding. Nothing is
   guessed from the modern web, and dual-city strings stay blank.

## Quality control

`qa_check.py` exists because the worst failure mode is silent: a dropped entry
or mangled name still reports success. It cross-reads every page's OCR text
layer — too fragmented to extract from, but an independent reading of the same
print — and checks four things: **completeness** (entries printed but never
returned; duplicates), **fidelity** (surname spellings, star counts, birth
dates against the print), **plausibility** (degrees before age 15, impossible
year spans), and **structure** (duplicate degrees, fused degree/institution
strings, missing current position). Errors demand re-extraction; warnings are
review pointers surfaced in `review_workbook.xlsx` as red/amber rows.

On the final hand-checked 10-page sample (127 entries): zero missed entries,
zero misread surnames, zero unflagged errors. Residual model slips (~1–2
fields per 10 pages, concentrated where the two-column print is scrambled)
all surface as flags.

## Model & cost

Default `gpt-5.6-sol`, chosen over the cheaper `gpt-5.6-terra` on a measured
A/B (half the surname misreads; surnames are the linking key). Reasoning
effort `low` — higher effort doubled tokens with no accuracy gain. Measured
$0.094/page through the Batch API on the full run:

| Full run (1,110 pages) | Cost |
| --- | ---: |
| `gpt-5.6-sol`, Batch API — **actual** | **$103.84** |
| `gpt-5.6-sol`, live calls (would have been) | $207.68 |

Location `--propose` used Claude Sonnet (Anthropic) on leftover strings after
the mailing-address prefill. That step is optional and separate from extraction.

## CLI reference (`extract_panel.py`)

| Flag | Default | Purpose |
| --- | --- | --- |
| `--pages START END` | 14 1123 | Inclusive 1-based PDF page range |
| `--panel-only` | off | Rebuild CSVs/workbook from checkpoint, no API calls |
| `--test-run` / `--dry-run` | off | First 2 pages only / render without API calls |
| `--fresh` | off | Delete checkpoint and start over |
| `--model` / `--reasoning-effort` / `--dpi` | sol / low / 150 | Extraction knobs |
| `--max-passes` | 1 | >1 unions repeat passes on short pages (can over-collect) |
| `--api-key` | `$OPENAI_API_KEY` | OpenAI API key |
