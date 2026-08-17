# American Men of Science (1927) → Scientist-Year Panel

**For a new agent / new machine:** read [HANDOFF.md](HANDOFF.md) first.
It is the project memory (decisions, full-run stats, QA, what to copy).
Do not re-extract the book.

Extracts all ~13,500 biographical entries of the *American Men of Science*
4th edition (1927) from page scans into a clean scientist-year panel for
mobility/migration research — vision-LLM transcription, cross-validated
against the volume's own OCR text layer, with locations sourced from the
book's printed addresses and hand verification. Nothing is interpolated or
guessed; unverifiable values are removed and flagged, not invented.

## The dataset

**Current build** (Batch API, 15 Aug 2026): 1,110 pages, **13,003 scientists**,
1,405 starred, 606,903 scientist-year rows, 109,652 dated events. Cost
**$103.84**. Institution locations: 1,280 of 17,840 unique strings prefilled
from printed mailing addresses, covering **59% of career events** — still
needs a human pass before treating geography as final.

Built into `release/` (by step 6 below), documented column-by-column in
`release/codebook.xlsx`:

| File | What it is |
| --- | --- |
| **`scientist_year_panel_1927.csv`** | **The main output.** One row per scientist per year (birth–1927). Degrees, positions, and parallel roles per year in numbered slots; activity filled only where the book confirms it. |
| **`institution_locations_1927.csv`** | **The location bridge.** Institution string → city/state/country. Prefilled from the book's own mailing addresses; join it to any institution column. Verify in `institution_locations.csv` before publishing. |
| `career_events_1927.csv` | One row per dated degree/position with its printed year span and location. |
| `scientists_1927.csv` | One row per scientist: identity, birthplace, field, star status, societies, research. |

The working copy of the bridge (`institution_locations.csv`, tracked in git)
is the one file that accumulates irreplaceable hand work. Rows whose `notes`
start with `auto:` are mailing-address proposals, not yet verified.

## Replicate from scratch

```powershell
# 0. Setup
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."
#    put the PDF here as "American Men of Science_4th edition_1927.pdf"
#    (https://babel.hathitrust.org/cgi/pt?id=mdp.39015039431948)

# 1. Extract everything through the Batch API (half price, ~$110, <24 h)
python batch_extract.py submit --pages 14 1123
python batch_extract.py harvest --wait

# 2. Build the panel CSVs + hand-review workbook
python extract_panel.py --panel-only

# 3. Quality report (free, no API calls); re-extract any pages it lists
python qa_check.py

# 4. Institution locations: prefill from mailing addresses, verify in Excel,
#    optionally hand-code more of the frequency-sorted list, then apply
python merge_institution_locations.py --export
python merge_institution_locations.py --propose   # optional: AI-draft frequent leftovers
python merge_institution_locations.py --apply

# 5. Spot-check red/amber rows in review_workbook.xlsx (frozen panes, QA flags)

# 6. Assemble the publishable dataset
python build_release.py
```

Every step is idempotent and resumable: extraction results append to
`extraction_checkpoint.jsonl`, completed pages are skipped on re-run, and all
CSVs rebuild from the checkpoint at any time (`--panel-only`).

## Repository map

| File | Purpose |
| --- | --- |
| `extract_panel.py` | Core pipeline: prompt + schema, page rendering, live extraction, panel build |
| `batch_extract.py` | Same extraction through OpenAI's Batch API (submit / status / harvest) |
| `qa_check.py` | Offline quality checks against the PDF's OCR text layer; birth-data repair |
| `profile_merge.py` | Merging of repeated extraction passes over one page |
| `review_workbook.py` | Hand-verification Excel workbook with QA flags |
| `merge_institution_locations.py` | Institution→location bridge: export, address prefill, apply |
| `build_release.py` | Clean publishable CSVs + codebook |
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
   text is nulled at panel build (the model occasionally invents a birthday
   from a degree year or its background knowledge). Repair removes, never
   writes; OCR/model disputes are kept and flagged for eyeballing.
6. **Evidence-based locations.** Birthplaces and mailing addresses are parsed
   from the entries themselves. Career institutions are located only by (a) a
   printed mailing address that names the institution (majority vote across
   scientists, abbreviation-aware matching, residence never counts) or (b)
   hand-coding in the frequency-sorted bridge file.

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
