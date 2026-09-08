#!/usr/bin/env python3
"""Unified categorize+enrich workflow: single CLI for all uncategorized YNAB txns."""
import sys
import json
import argparse
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
import os
from decimal import Decimal

from ynab_client import YNABClient, filter_uncategorized_writable
from categorizer import (
    load_payee_cache, save_payee_cache, build_cache_from_transactions,
    count_categories_from_transactions, count_categories, compute_confidence_threshold,
    filter_categories_by_usage, categorize_transactions,
)
from amazon_matcher import (
    is_amazon_payee, is_whole_foods_payee, find_latest_dump, extract_order_history_csv,
    parse_order_history, match_shipments_to_transactions, filter_shipments_to_window,
    check_dump_schema_drift,
    _json_default, _md_escape, _next_free_path,
    _merge_order_shipments, _order_rollup_blocker,
)
from category_profiles import (
    load_profiles, save_profiles, build_merchant_map_from_cache,
    sync_merchants_into_profiles, backfill_from_ynab_subtransactions,
    regenerate_stale, count_stale,
)

logger = logging.getLogger(__name__)


def _amount_dollars_str(txn: dict) -> str | None:
    """Format a txn's amount as an exact decimal-dollar string.

    Prefers integer milliunits (`txn["amount"]`) for exact computation. Falls
    back to a pre-existing string `amount_dollars` (used in some test inputs).
    Refuses a float `amount_dollars` — those are the source of the precision
    bug this helper exists to prevent.

    Returns None only if both keys are absent.
    """
    milliunits = txn.get("amount")
    if isinstance(milliunits, int) and not isinstance(milliunits, bool):
        return format(Decimal(milliunits) / Decimal(1000), "f")
    existing = txn.get("amount_dollars")
    if existing is None:
        return None
    if isinstance(existing, str):
        return existing
    raise TypeError(
        f"amount_dollars must be a str or derivable from int milliunits; "
        f"got {type(existing).__name__}={existing!r}. Float values cause "
        f"precision drift — fix the producer."
    )


NEAR_MISS_DATE_WINDOW_DAYS = 3


def _candidate_unmatched_shipments(txn: dict, unmatched_shipments: list) -> list[dict]:
    """List unmatched shipments plausible as a near-miss for `txn`, for a human to pick from.

    Used to give the user near-miss candidates for a YNAB txn with no
    shipment match ("no matching shipment in dump") — e.g. a shipment whose
    cost Amazon split across this card charge and a points/gift-card
    redemption of unpredictable size (seen in practice — a $196.70 shipment
    charged as $193.92 to a card plus $2.78 in Amazon Visa points, a split
    the order-history export has no record of). That gap has no bound, so
    amount can't gate candidacy or rank a single "best" pick reliably.

    Instead: gate on date (±NEAR_MISS_DATE_WINDOW_DAYS — matches the
    matcher's own match window) and on the shipment being otherwise
    unmatched, and return the whole plausible set for a human to read and
    choose from — recall matters more than precision here, since a wrong
    candidate costs one glance (its item list usually makes a false positive
    obvious) while a missing true candidate strands the user back at
    checking Amazon by hand. Whole Foods / Amazon Fresh deliveries are
    excluded: they're routed to Groceries through a separate match-required
    special case (`AmazonShipment.is_wf`) rather than real per-item dollar
    matching, and in practice are large multi-item hauls that pollute
    same-day pools for unrelated small charges without being genuine
    candidates.

    Sorted by date proximity then amount proximity — display order only,
    not a ranking meant to crown a winner.

    Returns [] if there are no unmatched shipments, the txn has no date, or
    none of the unmatched shipments (after excluding WF) ship within the
    date window.
    """
    if not unmatched_shipments:
        return []

    txn_date_str = txn.get("date")
    if not txn_date_str:
        return []
    txn_date = date.fromisoformat(txn_date_str)

    milliunits = txn.get("amount")
    if not (isinstance(milliunits, int) and not isinstance(milliunits, bool)):
        return []
    txn_amount = abs(Decimal(milliunits)) / Decimal(1000)

    def _date_delta_days(shipment):
        if shipment.ship_date is None:
            return None
        return (shipment.ship_date - txn_date).days

    def _shipment_key(shipment):
        return [
            shipment.order_id,
            shipment.ship_date.isoformat(),
            format(shipment.total_amount, "f"),
        ]

    def _as_candidate(shipment, members):
        candidate = {
            "order_id": shipment.order_id,
            "ship_date": shipment.ship_date.isoformat(),
            "amount_dollars": format(shipment.total_amount, "f"),
            "amount_delta_dollars": format(abs(txn_amount - shipment.total_amount), "f"),
            "date_delta_days": _date_delta_days(shipment),
            "parcels": len(members),
        }
        if len(members) > 1:
            # A fused order has no row of its own in the dump, so the reviewer
            # can't look its items up by the candidate's own key. Record the
            # constituent parcels' keys instead — they do have rows.
            candidate["parcel_keys"] = [_shipment_key(m) for m in members]
        return candidate

    eligible = [s for s in unmatched_shipments if not s.is_wf and s.ship_date is not None]

    candidates = [
        _as_candidate(s, [s])
        for s in eligible
        if abs(_date_delta_days(s)) <= NEAR_MISS_DATE_WINDOW_DAYS
    ]

    # Whole-order candidates. An order that ships in several parcels is billed
    # once, so when the charge is a near miss (a points or gift-card
    # redemption the export doesn't record) no single parcel is anywhere near
    # it — the order total is. Offer the same fusion the matcher's Phase 5
    # would have accepted had the amount been exact, so the walk isn't asking
    # the user to pick between two numbers that are both wrong.
    by_order = {}
    for s in eligible:
        by_order.setdefault(s.order_id, []).append(s)

    for order_id in sorted(by_order):
        group = by_order[order_id]
        if len(group) < 2 or _order_rollup_blocker(group) is not None:
            continue
        merged = _merge_order_shipments(group)
        if abs(_date_delta_days(merged)) > NEAR_MISS_DATE_WINDOW_DAYS:
            continue
        candidates.append(_as_candidate(merged, group))

    # Display order only — date proximity, then amount proximity, then order
    # ID to keep repeat runs stable. Not a ranking meant to crown a winner.
    candidates.sort(
        key=lambda c: (
            abs(c["date_delta_days"]),
            Decimal(c["amount_delta_dollars"]),
            c["order_id"],
        )
    )
    return candidates


def _summarize_split_items(subs: list) -> str:
    """Format an Amazon split proposal's subtransactions for the markdown report.

    Groups subtransactions by category and shows the first item's product name
    per group, with a count when more than one. Example output:

        Groceries ×24 (Whole Foods Market Organic Decaf Ground Coffee...),
        Household supplies ×2 (365 Recycled Paper Towels)
    """
    if not subs:
        return ""

    by_cat: dict[str, list] = {}
    order: list[str] = []
    for sub in subs:
        cat = getattr(sub, "category_name", None) or "[UNCATEGORIZED]"
        if cat not in by_cat:
            by_cat[cat] = []
            order.append(cat)
        by_cat[cat].append(sub)

    parts = []
    for cat in order:
        members = by_cat[cat]
        first_item = getattr(members[0], "item", None)
        first_name = (getattr(first_item, "product_name", None) or "").strip() if first_item else ""
        if len(first_name) > 60:
            first_name = first_name[:57] + "..."
        count = len(members)
        cat_md = _md_escape(cat)
        if first_name:
            name_md = _md_escape(first_name)
            parts.append(f"{cat_md} ×{count} ({name_md})" if count > 1 else f"{cat_md} ({name_md})")
        else:
            parts.append(f"{cat_md} ×{count}" if count > 1 else cat_md)
    return ", ".join(parts)


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
    account_name_lookup: dict[str, str] | None = None,
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
        match_result: Optional MatchResult with unmatched_shipments, excluded_shipments, parse_errors.
        account_name_lookup: Optional {account_id: account_name} mapping for resolving
            Amazon split account names when txn lacks account_name.
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
            "amount_dollars": _amount_dollars_str(txn),
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
            "amount_dollars": _amount_dollars_str(t),
            "date": t.get("date"),
        }
        for t in skipped
    ]

    # Serialize unmatched Amazon (txn, reason) tuples — sorted by reason then date then id
    # unmatched_amazon is list of (txn: dict, reason: str) tuples, so x[0] is the txn dict
    unmatched_shipments_for_hints = match_result.unmatched_shipments if match_result else []
    unmatched_ynab = []
    for txn, reason in sorted(unmatched_amazon, key=lambda x: (x[1], x[0].get('date', '9999-12-31'), x[0].get('id', ''))):
        unmatched_ynab.append({
            "transaction_id": txn.get("id"),
            "payee_name": txn.get("payee_name"),
            "amount_dollars": _amount_dollars_str(txn),
            "date": txn.get("date"),
            "memo": txn.get("memo"),
            "reason": reason,
            "candidate_shipments": _candidate_unmatched_shipments(txn, unmatched_shipments_for_hints),
        })

    def _parent_field(parent, field):
        if isinstance(parent, dict):
            return parent.get(field)
        return getattr(parent, field, None)

    # Serialize split proposals — sorted by parent txn date then id
    proposed_splits = []
    for proposal in sorted(
        split_proposals,
        key=lambda p: (
            _parent_field(p.parent_ynab_txn, "date") or "9999-12-31",
            _parent_field(p.parent_ynab_txn, "id") or "",
        ),
    ):
        parent = proposal.parent_ynab_txn
        parent_id = _parent_field(parent, "id")
        parent_date = _parent_field(parent, "date")
        # Resolve account_name (txn → lookup → account_id fallback)
        if isinstance(parent, dict):
            account_name = parent.get("account_name")
            if not account_name and account_name_lookup:
                account_name = account_name_lookup.get(parent.get("account_id"))
            if not account_name:
                account_name = parent.get("account_id")
        else:
            account_name = None

        ship = getattr(proposal, "shipment", None)
        parent_payee = _parent_field(parent, "payee_name")
        include_memo = not is_whole_foods_payee(parent_payee)
        subtransactions = []
        for sub in getattr(proposal, "subtransactions", []) or []:
            item = getattr(sub, "item", None)
            item_block = None
            memo = None
            if item is not None:
                item_block = {
                    "asin": getattr(item, "asin", None),
                    "product_name": getattr(item, "product_name", None),
                    "quantity": getattr(item, "quantity", None),
                }
                if include_memo:
                    product_name = getattr(item, "product_name", None) or ""
                    memo = product_name[:200] if product_name else None
            subtransactions.append({
                "item": item_block,
                "memo": memo,
                "allocated_amount": getattr(sub, "allocated_amount", None),
                "category_id": getattr(sub, "category_id", None),
                "category_name": getattr(sub, "category_name", None),
                "confidence": getattr(sub, "confidence", None),
                "rationale": getattr(sub, "rationale", None),
            })

        proposed_splits.append({
            "transaction_id": parent_id,
            "parent_ynab_transaction": parent if isinstance(parent, dict) else None,
            "account_name": account_name,
            "shipment": {
                "order_id": getattr(ship, "order_id", None) if ship is not None else None,
                "ship_date": getattr(ship, "ship_date", None) if ship is not None else None,
                "payment_method_last4": getattr(ship, "payment_method_last4", None) if ship is not None else None,
                "total_amount": getattr(ship, "total_amount", None) if ship is not None else None,
                "item_count": len(getattr(ship, "items", []) or []) if ship is not None else 0,
            },
            "subtransactions": subtransactions,
        })

    # Serialize match_result if provided
    unmatched_shipments = []
    excluded_shipments = []
    parse_errors = []
    if match_result:
        from datetime import date as _date

        def _shipment_dict(s):
            return {
                "order_id": getattr(s, "order_id", None),
                "ship_date": getattr(s, "ship_date", None),
                "payment_method_last4": getattr(s, "payment_method_last4", None),
                "total_amount": getattr(s, "total_amount", None),
                "item_count": len(getattr(s, "items", []) or []),
            }

        # None-safe sort key matching amazon_matcher's pattern
        _ship_sort = lambda s: (
            getattr(s, "order_id", "") or "",
            getattr(s, "ship_date", None) is None,
            getattr(s, "ship_date", None) or _date.min,
        )

        unmatched_shipments = [
            _shipment_dict(s)
            for s in sorted(match_result.unmatched_shipments, key=_ship_sort)
        ]

        # excluded_shipments is list[tuple[shipment, reason]]
        excluded_shipments = [
            {"shipment": _shipment_dict(s), "reason": reason}
            for s, reason in sorted(
                match_result.excluded_shipments,
                key=lambda x: (
                    getattr(x[0], "order_id", "") or "",
                    getattr(x[0], "ship_date", None) is None,
                    getattr(x[0], "ship_date", None) or _date.min,
                    x[1],
                ),
            )
        ]

        def _serialize_parse_error(e):
            if isinstance(e, dict):
                return e
            return {
                "row_index": getattr(e, "row_index", None),
                "reason": getattr(e, "reason", None),
            }

        def _parse_error_sort_key(e):
            if isinstance(e, dict):
                return e.get("line", e.get("row_index", 0)) or 0
            return getattr(e, "row_index", 0) or 0

        parse_errors = [
            _serialize_parse_error(e)
            for e in sorted(match_result.parse_errors, key=_parse_error_sort_key)
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
            "proposed_splits": proposed_splits,
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
            "| Date | Order ID | Ship Date | Items by category |",
            "|------|----------|-----------|-------------------|",
        ])
        for proposal in sorted(
            split_proposals,
            key=lambda p: (
                _parent_field(p.parent_ynab_txn, "date") or "9999-12-31",
                _parent_field(p.parent_ynab_txn, "id") or "",
            ),
        ):
            parent_date = _parent_field(proposal.parent_ynab_txn, "date") or ""
            ship = getattr(proposal, "shipment", None)
            order_id = getattr(ship, "order_id", "") if ship is not None else ""
            ship_date = str(getattr(ship, "ship_date", "")) if ship is not None and getattr(ship, "ship_date", None) else ""
            subs = getattr(proposal, "subtransactions", []) or []
            items_summary = _summarize_split_items(subs)
            markdown_lines.append(f"| {parent_date} | {order_id} | {ship_date} | {items_summary} |")
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
                txn_date = u.get("date", "")
                memo = u.get("memo")
                line = f"- {txn_date} {payee} {amount}"
                if memo:
                    line += f" — memo: {_md_escape(memo)}"
                markdown_lines.append(line)
                candidates = u.get("candidate_shipments") or []
                for candidate in candidates:
                    parcels = candidate.get("parcels", 1)
                    label = f" [whole order, {parcels} parcels]" if parcels > 1 else ""
                    markdown_lines.append(
                        f"    candidate: order {candidate['order_id']}{label} "
                        f"${candidate['amount_dollars']} shipped {candidate['ship_date']} "
                        f"(Δ${candidate['amount_delta_dollars']}, Δ{candidate['date_delta_days']}d)"
                    )
            markdown_lines.append("")

    markdown_lines.extend([
        "## Changeset",
        f"- {md_path.name}",
        f"- {json_path.name}",
    ])

    markdown = "\n".join(markdown_lines)
    md_path.write_text(markdown)

    return md_path, json_path


def build_profiles(cache, history_txns, anthropic_key, *, regenerate="none"):
    """Load and update the category profile store.

    Category-description generation is an explicit, expensive asset — it is NOT
    triggered by routine enrich runs. The data-sync steps (merchants, exemplar
    backfill) are cheap and always run; whether descriptions get (re)generated is
    controlled by `regenerate`:

    - "none"    — sync data only, never call Claude. Used to load profiles for
                  prompt injection during a normal enrich run.
    - "stale"   — regenerate only categories flagged stale (no description yet,
                  or dirtied by a rejection). The --refresh-profiles path.
    - "all"     — force every category's description to regenerate. The
                  --rebuild-profiles (setup) path.

    Args:
        cache: Payee frequency cache (source of per-category merchants).
        history_txns: Broad transaction list (for exemplar backfill).
        anthropic_key: Anthropic API key (only used when regenerate != "none").
        regenerate: "none" | "stale" | "all".

    Returns:
        The updated profiles dict (already saved to disk).
    """
    if regenerate not in ("none", "stale", "all"):
        raise ValueError(f"regenerate must be none|stale|all, got {regenerate!r}")

    profiles = load_profiles()

    merchant_map = build_merchant_map_from_cache(cache)
    sync_merchants_into_profiles(profiles, merchant_map)

    # Exemplar backfill is skipped ONLY on the "stale" (--refresh-profiles) path.
    # That path consumes the corrections queue (cat["corrections"] = []), which is
    # irreplaceable user evidence, and descriptions are built from exemplar KEYS,
    # not counts (see _bootstrap_descriptions_batch) — so backfilling contributes
    # nothing there while mutating exemplars in the same pass that clears
    # corrections. "all" (--rebuild-profiles) is the setup path and DOES backfill:
    # on a first-ever run the store is empty and exemplars have never been
    # ingested, so skipping it would leave descriptions with no item evidence.
    if regenerate != "stale":
        n_backfilled = backfill_from_ynab_subtransactions(profiles, history_txns)
        if n_backfilled:
            logger.info("Backfilled %d item exemplars from split Amazon history", n_backfilled)

    if regenerate == "all":
        for cat in profiles["categories"].values():
            cat["dirty"] = True
            cat["description"] = ""

    if regenerate in ("stale", "all"):
        regenerated = regenerate_stale(profiles, anthropic_key)
        if regenerated:
            print(f"Generated category descriptions for {len(regenerated)} categories.")
        else:
            print("No categories needed description regeneration.")

    save_profiles(profiles)
    return profiles


def main(argv=None):
    """Main entry point for tag.py.

    Args:
        argv: Optional list of command-line arguments (for testing).

    Returns:
        Exit code: 0 (success), 1 (config/env error), 2 (dump error).
    """
    parser = argparse.ArgumentParser(
        description="Unified categorize+enrich: process all uncategorized YNAB transactions"
    )
    parser.add_argument("--days", type=int, default=None, help="Working window in days back from today")
    parser.add_argument("--dump", type=Path, default=None, help="Override path to Amazon order history zip/dir")
    parser.add_argument("--out-dir", type=Path, default=Path("data/cache"), help="Output directory for changeset files")
    parser.add_argument(
        "--rebuild-profiles",
        action="store_true",
        help="Setup: force-regenerate ALL category profile descriptions from "
             "current merchants/exemplars, then exit without categorizing.",
    )
    parser.add_argument(
        "--refresh-profiles",
        action="store_true",
        help="Regenerate ONLY category descriptions flagged stale (new categories "
             "or ones the user overrode during review), then exit. Cheap.",
    )

    args = parser.parse_args(argv)

    profile_only = args.rebuild_profiles or args.refresh_profiles
    if args.days is None and not profile_only:
        parser.error("--days is required unless running --rebuild-profiles or --refresh-profiles")

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

    budget_name = os.environ.get("YNAB_DEFAULT_BUDGET")
    if not budget_name:
        print("Error: YNAB_DEFAULT_BUDGET not set in .env or environment")
        return 1

    # config.json is optional; if present, only the 'amazon' section is read
    config_path = Path("config.json")
    config = {}
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
        except json.JSONDecodeError as e:
            print(f"Error: {config_path} is not valid JSON: {e}")
            return 1

    # Initialize YNAB client and resolve budget name → UUID
    client = YNABClient(token=ynab_token)
    budget_id = client.resolve_budget_id(budget_name)

    # Profile-only commands (--rebuild-profiles / --refresh-profiles) don't
    # categorize and don't need the windowed fetches. Handle them up front using
    # only the payee cache + full transaction history, then exit.
    if args.rebuild_profiles or args.refresh_profiles:
        cache = load_payee_cache()
        if not cache or cache.get("_migrated_from_v1"):
            all_txns, _ = client.get_transactions(budget_id)
            cache = build_cache_from_transactions(all_txns)
            save_payee_cache(cache)
        else:
            all_txns, _ = client.get_transactions(budget_id)
        mode = "all" if args.rebuild_profiles else "stale"
        build_profiles(cache, all_txns, anthropic_key, regenerate=mode)
        print("Category profiles rebuilt." if args.rebuild_profiles else "Category profiles refreshed.")
        return 0

    # Calculate date windows
    now = datetime.now()
    since_date = (now - timedelta(days=args.days)).strftime("%Y-%m-%d")
    k_since = (now - timedelta(days=548)).strftime("%Y-%m-%d")

    # YNAB fetches (counted order)
    txns_window, _ = client.get_transactions(budget_id, since_date=since_date)  # call #1
    categories = client.get_categories(budget_id)  # call #2
    accounts = client.get_accounts(budget_id)  # call #3
    k_txns, _ = client.get_transactions(budget_id, since_date=k_since)  # call #4
    account_name_lookup = {a["id"]: a["name"] for a in accounts if a.get("id")}

    # Confidence threshold + recent categories
    K = count_categories_from_transactions(k_txns) or count_categories(categories)
    confidence_threshold = compute_confidence_threshold(K)
    recent_categories = filter_categories_by_usage(categories, k_txns)

    # Cache load (and bootstrap if needed) — BEFORE early return
    all_txns = None
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

    has_amazon = any(is_amazon_payee(t.get("payee_name")) for t in writable)

    # Load category profiles to inform BOTH Claude tiers (novel payees and Amazon
    # items) about what each category means in this budget. regenerate="none" so
    # this never calls Claude during a normal run — descriptions are an explicit
    # asset built via --rebuild-profiles / --refresh-profiles. If no descriptions
    # exist yet, format_profiles_for_prompt warns and falls back to bare names.
    profile_history = all_txns if all_txns is not None else k_txns
    profiles = build_profiles(cache, profile_history, anthropic_key, regenerate="none")

    # Nudge: a normal run never regenerates descriptions, so stale ones (new
    # categories, or ones the user overrode during review) silently keep their
    # old meaning until an explicit refresh. Surface that so corrections don't
    # pile up unused.
    n_undescribed, n_dirty = count_stale(profiles)
    if n_dirty:
        print(
            f"Note: {n_dirty} category description(s) are out of date from your "
            f"prior review corrections. Run `tag.py --refresh-profiles` to apply them."
        )
    if n_undescribed:
        print(
            f"Note: {n_undescribed} category(ies) have no learned description yet "
            f"(using bare names). Run `tag.py --rebuild-profiles` to generate them."
        )

    # Amazon dump (conditional on Amazon payees present)
    match_result = None
    dump_path = None
    shipments = []
    if has_amazon:
        try:
            dump_path = args.dump or find_latest_dump()
        except FileNotFoundError:
            print("Error: No Amazon dump found and Amazon txns present. Use --dump or place dump in data/imports/")
            return 2

        csv_text = extract_order_history_csv(dump_path)
        shipments, parse_errors_list = parse_order_history(csv_text)

        baseline_path = Path("data/cache/amazon_known_statuses.json")
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        new_order_st, new_ship_st = check_dump_schema_drift(csv_text, baseline_path)
        if new_order_st or new_ship_st:
            parts = []
            if new_order_st:
                parts.append(f"Order Status: {sorted(new_order_st)}")
            if new_ship_st:
                parts.append(f"Shipment Status: {sorted(new_ship_st)}")
            msg = (
                f"Unknown Amazon status values detected (possible dump-format drift) — "
                + "; ".join(parts)
                + f". Recorded in {baseline_path.name}; will not warn again."
            )
            print(f"Warning: {msg}")
            logger.warning(msg)

        amazon_writable = [t for t in writable if is_amazon_payee(t.get("payee_name"))]
        date_window_days = config.get("amazon", {}).get("date_window_days", 3)

        from datetime import date as _date
        shipments_in_window = filter_shipments_to_window(
            shipments,
            since_date=_date.fromisoformat(since_date),
            date_window_days=date_window_days,
        )

        match_result = match_shipments_to_transactions(
            amazon_writable, shipments_in_window,
            parse_errors=parse_errors_list,
            date_window_days=date_window_days,
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
        profiles=profiles,
    )

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
        account_name_lookup=account_name_lookup,
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
