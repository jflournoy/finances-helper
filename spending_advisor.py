"""spending_advisor.py — behavioral spending analysis and insights generation."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from pathlib import Path
import anthropic


@dataclass
class SpendingSignal:
    """A neutral behavioral spending pattern with reflective framing."""

    pattern_type: str
    category: str | None
    summary: str
    magnitude_dollars: float
    payees_involved: list[str]
    reflective_prompt: str


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
    insights: list[SpendingSignal] = field(default_factory=list)
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

def analyze_delivery_premium(
    payee_groups: dict[str, list[dict]],
    period_months: int,
) -> list[SpendingSignal]:
    """Identify delivery app spend patterns. Returns one aggregated signal or empty list."""
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
    monthly_avg = total_delivery / period_months

    return [
        SpendingSignal(
            pattern_type="delivery_premium",
            category=None,
            summary=f"{len(delivery_txns)} delivery charges averaging ${monthly_avg:.2f}/month over {period_months} months",
            magnitude_dollars=monthly_avg,
            payees_involved=sorted(list(matching_payees)),
            reflective_prompt="Does this delivery spend feel like it reflects how you want to be living? Or does it feel like more than you intended?",
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

def analyze_convenience_markup(
    payee_groups: dict[str, list[dict]],
    period_months: int,
) -> list[SpendingSignal]:
    """Flag significant spend at convenience stores. Only fires if total > $50/period."""
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

    monthly_avg = total_convenience / period_months

    return [
        SpendingSignal(
            pattern_type="convenience_markup",
            category=None,
            summary=f"{len(convenience_txns)} convenience store charges, ~${monthly_avg:.2f}/month over {period_months} months",
            magnitude_dollars=monthly_avg,
            payees_involved=sorted(list(matching_payees)),
            reflective_prompt="Convenience store visits often reflect a need for quick access — does this feel like a pattern you'd want to budget for intentionally?",
        )
    ]


def analyze_frequent_small_charges(
    payee_groups: dict[str, list[dict]],
    category_name: str,
    period_months: int,
    min_frequency_per_month: float = 3.0,
    max_amount_per_charge: float = 30.0,
) -> list[SpendingSignal]:
    """Flag payees in a category with high-frequency, low-amount charges."""
    if period_months == 0:
        return []

    signals = []

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

            signals.append(
                SpendingSignal(
                    pattern_type="frequency",
                    category=category_name,
                    summary=f"{payee.title()}: {len(category_txns)} charges over {period_months} months (avg ${avg_amount:.2f}, {frequency:.1f}×/month)",
                    magnitude_dollars=monthly_avg,
                    payees_involved=[payee],
                    reflective_prompt=f"You visit {payee.title()} about {frequency:.0f} times a month — does that feel like it matches the role you want this to play in your budget?",
                )
            )

    return signals


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


CONTINUE_SYSTEM_PROMPT = """You are a reflective personal finance advisor continuing a conversation about \
spending patterns and priorities.

You have already shared initial observations and a forward-looking plan. Now you are listening and \
responding to the user's reaction.

Rules:
- Acknowledge what the user said without judgment.
- Adjust the plan in light of their response if they've clarified intent.
- Ask at most ONE follow-up question if something important is still unresolved.
- If the conversation feels complete (user said "done", "looks good", "that's right", etc.), \
summarize the agreed plan and close warmly.
- Never say "you should", "cut", "waste", "overpaid", "savings", or "reduce".
- Under 250 words.
"""

ADVISOR_SYSTEM_PROMPT = """You are a reflective personal finance advisor. Your role is to help the user see \
what their spending patterns suggest about their current priorities, and to explore whether that matches \
the life they intended to budget for.

You are not here to judge. You hold up a mirror, not a scorecard.

For this first turn:
1. Briefly reflect on what the patterns suggest (2-3 sentences, warm and curious).
2. Propose a forward-looking monthly plan number for each major category — framed as a starting \
point to react to, not a number to obey. Use the suggested starting points provided.
3. Ask exactly ONE reflective question to open the conversation.

Rules:
- Never say "you should", "cut", "waste", "overpaid", "savings", or "reduce".
- Never assume a pattern is a problem — it might be exactly what the user wants.
- One question only. The dialogue that follows is where alignment happens.
- Warm, curious, and concise. Under 300 words.
- Honor YNAB's core principle: every dollar is a choice about what matters. \
Your job is alignment between intended priorities and revealed ones.
"""


def _compute_category_medians(monthly_by_category: dict[str, dict[str, float]]) -> dict[str, float]:
    """Compute trailing median of complete months per category as budget starting points."""
    medians: dict[str, float] = {}
    for category, monthly in monthly_by_category.items():
        vals = sorted(monthly.values())
        n = len(vals)
        if n == 0:
            continue
        if n % 2 == 1:
            medians[category] = vals[n // 2]
        else:
            medians[category] = (vals[n // 2 - 1] + vals[n // 2]) / 2
    return medians


def synthesize_advisory(
    context: SpendingContext,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, list[dict]]:
    """Call Claude Sonnet with spending context; return (prose_text, messages).

    Builds a user message summarizing period, total spend, signals, and trends.
    Returns the assistant's first-turn text and the full conversation messages list
    (suitable for passing to continue_advisory_turn). Raises ValueError on empty
    response or max_tokens.
    """
    if not api_key:
        raise ValueError("api_key is required for synthesis")

    # Sort signals by magnitude (descending)
    sorted_signals = sorted(
        context.insights,
        key=lambda s: s.magnitude_dollars,
        reverse=True,
    )

    # Build signals section
    insights_text = "\n".join(
        f"- {s.summary}\n"
        f"  Explore: {s.reflective_prompt}"
        for s in sorted_signals
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

    # Compute per-category medians as plan starting points
    category_medians = _compute_category_medians(context.monthly_by_category)
    medians_text = "\n".join(
        f"- {cat}: ${amt:.2f}/month"
        for cat, amt in sorted(category_medians.items(), key=lambda x: -x[1])
    ) or "(no multi-month data)"

    # Build user message
    user_message = f"""Period: {context.period_days} days ({context.period_months_complete} complete months)
Total Spending: ${context.total_spend_dollars:.2f}

Spending by Category (total over period):
{chr(10).join(f"- {cat}: ${total:.2f}" for cat, total in sorted(context.spend_by_category.items(), key=lambda x: -x[1]))}

Suggested Monthly Budget Starting Points (trailing median of complete months):
{medians_text}

Spending Signals (ranked by monthly magnitude):
{insights_text}{trends_text}
"""

    system_prompt = ADVISOR_SYSTEM_PROMPT

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

    text = response.content[0].text
    messages = [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": text},
    ]
    return text, messages


def continue_advisory_turn(
    messages: list[dict],
    user_response: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> tuple[str, list[dict]]:
    """Send one more user turn and return (assistant_text, updated_messages).

    Appends the user response to messages, calls Claude, then appends the
    assistant reply. Returns the assistant text and the full updated messages list.
    Raises ValueError on empty response or max_tokens.
    """
    if not api_key:
        raise ValueError("api_key is required for advisory continuation")
    if not messages:
        raise ValueError("messages must not be empty — call synthesize_advisory first")

    updated = messages + [{"role": "user", "content": user_response}]

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=CONTINUE_SYSTEM_PROMPT,
        messages=updated,
    )

    if response.stop_reason == "max_tokens":
        raise ValueError("Advisory response was truncated (max_tokens). Increase max_tokens.")

    if not response.content or not response.content[0].text:
        raise ValueError("Claude returned empty advisory response")

    text = response.content[0].text
    updated.append({"role": "assistant", "content": text})
    return text, updated
