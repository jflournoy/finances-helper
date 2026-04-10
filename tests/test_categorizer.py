"""Tests for categorizer.py — payee normalization, tier routing."""
import pytest
from categorizer import CategoryResult, normalize_payee, FUZZY_THRESHOLD, CLAUDE_BATCH_SIZE


# normalization — basic
def test_normalize_payee_strips_whitespace():
    assert normalize_payee("  Starbucks  ") == "starbucks"


def test_normalize_payee_lowercases():
    assert normalize_payee("Netflix") == "netflix"


# normalization — trailing codes
def test_normalize_payee_strips_hash_code():
    assert normalize_payee("WHOLE FOODS #1234") == "whole foods"


def test_normalize_payee_strips_star_code():
    assert normalize_payee("Amazon.com*1A2B3C") == "amazon.com"


def test_normalize_payee_strips_dash_location():
    assert normalize_payee("TRADER JOE'S - Portland") == "trader joe's"


def test_normalize_payee_strips_multiple_codes():
    assert normalize_payee("TRADER JOE'S #123 - Portland") == "trader joe's"


# normalization — no-ops
def test_normalize_payee_no_change_when_clean():
    assert normalize_payee("netflix") == "netflix"


def test_normalize_payee_collapses_spaces():
    assert normalize_payee("Whole  Foods") == "whole foods"


# normalization — error cases
def test_normalize_payee_raises_on_none():
    with pytest.raises(ValueError):
        normalize_payee(None)


def test_normalize_payee_raises_on_empty_string():
    with pytest.raises(ValueError):
        normalize_payee("")


def test_normalize_payee_raises_on_whitespace_only():
    with pytest.raises(ValueError):
        normalize_payee("   ")


# CategoryResult
def test_category_result_fields():
    result = CategoryResult(
        transaction_id="abc123",
        category_id="cat123",
        category_name="Groceries",
        confidence=0.95,
        rationale="test",
        tier="history"
    )
    assert result.transaction_id == "abc123"
    assert result.category_id == "cat123"
    assert result.category_name == "Groceries"
    assert result.confidence == 0.95
    assert result.rationale == "test"
    assert result.tier == "history"


def test_category_result_tier_values():
    for tier in ["history", "fuzzy", "claude"]:
        result = CategoryResult(
            transaction_id="x",
            category_id="c",
            category_name="n",
            confidence=1.0,
            rationale="r",
            tier=tier
        )
        assert result.tier == tier
