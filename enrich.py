#!/usr/bin/env python3
"""Unified categorize+enrich workflow: single CLI for all uncategorized YNAB txns."""
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
from decimal import Decimal

from ynab_client import YNABClient, filter_uncategorized_writable
from categorizer import (
    load_payee_cache, save_payee_cache, build_cache_from_transactions,
    count_categories_from_transactions, count_categories, compute_confidence_threshold,
    filter_categories_by_usage, categorize_transactions, update_cache_from_claude_results,
)
from amazon_matcher import (
    is_amazon_payee, find_latest_dump, extract_order_history_csv, parse_order_history,
    match_shipments_to_transactions, _json_default, _md_escape, _next_free_path,
)

logger = logging.getLogger(__name__)


def _dump_freshness_warning(shipments, since_date_str: str, days_back: int, *, log=logger) -> str | None:
    """Check if Amazon dump is stale relative to working window.

    Args:
        shipments: List of AmazonShipment objects.
        since_date_str: ISO 8601 string (YYYY-MM-DD) for window start.
        days_back: Number of days in working window.
        log: Logger instance (for testing).

    Returns:
        Warning message if dump is stale, None if fresh.
    """
    from datetime import date

    if not shipments:
        return "Amazon dump contains 0 shipments — every Amazon txn will be unmatched."

    latest_ship = max((s.ship_date for s in shipments if s.ship_date), default=None)
    if latest_ship is None:
        return "Amazon dump contains no parseable ship dates."

    since_date = date.fromisoformat(since_date_str)
    if latest_ship < since_date:
        return (
            f"Amazon dump's latest ship date {latest_ship} is before --days={days_back} "
            f"window start {since_date}. Most/all Amazon txns will end up unmatched. "
            f"Re-export an Amazon dump."
        )

    return None


def write_unified_changeset(
    flat_results,
    skipped,
    unmatched_amazon,
    split_proposals,
    *,
    budget_id: str,
    since_date: str,
    days_back: int,
    K: int,
    confidence_threshold: float,
    dump_path: Path | None,
    out_dir: Path = Path("data/cache"),
    now: datetime | None = None,
) -> tuple[Path, Path]:
    """Write unified enrich changeset to paired markdown + JSON artifacts.

    Args:
        flat_results: List of CategoryResult for non-Amazon txns.
        skipped: List of skipped transactions.
        unmatched_amazon: List of (txn, reason) tuples for unmatched Amazon txns.
        split_proposals: List of AmazonSplitProposal objects.
        budget_id: YNAB budget ID for metadata.
        since_date: ISO 8601 string (YYYY-MM-DD) for window start.
        days_back: Number of days in working window.
        K: Number of categories used for threshold computation.
        confidence_threshold: Confidence threshold used.
        dump_path: Path to Amazon dump (may be None if no Amazon txns).
        out_dir: Directory for output files (created if missing).
        now: Injected datetime for testing.

    Returns:
        (markdown_path, json_path)
    """
    if now is None:
        now = datetime.now()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now.strftime("%Y%m%d-%H%M%S")
    base_path = out_dir / f"enrich-changeset-{timestamp}"

    # Build JSON payload
    payload = {
        "version": 1,
        "kind": "enrich-changeset",
        "metadata": {
            "timestamp": now.isoformat(),
            "days_back": days_back,
            "since_date": since_date,
            "budget_id": budget_id,
            "dump_path": str(dump_path) if dump_path else None,
            "K": K,
            "confidence_threshold": confidence_threshold,
        },
        "amazon": {
            "splits": [],
            "unmatched_ynab": [],
            "unmatched_shipments": [],
            "excluded_shipments": [],
            "parse_errors": [],
        },
        "non_amazon": {
            "proposals": [],
            "skipped_transfers": [],
        },
    }

    # File paths (collision-safe)
    json_path = _next_free_path(base_path, ".json")
    md_path = json_path.with_suffix(".md")

    # Write JSON
    json_content = json.dumps(payload, default=_json_default, indent=2)
    json_path.write_text(json_content)

    # Write Markdown (basic for now)
    markdown_lines = [
        "# Enrich Changeset",
        "",
        f"## Dump",
        f"Path: {dump_path or '(none)'}",
        "",
        f"## Days back",
        f"{days_back} (since {since_date})",
        "",
        f"## K",
        f"{K} categories | threshold: {confidence_threshold}",
        "",
        f"## Writable txns",
        f"{len(flat_results) + len(split_proposals)} total",
        "",
        f"## Amazon splits",
        f"{len(split_proposals)} item-level splits",
        "",
        f"## Non-Amazon",
        f"{len(flat_results)} categorized",
        "",
        f"## Skipped",
        f"{len(skipped)} transfers/reconciled",
        "",
        f"## Unmatched Amazon txns",
        f"{len(unmatched_amazon)} YNAB",
        "",
        f"## Changeset",
        f"- {md_path.name}",
        f"- {json_path.name}",
    ]

    markdown = "\n".join(markdown_lines)
    md_path.write_text(markdown)

    return md_path, json_path


def main(argv=None):
    """Main entry point for enrich.py.

    Args:
        argv: Optional list of command-line arguments (for testing).

    Returns:
        Exit code: 0 (success), 1 (config/env error), 2 (dump error).
    """
    parser = argparse.ArgumentParser(
        description="Unified categorize+enrich: process all uncategorized YNAB transactions"
    )
    parser.add_argument("--days", type=int, required=True, help="Working window in days back from today")
    parser.add_argument("--dump", type=Path, default=None, help="Override path to Amazon order history zip/dir")
    parser.add_argument("--out-dir", type=Path, default=Path("data/cache"), help="Output directory for changeset files")

    args = parser.parse_args(argv)

    # Load environment
    load_dotenv()
    ynab_token = os.environ.get("YNAB_API_TOKEN")
    if not ynab_token:
        print("Error: YNAB_API_TOKEN not set in .env or environment")
        return 1

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("Error: ANTHROPIC_API_KEY not set in .env or environment")
        return 1

    # Load config
    config_path = Path("config.json")
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        return 1

    try:
        config = json.loads(config_path.read_text())
    except json.JSONDecodeError as e:
        print(f"Error: {config_path} is not valid JSON: {e}")
        return 1

    if "budget_id" not in config:
        print("Error: config.json missing 'budget_id'")
        return 1

    budget_id = config["budget_id"]

    # Calculate date windows
    now = datetime.now()
    since_date = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")
    k_since = (now - timedelta(days=548)).strftime("%Y-%m-%d")

    # Initialize YNAB client
    client = YNABClient(token=ynab_token)

    # YNAB fetches (counted order)
    txns_window, _ = client.get_transactions(budget_id, since_date=since_date)  # call #1
    categories = client.get_categories(budget_id)  # call #2
    accounts = client.get_accounts(budget_id)  # call #3
    k_txns, _ = client.get_transactions(budget_id, since_date=k_since)  # call #4

    # Confidence threshold + recent categories
    K = count_categories_from_transactions(k_txns) or count_categories(categories)
    confidence_threshold = compute_confidence_threshold(K)
    recent_categories = filter_categories_by_usage(categories, k_txns)

    # Filter writable uncategorized
    writable = filter_uncategorized_writable(txns_window)

    # If no work to do, exit early
    if not writable:
        print("No uncategorized writable transactions found.")
        return 0

    # Cache load (and bootstrap if needed)
    cache = load_payee_cache()
    if not cache or cache.get("_migrated_from_v1"):
        all_txns, _ = client.get_transactions(budget_id)  # bootstrap-only fetch (call #5)
        cache = build_cache_from_transactions(all_txns)
        save_payee_cache(cache)

    # Amazon dump (conditional on Amazon payees present)
    match_result = None
    if any(is_amazon_payee(t.get("payee_name")) for t in writable):
        try:
            dump_path = args.dump or find_latest_dump()
        except FileNotFoundError:
            print("Error: No Amazon dump found and Amazon txns present. Use --dump or place dump in data/imports/")
            return 2

        csv_text = extract_order_history_csv(dump_path)
        shipments, parse_errors_list = parse_order_history(csv_text)
        amazon_writable = [t for t in writable if is_amazon_payee(t.get("payee_name"))]
        match_result = match_shipments_to_transactions(
            amazon_writable, shipments,
            parse_errors=parse_errors_list,
            date_window_days=config.get("amazon", {}).get("date_window_days", 3),
        )

    # Categorize (engine handles partition internally)
    flat_results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
        writable, cache, recent_categories, anthropic_key,
        K=K, confidence_threshold=confidence_threshold,
        amazon_matches=match_result,
    )

    # Update cache with Claude-tier results
    update_cache_from_claude_results(cache, writable, flat_results)
    save_payee_cache(cache)

    # TODO: write unified changeset in issue #117
    # TODO: print summary in issue #118

    return 0


if __name__ == "__main__":
    sys.exit(main())
