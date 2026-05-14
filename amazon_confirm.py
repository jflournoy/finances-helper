"""Amazon matcher changeset loading and confirmation.

Provides tools for loading, validating, and summarizing changesets produced by amazon_matcher.py,
then applying them to YNAB via the update_transaction() write API.
"""
import json
from pathlib import Path
from dataclasses import dataclass
from decimal import Decimal


def load_changeset(path: Path) -> dict:
    """Load and validate a changeset JSON file.

    Validates:
    - version == 1 (raises ValueError if not)
    - required top-level keys: version, generated_at, summary, proposed_splits
    - each proposed_splits[i] has: parent_ynab_transaction_id, parent_ynab_transaction, subtransactions

    Args:
        path: Path to amazon-changeset-*.json

    Returns:
        Parsed changeset dict as-is (no transformation).

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If version is not 1, required keys are missing, or per-proposal fields are missing.
                    Error messages are actionable (include the key name and index).
    """
    with open(path) as f:
        changeset = json.load(f)

    if changeset.get("version") != 1:
        raise ValueError(
            f"unsupported changeset version {changeset.get('version')!r}: only version 1 is supported"
        )

    for key in ("generated_at", "summary", "proposed_splits"):
        if key not in changeset:
            raise ValueError(f"changeset missing required key: {key!r}")

    for i, proposal in enumerate(changeset["proposed_splits"]):
        for field in ("parent_ynab_transaction_id", "parent_ynab_transaction", "subtransactions"):
            if field not in proposal:
                raise ValueError(f"proposed_splits[{i}] missing required field: {field!r}")

    return changeset


@dataclass
class ChangesetSummary:
    """Summary statistics for a changeset."""
    total_proposals: int
    split_proposals: int
    flat_proposals: int
    skipped_uncategorized: int
    applied_previously: int
    total_outflow_dollars: Decimal
    by_category: dict[str, Decimal]


def summarize_changeset(changeset: dict) -> ChangesetSummary:
    """Compute summary statistics from a loaded changeset.

    - total_outflow_dollars: sum abs(proposal['parent_ynab_transaction']['amount']) / 1000
      using Decimal arithmetic (parent amounts are negative milliunits).
    - by_category: for each subtransaction with a non-null category_name,
      add its allocated_amount (Decimal string) to the category bucket.
    - A proposal counted in skipped_uncategorized is still included in
      total_outflow_dollars and split/flat counts.
    """
    total_proposals = 0
    split_proposals = 0
    flat_proposals = 0
    skipped_uncategorized = 0
    applied_previously = 0
    total_outflow = Decimal("0")
    by_category = {}

    for proposal in changeset["proposed_splits"]:
        total_proposals += 1

        if "applied_at" in proposal:
            applied_previously += 1

        parent = proposal["parent_ynab_transaction"]
        amount_milliunits = parent.get("amount", 0)
        outflow_dollars = Decimal(str(abs(amount_milliunits) / 1000))
        total_outflow += outflow_dollars

        subtransactions = proposal.get("subtransactions", [])
        if len(subtransactions) == 1:
            flat_proposals += 1
        elif len(subtransactions) >= 2:
            split_proposals += 1

        has_null_category = any(s.get("category_id") is None for s in subtransactions)
        if has_null_category:
            skipped_uncategorized += 1

        for subtxn in subtransactions:
            category_name = subtxn.get("category_name")
            if category_name:
                allocated_amount = Decimal(str(subtxn.get("allocated_amount", "0")))
                by_category[category_name] = by_category.get(category_name, Decimal("0")) + allocated_amount

    return ChangesetSummary(
        total_proposals=total_proposals,
        split_proposals=split_proposals,
        flat_proposals=flat_proposals,
        skipped_uncategorized=skipped_uncategorized,
        applied_previously=applied_previously,
        total_outflow_dollars=total_outflow,
        by_category=by_category,
    )
