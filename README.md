# American Men of Science (1927) — Extraction Pipeline

`extract_panel.py` extracts the ~13,500 biographical entries of the 4th Edition
(1927) directory into a long-format Scientist-Year-Event panel
(`scientist_mobility_panel.csv`) using the OpenAI vision API.

**Source material:** [EDITIONS.md](EDITIONS.md) (PDF links) ·
[DIRECTORY_REFERENCE.md](DIRECTORY_REFERENCE.md) (field notes for the 1927 volume)

## Setup

```powershell
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."          # or pass --api-key
```

Place the PDF in the project folder as
`American Men of Science_4th edition_1927.pdf` (not committed to git — see
[HathiTrust](https://babel.hathitrust.org/cgi/pt?id=mdp.39015039431948&seq=7)).

## Usage

```powershell
# 1. Free dry run: renders page images, no API calls
python extract_panel.py --pages 15 16 --dry-run

# 2. Test run: extracts focus pages 15 and 16 only, then builds the CSV
python extract_panel.py --test-run --pages 15 16

# 3. Full run over the whole listing (PDF pages 14–1123)
python extract_panel.py --pages 14 1123

# Rebuild the CSV from the checkpoint without any API calls
python extract_panel.py --panel-only

# Start over from scratch (deletes checkpoint + failed-pages log)
python extract_panel.py --fresh --pages 14 1123
```

## How it works

- **Focus + Look-Ahead windows:** each API call sends images of page N (focus)
  and page N+1 (look-ahead). Only entries that *begin* on the focus page are
  extracted; entries spilling onto N+1 are completed from the look-ahead image,
  and entries continued from N−1 are ignored (already captured).
- **Validation:** the request pins a strict JSON Schema via OpenAI Structured
  Outputs, and the reply is re-parsed against the matching Pydantic contract
  (`PageExtractionContainer` → `ScientistProfile` → degree/employment records).
- **Fault tolerance:** exponential-backoff retries (up to 5) on 429/5xx/network
  errors via `tenacity`; truncated or unparseable responses are logged to
  `failed_pages.log` with raw output saved under `debug/`, and the run continues.
  A rejected key fails fast — the model is checked once before any page is sent,
  and an auth error mid-run aborts instead of burning through the remaining pages.
- **Checkpointing & resume:** each successful page window is appended to
  `extraction_checkpoint.jsonl` (fsync'd). Re-running the same command skips
  completed pages automatically and retries failed ones.
- **Panel build:** after extraction, nested records are flattened so each row is
  one Scientist-Year-Event (Education or Employment) observation, written to
  `scientist_mobility_panel.csv`. Raw nested profiles are archived to
  `scientists_raw.json`.

## Outputs

The pipeline writes three CSVs plus a raw JSON archive:

### 1. `scientist_mobility_panel.csv` — balanced scientist-year panel (main)

One row **per scientist per calendar year**, from the scientist's birth year
through 1927 (the edition year). Activity columns are filled **only where the
directory confirms activity** — a dated degree, a dated employment spell/range,
or a dated parallel position. Gaps between separate confirmed stations are left
blank (no interpolation). A confirmed employment **range** (e.g. `14-17`) fills
every year in that range; the italicized **current** position fills from its
start year through 1927; a single-year station fills only that one year.

Columns (in order): `year`, `first_name`, `last_name`, `scientist_name`,
`title`, `age`, `activity_confirmed` (0/1), then **numbered slot columns**
`degree_earned_1`/`degree_institution_1` (…_2, …), `position_1`/`institution_1`
(…_2, …), `is_current_1927_role` (0/1), `parallel_position_1`/
`parallel_institution_1` (…_2, …), then `birth_year`, `birth_date` (DD.MM.YYYY),
`birth_city`, `birth_state`, `birth_country`, `star_status` (0/1),
`primary_department`, `mailing_city`, `mailing_state`, `mailing_country`,
`research` (accomplished + in-progress subjects combined, `|`-separated),
`source_pdf_page`.

`research` is blank only when the directory entry itself lists no research
subjects (some entries have none). Society memberships live in
`scientist_summary.csv`, not in the panel.

Multi-value cells (`research`, and `societies` in the summary file) use a pipe
` | ` as the in-cell separator, **not** a semicolon. Semicolon is Excel's list
separator in several locales (e.g. German), so an in-cell `;` breaks
Text-to-Columns; the pipe never does. To split research subjects into their own
columns in Excel: select the column, Data → Text to Columns → Delimited →
Other = `|`.

OCR note: the italic typeface confuses s/z, so `zool` was frequently misread as
`sool`. A deterministic normalizer rewrites `sool`->`zool` in the derived CSVs
(the raw model output in `scientists_raw.json` is left untouched). Bare
carried-forward position words are reconstructed per the directory's own
convention, e.g. `biol` following `Instr. zool` becomes `Instr. biol`.

**Concurrent positions never share a cell.** When more than one position covers
the same year — overlapping spells, or a mid-year career transition — each one is
written to its own numbered slot (`position_1`, `position_2`, …). The number of
slots equals the maximum concurrency seen in the data, so every cell holds a
single value (panel-ready). For primary positions the italic current-1927 role is
placed in `position_1` whenever it is active; other years are ordered by start
year. Parallel/temporary roles (fellowships, military, committee/editorial, summer
posts) use the separate `parallel_position_*` slots so they never collide with the
main career track.

### 2. `scientist_events_long.csv` — lossless event audit

One row per **dated event** (Education / Employment / MinorPosition) with the
explicit `start_year` and `end_year` preserved (not expanded). Use this to
verify spell durations or rebuild the panel differently.

### 3. `scientist_summary.csv` — one row per scientist

Time-invariant attributes plus `societies`, `research_accomplished`,
`research_in_progress`, and counts (`n_degrees`, `n_positions`,
`n_parallel_positions`).

### 4. `scientists_raw.json`

The full nested LLM output, the source of truth from which all three CSVs are
derived (rebuild any time with `--panel-only`).

## Institution geography (hand-coded, post-extraction)

The directory usually prints institution names only (`Brown`, `Yale`) — not countries.
To study migration without guessing, code locations yourself:

```powershell
# 1. After extraction, export every unique institution string:
python merge_institution_locations.py --export
#    (creates institution_locations.csv from institution_locations.example.csv
#     if the file does not exist yet)

# 2. Open institution_locations.csv in Excel; fill city / state_region / country
#    only where you are confident. Leave blank when unsure.

# 3. Merge back into the event audit table:
python merge_institution_locations.py --apply
```

This adds `institution_city`, `institution_state_region`, and `institution_country`
to `scientist_events_long.csv` for exact institution-string matches only.

## Model choice

The default is **`gpt-5.6-terra`**, the closest OpenAI counterpart to the
Sonnet-tier model this pipeline originally used — both on price
($2.50 / $15 per M input/output vs Sonnet 5's $3 / $15) and on positioning
(OpenAI describes Terra as "everyday production work at near-Sol quality").
`gpt-5.6-sol` doubles the input price for frontier reasoning that transcription
does not need; `gpt-5.6-luna` is the cheap tier worth testing if the budget
tightens. Any vision-capable OpenAI model works via `--model`.

Reasoning tokens are billed as output, so `--reasoning-effort` defaults to
`low`. Raising it did **not** measurably improve boundary accuracy in testing
(see below) but did roughly double the output tokens.

## API budget estimate (full listing, pages 14–1123)

Based on **10 measured pages** (75–84) with `gpt-5.6-terra` at 150 DPI,
reasoning effort `low`:

| Metric | Value |
| --- | --- |
| Focus pages | 1,110 |
| Avg input tokens / page | ~7,240 (~3,000 served from cache) |
| Avg output tokens / page | ~5,500 (~1,000 of them reasoning) |
| Scientists / page | ~12.3 |
| Wall-clock / page | ~45 s |

| Pricing tier | Estimated total |
| --- | ---: |
| **Standard** ($2.50 / $15 per M, incl. cached-input discount) | **~$104** |
| Batch API (50% off) | ~$52 |
| Recommended budget request (+10% buffer / retries) | **~$115** |

At ~45 s per page a sequential full run takes roughly **14 hours**. Run it
overnight, or use the Batch API, which halves the price at the cost of latency.
The checkpoint makes the run fully resumable, so interrupting it is safe.

Re-run cost check after more pages: token totals are logged per page in
`pipeline.log` and stored in `extraction_checkpoint.jsonl` under `usage`.

## Known accuracy limit: page-boundary drift

Repeated extractions of the *same* page do not always return the same set of
entries. On sample page 84 (12 entries by hand count) five runs returned 10, 11,
11, 12 and 12 entries. The failure modes are dropped entries at the top of the
focus page and occasional leakage of an entry from the look-ahead page. Effort
levels `low`, `medium` and `high` all showed the drift, so it is not fixed by
spending more on reasoning.

This matters because it is **silent** — nothing lands in `failed_pages.log`.
The cheap detector is alphabetical continuity: the directory is strictly
alphabetical, so the first entry of page N should follow the last entry of page
N−1 with no gap and no overlap. Nine of the ten sample pages chained perfectly;
the one bad page showed up immediately as a gap. Worth running as a QA pass over
the full extraction before treating the panel as final.

## Key CLI options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--pages START END` | 14 1123 | Inclusive 1-based PDF page range of focus pages |
| `--test-run` | off | Restrict to the first 2 focus pages of the range |
| `--model` | `gpt-5.6-terra` | OpenAI model ID |
| `--reasoning-effort` | `low` | `none`/`low`/`medium`/`high`/`xhigh`; billed as output tokens |
| `--dpi` | 150 | Page image resolution |
| `--max-tokens` | 32000 | Output token cap per call (raise if pages truncate) |
| `--api-key` | `$OPENAI_API_KEY` | OpenAI API key |
