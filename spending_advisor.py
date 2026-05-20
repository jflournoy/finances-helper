"""spending_advisor.py — behavioral spending analysis and insights generation."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from pathlib import Path
import anthropic
from subscription_classifier import (
    classify_billing_period,
    infer_monthly_cost,
    load_subscription_cache,
    save_subscription_cache,
    update_subscription_cache,
)


@dataclass
class SpendingInsight:
    """A single behavioral spending insight with financial impact estimate."""

    pattern_type: str
    title: str
    estimated_monthly_savings_dollars: float
    payees_involved: list[str]
    evidence: str
    suggested_action: str


@dataclass
class CategoryTrend:
    """Spending trend for a single category across months."""

    category_name: str
    monthly_totals: list[float]
    direction: str
    pct_change_recent: float


@dataclass
class SpendingContext:
    """Aggregated spending data for analysis and synthesis."""

    period_days: int
    period_months_complete: int
    total_spend_dollars: float
    spend_by_category: dict[str, float]
    monthly_by_category: dict[str, dict[str, float]]
    payee_groups: dict[str, list[dict]]
    insights: list[SpendingInsight] = field(default_factory=list)
    trends: list[CategoryTrend] = field(default_factory=list)


def filter_advisory_transactions(transactions: list[dict]) -> list[dict]:
    """Remove transfers, payments, and deleted transactions.

    Keeps reconciled transactions (part of real spend history).
    Removes:
    - transfer_account_id is set
    - payee_name starts with "Transfer :" or "Payment :"
    - deleted == True
    """
    return [
        t
        for t in transactions
        if not t.get("transfer_account_id")
        and not t.get("payee_name", "").startswith(("Transfer :", "Payment :"))
        and not t.get("deleted", False)
    ]


def join_category_names(
    transactions: list[dict],
    flat_categories: dict[str, str],
) -> list[dict]:
    """Return transactions with category_name injected.

    For transactions with no category_id, category_name is None.
    For transactions with unknown category_id (deleted/orphaned), category_name is None.
    Returns tuple (result_transactions, unknown_count) to allow caller to warn.
    """
    result = []
    unknown_count = 0
    for txn in transactions:
        t = txn.copy()
        cat_id = t.get("category_id")

        if cat_id is None:
            t["category_name"] = None
        elif cat_id not in flat_categories:
            t["category_name"] = None
            unknown_count += 1
        else:
            t["category_name"] = flat_categories[cat_id]

        result.append(t)

    return result, unknown_count


def collect_spending_data(transactions: list[dict]) -> SpendingContext:
    """Aggregate transaction data into SpendingContext.

    Computes total spend, spend by category, monthly aggregates, and payee groups.
    Raises ValueError if any transaction has amount_dollars >= 0 (income not spending).
    """
    if not transactions:
        return SpendingContext(
            period_days=0,
            period_months_complete=0,
            total_spend_dollars=0.0,
            spend_by_category={},
            monthly_by_category={},
            payee_groups={},
        )

    # Validate that all amounts are negative (spending)
    for txn in transactions:
        amount = txn.get("amount_dollars", 0)
        if amount >= 0:
            raise ValueError(
                f"Transaction {txn.get('id')} has non-negative amount_dollars={amount}. "
                "Caller should pre-filter out income transactions."
            )

    # Compute period days
    dates = [txn["date"] for txn in transactions]
    min_date = min(dates)
    max_date = max(dates)
    min_dt = datetime.fromisoformat(min_date)
    max_dt = datetime.fromisoformat(max_date)
    period_days = (max_dt - min_dt).days + 1

    # Count complete calendar months (exclude current month only if today is in it)
    today = datetime.now()
    current_month_str = today.strftime("%Y-%m")

    month_strings = set()
    for txn in transactions:
        month_str = txn["date"][:7]
        # Only exclude current month if we're actually in it and have partial data
        if month_str == current_month_str:
            continue
        month_strings.add(month_str)

    # If we found no complete months, check if all transactions are in a single month
    if not month_strings:
        # All transactions are in current month, so 0 complete months
        period_months_complete = 0
    else:
        period_months_complete = len(month_strings)

    # Aggregate by category
    spend_by_category = {}
    monthly_by_category = {}

    for txn in transactions:
        amount = abs(txn["amount_dollars"])
        category = txn.get("category_name")

        if category:
            spend_by_category[category] = spend_by_category.get(category, 0.0) + amount
            month_str = txn["date"][:7]

            if category not in monthly_by_category:
                monthly_by_category[category] = {}

            monthly_by_category[category][month_str] = (
                monthly_by_category[category].get(month_str, 0.0) + amount
            )

    # Total spend
    total_spend = sum(abs(txn["amount_dollars"]) for txn in transactions)

    # Payee groups (normalized lowercase)
    payee_groups = {}
    for txn in transactions:
        payee = txn.get("payee_name", "").lower().strip()
        if payee:
            if payee not in payee_groups:
                payee_groups[payee] = []
            payee_groups[payee].append(txn)

    return SpendingContext(
        period_days=period_days,
        period_months_complete=period_months_complete,
        total_spend_dollars=total_spend,
        spend_by_category=spend_by_category,
        monthly_by_category=monthly_by_category,
        payee_groups=payee_groups,
    )


DELIVERY_APPS = {
    "doordash",
    "uber eats",
    "grubhub",
    "instacart",
    "postmates",
    "seamless",
    "caviar",
    "gopuff",
    "shipt",
}
DELIVERY_MARKUP_RATE = 0.35


def analyze_delivery_premium(
    payee_groups: dict[str, list[dict]],
    period_months: int,
) -> list[SpendingInsight]:
    """Identify delivery app spend and estimate overpay vs. pickup.

    Returns one aggregated insight or empty list.
    """
    if period_months == 0:
        return []

    delivery_txns = []
    matching_payees = set()

    for payee, txns in payee_groups.items():
        for app in DELIVERY_APPS:
            if app in payee:
                delivery_txns.extend(txns)
                matching_payees.add(payee)
                break

    if not delivery_txns:
        return []

    total_delivery = sum(abs(txn["amount_dollars"]) for txn in delivery_txns)
    overpay = total_delivery * DELIVERY_MARKUP_RATE / (1 + DELIVERY_MARKUP_RATE)
    monthly_savings = overpay / period_months

    evidence = f"{len(delivery_txns)} charges totaling ${total_delivery:.2f} over {period_months} months"

    return [
        SpendingInsight(
            pattern_type="delivery_premium",
            title="Delivery app premium",
            estimated_monthly_savings_dollars=monthly_savings,
            payees_involved=sorted(list(matching_payees)),
            evidence=evidence,
            suggested_action=f"Switch to pickup where possible — estimated {DELIVERY_MARKUP_RATE*100:.0f}% savings (~${monthly_savings:.2f}/month)",
        )
    ]


CONVENIENCE_STORES = {
    "cvs",
    "walgreens",
    "rite aid",
    "7-eleven",
    "circle k",
    "casey's",
    "sheetz",
    "wawa",
}
CONVENIENCE_MARKUP_RATE = 0.30


def analyze_convenience_markup(
    payee_groups: dict[str, list[dict]],
    period_months: int,
) -> list[SpendingInsight]:
    """Flag significant spend at convenience stores.

    Only fires if total > $50/period.
    """
    if period_months == 0:
        return []

    convenience_txns = []
    matching_payees = set()

    for payee, txns in payee_groups.items():
        for store in CONVENIENCE_STORES:
            if store in payee:
                convenience_txns.extend(txns)
                matching_payees.add(payee)
                break

    if not convenience_txns:
        return []

    total_convenience = sum(abs(txn["amount_dollars"]) for txn in convenience_txns)

    if total_convenience <= 50:
        return []

    overpay = total_convenience * CONVENIENCE_MARKUP_RATE / (1 + CONVENIENCE_MARKUP_RATE)
    monthly_savings = overpay / period_months

    evidence = f"{len(convenience_txns)} charges totaling ${total_convenience:.2f} over {period_months} months"

    return [
        SpendingInsight(
            pattern_type="convenience_markup",
            title="Convenience store premium",
            estimated_monthly_savings_dollars=monthly_savings,
            payees_involved=sorted(list(matching_payees)),
            evidence=evidence,
            suggested_action=f"Shop at grocery stores instead — estimated {CONVENIENCE_MARKUP_RATE*100:.0f}% savings (~${monthly_savings:.2f}/month)",
        )
    ]


def analyze_subscriptions(
    transactions: list[dict],
    period_months: int,
    cache_path: Path | None = None,
) -> list[SpendingInsight]:
    """Identify likely recurring charges with billing period classification.

    Uses manual subscription list for accuracy instead of temporal pattern matching.
    Falls back to heuristic detection for any subscriptions not in the manual list.

    Args:
        transactions: List of transactions
        period_months: Number of complete months in observation period
        cache_path: Path to subscriptions cache; if None, no caching

    Returns:
        List with single aggregated SpendingInsight or empty list
    """
    if period_months == 0:
        return []

    # Try to load manual subscription list first
    manual_subs_path = Path(__file__).parent / "data" / "cache" / "subscriptions-manual.json"
    manual_subs = {}
    if manual_subs_path.exists():
        import json
        try:
            with open(manual_subs_path) as f:
                manual_data = json.load(f)
                for sub in manual_data.get('active', []):
                    payee_normalized = (sub.get('payee') or '').lower().strip()
                    manual_subs[payee_normalized] = sub
        except Exception:
            pass

    if cache_path is None:
        cache_path = Path(__file__).parent / "data" / "cache" / "subscriptions.json"

    cache = load_subscription_cache(cache_path)

    payee_txns = {}
    for txn in transactions:
        payee = (txn.get("payee_name") or "").lower().strip()
        if payee:
            if payee not in payee_txns:
                payee_txns[payee] = []
            payee_txns[payee].append(txn)

    subscriptions = []

    for payee, txns in payee_txns.items():
        # Skip food categories
        category_name = txns[0].get("category_name") if txns else None
        category = (category_name or "").lower()
        if any(word in category for word in ["dining", "restaurant", "food"]):
            continue

        sorted_txns = sorted(txns, key=lambda t: t["date"])
        amounts = [abs(t["amount_dollars"]) for t in sorted_txns]

        period = None
        monthly_cost = 0
        evidence = ""

        # First, check manual subscription list
        if payee in manual_subs:
            manual_sub = manual_subs[payee]
            monthly_cost = abs(manual_sub.get('monthly_equivalent', 0))
            period = manual_sub.get('frequency', 'unknown')
            evidence = f"Verified subscription ({period}): {manual_sub.get('notes', '')}"
        else:
            # Fall back to heuristic detection
            # Check amount consistency (±$2)
            min_amt = min(amounts)
            max_amt = max(amounts)

            if max_amt - min_amt > 2.0:
                continue

            # Try direct detection: 2+ charges with >=25-day gaps
            if len(txns) >= max(2, period_months - 1):
                valid = True
                for i in range(len(sorted_txns) - 1):
                    date1 = datetime.fromisoformat(sorted_txns[i]["date"])
                    date2 = datetime.fromisoformat(sorted_txns[i + 1]["date"])
                    gap_days = (date2 - date1).days

                    if gap_days < 25:
                        valid = False
                        break

                if valid:
                    # Classify billing period
                    period, avg_gap, min_gap, max_gap = classify_billing_period(
                        payee, sorted_txns
                    )
                    total_cost = sum(amounts)
                    monthly_cost = infer_monthly_cost(total_cost, period, period_months)
                    evidence = f"{len(sorted_txns)} charges at ${min_amt:.2f} ({period}, {avg_gap:.0f}d gap) over {period_months} months"

            # If not detected directly, check cache for billing period hint
            if period is None and payee in cache:
                cached = cache[payee]
                min_amt = min(amounts)
                # Verify the amount is consistent with historical data (±$3)
                if abs(min_amt - cached.amount_usd) <= 3.0:
                    period = cached.billing_period
                    total_cost = sum(amounts)
                    monthly_cost = infer_monthly_cost(total_cost, period, period_months)
                    evidence = f"Detected in cache: {len(sorted_txns)} charges at ${min_amt:.2f} ({period})"

            # Skip if still not classified
            if period is None:
                continue

        subscriptions.append(
            SpendingInsight(
                pattern_type="subscription",
                title=f"{payee.title()} ({period})",
                estimated_monthly_savings_dollars=monthly_cost,
                payees_involved=[payee],
                evidence=evidence,
                suggested_action=f"Cancel if no longer used — would save ${monthly_cost:.2f}/month",
            )
        )

    if not subscriptions:
        save_subscription_cache(cache, cache_path)
        return []

    # Save cache before returning
    save_subscription_cache(cache, cache_path)

    # Aggregate into single insight
    total_monthly = sum(s.estimated_monthly_savings_dollars for s in subscriptions)
    payees = sorted(
        list({p for s in subscriptions for p in s.payees_involved})
    )
    titles = ", ".join(s.title.split(" (")[0].title() for s in subscriptions)
    evidence = f"{len(subscriptions)} recurring subscriptions with combined cost ${total_monthly:.2f}/month"

    return [
        SpendingInsight(
            pattern_type="subscription",
            title=f"Recurring subscriptions: {titles}",
            estimated_monthly_savings_dollars=total_monthly,
            payees_involved=payees,
            evidence=evidence,
            suggested_action="Review subscriptions and cancel ones you no longer use",
        )
    ]


def analyze_frequent_small_charges(
    payee_groups: dict[str, list[dict]],
    category_name: str,
    period_months: int,
    min_frequency_per_month: float = 3.0,
    max_amount_per_charge: float = 30.0,
) -> list[SpendingInsight]:
    """Flag payees in a category with high-frequency, low-amount charges."""
    if period_months == 0:
        return []

    insights = []

    for payee, txns in payee_groups.items():
        category_txns = [
            t for t in txns if t.get("category_name") == category_name
        ]
        if not category_txns:
            continue

        frequency = len(category_txns) / period_months
        avg_amount = sum(abs(t["amount_dollars"]) for t in category_txns) / len(
            category_txns
        )

        if (
            frequency >= min_frequency_per_month
            and avg_amount <= max_amount_per_charge
        ):
            total = sum(abs(t["amount_dollars"]) for t in category_txns)
            monthly_avg = total / period_months
            estimated_savings = min(monthly_avg * 0.25, monthly_avg)

            evidence = f"{len(category_txns)} charges over {period_months} months, avg ${avg_amount:.2f} per charge, freq {frequency:.1f}/month"

            insights.append(
                SpendingInsight(
                    pattern_type="frequency",
                    title=f"{payee.title()} (high-frequency)",
                    estimated_monthly_savings_dollars=estimated_savings,
                    payees_involved=[payee],
                    evidence=evidence,
                    suggested_action=f"Consider reducing frequency — estimated savings ${estimated_savings:.2f}/month",
                )
            )

    return insights


def analyze_trends(
    monthly_by_category: dict[str, dict[str, float]],
    min_monthly_spend: float = 50.0,
) -> list[CategoryTrend]:
    """Detect spending direction in complete calendar months.

    Only uses complete calendar months (not current partial).
    Requires at least 2 complete months.
    """
    today = datetime.now()
    current_month_str = today.strftime("%Y-%m")

    trends = []

    for category, monthly_totals_dict in monthly_by_category.items():
        # Filter to complete months (exclude current month)
        complete_months = {
            m: t for m, t in monthly_totals_dict.items() if m != current_month_str
        }

        if len(complete_months) < 2:
            continue

        sorted_months = sorted(complete_months.keys())
        totals = [complete_months[m] for m in sorted_months]

        # Check average spend threshold
        avg_spend = sum(totals) / len(totals)
        if avg_spend < min_monthly_spend:
            continue

        latest = totals[-1]
        prior = totals[-2]
        pct_change = ((latest - prior) / prior * 100) if prior > 0 else 0

        # Determine direction
        if latest > prior * 1.15 and latest > (sum(totals) / len(totals)) * 1.2:
            direction = "up"
        elif latest < prior * 0.85 and latest < (sum(totals) / len(totals)) * 0.8:
            direction = "down"
        else:
            direction = "flat"

        if direction != "flat":
            trends.append(
                CategoryTrend(
                    category_name=category,
                    monthly_totals=totals,
                    direction=direction,
                    pct_change_recent=pct_change,
                )
            )

    return sorted(trends, key=lambda t: abs(t.pct_change_recent), reverse=True)


ADVISOR_SYSTEM_PROMPT = """You are a personal finance advisor. The user wants to reduce their spending.
You have been given a structured summary of their spending patterns for the last {period_months} months.
Your job is to write a plain-English advisory report.

Rules:
- Lead with the 1-2 highest-impact behavioral changes (by estimated dollar savings).
- Be specific: name the payees, dollar amounts, frequency.
- Be direct: "You spent $X on DoorDash" not "Some charges may indicate delivery usage."
- Estimate total potential monthly savings if the user makes the suggested changes.
- Acknowledge uncertainty where it exists (e.g., convenience store visits may include pharmacy).
- Do NOT lecture about budgeting generally. Focus on what they can change.
- Keep it under 400 words.
"""


def synthesize_advisory(
    context: SpendingContext,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> str:
    """Call Claude Sonnet with spending context; return prose advisory string.

    Builds a user message summarizing period, total spend, top insights, and trends.
    Returns raw prose text (not JSON). Raises ValueError on empty response or
    max_tokens, RuntimeError on API error.
    """
    if not api_key:
        raise ValueError("api_key is required for synthesis")

    # Sort insights by estimated savings (descending)
    sorted_insights = sorted(
        context.insights,
        key=lambda i: i.estimated_monthly_savings_dollars,
        reverse=True,
    )

    # Build insights section
    insights_text = "\n".join(
        f"- {i.title}: ${i.estimated_monthly_savings_dollars:.2f}/month possible savings\n"
        f"  Evidence: {i.evidence}\n"
        f"  Action: {i.suggested_action}"
        for i in sorted_insights
    )

    # Build trends section
    trends_text = ""
    if context.trends:
        trends_text = "\n\nSpending Trends (complete months):\n"
        for trend in context.trends:
            trends_text += (
                f"- {trend.category_name}: {trend.direction.upper()} "
                f"({trend.pct_change_recent:+.1f}%)\n"
            )

    # Build user message
    user_message = f"""Period: {context.period_days} days ({context.period_months_complete} complete months)
Total Spending: ${context.total_spend_dollars:.2f}

Spending by Category:
{chr(10).join(f"- {cat}: ${total:.2f}" for cat, total in sorted(context.spend_by_category.items(), key=lambda x: -x[1]))}

Key Insights (ranked by potential monthly savings):
{insights_text}{trends_text}
"""

    system_prompt = ADVISOR_SYSTEM_PROMPT.format(
        period_months=context.period_months_complete
    )

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    # Validate response
    if response.stop_reason == "max_tokens":
        raise ValueError(
            "Advisory response was truncated (max_tokens). Increase max_tokens."
        )

    if not response.content or not response.content[0].text:
        raise ValueError("Claude returned empty advisory response")

    return response.content[0].text
