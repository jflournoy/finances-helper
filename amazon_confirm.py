"""Amazon matcher changeset loading and confirmation.

Provides tools for loading, validating, and summarizing changesets produced by amazon_matcher.py,
then applying them to YNAB via the update_transaction() write API.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from ynab_client import YNABClient

if TYPE_CHECKING:
    pass


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


def proposal_to_patch_body(proposal: dict) -> dict | None:
    """Convert a changeset proposed_split entry into a YNAB PATCH body.

    Returns None if the proposal is not applyable (any subtransaction has
    category_id == None). The whole proposal is skipped because YNAB requires
    every subtransaction in a split to have a category.

    Single-subtransaction proposals (len == 1):
        Returns {"category_id": "...", "memo": "<parent memo preserved>"}
        (YNAB rejects subtransactions arrays of length 1.)

    Multi-subtransaction proposals (len >= 2):
        Returns {
            "subtransactions": [
                {"amount": <negative int milliunits>, "category_id": "...", "memo": "..."},
                ...
            ]
        }
    """
    subtransactions = proposal.get("subtransactions", [])

    if any(s.get("category_id") is None for s in subtransactions):
        return None

    if len(subtransactions) == 1:
        subtxn = subtransactions[0]
        parent_memo = proposal["parent_ynab_transaction"].get("memo", "")
        return {
            "category_id": subtxn["category_id"],
            "memo": parent_memo,
        }

    if len(subtransactions) >= 2:
        patch_subtxns = []
        total_amount = 0

        for subtxn in subtransactions:
            allocated_dollars = Decimal(str(subtxn.get("allocated_amount", "0")))
            amount_milliunits = -int(allocated_dollars * 1000)

            item = subtxn.get("item")
            if not isinstance(item, dict):
                raise ValueError(
                    f"subtransaction missing required 'item' dict in proposal "
                    f"{proposal['parent_ynab_transaction_id']}"
                )
            product_name = item["product_name"][:128]
            asin = item["asin"]
            memo = f"{product_name} (ASIN {asin})"[:200]

            patch_subtxns.append({
                "amount": amount_milliunits,
                "category_id": subtxn["category_id"],
                "memo": memo,
            })

            total_amount += amount_milliunits

        parent_amount = proposal["parent_ynab_transaction"]["amount"]
        if total_amount != parent_amount:
            raise ValueError(
                f"Sum invariant violated for txn {proposal['parent_ynab_transaction_id']}: "
                f"subtransactions sum to {total_amount} but parent is {parent_amount}"
            )

        return {"subtransactions": patch_subtxns}

    return None


@dataclass
class ApplyReport:
    """Report from apply_changeset() execution."""
    changeset_path: str
    completed_at: str
    total: int
    applied: list[dict]
    skipped: list[dict]
    failed: list[dict]
    aborted: bool
    abort_reason: str | None = None


def _json_default(obj):
    """JSON encoder for Decimal, date, datetime."""
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def apply_changeset(
    changeset_path: Path,
    client: YNABClient,
    budget_id: str,
    *,
    dry_run: bool = False,
    throttle_seconds: float = 0.5,
    rate_limit_floor: int = 5,
    report_dir: Path = Path("data/cache"),
) -> ApplyReport:
    """Apply changeset proposals to YNAB.

    Args:
        changeset_path: Path to changeset JSON file.
        client: YNABClient instance (sandbox_mode must be False).
        budget_id: YNAB budget ID.
        dry_run: If True, validate but don't apply.
        throttle_seconds: Delay between YNAB API calls.
        rate_limit_floor: Abort if remaining requests fall below this.
        report_dir: Directory for the summary report (created if missing).
            Defaults to data/cache; tests should pass tmp_path.

    Returns:
        ApplyReport with detailed results.

    Raises:
        RuntimeError: If client.sandbox_mode=True or on systemic errors.
    """
    from ynab_client import (
        YNABNotFoundError, YNABConflictError, YNABValidationError,
        YNABRateLimitError, YNABAPIError,
    )

    if client.sandbox_mode:
        raise RuntimeError("apply_changeset() requires sandbox_mode=False. PATCH cannot be redirected.")

    bak_path = changeset_path.with_suffix(".json.bak")
    if not bak_path.exists():
        bak_path.write_text(changeset_path.read_text())

    changeset = load_changeset(changeset_path)
    proposals = changeset.get("proposed_splits", [])

    applied = []
    skipped = []
    failed = []
    aborted = False
    abort_reason = None

    for i, proposal in enumerate(proposals):
        txn_id = proposal["parent_ynab_transaction_id"]

        if "applied_at" in proposal:
            skipped.append({"txn_id": txn_id, "reason": f"already applied at {proposal['applied_at']}"})
            continue

        patch_body = proposal_to_patch_body(proposal)
        if patch_body is None:
            skipped.append({"txn_id": txn_id, "reason": "contains uncategorized items"})
            continue

        if dry_run:
            skipped.append({"txn_id": txn_id, "reason": "dry run"})
            continue

        try:
            client.update_transaction(budget_id, txn_id, patch_body)
            changeset["proposed_splits"][i]["applied_at"] = datetime.now().isoformat()
            changeset_path.write_text(json.dumps(changeset, indent=2, default=_json_default))

            subtxn_count = len(patch_body.get("subtransactions", [1]))
            applied.append({
                "txn_id": txn_id,
                "type": "split" if "subtransactions" in patch_body else "flat",
                "subtxn_count": subtxn_count,
            })
        except YNABRateLimitError as e:
            failed.append({
                "txn_id": txn_id,
                "http_status": 429,
                "error_id": e.id,
                "error_name": e.name,
                "detail": e.detail,
            })
            aborted = True
            abort_reason = "rate limit exceeded"
            break
        except (YNABValidationError, YNABConflictError, YNABNotFoundError) as e:
            status = e.status_code or 500
            failed.append({
                "txn_id": txn_id,
                "http_status": status,
                "error_id": e.id,
                "error_name": e.name,
                "detail": e.detail,
            })
        except YNABAPIError as e:
            failed.append({
                "txn_id": txn_id,
                "http_status": e.status_code or 500,
                "error_id": e.id,
                "error_name": e.name,
                "detail": e.detail,
            })
            aborted = True
            abort_reason = f"unexpected error: {e.name}"
            break

        remaining = client.rate_limit_remaining()
        if remaining is not None and remaining < rate_limit_floor:
            aborted = True
            abort_reason = f"rate limit floor breached: only {remaining} requests remaining"
            break

        time.sleep(throttle_seconds)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"amazon-confirmed-{timestamp}.json"

    report = ApplyReport(
        changeset_path=str(changeset_path),
        completed_at=datetime.now().isoformat(),
        total=len(proposals),
        applied=applied,
        skipped=skipped,
        failed=failed,
        aborted=aborted,
        abort_reason=abort_reason,
    )

    report_path.write_text(json.dumps(asdict(report), indent=2, default=_json_default))

    return report


def _print_summary(changeset: dict, summary: ChangesetSummary, budget_name: str, path: Path) -> None:
    print(f"Changeset:        {path}")
    print(f"Generated:        {changeset.get('generated_at', '?')}")
    print(f"Budget:           {budget_name}")
    print(f"Total proposals:  {summary.total_proposals}")
    print(f"  Splits (>=2):   {summary.split_proposals}")
    print(f"  Flats (1):      {summary.flat_proposals}")
    print(f"  Uncategorized:  {summary.skipped_uncategorized}")
    print(f"  Resume (done):  {summary.applied_previously}")
    print(f"Total outflow:    ${summary.total_outflow_dollars:,.2f}")
    if summary.by_category:
        print("By category:")
        for name, amount in sorted(summary.by_category.items(), key=lambda kv: -kv[1]):
            print(f"  {name:30s} ${amount:>10,.2f}")


def _exit_code_for_report(report: ApplyReport) -> int:
    if report.aborted:
        return 2
    if report.failed:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for applying a changeset to YNAB.

    Exit codes:
      0 — clean run, no failures, no abort
      1 — at least one PATCH failed
      2 — loop aborted (rate limit or systemic error, or sandbox mode set)
      3 — pre-flight failure (missing file, missing env var, etc.)
    """
    parser = argparse.ArgumentParser(
        description="Apply an Amazon changeset to YNAB via PATCH /transactions."
    )
    parser.add_argument("changeset", type=Path, help="path to amazon-changeset-*.json")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; do not call YNAB")
    parser.add_argument("--yes", action="store_true", help="skip interactive confirmation")
    parser.add_argument("--throttle", type=float, default=0.5, help="seconds between PATCH calls")
    args = parser.parse_args(argv)

    if not args.changeset.exists():
        print(f"ERROR: changeset file not found: {args.changeset}", file=sys.stderr)
        return 3

    if os.environ.get("YNAB_SANDBOX_MODE", "").strip() == "1":
        print(
            "ERROR: YNAB_SANDBOX_MODE=1 is set, but the confirm step does not support "
            "sandbox redirect (PATCH operates on a specific transaction ID, which would "
            "404 in the redirected budget). Unset YNAB_SANDBOX_MODE or use a non-sandbox "
            "client pointed at the Sandbox budget directly.",
            file=sys.stderr,
        )
        return 2

    if not os.environ.get("YNAB_API_TOKEN"):
        print("ERROR: YNAB_API_TOKEN not set", file=sys.stderr)
        return 3

    budget_name_or_id = os.environ.get("YNAB_DEFAULT_BUDGET")
    if not budget_name_or_id:
        print("ERROR: YNAB_DEFAULT_BUDGET not set", file=sys.stderr)
        return 3

    from ynab_client import YNABAPIError, YNABRateLimitError

    try:
        client = YNABClient()
        budget_id = client.resolve_budget_id(budget_name_or_id)
        budget = client.get_budget(budget_id)
        budget_name = budget.get("name", budget_name_or_id)
    except YNABRateLimitError as e:
        print(f"ERROR: YNAB rate limit hit while resolving budget ({e.detail or '429'}); try again next hour.", file=sys.stderr)
        return 2
    except YNABAPIError as e:
        if e.status_code == 401:
            print("ERROR: YNAB_API_TOKEN is invalid (401 Unauthorized). Check the token in your .env.", file=sys.stderr)
        elif e.status_code == 404:
            print(f"ERROR: budget {budget_name_or_id!r} not found at YNAB (404). Check YNAB_DEFAULT_BUDGET.", file=sys.stderr)
        else:
            print(f"ERROR: YNAB API error while resolving budget: {e.status_code} {e.name} — {e.detail}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"ERROR: unexpected error contacting YNAB: {type(e).__name__}: {e}", file=sys.stderr)
        return 3

    try:
        changeset = load_changeset(args.changeset)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: invalid changeset: {e}", file=sys.stderr)
        return 3

    summary = summarize_changeset(changeset)
    _print_summary(changeset, summary, budget_name, args.changeset)

    if args.dry_run:
        report = apply_changeset(
            args.changeset, client, budget_id,
            dry_run=True, throttle_seconds=args.throttle,
        )
        print(f"\nDRY RUN — no PATCH calls made.")
        print(f"  Would apply: {len([s for s in report.skipped if s.get('reason') == 'dry run'])}")
        return 0

    if not args.yes:
        applyable = (
            summary.total_proposals
            - summary.applied_previously
            - summary.skipped_uncategorized
        )
        prompt = f"\nApply {applyable} proposals to budget '{budget_name}'? [y/N]: "
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print("Aborted by user.")
            return 0

    report = apply_changeset(
        args.changeset, client, budget_id,
        dry_run=False, throttle_seconds=args.throttle,
    )

    print(f"\nApplied:  {len(report.applied)} of {report.total}")
    print(f"Skipped:  {len(report.skipped)}")
    print(f"Failed:   {len(report.failed)}")
    print(f"Aborted:  {'yes' if report.aborted else 'no'}" + (f" ({report.abort_reason})" if report.aborted else ""))

    return _exit_code_for_report(report)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    sys.exit(main())
