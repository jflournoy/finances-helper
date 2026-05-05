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

from ynab_client import YNABClient, filter_uncategorized_writable
from categorizer import (
    load_payee_cache, save_payee_cache, build_cache_from_transactions,
    count_categories_from_transactions, count_categories, compute_confidence_threshold,
    filter_categories_by_usage, categorize_transactions, update_cache_from_claude_results,
)
from amazon_matcher import is_amazon_payee

logger = logging.getLogger(__name__)


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
        # TODO: implement dump loading in issue #116
        pass

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
