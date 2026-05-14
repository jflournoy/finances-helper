"""Integration tests for amazon_confirm.apply_changeset against a real YNAB Sandbox budget.

Pattern: seed real txns in Sandbox via sandbox_client.create_transactions, then
build a synthetic changeset referencing those txn IDs and apply it via a
non-sandbox patch_client (PATCH cannot be safely redirected — see
update_transaction's NotImplementedError when sandbox_mode=True).

Seeded txns are tagged with memo prefix 'PYTEST-CONFIRM-' so they can be
identified for manual cleanup in the Sandbox UI (YNAB API does not support
deletion on the standard plan as of this writing).

Gated on `pytest -m integration` (requires YNAB_API_TOKEN, YNAB_DEFAULT_BUDGET,
YNAB_SANDBOX_MODE=1 in .env).
"""
import json
import pytest
from pathlib import Path

from amazon_confirm import apply_changeset
from ynab_client import dollars_to_milliunits, YNABValidationError

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


def _seed_txn(sandbox_client, live_budget_id, amount_milliunits, memo_suffix):
    txn = {
        "date": "2026-05-10",
        "amount": amount_milliunits,
        "payee_name": "Amazon.com",
        "memo": f"{SEED_MEMO_PREFIX}{memo_suffix}",
        "cleared": "uncleared",
        "approved": False,
        "account_id": "will-be-overridden-by-sandbox",
    }
    result = sandbox_client.create_transactions(live_budget_id, [txn])
    return result["transactions"][0]


def _build_changeset(tmp_path, proposals, generated_at="2026-05-10T00:00:00"):
    cs = {
        "version": 1,
        "generated_at": generated_at,
        "summary": {"proposed_splits": len(proposals)},
        "proposed_splits": proposals,
    }
    path = tmp_path / "amazon-changeset-integration.json"
    path.write_text(json.dumps(cs, indent=2))
    return path


def _flat_proposal(parent_txn, cat_id):
    return {
        "parent_ynab_transaction_id": parent_txn["id"],
        "parent_ynab_transaction": parent_txn,
        "shipment": {
            "order_id": "111-INT-TEST",
            "ship_date": "2026-05-10",
            "payment_method_last4": "1234",
            "total_amount": "15.50",
            "item_count": 1,
        },
        "subtransactions": [
            {
                "item": {
                    "order_id": "111-INT-TEST",
                    "ship_date": "2026-05-10",
                    "asin": "B0INT00001",
                    "product_name": "Integration Test Widget",
                    "quantity": 1,
                    "unit_price": "15.50",
                    "unit_price_tax": "0.00",
                    "raw_row_index": 0,
                },
                "allocated_amount": "15.50",
                "category_id": cat_id,
                "category_name": "Test",
                "confidence": 1.0,
                "rationale": "integration test",
            }
        ],
    }


def _split_proposal(parent_txn, cat_id_a, cat_id_b, amount_a="10.00", amount_b="5.50"):
    return {
        "parent_ynab_transaction_id": parent_txn["id"],
        "parent_ynab_transaction": parent_txn,
        "shipment": {
            "order_id": "111-INT-SPLIT",
            "ship_date": "2026-05-10",
            "payment_method_last4": "1234",
            "total_amount": "15.50",
            "item_count": 2,
        },
        "subtransactions": [
            {
                "item": {
                    "order_id": "111-INT-SPLIT",
                    "ship_date": "2026-05-10",
                    "asin": "B0INT00002",
                    "product_name": "Integration Item A",
                    "quantity": 1,
                    "unit_price": amount_a,
                    "unit_price_tax": "0.00",
                    "raw_row_index": 0,
                },
                "allocated_amount": amount_a,
                "category_id": cat_id_a,
                "category_name": "TestA",
                "confidence": 1.0,
                "rationale": "integration test A",
            },
            {
                "item": {
                    "order_id": "111-INT-SPLIT",
                    "ship_date": "2026-05-10",
                    "asin": "B0INT00003",
                    "product_name": "Integration Item B",
                    "quantity": 1,
                    "unit_price": amount_b,
                    "unit_price_tax": "0.00",
                    "raw_row_index": 1,
                },
                "allocated_amount": amount_b,
                "category_id": cat_id_b,
                "category_name": "TestB",
                "confidence": 1.0,
                "rationale": "integration test B",
            },
        ],
    }


def test_integration_flat_patch_applies_category(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    cat_ids = _pick_two_categories(patch_client, sandbox_budget_id)
    cat_id = cat_ids[0]

    txn = _seed_txn(sandbox_client, live_budget_id, dollars_to_milliunits(-15.50), "flat")
    proposal = _flat_proposal(txn, cat_id)
    changeset_path = _build_changeset(tmp_path, [proposal])

    report = apply_changeset(changeset_path, patch_client, sandbox_budget_id, throttle_seconds=0.0)
    assert not report.aborted
    assert len(report.applied) == 1
    assert len(report.failed) == 0

    txns, _ = patch_client.get_account_transactions(sandbox_budget_id, sandbox_client._sandbox_account_id)
    found = next((t for t in txns if t["id"] == txn["id"]), None)
    assert found is not None
    assert found["category_id"] == cat_id


def test_integration_split_patch_creates_subtransactions(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    cat_ids = _pick_two_categories(patch_client, sandbox_budget_id)

    txn = _seed_txn(sandbox_client, live_budget_id, dollars_to_milliunits(-15.50), "split")
    proposal = _split_proposal(txn, cat_ids[0], cat_ids[1], amount_a="10.00", amount_b="5.50")
    changeset_path = _build_changeset(tmp_path, [proposal])

    report = apply_changeset(changeset_path, patch_client, sandbox_budget_id, throttle_seconds=0.0)
    assert not report.aborted
    assert len(report.applied) == 1
    assert len(report.failed) == 0

    txns, _ = patch_client.get_account_transactions(sandbox_budget_id, sandbox_client._sandbox_account_id)
    found = next((t for t in txns if t["id"] == txn["id"]), None)
    assert found is not None
    assert len(found.get("subtransactions", [])) == 2
    sub_amounts = sorted(s["amount"] for s in found["subtransactions"])
    assert sub_amounts == [-10000, -5500] or sub_amounts == [-5500, -10000][::-1] or sub_amounts == sorted([-10000, -5500])
    assert sum(s["amount"] for s in found["subtransactions"]) == -15500


def test_integration_resume_skips_already_applied(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    cat_ids = _pick_two_categories(patch_client, sandbox_budget_id)
    cat_id = cat_ids[0]

    txn = _seed_txn(sandbox_client, live_budget_id, dollars_to_milliunits(-15.50), "resume")
    proposal = _flat_proposal(txn, cat_id)
    changeset_path = _build_changeset(tmp_path, [proposal])

    first = apply_changeset(changeset_path, patch_client, sandbox_budget_id, throttle_seconds=0.0)
    assert len(first.applied) == 1

    second = apply_changeset(changeset_path, patch_client, sandbox_budget_id, throttle_seconds=0.0)
    assert len(second.applied) == 0
    assert any("already applied" in s["reason"] for s in second.skipped)


def test_integration_400_on_split_sum_mismatch(
    sandbox_client, patch_client, sandbox_budget_id, live_budget_id, tmp_path
):
    """A changeset whose subtransaction amounts don't sum to parent amount
    will be rejected. proposal_to_patch_body() catches the mismatch client-side
    and raises ValueError; apply_changeset propagates it (this is a bug-in-data
    case that should fail loudly, not be caught as YNAB validation)."""
    cat_ids = _pick_two_categories(patch_client, sandbox_budget_id)

    txn = _seed_txn(sandbox_client, live_budget_id, dollars_to_milliunits(-15.50), "summismatch")
    proposal = _split_proposal(txn, cat_ids[0], cat_ids[1], amount_a="10.00", amount_b="3.00")
    changeset_path = _build_changeset(tmp_path, [proposal])

    with pytest.raises(ValueError, match="invariant"):
        apply_changeset(changeset_path, patch_client, sandbox_budget_id, throttle_seconds=0.0)
