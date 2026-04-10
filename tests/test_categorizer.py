"""Tests for categorizer.py — payee normalization, tier routing."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, Mock
from categorizer import (
    CategoryResult,
    normalize_payee,
    FUZZY_THRESHOLD,
    CLAUDE_BATCH_SIZE,
    load_payee_cache,
    save_payee_cache,
    build_cache_from_transactions,
    history_lookup,
    fuzzy_match,
    claude_categorize,
    categorize_transactions,
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


# Tier 2: fuzzy_match

def test_fuzzy_match_exact_returns_result():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = fuzzy_match("whole foods", cache)
    assert result is not None
    assert result.tier == "fuzzy"


def test_fuzzy_match_close_variant_hits():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = fuzzy_match("whole foods market", cache)
    assert result is not None
    assert result.category_id == "cat1"


def test_fuzzy_match_store_number_still_hits():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = fuzzy_match("whole foods #999", cache)
    assert result is not None
    assert result.category_id == "cat1"


def test_fuzzy_match_below_threshold_returns_none():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = fuzzy_match("xyz abc def", cache)
    assert result is None


def test_fuzzy_match_empty_cache_returns_none():
    result = fuzzy_match("whole foods", {})
    assert result is None


def test_fuzzy_match_confidence_is_fraction():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = fuzzy_match("whole foods market", cache)
    assert 0.0 <= result.confidence <= 1.0
    assert result.confidence > 0.8


def test_fuzzy_match_tier_is_fuzzy():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    result = fuzzy_match("whole foods market", cache)
    assert result.tier == "fuzzy"


def test_fuzzy_match_threshold_boundary():
    cache = {"target": {"category_id": "cat1", "category_name": "Retail"}}
    result = fuzzy_match("target", cache)
    assert result is not None
    assert result.confidence == 1.0


def test_fuzzy_match_with_fixture_cache():
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    result = fuzzy_match("Whole Foods Market #555", cache)
    assert result is not None
    assert result.category_id == "dddddddd-0000-0000-0000-000000000003"


# Tier 3: claude_categorize

def test_claude_categorize_empty_input_returns_empty():
    categories = []
    with patch("categorizer.anthropic.Anthropic") as mock_anthropic:
        result = claude_categorize([], categories, "test-key")
    assert result == []
    # Verify API was not called
    mock_anthropic.assert_not_called()


def test_claude_categorize_parses_valid_response():
    transactions = [
        {
            "id": "txn1",
            "payee_name": "Amazon",
            "amount": -12300,
            "date": "2026-03-20"
        }
    ]
    categories = [
        {
            "id": "cccccccc-0000-0000-0000-000000000002",
            "name": "Online",
            "categories": [
                {"id": "dddddddd-0000-0000-0000-000000000002", "name": "Online Shopping"}
            ]
        }
    ]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "dddddddd-0000-0000-0000-000000000002", "category_name": "Online Shopping", "confidence": 0.9, "rationale": "test"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response

        result = claude_categorize(transactions, categories, "test-key")

    assert len(result) == 1
    assert result[0].transaction_id == "txn1"
    assert result[0].category_id == "dddddddd-0000-0000-0000-000000000002"
    assert result[0].confidence == 0.9


def test_claude_categorize_tier_is_claude():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -12300, "date": "2026-03-20"}]
    categories = [{"id": "g1", "name": "Online", "categories": [{"id": "c1", "name": "Shopping"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        result = claude_categorize(transactions, categories, "test-key")

    assert result[0].tier == "claude"


def test_claude_categorize_raises_on_invalid_json():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -12300, "date": "2026-03-20"}]
    categories = []

    mock_response = Mock()
    mock_response.content = [Mock(text='not valid json {')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        with pytest.raises(ValueError):
            claude_categorize(transactions, categories, "test-key")


def test_claude_categorize_raises_on_length_mismatch():
    transactions = [
        {"id": "txn1", "payee_name": "A", "amount": -100, "date": "2026-03-20"},
        {"id": "txn2", "payee_name": "B", "amount": -200, "date": "2026-03-20"}
    ]
    categories = []

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "A", "category_id": "c1", "category_name": "Cat", "confidence": 0.8, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        with pytest.raises(ValueError):
            claude_categorize(transactions, categories, "test-key")


def test_claude_categorize_uses_haiku_model():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -12300, "date": "2026-03-20"}]
    categories = [{"id": "g1", "name": "Online", "categories": [{"id": "c1", "name": "Shopping"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize(transactions, categories, "test-key")

    call_kwargs = mock_client.messages.create.call_args[1]
    assert call_kwargs["model"] == "claude-haiku-4-5-20251001"


def test_claude_categorize_max_tokens_scales_with_batch_size():
    transactions = [
        {"id": f"txn{i}", "payee_name": f"Store{i}", "amount": -1000, "date": "2026-03-20"}
        for i in range(20)
    ]
    categories = [{"id": "g1", "name": "Online", "categories": [{"id": "c1", "name": "Shopping"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text=json.dumps([
        {"payee_name": f"Store{i}", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r"}
        for i in range(20)
    ]))]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize(transactions, categories, "test-key")

    call_kwargs = mock_client.messages.create.call_args[1]
    # 20 transactions * 100 = 2000, which is > 1024 minimum
    assert call_kwargs["max_tokens"] == 2000


def test_claude_categorize_max_tokens_has_minimum():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -1000, "date": "2026-03-20"}]
    categories = [{"id": "g1", "name": "Online", "categories": [{"id": "c1", "name": "Shopping"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize(transactions, categories, "test-key")

    call_kwargs = mock_client.messages.create.call_args[1]
    # 1 transaction * 100 = 100, but minimum is 1024
    assert call_kwargs["max_tokens"] == 1024


def test_claude_categorize_with_fixture_response():
    fixture = json.loads(Path("data/fixtures/claude_categorize_response.json").read_text())
    categories = json.loads(Path("data/fixtures/ynab_categories.json").read_text())["data"]["category_groups"]
    transactions = json.loads(Path("data/fixtures/ynab_transactions.json").read_text())["data"]["transactions"]

    # Get the uncategorized Amazon transaction
    amazon_txn = next((t for t in transactions if t.get("payee_name") == "Amazon"), None)
    assert amazon_txn is not None

    mock_response = Mock()
    mock_response.content = [Mock(text=fixture["content"][0]["text"])]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        result = claude_categorize([amazon_txn], categories, "test-key")

    assert len(result) == 1
    assert result[0].transaction_id == amazon_txn["id"]
    assert result[0].category_id == "dddddddd-0000-0000-0000-000000000003"


def test_claude_categorize_raises_on_missing_fields():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -12300, "date": "2026-03-20"}]
    categories = [{"id": "g1", "name": "Online", "categories": [{"id": "c1", "name": "Shopping"}]}]

    mock_response = Mock()
    # Missing 'confidence' field
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="missing fields"):
            claude_categorize(transactions, categories, "test-key")


def test_claude_categorize_raises_on_non_array_json():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -12300, "date": "2026-03-20"}]
    categories = []

    mock_response = Mock()
    # Valid JSON but not an array
    mock_response.content = [Mock(text='{"result": "success"}')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="not a JSON array"):
            claude_categorize(transactions, categories, "test-key")


# Orchestrator: categorize_transactions

def test_categorize_transactions_tier1_hit_no_claude_call():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    transactions = [{"id": "txn1", "payee_name": "Whole Foods", "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].tier == "history"
    assert results[0].category_id == "cat1"
    # Verify Claude was not called
    mock_anthropic_class.assert_not_called()


def test_categorize_transactions_tier2_hit_no_claude_call():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    transactions = [{"id": "txn1", "payee_name": "Whole Foods Market #999", "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].tier == "fuzzy"
    mock_anthropic_class.assert_not_called()


def test_categorize_transactions_tier3_called_for_novel_payee():
    cache = {}
    transactions = [{"id": "txn1", "payee_name": "Unknown Store", "amount": -5000, "date": "2026-03-01"}]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Retail"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Unknown Store", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].tier == "claude"
    mock_anthropic_class.assert_called_once()


def test_categorize_transactions_claude_called_once_for_batch():
    cache = {}
    transactions = [
        {"id": "txn1", "payee_name": "Unknown1", "amount": -5000, "date": "2026-03-01"},
        {"id": "txn2", "payee_name": "Unknown2", "amount": -6000, "date": "2026-03-01"},
        {"id": "txn3", "payee_name": "Unknown3", "amount": -7000, "date": "2026-03-01"},
    ]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Retail"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Unknown1", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r"},{"payee_name": "Unknown2", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r"},{"payee_name": "Unknown3", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 3
    # Claude should be called once with all 3 transactions
    mock_anthropic_class.assert_called_once()


def test_categorize_transactions_raises_on_missing_payee_name():
    cache = {}
    transactions = [{"id": "txn1", "payee_name": None, "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with pytest.raises(ValueError):
        categorize_transactions(transactions, cache, categories, "test-key")


def test_categorize_transactions_mixed_tiers():
    cache = {"whole foods": {"category_id": "cat1", "category_name": "Groceries"}}
    transactions = [
        {"id": "txn1", "payee_name": "Whole Foods", "amount": -5000, "date": "2026-03-01"},  # tier 1
        {"id": "txn2", "payee_name": "Unknown Store", "amount": -6000, "date": "2026-03-01"},  # tier 3
    ]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c2", "name": "Retail"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Unknown Store", "category_id": "c2", "category_name": "Retail", "confidence": 0.7, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 2
    # First result is from tier 1, second from tier 3
    tiers = {r.transaction_id: r.tier for r in results}
    assert tiers["txn1"] == "history"
    assert tiers["txn2"] == "claude"


def test_categorize_transactions_full_pipeline_with_fixtures():
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    categories = json.loads(Path("data/fixtures/ynab_categories.json").read_text())["data"]["category_groups"]
    transactions = json.loads(Path("data/fixtures/ynab_transactions.json").read_text())["data"]["transactions"]

    # Use the uncategorized Amazon transaction but modify payee so it's not in cache
    amazon_txn = next((t for t in transactions if t.get("payee_name") == "Amazon"), None)
    assert amazon_txn is not None

    # Create a novel transaction not in cache
    novel_txn = {
        "id": amazon_txn["id"],
        "payee_name": "NewNovelStore",
        "amount": amazon_txn["amount"],
        "date": amazon_txn["date"],
        "category_id": None,
    }

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "NewNovelStore", "category_id": "dddddddd-0000-0000-0000-000000000003", "category_name": "Groceries", "confidence": 0.85, "rationale": "test"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results = categorize_transactions([novel_txn], cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].transaction_id == novel_txn["id"]
    assert results[0].tier == "claude"
    assert results[0].category_id == "dddddddd-0000-0000-0000-000000000003"


def test_categorize_transactions_splits_large_batch():
    # Create 60 transactions, all need Claude (empty cache)
    cache = {}
    transactions = [
        {"id": f"txn{i}", "payee_name": f"Store{i}", "amount": -5000, "date": "2026-03-01"}
        for i in range(60)
    ]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Retail"}]}]

    # Mock response for first batch (50 items)
    first_batch_response = json.dumps([
        {
            "payee_name": f"Store{i}",
            "category_id": "c1",
            "category_name": "Retail",
            "confidence": 0.7,
            "rationale": "r"
        }
        for i in range(50)
    ])

    # Mock response for second batch (10 items)
    second_batch_response = json.dumps([
        {
            "payee_name": f"Store{i}",
            "category_id": "c1",
            "category_name": "Retail",
            "confidence": 0.7,
            "rationale": "r"
        }
        for i in range(50, 60)
    ])

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client

        # Return different responses for each call
        mock_client.messages.create.side_effect = [
            Mock(content=[Mock(text=first_batch_response)]),
            Mock(content=[Mock(text=second_batch_response)]),
        ]

        results = categorize_transactions(transactions, cache, categories, "test-key")

    # Should have made 2 API calls for 60 transactions
    assert mock_client.messages.create.call_count == 2
    assert len(results) == 60
    # Results should be in original order
    assert results[0].transaction_id == "txn0"
    assert results[59].transaction_id == "txn59"


# Additional coverage: edge cases in normalize_payee and build_cache_from_transactions


def test_normalize_payee_raises_on_punctuation_only():
    """Test that a payee that is only punctuation raises ValueError."""
    with pytest.raises(ValueError, match="cannot be empty after normalization"):
        normalize_payee(".,!?;:")


def test_build_cache_from_transactions_date_missing_in_existing_cache():
    """Test the elif branch where new txn has date but cached one doesn't."""
    transactions = [
        {"payee_name": "Whole Foods", "category_id": "cat1", "category_name": "Groceries", "date": None},
        {"payee_name": "Whole Foods", "category_id": "cat2", "category_name": "Other", "date": "2026-01-01"},
    ]
    cache = build_cache_from_transactions(transactions)
    # First txn has no date, second has date. The elif branch (line 151) should trigger and keep the second.
    assert cache["whole foods"]["category_id"] == "cat2"


# CLI main() tests

from categorizer import main


def test_main_raises_when_ynab_token_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("YNAB_API_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with pytest.raises(ValueError, match="YNAB_API_TOKEN"):
                main()


def test_main_raises_when_anthropic_key_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                main()


def test_main_raises_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with pytest.raises(ValueError, match="config.json not found"):
                main()


def test_main_raises_when_budget_id_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({}))

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with pytest.raises(ValueError, match="budget_id not found"):
                main()


def test_main_no_uncategorized_exits_early(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"budget_id": "b1"}))

    mock_client = Mock()
    mock_client.get_transactions.return_value = ([
        {"id": "t1", "payee_name": "X", "category_id": "c1", "amount": -1000, "date": "2026-03-01"}
    ], {})
    mock_client.get_categories.return_value = []

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with patch("ynab_client.YNABClient", return_value=mock_client):
                main()

    assert "No uncategorized transactions found" in capsys.readouterr().out


def test_main_bootstraps_empty_cache(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"budget_id": "b1"}))

    mock_ynab = Mock()
    mock_ynab.get_transactions.side_effect = [
        ([{"id": "t1", "payee_name": "NewStore", "category_id": None, "amount": -5000, "date": "2026-03-01"}], {}),
        ([{"id": "t2", "payee_name": "OldStore", "category_id": "c1", "category_name": "Groceries", "amount": -3000, "date": "2026-02-01"}], {}),
    ]
    mock_ynab.get_categories.return_value = [
        {"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Groceries"}]}
    ]

    mock_claude_result = CategoryResult(
        transaction_id="t1", category_id="c1", category_name="Groceries",
        confidence=0.9, rationale="test", tier="claude"
    )

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with patch("ynab_client.YNABClient", return_value=mock_ynab):
                with patch("categorizer.categorize_transactions", return_value=[mock_claude_result]):
                    main()

    out = capsys.readouterr().out
    assert "Cache is empty" in out
    assert "Proposed categorizations" in out
    assert "Cache updated" in out


def test_main_full_run_with_existing_cache(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"budget_id": "b1"}))

    # Pre-populate cache
    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "payee_lookup.json").write_text(json.dumps({
        "amazon": {"category_id": "c1", "category_name": "Shopping"}
    }))

    mock_ynab = Mock()
    mock_ynab.get_transactions.return_value = ([
        {"id": "t1", "payee_name": "Amazon", "category_id": None, "amount": -5000, "date": "2026-03-01"}
    ], {})
    mock_ynab.get_categories.return_value = [
        {"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Shopping"}]}
    ]

    history_result = CategoryResult(
        transaction_id="t1", category_id="c1", category_name="Shopping",
        confidence=1.0, rationale="Exact history match", tier="history"
    )

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with patch("ynab_client.YNABClient", return_value=mock_ynab):
                with patch("categorizer.categorize_transactions", return_value=[history_result]):
                    main()

    out = capsys.readouterr().out
    assert "Proposed categorizations (1 transactions)" in out
    assert "[HISTORY]" in out
    assert "Cache updated with 0 new payees" in out


def test_main_updates_cache_with_claude_results(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({"budget_id": "b1"}))

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "payee_lookup.json").write_text(json.dumps({}))

    mock_ynab = Mock()
    mock_ynab.get_transactions.side_effect = [
        ([{"id": "t1", "payee_name": "NewPlace", "category_id": None, "amount": -5000, "date": "2026-03-01"}], {}),
        ([{"id": "t2", "payee_name": "OldPlace", "category_id": "c1", "category_name": "Dining", "amount": -3000, "date": "2026-02-01"}], {}),
    ]
    mock_ynab.get_categories.return_value = [
        {"id": "g1", "name": "Food", "categories": [{"id": "c2", "name": "Dining"}]}
    ]

    claude_result = CategoryResult(
        transaction_id="t1", category_id="c2", category_name="Dining",
        confidence=0.85, rationale="restaurant", tier="claude"
    )

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with patch("ynab_client.YNABClient", return_value=mock_ynab):
                with patch("categorizer.categorize_transactions", return_value=[claude_result]):
                    main()

    # Verify cache was updated with the claude result
    saved_cache = json.loads((cache_dir / "payee_lookup.json").read_text())
    assert "newplace" in saved_cache
    assert saved_cache["newplace"]["category_id"] == "c2"
    assert saved_cache["newplace"]["category_name"] == "Dining"

