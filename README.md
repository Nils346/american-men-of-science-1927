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
`gpt-5.6-luna` is the cheap tier worth testing if the budget tightens. Any
vision-capable OpenAI model works via `--model`.

Reasoning tokens are billed as output, so `--reasoning-effort` defaults to
`low`. Raising it did **not** measurably improve boundary accuracy in testing
(see below) but did roughly double the output tokens.

### Terra vs Sol, measured

Ten calls across the three hardest pages (79, 82, 83 — the ones with the longest
runs of repeated surnames), full-name roster prompt, effort `low`:

| | `gpt-5.6-terra` | `gpt-5.6-sol` |
| --- | --- | --- |
| Entries returned (truth 68) | 66 | 65 |
| Surnames misread | 19 | 7 |
| Runs with a phantom entry | 1 of 5 | 0 of 5 |
| Cost per page | $0.108 | $0.175 |
| Full run, 1110 pages | ~$120 | ~$194 |

Both models now find the same number of entries; the roster fix, not the model,
is what closed the counting gap. Sol's advantage is **transcription fidelity** —
it misread less than half as many surnames, and on page 79 it returned all
thirteen spellings correctly where Terra produced `Beare` for `Bear`/`Bearce`
and `Bean` for `Beans`. Terra also has a systematic misread of this typeface,
turning `Behre` into `Behro` on every attempt.

Whether the extra ~$72 is worth it depends on the downstream use. Surnames are
the linking key to other sources, so a misread is not a cosmetic problem — but
`qa_check.py` catches most of them against the text layer either way.

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

## Quality control (`qa_check.py`)

```powershell
python qa_check.py                  # everything, writes qa_findings.csv
python qa_check.py --severity error # only near-certain problems
python qa_check.py --pages 75 84    # one range
```

Makes **no API calls**, so run it as often as you like. It exists because the
pipeline's worst failure mode is silent: the model sometimes drops an entry or
mangles a surname and still reports success, so `failed_pages.log` stays empty.

The strongest check exploits the fact that the PDF carries its **own OCR text
layer**. That layer is far too fragmented to extract structured data from, but it
is an *independent* reading of the same page, so disagreement is a reliable
review trigger. Concretely, on the 10-page sample it caught every error that
hand-checking had found, plus three that hand-checking had missed:

| Finding | Meaning |
| --- | --- |
| `entry_possibly_missed` | The page prints an entry the model never returned |
| `surname_misread` | Model spelling vs printed spelling disagree (`Boer` / `Beer`) |
| `surname_not_printed` | Returned a surname the page never prints |
| `milestone_before_adulthood` | A degree or post dated before age 15 — in practice a misread birth year |
| `page_overlap`, `duplicate_scientist` | The same entry captured on two pages |

Candidate surnames are filtered to the alphabetical window the neighbouring pages
define, which discards the OCR debris (glued running headers, hyphenated line
breaks) that would otherwise dominate the output. The script exits non-zero when
any page looks worth re-extracting and prints that page list.

## Known accuracy limit: page-boundary drift

Repeated extractions of the *same* page do not always return the same set of
entries. On sample page 84 (12 entries by hand count) five runs returned 10, 11,
11, 12 and 12 entries. The failure modes are dropped entries at the top of the
focus page and occasional leakage of an entry from the look-ahead page. Effort
levels `low`, `medium` and `high` all showed the drift, so it is not fixed by
spending more on reasoning.

Omissions are not confined to page edges. Sample page 83 dropped an entry from
the *middle* of a column, and the 10-page sample contained **4 missed entries
across 125 profiles (~3%)** plus **5 corrupted surnames**.

The drops are not uniformly random: they concentrate on **runs of repeated
surnames**. A page printing Beckwith five times, Behre three times or Bean four
times is where entries go missing — the model collapses the run and loses count.
Pages of distinct surnames extract cleanly.

**Fix: roster by full name before transcribing.** The model now has to list the
*complete bold heading* of every entry beginning on the page —
`Behre, Dr. J(eanette) A(llen)` — into `entries_beginning_on_focus_page` before
it writes any detail, and the schema declares that field ahead of `scientists`
so it is generated first. The full heading is the unit of identity, never the
surname: within a page every heading is distinct, so there is no run of
identical tokens for the model to lose count of. An earlier version of this idea
rostered *surnames*, which reproduced the bug inside the roster itself
(`Behre, Behre` for three Behres) and then faithfully propagated the undercount.

The collapse is essentially gone. Page 83 (13 entries, `Behre` ×3, `Beeson` ×2)
had been returning 11–12 and now returns 13. The `Beckwith` ×5 and `Beebe` ×4
runs on page 82 come through complete. What remains is an occasional single
dropped entry, no longer tied to repeat runs.

**Multi-pass merge (off by default).** With `--max-passes` above 1 the text
layer is consulted after each page, and if it says the pass came up short the
page is extracted again and the passes are merged as a *union*. Because the
drops are near-random, two passes rarely lose the same scientist, and the merge
also repairs spellings: where two passes disagree, the one the text layer
confirms wins, so `Boer` becomes `Beer`.

It is off by default because it trades one error for another. On the four
hand-verified pages it scored no better overall: it fixed page 83 (12 → 13,
correct) but left page 80 one *over* — a mangled entry that no longer matched
its twin and so survived the union as a second copy of a real scientist. A
phantom scientist is worse than a missing one, since it silently enters the
panel as a real observation. Turn it on only if you would rather over-collect
and filter by hand.

Alphabetical ordering alone does **not** catch this. Ordering detects overlaps
and duplicates, but a gap in the alphabet is indistinguishable from two
genuinely adjacent surnames, so a dropped entry leaves the chain intact. Use
`qa_check.py`, whose text-layer cross-check does detect omissions, and re-extract
the pages it flags before treating the panel as final.

Corrupted surnames deserve particular attention if the panel will be linked to
other sources: the errors seen so far mangle the *end* of the name
(`Behre`→`Behr`, `Beghtel`→`Beghte`, `Bawden`→`Bawen`, `Belfield`→`Belsfield`).

## Key CLI options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--pages START END` | 14 1123 | Inclusive 1-based PDF page range of focus pages |
| `--test-run` | off | Restrict to the first 2 focus pages of the range |
| `--model` | `gpt-5.6-terra` | OpenAI model ID |
| `--reasoning-effort` | `low` | `none`/`low`/`medium`/`high`/`xhigh`; billed as output tokens |
| `--dpi` | 150 | Page image resolution |
| `--max-tokens` | 32000 | Output token cap per call (raise if pages truncate) |
| `--max-passes` | 1 | Above 1, re-extracts pages the text layer says came up short and unions the passes (can duplicate; see below) |
| `--no-verify` | off | Skip the free omission check (one pass per page) |
| `--api-key` | `$OPENAI_API_KEY` | OpenAI API key |
