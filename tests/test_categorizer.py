"""Tests for categorizer.py — payee normalization, tier routing."""
import json
import pytest
import copy
from pathlib import Path
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
from unittest.mock import patch, Mock
from categorizer import (
    CategoryResult,
    ItemCategoryResult,
    AmazonSplitProposal,
    normalize_payee,
    fuzzy_score,
    FUZZY_THRESHOLD,
    CLAUDE_BATCH_SIZE,
    load_payee_cache,
    save_payee_cache,
    build_cache_from_transactions,
    history_lookup,
    fuzzy_match,
    claude_categorize,
    categorize_transactions,
    count_categories,
    compute_confidence,
    record_categorization,
    update_cache_from_claude_results,
    _dominant_category,
    _resolve_alias,
    normalize_import_payee,
    count_categories_from_transactions,
    compute_confidence_threshold,
    filter_categories_by_usage,
    MIN_OBSERVATIONS,
)
from amazon_matcher import MatchResult, is_amazon_payee


def _v2_entry(cat_id, cat_name, count=1):
    """Helper to create a v2 frequency cache entry for tests."""
    return {"total": count, "categories": {cat_id: {"name": cat_name, "count": count}}}


def _v2_cache(entries):
    """Helper to create a v2 cache from a dict of {payee: (cat_id, cat_name, count)}."""
    cache = {"_version": 2}
    for payee, (cat_id, cat_name, *rest) in entries.items():
        count = rest[0] if rest else 1
        cache[payee] = _v2_entry(cat_id, cat_name, count)
    return cache


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
    assert normalize_payee("TRADER JOE'S - Portland") == "trader joe's - portland"


def test_normalize_payee_strips_multiple_codes():
    assert normalize_payee("TRADER JOE'S #123 - Portland") == "trader joe's #123 - portland"


def test_normalize_payee_preserves_transfer_with_account_number():
    assert normalize_payee("Transfer : Classic Checking -- 6190") == "transfer : classic checking"


def test_normalize_payee_preserves_transfer_with_card_number():
    assert normalize_payee("Transfer : Alaska Airlines Visa Signature - 1783") == "transfer : alaska airlines visa signature"


def test_normalize_payee_preserves_transfer_no_trailing_code():
    assert normalize_payee("Transfer : Delta SkyMiles Platinum") == "transfer : delta skymiles platinum"


def test_normalize_payee_preserves_dash_word_location():
    assert normalize_payee("TRADER JOE'S - Portland") == "trader joe's - portland"


def test_normalize_payee_strips_hash_then_preserves_dash_word():
    assert normalize_payee("TRADER JOE'S #123 - Portland") == "trader joe's #123 - portland"


def test_normalize_payee_preserves_hyphenated_name():
    assert normalize_payee("Chick-fil-A") == "chick-fil-a"


# normalization — Issue #29 (star-prefix stripping)

def test_normalize_payee_preserves_sq_prefix():
    assert normalize_payee("SQ *COFFEE SHOP") == "sq *coffee shop"


def test_normalize_payee_preserves_tst_prefix():
    assert normalize_payee("TST*JOE PIZZA") == "tst*joe pizza"


def test_normalize_payee_preserves_pp_prefix():
    assert normalize_payee("PP*SPOTIFY") == "pp*spotify"


def test_normalize_payee_preserves_att_prefix():
    assert normalize_payee("AT&T*WIRELESS") == "at&t*wireless"


def test_normalize_payee_strips_trailing_star_with_digits():
    assert normalize_payee("AMZN MKTP US*1A2B3C") == "amzn mktp us"


# normalization — Issue #30 (unicode dash + number codes)

def test_normalize_payee_strips_endash_number():
    assert normalize_payee("Transfer – 6190") == "transfer"


def test_normalize_payee_strips_emdash_number():
    assert normalize_payee("Transfer — 6190") == "transfer"


# normalization — Issue #31 (trailing dash artifact)

def test_normalize_payee_no_trailing_dash_artifact():
    assert normalize_payee("REFUND - -500") == "refund"


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


# normalization — Integration tests (fuzzy matching with normalized payees)

def test_fuzzy_match_sq_prefix_consistent():
    norm1 = normalize_payee("SQ *COFFEE SHOP")
    norm2 = normalize_payee("SQ *TACOS")
    score = fuzzy_score(norm1, norm2)
    assert score < 70


def test_fuzzy_match_mid_hash_still_matches():
    norm_with_hash = normalize_payee("TRADER JOE'S #123 - Portland")
    norm_without_hash = normalize_payee("TRADER JOE'S - Portland")
    score = fuzzy_score(norm_with_hash, norm_without_hash)
    assert score >= 70


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
    assert result["_version"] == 2
    cat_id, cat_name = _dominant_category(result["whole foods"])
    assert cat_id == "dddddddd-0000-0000-0000-000000000003"
    assert cat_name == "Groceries"


def test_load_payee_cache_raises_on_invalid_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not valid json {")
    with pytest.raises(ValueError):
        load_payee_cache(str(path))


def test_save_payee_cache_writes_file(tmp_path):
    cache = {"_version": 2, "test": {"total": 1, "categories": {"abc": {"name": "Test", "count": 1}}}}
    path = tmp_path / "cache.json"
    save_payee_cache(cache, str(path))
    assert path.exists()


def test_save_payee_cache_roundtrip(tmp_path):
    original = {
        "_version": 2,
        "whole foods": {"total": 1, "categories": {"id1": {"name": "Groceries", "count": 1}}},
        "amazon": {"total": 1, "categories": {"id2": {"name": "Shopping", "count": 1}}},
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
    assert len(cache) == 2  # "valid" + "_version"


def test_build_cache_from_transactions_accumulates_categories():
    """v2 format: same payee with different categories accumulates counts."""
    transactions = [
        {"payee_name": "Whole Foods", "category_id": "cat1", "category_name": "Groceries"},
        {"payee_name": "Whole Foods", "category_id": "cat2", "category_name": "Household"},
        {"payee_name": "Whole Foods", "category_id": "cat1", "category_name": "Groceries"},
    ]
    cache = build_cache_from_transactions(transactions)
    entry = cache["whole foods"]
    assert entry["total"] == 3
    assert entry["categories"]["cat1"]["count"] == 2
    assert entry["categories"]["cat2"]["count"] == 1


def test_build_cache_from_transactions_has_version():
    cache = build_cache_from_transactions([
        {"payee_name": "X", "category_id": "c1", "category_name": "Cat"},
    ])
    assert cache["_version"] == 2


def test_build_cache_from_transactions_empty_list():
    cache = build_cache_from_transactions([])
    assert cache == {"_version": 2}


# Tier 1: history_lookup

def test_history_lookup_returns_result_on_hit():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = history_lookup("Whole Foods", cache)
    assert result is not None
    assert result.category_id == "cat1"
    assert result.category_name == "Groceries"


def test_history_lookup_returns_none_on_miss():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = history_lookup("Amazon", cache)
    assert result is None


def test_history_lookup_returns_none_on_empty_cache():
    result = history_lookup("Whole Foods", {})
    assert result is None


def test_history_lookup_confidence_is_1_0():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = history_lookup("Whole Foods", cache)
    assert result.confidence == 1.0


def test_history_lookup_tier_is_history():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
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
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = history_lookup("WHOLE FOODS #1234", cache)
    assert result is not None
    assert result.category_id == "cat1"


# Tier 2: fuzzy_match

def test_fuzzy_match_exact_returns_result():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = fuzzy_match("whole foods", cache)
    assert result is not None
    assert result.tier == "fuzzy"


def test_fuzzy_match_close_variant_hits():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = fuzzy_match("whole foods market", cache)
    assert result is not None
    assert result.category_id == "cat1"


def test_fuzzy_match_store_number_still_hits():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = fuzzy_match("whole foods #999", cache)
    assert result is not None
    assert result.category_id == "cat1"


def test_fuzzy_match_below_threshold_returns_none():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = fuzzy_match("xyz abc def", cache)
    assert result is None


def test_fuzzy_match_empty_cache_returns_none():
    result = fuzzy_match("whole foods", {})
    assert result is None


def test_fuzzy_match_confidence_is_fraction():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = fuzzy_match("whole foods market", cache)
    assert 0.0 <= result.confidence <= 1.0
    assert result.confidence > 0.7


def test_fuzzy_match_tier_is_fuzzy():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    result = fuzzy_match("whole foods market", cache)
    assert result.tier == "fuzzy"


def test_fuzzy_match_threshold_boundary():
    cache = _v2_cache({"target": ("cat1", "Retail")})
    result = fuzzy_match("target", cache)
    assert result is not None
    assert result.confidence == 1.0


def test_fuzzy_match_with_fixture_cache():
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    result = fuzzy_match("Whole Foods Market #555", cache)
    assert result is not None
    assert result.category_id == "dddddddd-0000-0000-0000-000000000003"


# fuzzy_score function

def test_fuzzy_score_identical_strings():
    assert fuzzy_score("whole foods", "whole foods") == 100


def test_fuzzy_score_similar_strings():
    score = fuzzy_score("whole foods market", "whole foods")
    assert score >= 70


def test_fuzzy_score_penalizes_length_mismatch():
    score = fuzzy_score("transfer : classic checking", "transfer")
    assert score < 50


def test_fuzzy_score_no_penalty_for_similar_length():
    score = fuzzy_score("trader joe's - portland", "trader joe's")
    assert score >= 70


# fuzzy_match — transfer false-positive prevention

def test_fuzzy_match_transfer_does_not_match_generic_transfer():
    cache = _v2_cache({"transfer": ("x", "Water (addup)")})
    result = fuzzy_match("Transfer : Classic Checking -- 6190", cache)
    assert result is None


def test_fuzzy_match_transfer_does_not_match_different_transfer():
    cache = _v2_cache({"transfer : savings": ("x", "Savings")})
    result = fuzzy_match("Transfer : Classic Checking -- 6190", cache)
    assert result is None


def test_categorize_transactions_transfer_skipped():
    """Transfer : payees are skipped entirely, not sent to any tier."""
    cache = _v2_cache({"transfer": ("water_id", "Water (addup)")})
    transactions = [
        {"id": "txn1", "payee_name": "Transfer : Classic Checking -- 6190", "amount": -5000, "date": "2026-03-01"},
    ]
    categories = [{"id": "g1", "name": "Bills", "categories": [{"id": "c1", "name": "Water"}]}]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results, skipped, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 0
    assert len(skipped) == 1
    mock_anthropic_class.assert_not_called()


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
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "dddddddd-0000-0000-0000-000000000002", "category_name": "Online Shopping", "confidence": 0.9, "rationale": "test", "prior_strength": 10}]')]

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
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r", "prior_strength": 10}]')]

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
    mock_response.content = [Mock(text='[{"payee_name": "A", "category_id": "c1", "category_name": "Cat", "confidence": 0.8, "rationale": "r", "prior_strength": 10}]')]

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
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r", "prior_strength": 10}]')]

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
        {"payee_name": f"Store{i}", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r", "prior_strength": 10}
        for i in range(20)
    ]))]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize(transactions, categories, "test-key")

    call_kwargs = mock_client.messages.create.call_args[1]
    # 20 transactions * 250 = 5000, which is > 2048 minimum
    assert call_kwargs["max_tokens"] == 5000


def test_claude_categorize_max_tokens_has_minimum():
    transactions = [{"id": "txn1", "payee_name": "Amazon", "amount": -1000, "date": "2026-03-20"}]
    categories = [{"id": "g1", "name": "Online", "categories": [{"id": "c1", "name": "Shopping"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "confidence": 0.8, "rationale": "r", "prior_strength": 10}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize(transactions, categories, "test-key")

    call_kwargs = mock_client.messages.create.call_args[1]
    # 1 transaction * 250 = 250, but minimum is 2048
    assert call_kwargs["max_tokens"] == 2048


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
    mock_response.content = [Mock(text='[{"payee_name": "Amazon", "category_id": "c1", "category_name": "Shopping", "rationale": "r", "prior_strength": 10}]')]

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
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    transactions = [{"id": "txn1", "payee_name": "Whole Foods", "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].tier == "history"
    assert results[0].category_id == "cat1"
    # Verify Claude was not called
    mock_anthropic_class.assert_not_called()


def test_categorize_transactions_tier2_hit_no_claude_call():
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    transactions = [{"id": "txn1", "payee_name": "Whole Foods Market #999", "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].tier == "fuzzy"
    mock_anthropic_class.assert_not_called()


def test_categorize_transactions_tier3_called_for_novel_payee():
    cache = {}
    transactions = [{"id": "txn1", "payee_name": "Unknown Store", "amount": -5000, "date": "2026-03-01"}]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Retail"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Unknown Store", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

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
    mock_response.content = [Mock(text='[{"payee_name": "Unknown1", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10},{"payee_name": "Unknown2", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10},{"payee_name": "Unknown3", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

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
    cache = _v2_cache({"whole foods": ("cat1", "Groceries")})
    transactions = [
        {"id": "txn1", "payee_name": "Whole Foods", "amount": -5000, "date": "2026-03-01"},  # tier 1
        {"id": "txn2", "payee_name": "Unknown Store", "amount": -6000, "date": "2026-03-01"},  # tier 3
    ]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c2", "name": "Retail"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Unknown Store", "category_id": "c2", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

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
    mock_response.content = [Mock(text='[{"payee_name": "NewNovelStore", "category_id": "dddddddd-0000-0000-0000-000000000003", "category_name": "Groceries", "confidence": 0.85, "rationale": "test", "prior_strength": 10}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results, _, _, _ = categorize_transactions([novel_txn], cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].transaction_id == novel_txn["id"]
    assert results[0].tier == "claude"
    assert results[0].category_id == "dddddddd-0000-0000-0000-000000000003"


def test_categorize_transactions_splits_large_batch():
    # Create 60 transactions, all need Claude (empty cache)
    # With CLAUDE_BATCH_SIZE=25, expect 3 batches: 25+25+10
    cache = {}
    transactions = [
        {"id": f"txn{i}", "payee_name": f"Store{i}", "amount": -5000, "date": "2026-03-01"}
        for i in range(60)
    ]
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Retail"}]}]

    def make_batch_response(start, end):
        return json.dumps([
            {
                "payee_name": f"Store{i}",
                "category_id": "c1",
                "category_name": "Retail",
                "confidence": 0.7,
                "rationale": "r",
                "prior_strength": 10,
            }
            for i in range(start, end)
        ])

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client

        mock_client.messages.create.side_effect = [
            Mock(content=[Mock(text=make_batch_response(0, 25))]),
            Mock(content=[Mock(text=make_batch_response(25, 50))]),
            Mock(content=[Mock(text=make_batch_response(50, 60))]),
        ]

        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

    assert mock_client.messages.create.call_count == 3
    assert len(results) == 60
    assert results[0].transaction_id == "txn0"
    assert results[59].transaction_id == "txn59"


# Additional coverage: edge cases in normalize_payee and build_cache_from_transactions


def test_normalize_payee_raises_on_punctuation_only():
    """Test that a payee that is only punctuation raises ValueError."""
    with pytest.raises(ValueError, match="cannot be empty after normalization"):
        normalize_payee(".,!?;:")


def test_build_cache_from_transactions_accumulates_both_categories():
    """v2 format: both categories are accumulated regardless of date."""
    transactions = [
        {"payee_name": "Whole Foods", "category_id": "cat1", "category_name": "Groceries", "date": None},
        {"payee_name": "Whole Foods", "category_id": "cat2", "category_name": "Other", "date": "2026-01-01"},
    ]
    cache = build_cache_from_transactions(transactions)
    assert cache["whole foods"]["total"] == 2
    assert "cat1" in cache["whole foods"]["categories"]
    assert "cat2" in cache["whole foods"]["categories"]


# Regression tests: bug #22 — transfer payee miscategorization

def test_fixture_cache_has_transfer_entry():
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    assert "transfer" in cache
    cat_id, cat_name = _dominant_category(cache["transfer"])
    assert cat_name == "Water (addup)"


def test_regression_bug22_transfer_payees_not_miscategorized():
    """End-to-end: transfer payees must NOT fuzzy-match to 'transfer' in cache."""
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    transfer_transactions = [
        {"id": "txn1", "payee_name": "Transfer : Classic Checking -- 6190", "amount": -50000, "date": "2026-03-01"},
        {"id": "txn2", "payee_name": "Transfer : Alaska Airlines Visa Signature - 1783", "amount": -25000, "date": "2026-03-01"},
        {"id": "txn3", "payee_name": "Transfer : Delta SkyMiles Platinum", "amount": -10000, "date": "2026-03-01"},
    ]
    categories = [{"id": "g1", "name": "Bills", "categories": [{"id": "c1", "name": "Water"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text=json.dumps([
        {"payee_name": t["payee_name"], "category_id": "c1", "category_name": "Water", "confidence": 0.7, "rationale": "r", "prior_strength": 10}
        for t in transfer_transactions
    ]))]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results, skipped, _, _ = categorize_transactions(transfer_transactions, cache, categories, "test-key")

    # All "Transfer :" payees are now skipped entirely (not sent to Claude)
    assert len(results) == 0
    assert len(skipped) == 3
    mock_anthropic_class.assert_not_called()


def test_categorize_transactions_legitimate_transfer_exact_match():
    """A bare 'Transfer' payee (without colon) should still match 'transfer' in cache."""
    cache = _v2_cache({"transfer": ("water_id", "Water (addup)")})
    transactions = [{"id": "txn1", "payee_name": "Transfer", "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results, _, _, _ = categorize_transactions(transactions, cache, categories, "test-key")

    # "Transfer" (no colon) is NOT skipped — only "Transfer :" is
    assert len(results) == 1
    assert results[0].tier == "history"
    assert results[0].category_name == "Water (addup)"
    mock_anthropic_class.assert_not_called()


def test_fuzzy_match_with_fixture_cache_no_transfer_false_positive():
    """Integration: fixture cache with 'transfer' entry, transfer payee should NOT match."""
    cache = load_payee_cache("data/fixtures/payee_cache.json")
    result = fuzzy_match("Transfer : Classic Checking -- 6190", cache)
    assert result is None


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


def test_main_raises_when_budget_env_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.delenv("YNAB_DEFAULT_BUDGET", raising=False)
    monkeypatch.chdir(tmp_path)

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with pytest.raises(ValueError, match="YNAB_DEFAULT_BUDGET"):
                main()


def test_main_no_uncategorized_exits_early(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b1")

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
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b1")

    mock_ynab = Mock()
    k_txns = [{"id": "t2", "payee_name": "OldStore", "category_id": "c1", "category_name": "Groceries", "amount": -3000, "date": "2026-02-01"}]
    mock_ynab.get_transactions.side_effect = [
        ([{"id": "t1", "payee_name": "NewStore", "category_id": None, "amount": -5000, "date": "2026-03-01"}], {}),
        (k_txns, {}),
        (k_txns, {}),
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
                with patch("categorizer.categorize_transactions", return_value=([mock_claude_result], [], [], [])):
                    main()

    out = capsys.readouterr().out
    assert "Cache is empty" in out
    assert "Proposed categorizations" in out
    assert "Cache updated" in out


def test_main_full_run_with_existing_cache(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b1")

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
                with patch("categorizer.categorize_transactions", return_value=([history_result], [], [], [])):
                    main()

    out = capsys.readouterr().out
    assert "Proposed categorizations (1 transactions)" in out
    assert "[HISTORY]" in out
    assert "Cache updated with 0 new payees" in out


def test_main_updates_cache_with_claude_results(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b1")

    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "payee_lookup.json").write_text(json.dumps({}))

    k_txns = [{"id": "t2", "payee_name": "OldPlace", "category_id": "c1", "category_name": "Dining", "amount": -3000, "date": "2026-02-01"}]
    mock_ynab = Mock()
    mock_ynab.get_transactions.side_effect = [
        ([{"id": "t1", "payee_name": "NewPlace", "category_id": None, "amount": -5000, "date": "2026-03-01"}], {}),
        (k_txns, {}),
        (k_txns, {}),
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
                with patch("categorizer.categorize_transactions", return_value=([claude_result], [], [], [])):
                    main()

    # Verify cache was updated with the claude result (v2 format)
    saved_cache = json.loads((cache_dir / "payee_lookup.json").read_text())
    assert "newplace" in saved_cache
    assert saved_cache["newplace"]["categories"]["c2"]["name"] == "Dining"
    assert saved_cache["newplace"]["total"] == 1


def test_main_triggers_rebuild_on_v1_migration(monkeypatch, tmp_path, capsys):
    """When load_payee_cache migrates v1→v2, main() triggers a full rebuild."""
    monkeypatch.setenv("YNAB_API_TOKEN", "token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b1")

    # Write a v1 cache
    cache_dir = tmp_path / "data" / "cache"
    cache_dir.mkdir(parents=True)
    v1_cache = {"starbucks": {"category_id": "c1", "category_name": "Coffee"}}
    (cache_dir / "payee_lookup.json").write_text(json.dumps(v1_cache))

    mock_ynab = Mock()
    rebuild_txns = [
        {"id": "t2", "payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee", "amount": -500, "date": "2026-01-01"},
        {"id": "t3", "payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee", "amount": -500, "date": "2026-01-02"},
        {"id": "t4", "payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee", "amount": -500, "date": "2026-01-03"},
    ]
    # Calls: (1) recent txns, (2) 18-month K txns, (3) all txns for rebuild
    mock_ynab.get_transactions.side_effect = [
        ([{"id": "t1", "payee_name": "NewStore", "category_id": None, "amount": -5000, "date": "2026-03-01"}], {}),
        (rebuild_txns, {}),
        (rebuild_txns, {}),
    ]
    mock_ynab.get_categories.return_value = [
        {"id": "g1", "name": "Food", "deleted": False, "hidden": False,
         "categories": [{"id": "c1", "name": "Coffee", "deleted": False}]}
    ]

    mock_claude_result = CategoryResult(
        transaction_id="t1", category_id="c1", category_name="Coffee",
        confidence=0.9, rationale="test", tier="claude"
    )

    with patch("dotenv.load_dotenv"):
        with patch("sys.argv", ["categorizer.py", "--days", "7"]):
            with patch("ynab_client.YNABClient", return_value=mock_ynab):
                with patch("categorizer.categorize_transactions", return_value=([mock_claude_result], [], [], [])):
                    main()

    out = capsys.readouterr().out
    assert "Rebuilding" in out

    # After rebuild, cache should have frequency counts > 1
    rebuilt_cache = json.loads((cache_dir / "payee_lookup.json").read_text())
    assert rebuilt_cache["_version"] == 2
    assert rebuilt_cache["starbucks"]["total"] == 3


# ── Cache format migration (v1 → v2) ─────────────────────────────────────────

def test_load_payee_cache_v1_migration(tmp_path):
    """Load v1 cache → auto-migrates → entries have total: 1, correct structure."""
    v1 = {"starbucks": {"category_id": "c1", "category_name": "Coffee"}}
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(v1))
    result = load_payee_cache(str(path))
    assert result["_version"] == 2
    assert result["starbucks"]["total"] == 1
    assert result["starbucks"]["categories"]["c1"]["name"] == "Coffee"
    assert result["starbucks"]["categories"]["c1"]["count"] == 1


def test_load_payee_cache_v2_no_migration(tmp_path):
    """Load v2 cache → returned as-is."""
    v2 = {"_version": 2, "x": {"total": 5, "categories": {"c1": {"name": "Y", "count": 5}}}}
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(v2))
    result = load_payee_cache(str(path))
    assert result == v2


def test_load_payee_cache_v1_saves_migrated(tmp_path):
    """After loading v1 cache, the file on disk is updated to v2 format."""
    v1 = {"starbucks": {"category_id": "c1", "category_name": "Coffee"}}
    path = tmp_path / "cache.json"
    path.write_text(json.dumps(v1))
    load_payee_cache(str(path))
    on_disk = json.loads(path.read_text())
    assert on_disk["_version"] == 2
    assert "starbucks" in on_disk
    assert on_disk["starbucks"]["total"] == 1


def test_load_payee_cache_v1_fixture(tmp_path):
    """The v1 fixture file migrates to v2 correctly without corrupting the original."""
    import shutil
    src = Path("data/fixtures/payee_cache_v1.json")
    dst = tmp_path / "payee_cache_v1.json"
    shutil.copy(src, dst)

    result = load_payee_cache(str(dst))
    assert result["_version"] == 2
    assert result["whole foods"]["total"] == 1
    assert result["whole foods"]["categories"]["dddddddd-0000-0000-0000-000000000003"]["name"] == "Groceries"
    assert result["whole foods"]["categories"]["dddddddd-0000-0000-0000-000000000003"]["count"] == 1

    # Original fixture must remain v1 (no _version key)
    original = json.loads(src.read_text())
    assert "_version" not in original
    assert original["whole foods"]["category_id"] == "dddddddd-0000-0000-0000-000000000003"


def test_build_cache_multi_category_payee():
    """Payee with transactions in 2 different categories → both present."""
    txns = [
        {"payee_name": "Amazon", "category_id": "c1", "category_name": "Groceries"},
        {"payee_name": "Amazon", "category_id": "c1", "category_name": "Groceries"},
        {"payee_name": "Amazon", "category_id": "c2", "category_name": "Electronics"},
    ]
    cache = build_cache_from_transactions(txns)
    entry = cache["amazon"]
    assert entry["total"] == 3
    assert entry["categories"]["c1"]["count"] == 2
    assert entry["categories"]["c2"]["count"] == 1


def test_build_cache_roundtrip(tmp_path):
    """build → save → load → entries identical."""
    txns = [
        {"payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee"},
        {"payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee"},
    ]
    cache = build_cache_from_transactions(txns)
    path = tmp_path / "cache.json"
    save_payee_cache(cache, str(path))
    loaded = load_payee_cache(str(path))
    assert loaded == cache


def test_build_cache_then_compute_confidence():
    """Build cache from transactions, compute confidence on an entry."""
    txns = [{"payee_name": "X", "category_id": "c1", "category_name": "Cat"}] * 10
    cache = build_cache_from_transactions(txns)
    cat_id, cat_name, conf = compute_confidence(cache["x"], 5)
    assert cat_id == "c1"
    assert conf > 0


# ── count_categories ──────────────────────────────────────────────────────────

def test_count_categories_fixture():
    """Using ynab_categories.json: Rent + Groceries = 2 (excludes deleted 'Old Category'
    and categories in deleted group 'Deleted Group')."""
    fixture = json.loads(Path("data/fixtures/ynab_categories.json").read_text())
    groups = fixture["data"]["category_groups"]
    assert count_categories(groups) == 2


def test_count_categories_includes_hidden():
    """Hidden but non-deleted categories are counted."""
    groups = [
        {
            "id": "g1", "name": "G1", "hidden": False, "deleted": False,
            "categories": [
                {"id": "c1", "name": "Visible", "hidden": False, "deleted": False},
                {"id": "c2", "name": "Hidden", "hidden": True, "deleted": False},
            ]
        }
    ]
    assert count_categories(groups) == 2


def test_count_categories_empty():
    assert count_categories([]) == 0


# ── count_categories_from_transactions ────────────────────────────────────────

def test_count_categories_from_transactions_basic():
    """Counts unique category_ids from transactions."""
    txns = [
        {"category_id": "c1", "category_name": "Groceries", "date": "2026-01-01"},
        {"category_id": "c1", "category_name": "Groceries", "date": "2026-01-02"},
        {"category_id": "c2", "category_name": "Dining", "date": "2026-01-03"},
    ]
    assert count_categories_from_transactions(txns) == 2


def test_count_categories_from_transactions_skips_uncategorized():
    txns = [
        {"category_id": "c1", "category_name": "Groceries", "date": "2026-01-01"},
        {"category_id": None, "category_name": None, "date": "2026-01-01"},
        {"date": "2026-01-01"},
    ]
    assert count_categories_from_transactions(txns) == 1


def test_count_categories_from_transactions_empty():
    assert count_categories_from_transactions([]) == 0


# ── filter_categories_by_usage ────────────────────────────────────────────────

def test_filter_categories_by_usage_basic():
    groups = [
        {"id": "g1", "name": "Food", "categories": [
            {"id": "c1", "name": "Groceries"},
            {"id": "c2", "name": "Dining"},
        ]},
        {"id": "g2", "name": "Bills", "categories": [
            {"id": "c3", "name": "Electric"},
        ]},
    ]
    txns = [
        {"category_id": "c1"},
        {"category_id": "c1"},
    ]
    filtered = filter_categories_by_usage(groups, txns)
    assert len(filtered) == 1
    assert filtered[0]["name"] == "Food"
    assert len(filtered[0]["categories"]) == 1
    assert filtered[0]["categories"][0]["name"] == "Groceries"


def test_filter_categories_by_usage_empty_txns():
    groups = [{"id": "g1", "name": "Food", "categories": [{"id": "c1", "name": "X"}]}]
    assert filter_categories_by_usage(groups, []) == []


# ── compute_confidence_threshold ──────────────────────────────────────────────

def test_compute_confidence_threshold_basic():
    """Threshold derived from min_observations and K."""
    from scipy.stats import beta
    K = 72
    thresh = compute_confidence_threshold(K)
    expected = beta.ppf(0.10, MIN_OBSERVATIONS + 1, K - 1)
    assert thresh == pytest.approx(expected)


def test_compute_confidence_threshold_varies_with_k():
    """Larger K produces lower threshold (harder to be confident)."""
    t_small = compute_confidence_threshold(20)
    t_large = compute_confidence_threshold(142)
    assert t_small > t_large


def test_compute_confidence_threshold_custom_min_obs():
    """Can override min_observations."""
    t3 = compute_confidence_threshold(72, min_observations=3)
    t5 = compute_confidence_threshold(72, min_observations=5)
    assert t5 > t3


def test_compute_confidence_threshold_k_equals_1():
    """K=1 produces threshold of 1.0 (degenerate case)."""
    assert compute_confidence_threshold(1) == 1.0


# ── Integration: threshold with real cache ────────────────────────────────────

def test_threshold_resolves_consistent_payee():
    """A payee seen 3+ times consistently should pass the derived threshold."""
    K = 72
    thresh = compute_confidence_threshold(K)
    cache = {"_version": 2}
    for _ in range(3):
        record_categorization(cache, "Netflix", "c1", "Streaming", "history")
    result = history_lookup("Netflix", cache, K=K, confidence_threshold=thresh)
    assert result is not None
    assert result.category_id == "c1"


def test_threshold_rejects_single_observation():
    """A payee seen once should NOT pass the derived threshold."""
    K = 72
    thresh = compute_confidence_threshold(K)
    cache = {"_version": 2}
    record_categorization(cache, "NewPlace", "c1", "Dining", "history")
    result = history_lookup("NewPlace", cache, K=K, confidence_threshold=thresh)
    assert result is None


# ── compute_confidence ────────────────────────────────────────────────────────

def test_compute_confidence_matches_formula():
    """Validate beta.ppf(0.10, c+1, (n-c)+(K-1)) with K=137.

    The issue #25 table values were illustrative. These are the actual
    computed values from the formula with K=137.
    """
    K = 137

    # n=1, c=1: very low (~0.004)
    entry_1 = {"total": 1, "categories": {"cat1": {"name": "Coffee", "count": 1}}}
    _, _, conf = compute_confidence(entry_1, K)
    assert conf < 0.02

    # n=47, c=45: moderate (~0.21, prior mass from K=137 dilutes heavily)
    entry_47 = {"total": 47, "categories": {
        "cat1": {"name": "Groceries", "count": 45},
        "cat2": {"name": "Household", "count": 2},
    }}
    _, _, conf = compute_confidence(entry_47, K)
    assert 0.15 < conf < 0.30

    # Confidence increases monotonically with n (all same category)
    confs = []
    for n in [1, 10, 20, 50, 100]:
        entry = {"total": n, "categories": {"cat1": {"name": "X", "count": n}}}
        _, _, c = compute_confidence(entry, K)
        confs.append(c)
    assert confs == sorted(confs)


def test_compute_confidence_single_observation():
    """n=1, K=137 → very low confidence."""
    entry = {"total": 1, "categories": {"cat1": {"name": "X", "count": 1}}}
    _, _, conf = compute_confidence(entry, 137)
    assert conf < 0.05


def test_compute_confidence_even_split():
    """Two categories with equal counts → returns alphabetically first category_id."""
    entry = {
        "total": 10,
        "categories": {
            "cat_b": {"name": "B", "count": 5},
            "cat_a": {"name": "A", "count": 5},
        }
    }
    cat_id, cat_name, _ = compute_confidence(entry, 10)
    assert cat_id == "cat_a"
    assert cat_name == "A"


def test_compute_confidence_k_equals_1():
    """K=1 (only one category exists)."""
    entry = {"total": 5, "categories": {"cat1": {"name": "Only", "count": 5}}}
    cat_id, cat_name, conf = compute_confidence(entry, 1)
    assert cat_id == "cat1"
    assert conf > 0.5


def test_compute_confidence_raises_on_zero_total():
    entry = {"total": 0, "categories": {}}
    with pytest.raises(ValueError, match="total=0"):
        compute_confidence(entry, 10)


def test_compute_confidence_raises_on_invalid_k():
    entry = {"total": 1, "categories": {"c": {"name": "X", "count": 1}}}
    with pytest.raises(ValueError, match="K must be >= 1"):
        compute_confidence(entry, 0)
    with pytest.raises(ValueError, match="K must be >= 1"):
        compute_confidence(entry, -1)


# ── record_categorization ────────────────────────────────────────────────────

def test_record_categorization_creates_entry():
    cache = {}
    record_categorization(cache, "Whole Foods", "cat1", "Groceries", "history")
    assert "whole foods" in cache
    entry = cache["whole foods"]
    assert entry["total"] == 1
    assert entry["categories"]["cat1"]["name"] == "Groceries"
    assert entry["categories"]["cat1"]["count"] == 1


def test_record_categorization_accumulates():
    cache = {}
    record_categorization(cache, "Whole Foods", "cat1", "Groceries", "history")
    record_categorization(cache, "Whole Foods", "cat1", "Groceries", "history")
    entry = cache["whole foods"]
    assert entry["total"] == 2
    assert entry["categories"]["cat1"]["count"] == 2


def test_record_categorization_multiple_categories():
    cache = {}
    record_categorization(cache, "Whole Foods", "cat1", "Groceries", "history")
    record_categorization(cache, "Whole Foods", "cat2", "Household", "history")
    entry = cache["whole foods"]
    assert entry["total"] == 2
    assert entry["categories"]["cat1"]["count"] == 1
    assert entry["categories"]["cat2"]["count"] == 1


def test_record_categorization_prior_strength():
    cache = {}
    record_categorization(cache, "Starbucks", "cat1", "Coffee", "claude", prior_strength=15)
    entry = cache["starbucks"]
    assert entry["total"] == 15
    assert entry["categories"]["cat1"]["count"] == 15


# ── CategoryResult.prior_strength ─────────────────────────────────────────────

def test_category_result_prior_strength_default():
    result = CategoryResult(
        transaction_id="t1", category_id="c1", category_name="X",
        confidence=0.9, rationale="r", tier="history"
    )
    assert result.prior_strength is None


def test_category_result_prior_strength_set():
    result = CategoryResult(
        transaction_id="t1", category_id="c1", category_name="X",
        confidence=0.9, rationale="r", tier="claude", prior_strength=10
    )
    assert result.prior_strength == 10


# ── Integration: record then compute ─────────────────────────────────────────

def test_record_then_compute_confidence():
    """Record 20 categorizations (18 Groceries, 2 Household), compute confidence.

    With K=5 (small category space), 18/20 dominant gives high confidence.
    """
    cache = {}
    for _ in range(18):
        record_categorization(cache, "Whole Foods", "cat1", "Groceries", "history")
    for _ in range(2):
        record_categorization(cache, "Whole Foods", "cat2", "Household", "history")

    cat_id, cat_name, conf = compute_confidence(cache["whole foods"], 5)
    assert cat_id == "cat1"
    assert cat_name == "Groceries"
    assert conf > 0.5


# ── normalize_import_payee ────────────────────────────────────────────────────

def test_normalize_import_payee_strips_id_co():
    result = normalize_import_payee("NEWREZ-SHELLPOIN ID: 6371542226 CO: NEWREZ-SHELLPOIN")
    assert result == "newrez-shellpoin"


def test_normalize_import_payee_strips_ach():
    result = normalize_import_payee("PAYROLL ACH ACMECORP")
    assert result == "payroll"


def test_normalize_import_payee_passthrough():
    result = normalize_import_payee("Whole Foods")
    assert result == "whole foods"


def test_normalize_import_payee_raises_on_empty():
    with pytest.raises(ValueError):
        normalize_import_payee("")


# ── Alias system ─────────────────────────────────────────────────────────────

def test_record_categorization_creates_alias():
    cache = {}
    record_categorization(
        cache, "Transfer : Mortgage 4081", "c1", "Mortgage", "history",
        import_names=["NEWREZ-SHELLPOIN ID: 6371542226 CO: NEWREZ-SHELLPOIN"],
    )
    assert "newrez-shellpoin" in cache
    assert cache["newrez-shellpoin"]["alias_of"] == "transfer : mortgage 4081"
    assert "newrez-shellpoin" in cache["transfer : mortgage 4081"]["aliases"]


def test_record_categorization_dedup_alias():
    """Import name that normalizes same as payee_name doesn't create alias."""
    cache = {}
    record_categorization(
        cache, "Whole Foods", "c1", "Groceries", "history",
        import_names=["WHOLE FOODS"],
    )
    assert "aliases" not in cache.get("whole foods", {})


def test_resolve_alias():
    cache = {"alias_key": {"alias_of": "primary"}, "primary": {"total": 1, "categories": {}}}
    assert _resolve_alias(cache, "alias_key") == "primary"


def test_resolve_alias_no_alias():
    cache = {"primary": {"total": 1, "categories": {}}}
    assert _resolve_alias(cache, "primary") == "primary"


def test_record_categorization_multiple_import_names():
    cache = {}
    record_categorization(
        cache, "Transfer : Mortgage 4081", "c1", "Mortgage", "history",
        import_names=["NEWREZ-SHELLPOIN ID: 123", "SHELLPOINT MTG CO: ABC"],
    )
    primary = cache["transfer : mortgage 4081"]
    assert "newrez-shellpoin" in primary["aliases"]
    assert "shellpoint mtg" in primary["aliases"]
    assert cache["newrez-shellpoin"]["alias_of"] == "transfer : mortgage 4081"
    assert cache["shellpoint mtg"]["alias_of"] == "transfer : mortgage 4081"


# ── history_lookup with import names ──────────────────────────────────────────

def test_history_lookup_via_import_name():
    """payee_name not in cache, but import_payee_name_original resolves via alias."""
    cache = _v2_cache({"transfer : mortgage 4081": ("c1", "Mortgage", 10)})
    cache["newrez-shellpoin"] = {"alias_of": "transfer : mortgage 4081"}
    cache["transfer : mortgage 4081"]["aliases"] = ["newrez-shellpoin"]

    result = history_lookup(
        "Some New Payee Name", cache,
        import_payee_name_original="NEWREZ-SHELLPOIN ID: 123 CO: NEWREZ-SHELLPOIN",
    )
    assert result is not None
    assert result.category_id == "c1"
    assert result.tier == "history"


def test_history_lookup_confidence_threshold():
    """With K, low-count entry fails threshold."""
    cache = _v2_cache({"starbucks": ("c1", "Coffee", 1)})

    result = history_lookup("Starbucks", cache, K=137, confidence_threshold=0.5)
    assert result is None

    cache_high = _v2_cache({"starbucks": ("c1", "Coffee", 200)})
    result = history_lookup("Starbucks", cache_high, K=137, confidence_threshold=0.5)
    assert result is not None


def test_history_lookup_payee_name_preferred():
    """When payee_name has a direct hit, import name is not needed."""
    cache = _v2_cache({"whole foods": ("c1", "Groceries")})
    result = history_lookup(
        "Whole Foods", cache,
        import_payee_name_original="SOMETHING ELSE",
    )
    assert result is not None
    assert result.category_id == "c1"


# ── fuzzy_match with import names ─────────────────────────────────────────────

def test_fuzzy_match_tries_import_names():
    cache = _v2_cache({"newrez-shellpoin": ("c1", "Mortgage")})
    result = fuzzy_match(
        "Unknown Payee", cache,
        import_payee_name_original="NEWREZ-SHELLPOIN ID: 123 CO: NEWREZ-SHELLPOIN",
    )
    assert result is not None
    assert result.category_id == "c1"


def test_fuzzy_match_confidence_threshold():
    """Good fuzzy score but low Bayesian confidence (count=1, K=137) returns None."""
    cache = _v2_cache({"starbucks": ("c1", "Coffee", 1)})
    result = fuzzy_match("Starbucks", cache, K=137, confidence_threshold=0.5)
    assert result is None


# ── build_cache_from_transactions with import names ───────────────────────────

def test_build_cache_indexes_import_names():
    txns = [
        {
            "payee_name": "Transfer : Mortgage 4081",
            "category_id": "c1",
            "category_name": "Mortgage",
            "import_payee_name_original": "NEWREZ-SHELLPOIN ID: 123 CO: NEWREZ-SHELLPOIN",
        }
    ]
    cache = build_cache_from_transactions(txns)
    assert "newrez-shellpoin" in cache
    assert cache["newrez-shellpoin"]["alias_of"] == "transfer : mortgage 4081"


# ── Integration: import name end-to-end ───────────────────────────────────────

def test_import_name_end_to_end():
    """Build cache with import name, then look up via import name."""
    txns = [
        {
            "payee_name": "Transfer : Mortgage 4081",
            "category_id": "c1",
            "category_name": "Mortgage",
            "import_payee_name_original": "NEWREZ-SHELLPOIN ID: 6371542226 CO: NEWREZ-SHELLPOIN",
        }
    ] * 10
    cache = build_cache_from_transactions(txns)

    result = history_lookup(
        "Some Unknown Payee", cache,
        import_payee_name_original="NEWREZ-SHELLPOIN ID: 999 CO: NEWREZ-SHELLPOIN",
    )
    assert result is not None
    assert result.category_id == "c1"
    assert result.category_name == "Mortgage"


# ── Claude prior_strength and import name in prompt (#38) ─────────────────────

def test_claude_response_with_prior_strength():
    """Mock response includes prior_strength → parsed into CategoryResult."""
    transactions = [{"id": "txn1", "payee_name": "Starbucks", "amount": -5000, "date": "2026-03-01"}]
    categories = [{"id": "g1", "name": "Food", "categories": [{"id": "c1", "name": "Coffee"}]}]
    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee", "confidence": 0.9, "rationale": "coffee shop", "prior_strength": 15}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results = claude_categorize(transactions, categories, "key")

    assert results[0].prior_strength == 15


def test_claude_response_missing_prior_strength():
    """Mock response omits prior_strength → raises ValueError."""
    transactions = [{"id": "txn1", "payee_name": "X", "amount": -1000, "date": "2026-03-01"}]
    categories = [{"id": "g1", "name": "G", "categories": [{"id": "c1", "name": "C"}]}]
    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "X", "category_id": "c1", "category_name": "C", "confidence": 0.8, "rationale": "r"}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="missing fields.*prior_strength"):
            claude_categorize(transactions, categories, "key")


def test_claude_response_invalid_prior_strength():
    """prior_strength out of range → raises ValueError."""
    for bad_val in [0, 25, "high"]:
        transactions = [{"id": "txn1", "payee_name": "X", "amount": -1000, "date": "2026-03-01"}]
        categories = [{"id": "g1", "name": "G", "categories": [{"id": "c1", "name": "C"}]}]
        mock_response = Mock()
        mock_response.content = [Mock(text=json.dumps([{
            "payee_name": "X", "category_id": "c1", "category_name": "C",
            "confidence": 0.8, "rationale": "r", "prior_strength": bad_val,
        }]))]
        with patch("categorizer.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = mock_response
            with pytest.raises(ValueError, match="invalid prior_strength"):
                claude_categorize(transactions, categories, "key")


def test_claude_prompt_includes_import_name():
    """When transaction has import_payee_name_original, prompt contains it."""
    transactions = [{"id": "txn1", "payee_name": "Mortgage", "amount": -100000,
                     "date": "2026-03-01", "import_payee_name_original": "NEWREZ ID:123"}]
    categories = [{"id": "g1", "name": "Bills", "categories": [{"id": "c1", "name": "Mortgage"}]}]
    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Mortgage", "category_id": "c1", "category_name": "Mortgage", "confidence": 0.95, "rationale": "r", "prior_strength": 18}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_client = Mock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize(transactions, categories, "key")

    call_args = mock_client.messages.create.call_args
    user_msg = call_args[1]["messages"][0]["content"]
    assert "NEWREZ ID:123" in user_msg


def test_claude_prompt_without_import_name():
    """When transaction lacks import_payee_name_original, prompt still works."""
    transactions = [{"id": "txn1", "payee_name": "Starbucks", "amount": -5000, "date": "2026-03-01"}]
    categories = [{"id": "g1", "name": "Food", "categories": [{"id": "c1", "name": "Coffee"}]}]
    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Starbucks", "category_id": "c1", "category_name": "Coffee", "confidence": 0.9, "rationale": "r", "prior_strength": 15}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_client = Mock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results = claude_categorize(transactions, categories, "key")

    assert len(results) == 1
    call_args = mock_client.messages.create.call_args
    user_msg = call_args[1]["messages"][0]["content"]
    assert "Bank description" not in user_msg


def test_orchestrator_passes_import_names_to_lookup():
    """Verify import name fields are threaded to history_lookup."""
    cache = _v2_cache({"whole foods": ("c1", "Groceries", 50)})
    cache["wf market"] = {"alias_of": "whole foods"}
    cache["whole foods"]["aliases"] = ["wf market"]

    txns = [{"id": "t1", "payee_name": "Unknown", "amount": -5000, "date": "2026-03-01",
             "import_payee_name_original": "WF MARKET"}]

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        results, _, _, _ = categorize_transactions(txns, cache, [], "key")

    assert len(results) == 1
    assert results[0].tier in ("history", "fuzzy")
    mock_cls.assert_not_called()


def test_orchestrator_records_claude_with_strength():
    """After Claude categorizes, record_categorization is called with prior_strength."""
    cache = {"_version": 2}
    txns = [{"id": "t1", "payee_name": "NewStore", "amount": -5000, "date": "2026-03-01"}]
    categories = [{"id": "g1", "name": "G", "categories": [{"id": "c1", "name": "C"}]}]
    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "NewStore", "category_id": "c1", "category_name": "C", "confidence": 0.8, "rationale": "r", "prior_strength": 12}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results, _, _, _ = categorize_transactions(txns, cache, categories, "key")

    assert results[0].prior_strength == 12




# ============================================================================
# Tests for Phase D: Amazon item categorization (Issue #83)
# ============================================================================

from decimal import Decimal
from datetime import date
from amazon_matcher import AmazonItem, AmazonShipment, ItemAllocation


CATEGORIES_FIXTURE = [
    {
        "id": "grp1",
        "name": "Shopping",
        "categories": [
            {"id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90", "name": "Shopping"},
            {"id": "c5f3b2a1-d8f4-4c9b-a1e2-9f8c7d6e5f4a", "name": "Electronics"},
        ],
    }
]


def _make_item(order_id="111-0000001", ship_date=None, asin="B001", product_name="Test Product", qty=1, price="10.00"):
    return AmazonItem(
        order_id=order_id,
        ship_date=ship_date or date(2026, 1, 15),
        asin=asin,
        product_name=product_name,
        quantity=qty,
        unit_price=Decimal(price),
        unit_price_tax=Decimal("0"),
        raw_row_index=2,
    )


def _make_allocation(item, amount="10.00"):
    return ItemAllocation(item=item, allocated_amount=Decimal(amount), share_of_subtotal=Decimal("1"))


def _make_parent_txn(txn_id="txn-001", amount=-10000, date_str="2026-01-15", account="Visa"):
    return {"id": txn_id, "amount": amount, "date": date_str, "account_name": account}


def test_claude_categorize_amazon_items_happy_path():
    """Test happy path: 3 items, 1 with null category_id."""
    from categorizer import claude_categorize_amazon_items, ItemCategoryResult
    
    items = [
        _make_item(asin="B001", product_name="Product A"),
        _make_item(asin="B002", product_name="Product B"),
        _make_item(asin="B003", product_name="Product C"),
    ]
    allocations = [
        _make_allocation(items[0], "26.75"),
        _make_allocation(items[1], "16.05"),
        _make_allocation(items[2], "10.70"),
    ]
    parent_txn = _make_parent_txn(amount=-53500)

    response_data = json.dumps([
        {"item_index": 1, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
         "category_name": "Shopping", "confidence": 0.92, "rationale": "Office supplies"},
        {"item_index": 2, "category_id": "c5f3b2a1-d8f4-4c9b-a1e2-9f8c7d6e5f4a",
         "category_name": "Electronics", "confidence": 0.88, "rationale": "Consumer electronics"},
        {"item_index": 3, "category_id": None,
         "category_name": None, "confidence": 0.0, "rationale": "Unable to categorize"},
    ])

    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results = claude_categorize_amazon_items(parent_txn, allocations, CATEGORIES_FIXTURE, "key")

    assert len(results) == 3
    assert results[0].category_id == "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90"
    assert results[0].allocated_amount == Decimal("26.75")
    assert results[0].item is items[0]
    assert results[0].ynab_transaction_id == "txn-001"
    assert results[2].category_id is None
    assert results[2].category_name is None


def test_claude_categorize_amazon_items_length_mismatch():
    """Test error when Claude returns wrong number of items."""
    from categorizer import claude_categorize_amazon_items
    
    allocations = [_make_allocation(_make_item(), "10.00"), _make_allocation(_make_item(), "10.00")]
    response_data = json.dumps([
        {"item_index": 1, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
         "category_name": "Shopping", "confidence": 0.9, "rationale": "r"},
        {"item_index": 2, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
         "category_name": "Shopping", "confidence": 0.9, "rationale": "r"},
        {"item_index": 3, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
         "category_name": "Shopping", "confidence": 0.9, "rationale": "r"},
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="3 results for 2 allocations"):
            claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")


def test_claude_categorize_amazon_items_item_index_mismatch():
    """Test error when item_index doesn't match position."""
    from categorizer import claude_categorize_amazon_items
    
    items = [_make_item() for _ in range(3)]
    allocations = [_make_allocation(i) for i in items]
    response_data = json.dumps([
        {"item_index": 1, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90", "category_name": "Shopping", "confidence": 0.9, "rationale": "r"},
        {"item_index": 3, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90", "category_name": "Shopping", "confidence": 0.9, "rationale": "r"},
        {"item_index": 2, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90", "category_name": "Shopping", "confidence": 0.9, "rationale": "r"},
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="item_index"):
            claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")


def test_claude_categorize_amazon_items_unknown_category_id():
    """Test error when Claude returns unknown category_id."""
    from categorizer import claude_categorize_amazon_items
    
    allocations = [_make_allocation(_make_item())]
    response_data = json.dumps([
        {"item_index": 1, "category_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
         "category_name": "Fake", "confidence": 0.9, "rationale": "r"},
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="unknown category_id"):
            claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")


def test_claude_categorize_amazon_items_null_category_id_accepted():
    """Test that null category_id is accepted (no error)."""
    from categorizer import claude_categorize_amazon_items
    
    allocations = [_make_allocation(_make_item())]
    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "unsure"},
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results = claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")

    assert results[0].category_id is None


def test_claude_categorize_amazon_items_missing_field():
    """Test error when required field is missing."""
    from categorizer import claude_categorize_amazon_items
    
    allocations = [_make_allocation(_make_item())]
    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0},
        # "rationale" missing
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="rationale"):
            claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")


@pytest.mark.parametrize("bad_confidence", [1.5, -0.1, "high"])
def test_claude_categorize_amazon_items_bad_confidence(bad_confidence):
    """Test error for confidence out of range."""
    from categorizer import claude_categorize_amazon_items
    
    allocations = [_make_allocation(_make_item())]
    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None,
         "confidence": bad_confidence, "rationale": "r"},
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="confidence"):
            claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")


def test_claude_categorize_amazon_items_markdown_fences_stripped():
    """Test markdown fence stripping."""
    from categorizer import claude_categorize_amazon_items
    
    allocations = [_make_allocation(_make_item())]
    inner = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "r"}
    ])
    fenced = f"```json\n{inner}\n```"
    mock_response = Mock()
    mock_response.content = [Mock(text=fenced)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results = claude_categorize_amazon_items(_make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key")

    assert len(results) == 1


def test_claude_categorize_amazon_items_empty_content():
    """Test error on empty Claude content."""
    from categorizer import claude_categorize_amazon_items
    
    mock_response = Mock()
    mock_response.content = []
    mock_response.stop_reason = "end_turn"
    mock_response.usage = Mock()

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="empty content"):
            claude_categorize_amazon_items(_make_parent_txn(), [_make_allocation(_make_item())], CATEGORIES_FIXTURE, "key")


def test_claude_categorize_amazon_items_max_tokens():
    """Test error when Claude hits max_tokens."""
    from categorizer import claude_categorize_amazon_items
    
    mock_response = Mock()
    mock_response.content = [Mock(text="[truncated")]
    mock_response.stop_reason = "max_tokens"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        with pytest.raises(ValueError, match="max_tokens"):
            claude_categorize_amazon_items(_make_parent_txn(), [_make_allocation(_make_item())], CATEGORIES_FIXTURE, "key")


def test_claude_categorize_amazon_items_prompt_contents():
    """Test that prompt contains required fields."""
    from categorizer import claude_categorize_amazon_items
    
    item = _make_item(asin="B0TEST123", product_name="My Test Widget", qty=2)
    allocation = _make_allocation(item, "23.45")
    parent_txn = _make_parent_txn(amount=-23450, date_str="2026-01-15", account="Rewards Visa")

    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "r"}
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_client = Mock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize_amazon_items(parent_txn, [allocation], CATEGORIES_FIXTURE, "key")

    call_kwargs = mock_client.messages.create.call_args.kwargs
    user_msg = call_kwargs["messages"][0]["content"]
    assert "My Test Widget" in user_msg
    assert "B0TEST123" in user_msg
    assert "qty: 2" in user_msg
    assert "$23.45" in user_msg
    assert "2026-01-15" in user_msg
    assert "Rewards Visa" in user_msg


def _profiles_with_description(cat_id, name, description, exemplars=None):
    from category_profiles import PROFILES_VERSION
    return {
        "_version": PROFILES_VERSION,
        "categories": {
            cat_id: {
                "name": name,
                "description": description,
                "merchants": [],
                "item_exemplars": exemplars or {},
                "items_since_generation": 0,
                "dirty": False,
            }
        },
    }


def test_claude_categorize_amazon_items_injects_profile_descriptions():
    """When profiles are passed, the system prompt carries learned descriptions."""
    from categorizer import claude_categorize_amazon_items

    cat_id = "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90"
    profiles = _profiles_with_description(
        cat_id, "Shopping", "Everyday household items and accessories."
    )
    allocations = [_make_allocation(_make_item())]
    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "r"}
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_client = Mock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize_amazon_items(
            _make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key", profiles=profiles
        )

    system_prompt = mock_client.messages.create.call_args.kwargs["system"]
    assert "Everyday household items and accessories." in system_prompt
    assert cat_id in system_prompt


def test_claude_categorize_amazon_items_no_profiles_uses_bare_names():
    """Backward compat: with profiles=None the prompt still lists bare category names/ids."""
    from categorizer import claude_categorize_amazon_items

    allocations = [_make_allocation(_make_item())]
    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "r"}
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_client = Mock()
        mock_cls.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        claude_categorize_amazon_items(
            _make_parent_txn(), allocations, CATEGORIES_FIXTURE, "key"
        )

    system_prompt = mock_client.messages.create.call_args.kwargs["system"]
    assert "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90" in system_prompt
    assert "Electronics" in system_prompt


# ============================================================================
# Tests for Phase D: categorize_transactions 4-tuple return (Issue #84)
# ============================================================================

import copy
from amazon_matcher import (
    filter_amazon_transactions, match_shipments_to_transactions,
    parse_order_history, MatchCandidate,
)


def test_categorize_transactions_backward_compat_amazon_matches_none():
    """With amazon_matches=None, Amazon txns flow through normal Tier-3 pipeline."""
    cache = {}
    amazon_txn = {"id": "amz1", "payee_name": "Amazon.com", "amount": -10000, "date": "2026-01-15"}
    normal_txn = {"id": "nrm1", "payee_name": "Target", "amount": -5000, "date": "2026-01-15"}
    categories = [{"id": "g1", "name": "Shopping", "categories": [{"id": "c1", "name": "Retail"}]}]

    tier3_response = json.dumps([
        {"payee_name": "Amazon.com", "category_id": "c1", "category_name": "Retail",
         "confidence": 0.7, "rationale": "r", "prior_strength": 5},
        {"payee_name": "Target", "category_id": "c1", "category_name": "Retail",
         "confidence": 0.8, "rationale": "r", "prior_strength": 8},
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=tier3_response)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
            [amazon_txn, normal_txn], cache, categories, "key"
        )

    assert len(results) == 2
    assert unmatched_amazon == []
    assert split_proposals == []
    # Amazon txn went through tier 3 (not the Amazon branch)
    amazon_result = next(r for r in results if r.transaction_id == "amz1")
    assert amazon_result.tier == "claude"


def test_categorize_transactions_amazon_single_item_goes_to_proposals():
    """Single-item Amazon match goes to split_proposals (not results)."""
    from amazon_matcher import AmazonShipment, MatchCandidate
    
    item = _make_item(asin="B001", product_name="Widget")
    shipment = AmazonShipment(
        order_id="111-0000001", ship_date=date(2026,1,15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("10.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("10.00"), items=[item],
        shipment_status="Shipped",
    )
    amazon_txn = {"id": "amz1", "payee_name": "Amazon.com", "amount": -10000,
                  "date": "2026-01-15", "account_name": "Visa"}
    match = MatchCandidate(ynab_txn=amazon_txn, shipment=shipment, date_delta_days=0)
    match_result = MatchResult(
        matched=[match],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    response_data = json.dumps([
        {"item_index": 1, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
         "category_name": "Shopping", "confidence": 0.9, "rationale": "r"}
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
            [amazon_txn], {}, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
        )

    assert results == []  # Amazon does NOT go into results
    assert len(split_proposals) == 1
    assert len(split_proposals[0].subtransactions) == 1
    assert split_proposals[0].total_allocated() == Decimal("10.00")


def test_categorize_transactions_amazon_multi_item_goes_to_proposals():
    """Multi-item Amazon match goes to split_proposals."""
    from amazon_matcher import AmazonShipment, MatchCandidate
    
    items = [_make_item(asin=f"B00{i}", product_name=f"Item {i}") for i in range(3)]
    shipment = AmazonShipment(
        order_id="111-0000002", ship_date=date(2026,1,15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("30.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("30.00"), items=items,
        shipment_status="Shipped",
    )
    amazon_txn = {"id": "amz2", "payee_name": "AMZN Mktp US", "amount": -30000,
                  "date": "2026-01-15", "account_name": "Visa"}
    match = MatchCandidate(ynab_txn=amazon_txn, shipment=shipment, date_delta_days=0)
    match_result = MatchResult(
        matched=[match],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    response_data = json.dumps([
        {"item_index": i+1, "category_id": "e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
         "category_name": "Shopping", "confidence": 0.9, "rationale": "r"}
        for i in range(3)
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
            [amazon_txn], {}, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
        )

    assert results == []
    assert len(split_proposals) == 1
    assert len(split_proposals[0].subtransactions) == 3
    assert split_proposals[0].total_allocated() == Decimal("30.00")


def test_categorize_transactions_amazon_wf_short_circuits_to_groceries():
    """Whole Foods/Fresh shipments skip the split and emit a flat Groceries result.

    No Claude call should be made (verify by raising if the mock is invoked).
    """
    from amazon_matcher import AmazonShipment, MatchCandidate

    wf_categories = [{
        "id": "grp1", "name": "Everyday",
        "categories": [
            {"id": "groc-id", "name": "Groceries"},
            {"id": "ship-id", "name": "Shopping"},
        ],
    }]

    items = [
        _make_item(asin="WF1", product_name="365 by Whole Foods Market Organic Spinach"),
        _make_item(asin="WF2", product_name="Whole Foods Market Organic Coffee"),
    ]
    shipment = AmazonShipment(
        order_id="113-5220110-0665003", ship_date=date(2026, 1, 15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("20.00"), items=items,
        shipment_status="Shipped",
        website="Amazon.com",
        carrier="RABBIT(spARA3C26rGGDQ) and RABBIT(spARA3C264S57V)",
    )
    assert shipment.is_wf

    amazon_txn = {"id": "amz-wf", "payee_name": "Amazon", "amount": -20000,
                  "date": "2026-01-15", "account_name": "Visa"}
    match = MatchCandidate(ynab_txn=amazon_txn, shipment=shipment, date_delta_days=0)
    match_result = MatchResult(
        matched=[match], unmatched_ynab=[], unmatched_shipments=[],
        excluded_shipments=[], parse_errors=[],
    )

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = AssertionError(
            "Claude must not be called for WF Amazon shipments"
        )
        results, _skipped, unmatched_amazon, split_proposals = categorize_transactions(
            [amazon_txn], {}, wf_categories, "key", amazon_matches=match_result
        )

    assert split_proposals == []
    assert unmatched_amazon == []
    assert len(results) == 1
    r = results[0]
    assert r.transaction_id == "amz-wf"
    assert r.category_name == "Groceries"
    assert r.category_id == "groc-id"
    assert r.tier == "amazon-wf"


def test_categorize_transactions_amazon_wf_missing_groceries_category_raises():
    """If 'Groceries' doesn't exist in YNAB, the WF short-circuit fails loudly."""
    from amazon_matcher import AmazonShipment, MatchCandidate

    categories_without_groceries = [{
        "id": "grp1", "name": "Everyday",
        "categories": [{"id": "ship-id", "name": "Shopping"}],
    }]

    item = _make_item(product_name="365 by Whole Foods Market Organic Spinach")
    shipment = AmazonShipment(
        order_id="113-5220110-0665003", ship_date=date(2026, 1, 15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("10.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("10.00"), items=[item],
        shipment_status="Shipped",
        website="PrimeNow-US",
    )
    amazon_txn = {"id": "amz-wf", "payee_name": "Amazon", "amount": -10000,
                  "date": "2026-01-15", "account_name": "Visa"}
    match = MatchCandidate(ynab_txn=amazon_txn, shipment=shipment, date_delta_days=0)
    match_result = MatchResult(
        matched=[match], unmatched_ynab=[], unmatched_shipments=[],
        excluded_shipments=[], parse_errors=[],
    )

    with pytest.raises(RuntimeError, match="Groceries"):
        categorize_transactions(
            [amazon_txn], {}, categories_without_groceries, "key",
            amazon_matches=match_result,
        )


def test_categorize_transactions_cache_untouched_for_amazon():
    """Amazon txns must NOT modify the payee cache."""
    from amazon_matcher import AmazonShipment, MatchCandidate
    
    cache = _v2_cache({"amazon.com": ("c1", "Shopping")})
    initial_cache = copy.deepcopy(cache)

    item1 = _make_item()
    item2 = _make_item()
    item3 = _make_item()
    ship1 = AmazonShipment(
        order_id="111-0000001", ship_date=date(2026,1,15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("10.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("10.00"), items=[item1],
        shipment_status="Shipped",
    )
    ship2 = AmazonShipment(
        order_id="111-0000002", ship_date=date(2026,1,16),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("20.00"), items=[item2, item3],
        shipment_status="Shipped",
    )
    txn1 = {"id": "amz1", "payee_name": "Amazon.com", "amount": -10000,
             "date": "2026-01-15", "account_name": "Visa"}
    txn2 = {"id": "amz2", "payee_name": "Amazon.com", "amount": -20000,
             "date": "2026-01-16", "account_name": "Visa"}
    match1 = MatchCandidate(ynab_txn=txn1, shipment=ship1, date_delta_days=0)
    match2 = MatchCandidate(ynab_txn=txn2, shipment=ship2, date_delta_days=0)
    match_result = MatchResult(
        matched=[match1, match2],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    response_data = json.dumps([
        {"item_index": 1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "r"}
    ])
    mock_response = Mock()
    mock_response.content = [Mock(text=response_data)]
    mock_response.stop_reason = "end_turn"

    response_data2 = json.dumps([
        {"item_index": i+1, "category_id": None, "category_name": None, "confidence": 0.0, "rationale": "r"}
        for i in range(2)
    ])
    mock_response2 = Mock()
    mock_response2.content = [Mock(text=response_data2)]
    mock_response2.stop_reason = "end_turn"

    with patch("categorizer.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.side_effect = [mock_response, mock_response2]
        categorize_transactions([txn1, txn2], cache, CATEGORIES_FIXTURE, "key", amazon_matches=match_result)

    assert cache == initial_cache, "Cache was modified by Amazon txns — invariant violated"


def test_categorize_transactions_allocator_error_goes_to_unmatched():
    """Allocator RuntimeError → unmatched_amazon (no Claude call)."""
    from amazon_matcher import AmazonShipment, MatchCandidate
    
    item = _make_item()
    shipment = AmazonShipment(
        order_id="111-0000001", ship_date=date(2026,1,15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("10.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("10.00"), items=[item],
        shipment_status="Shipped",
    )
    amazon_txn = {"id": "amz1", "payee_name": "Amazon.com", "amount": -10000,
                  "date": "2026-01-15", "account_name": "Visa"}
    match = MatchCandidate(ynab_txn=amazon_txn, shipment=shipment, date_delta_days=0)
    match_result = MatchResult(
        matched=[match],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    with patch("categorizer.allocate_shipment_to_items", side_effect=RuntimeError("subtotal mismatch")):
        with patch("categorizer.anthropic.Anthropic") as mock_cls:
            results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
                [amazon_txn], {}, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
            )
            mock_cls.assert_not_called()  # Claude must NOT be called

    assert len(unmatched_amazon) == 1
    assert "allocator failed" in unmatched_amazon[0][1]
    assert "subtotal mismatch" in unmatched_amazon[0][1]


def test_categorize_transactions_matcher_invariant_violation():
    """If Amazon txn is in neither matched nor unmatched_ynab, RuntimeError."""
    amazon_txn = {"id": "ghost-id", "payee_name": "Amazon.com", "amount": -10000, "date": "2026-01-15"}
    match_result = MatchResult(
        matched=[],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    with pytest.raises(RuntimeError, match="matcher invariant violated"):
        categorize_transactions([amazon_txn], {}, [], "key", amazon_matches=match_result)


def test_categorize_transactions_allocation_invariant_violation():
    """If allocations don't sum to parent amount, RuntimeError with diagnostic."""
    from amazon_matcher import AmazonShipment, MatchCandidate

    item = _make_item()
    shipment = AmazonShipment(
        order_id="111-0000001", ship_date=date(2026,1,15),
        payment_method_raw="Visa - 1234", payment_method_last4="1234",
        is_split_tender=False, currency="USD",
        item_subtotal=Decimal("10.00"), tax=Decimal("0"),
        shipping=Decimal("0"), discounts=Decimal("0"),
        total_amount=Decimal("10.00"), items=[item],
        shipment_status="Shipped",
    )
    amazon_txn = {"id": "amz1", "payee_name": "Amazon.com", "amount": -10000,
                  "date": "2026-01-15", "account_name": "Visa"}
    match = MatchCandidate(ynab_txn=amazon_txn, shipment=shipment, date_delta_days=0)
    match_result = MatchResult(
        matched=[match],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    # Mock allocator to return wrong total
    mock_alloc = Mock()
    mock_alloc.item = item
    mock_alloc.allocated_amount = Decimal("9.00")  # Should be 10.00

    with patch("categorizer.allocate_shipment_to_items", return_value=[mock_alloc]):
        with patch("categorizer.claude_categorize_amazon_items") as mock_claude:
            # Claude returns one result with allocated_amount from the mock
            mock_claude.return_value = [ItemCategoryResult(
                ynab_transaction_id="amz1", item=item, allocated_amount=Decimal("9.00"),
                category_id="c1", category_name="Shopping", confidence=0.9, rationale="r"
            )]
            with pytest.raises(RuntimeError, match="total_allocated.*parent_amount"):
                categorize_transactions(
                    [amazon_txn], {}, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
                )


def test_categorize_transactions_excluded_shipment_unmatched():
    """YNAB Amazon txn whose only shipment was excluded → unmatched_amazon with reason."""
    amazon_txn = {
        "id": "amz-excluded",
        "payee_name": "Amazon.com",
        "amount": -5000,
        "date": "2026-01-15",
        "account_name": "Visa"
    }
    match_result = MatchResult(
        matched=[],
        unmatched_ynab=[(amazon_txn, "shipment excluded: split tender")],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
        [amazon_txn], {}, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
    )

    assert len(unmatched_amazon) == 1
    assert unmatched_amazon[0][0]["id"] == "amz-excluded"
    assert "split tender" in unmatched_amazon[0][1]


def test_integration_orchestrator_end_to_end():
    """End-to-end: parse CSV → match → categorize with real fixture data, all txn types."""
    from amazon_matcher import (
        parse_order_history, filter_amazon_transactions, match_shipments_to_transactions
    )

    # Load CSV and YNAB txns from fixtures
    csv_path = Path("data/fixtures/amazon_order_history_sample.csv")
    txns_path = Path("data/fixtures/amazon_ynab_transactions.json")

    csv_text = csv_path.read_text()
    shipments, _ = parse_order_history(csv_text)
    ynab_txns = json.loads(txns_path.read_text())

    # Include uncategorized AND create a Transfer txn to test skipping
    uncategorized = [t for t in ynab_txns if t.get("category_id") is None]
    transfer_txn = {
        "id": "txn-transfer-001",
        "payee_name": "Transfer : Some Account",
        "amount": -5000,
        "date": "2026-01-17",
        "account_name": "Visa"
    }
    # Note: match_result will only know about Amazon txns in the filtered set
    amazon_txns = filter_amazon_transactions(uncategorized)
    match_result = match_shipments_to_transactions(amazon_txns, shipments)

    # Build test_txns: include only txns that match_result knows about
    # (either matched, unmatched, or non-Amazon per filter criteria)
    matched_ids = {m.ynab_txn["id"] for m in match_result.matched}
    unmatched_ids = {t["id"] for t, _ in match_result.unmatched_ynab}
    amazon_in_result = matched_ids | unmatched_ids
    amazon_txn_ids = {t["id"] for t in amazon_txns}

    # Include: Amazon txns in match_result, non-Amazon txns, Transfer
    test_txns = []
    for t in uncategorized:
        if t["id"] in amazon_in_result:  # Amazon txn in match_result
            test_txns.append(t)
        elif t["id"] not in amazon_txn_ids and not is_amazon_payee(t.get("payee_name")):  # Non-Amazon
            test_txns.append(t)
    test_txns.append(transfer_txn)

    # Verify fixture has the expected structure
    assert len(match_result.matched) > 0, "Fixture should have matched shipments"
    assert len(match_result.unmatched_ynab) > 0, "Fixture should have unmatched YNAB txns"

    # Mock Claude to return categorizations for all items
    def mock_claude_response(parent_txn, allocations, categories, api_key, profiles=None):
        results = []
        for i, alloc in enumerate(allocations, 1):
            results.append(ItemCategoryResult(
                ynab_transaction_id=parent_txn["id"],
                item=alloc.item,
                allocated_amount=alloc.allocated_amount,
                category_id="e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
                category_name="Shopping",
                confidence=0.85,
                rationale=f"Item {i}"
            ))
        return results

    cache = {}

    def mock_claude_categorize(batch, categories, api_key):
        """Mock claude_categorize for non-Amazon txns (Target, etc.)."""
        return [CategoryResult(
            transaction_id=t["id"],
            category_id="e5d6a5ee-c297-4e4f-8bc9-64c3b15b4c90",
            category_name="Shopping",
            confidence=0.9,
            rationale="Non-Amazon mock",
            tier="claude"
        ) for t in batch]

    with patch("categorizer.claude_categorize_amazon_items", side_effect=mock_claude_response):
        with patch("categorizer.claude_categorize", side_effect=mock_claude_categorize):
            results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
                test_txns, cache, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
            )

    # Assertions: verify all txn types routed correctly
    assert len(split_proposals) == len(match_result.matched), \
        f"Expected {len(match_result.matched)} proposals, got {len(split_proposals)}"

    # Each proposal must sum to parent amount
    for proposal in split_proposals:
        parent_amount = abs(Decimal(proposal.parent_ynab_txn["amount"])) / Decimal("1000")
        parent_quantized = parent_amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
        assert proposal.total_allocated() == parent_quantized, \
            f"Proposal {proposal.shipment.order_id} totals mismatch"

    # Unmatched Amazon txns: verify tuple unpacking and reason preservation
    unmatched_ids = {t["id"] for t, _ in match_result.unmatched_ynab}
    assert len(unmatched_amazon) == len(match_result.unmatched_ynab), \
        f"Expected {len(match_result.unmatched_ynab)} unmatched Amazon, got {len(unmatched_amazon)}"
    for unmatched_txn, reason in unmatched_amazon:
        assert unmatched_txn["id"] in unmatched_ids, \
            f"Txn {unmatched_txn['id']} not in matcher.unmatched_ynab"
        assert reason, "Reason should be non-empty"

    # Transfer must be skipped
    assert any(t["id"] == transfer_txn["id"] for t in skipped), \
        "Transfer txn should be in skipped"

    # Cache remains empty (Amazon never writes to it)
    assert cache == {}, "Amazon processing should not modify cache"


def test_categorize_transactions_unmatched_ynab_tuple_unpacking():
    """Amazon txn in unmatched_ynab → lands in unmatched_amazon with reason preserved."""
    amazon_txn = {
        "id": "amz-no-shipment",
        "payee_name": "Amazon.com",
        "amount": -10000,
        "date": "2026-01-15",
        "account_name": "Visa"
    }
    match_result = MatchResult(
        matched=[],
        unmatched_ynab=[(amazon_txn, "no matching shipment in CSV dump")],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
        [amazon_txn], {}, CATEGORIES_FIXTURE, "key", amazon_matches=match_result
    )

    assert results == []
    assert skipped == []
    assert split_proposals == []
    assert len(unmatched_amazon) == 1
    assert unmatched_amazon[0][0] == amazon_txn
    assert "no matching shipment" in unmatched_amazon[0][1]


class TestUpdateCacheFromClaudeResults:
    """Test update_cache_from_claude_results extraction logic."""

    def test_claude_tier_calls_record_categorization(self):
        """CategoryResult with tier='claude' triggers cache update."""
        cache = {}
        source_txns = [{"id": "t1", "payee_name": "Amazon Store", "import_payee_name": None}]
        results = [
            CategoryResult(
                transaction_id="t1",
                category_id="cat-123",
                category_name="Shopping",
                tier="claude",
                confidence=0.95,
                rationale="test",
                prior_strength=2,
            )
        ]

        update_cache_from_claude_results(cache, source_txns, results)

        # Cache should have been updated (payee name is normalized to lowercase)
        assert "amazon store" in cache
        assert cache["amazon store"]["categories"]["cat-123"]["name"] == "Shopping"
        assert cache["amazon store"]["total"] == 2

    def test_history_tier_not_called(self):
        """CategoryResult with tier='history' does not trigger cache update."""
        cache = {}
        source_txns = [{"id": "t1", "payee_name": "Store A"}]
        results = [
            CategoryResult(
                transaction_id="t1",
                category_id="cat-123",
                category_name="Shopping",
                tier="history",
                confidence=1.0,
                rationale="",
                prior_strength=None,
            )
        ]

        update_cache_from_claude_results(cache, source_txns, results)

        # Cache should not be updated
        assert cache == {}

    def test_fuzzy_tier_not_called(self):
        """CategoryResult with tier='fuzzy' does not trigger cache update."""
        cache = {}
        source_txns = [{"id": "t1", "payee_name": "Store B"}]
        results = [
            CategoryResult(
                transaction_id="t1",
                category_id="cat-456",
                category_name="Groceries",
                tier="fuzzy",
                confidence=0.85,
                rationale="",
                prior_strength=None,
            )
        ]

        update_cache_from_claude_results(cache, source_txns, results)

        assert cache == {}

    def test_item_category_result_not_processed(self):
        """ItemCategoryResult objects are not processed."""
        from amazon_matcher import AmazonItem
        from decimal import Decimal
        from datetime import date

        cache = {}
        source_txns = [{"id": "t1", "payee_name": "Amazon"}]

        # ItemCategoryResult doesn't have a tier attribute
        item = AmazonItem(
            order_id="ord-123",
            ship_date=date(2026, 1, 1),
            asin="B123",
            product_name="Widget",
            quantity=1,
            unit_price=Decimal("10.00"),
            unit_price_tax=Decimal("0.00"),
            raw_row_index=1,
        )
        item_result = ItemCategoryResult(
            ynab_transaction_id="t1",
            item=item,
            allocated_amount=Decimal("10.00"),
            category_id="cat-789",
            category_name="Purchases",
            confidence=0.9,
            rationale="test",
        )

        results = [item_result]

        update_cache_from_claude_results(cache, source_txns, results)

        # Cache should not be updated (ItemCategoryResult is not CategoryResult)
        assert cache == {}

    def test_prior_strength_default(self):
        """When prior_strength is None, defaults to 1."""
        cache = {}
        source_txns = [{"id": "t1", "payee_name": "Store C"}]
        results = [
            CategoryResult(
                transaction_id="t1",
                category_id="cat-999",
                category_name="Other",
                tier="claude",
                confidence=0.9,
                rationale="test",
                prior_strength=None,  # Will default to 1
            )
        ]

        with patch("categorizer.record_categorization") as mock_record:
            update_cache_from_claude_results(cache, source_txns, results)
            # Check that prior_strength=1 was passed
            call_kwargs = mock_record.call_args[1]
            assert call_kwargs["prior_strength"] == 1

    def test_import_names_both_present(self):
        """Both import_payee_name and import_payee_name_original collected."""
        cache = {}
        source_txns = [
            {
                "id": "t1",
                "payee_name": "Store D",
                "import_payee_name": "STORE D LLC",
                "import_payee_name_original": "store d llc",
            }
        ]
        results = [
            CategoryResult(
                transaction_id="t1",
                category_id="cat-111",
                category_name="Retail",
                tier="claude",
                confidence=0.92,
                rationale="test",
                prior_strength=1,
            )
        ]

        with patch("categorizer.record_categorization") as mock_record:
            update_cache_from_claude_results(cache, source_txns, results)
            call_kwargs = mock_record.call_args[1]
            # Should have both import names in list
            assert len(call_kwargs["import_names"]) == 2

    def test_no_import_names(self):
        """No import names passed → import_names=None."""
        cache = {}
        source_txns = [{"id": "t1", "payee_name": "Store E"}]
        results = [
            CategoryResult(
                transaction_id="t1",
                category_id="cat-222",
                category_name="Restaurant",
                tier="claude",
                confidence=0.88,
                rationale="test",
                prior_strength=1,
            )
        ]

        with patch("categorizer.record_categorization") as mock_record:
            update_cache_from_claude_results(cache, source_txns, results)
            call_kwargs = mock_record.call_args[1]
            assert call_kwargs["import_names"] is None

    def test_txn_not_found_skipped(self):
        """If transaction_id not in source_txns, skip silently."""
        cache = {}
        source_txns = [{"id": "t999", "payee_name": "Other"}]
        results = [
            CategoryResult(
                transaction_id="t1",  # This doesn't exist in source_txns
                category_id="cat-333",
                category_name="Travel",
                tier="claude",
                confidence=0.85,
                rationale="test",
                prior_strength=1,
            )
        ]

        with patch("categorizer.record_categorization") as mock_record:
            update_cache_from_claude_results(cache, source_txns, results)
            # Should not be called
            mock_record.assert_not_called()

    def test_mixed_results(self):
        """Multiple results with mixed tiers processes only claude tier."""
        cache = {}
        source_txns = [
            {"id": "t1", "payee_name": "Store F"},
            {"id": "t2", "payee_name": "Store G"},
            {"id": "t3", "payee_name": "Store H"},
        ]
        results = [
            CategoryResult(transaction_id="t1", category_id="c1", category_name="Cat1", tier="history", confidence=1.0, rationale="", prior_strength=None),
            CategoryResult(transaction_id="t2", category_id="c2", category_name="Cat2", tier="claude", confidence=0.9, rationale="test", prior_strength=1),
            CategoryResult(transaction_id="t3", category_id="c3", category_name="Cat3", tier="fuzzy", confidence=0.8, rationale="", prior_strength=None),
        ]

        with patch("categorizer.record_categorization") as mock_record:
            update_cache_from_claude_results(cache, source_txns, results)
            # Should be called exactly once (only for t2)
            assert mock_record.call_count == 1
            call_kwargs = mock_record.call_args[1]
            assert call_kwargs["source"] == "claude"
