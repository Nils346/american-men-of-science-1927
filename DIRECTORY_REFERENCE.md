# Directory Reference — 4th Edition (1927)

Notes on the source volume used by this pipeline. For setup and usage see [README.md](README.md).

## Scope and scale

- Contains approximately 13,500 sketches.
- Tolerably complete for those in North America conducting research in the natural and exact sciences.
- Includes some figures in engineering, medicine, and applied sciences.
- Explicitly excludes most workers in education, economics, and other subjects outside the exact and natural sciences.
- Scientist listing starts on PDF page 14 and runs through page 1123.

## Information per scientist (p. 9)

- The full name with title and mail address, the part of the name ordinarily omitted in correspondence being in parentheses.
- The department of investigation given in italics.
- The place and date of birth.
- Education and degrees.
- Positions with dates, the present position being given in italics.
- Temporary and minor positions.
- Honorary degrees and other scientific honors.
- Membership in scientific and learned societies.
- Chief subjects of research, those accomplished being separated by a dash from those in progress.

## Abbreviation guide (p. 10)

- Abbreviations are intended to be self-explanatory, except for scientific societies (e.g. F.A.A. = fellow of the AAAS; “American” is omitted for national societies).
- In citing institutions of learning the words “college” and “university” are omitted.
- Degrees are cited as A.B., A.M., etc., although the reverse order of the letters is used in Great Britain and sometimes here.
- When the same position has been occupied successively at different institutions, the position title is not repeated; when different positions have been occupied at the same institution, the institution is not repeated.

## Starred scientists

- A star is prefixed to the subject of research for the top 1,000 leading students of the exact and natural sciences.
- In the 4th edition (1927), 250 new stars were awarded.
- Baseline distribution of the 1,000 stars: Chemistry (175), Physics (150), Zoology (150), Botany (100), Geology (100), Mathematics (80), Pathology (60), Astronomy (50), Psychology (50), Physiology (40), Anatomy (25), Anthropology (20).

## Features collected by the pipeline

Ordered by chronological appearance in each entry:

| Field | Description |
| --- | --- |
| Full name | Bold, left-indented: `Last, First (Omitted)` |
| Titles | e.g. Dr., Prof., Gen. |
| Mailing address | Street, department, city, state/country |
| Star status | `*` before italic department |
| Department | Primary field, in italics |
| Birth data | Place + date; expanded to `birth_year`, `birth_date`, city/state/country |
| Education | Degree type, institution, year |
| Employment | Position, institution, date range; current role in italics |
| Minor positions | Fellowships, military, visiting roles, etc. |
| Societies | Printed society abbreviations |
| Research | Topics before/after dash (combined in panel output) |

Edition links for other years: [EDITIONS.md](EDITIONS.md).
