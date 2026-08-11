"""Run the extraction through OpenAI's Batch API at half the live-call price.

Same prompt, same schema, same model, same checkpoint format as
`extract_panel.py` -- only the transport differs. The request body comes from
`ExtractionClient.request_body`, so the two paths cannot drift apart.

The trade for the 50% discount is latency: a batch is promised within 24 hours,
so this is a submit-now, harvest-later workflow.

    python batch_extract.py submit  --pages 75 84     # returns immediately
    python batch_extract.py status                    # cheap progress check
    python batch_extract.py harvest --wait            # poll, then write results

`submit` records the batch IDs in batch_state.json, so `harvest` can run in a
different session, tomorrow, on a different machine. Harvested pages land in
extraction_checkpoint.jsonl exactly as live extraction writes them; build the
panel afterwards with `python extract_panel.py --panel-only`.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fitz
from openai import OpenAI

import extract_panel as ep
import qa_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("batch_extract")

STATE_FILE = "batch_state.json"
BATCH_ENDPOINT = "/v1/responses"
COMPLETION_WINDOW = "24h"

# OpenAI caps a batch input file at 200 MB. Page images dominate the payload
# (~4 MB of base64 per request at 150 DPI), so a long run is split across
# several batches. The margin absorbs the JSON envelope.
MAX_FILE_BYTES = 180 * 1024 * 1024
MAX_REQUESTS_PER_BATCH = 50_000

TERMINAL_STATES = {"completed", "failed", "expired", "cancelled"}


# ---------------------------------------------------------------------------
# Building the request file
# ---------------------------------------------------------------------------

def custom_id_for(page: int) -> str:
    return "page-%05d" % page


def page_from_custom_id(custom_id: str) -> int:
    return int(custom_id.rsplit("-", 1)[1])


def build_request_lines(doc, pages: list[int], client: ep.ExtractionClient,
                        dpi: int):
    """Yield (page, jsonl_line) for each focus page, rendering as we go."""
    for page in pages:
        focus_png = ep.render_page_png(doc, page - 1, dpi)
        lookahead_png = (ep.render_page_png(doc, page, dpi)
                         if page < doc.page_count else None)
        content = ep.build_user_content(page, focus_png, lookahead_png,
                                        qa_check.printed_headings(doc, page))
        line = json.dumps({
            "custom_id": custom_id_for(page),
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": client.request_body(content),
        }, ensure_ascii=False)
        yield page, line


def write_shards(doc, pages: list[int], client: ep.ExtractionClient, dpi: int,
                 out_dir: Path) -> list[Path]:
    """Split the requests into files that respect the batch size limits."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("batch_input_*.jsonl"):
        stale.unlink()

    shards: list[Path] = []
    handle = None
    size = count = 0

    def close():
        if handle is not None:
            handle.close()
            logger.info("  %s: %d requests, %.0f MB",
                        shards[-1].name, count, size / 1e6)

    for i, (page, line) in enumerate(build_request_lines(doc, pages, client, dpi), 1):
        encoded = (line + "\n").encode("utf-8")
        if handle is None or size + len(encoded) > MAX_FILE_BYTES \
                or count >= MAX_REQUESTS_PER_BATCH:
            close()
            path = out_dir / ("batch_input_%02d.jsonl" % (len(shards) + 1))
            shards.append(path)
            handle = path.open("wb")
            size = count = 0
        handle.write(encoded)
        size += len(encoded)
        count += 1
        if i % 50 == 0:
            logger.info("  rendered %d/%d pages ...", i, len(pages))
    close()
    return shards


# ---------------------------------------------------------------------------
# Reading a batch result back
# ---------------------------------------------------------------------------

def text_from_body(body: dict) -> str:
    """Pull the JSON payload out of a raw Responses body.

    The batch output file holds plain JSON, not SDK objects, so the
    `output_text` convenience property is not available here.
    """
    parts = []
    for item in body.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "output_text":
                parts.append(part.get("text") or "")
    return "".join(parts)


def refusal_from_body(body: dict) -> str | None:
    for item in body.get("output") or []:
        for part in item.get("content") or []:
            if part.get("type") == "refusal":
                return part.get("refusal")
    return None


def usage_from_body(body: dict) -> dict:
    usage = body.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0,
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0,
    }


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def load_state(base_dir: Path) -> dict:
    path = base_dir / STATE_FILE
    if not path.exists():
        logger.error("No %s here. Run 'submit' first.", STATE_FILE)
        sys.exit(1)
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(base_dir: Path, state: dict) -> None:
    (base_dir / STATE_FILE).write_text(
        json.dumps(state, indent=2), encoding="utf-8")


def make_client(args) -> tuple[OpenAI, ep.ExtractionClient]:
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        logger.error("No API key. Pass --api-key or set OPENAI_API_KEY.")
        sys.exit(1)
    extraction = ep.ExtractionClient(api_key=api_key, model=args.model,
                                     max_tokens=args.max_tokens,
                                     reasoning_effort=args.reasoning_effort)
    return extraction.client, extraction


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_submit(args, base_dir: Path) -> None:
    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = base_dir / pdf_path
    if not pdf_path.exists():
        logger.error("PDF not found: %s", pdf_path)
        sys.exit(1)

    doc = fitz.open(pdf_path)
    start, end = args.pages
    pages = list(range(max(1, start), min(doc.page_count, end) + 1))

    checkpoint = base_dir / ep.CHECKPOINT_FILE
    done = ep.load_completed_pages(checkpoint)
    pages = [p for p in pages if p not in done]
    if not pages:
        logger.info("Every page in that range is already in the checkpoint.")
        return

    api, extraction = make_client(args)
    extraction.preflight()

    logger.info("Rendering %d page(s) into batch request files ...", len(pages))
    shards = write_shards(doc, pages, extraction, args.dpi, base_dir / "batch_input")

    batches = []
    for shard in shards:
        logger.info("Uploading %s ...", shard.name)
        uploaded = api.files.create(file=shard.open("rb"), purpose="batch")
        batch = api.batches.create(input_file_id=uploaded.id,
                                   endpoint=BATCH_ENDPOINT,
                                   completion_window=COMPLETION_WINDOW)
        logger.info("  batch %s created (status %s)", batch.id, batch.status)
        batches.append({"batch_id": batch.id, "input_file_id": uploaded.id,
                        "shard": shard.name})

    state = {
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "reasoning_effort": args.reasoning_effort,
        "dpi": args.dpi,
        "pages": pages,
        "batches": batches,
        "harvested": [],
    }
    save_state(base_dir, state)
    logger.info("Submitted %d page(s) in %d batch(es). Results are promised "
                "within %s.", len(pages), len(batches), COMPLETION_WINDOW)
    logger.info("Check with:   python batch_extract.py status")
    logger.info("Collect with: python batch_extract.py harvest --wait")


def _batch_progress(batch) -> str:
    counts = getattr(batch, "request_counts", None)
    if counts is None:
        return batch.status
    return "%s (%s/%s done, %s failed)" % (
        batch.status, getattr(counts, "completed", 0),
        getattr(counts, "total", 0), getattr(counts, "failed", 0))


def cmd_status(args, base_dir: Path) -> None:
    state = load_state(base_dir)
    api, _ = make_client(args)
    logger.info("Submitted %s for %d page(s), model %s.",
                state["submitted_at"], len(state["pages"]), state["model"])
    for entry in state["batches"]:
        batch = api.batches.retrieve(entry["batch_id"])
        logger.info("  %s  %s", entry["batch_id"], _batch_progress(batch))


def cmd_harvest(args, base_dir: Path) -> None:
    state = load_state(base_dir)
    api, _ = make_client(args)
    checkpoint = base_dir / ep.CHECKPOINT_FILE
    model = state["model"]

    pending = [e for e in state["batches"] if e["batch_id"] not in state["harvested"]]
    if not pending:
        logger.info("Nothing left to harvest.")
        return

    n_ok = n_failed = n_scientists = 0
    total_cost = 0.0

    for entry in pending:
        batch_id = entry["batch_id"]
        while True:
            batch = api.batches.retrieve(batch_id)
            if batch.status in TERMINAL_STATES:
                break
            if not args.wait:
                logger.info("%s is %s; re-run with --wait to block until done.",
                            batch_id, _batch_progress(batch))
                return
            logger.info("%s %s -- checking again in %ds",
                        batch_id, _batch_progress(batch), args.poll_seconds)
            time.sleep(args.poll_seconds)

        if batch.status != "completed":
            logger.error("%s ended as '%s'; nothing to harvest from it.",
                         batch_id, batch.status)
            continue

        logger.info("Downloading results for %s ...", batch_id)
        content = api.files.content(batch.output_file_id).read().decode("utf-8")

        for line in content.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            page = page_from_custom_id(row["custom_id"])
            response = row.get("response") or {}
            body = response.get("body") or {}

            if row.get("error") or response.get("status_code") != 200:
                detail = row.get("error") or body.get("error")
                logger.error("Page %d failed in batch: %s", page, detail)
                ep.append_checkpoint(checkpoint, {
                    "focus_page_number": page,
                    "status": "error",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "error": json.dumps(detail)[:500],
                })
                n_failed += 1
                continue

            usage = usage_from_body(body)
            total_cost += ep.usd_cost(model, usage, batch=True)
            raw_text = text_from_body(body)

            if (body.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
                logger.error("Page %d hit the output token wall; re-run it live "
                             "with a higher --max-tokens.", page)
                n_failed += 1
                continue
            if not raw_text.strip():
                logger.error("Page %d returned no JSON (%s).",
                             page, refusal_from_body(body) or "empty response")
                n_failed += 1
                continue

            try:
                container = ep.parse_llm_json(raw_text)
            except Exception as e:
                logger.error("Page %d: unparseable response (%s).", page, e)
                n_failed += 1
                continue

            profiles = [s.model_dump() for s in container.scientists]
            roster = container.entries_beginning_on_focus_page
            if roster and len(roster) != len(profiles):
                logger.warning("Page %d: rostered %d headings but transcribed %d.",
                               page, len(roster), len(profiles))

            ep.append_checkpoint(checkpoint, {
                "focus_page_number": page,
                "status": "ok",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "n_scientists": len(profiles),
                "n_rostered": len(roster),
                "pass": 1,
                "via": "batch",
                "usage": usage,
                "scientists": profiles,
            })
            n_ok += 1
            n_scientists += len(profiles)

        state["harvested"].append(batch_id)
        save_state(base_dir, state)

    logger.info("=" * 60)
    logger.info("Harvested %d page(s) OK, %d failed, %d scientists.",
                n_ok, n_failed, n_scientists)
    logger.info("Batch cost: $%.2f  (list price would have been $%.2f)",
                total_cost, total_cost / ep.BATCH_DISCOUNT)
    if n_ok:
        logger.info("Per page: $%.4f -> full 1110-page run $%.2f",
                    total_cost / n_ok, total_cost / n_ok * 1110)
    logger.info("Next: python extract_panel.py --panel-only")


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--model", default=ep.DEFAULT_MODEL)
        p.add_argument("--max-tokens", type=int, default=ep.DEFAULT_MAX_TOKENS)
        p.add_argument("--reasoning-effort", default=ep.DEFAULT_REASONING_EFFORT)
        p.add_argument("--api-key", default=None)

    p_submit = sub.add_parser("submit", help="Render pages and queue them as batches.")
    p_submit.add_argument("--pages", nargs=2, type=int, metavar=("START", "END"),
                          default=[ep.DEFAULT_START_PAGE, ep.DEFAULT_END_PAGE])
    p_submit.add_argument("--pdf", default=ep.DEFAULT_PDF)
    p_submit.add_argument("--dpi", type=int, default=ep.DEFAULT_DPI)
    common(p_submit)

    p_status = sub.add_parser("status", help="Show progress of the queued batches.")
    common(p_status)

    p_harvest = sub.add_parser("harvest", help="Write finished batch results to the checkpoint.")
    p_harvest.add_argument("--wait", action="store_true",
                           help="Block and poll until every batch is finished.")
    p_harvest.add_argument("--poll-seconds", type=int, default=120)
    common(p_harvest)

    args = parser.parse_args()
    {"submit": cmd_submit, "status": cmd_status, "harvest": cmd_harvest}[args.command](
        args, base_dir)


if __name__ == "__main__":
    main()
