# HANDOFF — American Men of Science 1927 extraction

**For the next agent (and for Nils on the laptop).** This file is the
conversation memory of the Cursor project. Read it before changing anything.
Do not re-extract the book. Do not spend API money without being asked.

Last updated: 17 August 2026.

---

## 0. How to use this file

Paste or `@HANDOFF.md` into a new Cursor chat on the laptop. Then say what
you want next (usually: verify locations, inspect QA flags, optionally
re-extract 49 pages). The code, README, and this file together replace the
lost chat.

GitHub: `https://github.com/Nils346/american-men-of-science-1927`

---

## 1. Copying the project to another laptop

Git does **not** contain the extracted data. Clone is not enough.

### Must copy (cannot regenerate without ~$104 and the PDF)

| Path | Why |
| --- | --- |
| Whole project folder, including gitignored files | Checkpoint + panel + workbook |
| `American Men of Science_4th edition_1927.pdf` (~302 MB) | Source scans |
| `extraction_checkpoint.jsonl` (~43 MB) | Every page's raw extraction. Source of truth. |
| `scientists_raw.json` (~30 MB) | Nested profiles |
| `scientist_mobility_panel.csv` (~175 MB) | Working panel |
| `scientist_events_long.csv` (~18 MB) | Dated events (locations merged on `--apply`) |
| `scientist_summary.csv` (~3.5 MB) | One row per scientist |
| `review_workbook.xlsx` (~4 MB) | Hand-review Excel with QA flags |
| `qa_findings.csv` | Full QA report |
| `release/` | Publishable CSVs + formatted Excel + codebook |
| `institution_locations.csv` | Also in git; the working bridge |
| `.env` if you used one | API key — do not commit |

### Already in git (code + docs + location bridge)

Python modules, README, EDITIONS.md, DIRECTORY_REFERENCE.md, requirements.txt,
this HANDOFF.md, `institution_locations.csv`.

### Do not copy / do not need

`batch_input/` (huge rendered JSONL, regenerable), old checkpoint snapshots
(already deleted), Cursor chat transcripts (this file replaces them).

### On the new laptop

```powershell
pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."   # professor's OpenAI key, not Anthropic
```

PDF filename must be exactly `American Men of Science_4th edition_1927.pdf`.
All panel rebuilds are free: `python extract_panel.py --panel-only`.
QA is free: `python qa_check.py`. Location export/apply and `build_release.py`
are free.

---

## 2. What this project is

Turn the 4th edition (1927) of *American Men of Science* (~13,500
biographical entries, PDF pages 14–1123) into a **scientist-year panel** for
mobility / migration research.

Principles the user insisted on throughout:

- Do not invent facts. If the page does not print it, the field is null.
- Do not interpolate career years. Only confirmed spells.
- Avoid messy multi-pass merges and post-hoc artifacts.
- Ask before spending money.
- Generalize from sample errors; do not hardcode named scientists.
- Final outputs should look like a real dataset a professor can show
  (codebook, clean CSVs, evidence-based locations).

---

## 3. Current state (after the full run)

**Extraction is done.** Batch API, 15 Aug 2026.

| Metric | Value |
| --- | ---: |
| Pages | 1,110 (PDF 14–1123) |
| Scientists | 13,003 |
| Starred | 1,405 |
| Scientist-year rows | 606,865 |
| Dated events | 109,652 |
| Unique institution strings | 17,840 |
| Institution strings located | 3,565 (mailing address + AI draft + hand) |
| Career events with a location | 83,930 / 105,812 (**79.3%**) |
| Cost | **$103.84** (list would have been $207.68) |
| Failed API pages | **0** |

Headline files for sharing (after `python build_release.py`):

- `release/scientists_1927.xlsx` / `.csv` — open this first
- `release/scientist_year_panel_1927.xlsx` / `.csv` — main analysis file
- `release/career_events_1927.xlsx` / `.csv`
- `release/institution_locations_1927.xlsx` / `.csv` (located rows + `source`)
- `release/codebook.xlsx`

`source` on the location bridge is one of `mailing address` / `AI` / `hand`.
Working review file: `review_workbook.xlsx` (red/amber QA flags, frozen panes).

### What is NOT done

1. **Human verification of AI location drafts.** Rows whose `notes` start
   with `ai:` are classify-then-locate proposals, not yet checked. `auto:`
   rows are mailing-address evidence from the book. Blank anything wrong,
   then `--apply` and `build_release.py` again.
2. **Optional re-extraction of 49 QA-flagged pages** (costs money; not done).
   Pages: 96, 117, 175, 176, 259, 260, 287, 304, 313, 332, 341, 352, 389,
   407, 457, 469, 535, 565, 569, 633, 635, 636, 640, 643, 645, 646, 648,
   683, 697, 698, 700, 719, 721, 722, 741, 742, 751, 756, 805, 838, 891,
   896, 913, 921, 925, 948, 1037, 1038, 1120.
3. **Hand spot-check of red/amber rows** in the workbook.
4. **Join locations onto the year panel** (events table already has them;
   panel institution slot columns do not yet). Small free step when wanted.

---

## 4. Conversation history (what we did and why)

Work spanned roughly late July – mid August 2026, with a two-week break.
The user is a research assistant; the professor's OpenAI key pays the API.

### Phase 1 — understand the pipeline, 10-page sample

User wanted 10 random pages extracted and hand-checked before spending ~$100
on the whole book. Pipeline originally used **Anthropic Claude vision**. User
pasted an **OpenAI** key. We switched the whole stack to OpenAI.

Default model path: first `gpt-5.6-terra` (cost analogue of Sonnet), then
**`gpt-5.6-sol`** after an A/B on messy pages: same entry counts, far fewer
surname misreads. Reasoning effort `low` (higher effort doubled tokens, no
accuracy gain). Live calls then **Batch API at half price**.

### Phase 2 — silent omissions and surname corruption

The model dropped entries (especially runs of the same surname) and
normalized rare names (`Bear`→`Bean`, `Becket`→`Beckett`). Alphabetical
order cannot detect mid-page drops.

Fixes (all still in the code):

1. **Roster first:** schema field `entries_beginning_on_focus_page` is
   generated before `scientists`. Identity unit is the **full bold heading**,
   not the surname.
2. **OCR heading hint** in the prompt: PDF text-layer headings as a spelling
   cross-check. Image is sole authority for *existence* (early bug: model
   anchored to an incomplete OCR list and dropped Belknap).
3. **`--max-passes` union-merge** was tried and **turned off** (default 1).
   It over-collected a mangled twin as a phantom scientist. User explicitly
   did not want messy merges or cost explosions.

`qa_check.py` compares extraction to the PDF OCR layer: missed entries,
misread surnames, star counts, invented birth dates.

### Phase 3 — hand review of pages 75–84 (the important qualitative pass)

All people were included. Categories of error (fixed generally, not by name):

| Category | Cause | Fix |
| --- | --- | --- |
| False stars | Speck read as `*` | Prompt: only `*` before italic department; QA `star_mismatch` |
| Invented birth data | Model fills from degree years or world knowledge | Prompt: never invent; **subtractive repair** nulls unverifiable dates/years; QA `birth_date_not_on_page`, `birth_place_looks_like_degree` |
| Bare-year birth (`Cincinnati, 91`) | Treated as missing | Prompt: that IS birth_year |
| `1912-?` for a single year | Prompt said open-ended | Date semantics: `21-23` closed, `21` that year only (start=end), `21-` current through 1927, italic undated = current with null years |
| Multiple current jobs missed | Prompt said “at most one or two” | Flag every trailing-dash and italic role; split `and`-groups |
| Study without a degree dropped or faked as a degree | `degree_type` was required string | Nullable `degree_type`, `end_year` on education; “study” in the panel |
| Fellowships inside degree chain dropped the next degree | e.g. Becht Ph.D. | Fellow → minor_position; degree chain continues |
| Shared institution/year for two degrees fused | `A.B, M.E, Va. Polytech, 86` | Prompt forward-share; QA `degree_glued_into_institution` / dangling degree |
| Current job with no start year | Missing start | `confirmed_years(None, None, current=True)` → `[1927]` only |

Workbook year display: never print `?`. Use `1921-1923` / `1921` / `1921-` /
`undated`.

Repair is **strictly subtractive** and conservative: if the entry cannot be
located in OCR, leave it; if day+year match under a different month word,
keep the extraction and warn (`birth_month_ocr_mismatch`) because OCR
misreads months (Beadle: model January was correct, OCR said August).

**Do not hardcode Stanford / Beal / Bates / etc.** Degree-in-birthplace is
any degree-token regex. Date repair is any date without a printed trace.

### Phase 4 — institutions and publishable release

User wanted to hand-collect institutions and assign city/state/country for a
migration panel. Tooling: `merge_institution_locations.py`.

- Export unique strings, **sorted by event count** (`n_events`).
- **Prefill from the book's mailing addresses** (not from an LLM web search).
  If “Midwest Exploration Co, Amarillo, Texas” is someone’s address, that
  locates the company. Majority vote ≥60%. Abbreviation-aware prefix match.
  Trailing generic words (University, Co, Hospital) allowed. **Residence
  segments do not vote** (living in West Lafayette does not locate Lafayette
  College). Provenance in `notes`: `auto: N mailing address(es), unanimous`.
- Never overwrite hand-coded cells. Never guess blanks.
- `--apply` exact-string merges onto `scientist_events_long.csv`.

User asked whether an AI should search for institutions. Decision recorded:
**no unsupervised geocoding.** AI-as-draft later is optional; mailing-address
evidence first. Ambiguous names (Washington, Columbia, Miami Ohio vs Florida)
and 1927-era companies are where LLMs confidently lie, and there is no
in-book ground truth to catch it.

`build_release.py` writes analysis CSVs plus formatted Excel (book order,
frozen Page + Scientist, location `source` tagged). The ~600k-row panel
xlsx is large; `--no-panel-xlsx` skips only that file.

Claude Sonnet `--propose` (17 Aug 2026) classified leftover strings first
(university vs `not_a_place` / `ambiguous`) and located only confident
places. Notes `ai:`. Never overwrote `auto:`. Stopped when Anthropic
credits hit $0. Coverage rose from 59% to **79%** of events. Bare
`Berlin` is located (Friedrich-Wilhelms-Universität); tour strings stay blank.

### Phase 5 — cleanup and full run

Repo trimmed to essentials. README rewritten top-down (dataset first,
replication steps, method). `institution_locations.csv` is **tracked**;
extracted CSVs / checkpoint / workbook / `release/` are gitignored.

Full run: `python batch_extract.py submit --pages 14 1123`. The already-
extracted sample pages 75–84 were skipped (checkpoint). 1,100 new pages in
13 batches. Harvest ~1 hour. Then `--panel-only`, `qa_check.py`, location
export/apply, `build_release.py`. README updated with real counts. Pushed.

---

## 5. Architecture (how the code fits together)

```
PDF page images ──► OpenAI Responses API (gpt-5.6-sol, structured JSON)
                         │
                         ▼
              extraction_checkpoint.jsonl
                         │
         qa_check.repair_birth_fields (subtractive)
                         │
              scientists_raw.json
                 ├── scientist_mobility_panel.csv
                 ├── scientist_events_long.csv  ◄── institution_locations.csv
                 ├── scientist_summary.csv
                 └── review_workbook.xlsx  ◄── qa_findings.csv
                         │
                         ▼
                      release/
```

| Module | Role |
| --- | --- |
| `extract_panel.py` | Prompt, JSON schema, Pydantic, live extract, panel/events/summary |
| `batch_extract.py` | Same request bodies via Batch API; submit / status / harvest |
| `qa_check.py` | OCR cross-check + birth repair + findings CSV |
| `profile_merge.py` | Multi-pass union (default unused) |
| `review_workbook.py` | Excel for hand verification |
| `merge_institution_locations.py` | Bridge: export, address prefill, `--propose` (AI classify-then-locate), apply |
| `build_release.py` | Publishable CSVs + Excel + codebook.xlsx |

Default model: `gpt-5.6-sol`. Env: `OPENAI_API_KEY`. Pages: 14–1123.
DPI 150. `--max-passes 1`. `--reasoning-effort low`.

Directory year is **1927**. Current positions with a start and no end fill
every year from start through 1927. Non-current with only a start fill that
one year. Current with no dates fill 1927 only.

---

## 6. Entry grammar (how the book prints, how we parse)

Typical entry:

```
Surname, Title F(irst) M(iddle), mailing address. *Field. Birthplace, Month day, yy.
A.B, Inst, yy, A.M, yy, Ph.D, yy. Job1, Inst, yy-yy; job2, Inst, yy- .
Minor/summer/military. Societies. Research topics - in progress.
```

- Star `*` sits immediately before the italic field, not on the name.
- Two-digit years: 19th / early 20th century; expand with chronology, ≤1927.
- Education mixes **degrees** and **study spells** (`Illinois, 88-90`,
  `Berlin, 05, 07-08`, `Polytech, Berlin, 94; London; Paris`).
- Several degree letters can share one institution+year:
  `A.B, M.E, Va. Polytech, 86`.
- `fellow, yy-yy` inside the degree chain is a minor position.
- Career chain is semicolon-separated. Trailing `yy-` = still held in 1927.
- Several concurrent current jobs are normal (prof + consulting + chief eng).
- Italic undated role = current, dates unknown.
- Research subjects must never leak into employment.

L.R.C.P. is a UK medical licence — null institution is correct.

---

## 7. Full-run QA (17 Aug 2026) — how to read it

`qa_check.py` over 1,110 pages / 13,003 profiles. Many ERROR codes are
**noisy** (OCR encoding, hyphenated names, military ages). Do not treat the
raw counts as “13,003 × error rate”.

### Errors (and what they often mean)

| Code | n | Likely meaning |
| --- | ---: | --- |
| `surname_not_printed` | 134 | Often OCR vs Unicode (`Buttenmüller`, `van der Bijl`, accents). Check before re-extracting. |
| `milestone_before_adulthood` | 129 | Degree/job before age 15 — sometimes real (enlisted young), often a misread birth year. |
| `duplicate_scientist` | 77 | Same person on two pages — page-boundary leakage. Dedup or re-extract the pair. |
| `birth_date_not_on_page` | 29 | Invented month/day; repair should have stripped many of these already. |
| `entry_possibly_missed` | 21 | OCR thinks an entry exists that the model skipped. Highest-value re-extracts. |
| `surname_misread` | 19 | Real spelling disagreements. |
| `page_overlap` | 19 | Consecutive pages both captured the same entries. |
| `end_before_start` | 18 | Bad year pair. |
| `position_in_education` | 14 | Job title in degree_type. |
| `year_out_of_range` | 13 | Year not in 1800–1927. |
| `birth_year_implausible` | 2 | Birth year outside 1800–1915. |

### Warnings (mostly expected)

| Code | n | Notes |
| --- | ---: | --- |
| `no_birth_year` | 789 | Book often prints no birth data — legitimate. |
| `no_current_position` | 688 | Retired / no italic 1927 role — often legitimate. |
| `birth_field_repaired` | 546 | Subtractive repair did its job. |
| `duplicate_degree_type` | 403 | Two B.S. from two universities is often real. |
| `star_mismatch` | 186 | Page `*` count ≠ starred profiles. Worth sampling. |
| `out_of_order` | 129 | Sort vs print order. |
| `surname_undercounted` | 110 | OCR multiplicity vs returned count. |
| `no_education` / `no_employment` | 102 / 74 | Some entries really have none. |
| `degree_glued_into_institution` | 67 | `M.E. Va. Polytech` fusion. |
| `degree_missing_institution_and_year` | 36 | Dangling first of a shared pair. |
| `many_current_positions` | 35 | >4 current flags; sometimes real pile-ups. |
| `birth_month_ocr_mismatch` | 14 | Keep extraction; verify on the image. |
| `low_entry_count` | 6 | p742=1 and p1123=5 may be real (short/last pages). |

**Pages QA listed for re-extract:** see section 3. Re-extracting is optional
and costs money. If you do it: delete those pages' checkpoint lines first,
then `python batch_extract.py submit` for just those pages (or live
`extract_panel.py --pages N N`), then `--panel-only` and `qa_check.py`.

---

## 8. Next work (priority order)

1. **Spot-check AI location drafts.** Open `institution_locations.csv` in
   Excel, sorted by `n_events` descending. Check `ai:` rows (and remaining
   `auto:` if you have not). Blank anything wrong. Then:

   ```powershell
   python merge_institution_locations.py --apply
   python build_release.py
   ```

2. **Optional:** re-extract the 49 flagged pages (ask the user; spends money).

3. **Optional:** join the location bridge onto panel `institution_N` columns
   so the year panel is a scientist-year-**place** file.

4. Spot-check `review_workbook.xlsx` red/amber rows.

5. Do not run `--fresh`. Do not submit another 1,110-page batch.

---

## 9. Hard rules for the next agent

- **No API spend** unless the user explicitly asks (re-extract specific pages,
  or a small test). The book is already extracted.
- **Never interpolate** missing career years.
- **Never invent** birth dates, stars, degrees, or institution countries.
- **Never hardcode** individual scientists or universities in repair logic.
- Location merge is **exact string match** only.
- `--max-passes` stays 1 unless the user wants over-collection.
- Do not commit `extraction_checkpoint.jsonl`, the big CSVs, the PDF, or
  `review_workbook.xlsx` (gitignored). Do commit improvements to
  `institution_locations.csv` after human edits.
- User communication: they prefer being asked when in doubt; they review
  samples carefully; they care that the professor sees an organized dataset,
  not an LLM dump.

---

## 10. CLI cheat sheet

```powershell
# status of the (already completed) batches
python batch_extract.py status

# rebuild everything from checkpoint (FREE)
python extract_panel.py --panel-only

# quality report (FREE)
python qa_check.py
python qa_check.py --severity error
python qa_check.py --pages 75 84

# locations
python merge_institution_locations.py --export
python merge_institution_locations.py --propose --min-events 20
python merge_institution_locations.py --apply

# publishable folder
python build_release.py

# workbook only
python review_workbook.py
```

Environment: `OPENAI_API_KEY`. Model: `gpt-5.6-sol`.

---

## 11. Design decisions that should not be silently reversed

- OpenAI, not Anthropic.
- `gpt-5.6-sol` default, not terra (surnames are the linking key).
- Batch API for real runs.
- Full-name roster + OCR spelling hint; image decides existence.
- Single pass, no union-merge by default.
- Subtractive birth repair at panel build, not a second LLM call.
- Institution geography from printed addresses + tagged AI drafts + human
  coding, not unsupervised geocoding APIs. Dual-city / non-place strings stay blank.
- Public artifact is clean CSVs + formatted Excel + codebook, not the QA workbook.
- Panel is balanced scientist-year, activity only where confirmed. The
  calendar never extends past 1927: overshooting end dates (model `1928`)
  are clipped; spells that only exist after 1927 are dropped.
- Release tables are sorted in book order (PDF page, surname, year).

---

## 12. Pointers into the code

- System prompt and date/education/employment rules: `extract_panel.py`
  `SYSTEM_PROMPT`.
- JSON schema / Pydantic: same file, `_DEGREE_SCHEMA`, `DegreeRecord`,
  `MinorPositionRecord`.
- Birth repair: `qa_check.py` `repair_birth_fields`, `_entry_slice`,
  `_DEGREE_TOKEN_RE`.
- Year expansion: `extract_panel.py` `confirmed_years`.
- Workbook spans: `review_workbook.py` `_span`.
- Address prefill matcher: `merge_institution_locations.py`
  `address_evidence`, `_segment_matches`.
- Batch custom ids / harvest: `batch_extract.py`.

Directory field notes: `DIRECTORY_REFERENCE.md`. PDF edition links:
`EDITIONS.md`. User-facing overview: `README.md`.
