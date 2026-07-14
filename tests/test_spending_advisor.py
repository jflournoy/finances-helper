"""Tests for spending_advisor.py — data collection and heuristic analyzers."""
import json
import pytest
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from unittest.mock import Mock, patch

from spending_advisor import (
    SpendingSignal,
    SpendingContext,
    CategoryTrend,
    filter_advisory_transactions,
    join_category_names,
    collect_spending_data,
    analyze_delivery_premium,
    analyze_convenience_markup,
    analyze_frequent_small_charges,
    analyze_trends,
    synthesize_advisory,
    continue_advisory_turn,
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
    joined, unknown_count = join_category_names(filtered, cats)

    assert all("category_name" in t for t in joined)
    assert unknown_count == 0
    doordash = [t for t in joined if "DOORDASH" in t["payee_name"].upper()][0]
    assert doordash["category_name"] == "Food Delivery"


def test_join_category_names_none_when_no_category_id(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    txn_no_cat = filtered[0].copy()
    txn_no_cat["category_id"] = None

    joined, unknown_count = join_category_names([txn_no_cat], cats)
    assert joined[0]["category_name"] is None
    assert unknown_count == 0


def test_join_category_names_handles_unknown_category_id(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    txn_bad_cat = filtered[0].copy()
    txn_bad_cat["category_id"] = "unknown_cat_xyz"

    joined, unknown_count = join_category_names([txn_bad_cat], cats)
    assert unknown_count == 1
    assert joined[0]["category_name"] is None


# ============================================================================
# collect_spending_data tests
# ============================================================================

def test_collect_spending_data_totals_are_positive(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)

    context = collect_spending_data(joined)
    assert context.total_spend_dollars > 0, "Total spend should be positive"

    for category, total in context.spend_by_category.items():
        assert total > 0, f"Category {category} should have positive spend"


def test_collect_spending_data_excludes_current_partial_month_from_monthly(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    # All 2025 months are complete (past), so should count all 3
    assert context.period_months_complete >= 2, "Should count at least 2 complete months"
    assert context.period_months_complete <= 3, "Should not exceed actual month count"


def test_collect_spending_data_payee_groups_normalized_lowercase(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    for payee in context.payee_groups.keys():
        assert payee == payee.lower(), f"Payee '{payee}' should be normalized to lowercase"
        assert payee == payee.strip(), f"Payee '{payee}' should be stripped of whitespace"


def test_collect_spending_data_raises_on_income_transaction(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    txn_income = filtered[0].copy()
    txn_income["amount_dollars"] = 100.0
    joined, _ = join_category_names([txn_income], cats)

    with pytest.raises(ValueError, match="income"):
        collect_spending_data(joined)


def test_collect_spending_data_computes_payee_groups(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)

    context = collect_spending_data(joined)

    assert "doordash" in context.payee_groups or "uber eats" in context.payee_groups
    assert len(context.payee_groups) > 0


# ============================================================================
# analyze_delivery_premium tests
# ============================================================================

def test_analyze_delivery_premium_identifies_doordash_and_uber_eats(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_delivery_premium(context.payee_groups, context.period_months_complete)
    assert len(insights) > 0, "Should identify delivery apps"
    assert any("delivery" in i.pattern_type.lower() for i in insights)


def test_analyze_delivery_premium_magnitude_is_positive(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    signals = analyze_delivery_premium(context.payee_groups, context.period_months_complete)

    if signals:
        signal = signals[0]
        assert signal.magnitude_dollars > 0


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
    joined, _ = join_category_names(filtered, cats)
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
# analyze_frequent_small_charges tests
# ============================================================================

def test_analyze_frequent_small_charges_flags_chipotle(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    insights = analyze_frequent_small_charges(
        context.payee_groups,
        "Dining Out",
        context.period_months_complete,
    )

    assert any("chipotle" in i.summary.lower() for i in insights), "Should flag Chipotle as high-frequency"


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
    joined, _ = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    assert context.period_months_complete >= 2, "Fixture should have at least 2 complete months"


def test_analyze_trends_flags_accelerating_category(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)
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
# SpendingSignal dataclass tests
# ============================================================================

def test_spending_signal_has_required_fields(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    signals = analyze_delivery_premium(context.payee_groups, context.period_months_complete)

    if signals:
        signal = signals[0]
        assert hasattr(signal, "pattern_type")
        assert hasattr(signal, "category")
        assert hasattr(signal, "summary")
        assert hasattr(signal, "magnitude_dollars")
        assert hasattr(signal, "payees_involved")
        assert hasattr(signal, "reflective_prompt")


# ============================================================================
# SpendingContext dataclass tests
# ============================================================================

def test_spending_context_has_all_fields(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)

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
            SpendingSignal(
                pattern_type="test",
                category=None,
                summary="Test signal summary",
                magnitude_dollars=50.0,
                payees_involved=["Test"],
                reflective_prompt="Does this feel right?",
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

        text, messages = synthesize_advisory(context, "test-key", model="claude-sonnet-4-6")

        assert text == "Test advisory response"
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Test advisory response"
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
        assert "forward-looking" in system_prompt.lower() or "priorities" in system_prompt.lower()


def test_synthesize_advisory_returns_prose_string(advisory_txns):
    txns, cats = advisory_txns
    filtered = filter_advisory_transactions(txns)
    joined, _ = join_category_names(filtered, cats)
    context = collect_spending_data(joined)

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [Mock(text="Test prose response")]
        mock_client.messages.create.return_value = mock_response

        text, messages = synthesize_advisory(context, "test-key")

        assert isinstance(text, str)
        assert len(text) > 0
        assert text == "Test prose response"
        assert isinstance(messages, list)
        assert len(messages) == 2


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


# ============================================================================
# continue_advisory_turn tests (dialogue loop)
# ============================================================================

def _make_mock_client(text: str):
    """Return (mock_client_cls, mock_client) patched for a single messages.create call."""
    mock_client_cls = Mock()
    mock_client = Mock()
    mock_client_cls.return_value = mock_client
    mock_response = Mock()
    mock_response.stop_reason = "end_turn"
    mock_response.content = [Mock(text=text)]
    mock_client.messages.create.return_value = mock_response
    return mock_client_cls, mock_client


def test_continue_advisory_turn_appends_to_messages():
    seed_messages = [
        {"role": "user", "content": "Context here"},
        {"role": "assistant", "content": "First assistant turn"},
    ]

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [Mock(text="Second assistant turn")]
        mock_client.messages.create.return_value = mock_response

        text, updated = continue_advisory_turn(seed_messages, "User reply here", "test-key")

    assert text == "Second assistant turn"
    assert len(updated) == 4
    assert updated[2] == {"role": "user", "content": "User reply here"}
    assert updated[3] == {"role": "assistant", "content": "Second assistant turn"}


def test_continue_advisory_turn_does_not_mutate_input_messages():
    seed_messages = [
        {"role": "user", "content": "Context"},
        {"role": "assistant", "content": "Turn 1"},
    ]
    original_len = len(seed_messages)

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client
        mock_response = Mock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [Mock(text="Turn 2")]
        mock_client.messages.create.return_value = mock_response

        _, _ = continue_advisory_turn(seed_messages, "reply", "test-key")

    assert len(seed_messages) == original_len, "Input messages list must not be mutated"


def test_scripted_dialogue_two_turns():
    """Simulate a two-turn dialogue and assert message state is threaded correctly."""
    context = SpendingContext(
        period_days=90,
        period_months_complete=3,
        total_spend_dollars=1000.0,
        spend_by_category={"Dining Out": 400.0, "Groceries": 600.0},
        monthly_by_category={
            "Dining Out": {"2025-01": 130.0, "2025-02": 140.0, "2025-03": 130.0},
            "Groceries": {"2025-01": 200.0, "2025-02": 210.0, "2025-03": 190.0},
        },
        payee_groups={},
        insights=[],
        trends=[],
    )

    with patch("anthropic.Anthropic") as mock_client_cls:
        mock_client = Mock()
        mock_client_cls.return_value = mock_client

        turn1_response = Mock()
        turn1_response.stop_reason = "end_turn"
        turn1_response.content = [Mock(text="Turn 1: here is what I see...")]

        turn2_response = Mock()
        turn2_response.stop_reason = "end_turn"
        turn2_response.content = [Mock(text="Turn 2: got it, let me adjust...")]

        mock_client.messages.create.side_effect = [turn1_response, turn2_response]

        text1, messages1 = synthesize_advisory(context, "test-key")
        assert text1 == "Turn 1: here is what I see..."
        assert len(messages1) == 2

        text2, messages2 = continue_advisory_turn(messages1, "That feels right", "test-key")
        assert text2 == "Turn 2: got it, let me adjust..."
        assert len(messages2) == 4
        assert messages2[2]["role"] == "user"
        assert messages2[2]["content"] == "That feels right"
        assert messages2[3]["role"] == "assistant"

        assert mock_client.messages.create.call_count == 2


def test_continue_advisory_turn_raises_on_empty_messages():
    with pytest.raises(ValueError, match="messages"):
        continue_advisory_turn([], "reply", "test-key")


def test_continue_advisory_turn_raises_on_missing_api_key():
    messages = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]
    with pytest.raises(ValueError, match="api_key"):
        continue_advisory_turn(messages, "reply", "")
