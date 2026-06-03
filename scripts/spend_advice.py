#!/usr/bin/env python
"""CLI for generating spending advisor reports with behavioral insights."""
import sys
import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from ynab_client import YNABClient
from spending_advisor import (
    filter_advisory_transactions,
    join_category_names,
    collect_spending_data,
    analyze_delivery_premium,
    analyze_convenience_markup,
    analyze_frequent_small_charges,
    analyze_trends,
    synthesize_advisory,
    SpendingContext,
)


def _flatten_categories(categories):
    """Flatten YNAB category group list to {id: name} dict."""
    result = {}
    for group in categories:
        for cat in group.get("categories", []):
            result[cat["id"]] = cat["name"]
    return result


def _render_markdown(ctx: SpendingContext, advisory_text: str) -> str:
    """Render the full advisory report as Markdown."""
    lines = [
        "# Spending Advisory Report",
        "",
        f"**Period:** {ctx.period_days} days ({ctx.period_months_complete} complete months)",
        f"**Total Spending:** ${ctx.total_spend_dollars:,.2f}",
        "",
        "## Your Personal Advisory",
        "",
        advisory_text,
        "",
    ]

    # Insights section
    if ctx.insights:
        lines.append("## Insights")
        lines.append("")
        sorted_insights = sorted(
            ctx.insights,
            key=lambda i: i.estimated_monthly_savings_dollars,
            reverse=True,
        )
        for insight in sorted_insights:
            lines.append(f"### {insight.title}")
            lines.append("")
            lines.append(f"**Estimated Monthly Savings:** ${insight.estimated_monthly_savings_dollars:.2f}")
            lines.append("")
            lines.append(f"**Evidence:** {insight.evidence}")
            lines.append("")
            lines.append(f"**Action:** {insight.suggested_action}")
            lines.append("")

    # Trends section
    if ctx.trends:
        lines.append("## Spending Trends")
        lines.append("")
        direction_symbols = {"up": "↑", "down": "↓", "flat": "→"}
        for trend in ctx.trends:
            symbol = direction_symbols.get(trend.direction, "→")
            lines.append(
                f"- **{trend.category_name}** {symbol} ({trend.pct_change_recent:+.1f}%)"
            )
        lines.append("")

    # Category totals
    if ctx.spend_by_category:
        lines.append("## Category Totals")
        lines.append("")
        lines.append("| Category | Total |")
        lines.append("|----------|-------|")
        for cat, total in sorted(
            ctx.spend_by_category.items(), key=lambda x: -x[1]
        ):
            lines.append(f"| {cat} | ${total:,.2f} |")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    """Run the spending advisor CLI."""
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Generate a spending advisor report with behavioral insights."
    )
    parser.add_argument(
        "--days",
        type=int,
        required=True,
        help="How many days back to fetch (e.g., 90)",
    )
    parser.add_argument(
        "--out-dir",
        default="data/cache",
        help="Output directory for reports (default: data/cache)",
    )
    parser.add_argument(
        "--budget",
        default=None,
        help="Budget name (default: YNAB_DEFAULT_BUDGET env var)",
    )

    args = parser.parse_args()

    # Validate environment
    api_token = os.getenv("YNAB_API_TOKEN")
    if not api_token:
        print("Error: YNAB_API_TOKEN not set in environment", file=sys.stderr)
        return 1

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key:
        print("Error: ANTHROPIC_API_KEY not set in environment", file=sys.stderr)
        return 1

    # Create output directory
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Initialize YNAB client
    client = YNABClient(api_token)

    # Resolve budget
    budget_name = args.budget or os.getenv("YNAB_DEFAULT_BUDGET")
    try:
        budget_id = client.resolve_budget_id(budget_name)
    except Exception as e:
        print(f"Error resolving budget: {e}", file=sys.stderr)
        return 1

    # Fetch data
    since_date = (datetime.now() - timedelta(days=args.days)).isoformat()
    try:
        txns, _ = client.get_transactions(budget_id, since_date=since_date)
        categories = client.get_categories(budget_id)
    except Exception as e:
        print(f"Error fetching YNAB data: {e}", file=sys.stderr)
        return 1

    flat_categories = _flatten_categories(categories)

    # Process transactions
    # Filter to outflows only (spending, where amount_dollars < 0)
    outflow_txns = [t for t in txns if t.get("amount_dollars", 0) < 0]
    filtered_txns = filter_advisory_transactions(outflow_txns)
    joined_txns, unknown_cat_count = join_category_names(filtered_txns, flat_categories)
    if unknown_cat_count > 0:
        print(
            f"Warning: {unknown_cat_count} transaction(s) have orphaned/deleted category IDs (will be excluded from category analysis)",
            file=sys.stderr,
        )
    context = collect_spending_data(joined_txns)

    # Run analyzers
    insights = []
    insights += analyze_delivery_premium(
        context.payee_groups, context.period_months_complete
    )
    insights += analyze_convenience_markup(
        context.payee_groups, context.period_months_complete
    )

    # Frequent small charges for dining/food categories
    for cat_name in context.spend_by_category:
        if any(
            kw in cat_name.lower()
            for kw in ("dining", "restaurant", "food")
        ):
            insights += analyze_frequent_small_charges(
                context.payee_groups,
                cat_name,
                context.period_months_complete,
            )

    trends = analyze_trends(context.monthly_by_category)

    # Sort insights and attach to context
    insights.sort(
        key=lambda i: i.estimated_monthly_savings_dollars, reverse=True
    )
    context.insights = insights
    context.trends = trends

    # Warn if limited trend data
    if context.period_months_complete < 2:
        print(
            f"Warning: Only {context.period_months_complete} complete month(s) available. "
            "Trend analysis requires at least 2 complete months.",
            file=sys.stderr,
        )

    # Call Claude
    try:
        advisory_text = synthesize_advisory(context, anthropic_key)
    except Exception as e:
        print(f"Error synthesizing advisory: {e}", file=sys.stderr)
        return 1

    # Render and write outputs
    markdown_text = _render_markdown(context, advisory_text)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    md_path = out_dir / f"spend-advice-{timestamp}.md"
    json_path = out_dir / f"spend-advice-{timestamp}.json"

    with open(md_path, "w") as f:
        f.write(markdown_text)

    json_data = {
        "generated_at": datetime.now().isoformat(),
        "period_days": context.period_days,
        "period_months_complete": context.period_months_complete,
        "total_spend_dollars": context.total_spend_dollars,
        "insights": [
            {
                "pattern_type": i.pattern_type,
                "title": i.title,
                "estimated_monthly_savings_dollars": i.estimated_monthly_savings_dollars,
                "payees_involved": i.payees_involved,
                "evidence": i.evidence,
                "suggested_action": i.suggested_action,
            }
            for i in context.insights
        ],
        "trends": [
            {
                "category_name": t.category_name,
                "monthly_totals": t.monthly_totals,
                "direction": t.direction,
                "pct_change_recent": t.pct_change_recent,
            }
            for t in context.trends
        ],
    }

    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2)

    # Print summary
    total_savings = sum(i.estimated_monthly_savings_dollars for i in context.insights)
    print()
    print(f"Analyzed {len(joined_txns)} transactions over {context.period_months_complete} complete months (${context.total_spend_dollars:,.2f} total)")
    print(f"Found {len(context.insights)} insights — estimated ${total_savings:.2f}/month in potential savings")
    print(f"Report: {md_path}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
