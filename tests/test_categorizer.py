"""Tests for categorizer.py — payee normalization, tier routing."""
import json
import pytest
from pathlib import Path
from categorizer import (
    CategoryResult,
    normalize_payee,
    FUZZY_THRESHOLD,
    CLAUDE_BATCH_SIZE,
    load_payee_cache,
    save_payee_cache,
    build_cache_from_transactions,
    history_lookup,
)


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


# Cache I/O

def test_load_payee_cache_returns_empty_dict_when_missing(tmp_path):
    path = tmp_path / "missing_cache.json"
    result = load_payee_cache(str(path))
    assert result == {}


def test_load_payee_cache_loads_fixture():
    result = load_payee_cache("data/fixtures/payee_cache.json")
    assert "whole foods" in result
    assert result["whole foods"]["category_id"] == "dddddddd-0000-0000-0000-000000000003"
    assert result["whole foods"]["category_name"] == "Groceries"


def test_load_payee_cache_raises_on_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not valid json {")
    with pytest.raises(ValueError):
        load_payee_cache(str(path))


def test_save_payee_cache_writes_file(tmp_path):
    cache = {"test": {"category_id": "abc", "category_name": "Test"}}
    path = tmp_path / "cache.json"
    save_payee_cache(cache, str(path))
    assert path.exists()


def test_save_payee_cache_roundtrip(tmp_path):
    original = {
        "whole foods": {"category_id": "id1", "category_name": "Groceries"},
        "amazon": {"category_id": "id2", "category_name": "Shopping"}
    }
    path = tmp_path / "cache.json"
    save_payee_cache(original, str(path))
    loaded = load_payee_cache(str(path))
    assert loaded == original


def test_build_cache_from_transactions_basic():
    transactions = json.loads(Path("data/fixtures/ynab_transactions.json").read_text())["data"]["transactions"]
    cache = build_cache_from_transactions(transactions)
    assert isinstance(cache, dict)
    # Whole Foods is categorized, should be in cache
    assert "whole foods" in cache or len(cache) > 0


def test_build_cache_from_transactions_skips_uncategorized():
    transactions = json.loads(Path("data/fixtures/ynab_transactions.json").read_text())["data"]["transactions"]
    cache = build_cache_from_transactions(transactions)
    # Amazon transaction is uncategorized (category_id=null), should be skipped
    assert "amazon" not in cache


def test_build_cache_from_transactions_skips_no_payee():
    transactions = [
        {"payee_name": None, "category_id": "cat1", "category_name": "Cat1", "date": "2026-03-01"},
        {"payee_name": "Valid", "category_id": "cat2", "category_name": "Cat2", "date": "2026-03-02"},
    ]
    cache = build_cache_from_transactions(transactions)
    assert "valid" in cache
    assert len(cache) == 1


def test_build_cache_from_transactions_uses_most_recent():
    transactions = [
        {"payee_name": "Whole Foods", "category_id": "cat1", "category_name": "OldCat", "date": "2026-03-01"},
        {"payee_name": "Whole Foods", "category_id": "cat2", "category_name": "NewCat", "date": "2026-03-15"},
    ]
    cache = build_cache_from_transactions(transactions)
    assert cache["whole foods"]["category_id"] == "cat2"
    assert cache["whole foods"]["category_name"] == "NewCat"


def test_build_cache_from_transactions_empty_list():
    cache = build_cache_from_transactions([])
    assert cache == {}


# Tier 1: history_lookup

def test_history_lookup_returns_result_on_hit():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = history_lookup("Whole Foods", cache)
    assert result is not None
    assert result.category_id == "cat1"
    assert result.category_name == "Groceries"


def test_history_lookup_returns_none_on_miss():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = history_lookup("Amazon", cache)
    assert result is None


def test_history_lookup_returns_none_on_empty_cache():
    result = history_lookup("Whole Foods", {})
    assert result is None


def test_history_lookup_confidence_is_1_0():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = history_lookup("Whole Foods", cache)
    assert result.confidence == 1.0


def test_history_lookup_tier_is_history():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = history_lookup("Whole Foods", cache)
    assert result.tier == "history"


def test_history_lookup_with_fixture_cache_hit():
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    result = history_lookup("Whole Foods", cache)
    assert result is not None
    assert result.category_id == "dddddddd-0000-0000-0000-000000000003"


def test_history_lookup_with_fixture_cache_miss():
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    result = history_lookup("UnknownPayee", cache)
    assert result is None


def test_history_lookup_normalizes_before_lookup():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = history_lookup("WHOLE FOODS #1234", cache)
    assert result is not None
    assert result.category_id == "cat1"
