# Panel Data Set: American Men of Science - 4th Edition (1927)

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

## Rebuilding the tables

The book is already transcribed. With `extraction_checkpoint.jsonl` in this
folder, the CSVs and Excel files rebuild at no cost:

```powershell
pip install -r requirements.txt
python extract_panel.py --panel-only
python merge_institution_locations.py --apply
python build_release.py
```

Git holds the code, this README, and `institution_locations.csv`. The PDF,
checkpoint, working CSVs, and `release/` are not in git — copy the whole
folder to move the project. Do not re-extract the 1,110 pages unless you
mean to spend about $104 again.

## How it was built

The hard problem is not “read a paragraph.” It is to produce a panel a
mobility paper can trust: every person who is on the page, names spelled as
printed, years only where the directory dates them, and places only where
there is evidence. The pipeline is a series of decisions against the ways a
vision model quietly fails.

### What we refused to do

If the page does not print a fact, the field is null. Career gaps are left
blank; we never fill the years between two jobs. Repair only deletes: an
unverifiable birth date is stripped, never replaced with a guess from a
degree year or from Wikipedia. Errors found on sample pages were turned into
general rules, not patches for named scientists. Locations are not geocoded
from the modern web.

### Reading a page

Each request sees the focus page and the next one. Only entries that
**begin** on the focus page are kept; spillover text on the look-ahead page
completes them. Text at the top of a page that belongs to the previous
entry is ignored, so people are not doubled at the page break.

Before any field is filled, the model must list every bold heading on the
page. That roster is the unit of identity. Surname alone is not enough: the
directory stacks nine people called the same thing, and the model’s instinct
is to collapse the run. The PDF’s own text layer is passed in as a spelling
hint — vision normalises rare names toward familiar ones (`Bear` to `Bean`);
the OCR layer does not. The image still decides who exists. An early version
that treated the OCR list as a census dropped people the text layer had
missed.

A second extraction pass, merged with the first, recovered some drops and
invented a phantom twin. One pass is the default.

### What the book actually means by a date

The directory’s year grammar is tighter than it looks, and getting it wrong
looks like interpolation:

- `21-23` is a closed stay, 1921 through 1923.
- `21` is that year only, not an open spell.
- `21-` (trailing dash) is still held in 1927.
- An italic role with no year is current, but only 1927 is confirmed.
- Two-digit years are expanded from the person’s own chronology, and never
  past the edition year. A model `1928` is clipped; a spell that exists only
  after 1927 is dropped.

Education mixes degrees and study without a degree (`Illinois, 88-90`,
`Berlin, 05, 07-08`). Degree letters can share one institution and year
(`A.B, M.E, Va. Polytech, 86`). A fellowship sitting in the degree chain is
a minor post, not a reason to drop the next degree. Several current jobs at
once are normal (professor, consulting, chief engineer). Research topics
never belong in the employment field. Stars are an asterisk immediately
before the italic subject, not a speck on the name.

### The panel

One row per scientist per year from birth through 1927. Activity is filled
only for years the directory itself confirms. Concurrent roles occupy
separate numbered slots rather than being concatenated. The event table
keeps the printed spans unexpanded, so duration is not lost when the panel
spreads a spell across years.

### Checking against the print

The dangerous failure is silent: a dropped entry or a mangled surname still
reports success. `qa_check.py` rereads the PDF text layer — too broken to
extract from, good enough to argue with — and looks for missing people,
spelling fights, invented birthdays, impossible ages, and fused
degree/institution strings. Subtractive birth repair runs at panel build:
if the date has no trace in that entry’s print, it is nulled. A 10-page
hand check (127 entries) found no silent omissions. Residual slips, about
one or two fields per ten scrambled pages, surface as flags in
`review_workbook.xlsx`.

### Places

Birthplace and mailing address come from the entry. Career institutions are
a separate bridge, sorted by how many events they carry so the first few
hundred strings buy most of the coverage.

The first fill is the book talking about itself: if three scientists list
“Midwest Exploration Co, Amarillo, Texas” as their address, that locates the
company. Matching is abbreviation-aware. Living in a town does not locate a
college of the same name. Whatever remains can be drafted by a model that
**classifies first** (university, government, firm versus society, journal,
or “Berlin and Vienna”) and locates only when the string is a real 1927
place. Each row is tagged `mailing address`, `AI`, or `hand`. Dual-city
tours stay blank. Unsupervised geocoding was rejected: Columbia, Washington,
and 1927 company names are where a web model is confidently wrong, and the
book cannot catch it.

### Cost

The vision model is `gpt-5.6-sol`, chosen over a cheaper alternative on an
A/B: same people found, far fewer surname misreads, and the surname is the
linking key. Higher reasoning effort doubled tokens with no gain. The full
run used OpenAI’s Batch API (half price): 1,110 pages, 0 failed pages,
**$103.84**.

Volume notes are in `DIRECTORY_REFERENCE.md`. Pipeline memory for continuing
the work is in [HANDOFF.md](HANDOFF.md).
