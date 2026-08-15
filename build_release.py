"""
build_release.py
================
Assemble the clean, publishable dataset in `release/` -- the version that goes
online / to other researchers. No QA flags, no working files: four tidy CSVs
plus a formatted codebook workbook that documents every column.

    python build_release.py

Inputs (produced by `extract_panel.py --panel-only` and, for locations,
`merge_institution_locations.py`):
    scientist_mobility_panel.csv, scientist_events_long.csv,
    scientist_summary.csv, institution_locations.csv (optional)

Outputs in release/:
    scientist_year_panel_1927.csv   balanced scientist-year panel
    career_events_1927.csv          dated events, with locations where coded
    scientists_1927.csv             one row per scientist
    institution_locations_1927.csv  the institution -> place bridge
    codebook.xlsx                   overview + column dictionary
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from merge_institution_locations import load_location_lookup

BASE = Path(__file__).resolve().parent
RELEASE = BASE / "release"

PANEL_IN, PANEL_OUT = "scientist_mobility_panel.csv", "scientist_year_panel_1927.csv"
EVENTS_IN, EVENTS_OUT = "scientist_events_long.csv", "career_events_1927.csv"
SUMMARY_IN, SUMMARY_OUT = "scientist_summary.csv", "scientists_1927.csv"
LOCATIONS_IN, LOCATIONS_OUT = "institution_locations.csv", "institution_locations_1927.csv"
CODEBOOK = "codebook.xlsx"

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(bold=True, color="FFFFFF")
SECTION_FONT = Font(bold=True, size=12, color="1F4E79")

# ---------------------------------------------------------------------------
# Column dictionary. Numbered slot columns are documented once via their stem.
# ---------------------------------------------------------------------------

PANEL_COLS = [
    ("year", "Calendar year; one row per scientist per year from birth through 1927."),
    ("first_name", "First name(s), parenthetical expansions resolved."),
    ("last_name", "Surname as printed."),
    ("scientist_name", "Full name, 'Surname, First' form."),
    ("title", "Honorific printed with the name (Dr., Prof., ...), if any."),
    ("age", "Age in this year (year - birth_year); blank without a birth year."),
    ("activity_confirmed", "1 if the directory confirms any dated activity this year; never interpolated."),
    ("degree_earned_N / degree_institution_N", "Degree(s) conferred this year and where; 'study' marks a degreeless study spell. Numbered slots hold concurrent records."),
    ("position_N / institution_N", "Primary career position(s) held this year. The current-1927 role occupies slot 1 while active."),
    ("is_current_1927_role", "1 if the position in slot 1 is the role held at printing (1927)."),
    ("parallel_position_N / parallel_institution_N", "Minor/parallel roles this year (fellowships, military, editorial, summer posts)."),
    ("birth_year / birth_date", "As printed in the entry; blank when the book prints none (never inferred)."),
    ("birth_city / birth_state / birth_country", "Parsed birthplace geography."),
    ("star_status", "1 if starred (top-1000 scientist, asterisk before the subject)."),
    ("primary_department", "Field of investigation printed in italics."),
    ("mailing_city / mailing_state / mailing_country", "Parsed 1927 mailing-address geography."),
    ("research", "Research subjects, ' | '-separated (accomplished + in progress)."),
    ("source_pdf_page", "PDF page of the printed entry (provenance)."),
]

EVENTS_COLS = [
    ("record_type", "Education, Employment, or MinorPosition."),
    ("start_year / end_year", "Explicit printed span, not expanded. Equal years = a single-year stay; blank end on a current role = held through 1927."),
    ("institution_organization", "Institution/employer as printed (abbreviations kept)."),
    ("role_or_degree", "Degree letters, 'study' for degreeless spells, or the position title."),
    ("is_current_1927_role", "1 if the role was held at printing."),
    ("institution_city / institution_state_region / institution_country", "From the institution-locations bridge; blank where not (yet) coded."),
    ("(identity & entry columns)", "Same scientist identity, birth, star, department, mailing and provenance columns as the panel."),
]

SUMMARY_COLS = [
    ("n_degrees", "Degrees with degree letters."),
    ("n_study_spells", "Education records without a degree (study stays)."),
    ("n_positions / n_parallel_positions", "Primary and minor career records."),
    ("societies", "Society memberships as printed, ' | '-separated."),
    ("research_accomplished / research_in_progress", "Research subjects split at the printed dash."),
    ("(identity & entry columns)", "Same identity, birth, star, department, mailing and provenance columns as the panel."),
]

LOCATION_COLS = [
    ("institution", "Institution string exactly as it appears in the career data (join key)."),
    ("n_events", "Number of career events carrying this string (coverage weight)."),
    ("city / state_region / country", "Assigned location. Sources: the scientist's own printed mailing address where it names the institution, plus hand-coding; blank = not coded, never guessed."),
]


def _sheet_from_rows(wb, title, header, rows, widths):
    ws = wb.create_sheet(title)
    for c, h in enumerate(header, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]
    for r, row in enumerate(rows, start=2):
        for c, v in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.alignment = Alignment(vertical="top", wrap_text=(c == len(row)))
    ws.freeze_panes = "A2"
    return ws


def build_codebook(stats: dict) -> None:
    wb = Workbook()

    ws = wb.active
    ws.title = "Overview"
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 95
    lines = [
        ("Dataset", "American Men of Science, 4th edition (1927) -- scientist-year panel"),
        ("Source", "J. McKeen Cattell (ed.), American Men of Science: A Biographical "
                   "Directory, 4th ed., The Science Press, 1927."),
        ("Construction", "Biographical entries transcribed from page scans with a vision "
                         "LLM under a strict JSON schema, cross-validated against the "
                         "volume's own OCR text layer (entry rosters, name spellings, "
                         "birth data, star counts), with unverifiable values removed "
                         "rather than guessed. Institution locations come from the "
                         "printed mailing addresses and hand-coding only."),
        ("Panel rule", "Activity is filled only for years the directory itself confirms "
                       "(a dated degree or a dated spell); gaps are left blank, never "
                       "interpolated. Concurrent roles occupy separate numbered slots."),
        ("", ""),
        ("Scientists", stats["n_scientists"]),
        ("Panel rows (scientist-years)", stats["n_panel"]),
        ("Dated career events", stats["n_events"]),
        ("Starred (top-1000) scientists", stats["n_starred"]),
        ("Institutions located", "%s of %s distinct institution strings (%s of events)"
         % (stats["n_inst_coded"], stats["n_inst"], stats["inst_coverage"])),
        ("Built", dt.date.today().isoformat()),
        ("", ""),
        ("Files", ""),
        (PANEL_OUT, "Balanced scientist-year panel (main analysis file)."),
        (EVENTS_OUT, "One row per dated degree/position; spans preserved; locations merged."),
        (SUMMARY_OUT, "One row per scientist: identity, societies, research, counts."),
        (LOCATIONS_OUT, "Institution -> city/state/country bridge (join on the institution string)."),
    ]
    for r, (k, v) in enumerate(lines, start=1):
        a = ws.cell(row=r, column=1, value=k)
        ws.cell(row=r, column=2, value=v).alignment = Alignment(wrap_text=True, vertical="top")
        a.font = Font(bold=True)
    ws["A1"].font = ws["A13"].font = SECTION_FONT

    for title, cols in (("Panel columns", PANEL_COLS),
                        ("Events columns", EVENTS_COLS),
                        ("Scientists columns", SUMMARY_COLS),
                        ("Locations columns", LOCATION_COLS)):
        _sheet_from_rows(wb, title, ["Column", "Description"], cols, [42, 95])

    wb.save(RELEASE / CODEBOOK)


def main() -> None:
    for name in (PANEL_IN, EVENTS_IN, SUMMARY_IN):
        if not (BASE / name).exists():
            raise SystemExit("Missing %s -- run 'python extract_panel.py "
                             "--panel-only' first." % name)
    RELEASE.mkdir(exist_ok=True)

    panel = pd.read_csv(BASE / PANEL_IN, dtype=str)
    events = pd.read_csv(BASE / EVENTS_IN, dtype=str)
    summary = pd.read_csv(BASE / SUMMARY_IN, dtype=str)

    # Merge coded institution locations into the release events table.
    n_inst = n_inst_coded = 0
    inst_coverage = "0%"
    loc_path = BASE / LOCATIONS_IN
    if loc_path.exists():
        full = pd.read_csv(loc_path, dtype=str).fillna("")
        n_inst = len(full)
        lookup = load_location_lookup(loc_path)
        n_inst_coded = len(lookup)
        events = events.drop(columns=[c for c in events.columns
                                      if c.startswith("institution_city")
                                      or c.startswith("institution_state")
                                      or c.startswith("institution_country")],
                             errors="ignore")
        bridge = lookup.rename(columns={
            "institution": "institution_organization",
            "city": "institution_city",
            "state_region": "institution_state_region",
            "country": "institution_country",
        })[["institution_organization", "institution_city",
            "institution_state_region", "institution_country"]]
        events = events.merge(bridge, on="institution_organization", how="left")
        with_inst = events["institution_organization"].notna()
        located = events["institution_country"].notna() & with_inst
        if with_inst.sum():
            inst_coverage = "%.0f%%" % (100 * located.sum() / with_inst.sum())

        release_bridge = full[full[["city", "state_region", "country"]]
                              .replace("", pd.NA).notna().any(axis=1)]
        release_bridge = release_bridge[["institution", "n_events", "city",
                                         "state_region", "country"]]
        release_bridge.to_csv(RELEASE / LOCATIONS_OUT, index=False,
                              encoding="utf-8-sig")

    panel.to_csv(RELEASE / PANEL_OUT, index=False, encoding="utf-8-sig")
    events.to_csv(RELEASE / EVENTS_OUT, index=False, encoding="utf-8-sig")
    summary.to_csv(RELEASE / SUMMARY_OUT, index=False, encoding="utf-8-sig")

    build_codebook({
        "n_scientists": len(summary),
        "n_panel": len(panel),
        "n_events": len(events),
        "n_starred": int(pd.to_numeric(summary.get("star_status"),
                                       errors="coerce").fillna(0).sum()),
        "n_inst": n_inst,
        "n_inst_coded": n_inst_coded,
        "inst_coverage": inst_coverage,
    })

    print("Release written to %s\\:" % RELEASE.name)
    for f in sorted(RELEASE.iterdir()):
        print("  %-34s %8.0f KB" % (f.name, f.stat().st_size / 1024))


if __name__ == "__main__":
    main()
