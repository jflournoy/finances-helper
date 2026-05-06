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


def _print_unified_summary(
    dump_path: Path | None,
    since_date: str,
    days_back: int,
    K: int,
    confidence_threshold: float,
    writable_count: int,
    amazon_splits_count: int,
    non_amazon_count: int,
    skipped_count: int,
    unmatched_amazon_count: int,
    md_path: Path,
    json_path: Path,
) -> None:
    """Print unified summary to stdout."""
    print("\n" + "=" * 60)
    print("Enrich Changeset Summary")
    print("=" * 60)
    print(f"Dump: {dump_path or '(none)'}")
    print(f"Days back: {days_back} (since {since_date})")
    print(f"K: {K} categories | Threshold: {confidence_threshold:.4f}")
    print()
    print(f"Writable: {writable_count} transactions")
    print(f"  Amazon splits: {amazon_splits_count}")
    print(f"  Non-Amazon: {non_amazon_count}")
    print(f"  Skipped: {skipped_count}")
    print(f"  Unmatched: {unmatched_amazon_count}")
    print()
    print(f"Changeset:")
    print(f"  - {md_path.name}")
    print(f"  - {json_path.name}")
    print("=" * 60 + "\n")


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
    source_txns: list[dict],
    budget_id: str,
    since_date: str,
    days_back: int,
    K: int,
    confidence_threshold: float,
    dump_path: Path | None,
    out_dir: Path = Path("data/cache"),
    match_result=None,
    now: datetime | None = None,
) -> tuple[Path, Path]:
    """Write unified enrich changeset to paired markdown + JSON artifacts.

    Args:
        flat_results: List of CategoryResult for non-Amazon txns.
        skipped: List of skipped transactions.
        unmatched_amazon: List of (txn, reason) tuples for unmatched Amazon txns.
        split_proposals: List of AmazonSplitProposal objects.
        source_txns: List of source YNAB transactions (dict) to join proposal metadata.
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

    Raises:
        ValueError: If a proposal references a transaction_id not in source_txns.
    """
    if now is None:
        now = datetime.now()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now.strftime("%Y%m%d-%H%M%S")
    base_path = out_dir / f"enrich-changeset-{timestamp}"

    # Build lookup for source txns by id
    txn_by_id = {t["id"]: t for t in source_txns}

    # Serialize flat results (non-Amazon proposals) — sorted by date then id
    proposals = []
    for result in sorted(flat_results, key=lambda r: (txn_by_id.get(r.transaction_id, {}).get("date", "9999-12-31"), r.transaction_id)):
        txn = txn_by_id.get(result.transaction_id)
        if txn is None:
            raise ValueError(f"Proposal references unknown transaction_id: {result.transaction_id}")
        proposals.append({
            "transaction_id": result.transaction_id,
            "payee_name": txn.get("payee_name"),
            "amount_dollars": txn.get("amount_dollars"),
            "date": txn.get("date"),
            "category_id": result.category_id,
            "category_name": result.category_name,
            "tier": result.tier,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "prior_strength": result.prior_strength,
        })

    # Serialize skipped transactions
    skipped_transfers = [
        {
            "id": t.get("id"),
            "payee_name": t.get("payee_name"),
            "amount_dollars": t.get("amount_dollars"),
            "date": t.get("date"),
        }
        for t in skipped
    ]

    # Serialize unmatched Amazon (txn, reason) tuples — sorted by reason then date then id
    # unmatched_amazon is list of (txn: dict, reason: str) tuples, so x[0] is the txn dict
    unmatched_ynab = []
    for txn, reason in sorted(unmatched_amazon, key=lambda x: (x[1], x[0].get('date', '9999-12-31'), x[0].get('id', ''))):
        unmatched_ynab.append({
            "transaction_id": txn.get("id"),
            "payee_name": txn.get("payee_name"),
            "amount_dollars": txn.get("amount_dollars"),
            "date": txn.get("date"),
            "reason": reason,
        })

    # Serialize split proposals — sorted by parent txn date then id
    splits = []
    for proposal in sorted(split_proposals, key=lambda p: (getattr(p.parent_ynab_txn, 'date', '9999-12-31') if hasattr(p, 'parent_ynab_txn') else '9999-12-31', getattr(p.parent_ynab_txn, 'id', '') if hasattr(p, 'parent_ynab_txn') else '')):
        parent_date = getattr(proposal.parent_ynab_txn, 'date', None) if hasattr(proposal, 'parent_ynab_txn') else None
        parent_id = getattr(proposal.parent_ynab_txn, 'id', None) if hasattr(proposal, 'parent_ynab_txn') else None
        splits.append({
            "transaction_id": parent_id,
            "parent_txn_date": parent_date,
            "order_id": getattr(proposal.shipment, 'order_id', None) if hasattr(proposal, 'shipment') else None,
            "ship_date": str(getattr(proposal.shipment, 'ship_date', None)) if hasattr(proposal, 'shipment') and getattr(proposal.shipment, 'ship_date', None) else None,
            "items": [
                {
                    "description": getattr(item, 'description', None),
                    "category_id": getattr(item, 'category_id', None),
                    "category_name": getattr(item, 'category_name', None),
                }
                for item in (getattr(proposal, 'subtransactions', []) or [])
            ],
        })

    # Serialize match_result if provided
    unmatched_shipments = []
    excluded_shipments = []
    parse_errors = []
    if match_result:
        unmatched_shipments = [
            {
                "order_id": getattr(s, 'order_id', None),
                "ship_date": str(getattr(s, 'ship_date', None)) if getattr(s, 'ship_date', None) else None,
                "total_amount": float(getattr(s, 'total_amount', 0)) if getattr(s, 'total_amount', None) else None,
            }
            for s in sorted(match_result.unmatched_shipments, key=lambda s: (getattr(s, 'order_id', ''), getattr(s, 'ship_date', '')))
        ]
        excluded_shipments = [
            {
                "order_id": getattr(s, 'order_id', None),
                "ship_date": str(getattr(s, 'ship_date', None)) if getattr(s, 'ship_date', None) else None,
                "total_amount": float(getattr(s, 'total_amount', 0)) if getattr(s, 'total_amount', None) else None,
            }
            for s in sorted(match_result.excluded_shipments, key=lambda s: (getattr(s, 'order_id', ''), getattr(s, 'ship_date', '')))
        ]
        def _serialize_parse_error(e):
            if isinstance(e, dict):
                return e
            return {
                "row_index": getattr(e, 'row_index', None),
                "reason": getattr(e, 'reason', None),
            }

        parse_errors = [
            _serialize_parse_error(e)
            for e in sorted(match_result.parse_errors, key=lambda e: getattr(e, 'row_index', e.get('line', 0) if isinstance(e, dict) else 0))
        ]

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
            "splits": splits,
            "unmatched_ynab": unmatched_ynab,
            "unmatched_shipments": unmatched_shipments,
            "excluded_shipments": excluded_shipments,
            "parse_errors": parse_errors,
        },
        "non_amazon": {
            "proposals": proposals,
            "skipped_transfers": skipped_transfers,
        },
    }

    # File paths (collision-safe)
    json_path = _next_free_path(base_path, ".json")
    md_path = json_path.with_suffix(".md")

    # Write JSON
    json_content = json.dumps(payload, default=_json_default, indent=2)
    json_path.write_text(json_content)

    # Write Markdown with per-txn tables
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
    ]

    if split_proposals:
        markdown_lines.extend([
            f"## Amazon splits ({len(split_proposals)} proposed)",
            "| Date | Order ID | Ship Date | Items | Categories |",
            "|------|----------|-----------|-------|------------|",
        ])
        for proposal in sorted(split_proposals, key=lambda p: (getattr(p.parent_ynab_txn, 'date', '9999-12-31') if hasattr(p, 'parent_ynab_txn') else '9999-12-31', getattr(p.parent_ynab_txn, 'id', '') if hasattr(p, 'parent_ynab_txn') else '')):
            parent_date = getattr(proposal.parent_ynab_txn, 'date', '') if hasattr(proposal, 'parent_ynab_txn') else ''
            order_id = getattr(proposal.shipment, 'order_id', '') if hasattr(proposal, 'shipment') else ''
            ship_date = str(getattr(proposal.shipment, 'ship_date', '')) if hasattr(proposal, 'shipment') and getattr(proposal.shipment, 'ship_date', None) else ''
            items = getattr(proposal.shipment, 'items', []) if hasattr(proposal, 'shipment') else []
            item_count = len(items) if items else 0
            categories = ', '.join(_md_escape(getattr(item, 'category_name', '')) for item in items if hasattr(item, 'category_name')) if items else ''
            markdown_lines.append(f"| {parent_date} | {order_id} | {ship_date} | {item_count} | {categories} |")
        markdown_lines.append("")

    if proposals:
        markdown_lines.extend([
            "## Non-Amazon",
            "| Date | Payee | Amount | → Category | Tier | Confidence |",
            "|------|-------|--------|-----------|------|------------|",
        ])
        for p in proposals:
            date_str = p.get("date", "")
            payee_str = _md_escape(p.get("payee_name", ""))
            amount_str = p.get("amount_dollars", "")
            cat_str = _md_escape(p.get("category_name", ""))
            tier_str = p.get("tier", "")
            conf_str = f"{p.get('confidence', 0):.2f}"
            markdown_lines.append(f"| {date_str} | {payee_str} | {amount_str} | {cat_str} | {tier_str} | {conf_str} |")
        markdown_lines.append("")

    if skipped_transfers:
        markdown_lines.extend([
            "## Skipped",
        ])
        for t in skipped_transfers:
            payee = _md_escape(t.get("payee_name", ""))
            amount = t.get("amount_dollars", "")
            date = t.get("date", "")
            markdown_lines.append(f"- {date} {payee} {amount}")
        markdown_lines.append("")

    if unmatched_ynab:
        markdown_lines.extend([
            "## Unmatched Amazon txns",
        ])
        by_reason = {}
        for u in unmatched_ynab:
            reason = u.get("reason", "unknown")
            if reason not in by_reason:
                by_reason[reason] = []
            by_reason[reason].append(u)
        for reason in sorted(by_reason.keys()):
            markdown_lines.append(f"### {reason}")
            for u in by_reason[reason]:
                payee = _md_escape(u.get("payee_name", ""))
                amount = u.get("amount_dollars", "")
                date = u.get("date", "")
                markdown_lines.append(f"- {date} {payee} {amount}")
            markdown_lines.append("")

    markdown_lines.extend([
        "## Changeset",
        f"- {md_path.name}",
        f"- {json_path.name}",
    ])

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

    # Cache load (and bootstrap if needed) — BEFORE early return
    cache = load_payee_cache()
    if not cache or cache.get("_migrated_from_v1"):
        all_txns, _ = client.get_transactions(budget_id)  # bootstrap-only fetch (call #5)
        cache = build_cache_from_transactions(all_txns)
        save_payee_cache(cache)

    # Filter writable uncategorized
    writable = filter_uncategorized_writable(txns_window)

    # If no work to do, exit early
    if not writable:
        print("No uncategorized writable transactions found.")
        return 0

    # Amazon dump (conditional on Amazon payees present)
    match_result = None
    dump_path = None
    shipments = []
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

        # Warn if dump is stale
        warning = _dump_freshness_warning(shipments, since_date, args.days)
        if warning:
            print(f"Warning: {warning}")
            logger.warning(warning)

    # Categorize (engine handles partition internally)
    flat_results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
        writable, cache, recent_categories, anthropic_key,
        K=K, confidence_threshold=confidence_threshold,
        amazon_matches=match_result,
    )

    # Update cache with Claude-tier results
    update_cache_from_claude_results(cache, writable, flat_results)
    save_payee_cache(cache)

    # Write unified changeset
    md_path, json_path = write_unified_changeset(
        flat_results=flat_results,
        skipped=skipped,
        unmatched_amazon=unmatched_amazon,
        split_proposals=split_proposals,
        source_txns=writable,
        budget_id=budget_id,
        since_date=since_date,
        days_back=args.days,
        K=K,
        confidence_threshold=confidence_threshold,
        dump_path=dump_path,
        match_result=match_result,
        out_dir=args.out_dir,
    )

    logger.info(f"Changeset written to {md_path} and {json_path}")

    # Print summary
    _print_unified_summary(
        dump_path=dump_path,
        since_date=since_date,
        days_back=args.days,
        K=K,
        confidence_threshold=confidence_threshold,
        writable_count=len(writable),
        amazon_splits_count=len(split_proposals),
        non_amazon_count=len(flat_results),
        skipped_count=len(skipped),
        unmatched_amazon_count=len(unmatched_amazon),
        md_path=md_path,
        json_path=json_path,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
