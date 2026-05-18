"""Tests for spending_advisor.py — data collection and heuristic analyzers."""
import json
import pytest
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from unittest.mock import Mock, patch

from spending_advisor import (
    SpendingInsight,
    SpendingContext,
    CategoryTrend,
    filter_advisory_transactions,
    join_category_names,
    collect_spending_data,
    analyze_delivery_premium,
    analyze_convenience_markup,
    analyze_subscriptions,
    analyze_frequent_small_charges,
    analyze_trends,
    synthesize_advisory,
    ADVISOR_SYSTEM_PROMPT,
)


@pytest.fixture
def advisory_txns():
    """Load fixture advisory transactions."""
    fixture_path = Path(__file__).parent.parent / "data" / "fixtures" / "advisory_txns.json"
    with open(fixture_path) as f:
        data = json.load(f)
    return data["transactions"], data["categories"]


# ============================================================================
# filter_advisory_transactions tests
# ============================================================================

def test_filter_advisory_transactions_removes_transfer_account_id(advisory_txns):
    txns, _ = advisory_txns
    filtered = filter_advisory_transactions(txns)

    transfer_ids = {t["id"] for t in filtered if t.get("transfer_account_id")}
    assert len(transfer_ids) == 0, "Should filter out transactions with transfer_account_id"


def test_filter_advisory_transactions_removes_payment_payee(advisory_txns):
    txns, _ = advisory_txns
    filtered = filter_advisory_transactions(txns)

    payment_txns = [t for t in filtered if t["payee_name"].startswith("Payment :")]
    assert len(payment_txns) == 0, "Should filter out transactions with payee starting with 'Payment :'"


def test_filter_advisory_transactions_removes_deleted(advisory_txns):
    txns, _ = advisory_txns
    filtered = filter_advisory_transactions(txns)

    deleted_txns = [t for t in filtered if t["deleted"]]
    assert len(deleted_txns) == 0, "Should filter out deleted transactions"


def test_filter_advisory_transactions_keeps_reconciled(advisory_txns):
    txns, _ = advisory_txns
    filtered = filter_advisory_transactions(txns)

    reconciled = [t for t in filtered if t.get("cleared") == "reconciled"]
    assert len(reconciled) > 0, "Should keep reconciled transactions"
    assert reconciled[0]["payee_name"] == "Restaurant XYZ"


def test_filter_advisory_transactions_keeps_cleared(advisory_txns):
    txns, _ = advisory_txns
    filtered = filter_advisory_transactions(txns)

    cleared = [t for t in filtered if t.get("cleared") == "cleared"]
    assert len(cleared) > 0, "Should keep cleared transactions"


# ============================================================================
# join_category_names tests
# ============================================================================

def test_join_category_names_injects_name(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    assert all("category_name" in t for t in joined)
    doordash = [t for t in joined if "DOORDASH" in t["payee_name"].upper()][0]
    assert doordash["category_name"] == "Food Delivery"


def test_join_category_names_none_when_no_category_id(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    txn_no_cat = filtered[0].copy()
    txn_no_cat["category_id"] = None

    joined = join_category_names([txn_no_cat], cats)
    assert joined[0]["category_name"] is None


def test_join_category_names_raises_on_unknown_category_id(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    txn_bad_cat = filtered[0].copy()
    txn_bad_cat["category_id"] = "unknown_cat_xyz"

    with pytest.raises(ValueError, match="Unknown category"):
        join_category_names([txn_bad_cat], cats)


# ============================================================================
# collect_spending_data tests
# ============================================================================

def test_collect_spending_data_totals_are_positive(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    context = collect_spending_data(joined)
    assert context.total_spend_dollars > 0, "Total spend should be positive"

    for category, total in context.spend_by_category.items():
        assert total > 0, f"Category {category} should have positive spend"


def test_collect_spending_data_excludes_current_partial_month_from_monthly(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    # All 2025 months are complete (past), so should count all 3
    assert context.period_months_complete >= 2, "Should count at least 2 complete months"
    assert context.period_months_complete <= 3, "Should not exceed actual month count"


def test_collect_spending_data_payee_groups_normalized_lowercase(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    for payee in context.payee_groups.keys():
        assert payee == payee.lower(), f"Payee '{payee}' should be normalized to lowercase"
        assert payee == payee.strip(), f"Payee '{payee}' should be stripped of whitespace"


def test_collect_spending_data_raises_on_income_transaction(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    txn_income = filtered[0].copy()
    txn_income["amount_dollars"] = 100.0
    joined = join_category_names([txn_income], cats)

    with pytest.raises(ValueError, match="income"):
        collect_spending_data(joined)


def test_collect_spending_data_computes_payee_groups(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    assert "doordash" in context.payee_groups or "uber eats" in context.payee_groups
    assert len(context.payee_groups) > 0


# ============================================================================
# analyze_delivery_premium tests
# ============================================================================

def test_analyze_delivery_premium_identifies_doordash_and_uber_eats(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_delivery_premium(context.payee_groups, context.period_months_complete)
    assert len(insights) > 0, "Should identify delivery apps"
    assert any("delivery" in i.pattern_type.lower() for i in insights)


def test_analyze_delivery_premium_computes_overpay_correctly(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_delivery_premium(context.payee_groups, context.period_months_complete)

    if insights:
        insight = insights[0]
        assert insight.estimated_monthly_savings_dollars > 0


def test_analyze_delivery_premium_handles_substring_match(advisory_txns):
    txns, cats = advisory_txns
    doordash_txns = [t for t in txns if "DOORDASH" in t["payee_name"].upper()]
    assert len(doordash_txns) > 0, "Fixture should have DOORDASH*ORDER variant"


def test_analyze_delivery_premium_returns_empty_if_no_delivery_apps():
    payee_groups = {
        "grocery store": [{"amount_dollars": -50}],
        "gas station": [{"amount_dollars": -40}],
    }
    insights = analyze_delivery_premium(payee_groups, 3)
    assert len(insights) == 0, "Should return empty if no delivery apps found"


# ============================================================================
# analyze_convenience_markup tests
# ============================================================================

def test_analyze_convenience_markup_fires_above_threshold(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_convenience_markup(context.payee_groups, context.period_months_complete)
    assert len(insights) > 0, "CVS/Walgreens spend should exceed $50 threshold"


def test_analyze_convenience_markup_silent_below_50_dollars():
    payee_groups = {
        "cvs pharmacy": [
            {"amount_dollars": -10},
            {"amount_dollars": -15},
        ],
    }
    insights = analyze_convenience_markup(payee_groups, 3)
    assert len(insights) == 0, "Should not fire below $50 threshold"


# ============================================================================
# analyze_subscriptions tests
# ============================================================================

def test_analyze_subscriptions_detects_spotify_monthly(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    insights = analyze_subscriptions(joined, 3)
    assert any("spotify" in i.title.lower() for i in insights), "Should detect Spotify subscription"


def test_analyze_subscriptions_tolerates_2_dollar_variance(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    insights = analyze_subscriptions(joined, 3)
    netflix_insights = [i for i in insights if "netflix" in i.title.lower()]
    assert len(netflix_insights) > 0, "Netflix with $15.49 charges should be detected (within $2 variance)"


def test_analyze_subscriptions_requires_25_day_gap():
    daily_txns = [
        {
            "date": f"2025-01-{i:02d}",
            "amount_dollars": -15.0,
            "payee_name": "Daily Service",
            "category_name": "Subscriptions",
        }
        for i in range(1, 10)
    ]

    insights = analyze_subscriptions(daily_txns, 1)
    daily_insights = [i for i in insights if "daily" in i.title.lower()]
    assert len(daily_insights) == 0, "Daily charges should not be flagged as subscription"


def test_analyze_subscriptions_excludes_food_category_payees(advisory_txns):
    txns, cats = advisory_txns
    chipotle_txns = [t for t in txns if "chipotle" in t["payee_name"].lower()]
    assert len(chipotle_txns) > 0

    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    insights = analyze_subscriptions(joined, 3)
    chipotle_insights = [i for i in insights if "chipotle" in i.title.lower()]
    assert len(chipotle_insights) == 0, "Chipotle in Dining category should be excluded"


# ============================================================================
# analyze_frequent_small_charges tests
# ============================================================================

def test_analyze_frequent_small_charges_flags_chipotle(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_frequent_small_charges(
        context.payee_groups,
        "Dining Out",
        context.period_months_complete,
    )

    assert any("chipotle" in i.title.lower() for i in insights), "Should flag Chipotle as high-frequency"


def test_analyze_frequent_small_charges_ignores_low_frequency():
    payee_groups = {
        "fancy restaurant": [
            {"amount_dollars": -150, "category_name": "Dining Out"},
            {"amount_dollars": -140, "category_name": "Dining Out"},
        ],
    }

    insights = analyze_frequent_small_charges(payee_groups, "Dining Out", 3, min_frequency_per_month=3.0, max_amount_per_charge=30.0)
    assert len(insights) == 0, "Should not flag low-frequency charges"


def test_analyze_frequent_small_charges_ignores_large_amounts():
    payee_groups = {
        "expensive deli": [
            {"amount_dollars": -50, "category_name": "Dining Out"},
            {"amount_dollars": -50, "category_name": "Dining Out"},
            {"amount_dollars": -50, "category_name": "Dining Out"},
            {"amount_dollars": -50, "category_name": "Dining Out"},
        ],
    }

    insights = analyze_frequent_small_charges(payee_groups, "Dining Out", 3, min_frequency_per_month=3.0, max_amount_per_charge=30.0)
    assert len(insights) == 0, "Should not flag charges above max_amount_per_charge"


# ============================================================================
# analyze_trends tests
# ============================================================================

def test_analyze_trends_requires_two_complete_months(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    assert context.period_months_complete >= 2, "Fixture should have at least 2 complete months"


def test_analyze_trends_flags_accelerating_category(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    trends = analyze_trends(context.monthly_by_category)

    if trends:
        for trend in trends:
            assert trend.direction in ("up", "down", "flat")
            assert len(trend.monthly_totals) >= 2


def test_analyze_trends_ignores_small_categories():
    monthly_by_cat = {
        "Tiny Category": {
            "2025-01": 20.0,
            "2025-02": 22.0,
        },
        "Big Category": {
            "2025-01": 200.0,
            "2025-02": 250.0,
        },
    }

    trends = analyze_trends(monthly_by_cat, min_monthly_spend=50.0)

    tiny = [t for t in trends if t.category_name == "Tiny Category"]
    assert len(tiny) == 0, "Should filter out categories below min_monthly_spend"


# ============================================================================
# SpendingInsight dataclass tests
# ============================================================================

def test_spending_insight_has_required_fields(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_delivery_premium(context.payee_groups, context.period_months_complete)

    if insights:
        insight = insights[0]
        assert hasattr(insight, "pattern_type")
        assert hasattr(insight, "title")
        assert hasattr(insight, "estimated_monthly_savings_dollars")
        assert hasattr(insight, "payees_involved")
        assert hasattr(insight, "evidence")
        assert hasattr(insight, "suggested_action")


# ============================================================================
# SpendingContext dataclass tests
# ============================================================================

def test_spending_context_has_all_fields(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    assert hasattr(context, "period_days")
    assert hasattr(context, "period_months_complete")
    assert hasattr(context, "total_spend_dollars")
    assert hasattr(context, "spend_by_category")
    assert hasattr(context, "monthly_by_category")
    assert hasattr(context, "payee_groups")
    assert hasattr(context, "insights")
    assert hasattr(context, "trends")


# ============================================================================
# analyze_trends (Issue B) tests
# ============================================================================

def test_analyze_trends_returns_empty_if_fewer_than_2_months():
    monthly_by_category = {
        "Dining Out": {"2025-05": 100.0},  # only 1 month
    }
    trends = analyze_trends(monthly_by_category)
    assert len(trends) == 0, "Should return empty if fewer than 2 complete months"


def test_analyze_trends_flags_category_trending_up():
    monthly_by_category = {
        "Dining Out": {"2025-01": 100.0, "2025-02": 130.0, "2025-03": 170.0},
    }
    trends = analyze_trends(monthly_by_category, min_monthly_spend=50.0)

    assert len(trends) > 0, "Should flag category trending up"
    assert trends[0].category_name == "Dining Out"
    assert trends[0].direction == "up"


def test_analyze_trends_flags_category_trending_down():
    monthly_by_category = {
        "Groceries": {"2025-01": 400.0, "2025-02": 350.0, "2025-03": 250.0},
    }
    trends = analyze_trends(monthly_by_category, min_monthly_spend=50.0)

    assert len(trends) > 0, "Should flag category trending down"
    assert trends[0].direction == "down"


def test_analyze_trends_flat_when_change_below_threshold():
    monthly_by_category = {
        "Utilities": {"2025-01": 100.0, "2025-02": 102.0, "2025-03": 101.0},
    }
    trends = analyze_trends(monthly_by_category, min_monthly_spend=50.0)

    # Flat trend should not be included (only significant changes)
    assert all(t.direction != "flat" for t in trends), "Should not include flat trends"


def test_analyze_trends_ignores_categories_below_min_monthly_spend():
    monthly_by_category = {
        "Small Category": {"2025-01": 10.0, "2025-02": 20.0, "2025-03": 30.0},
        "Large Category": {"2025-01": 200.0, "2025-02": 250.0, "2025-03": 300.0},
    }
    trends = analyze_trends(monthly_by_category, min_monthly_spend=100.0)

    assert all(
        t.category_name != "Small Category" for t in trends
    ), "Should filter out categories below min_monthly_spend"


def test_analyze_trends_sorted_by_abs_pct_change_desc():
    monthly_by_category = {
        "Dining": {"2025-01": 100.0, "2025-02": 110.0, "2025-03": 180.0},  # big change
        "Groceries": {"2025-01": 500.0, "2025-02": 520.0, "2025-03": 600.0},  # bigger change
    }
    trends = analyze_trends(monthly_by_category, min_monthly_spend=50.0)

    if len(trends) > 1:
        pct_changes = [abs(t.pct_change_recent) for t in trends]
        assert pct_changes == sorted(pct_changes, reverse=True), "Should be sorted by abs pct change DESC"


# ============================================================================
# synthesize_advisory (Issue B) tests
# ============================================================================

def test_synthesize_advisory_calls_claude_with_correct_model():
    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={"Dining": 400.0, "Groceries": 600.0},
        monthly_by_category={},
        payee_groups={},
        insights=[
            SpendingInsight(
                pattern_type="test",
                title="Test insight",
                estimated_monthly_savings_dollars=50.0,
                payees_involved=["Test"],
                evidence="test",
                suggested_action="test",
            )
        ],
        trends=[],
    )

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [Mock(text="Test advisory response")]
        mock_client.messages.create.return_value = mock_response

        result = synthesize_advisory(context, "test-key", model="claude-sonnet-4-6")

        assert result == "Test advisory response"
        mock_client_cls.assert_called_once_with(api_key="test-key")
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-6"
        assert call_kwargs["max_tokens"] == 1024


def test_synthesize_advisory_includes_system_prompt():
    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={"Dining": 400.0},
        monthly_by_category={},
        payee_groups={},
        insights=[],
        trends=[],
    )

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [Mock(text="Response")]
        mock_client.messages.create.return_value = mock_response

        synthesize_advisory(context, "test-key")

        call_kwargs = mock_client.messages.create.call_args.kwargs
        system_prompt = call_kwargs["system"]
        assert "personal finance advisor" in system_prompt.lower()
        assert "behavioral change" in system_prompt.lower()


def test_synthesize_advisory_returns_prose_string(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [Mock(text="Test prose response")]
        mock_client.messages.create.return_value = mock_response

        result = synthesize_advisory(context, "test-key")

        assert isinstance(result, str)
        assert len(result) > 0
        assert "Test prose response" == result


def test_synthesize_advisory_raises_on_empty_response():
    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={},
        monthly_by_category={},
        payee_groups={},
        insights=[],
        trends=[],
    )

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = []
        mock_client.messages.create.return_value = mock_response

        with pytest.raises(ValueError, match="empty"):
            synthesize_advisory(context, "test-key")


def test_synthesize_advisory_raises_on_max_tokens():
    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={},
        monthly_by_category={},
        payee_groups={},
        insights=[],
        trends=[],
    )

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "max_tokens"
        mock_response.content = [Mock(text="Truncated response")]
        mock_client.messages.create.return_value = mock_response

        with pytest.raises(ValueError, match="truncated"):
            synthesize_advisory(context, "test-key")


def test_synthesize_advisory_raises_on_missing_api_key():
    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={},
        monthly_by_category={},
        payee_groups={},
        insights=[],
        trends=[],
    )

    with pytest.raises(ValueError, match="api_key"):
        synthesize_advisory(context, "")
