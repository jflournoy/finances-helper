"""Integration tests for amazon_confirm.apply_changeset against a real YNAB Sandbox budget.

Pattern: seed real txns in Sandbox via sandbox_client.create_transactions, then
build a synthetic enrich-changeset referencing those txn IDs and apply it via a
non-sandbox patch_client (PATCH cannot be safely redirected — see
update_transactions's NotImplementedError when sandbox_mode=True).

Seeded txns are tagged with memo prefix 'PYTEST-CONFIRM-' so they can be
identified for manual cleanup in the Sandbox UI (YNAB API does not support
deletion on the standard plan as of this writing).

These tests are the end-to-end gate for issue #179 (batch PATCH). They exercise
the full apply_changeset -> update_transactions -> YNAB Sandbox call chain with
no mocks, guarding against:
  - response-shape drift between our assumption and what YNAB actually returns
  - applied_at datetime serialization bugs in the on-disk changeset
  - wiring bugs between apply_changeset and update_transactions

Gated on `pytest -m integration` (requires YNAB_API_TOKEN, YNAB_DEFAULT_BUDGET,
YNAB_SANDBOX_MODE=1 in .env).
"""
import json
import pytest
from pathlib import Path

from amazon_confirm import apply_changeset
from ynab_client import dollars_to_milliunits

pytestmark = pytest.mark.integration


SEED_MEMO_PREFIX = "PYTEST-CONFIRM-"


def _pick_two_categories(client, budget_id):
    groups = client.get_categories(budget_id)
    found = []
    for g in groups:
        for c in g.get("categories", []):
            if not c.get("hidden"):
                found.append(c["id"])
                if len(found) >= 2:
                    return found
    raise AssertionError("Need at least 2 visible categories in Sandbox budget")


def _seed_txns(sandbox_client, live_budget_id, n, memo_suffix, amount_milliunits=-15500):
    txns = [
        {
            "date": "2026-05-10",
            "amount": amount_milliunits,
            "payee_name": "Amazon.com",
            "memo": f"{SEED_MEMO_PREFIX}{memo_suffix}-{i}",
            "cleared": "uncleared",
            "approved": False,
            "account_id": "will-be-overridden-by-sandbox",
        }
        for i in range(n)
    ]
    result = sandbox_client.create_transactions(live_budget_id, txns)
    return result["transactions"]


def _build_enrich_changeset(tmp_path, budget_id, amazon_splits=None, non_amazon=None):
    cs = {
        "kind": "enrich-changeset",
        "version": 1,
        "metadata": {
            "timestamp": "2026-05-17T00:00:00",
            "budget_id": budget_id,
        },
        "amazon": {"proposed_splits": amazon_splits or []},
        "non_amazon": {"proposals": non_amazon or []},
    }
    path = tmp_path / "enrich-changeset-integration.json"
    path.write_text(json.dumps(cs, indent=2))
    return path


def _flat_non_amazon_proposal(txn_id, cat_id):
    return {
        "transaction_id": txn_id,
        "payee_name": "Amazon.com",
        "category_id": cat_id,
        "confidence": 1.0,
        "tier": "history",
    }


def _amazon_split_proposal(parent_txn, cat_id_a, cat_id_b):
    """Build an amazon split proposal whose subtransaction amounts sum to the
    parent's milliunit amount. parent_txn['amount'] is in milliunits (negative
    for outflows). We split 10.00 / 5.50 of a 15.50 outflow."""
    return {
        "transaction_id": parent_txn["id"],
        "parent_ynab_transaction": {
            "id": parent_txn["id"],
            "amount": parent_txn["amount"],
            "memo": parent_txn.get("memo", ""),
            "date": parent_txn["date"],
            "payee_name": parent_txn.get("payee_name", ""),
        },
        "subtransactions": [
            {
                "item": {"asin": "B0INT00002", "product_name": "Integration Item A", "quantity": 1},
                "memo": "Integration Item A",
                "allocated_amount": "10.00",
                "category_id": cat_id_a,
                "category_name": "TestA",
                "confidence": 1.0,
                "rationale": "integration test A",
            },
            {
                "item": {"asin": "B0INT00003", "product_name": "Integration Item B", "quantity": 1},
                "memo": "Integration Item B",
                "allocated_amount": "5.50",
                "category_id": cat_id_b,
                "category_name": "TestB",
                "confidence": 1.0,
                "rationale": "integration test B",
            },
        ],
    }


def test_apply_changeset_batch_against_sandbox(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    """End-to-end: 5 non-amazon flat proposals batched into a single
    update_transactions call against the real Sandbox. Verifies:
      - all 5 applied_at written to the on-disk changeset
      - all 5 memos updated in YNAB
      - report.failed empty, report.aborted False
    Guards against response-shape drift and applied_at serialization bugs."""
    cat_id = _pick_two_categories(patch_client, sandbox_budget_id)[0]

    seeded = _seed_txns(sandbox_client, live_budget_id, n=5, memo_suffix="batch-flat")
    assert len(seeded) == 5
    txn_ids = [t["id"] for t in seeded]

    proposals = [_flat_non_amazon_proposal(txn_id, cat_id) for txn_id in txn_ids]
    cs_path = _build_enrich_changeset(tmp_path, sandbox_budget_id, non_amazon=proposals)

    report = apply_changeset(cs_path, patch_client, sandbox_budget_id, report_dir=tmp_path)

    assert not report.aborted, f"aborted: {report.abort_reason}"
    assert report.failed == []
    assert len(report.applied) == 5

    cs_after = json.loads(cs_path.read_text())
    applied_proposals = cs_after["non_amazon"]["proposals"]
    assert all("applied_at" in p for p in applied_proposals), (
        f"all 5 proposals must be marked applied_at; got "
        f"{[p.get('applied_at') for p in applied_proposals]}"
    )

    txns, _ = patch_client.get_account_transactions(
        sandbox_budget_id, sandbox_client._sandbox_account_id
    )
    fetched_by_id = {t["id"]: t for t in txns}
    for txn_id in txn_ids:
        found = fetched_by_id.get(txn_id)
        assert found is not None, f"seeded txn {txn_id} not found in account"
        assert found["category_id"] == cat_id, (
            f"txn {txn_id} category not updated; got {found.get('category_id')!r}"
        )


def test_apply_changeset_split_against_sandbox(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    """End-to-end: 1 amazon split proposal applied via the batch endpoint.
    Verifies subtransaction milliunit math survives the round-trip and that
    the on-disk changeset is marked applied_at."""
    cat_ids = _pick_two_categories(patch_client, sandbox_budget_id)

    seeded = _seed_txns(sandbox_client, live_budget_id, n=1, memo_suffix="batch-split")
    parent = seeded[0]

    proposal = _amazon_split_proposal(parent, cat_ids[0], cat_ids[1])
    cs_path = _build_enrich_changeset(tmp_path, sandbox_budget_id, amazon_splits=[proposal])

    report = apply_changeset(cs_path, patch_client, sandbox_budget_id, report_dir=tmp_path)

    assert not report.aborted, f"aborted: {report.abort_reason}"
    assert report.failed == []
    assert len(report.applied) == 1
    assert report.applied[0]["type"] == "split"
    assert report.applied[0]["subtxn_count"] == 2

    cs_after = json.loads(cs_path.read_text())
    assert "applied_at" in cs_after["amazon"]["proposed_splits"][0]

    txns, _ = patch_client.get_account_transactions(
        sandbox_budget_id, sandbox_client._sandbox_account_id
    )
    found = next((t for t in txns if t["id"] == parent["id"]), None)
    assert found is not None
    subs = found.get("subtransactions", [])
    assert len(subs) == 2
    sub_amounts = sorted(s["amount"] for s in subs)
    assert sub_amounts == [-10000, -5500]
    assert sum(s["amount"] for s in subs) == parent["amount"]


def test_apply_changeset_resume_against_sandbox(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    """Resume semantics: re-running apply_changeset on a partially-applied
    on-disk changeset must skip items already marked applied_at and not
    re-PATCH them."""
    cat_id = _pick_two_categories(patch_client, sandbox_budget_id)[0]

    seeded = _seed_txns(sandbox_client, live_budget_id, n=2, memo_suffix="batch-resume")
    proposals = [_flat_non_amazon_proposal(t["id"], cat_id) for t in seeded]
    cs_path = _build_enrich_changeset(tmp_path, sandbox_budget_id, non_amazon=proposals)

    first = apply_changeset(cs_path, patch_client, sandbox_budget_id, report_dir=tmp_path)
    assert len(first.applied) == 2

    second = apply_changeset(cs_path, patch_client, sandbox_budget_id, report_dir=tmp_path)
    assert len(second.applied) == 0
    assert all("already applied" in s["reason"] for s in second.skipped)
