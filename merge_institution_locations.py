"""
merge_institution_locations.py
==============================
Hand-code institution geography in institution_locations.csv, then apply it to
the event-level audit table (and optionally the panel).

Workflow
--------
1. After extraction:  python merge_institution_locations.py --export
   -> refreshes institution_locations.csv with any new institution names
      (existing rows you already coded are preserved), sorted by how many
      career events each string carries, and PREFILLS locations from the
      book's own evidence: when a scientist's printed mailing address contains
      the institution's name ("Midwest Exploration Co, Amarillo, Texas"), the
      address's parsed city/state/country become the institution's proposed
      location. Prefills carry a "notes" provenance line ("auto: 3 mailing
      addresses, unanimous") -- verify them, blank anything wrong.
2. Optional AI draft for frequent EMPTY rows:
   python merge_institution_locations.py --propose
   -> classifies each leftover string (real 1927 institution vs society /
      journal / too-generic), resolves directory shorthand ("Berlin" =
      University of Berlin), and fills city/state/country ONLY when the
      classification is a locatable place. Notes start with "ai:". Never
      overwrites mailing-address or hand-coded rows. Default: strings with
      at least 20 events (~200 rows).
3. Fill in / verify city / state_region / country by hand in Excel.
4. Apply:            python merge_institution_locations.py --apply
   -> writes scientist_events_long.csv with institution_city/state/country
      and institution_location_source (mailing address / AI / hand).
      Only filled rows merge; never guesses.

Only exact institution-string matches are merged. Leave cells blank when unsure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

import pandas as pd

DEFAULT_RAW = "scientists_raw.json"
DEFAULT_LOCATIONS = "institution_locations.csv"
DEFAULT_EVENTS = "scientist_events_long.csv"
INST_COL = "institution"


def collect_institutions(raw_path: Path) -> Counter:
    """Every institution string with the number of events it appears in.

    The distribution is heavy-tailed (a few universities carry most of the
    events, thousands of companies/hospitals appear once), so the count is
    what makes hand-coding tractable: sorted by it, the first few hundred
    rows buy most of the coverage.
    """
    profiles = json.loads(raw_path.read_text(encoding="utf-8"))
    counts: Counter = Counter()
    for p in profiles:
        for deg in p.get("education") or []:
            inst = (deg.get("institution") or "").strip()
            if inst:
                counts[inst] += 1
        for job in p.get("employment") or []:
            inst = (job.get("institution_org") or "").strip()
            if inst:
                counts[inst] += 1
        for mp in p.get("minor_positions") or []:
            if isinstance(mp, dict):
                inst = (mp.get("institution_org") or "").strip()
                if inst:
                    counts[inst] += 1
    return counts


def _tokens(s: str) -> list[str]:
    return re.sub(r"[.,;:]", " ", s.lower()).split()


# Generic institution-type words: allowed to trail a name match without
# invalidating it ("Hopkins" inside "Johns Hopkins University" is a match;
# "Union" inside "Union Central Life Ins. Co" is not).
_GENERIC_SUFFIX = frozenset(
    "university univ college col institute institution inst school sch academy"
    " acad co company corp works hospital hosp laboratory lab station sta park"
    " museum library foundation fund observatory garden gardens herbarium"
    " seminary sem".split())


def _segment_matches(inst_toks: list[str], seg_toks: list[str]) -> bool:
    """True when the institution's tokens appear, in order and contiguously,
    as PREFIXES of the segment's tokens ("Midwest Explor. Co" matches "Midwest
    Exploration Co"), and anything after the match is a generic type word."""
    n = len(inst_toks)
    for start in range(len(seg_toks) - n + 1):
        if all(seg_toks[start + i].startswith(t) for i, t in enumerate(inst_toks)):
            return all(t in _GENERIC_SUFFIX for t in seg_toks[start + n:])
    return False


def address_evidence(raw_path: Path) -> dict[str, tuple[tuple[str, str, str], int, int]]:
    """institution string -> ((city, state, country), n_agreeing, n_total).

    Evidence comes from the book itself: every entry prints a mailing address,
    and when that address contains an institution's name, the address's parsed
    city/state/country locate the institution -- no outside knowledge, no
    guessing. Multiple scientists at the same institution vote; the modal
    location wins only with a >=60% majority, otherwise no proposal is made.
    """
    profiles = json.loads(raw_path.read_text(encoding="utf-8"))

    # Comma-segments of every locatable address (institution names never span
    # a comma), indexed by 4-char token prefixes to keep the scan linear-ish.
    segments: list[tuple[list[str], tuple[str, str, str]]] = []
    index: dict[str, set[int]] = {}
    for p in profiles:
        city = (p.get("mailing_city") or "").strip()
        addr = (p.get("mailing_address") or "").strip()
        if not city or not addr:
            continue
        state = (p.get("mailing_state") or "").strip()
        country = (p.get("mailing_country") or "").strip()
        loc = (city, state, country)
        # Only the employer-name part of the address may vote: a segment that
        # is just the city/state ("West Lafayette", "Ind") says where someone
        # LIVES, not where an institution of that name is.
        geo_toks = set(_tokens(city) + _tokens(state) + _tokens(country))
        for seg in addr.split(","):
            toks = _tokens(seg)
            if not toks or all(t in geo_toks for t in toks):
                continue
            idx = len(segments)
            segments.append((toks, loc))
            for t in set(toks):
                index.setdefault(t[:4], set()).add(idx)

    evidence: dict[str, tuple[tuple[str, str, str], int, int]] = {}
    for inst in collect_institutions(raw_path):
        inst_toks = _tokens(inst)
        if not inst_toks or sum(map(len, inst_toks)) < 4:
            continue
        votes: Counter = Counter()
        seen: set[int] = set()
        for i in index.get(inst_toks[0][:4], ()):
            if i in seen:
                continue
            seen.add(i)
            toks, loc = segments[i]
            if _segment_matches(inst_toks, toks):
                votes[loc] += 1
        if not votes:
            continue
        (loc, n_top), n_total = votes.most_common(1)[0], sum(votes.values())
        if n_top / n_total >= 0.6:
            evidence[inst] = (loc, n_top, n_total)
    return evidence


def export_locations(raw_path: Path, locations_path: Path) -> None:
    if not raw_path.exists():
        raise SystemExit(f"Missing {raw_path.name}. Run extraction first.")

    if not locations_path.exists():
        locations_path.write_text(
            "institution,n_events,city,state_region,country,notes\n",
            encoding="utf-8",
        )

    counts = collect_institutions(raw_path)
    existing = pd.read_csv(locations_path, dtype=str).fillna("")
    if INST_COL not in existing.columns:
        raise SystemExit(f"{locations_path.name} must have an '{INST_COL}' column.")
    coded = set(existing[INST_COL].str.strip())
    rows = existing.to_dict("records")

    new_names = [n for n in counts if n not in coded]
    for name in new_names:
        rows.append({
            INST_COL: name,
            "n_events": "",
            "city": "",
            "state_region": "",
            "country": "",
            "notes": "",
        })

    out = pd.DataFrame(rows, columns=[INST_COL, "n_events", "city",
                                      "state_region", "country", "notes"])
    # Refresh every count (also for rows already coded) and sort by it, so the
    # top of the file is always where an hour of hand-coding buys the most.
    out["n_events"] = out[INST_COL].str.strip().map(counts).fillna(0).astype(int)

    # Prefill EMPTY rows from the book's own mailing addresses. Hand-coded
    # cells are never overwritten; provenance goes into "notes" for review.
    evidence = address_evidence(raw_path)
    n_prefilled = 0
    empty = out[["city", "state_region", "country"]].replace("", pd.NA).isna().all(axis=1)
    for i in out.index[empty]:
        ev = evidence.get(out.at[i, INST_COL].strip())
        if not ev:
            continue
        (city, state, country), n_top, n_total = ev
        out.at[i, "city"] = city
        out.at[i, "state_region"] = state
        out.at[i, "country"] = country
        out.at[i, "notes"] = ("auto: %d mailing address(es), unanimous" % n_total
                              if n_top == n_total else
                              "auto: %d of %d mailing addresses" % (n_top, n_total))
        n_prefilled += 1
    out = out.sort_values(["n_events", INST_COL],
                          ascending=[False, True], kind="stable").reset_index(drop=True)
    out.to_csv(locations_path, index=False, encoding="utf-8-sig")
    n_events_total = sum(counts.values())
    covered = out.loc[out[["city", "state_region", "country"]].replace("", pd.NA)
                      .notna().any(axis=1), "n_events"].sum()
    print(f"Wrote {locations_path.name}: {len(out)} institutions "
          f"({len(new_names)} newly added, {n_prefilled} prefilled from mailing "
          f"addresses). Coded+prefilled rows cover {covered}/{n_events_total} "
          f"institution events ({covered / max(n_events_total, 1):.0%}).")


LOCATE_PROMPT = """\
You are locating institution strings from the 1927 directory American Men of
Science. Each string is printed as an employer or school in a biography.

STEP 1 — classify, before any geography:
- university / college: a specific school. Bare European city names in the
  EDUCATION slot are the university of that city: "Berlin" = University of
  Berlin (Friedrich-Wilhelms-Universität), "Göttingen"/"Gottingen" =
  University of Göttingen, "Munich" = University of Munich, "Edinburgh" =
  University of Edinburgh. US shorthand: "Iowa Col" = Iowa State College
  (Ames), "Pa. State" = Pennsylvania State College, "Mass. Inst. Tech" = MIT
  (Cambridge), "Mass. Col" = Massachusetts Agricultural College (Amherst),
  "N. Y. Univ" = New York University, "Washington (St. Louis)" = Washington
  University in St. Louis, "Washington (Seattle)" = University of Washington.
- government: USDA, Bureau of Standards, Forest Service, a named experiment
  station WITH a state ("Wis. Exp. Sta").
- company / hospital / museum / observatory / other_institution: a specific
  named body that has a headquarters.
- not_a_place: scientific societies (A.A., Chem. Soc.), journals, newspapers,
  a country or army as the "institution" (U.S.A.), generic words with no site
  ("Exp. Sta" with no state, "college", "high sch"), ships, honorary bodies.
  These must NOT get a city.
- ambiguous: two equally plausible sites, or you are not sure. Leave geography
  empty.

STEP 2 — only if the class is a locatable institution AND you are confident:
give the 1927 city, US state/Canadian province as the directory would print
it (Mass, N. Y, Calif, Pa, D. C), and country (USA, Canada, Germany, ...).
resolved_name is the full historical name.

Never invent a campus to "complete" a society or a generic station.
"""

_KIND_LOCATABLE = frozenset({
    "university", "college", "government", "company", "hospital",
    "museum", "observatory", "other_institution",
})
_KIND_BLANK = frozenset({"not_a_place", "ambiguous"})

_PROPOSE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["institution", "kind", "resolved_name", "city",
                 "state_region", "country", "confident"],
    "properties": {
        "institution": {"type": "string"},
        "kind": {
            "type": "string",
            "enum": sorted(_KIND_LOCATABLE | _KIND_BLANK),
        },
        "resolved_name": {"type": ["string", "null"]},
        "city": {"type": ["string", "null"]},
        "state_region": {"type": ["string", "null"]},
        "country": {"type": ["string", "null"]},
        "confident": {"type": "boolean"},
    },
}

_PROPOSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {"type": "array", "items": _PROPOSE_ITEM},
    },
}


def _empty_geo(row) -> bool:
    return not any(str(row.get(c) or "").strip()
                   for c in ("city", "state_region", "country"))


_SONNET5_IN = 2.0 / 1_000_000
_SONNET5_OUT = 10.0 / 1_000_000


def _usd_sonnet5(input_tokens: int, output_tokens: int) -> float:
    return input_tokens * _SONNET5_IN + output_tokens * _SONNET5_OUT


def _propose_batch_openai(client, model: str, names: list[str]) -> tuple[list[dict], dict]:
    numbered = "\n".join("%d. %s" % (i + 1, n) for i, n in enumerate(names))
    kwargs = dict(
        model=model,
        instructions=LOCATE_PROMPT,
        input=[{"role": "user", "content":
                "Classify and, if locatable, site these printed strings:\n\n"
                + numbered}],
        max_output_tokens=8000,
        text={"format": {
            "type": "json_schema",
            "name": "institution_locations",
            "strict": True,
            "schema": _PROPOSE_SCHEMA,
        }},
        store=False,
        reasoning={"effort": "low"},
    )
    try:
        resp = client.responses.create(**kwargs)
    except Exception as exc:
        if "reasoning" in str(exc).lower():
            kwargs.pop("reasoning", None)
            resp = client.responses.create(**kwargs)
        else:
            raise
    text = getattr(resp, "output_text", None) or ""
    if not text:
        for item in getattr(resp, "output", None) or []:
            for block in getattr(item, "content", None) or []:
                if getattr(block, "type", "") == "output_text":
                    text += getattr(block, "text", "") or ""
    data = json.loads(text)
    usage = getattr(resp, "usage", None)
    return data["items"], {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


def _propose_batch_anthropic(client, model: str, names: list[str]) -> tuple[list[dict], dict]:
    numbered = "\n".join("%d. %s" % (i + 1, n) for i, n in enumerate(names))
    kwargs = dict(
        model=model,
        max_tokens=4000,
        system=LOCATE_PROMPT,
        messages=[{"role": "user", "content":
                   "Classify and, if locatable, site these printed strings:\n\n"
                   + numbered}],
        tools=[{
            "name": "institution_locations",
            "description": "Classifications and locations for every listed string.",
            "input_schema": _PROPOSE_SCHEMA,
        }],
        tool_choice={"type": "tool", "name": "institution_locations"},
        thinking={"type": "disabled"},
    )
    try:
        resp = client.messages.create(**kwargs)
    except Exception as exc:
        if "thinking" in str(exc).lower():
            kwargs.pop("thinking", None)
            resp = client.messages.create(**kwargs)
        else:
            raise
    payload = None
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "institution_locations":
            payload = block.input
            break
    if not payload:
        raise RuntimeError("Claude returned no tool payload")
    usage = resp.usage
    return payload["items"], {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
    }


def _accept_proposal(raw: dict, wanted: set[str]) -> tuple[str, dict] | None:
    """Return (institution, fields) or None if the model failed the check."""
    inst = (raw.get("institution") or "").strip()
    if inst not in wanted:
        hit = next((w for w in wanted if w.lower() == inst.lower()), None)
        if hit is None:
            return None
        inst = hit
    kind = (raw.get("kind") or "").strip()
    confident = bool(raw.get("confident"))
    city = (raw.get("city") or "").strip() or ""
    state = (raw.get("state_region") or "").strip() or ""
    country = (raw.get("country") or "").strip() or ""
    resolved = (raw.get("resolved_name") or "").strip() or inst

    if kind in _KIND_BLANK or not confident:
        return inst, {
            "city": "", "state_region": "", "country": "",
            "notes": "ai: %s | %s | not filled" % (resolved, kind or "unclassified"),
        }
    if kind not in _KIND_LOCATABLE or not country:
        return inst, {
            "city": "", "state_region": "", "country": "",
            "notes": "ai: %s | %s | failed check (need country + locatable kind)"
                     % (resolved, kind or "unclassified"),
        }
    return inst, {
        "city": city, "state_region": state, "country": country,
        "notes": "ai: %s | %s" % (resolved, kind),
    }


def propose_locations(locations_path: Path, min_events: int, model: str,
                      batch_size: int, api_key: str | None,
                      provider: str = "openai", max_spend: float | None = None,
                      workers: int = 1) -> None:
    """AI-draft locations for empty rows, highest-frequency first.

    Never overwrites mailing-address or hand-coded cells. Stops before
    max_spend (USD) when set. Writes the CSV after every successful batch.
    """
    if not locations_path.exists():
        raise SystemExit("Missing %s -- run --export first." % locations_path.name)
    df = pd.read_csv(locations_path, dtype=str).fillna("")
    df["n_events"] = pd.to_numeric(df["n_events"], errors="coerce").fillna(0).astype(int)
    empty = df.apply(_empty_geo, axis=1) & (df["n_events"] >= min_events)
    empty = empty & ~df["notes"].astype(str).str.startswith("ai:")
    targets = (df.loc[empty]
               .sort_values("n_events", ascending=False)[INST_COL]
               .str.strip().tolist())
    if not targets:
        print("No empty institutions with n_events >= %d." % min_events)
        return

    if provider == "anthropic":
        import anthropic
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("Set ANTHROPIC_API_KEY or pass --api-key.")
        client = anthropic.Anthropic(api_key=key, timeout=90.0)
        call = lambda names: _propose_batch_anthropic(client, model, names)
    else:
        from openai import OpenAI
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("Set OPENAI_API_KEY or pass --api-key.")
        client = OpenAI(api_key=key)
        call = lambda names: _propose_batch_openai(client, model, names)

    chunks = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]
    workers = max(1, workers)
    print("Proposing locations for %d strings (%d batches, %d workers), "
          "%s / %s%s ..."
          % (len(targets), len(chunks), workers, provider, model,
             "" if max_spend is None else ", cap $%.2f" % max_spend))

    filled = skipped = failed = done_n = 0
    in_tok = out_tok = 0
    spent = 0.0
    by_name: dict[str, dict] = {}
    lock = threading.Lock()
    halt = False

    def flush() -> pd.DataFrame:
        for i in df.index:
            name = str(df.at[i, INST_COL]).strip()
            if name not in by_name or not _empty_geo(df.loc[i]):
                continue
            fields = by_name[name]
            df.at[i, "city"] = fields["city"]
            df.at[i, "state_region"] = fields["state_region"]
            df.at[i, "country"] = fields["country"]
            df.at[i, "notes"] = fields["notes"]
        out = df.sort_values(["n_events", INST_COL],
                             ascending=[False, True], kind="stable").reset_index(drop=True)
        out.to_csv(locations_path, index=False, encoding="utf-8-sig")
        return out

    def absorb(chunk: list[str], items: list[dict], usage: dict) -> float:
        nonlocal filled, skipped, failed, done_n, in_tok, out_tok, spent, df
        cost = _usd_sonnet5(usage["input_tokens"], usage["output_tokens"])
        in_tok += usage["input_tokens"]
        out_tok += usage["output_tokens"]
        spent += cost
        done_n += len(chunk)
        wanted = set(chunk)
        for raw in items:
            got = _accept_proposal(raw, wanted)
            if got is None:
                failed += 1
                continue
            name, fields = got
            by_name[name] = fields
            if fields["country"]:
                filled += 1
            else:
                skipped += 1
        df = flush()
        print("  %d/%d  filled %d  blank %d  spent $%.3f (batch $%.3f)"
              % (done_n, len(targets), filled, skipped, spent, cost))
        return cost

    def run_one(chunk: list[str]):
        return chunk, call(chunk)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        chunk_iter = iter(chunks)
        futures = set()

        def pump() -> None:
            nonlocal halt
            while len(futures) < workers:
                if halt:
                    return
                if max_spend is not None and spent + 0.03 * (len(futures) + 1) >= max_spend:
                    halt = True
                    return
                nxt = next(chunk_iter, None)
                if nxt is None:
                    return
                futures.add(pool.submit(run_one, nxt))

        pump()
        while futures:
            finished, futures = wait(futures, return_when=FIRST_COMPLETED)
            for fut in finished:
                try:
                    chunk, (items, usage) = fut.result()
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)
                    print("  batch failed: %s" % exc)
                    if any(s in msg.lower() for s in (
                            "insufficient_quota", "credit_balance", "credit",
                            "billing", "429")):
                        halt = True
                        print("Stopping: API credits/rate. Progress is saved.")
                    else:
                        failed += 1
                    continue
                with lock:
                    absorb(chunk, items, usage)
                    if max_spend is not None and spent + 0.12 >= max_spend:
                        halt = True
                        print("Stopping before further batches (spend cap $%.2f)."
                              % max_spend)
            if not halt:
                pump()

    located = df[["city", "state_region", "country"]].replace("", pd.NA).notna().any(axis=1)
    print("Wrote %s: filled %d, left blank (not a place / ambiguous / failed "
          "check) %d, unmatched replies %d. Located rows now cover %d/%d "
          "events (%.0f%%). Estimated spend $%.2f (%d in + %d out tokens)."
          % (locations_path.name, filled, skipped, failed,
             int(df.loc[located, "n_events"].sum()),
             int(df["n_events"].sum()),
             100 * df.loc[located, "n_events"].sum() / max(int(df["n_events"].sum()), 1),
             spent, in_tok, out_tok))


def source_from_notes(notes: str) -> str:
    """How a location was assigned -- for the publishable bridge file."""
    n = (notes or "").strip()
    if n.startswith("auto:"):
        return "mailing address"
    if n.startswith("ai:"):
        return "AI"
    return "hand"


def load_location_lookup(locations_path: Path) -> pd.DataFrame:
    if not locations_path.exists():
        raise SystemExit(f"Missing {locations_path.name}. Run --export first.")
    loc = pd.read_csv(locations_path, dtype=str).fillna("")
    loc[INST_COL] = loc[INST_COL].str.strip()
    for col in ("city", "state_region", "country"):
        loc[col] = loc[col].str.strip()
        loc.loc[loc[col] == "", col] = pd.NA
    loc["source"] = loc["notes"].map(source_from_notes) if "notes" in loc.columns else "hand"
    loc = loc.dropna(subset=["city", "state_region", "country"], how="all")
    return loc.drop_duplicates(subset=[INST_COL], keep="first")


def apply_locations(events_path: Path, locations_path: Path, output_path: Path | None) -> None:
    if not events_path.exists():
        raise SystemExit(f"Missing {events_path.name}. Run extract_panel.py --panel-only first.")

    events = pd.read_csv(events_path, dtype=str)
    leftover = [
        "n_events", "notes",
        "institution_city", "institution_state_region", "institution_country",
        "institution_location_source",
    ]
    events = events.drop(columns=[c for c in leftover if c in events.columns])
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
            "source": "institution_location_source",
        })[[
            "institution_organization", "institution_city",
            "institution_state_region", "institution_country",
            "institution_location_source",
        ]],
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
    parser.add_argument("--propose", action="store_true",
                        help="AI-draft locations for frequent empty rows (classify first).")
    parser.add_argument("--apply", action="store_true",
                        help="Merge coded locations into scientist_events_long.csv.")
    parser.add_argument("--raw", default=DEFAULT_RAW)
    parser.add_argument("--locations", default=DEFAULT_LOCATIONS)
    parser.add_argument("--events", default=DEFAULT_EVENTS)
    parser.add_argument("--output", default=None,
                        help="Output path for --apply (default: overwrite --events).")
    parser.add_argument("--min-events", type=int, default=20,
                        help="--propose: only empty strings with at least this many events.")
    parser.add_argument("--model", default="gpt-5.6-terra",
                        help="--propose model (OpenAI default gpt-5.6-terra; Anthropic: claude-sonnet-5).")
    parser.add_argument("--provider", default="openai", choices=["openai", "anthropic"])
    parser.add_argument("--max-spend", type=float, default=None,
                        help="Stop --propose before estimated USD spend exceeds this.")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--workers", type=int, default=1,
                        help="--propose: parallel API calls (try 8–12).")
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args()
    base = Path(__file__).resolve().parent

    if args.export:
        export_locations(base / args.raw, base / args.locations)
    elif args.propose:
        propose_locations(base / args.locations, args.min_events, args.model,
                          args.batch_size, args.api_key,
                          provider=args.provider, max_spend=args.max_spend,
                          workers=args.workers)
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
