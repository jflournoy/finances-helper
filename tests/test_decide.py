"""Tests for decide.py — partitioning, decision marking, and sidecar paths.

These tests cover the pure logic; the interactive walk functions are not
unit-tested (they require TTY input).
"""
import json
from pathlib import Path

import pytest

import decide


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
    claude, fuzzy, history, other = decide._partition_non_amazon(proposals)
    assert len(claude) == 1
    assert len(fuzzy) == 1
    assert len(history) == 2
    assert len(other) == 1
    assert other[0]["tier"] == "amazon-wf"


def test_mark_accepted_sets_review_block():
    p = _make_proposal()
    decide._mark_accepted(p)
    assert p["review"]["decision"] == "accept"
    assert "reviewed_at" in p["review"]
    assert "applied_at" not in p  # accept does NOT set applied_at


def test_mark_skipped_sets_applied_at_sentinel():
    p = _make_proposal()
    decide._mark_skipped(p, reason="user")
    assert p["review"]["decision"] == "skip"
    assert p["applied_at"].startswith(decide.SKIP_SENTINEL_PREFIX)
    assert "user" in p["applied_at"]


def test_mark_recategorized_mutates_category_and_stashes_original():
    p = _make_proposal()
    new_cat = {"id": "cat-99", "name": "Dining Out"}
    decide._mark_recategorized(p, new_cat)
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
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "r")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "cat-home", "name": "Home Goods"})

    decide._edit_split_items(split, categories=[])

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
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "k")
    decide._edit_split_items(split, categories=[])
    sub = split["subtransactions"][0]
    assert "original_category_id" not in sub


def test_has_review_decision_detects_review_block():
    p = _make_proposal()
    assert not decide._has_review_decision(p)
    decide._mark_accepted(p)
    assert decide._has_review_decision(p)


def test_has_review_decision_detects_skip_sentinel_without_review_block():
    p = _make_proposal()
    p["applied_at"] = f"{decide.SKIP_SENTINEL_PREFIX}:legacy"
    assert decide._has_review_decision(p)


def test_has_review_decision_ignores_real_applied_at():
    p = _make_proposal()
    p["applied_at"] = "2026-05-16T15:00:00"
    assert not decide._has_review_decision(p)


def test_bulk_accept_history_skips_already_reviewed_and_non_history():
    proposals = [
        _make_proposal("history"),
        _make_proposal("history"),
        _make_proposal("fuzzy"),
        _make_proposal("claude"),
    ]
    decide._mark_accepted(proposals[1])
    n = decide._bulk_accept_history(proposals)
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
    flat = decide._flatten_categories(groups)
    names = {c["name"] for c in flat}
    assert names == {"Groceries"}


def test_reviewed_sidecar_path():
    p = Path("data/cache/enrich-changeset-20260516-150338.json")
    assert decide._reviewed_sidecar_path(p) == Path(
        "data/cache/enrich-changeset-20260516-150338-reviewed.json"
    )


def test_newest_changeset_excludes_reviewed_sidecars(tmp_path, monkeypatch):
    cache = tmp_path / "data" / "cache"
    cache.mkdir(parents=True)
    (cache / "enrich-changeset-001.json").write_text("{}")
    (cache / "enrich-changeset-002-reviewed.json").write_text("{}")
    (cache / "enrich-changeset-001-reviewed.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    newest = decide._newest_changeset_json()
    assert newest.name == "enrich-changeset-001.json"


def test_save_and_reload_roundtrip(tmp_path):
    changeset = {"version": 1, "kind": "enrich-changeset", "x": 42}
    path = tmp_path / "out.json"
    decide._save_reviewed(changeset, path)
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
    decide._mark_accepted(p)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("input was called"))
    decide._maybe_prompt_boost(p, enabled=False)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_skip_is_noop(monkeypatch):
    p = _make_proposal()
    decide._mark_skipped(p)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("input was called"))
    decide._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_no_response_does_not_record(monkeypatch):
    p = _make_proposal()
    decide._mark_accepted(p)
    _stub_input(monkeypatch, [""])
    decide._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_yes_records_normal_strength(monkeypatch):
    p = _make_proposal()
    decide._mark_accepted(p)
    _stub_input(monkeypatch, ["y"])
    decide._maybe_prompt_boost(p, enabled=True)
    assert p["review"]["boost"] == {"strength": decide.BOOST_STRENGTH_NORMAL}


def test_maybe_prompt_boost_strong_records_strong_strength(monkeypatch):
    p = _make_proposal()
    decide._mark_recategorized(p, {"id": "cat-2", "name": "Dining Out"})
    _stub_input(monkeypatch, ["s"])
    decide._maybe_prompt_boost(p, enabled=True)
    assert p["review"]["boost"] == {"strength": decide.BOOST_STRENGTH_STRONG}


def test_maybe_prompt_boost_reprompts_on_invalid(monkeypatch):
    p = _make_proposal()
    decide._mark_accepted(p)
    _stub_input(monkeypatch, ["bogus", "n"])
    decide._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_maybe_prompt_boost_skips_when_payee_missing(monkeypatch):
    p = _make_proposal(payee_name="")
    decide._mark_accepted(p)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: pytest.fail("input was called"))
    decide._maybe_prompt_boost(p, enabled=True)
    assert "boost" not in p["review"]


def test_apply_boosts_to_cache_writes_records(tmp_path):
    cache_path = tmp_path / "payee_lookup.json"
    cache_path.write_text("{}")
    p = _make_proposal(payee_name="Starbucks", category_id="cat-dining",
                       category_name="Dining Out")
    decide._mark_accepted(p)
    p["review"]["boost"] = {"strength": decide.BOOST_STRENGTH_STRONG}
    changeset = {
        "amazon": {"proposed_splits": []},
        "non_amazon": {"proposals": [p]},
    }

    n = decide._apply_boosts_to_cache(changeset, cache_path=str(cache_path))

    assert n == 1
    saved = json.loads(cache_path.read_text())
    entry = saved["starbucks"]
    assert entry["total"] == decide.BOOST_STRENGTH_STRONG
    assert entry["categories"]["cat-dining"]["count"] == decide.BOOST_STRENGTH_STRONG
    assert entry["categories"]["cat-dining"]["name"] == "Dining Out"


def test_apply_boosts_to_cache_no_pending_does_not_touch_disk(tmp_path):
    cache_path = tmp_path / "payee_lookup.json"
    changeset = {
        "amazon": {"proposed_splits": []},
        "non_amazon": {"proposals": [_make_proposal()]},
    }
    n = decide._apply_boosts_to_cache(changeset, cache_path=str(cache_path))
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
    decide._mark_accepted(p)
    p["review"]["boost"] = {"strength": decide.BOOST_STRENGTH_NORMAL}
    changeset = {
        "amazon": {"proposed_splits": []},
        "non_amazon": {"proposals": [p]},
    }

    decide._apply_boosts_to_cache(changeset, cache_path=str(cache_path))

    saved = json.loads(cache_path.read_text())
    assert saved["starbucks"]["total"] == 3 + decide.BOOST_STRENGTH_NORMAL
    assert saved["starbucks"]["categories"]["cat-dining"]["count"] == 3 + decide.BOOST_STRENGTH_NORMAL


# ============================================================================
# Near-miss walk: order URL, dump item index, and the interactive walk
# ============================================================================


def _make_candidate(**kw) -> dict:
    base = {
        "order_id": "111-0781058-6001837",
        "ship_date": "2026-06-29",
        "amount_dollars": "196.70",
        "amount_delta_dollars": "2.78",
        "date_delta_days": 0,
    }
    base.update(kw)
    return base


def _make_unmatched(**kw) -> dict:
    base = {
        "transaction_id": "tx-unmatched-1",
        "payee_name": "Amazon",
        "amount_dollars": "-193.92",
        "date": "2026-06-29",
        "memo": None,
        "reason": "no matching shipment in dump",
        "candidate_shipments": [_make_candidate()],
    }
    base.update(kw)
    return base


def test_amazon_order_url_contains_order_id():
    url = decide._amazon_order_url("111-0781058-6001837")
    assert "111-0781058-6001837" in url
    assert url.startswith("https://")


def _write_sample_dump_zip(tmp_path: Path) -> Path:
    from zipfile import ZipFile

    csv_text = Path("data/fixtures/amazon_order_history_sample.csv").read_text()
    zip_path = tmp_path / "amazon-order-history-2024-01-01.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr("Your Amazon Orders/Order History.csv", csv_text)
    return zip_path


def test_build_shipment_item_index_missing_dump_path_returns_warning():
    index, warning = decide._build_shipment_item_index(None)
    assert index == {}
    assert warning is not None
    assert "no recorded dump_path" in warning


def test_build_shipment_item_index_missing_file_returns_warning(tmp_path):
    missing = tmp_path / "does-not-exist.zip"
    index, warning = decide._build_shipment_item_index(str(missing))
    assert index == {}
    assert warning is not None
    assert "no longer exists" in warning


def test_build_shipment_item_index_parses_real_dump(tmp_path):
    zip_path = _write_sample_dump_zip(tmp_path)
    index, warning = decide._build_shipment_item_index(str(zip_path))
    assert warning is None
    assert len(index) > 0
    # Sample fixture's first order: 111-0000001-0000001, shipped 2024-01-16, $37.09
    key = decide._shipment_index_key("111-0000001-0000001", "2024-01-16", "37.09")
    assert key in index
    assert [s.items[0].product_name for s in index[key]] == ["Test Widget"]


def _two_parcel_dump_zip(tmp_path: Path) -> Path:
    """A dump where one order ships as two same-day parcels of different amounts.

    Mirrors real Amazon order 113-4262513-5433000: two tracking numbers, one
    ship date, two totals that sum to the single card charge.
    """
    from zipfile import ZipFile

    header = Path("data/fixtures/amazon_order_history_sample.csv").read_text().splitlines()[0]
    rows = [
        "B0FMW3M89N,REDACTED,AMZN_US(TBA333474726036),USD,,,,,2026-08-06,113-4262513-5433000,"
        "Closed,1,Visa - 5219,New,See Kai Run Sneaker,,2026-08-06,59.99,4.65,Shipped,REDACTED,"
        "0,Not Applicable,64.64,0,59.99,4.65,Amazon.com",
        "B0DNFLML4B,REDACTED,AMZN_US(TBA333475400228),USD,,,,,2026-08-06,113-4262513-5433000,"
        "Closed,1,Visa - 5219,New,Amazon Essentials Boys Shorts,,2026-08-06,18,1.4,Shipped,REDACTED,"
        "0,Not Applicable,19.4,0,18,1.4,Amazon.com",
    ]
    zip_path = tmp_path / "amazon-order-history-2026-08-10.zip"
    with ZipFile(zip_path, "w") as zf:
        zf.writestr("Your Amazon Orders/Order History.csv", "\n".join([header] + rows) + "\n")
    return zip_path


def test_build_shipment_item_index_keeps_both_same_day_parcels_of_one_order(tmp_path):
    """Two parcels of one order, same ship date: neither may overwrite the other.

    Keying on (order_id, ship_date) alone silently dropped one and showed the
    survivor's items under both amounts.
    """
    index, warning = decide._build_shipment_item_index(str(_two_parcel_dump_zip(tmp_path)))
    assert warning is None

    sneaker_key = decide._shipment_index_key("113-4262513-5433000", "2026-08-06", "64.64")
    shorts_key = decide._shipment_index_key("113-4262513-5433000", "2026-08-06", "19.4")
    assert [s.items[0].product_name for s in index[sneaker_key]] == ["See Kai Run Sneaker"]
    assert [s.items[0].product_name for s in index[shorts_key]] == ["Amazon Essentials Boys Shorts"]


def test_walk_near_misses_shows_each_parcels_own_items(tmp_path, monkeypatch, capsys):
    """Each candidate's item list is the one belonging to that candidate's amount."""
    index, _ = decide._build_shipment_item_index(str(_two_parcel_dump_zip(tmp_path)))
    changeset = {
        "amazon": {
            "unmatched_ynab": [
                _make_unmatched(
                    amount_dollars="-84.04",
                    date="2026-08-07",
                    candidate_shipments=[
                        _make_candidate(
                            order_id="113-4262513-5433000",
                            ship_date="2026-08-06",
                            amount_dollars="64.64",
                            amount_delta_dollars="19.40",
                            date_delta_days=-1,
                        ),
                        _make_candidate(
                            order_id="113-4262513-5433000",
                            ship_date="2026-08-06",
                            amount_dollars="19.4",
                            amount_delta_dollars="64.64",
                            date_delta_days=-1,
                        ),
                    ],
                )
            ]
        },
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index=index, item_index_warning=None
    )

    lines = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    numbered = [i for i, ln in enumerate(lines) if ln.startswith(("1. order", "2. order"))]
    first, second = numbered
    assert "$64.64 (" in lines[first]
    assert lines[first + 1] == "- See Kai Run Sneaker"
    assert "$19.4 (" in lines[second]
    assert lines[second + 1] == "- Amazon Essentials Boys Shorts"


def test_walk_near_misses_flags_ambiguity_instead_of_showing_wrong_items(tmp_path, monkeypatch, capsys):
    """If two shipments still collide on the key, show neither item list — say so."""
    shipment = decide.parse_order_history(
        Path("data/fixtures/amazon_order_history_sample.csv").read_text()
    )[0][0]
    key = decide._shipment_index_key("113-DUP", "2026-08-06", "64.64")
    index = {key: [shipment, shipment]}
    changeset = {
        "amazon": {
            "unmatched_ynab": [
                _make_unmatched(
                    candidate_shipments=[
                        _make_candidate(
                            order_id="113-DUP", ship_date="2026-08-06", amount_dollars="64.64"
                        )
                    ]
                )
            ]
        },
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index=index, item_index_warning=None
    )

    out = capsys.readouterr().out
    assert "item detail is ambiguous" in out
    assert "Test Widget" not in out


def test_walk_unmatched_near_misses_skips_entries_without_candidates():
    """Only entries with near-miss candidates are shown/walkable."""
    changeset = {
        "amazon": {
            "unmatched_ynab": [_make_unmatched(candidate_shipments=[])],
            "proposed_splits": [],
        },
        "non_amazon": {"proposals": []},
    }
    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)
    assert result is None
    assert len(changeset["amazon"]["unmatched_ynab"]) == 1
    assert len(changeset["non_amazon"]["proposals"]) == 0


def test_walk_unmatched_near_misses_categorize_single_candidate_moves_to_non_amazon_proposals(monkeypatch):
    """With exactly one candidate, categorize skips the pick-a-number prompt."""
    u = _make_unmatched()
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "cat-electronics", "name": "Electronics"})

    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    assert result is None
    assert changeset["amazon"]["unmatched_ynab"] == []
    proposals = changeset["non_amazon"]["proposals"]
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal["transaction_id"] == "tx-unmatched-1"
    assert proposal["category_id"] == "cat-electronics"
    assert proposal["category_name"] == "Electronics"
    assert proposal["tier"] == "near-miss"
    assert proposal["confidence"] == 1.0
    assert proposal["amount_dollars"] == "-193.92"
    assert "111-0781058-6001837" in proposal["rationale"]
    assert proposal["near_miss_order_id"] == "111-0781058-6001837"
    assert decide._has_review_decision(proposal)


def test_walk_unmatched_near_misses_categorize_multiple_candidates_prompts_for_pick(monkeypatch):
    """With multiple candidates, categorize asks which one before the category picker."""
    u = _make_unmatched(candidate_shipments=[
        _make_candidate(order_id="111-FIRST"),
        _make_candidate(order_id="111-SECOND"),
    ])
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    responses = iter(["c", "2"])
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: next(responses))
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "cat-electronics", "name": "Electronics"})

    decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    proposal = changeset["non_amazon"]["proposals"][0]
    assert proposal["near_miss_order_id"] == "111-SECOND"


def test_walk_unmatched_near_misses_open_loops_back_to_full_menu(monkeypatch):
    """Opening a candidate must not consume the turn -- the user can open
    multiple candidates (or the same one again) before choosing or skipping.
    """
    u = _make_unmatched(candidate_shipments=[
        _make_candidate(order_id="111-FIRST"),
        _make_candidate(order_id="111-SECOND"),
    ])
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    opened = []
    monkeypatch.setattr(decide.webbrowser, "open", lambda url: opened.append(url))
    responses = iter(["o", "1", "o", "2", "c", "2"])
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: next(responses))
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "cat-electronics", "name": "Electronics"})

    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    assert result is None
    assert len(opened) == 2
    assert "111-FIRST" in opened[0]
    assert "111-SECOND" in opened[1]
    proposal = changeset["non_amazon"]["proposals"][0]
    assert proposal["near_miss_order_id"] == "111-SECOND"


def test_walk_unmatched_near_misses_quit_after_open_returns_quit(monkeypatch):
    u = _make_unmatched()
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide.webbrowser, "open", lambda url: None)
    responses = iter(["o", "1", "q"])
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: next(responses))

    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    assert result == "quit"
    assert not decide._has_review_decision(u)


def test_walk_unmatched_near_misses_claimed_candidate_excluded_from_later_entries(monkeypatch):
    """Once a candidate shipment is claimed for one txn, it's not offered to
    another txn in the same walk -- it can't be two people's near-miss.
    """
    shared_candidate = _make_candidate(order_id="111-SHARED")
    u1 = _make_unmatched(transaction_id="tx-1", candidate_shipments=[shared_candidate])
    u2 = _make_unmatched(transaction_id="tx-2", candidate_shipments=[shared_candidate])
    changeset = {
        "amazon": {"unmatched_ynab": [u1, u2], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "cat-electronics", "name": "Electronics"})

    decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    # tx-1 claimed the only candidate; tx-2 had nothing left to offer, so it
    # stays in unmatched_ynab, unreviewed.
    assert len(changeset["non_amazon"]["proposals"]) == 1
    assert changeset["non_amazon"]["proposals"][0]["transaction_id"] == "tx-1"
    assert changeset["amazon"]["unmatched_ynab"] == [u2]
    assert not decide._has_review_decision(u2)


def test_claimed_order_ids_reads_existing_near_miss_proposals():
    """Resume must honor claims from a prior partial run, not just this one."""
    changeset = {
        "amazon": {"unmatched_ynab": [], "proposed_splits": []},
        "non_amazon": {"proposals": [
            {"tier": "near-miss", "near_miss_order_id": "111-ALREADY-CLAIMED"},
            {"tier": "history", "category_id": "cat-1"},
        ]},
    }
    claimed_keys, claimed_orders = decide._claimed_near_misses(changeset)
    assert claimed_keys == set()
    assert claimed_orders == {"111-ALREADY-CLAIMED"}


def test_walk_unmatched_near_misses_categorize_cancel_leaves_unmatched(monkeypatch):
    """Bailing out of the category picker leaves the entry unreviewed for next time."""
    u = _make_unmatched()
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: None)

    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    assert result is None
    assert changeset["amazon"]["unmatched_ynab"] == [u]
    assert changeset["non_amazon"]["proposals"] == []
    assert not decide._has_review_decision(u)


def test_walk_unmatched_near_misses_skip_marks_reviewed_without_moving(monkeypatch):
    u = _make_unmatched()
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    assert result is None
    assert changeset["amazon"]["unmatched_ynab"] == [u]
    assert decide._has_review_decision(u)
    assert changeset["non_amazon"]["proposals"] == []


def test_walk_unmatched_near_misses_quit_returns_quit(monkeypatch):
    u = _make_unmatched()
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "q")

    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    assert result == "quit"
    assert not decide._has_review_decision(u)


def test_walk_unmatched_near_misses_already_reviewed_entries_are_skipped():
    """A previously skipped/categorized entry doesn't re-prompt on resume."""
    u = _make_unmatched()
    decide._mark_skipped(u)
    changeset = {
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    # No prompt monkeypatch — if the walk tried to prompt, this would fail
    # since input() isn't available in the test environment.
    result = decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)
    assert result is None


def test_near_miss_proposal_schema_satisfies_apply_load_changeset(tmp_path, monkeypatch):
    """The synthetic near-miss proposal must satisfy apply.py's real validation
    and produce a valid category-only PATCH body — the integration seam this
    feature depends on.
    """
    import apply

    u = _make_unmatched()
    changeset = {
        "version": 1,
        "kind": "enrich-changeset",
        "metadata": {"timestamp": "2026-07-23T00:00:00", "budget_id": "b123"},
        "amazon": {"unmatched_ynab": [u], "proposed_splits": []},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "cat-electronics", "name": "Electronics"})
    decide._walk_unmatched_near_misses(changeset, categories=[], item_index={}, item_index_warning=None)

    sidecar_path = tmp_path / "changeset-reviewed.json"
    sidecar_path.write_text(json.dumps(changeset))

    loaded = apply.load_changeset(sidecar_path)
    proposal = loaded["non_amazon"]["proposals"][0]
    body = apply.flat_proposal_to_patch_body(proposal)
    assert body == {"category_id": "cat-electronics"}


# ============================================================================
# Near-miss claiming: per-parcel vs whole-order grain
# ============================================================================


def _parcel_candidate(**kw) -> dict:
    base = {
        "order_id": "113-4262513-5433000",
        "ship_date": "2026-08-06",
        "amount_dollars": "64.64",
        "amount_delta_dollars": "16.62",
        "date_delta_days": -1,
        "parcels": 1,
    }
    base.update(kw)
    return base


def _whole_order_candidate(**kw) -> dict:
    base = {
        "order_id": "113-4262513-5433000",
        "ship_date": "2026-08-06",
        "amount_dollars": "84.04",
        "amount_delta_dollars": "2.78",
        "date_delta_days": -1,
        "parcels": 2,
        "parcel_keys": [
            ["113-4262513-5433000", "2026-08-06", "64.64"],
            ["113-4262513-5433000", "2026-08-06", "19.4"],
        ],
    }
    base.update(kw)
    return base


def test_claimed_near_misses_parcel_pick_claims_only_that_parcel():
    changeset = {
        "non_amazon": {"proposals": [{
            "tier": "near-miss",
            "near_miss_order_id": "113-ORDER",
            "near_miss_shipment_key": ["113-ORDER", "2026-08-06", "64.64"],
            "near_miss_parcel_count": 1,
        }]},
    }
    claimed_keys, claimed_orders = decide._claimed_near_misses(changeset)
    assert claimed_keys == {("113-ORDER", "2026-08-06", "64.64")}
    assert claimed_orders == set()


def test_claimed_near_misses_whole_order_pick_claims_the_order():
    changeset = {
        "non_amazon": {"proposals": [{
            "tier": "near-miss",
            "near_miss_order_id": "113-ORDER",
            "near_miss_shipment_key": ["113-ORDER", "2026-08-06", "84.04"],
            "near_miss_parcel_count": 2,
        }]},
    }
    claimed_keys, claimed_orders = decide._claimed_near_misses(changeset)
    assert claimed_keys == set()
    assert claimed_orders == {"113-ORDER"}


def test_claiming_one_parcel_leaves_its_sibling_available():
    """Parcels that shipped on different days are billed separately.

    Claiming one for a charge must not hide the other from the charge that
    actually paid for it — the bug order-ID-grained claiming had.
    """
    claimed_keys = {("113-4262513-5433000", "2026-08-06", "64.64")}
    sibling = _parcel_candidate(ship_date="2026-08-09", amount_dollars="19.4")

    assert decide._candidate_is_claimed(_parcel_candidate(), claimed_keys, set())
    assert not decide._candidate_is_claimed(sibling, claimed_keys, set())


def test_claiming_one_parcel_kills_the_whole_order_candidate():
    """The order total includes the claimed parcel; offering it would double-spend."""
    claimed_keys = {("113-4262513-5433000", "2026-08-06", "64.64")}

    assert decide._candidate_is_claimed(_whole_order_candidate(), claimed_keys, set())


def test_claiming_the_whole_order_kills_every_parcel_of_it():
    claimed_orders = {"113-4262513-5433000"}

    assert decide._candidate_is_claimed(_parcel_candidate(), set(), claimed_orders)
    assert decide._candidate_is_claimed(_whole_order_candidate(), set(), claimed_orders)
    assert not decide._candidate_is_claimed(
        _parcel_candidate(order_id="112-OTHER"), set(), claimed_orders
    )


def test_walk_near_misses_parcel_pick_records_the_shipment_key(monkeypatch):
    changeset = {
        "amazon": {"unmatched_ynab": [
            _make_unmatched(candidate_shipments=[_parcel_candidate()])
        ]},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "c1", "name": "Clothing"})

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index={}, item_index_warning=None
    )

    proposal = changeset["non_amazon"]["proposals"][0]
    assert proposal["near_miss_shipment_key"] == ["113-4262513-5433000", "2026-08-06", "64.64"]
    assert proposal["near_miss_parcel_count"] == 1
    assert "parcel shipped 2026-08-06" in proposal["rationale"]


def test_walk_near_misses_whole_order_pick_records_the_parcel_count(monkeypatch):
    changeset = {
        "amazon": {"unmatched_ynab": [
            _make_unmatched(candidate_shipments=[_whole_order_candidate()])
        ]},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "c1", "name": "Clothing"})

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index={}, item_index_warning=None
    )

    proposal = changeset["non_amazon"]["proposals"][0]
    assert proposal["near_miss_parcel_count"] == 2
    assert "whole order, 2 parcels" in proposal["rationale"]


def test_walk_near_misses_sibling_parcel_survives_an_earlier_parcel_pick(monkeypatch):
    """End to end: pick parcel A for one charge, parcel B is still offered for the next."""
    changeset = {
        "amazon": {"unmatched_ynab": [
            _make_unmatched(
                transaction_id="tx-a",
                candidate_shipments=[_parcel_candidate()],
            ),
            _make_unmatched(
                transaction_id="tx-b",
                candidate_shipments=[
                    _parcel_candidate(ship_date="2026-08-09", amount_dollars="19.4")
                ],
            ),
        ]},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "c")
    monkeypatch.setattr(decide, "_pick_category", lambda *a, **k: {"id": "c1", "name": "Clothing"})

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index={}, item_index_warning=None
    )

    picked = [p["near_miss_shipment_key"] for p in changeset["non_amazon"]["proposals"]]
    assert picked == [
        ["113-4262513-5433000", "2026-08-06", "64.64"],
        ["113-4262513-5433000", "2026-08-09", "19.4"],
    ]


def test_walk_near_misses_whole_order_candidate_shows_every_parcels_items(tmp_path, monkeypatch, capsys):
    index, _ = decide._build_shipment_item_index(str(_two_parcel_dump_zip(tmp_path)))
    changeset = {
        "amazon": {"unmatched_ynab": [
            _make_unmatched(candidate_shipments=[_whole_order_candidate()])
        ]},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index=index, item_index_warning=None
    )

    out = capsys.readouterr().out
    assert "[whole order, 2 parcels]" in out
    assert "See Kai Run Sneaker" in out
    assert "Amazon Essentials Boys Shorts" in out


def test_walk_near_misses_whole_order_with_an_unresolvable_parcel_shows_no_items(
    tmp_path, monkeypatch, capsys
):
    """A partial list under a whole-order total reads as complete — show none instead."""
    index, _ = decide._build_shipment_item_index(str(_two_parcel_dump_zip(tmp_path)))
    candidate = _whole_order_candidate(
        parcel_keys=[
            ["113-4262513-5433000", "2026-08-06", "64.64"],
            ["113-4262513-5433000", "2026-08-06", "99.99"],
        ]
    )
    changeset = {
        "amazon": {"unmatched_ynab": [_make_unmatched(candidate_shipments=[candidate])]},
        "non_amazon": {"proposals": []},
    }
    monkeypatch.setattr(decide, "_prompt_choice", lambda *a, **k: "s")

    decide._walk_unmatched_near_misses(
        changeset, categories=[], item_index=index, item_index_warning=None
    )

    out = capsys.readouterr().out
    assert "is missing from the dump" in out
    assert "See Kai Run Sneaker" not in out
