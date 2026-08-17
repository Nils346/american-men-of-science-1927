# American Men of Science, 1927

This repository turns the 4th edition of *American Men of Science* (Cattell, 1927)
into a structured dataset for research on scientific careers, mobility, and
migration.

The book is a biographical directory: about 13,500 alphabetically ordered
entries, each with name, field, birth, education, jobs, and a 1927 mailing
address. The pages were transcribed from scans (HathiTrust,
[mdp.39015039431948](https://babel.hathitrust.org/cgi/pt?id=mdp.39015039431948))
into tables a researcher can open in Excel or load in Stata / R / Python.

Nothing is interpolated or guessed. If the directory does not print a fact,
the field is blank. Career years are filled only where the book itself dates
a degree or a position.

## Main output

The dataset lives in `release/`. **The file to analyse is the scientist-year
panel:** one row per scientist per calendar year from birth through 1927.

| | |
| --- | ---: |
| Scientists | 13,003 |
| of whom starred (Cattell's top 1,000) | 1,405 |
| Scientist-year rows | 606,865 |
| Dated degrees and positions | 109,652 |
| Career events with a city/country | 79% |

Open `release/scientists_1927.xlsx` to browse people in book order (page 14
onward). Open `release/scientist_year_panel_1927.xlsx` to scroll a career year
by year — Page and Scientist stay frozen, and a thick line marks each new
person. CSVs sit next to the Excel files for statistical software.
`release/codebook.xlsx` documents every column.

| File | Contents |
| --- | --- |
| `scientist_year_panel_1927` | **Main file.** Identity, year, age, degrees and jobs in that year, birthplace, 1927 address, field, research. |
| `scientists_1927` | One row per person: identity, birth, field, societies, research, counts. |
| `career_events_1927` | One row per dated degree or job, with the printed year span and (where known) the institution's city. |
| `institution_locations_1927` | Institution string → city / state / country, plus **source** (`mailing address`, `AI`, or `hand`). |
| `codebook.xlsx` | Overview and column dictionary. |

Institution places come from the book's own mailing addresses where those
name the employer, from a tagged AI draft of remaining strings, or from
hand coding. Ambiguous cases (two cities in one string, society names, journals)
are left blank. AI drafts are not yet fully hand-checked.

## Construction, in brief

Entries were read from page images with a vision language model under a
strict JSON schema, then checked against the PDF's own text layer (missed
people, surname spellings, invented birth dates). A 10-page hand check found
no silent omissions. Residual slips are flagged in `review_workbook.xlsx`.

The full transcription (1,110 pages) cost $103.84. The book is already
extracted; do not re-run that step unless you intend to spend that money
again.

## Rebuilding the tables

With `extraction_checkpoint.jsonl` present, the CSVs and Excel files rebuild
at no cost:

```powershell
pip install -r requirements.txt
python extract_panel.py --panel-only
python merge_institution_locations.py --apply
python build_release.py
```

The Python modules, this README, and `institution_locations.csv` are in git.
The PDF, checkpoint, working CSVs, and `release/` are not — copy the whole
folder to move the project. Pipeline decisions and QA notes are in
[HANDOFF.md](HANDOFF.md). Volume structure is in `DIRECTORY_REFERENCE.md`.
