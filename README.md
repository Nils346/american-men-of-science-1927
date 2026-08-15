# American Men of Science (1927) — Extraction Pipeline

Turns the ~13,500 biographical entries of the *American Men of Science* 4th
Edition (1927) into an analysis-ready **scientist-year panel**, using the
OpenAI vision API on page images, cross-checked against the PDF's own OCR text
layer, with an Excel workbook for hand verification.

**Source material:** [EDITIONS.md](EDITIONS.md) (PDF links) ·
[DIRECTORY_REFERENCE.md](DIRECTORY_REFERENCE.md) (field notes for the 1927 volume)

## Quickstart

```powershell
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."
```

Place the PDF in the project folder as
`American Men of Science_4th edition_1927.pdf` (not committed — see
[HathiTrust](https://babel.hathitrust.org/cgi/pt?id=mdp.39015039431948&seq=7)).

**Real runs go through the Batch API (half price, results within 24 h):**

```powershell
python batch_extract.py submit --pages 14 1123   # queue the whole listing
python batch_extract.py status                   # check progress any time
python batch_extract.py harvest --wait           # collect into the checkpoint
python extract_panel.py --panel-only             # build CSVs + workbook
python qa_check.py                               # free quality report
```

Live (non-batch) extraction with the same behaviour:
`python extract_panel.py --pages 14 1123`. Both modes write the same
resumable `extraction_checkpoint.jsonl` — interrupting and re-running is
always safe, completed pages are skipped.

## Outputs

| File | Contents |
| --- | --- |
| `review_workbook.xlsx` | **Start here for verification.** One formatted row per scientist, columns in print order, frozen header/name panes, rows tinted red/amber by QA severity, all QA flags attached. Second sheet: raw QA findings. |
| `scientist_mobility_panel.csv` | **Main output.** Balanced panel, one row per scientist per year (birth–1927). Activity filled only where the directory confirms it — never interpolated. Concurrent positions get numbered slot columns, never a shared cell. |
| `scientist_events_long.csv` | Lossless audit table: one row per dated degree / employment spell / minor position, ranges preserved. |
| `scientist_summary.csv` | One row per scientist: invariants, societies, research subjects, counts. |
| `scientists_raw.json` | Full nested model output; all CSVs rebuild from it via `--panel-only`. |

Multi-value cells use ` | ` as separator (never `;`, which breaks Excel in
several locales).

### Institution locations (evidence-based, then hand-verified)

Birth places and 1927 mailing addresses come geo-parsed from the book itself.
Career institutions are located via a bridge file:

```powershell
python merge_institution_locations.py --export   # build/refresh the bridge
#  -> institution_locations.csv, sorted by event count, PREFILLED from the
#     book's own mailing addresses (an address naming the institution locates
#     it; majority vote across scientists; provenance in the notes column)
#  verify/extend in Excel, blank anything doubtful, then:
python merge_institution_locations.py --apply    # locations onto the events table
```

Prefills use no outside knowledge — only printed addresses — and the export
never overwrites hand-coded cells. On the sample, address evidence alone
located the institutions behind a third of all career events.

### Publishable release

```powershell
python build_release.py
```

Writes `release/`: four clean CSVs (scientist-year panel, dated career events
with locations, one-row-per-scientist file, institution bridge) plus
`codebook.xlsx` documenting every column, the construction method, and
coverage counts. No QA flags or working files — this is the version to share.

## How it works

1. **Focus + look-ahead:** each request carries page N and N+1 as images;
   only entries *beginning* on N are extracted, completed from the look-ahead
   if they spill over.
2. **Roster first:** the model must list every entry's full bold heading
   before transcribing details. Full headings are unique within a page, which
   stops it collapsing runs of repeated surnames (the dominant omission mode).
3. **Spelling cross-check:** the prompt includes the entry headings as the
   PDF's OCR layer reads them. The vision model normalises rare surnames
   toward familiar ones (`Bear`→`Bean`); the OCR never does. The list is
   authoritative for spelling only — the image decides what exists.
4. **Strict schema:** replies are constrained by a JSON Schema (OpenAI
   Structured Outputs) and re-validated with Pydantic.
5. **Subtractive repair:** birth data that leaves no trace in the entry's own
   printed text is nulled at panel build (the model occasionally invents a
   birthday from a degree year or from its knowledge of a famous scientist).
   Repair only removes values, never writes them; disputed months are kept
   and flagged instead.
6. **Checkpoint everything:** per-page results append to
   `extraction_checkpoint.jsonl` (fsync'd); failures land in
   `failed_pages.log` + `debug/` and the run continues.

## Quality control

`qa_check.py` makes no API calls and exists because the worst failure mode is
silent: a dropped entry or mangled name still reports "success". Its strongest
tool is the PDF's own OCR text layer — too fragmented to extract data from,
but an *independent* reading of the same page, so disagreement is a reliable
review trigger. On the 10-page sample it caught every error hand-checking
found, plus several hand-checking missed.

Checks fall into four groups:

- **Completeness** — entries the page prints but the model never returned;
  overlaps and duplicates across pages; pages with implausibly few entries.
- **Fidelity** — surnames that disagree with the print; star counts that
  disagree with the page's printed asterisks; birth dates/months without a
  printed trace.
- **Plausibility** — degrees or jobs before age 15 (a misread birth year in
  practice), years outside 1800–1927, spells that end before they start.
- **Structure** — duplicate degree types (often a study spell dressed up as a
  degree), degrees with no institution and no year, degree letters fused into
  an institution name, missing current position.

Errors demand action; warnings are review pointers (several flag genuinely
unusual print, like two honorary Sc.D.s). The script exits non-zero and lists
pages worth re-extracting.

## Accuracy, measured on the 10-page hand-checked sample

- **127/127 entries returned, zero missed, zero misread surnames** after the
  roster + spelling-cross-check fixes (earlier: ~3% silently dropped, 5
  corrupted surnames). Model nondeterminism means a rare drop can still
  happen — `qa_check.py` catches it, so re-extract flagged pages before
  treating the panel as final.
- **Invented birth data is the one behaviour prompting cannot fully stop**
  (2–3 entries per 10 pages). The subtractive repair strips every occurrence;
  affected rows keep an amber flag in the workbook.
- Residual field-level slips (~1–2 per 10 pages) concentrate where the
  two-column print itself is scrambled; they surface as warnings, not
  silently.
- `--max-passes 2` (union-merging repeat passes) is available but **off**: it
  fixed one page and broke another by letting a mangled twin survive as a
  phantom scientist.

## Model & cost

Default **`gpt-5.6-sol`** ($5/$30 per M tokens), chosen over `gpt-5.6-terra`
on a measured A/B: same entry counts, but Sol misread less than half as many
surnames (7 vs 19 on the three hardest pages). Surnames are the linking key,
so the ~$50 premium buys the right thing. `--reasoning-effort low` — raising
it doubled output tokens without improving accuracy.

Measured on the full 10-page sample (**$0.099/page through the Batch API**):

| | Batch API | Live |
| --- | ---: | ---: |
| `gpt-5.6-sol` (default), 1,110 pages | **~$110** | ~$220 |
| `gpt-5.6-terra`, 1,110 pages | ~$60 | ~$120 |
| Recommended budget (Sol batch +15% buffer) | **~$127** | |

A live sequential run takes ~14 h (~45 s/page); batch needs no babysitting.
Per-page token usage is logged in the checkpoint under `usage`.

## Key CLI options (`extract_panel.py`)

| Flag | Default | Purpose |
| --- | --- | --- |
| `--pages START END` | 14 1123 | Inclusive 1-based PDF page range |
| `--test-run` | off | First 2 focus pages of the range only |
| `--dry-run` | off | Render images, no API calls |
| `--panel-only` | off | Rebuild CSVs/workbook from checkpoint, no API calls |
| `--fresh` | off | Delete checkpoint and start over |
| `--model` | `gpt-5.6-sol` | Any vision-capable OpenAI model |
| `--reasoning-effort` | `low` | Billed as output tokens |
| `--dpi` | 150 | Page image resolution |
| `--max-passes` | 1 | >1 unions repeat passes on short pages (see above) |
| `--api-key` | `$OPENAI_API_KEY` | OpenAI API key |
