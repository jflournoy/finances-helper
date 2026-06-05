"""Tests for category_profiles.py — learned per-category descriptions and item exemplars.

The category profile store teaches Claude what each YNAB category *means* in this
user's budget, so item-level Amazon categorization stops relying on the generic
English meaning of a category's name (e.g. "USB cable" -> "Computer" when it
should be "Home Goods").

Two levels of signal:
1. Bootstrap descriptions — generated once from each category's high-frequency
   merchants (Claude call), then cached.
2. Item exemplars — accumulated from confirmed Amazon splits with frequency
   counts, mirroring the payee frequency cache (dominant wins, recent breaks ties).
"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, Mock

from category_profiles import (
    PROFILES_VERSION,
    normalize_item_name,
    load_profiles,
    save_profiles,
    record_item_categorization,
    dominant_item_category,
    mark_dirty,
    is_stale,
    STALE_ITEM_THRESHOLD,
    bootstrap_descriptions,
    regenerate_stale,
    format_profiles_for_prompt,
    build_merchant_map_from_cache,
    sync_merchants_into_profiles,
    backfill_from_ynab_subtransactions,
    record_confirmed_splits,
)


# ---------------------------------------------------------------------------
# normalize_item_name
# ---------------------------------------------------------------------------

def test_normalize_item_name_lowercases_and_collapses_whitespace():
    assert normalize_item_name("  USB-C  Cable  ") == "usb-c cable"


def test_normalize_item_name_strips_trailing_pack_size_noise():
    # Product names carry pack/quantity noise that shouldn't fragment the key.
    a = normalize_item_name("AmazonBasics USB Cable (Pack of 2)")
    b = normalize_item_name("AmazonBasics USB Cable (Pack of 6)")
    assert a == b


def test_normalize_item_name_rejects_empty():
    with pytest.raises(ValueError):
        normalize_item_name("   ")


# ---------------------------------------------------------------------------
# load / save / version
# ---------------------------------------------------------------------------

def test_load_profiles_missing_file_returns_empty_versioned(tmp_path):
    profiles = load_profiles(str(tmp_path / "nope.json"))
    assert profiles["_version"] == PROFILES_VERSION
    assert profiles["categories"] == {}


def test_save_then_load_roundtrips(tmp_path):
    path = str(tmp_path / "category_profiles.json")
    profiles = load_profiles(path)
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods",
        "description": "Everyday household items and accessories.",
        "merchants": ["Target", "Costco"],
        "item_exemplars": {},
        "items_since_generation": 0,
        "dirty": False,
    }
    save_profiles(profiles, path)
    reloaded = load_profiles(path)
    assert reloaded["categories"]["cat-home"]["description"].startswith("Everyday")


def test_load_profiles_invalid_json_raises_loudly(tmp_path):
    path = tmp_path / "category_profiles.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="Invalid JSON"):
        load_profiles(str(path))


# ---------------------------------------------------------------------------
# record_item_categorization + frequency conflict resolution
# ---------------------------------------------------------------------------

def _empty():
    return {"_version": PROFILES_VERSION, "categories": {}}


def test_record_item_creates_exemplar_entry():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "", "merchants": [],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    record_item_categorization(profiles, "USB Cable", "cat-home", "Home Goods")
    ex = profiles["categories"]["cat-home"]["item_exemplars"]
    assert "usb cable" in ex
    assert ex["usb cable"]["cat-home"]["count"] == 1


def test_record_item_autocreates_missing_category():
    profiles = _empty()
    record_item_categorization(profiles, "GPU", "cat-comp", "Computer")
    assert "cat-comp" in profiles["categories"]
    assert profiles["categories"]["cat-comp"]["item_exemplars"]["gpu"]["cat-comp"]["count"] == 1


def test_dominant_item_category_frequency_weighted():
    # "usb cable" seen 4x in Home Goods, 1x in Computer -> Home Goods wins.
    profiles = _empty()
    for _ in range(4):
        record_item_categorization(profiles, "usb cable", "cat-home", "Home Goods")
    record_item_categorization(profiles, "usb cable", "cat-comp", "Computer")
    cat_id, cat_name = dominant_item_category(profiles, "usb cable")
    assert cat_id == "cat-home"
    assert cat_name == "Home Goods"


def test_dominant_item_category_unknown_returns_none():
    profiles = _empty()
    assert dominant_item_category(profiles, "never seen") == (None, None)


def test_record_item_marks_category_dirty_and_counts():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "old desc", "merchants": [],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    record_item_categorization(profiles, "Batteries", "cat-home", "Home Goods")
    cat = profiles["categories"]["cat-home"]
    assert cat["items_since_generation"] == 1
    assert cat["dirty"] is True


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------

def test_is_stale_true_when_dirty_and_enough_new_items():
    cat = {"dirty": True, "items_since_generation": STALE_ITEM_THRESHOLD, "description": "x"}
    assert is_stale(cat) is True


def test_is_stale_false_below_threshold():
    cat = {"dirty": True, "items_since_generation": STALE_ITEM_THRESHOLD - 1, "description": "x"}
    assert is_stale(cat) is False


def test_is_stale_true_when_description_empty():
    # A category that has merchants but no description yet is always stale.
    cat = {"dirty": False, "items_since_generation": 0, "description": ""}
    assert is_stale(cat) is True


def test_mark_dirty_sets_flag():
    profiles = _empty()
    profiles["categories"]["c"] = {
        "name": "C", "description": "d", "merchants": [],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    mark_dirty(profiles, "c")
    assert profiles["categories"]["c"]["dirty"] is True


def test_mark_dirty_unknown_category_raises():
    profiles = _empty()
    with pytest.raises(ValueError, match="unknown category_id"):
        mark_dirty(profiles, "nope")


# ---------------------------------------------------------------------------
# sync_merchants_into_profiles
# ---------------------------------------------------------------------------

def test_sync_merchants_creates_categories_and_marks_dirty():
    profiles = _empty()
    mmap = {"cat-home": {"name": "Home Goods", "merchants": ["Target", "Costco"]}}
    sync_merchants_into_profiles(profiles, mmap)
    cat = profiles["categories"]["cat-home"]
    assert cat["merchants"] == ["Target", "Costco"]
    assert cat["dirty"] is True


def test_sync_merchants_no_change_leaves_clean():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "d", "merchants": ["Target"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    sync_merchants_into_profiles(profiles, {"cat-home": {"name": "Home Goods", "merchants": ["Target"]}})
    assert profiles["categories"]["cat-home"]["dirty"] is False


# ---------------------------------------------------------------------------
# build_merchant_map_from_cache — derive per-category merchants from payee cache
# ---------------------------------------------------------------------------

def test_build_merchant_map_groups_payees_by_dominant_category():
    payee_cache = {
        "_version": 2,
        "target": {"total": 5, "categories": {"cat-home": {"name": "Home Goods", "count": 5}}},
        "best buy": {"total": 3, "categories": {"cat-comp": {"name": "Computer", "count": 3}}},
        "costco": {"total": 4, "categories": {"cat-home": {"name": "Home Goods", "count": 4}}},
    }
    mmap = build_merchant_map_from_cache(payee_cache)
    assert set(mmap["cat-home"]["merchants"]) == {"target", "costco"}
    assert mmap["cat-comp"]["merchants"] == ["best buy"]
    assert mmap["cat-home"]["name"] == "Home Goods"


def test_build_merchant_map_skips_aliases():
    payee_cache = {
        "_version": 2,
        "amazon": {"total": 2, "categories": {"cat-shop": {"name": "Shopping", "count": 2}}},
        "amzn mktp": {"alias_of": "amazon"},
    }
    mmap = build_merchant_map_from_cache(payee_cache)
    assert mmap["cat-shop"]["merchants"] == ["amazon"]


# ---------------------------------------------------------------------------
# backfill_from_ynab_subtransactions — seed exemplars from already-split history
# ---------------------------------------------------------------------------

def _amazon_split_txn(payee, subs):
    return {"id": "t1", "payee_name": payee, "subtransactions": subs}


def test_backfill_records_exemplars_from_amazon_splits():
    profiles = _empty()
    txns = [
        _amazon_split_txn("Amazon", [
            {"category_id": "cat-home", "category_name": "Home Goods", "memo": "USB Cable"},
            {"category_id": "cat-comp", "category_name": "Computer", "memo": "GPU"},
        ]),
    ]
    n = backfill_from_ynab_subtransactions(profiles, txns)
    assert n == 2
    assert dominant_item_category(profiles, "USB Cable") == ("cat-home", "Home Goods")
    assert dominant_item_category(profiles, "GPU") == ("cat-comp", "Computer")


def test_backfill_skips_non_amazon_payees():
    profiles = _empty()
    txns = [
        _amazon_split_txn("Costco", [
            {"category_id": "cat-home", "category_name": "Home Goods", "memo": "Paper Towels"},
        ]),
    ]
    n = backfill_from_ynab_subtransactions(profiles, txns)
    assert n == 0
    assert dominant_item_category(profiles, "Paper Towels") == (None, None)


def test_backfill_skips_subtransactions_without_memo_or_category():
    profiles = _empty()
    txns = [
        _amazon_split_txn("Amazon", [
            {"category_id": "cat-home", "category_name": "Home Goods", "memo": ""},
            {"category_id": None, "category_name": None, "memo": "Uncategorized item"},
            {"category_id": "cat-home", "category_name": "Home Goods", "memo": "Batteries"},
        ]),
    ]
    n = backfill_from_ynab_subtransactions(profiles, txns)
    assert n == 1
    assert dominant_item_category(profiles, "Batteries") == ("cat-home", "Home Goods")


def test_backfill_treats_history_as_weak_prior_outvoted_by_confirmations():
    # A historical split says "usb cable" -> Computer once. Then 2 fresh confirms
    # say Home Goods. Home Goods should win (latest consistent signal dominates).
    profiles = _empty()
    backfill_from_ynab_subtransactions(profiles, [
        _amazon_split_txn("Amazon", [
            {"category_id": "cat-comp", "category_name": "Computer", "memo": "usb cable"},
        ]),
    ])
    record_item_categorization(profiles, "usb cable", "cat-home", "Home Goods")
    record_item_categorization(profiles, "usb cable", "cat-home", "Home Goods")
    assert dominant_item_category(profiles, "usb cable") == ("cat-home", "Home Goods")


# ---------------------------------------------------------------------------
# record_confirmed_splits — learn from splits the user actually applied
# ---------------------------------------------------------------------------

def _applied_split(subs):
    """A changeset proposed_split shaped like _build_json_payload writes it."""
    return {
        "subtransactions": [
            {
                "item": {"asin": s.get("asin", "B0X"), "product_name": s["product_name"]},
                "category_id": s["category_id"],
                "category_name": s["category_name"],
            }
            for s in subs
        ]
    }


def test_record_confirmed_splits_records_each_subtransaction():
    profiles = _empty()
    splits = [
        _applied_split([
            {"product_name": "USB Cable", "category_id": "cat-home", "category_name": "Home Goods"},
            {"product_name": "GPU", "category_id": "cat-comp", "category_name": "Computer"},
        ]),
    ]
    n = record_confirmed_splits(profiles, splits)
    assert n == 2
    assert dominant_item_category(profiles, "USB Cable") == ("cat-home", "Home Goods")
    assert dominant_item_category(profiles, "GPU") == ("cat-comp", "Computer")


def test_record_confirmed_splits_skips_uncategorized_subtransactions():
    profiles = _empty()
    splits = [
        _applied_split([
            {"product_name": "Mystery", "category_id": None, "category_name": None},
            {"product_name": "Batteries", "category_id": "cat-home", "category_name": "Home Goods"},
        ]),
    ]
    n = record_confirmed_splits(profiles, splits)
    assert n == 1
    assert dominant_item_category(profiles, "Mystery") == (None, None)


def test_record_confirmed_splits_corrects_prior_via_frequency():
    # History/bootstrap said usb cable -> Computer. A confirmed apply says Home Goods.
    # With 1 vs 1 it's a tie broken alphabetically; a second confirm makes it decisive.
    profiles = _empty()
    record_item_categorization(profiles, "usb cable", "cat-comp", "Computer")
    record_confirmed_splits(profiles, [
        _applied_split([{"product_name": "usb cable", "category_id": "cat-home", "category_name": "Home Goods"}]),
    ])
    record_confirmed_splits(profiles, [
        _applied_split([{"product_name": "usb cable", "category_id": "cat-home", "category_name": "Home Goods"}]),
    ])
    assert dominant_item_category(profiles, "usb cable") == ("cat-home", "Home Goods")


def test_record_confirmed_splits_marks_categories_dirty():
    profiles = _empty()
    record_confirmed_splits(profiles, [
        _applied_split([{"product_name": "Lamp", "category_id": "cat-home", "category_name": "Home Goods"}]),
    ])
    assert profiles["categories"]["cat-home"]["dirty"] is True


# ---------------------------------------------------------------------------
# bootstrap_descriptions — Claude call to summarize each category from merchants
# ---------------------------------------------------------------------------

def _mk_claude_response(text):
    resp = Mock()
    resp.content = [Mock(text=text)]
    resp.stop_reason = "end_turn"
    return resp


def test_bootstrap_descriptions_fills_descriptions_for_target_categories():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "", "merchants": ["Target", "Costco"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    profiles["categories"]["cat-comp"] = {
        "name": "Computer", "description": "", "merchants": ["Best Buy", "Newegg"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    claude_json = json.dumps([
        {"category_id": "cat-home", "description": "Everyday household items, supplies, and accessories."},
        {"category_id": "cat-comp", "description": "Core computing hardware and components."},
    ])
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = _mk_claude_response(claude_json)
        bootstrap_descriptions(profiles, ["cat-home", "cat-comp"], "key")

    assert "household" in profiles["categories"]["cat-home"]["description"]
    assert "computing" in profiles["categories"]["cat-comp"]["description"]


def test_bootstrap_descriptions_resets_dirty_and_counter():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "stale", "merchants": ["Target"],
        "item_exemplars": {}, "items_since_generation": 9, "dirty": True,
    }
    claude_json = json.dumps([
        {"category_id": "cat-home", "description": "Fresh description."},
    ])
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = _mk_claude_response(claude_json)
        bootstrap_descriptions(profiles, ["cat-home"], "key")

    cat = profiles["categories"]["cat-home"]
    assert cat["description"] == "Fresh description."
    assert cat["items_since_generation"] == 0
    assert cat["dirty"] is False


def test_bootstrap_descriptions_empty_target_list_makes_no_call():
    profiles = _empty()
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        bootstrap_descriptions(profiles, [], "key")
        mock_cls.assert_not_called()


def test_bootstrap_descriptions_raises_on_unknown_category_id_in_response():
    # NO SILENT FALLBACK: a response referencing a category we didn't ask about errors.
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "", "merchants": ["Target"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    claude_json = json.dumps([
        {"category_id": "cat-BOGUS", "description": "x"},
    ])
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = _mk_claude_response(claude_json)
        with pytest.raises(ValueError, match="unknown category_id|cat-BOGUS"):
            bootstrap_descriptions(profiles, ["cat-home"], "key")


def test_bootstrap_descriptions_raises_on_empty_text():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "", "merchants": ["Target"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    resp = _mk_claude_response("   ")
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = resp
        with pytest.raises(ValueError, match="empty text"):
            bootstrap_descriptions(profiles, ["cat-home"], "key")


def test_bootstrap_descriptions_raises_when_category_omitted():
    # NO SILENT FALLBACK: Claude must return a description for every requested category.
    profiles = _empty()
    for cid in ("cat-a", "cat-b"):
        profiles["categories"][cid] = {
            "name": cid, "description": "", "merchants": ["M"],
            "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
        }
    claude_json = json.dumps([{"category_id": "cat-a", "description": "Only A."}])
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = _mk_claude_response(claude_json)
        with pytest.raises(ValueError, match="omitted descriptions"):
            bootstrap_descriptions(profiles, ["cat-a", "cat-b"], "key")


def test_bootstrap_descriptions_strips_code_fence():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "", "merchants": ["Target"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    inner = json.dumps([{"category_id": "cat-home", "description": "Fenced description."}])
    fenced = f"```json\n{inner}\n```"
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = _mk_claude_response(fenced)
        bootstrap_descriptions(profiles, ["cat-home"], "key")
    assert profiles["categories"]["cat-home"]["description"] == "Fenced description."


def test_bootstrap_descriptions_raises_on_truncated_response():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods", "description": "", "merchants": ["Target"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    resp = _mk_claude_response('[{"category_id": "cat-home", "desc')
    resp.stop_reason = "max_tokens"
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = resp
        with pytest.raises(ValueError, match="truncated|max_tokens"):
            bootstrap_descriptions(profiles, ["cat-home"], "key")


# ---------------------------------------------------------------------------
# regenerate_stale — orchestration over is_stale + bootstrap_descriptions
# ---------------------------------------------------------------------------

def test_regenerate_stale_only_regenerates_stale_categories():
    profiles = _empty()
    profiles["categories"]["fresh"] = {
        "name": "Fresh", "description": "good", "merchants": ["M"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    profiles["categories"]["stale"] = {
        "name": "Stale", "description": "", "merchants": ["M2"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    claude_json = json.dumps([
        {"category_id": "stale", "description": "Now described."},
    ])
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = _mk_claude_response(claude_json)
        regenerated = regenerate_stale(profiles, "key")

    assert regenerated == ["stale"]
    assert profiles["categories"]["stale"]["description"] == "Now described."
    assert profiles["categories"]["fresh"]["description"] == "good"


def test_regenerate_stale_no_stale_makes_no_claude_call():
    profiles = _empty()
    profiles["categories"]["fresh"] = {
        "name": "Fresh", "description": "good", "merchants": ["M"],
        "item_exemplars": {}, "items_since_generation": 0, "dirty": False,
    }
    with patch("category_profiles.anthropic.Anthropic") as mock_cls:
        regenerated = regenerate_stale(profiles, "key")
        mock_cls.assert_not_called()
    assert regenerated == []


# ---------------------------------------------------------------------------
# format_profiles_for_prompt — what Claude actually sees during item categorization
# ---------------------------------------------------------------------------

def test_format_profiles_includes_description_and_exemplars():
    profiles = _empty()
    profiles["categories"]["cat-home"] = {
        "name": "Home Goods",
        "description": "Everyday household items and accessories.",
        "merchants": ["Target"],
        "item_exemplars": {
            "usb cable": {"cat-home": {"name": "Home Goods", "count": 4}},
            "batteries": {"cat-home": {"name": "Home Goods", "count": 2}},
        },
        "items_since_generation": 0, "dirty": False,
    }
    categories = [{"id": "grp", "name": "G", "categories": [{"id": "cat-home", "name": "Home Goods"}]}]
    text = format_profiles_for_prompt(categories, profiles)
    assert "Home Goods" in text
    assert "cat-home" in text
    assert "Everyday household items" in text
    assert "usb cable" in text


def test_format_profiles_only_shows_exemplars_where_category_is_dominant():
    # "usb cable" is dominant in Home Goods, NOT in Computer — so it must not be
    # listed as a Computer exemplar even though Computer has a count of 1.
    profiles = _empty()
    for _ in range(4):
        record_item_categorization(profiles, "usb cable", "cat-home", "Home Goods")
    record_item_categorization(profiles, "usb cable", "cat-comp", "Computer")
    profiles["categories"]["cat-home"]["description"] = "Household."
    profiles["categories"]["cat-comp"]["description"] = "Computing hardware."

    categories = [{
        "id": "grp", "name": "G",
        "categories": [
            {"id": "cat-home", "name": "Home Goods"},
            {"id": "cat-comp", "name": "Computer"},
        ],
    }]
    text = format_profiles_for_prompt(categories, profiles)
    home_section = text.split("Computer")[0]
    assert "usb cable" in home_section
    computer_section = text.split("Computer", 1)[1]
    assert "usb cable" not in computer_section


def test_format_profiles_warns_when_category_has_no_description(caplog):
    # NO SILENT FALLBACK: a category with no learned description still gets listed
    # (so categorization can proceed) but emits a visible logged warning.
    import logging
    profiles = _empty()
    categories = [{"id": "grp", "name": "G", "categories": [{"id": "cat-x", "name": "Mystery"}]}]
    with caplog.at_level(logging.WARNING, logger="category_profiles"):
        text = format_profiles_for_prompt(categories, profiles)
    assert "Mystery" in text
    assert "cat-x" in text
    assert any(
        "Mystery" in r.getMessage() or "cat-x" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )
