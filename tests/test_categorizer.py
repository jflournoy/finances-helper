"""Tests for categorizer.py — payee normalization, tier routing."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, Mock
from categorizer import (
    CategoryResult,
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
    _dominant_category,
    _resolve_alias,
    normalize_import_payee,
)


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


def test_categorize_transactions_transfer_not_miscategorized():
    cache = _v2_cache({"transfer": ("water_id", "Water (addup)")})
    transactions = [
        {"id": "txn1", "payee_name": "Transfer : Classic Checking -- 6190", "amount": -5000, "date": "2026-03-01"},
    ]
    categories = [{"id": "g1", "name": "Bills", "categories": [{"id": "c1", "name": "Water"}]}]

    mock_response = Mock()
    mock_response.content = [Mock(text='[{"payee_name": "Transfer : Classic Checking -- 6190", "category_id": "c1", "category_name": "Water", "confidence": 0.7, "rationale": "r", "prior_strength": 10}]')]

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        mock_client = Mock()
        mock_anthropic_class.return_value = mock_client
        mock_client.messages.create.return_value = mock_response
        results = categorize_transactions(transactions, cache, categories, "test-key")

    assert len(results) == 1
    assert results[0].tier == "claude"


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
    # 20 transactions * 100 = 2000, which is > 1024 minimum
    assert call_kwargs["max_tokens"] == 2000


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
        results = categorize_transactions(transactions, cache, categories, "test-key")

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
        results = categorize_transactions(transactions, cache, categories, "test-key")

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
    mock_response.content = [Mock(text='[{"payee_name": "Unknown1", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10},{"payee_name": "Unknown2", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10},{"payee_name": "Unknown3", "category_id": "c1", "category_name": "Retail", "confidence": 0.7, "rationale": "r", "prior_strength": 10}]')]

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
    mock_response.content = [Mock(text='[{"payee_name": "NewNovelStore", "category_id": "dddddddd-0000-0000-0000-000000000003", "category_name": "Groceries", "confidence": 0.85, "rationale": "test", "prior_strength": 10}]')]

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
            "rationale": "r",
            "prior_strength": 10,
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
            "rationale": "r",
            "prior_strength": 10,
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
        results = categorize_transactions(transfer_transactions, cache, categories, "test-key")

    assert len(results) == 3
    for result in results:
        assert result.tier == "claude", f"Transaction {result.transaction_id} matched via {result.tier}, expected claude"
        assert result.category_name != "Water (addup)", f"Transaction {result.transaction_id} incorrectly matched to Water (addup)"


def test_categorize_transactions_legitimate_transfer_exact_match():
    """A bare 'Transfer' payee should still match 'transfer' in cache via history lookup."""
    cache = _v2_cache({"transfer": ("water_id", "Water (addup)")})
    transactions = [{"id": "txn1", "payee_name": "Transfer", "amount": -5000, "date": "2026-03-01"}]
    categories = []

    with patch("categorizer.anthropic.Anthropic") as mock_anthropic_class:
        results = categorize_transactions(transactions, cache, categories, "test-key")

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

    # Verify cache was updated with the claude result (v2 format)
    saved_cache = json.loads((cache_dir / "payee_lookup.json").read_text())
    assert "newplace" in saved_cache
    assert saved_cache["newplace"]["categories"]["c2"]["name"] == "Dining"
    assert saved_cache["newplace"]["total"] == 1


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


def test_load_payee_cache_v1_fixture():
    """The v1 fixture file can be loaded and migrates correctly."""
    result = load_payee_cache("data/fixtures/payee_cache_v1.json")
    assert result["_version"] == 2
    assert result["whole foods"]["total"] == 1


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
        results = categorize_transactions(txns, cache, [], "key")

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
        results = categorize_transactions(txns, cache, categories, "key")

    assert results[0].prior_strength == 12


