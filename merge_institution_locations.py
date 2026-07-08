"""
merge_institution_locations.py
==============================
Hand-code institution geography in institution_locations.csv, then apply it to
the event-level audit table (and optionally the panel).

Workflow
--------
1. After extraction:  python merge_institution_locations.py --export
   -> refreshes institution_locations.csv with any new institution names
      (existing rows you already coded are preserved).
2. Fill in city / state_region / country by hand in Excel.
3. Apply:            python merge_institution_locations.py --apply
   -> writes scientist_events_long.csv with institution_city/state/country
      columns (only where you filled them in; never guesses).

Only exact institution-string matches are merged. Leave cells blank when unsure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

DEFAULT_RAW = "scientists_raw.json"
DEFAULT_LOCATIONS = "institution_locations.csv"
DEFAULT_LOCATIONS_EXAMPLE = "institution_locations.example.csv"
DEFAULT_EVENTS = "scientist_events_long.csv"
INST_COL = "institution"


def collect_institutions(raw_path: Path) -> set[str]:
    profiles = json.loads(raw_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for p in profiles:
        for deg in p.get("education") or []:
            inst = (deg.get("institution") or "").strip()
            if inst:
                names.add(inst)
        for job in p.get("employment") or []:
            inst = (job.get("institution_org") or "").strip()
            if inst:
                names.add(inst)
        for mp in p.get("minor_positions") or []:
            if isinstance(mp, dict):
                inst = (mp.get("institution_org") or "").strip()
                if inst:
                    names.add(inst)
    return names


def export_locations(raw_path: Path, locations_path: Path) -> None:
    if not raw_path.exists():
        raise SystemExit(f"Missing {raw_path.name}. Run extraction first.")

    if not locations_path.exists():
        example = locations_path.parent / DEFAULT_LOCATIONS_EXAMPLE
        if example.exists():
            locations_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            locations_path.write_text(
                "institution,city,state_region,country,notes\n", encoding="utf-8"
            )

    discovered = sorted(collect_institutions(raw_path))
    existing = pd.read_csv(locations_path, dtype=str).fillna("")
    if INST_COL not in existing.columns:
        raise SystemExit(f"{locations_path.name} must have an '{INST_COL}' column.")
    coded = set(existing[INST_COL].str.strip())
    rows = existing.to_dict("records")

    new_names = [n for n in discovered if n not in coded]
    for name in new_names:
        rows.append({
            INST_COL: name,
            "city": "",
            "state_region": "",
            "country": "",
            "notes": "",
        })

    out = pd.DataFrame(rows, columns=[INST_COL, "city", "state_region", "country", "notes"])
    out = out.sort_values(INST_COL, kind="stable").reset_index(drop=True)
    out.to_csv(locations_path, index=False, encoding="utf-8-sig")
    print(f"Wrote {locations_path.name}: {len(out)} institutions "
          f"({len(new_names)} newly added, {len(discovered)} total in raw data).")


def load_location_lookup(locations_path: Path) -> pd.DataFrame:
    if not locations_path.exists():
        raise SystemExit(f"Missing {locations_path.name}. Run --export first.")
    loc = pd.read_csv(locations_path, dtype=str).fillna("")
    loc[INST_COL] = loc[INST_COL].str.strip()
    for col in ("city", "state_region", "country"):
        loc[col] = loc[col].str.strip()
        loc.loc[loc[col] == "", col] = pd.NA
    # Drop rows with no geography filled in.
    loc = loc.dropna(subset=["city", "state_region", "country"], how="all")
    return loc.drop_duplicates(subset=[INST_COL], keep="first")


def apply_locations(events_path: Path, locations_path: Path, output_path: Path | None) -> None:
    if not events_path.exists():
        raise SystemExit(f"Missing {events_path.name}. Run extract_panel.py --panel-only first.")

    events = pd.read_csv(events_path, dtype=str)
    lookup = load_location_lookup(locations_path)
    if lookup.empty:
        print("No coded locations found (all geography columns blank). Nothing to merge.")
        return

    merged = events.merge(
        lookup.rename(columns={
            INST_COL: "institution_organization",
            "city": "institution_city",
            "state_region": "institution_state_region",
            "country": "institution_country",
        }),
        on="institution_organization",
        how="left",
    )
    out = output_path or events_path
    merged.to_csv(out, index=False, encoding="utf-8-sig")
    n_hit = merged["institution_country"].notna().sum()
    print(f"Wrote {out.name}: {n_hit}/{len(merged)} event rows matched a coded institution.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", action="store_true",
                        help="Refresh institution_locations.csv from scientists_raw.json.")
    parser.add_argument("--apply", action="store_true",
                        help="Merge coded locations into scientist_events_long.csv.")
    parser.add_argument("--raw", default=DEFAULT_RAW)
    parser.add_argument("--locations", default=DEFAULT_LOCATIONS)
    parser.add_argument("--events", default=DEFAULT_EVENTS)
    parser.add_argument("--output", default=None,
                        help="Output path for --apply (default: overwrite --events).")
    args = parser.parse_args()
    base = Path(__file__).resolve().parent

    if args.export:
        export_locations(base / args.raw, base / args.locations)
    elif args.apply:
        apply_locations(
            base / args.events,
            base / args.locations,
            base / args.output if args.output else None,
        )
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
