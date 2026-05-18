"""Categorization audit script.

Fetches categorized YNAB transactions for a date window, sends them to
Claude Haiku in batches, and flags suspected mis-categorizations.

Output:
  audit-report-<timestamp>.md   — human-readable findings
  audit-findings-<timestamp>.json — machine-readable findings list

Read-only against YNAB. Never writes anything.

Usage:
    uv run python scripts/audit_categorization.py --days 90
    uv run python scripts/audit_categorization.py --days 30 --out-dir /tmp
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Allow importing from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from audit import AuditFinding, audit_batch, filter_auditable
from ynab_client import YNABClient

AUDIT_BATCH_SIZE = 25


def _flat_categories(categories: list[dict]) -> dict:
    """Return {category_id: category_name} from YNAB category group dicts."""
    out = {}
    for group in categories:
        for cat in group.get("categories", []):
            if cat.get("id"):
                out[cat["id"]] = cat.get("name", "")
    return out


def _render_markdown(findings: list[AuditFinding], n_audited: int, days: int) -> str:
    lines = [
        f"# Categorization Audit Report",
        f"",
        f"**Window:** last {days} days  ",
        f"**Transactions audited:** {n_audited}  ",
        f"**Findings:** {len(findings)}",
        f"",
    ]

    if not findings:
        lines += ["No findings — all categorized transactions look correct.", ""]
        return "\n".join(lines)

    # Sort by confidence descending (highest certainty errors first)
    sorted_findings = sorted(findings, key=lambda f: f.confidence, reverse=True)

    lines += ["## Findings", ""]
    for i, f in enumerate(sorted_findings, 1):
        suggestion = ""
        if f.suggested_category_name:
            suggestion = f" → **{f.suggested_category_name}**"
        lines += [
            f"### {i}. {f.payee_name}",
            f"",
            f"- **Date:** {f.date}",
            f"- **Amount:** ${f.amount_dollars:.2f}",
            f"- **Current category:** {f.current_category_name}{suggestion}",
            f"- **Confidence:** {f.confidence:.0%}",
            f"- **Rationale:** {f.rationale}",
            f"- **Transaction ID:** `{f.transaction_id}`",
            f"",
        ]

    return "\n".join(lines)


def _findings_to_json(findings: list[AuditFinding]) -> list[dict]:
    return [
        {
            "transaction_id": f.transaction_id,
            "payee_name": f.payee_name,
            "amount_dollars": f.amount_dollars,
            "date": f.date,
            "current_category_id": f.current_category_id,
            "current_category_name": f.current_category_name,
            "suggested_category_id": f.suggested_category_id,
            "suggested_category_name": f.suggested_category_name,
            "confidence": f.confidence,
            "rationale": f.rationale,
        }
        for f in findings
    ]


def main(argv=None):
    """Main entry point.

    Returns:
        Exit code: 0 (success), 1 (config/env error).
    """
    parser = argparse.ArgumentParser(
        description="Audit YNAB categorized transactions for mis-categorizations"
    )
    parser.add_argument(
        "--days", type=int, required=True,
        help="How many days back to audit (e.g. 90)"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=Path("data/cache"),
        help="Output directory for report files (default: data/cache)"
    )
    parser.add_argument(
        "--budget", type=str, default=None,
        help="Budget name override (default: YNAB_DEFAULT_BUDGET env var)"
    )

    args = parser.parse_args(argv)

    load_dotenv()

    ynab_token = os.environ.get("YNAB_API_TOKEN")
    if not ynab_token:
        print("Error: YNAB_API_TOKEN not set in .env or environment")
        return 1

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("Error: ANTHROPIC_API_KEY not set in .env or environment")
        return 1

    budget_name = args.budget or os.environ.get("YNAB_DEFAULT_BUDGET")
    if not budget_name:
        print("Error: YNAB_DEFAULT_BUDGET not set in .env or environment (or use --budget)")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    since_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print(f"Fetching transactions since {since_date}...")
    client = YNABClient(token=ynab_token)
    budget_id = client.resolve_budget_id(budget_name)
    txns, _ = client.get_transactions(budget_id, since_date=since_date)
    categories = client.get_categories(budget_id)

    auditable = filter_auditable(txns)
    flat_cats = _flat_categories(categories)

    print(f"Fetched {len(txns)} transactions, {len(auditable)} eligible for audit.")

    if not auditable:
        print("No auditable transactions found.")
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        md_path = args.out_dir / f"audit-report-{timestamp}.md"
        json_path = args.out_dir / f"audit-findings-{timestamp}.json"
        md_path.write_text(_render_markdown([], 0, args.days))
        json_path.write_text(json.dumps([], indent=2))
        print(f"Report: {md_path}")
        print(f"Findings: {json_path}")
        return 0

    print(f"Auditing {len(auditable)} transactions in batches of {AUDIT_BATCH_SIZE}...")
    all_findings = []
    for i in range(0, len(auditable), AUDIT_BATCH_SIZE):
        batch = auditable[i:i + AUDIT_BATCH_SIZE]
        batch_num = i // AUDIT_BATCH_SIZE + 1
        total_batches = (len(auditable) + AUDIT_BATCH_SIZE - 1) // AUDIT_BATCH_SIZE
        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} transactions)...")
        findings = audit_batch(batch, flat_cats, anthropic_key)
        all_findings.extend(findings)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = args.out_dir / f"audit-report-{timestamp}.md"
    json_path = args.out_dir / f"audit-findings-{timestamp}.json"

    md_path.write_text(_render_markdown(all_findings, len(auditable), args.days))
    json_path.write_text(json.dumps(_findings_to_json(all_findings), indent=2))

    print(f"\nAudit complete: {len(auditable)} transactions audited, {len(all_findings)} findings.")
    print(f"Report: {md_path}")
    print(f"Findings: {json_path}")

    # Summary by confidence band
    if all_findings:
        high = sum(1 for f in all_findings if f.confidence >= 0.9)
        mid = sum(1 for f in all_findings if 0.7 <= f.confidence < 0.9)
        low = sum(1 for f in all_findings if f.confidence < 0.7)
        print(f"  High confidence (≥90%): {high}")
        print(f"  Medium confidence (70-89%): {mid}")
        print(f"  Low confidence (<70%): {low}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
