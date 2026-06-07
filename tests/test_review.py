"""Tests for review.py — partitioning, decision marking, and sidecar paths.

These tests cover the pure logic; the interactive walk functions are not
unit-tested (they require TTY input).
"""
import json
from pathlib import Path

import pytest

import review


def _make_proposal(tier: str = "history", **kw) -> dict:
    base = {
        "transaction_id": "tx-1",
        "payee_name": "Foo",
        "amount_dollars": "-10",
        "date": "2026-01-01",
        "category_id": "cat-1",
        "category_name": "Groceries",
        "tier": tier,
        "confidence": 0.5,
        "rationale": "test",
        "prior_strength": None,
    }
    base.update(kw)
    return base


def test_partition_non_amazon():
    proposals = [
        _make_proposal("history"),
        _make_proposal("history"),
        _make_proposal("fuzzy"),
        _make_proposal("claude"),
        _make_proposal("amazon-wf"),
    ]
    claude, fuzzy, history, other = review._partition_non_amazon(proposals)
    assert len(claude) == 1
    assert len(fuzzy) == 1
    assert len(history) == 2
    assert len(other) == 1
    assert other[0]["tier"] == "amazon-wf"


def test_mark_accepted_sets_review_block():
    p = _make_proposal()
    review._mark_accepted(p)
    assert p["review"]["decision"] == "accept"
    assert "reviewed_at" in p["review"]
    assert "applied_at" not in p  # accept does NOT set applied_at


def test_mark_skipped_sets_applied_at_sentinel():
    p = _make_proposal()
    review._mark_skipped(p, reason="user")
    assert p["review"]["decision"] == "skip"
    assert p["applied_at"].startswith(review.SKIP_SENTINEL_PREFIX)
    assert "user" in p["applied_at"]


def test_mark_recategorized_mutates_category_and_stashes_original():
    p = _make_proposal()
    new_cat = {"id": "cat-99", "name": "Dining Out"}
    review._mark_recategorized(p, new_cat)
    assert p["category_id"] == "cat-99"
    assert p["category_name"] == "Dining Out"
    assert p["review"]["original_category_id"] == "cat-1"
    assert p["review"]["original_category_name"] == "Groceries"
    assert p["review"]["decision"] == "recategorize"


def test_edit_split_items_stashes_original_on_recategorize(monkeypatch):
    """Recategorizing an Amazon subtransaction must stash the original category
    so apply-time profile learning can see it as a rejection."""
    split = {
        "subtransactions": [
            {
                "item": {"product_name": "USB-C Cable", "asin": "B0X"},
                "category_id": "cat-comp", "category_name": "Computer",
            },
        ]
    }
    monkeypatch.setattr(review, "_prompt_choice", lambda *a, **k: "r")
    monkeypatch.setattr(review, "_pick_category", lambda *a, **k: {"id": "cat-home", "name": "Home Goods"})

    review._edit_split_items(split, categories=[])

    sub = split["subtransactions"][0]
    assert sub["category_id"] == "cat-home"
    assert sub["category_name"] == "Home Goods"
    assert sub["original_category_id"] == "cat-comp"
    assert sub["original_category_name"] == "Computer"


def test_edit_split_items_keep_leaves_no_original(monkeypatch):
    split = {
        "subtransactions": [
            {"item": {"product_name": "X"}, "category_id": "cat-a", "category_name": "A"},
        ]
    }
    monkeypatch.setattr(review, "_prompt_choice", lambda *a, **k: "k")
    review._edit_split_items(split, categories=[])
    sub = split["subtransactions"][0]
    assert "original_category_id" not in sub


def test_has_review_decision_detects_review_block():
    p = _make_proposal()
    assert not review._has_review_decision(p)
    review._mark_accepted(p)
    assert review._has_review_decision(p)


def test_has_review_decision_detects_skip_sentinel_without_review_block():
    p = _make_proposal()
    p["applied_at"] = f"{review.SKIP_SENTINEL_PREFIX}:legacy"
    assert review._has_review_decision(p)


def test_has_review_decision_ignores_real_applied_at():
    p = _make_proposal()
    p["applied_at"] = "2026-05-16T15:00:00"
    assert not review._has_review_decision(p)


def test_bulk_accept_history_skips_already_reviewed_and_non_history():
    proposals = [
        _make_proposal("history"),
        _make_proposal("history"),
        _make_proposal("fuzzy"),
        _make_proposal("claude"),
    ]
    review._mark_accepted(proposals[1])
    n = review._bulk_accept_history(proposals)
    assert n == 1  # only the un-reviewed history one
    assert proposals[0]["review"]["decision"] == "accept"
    assert "review" not in proposals[2]
    assert "review" not in proposals[3]


def test_flatten_categories_excludes_internal_and_hidden():
    groups = [
        {
            "name": "Internal Master Category",
            "hidden": False,
            "categories": [{"id": "x", "name": "Inflow: RTA", "hidden": False}],
        },
        {
            "name": "Monthly",
            "hidden": False,
            "categories": [
                {"id": "g1", "name": "Groceries", "hidden": False},
                {"id": "g2", "name": "Old Cat", "hidden": True},
            ],
        },
        {
            "name": "Hidden Group",
            "hidden": True,
            "categories": [{"id": "h1", "name": "Hidden", "hidden": False}],
        },
    ]
    flat = review._flatten_categories(groups)
    names = {c["name"] for c in flat}
    assert names == {"Groceries"}


def test_reviewed_sidecar_path():
    p = Path("data/cache/enrich-changeset-20260516-150338.json")
    assert review._reviewed_sidecar_path(p) == Path(
        "data/cache/enrich-changeset-20260516-150338-reviewed.json"
    )


def test_newest_changeset_excludes_reviewed_sidecars(tmp_path, monkeypatch):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    (cache / "enrich-changeset-001.json").write_text("{}")
    (cache / "enrich-changeset-002-reviewed.json").write_text("{}")
    (cache / "enrich-changeset-001-reviewed.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    newest = review._newest_changeset_json()
    assert newest.name == "enrich-changeset-001.json"


def test_save_and_reload_roundtrip(tmp_path):
    changeset = {"version": 1, "kind": "enrich-changeset", "x": 42}
    path = tmp_path / "out.json"
    review._save_reviewed(changeset, path)
    loaded = json.loads(path.read_text())
    assert loaded == changeset


# ============================================================================
# Cache-boost prompt (#43)
# ============================================================================


def _stub_input(monkeypatch, responses):
    it = iter(responses)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kw: next(it))


def test_maybe_prompt_boost_disabled_is_noop(monkeypatch):
    p = _make_proposal()
    review._mark_accepted(p)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("input was called"))
    review._maybe_prompt_boost(p, enabled=False)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_skip_is_noop(monkeypatch):
    p = _make_proposal()
    review._mark_skipped(p)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("input was called"))
    review._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_no_response_does_not_record(monkeypatch):
    p = _make_proposal()
    review._mark_accepted(p)
    _stub_input(monkeypatch, [""])
    review._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_yes_records_normal_strength(monkeypatch):
    p = _make_proposal()
    review._mark_accepted(p)
    _stub_input(monkeypatch, ["y"])
    review._maybe_prompt_boost(p, enabled=True)
    assert p["review"]["boost"] == {"strength": review.BOOST_STRENGTH_NORMAL}


def test_maybe_prompt_boost_strong_records_strong_strength(monkeypatch):
    p = _make_proposal()
    review._mark_recategorized(p, {"id": "cat-2", "name": "Dining Out"})
    _stub_input(monkeypatch, ["s"])
    review._maybe_prompt_boost(p, enabled=True)
    assert p["review"]["boost"] == {"strength": review.BOOST_STRENGTH_STRONG}


def test_maybe_prompt_boost_reprompts_on_invalid(monkeypatch):
    p = _make_proposal()
    review._mark_accepted(p)
    _stub_input(monkeypatch, ["bogus", "n"])
    review._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_skips_when_payee_missing(monkeypatch):
    p = _make_proposal(payee_name="")
    review._mark_accepted(p)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("input was called"))
    review._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_apply_boosts_to_cache_writes_records(tmp_path):
    cache_path = tmp_path / "payee_lookup.json"
    cache_path.write_text("{}")
    p = _make_proposal(payee_name="Starbucks", category_id="cat-dining",
                       category_name="Dining Out")
    review._mark_accepted(p)
    p["review"]["boost"] = {"strength": review.BOOST_STRENGTH_STRONG}
    changeset = {
        "amazon": {"proposed_splits": []},
        "non_amazon": {"proposals": [p]},
    }

    n = review._apply_boosts_to_cache(changeset, cache_path=str(cache_path))

    assert n == 1
    saved = json.loads(cache_path.read_text())
    entry = saved["starbucks"]
    assert entry["total"] == review.BOOST_STRENGTH_STRONG
    assert entry["categories"]["cat-dining"]["count"] == review.BOOST_STRENGTH_STRONG
    assert entry["categories"]["cat-dining"]["name"] == "Dining Out"


def test_apply_boosts_to_cache_no_pending_does_not_touch_disk(tmp_path):
    cache_path = tmp_path / "payee_lookup.json"
    changeset = {
        "amazon": {"proposed_splits": []},
        "non_amazon": {"proposals": [_make_proposal()]},
    }
    n = review._apply_boosts_to_cache(changeset, cache_path=str(cache_path))
    assert n == 0
    assert not cache_path.exists()


def test_apply_boosts_accumulates_on_existing_entry(tmp_path):
    cache_path = tmp_path / "payee_lookup.json"
    cache_path.write_text(json.dumps({
        "_version": 2,
        "starbucks": {
            "total": 3,
            "categories": {"cat-dining": {"name": "Dining Out", "count": 3}},
        }
    }))
    p = _make_proposal(payee_name="Starbucks", category_id="cat-dining",
                       category_name="Dining Out")
    review._mark_accepted(p)
    p["review"]["boost"] = {"strength": review.BOOST_STRENGTH_NORMAL}
    changeset = {
        "amazon": {"proposed_splits": []},
        "non_amazon": {"proposals": [p]},
    }

    review._apply_boosts_to_cache(changeset, cache_path=str(cache_path))

    saved = json.loads(cache_path.read_text())
    assert saved["starbucks"]["total"] == 3 + review.BOOST_STRENGTH_NORMAL
    assert saved["starbucks"]["categories"]["cat-dining"]["count"] == 3 + review.BOOST_STRENGTH_NORMAL
