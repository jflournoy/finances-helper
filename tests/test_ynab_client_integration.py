"""Integration tests — hit the real YNAB API.

Run with: uv run pytest tests/test_ynab_client_integration.py -v -m integration

Read tests use live_client + live_budget_id (My Budget, read-only).
Write tests use sandbox_client — requires YNAB_SANDBOX_MODE=1 in .env,
which redirects all writes to the Sandbox budget/account.

Requires YNAB_API_TOKEN and YNAB_DEFAULT_BUDGET in .env.
"""
import pytest
from ynab_client import YNABClient

pytestmark = pytest.mark.integration


def test_get_budgets_live(live_client):
    budgets = live_client.get_budgets()
    assert len(budgets) > 0
    assert "id" in budgets[0]
    assert "name" in budgets[0]


def test_get_accounts_live(live_client, live_budget_id):
    accounts = live_client.get_accounts(live_budget_id)
    assert isinstance(accounts, list)
    if accounts:
        assert "id" in accounts[0]
        assert "name" in accounts[0]
        assert "balance" in accounts[0]


def test_get_categories_live(live_client, live_budget_id):
    groups = live_client.get_categories(live_budget_id)
    assert isinstance(groups, list)
    assert len(groups) > 0
    assert "categories" in groups[0]


def test_get_payees_live(live_client, live_budget_id):
    payees = live_client.get_payees(live_budget_id)
    assert isinstance(payees, list)
    if payees:
        assert "id" in payees[0]
        assert "name" in payees[0]


def test_get_transactions_live(live_client, live_budget_id):
    txns, sk = live_client.get_transactions(live_budget_id, since_date="2026-01-01")
    assert isinstance(txns, list)
    assert isinstance(sk, int)
    if txns:
        t = txns[0]
        assert "id" in t
        assert "date" in t
        assert "amount" in t


def test_get_transactions_delta_sync_live(live_client, live_budget_id):
    _, sk = live_client.get_transactions(live_budget_id)
    txns2, sk2 = live_client.get_transactions(live_budget_id, last_knowledge_of_server=sk)
    assert isinstance(txns2, list)
    assert sk2 >= sk


def test_create_transactions_sandbox(sandbox_client, live_budget_id):
    """Write a real transaction to Sandbox via create_transactions."""
    txn = {
        "date": "2026-05-08",
        "amount": -9990,
        "payee_name": "Integration Test Payee",
        "memo": "pytest sandbox write test",
        "cleared": "uncleared",
        "approved": False,
        "account_id": "will-be-overridden-by-sandbox",
    }
    result = sandbox_client.create_transactions(live_budget_id, [txn])
    created = result.get("transactions", [])
    assert len(created) == 1
    t = created[0]
    assert t["amount"] == -9990
    assert t["date"] == "2026-05-08"
    assert t["memo"] == "pytest sandbox write test"


def test_create_transactions_sandbox_rejects_empty(sandbox_client, live_budget_id):
    with pytest.raises(ValueError, match="must not be empty"):
        sandbox_client.create_transactions(live_budget_id, [])


def test_sandbox_client_construction_makes_no_api_calls(monkeypatch):
    """Construction with YNAB_SANDBOX_MODE=1 must make zero API calls (lazy init)."""
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    client = YNABClient()
    assert client.sandbox_mode is True
    assert client._sandbox_budget_id is None
    assert client._sandbox_account_id is None
    assert client._sandbox_initialized is False


def test_update_transaction_sandbox_real_patch(sandbox_client, patch_client, sandbox_budget_id, live_budget_id):
    """Seed a real txn in Sandbox via sandbox_client.create_transactions,
    then PATCH it via patch_client.update_transaction (which has sandbox_mode=False
    because PATCH operates on a real txn ID and cannot be safely redirected).
    """
    from ynab_client import dollars_to_milliunits

    categories = patch_client.get_categories(sandbox_budget_id)
    cat_id = None
    for group in categories:
        for cat in group.get("categories", []):
            if not cat.get("hidden"):
                cat_id = cat["id"]
                break
        if cat_id:
            break
    assert cat_id, "No visible category found in Sandbox budget"

    txn = {
        "date": "2026-05-10",
        "amount": dollars_to_milliunits(-15.50),
        "payee_name": "Test PATCH Target",
        "memo": "PYTEST-CONFIRM-update_transaction",
        "cleared": "uncleared",
        "approved": False,
        "account_id": "will-be-overridden",
    }
    created_result = sandbox_client.create_transactions(live_budget_id, [txn])
    created_txns = created_result.get("transactions", [])
    assert len(created_txns) == 1
    txn_id = created_txns[0]["id"]

    patched = patch_client.update_transaction(
        sandbox_budget_id,
        txn_id,
        {"category_id": cat_id},
    )
    assert patched["id"] == txn_id
    assert patched["category_id"] == cat_id

    txns, _ = patch_client.get_account_transactions(
        sandbox_budget_id,
        sandbox_client._sandbox_account_id,
    )
    found = next((t for t in txns if t["id"] == txn_id), None)
    assert found is not None
    assert found["category_id"] == cat_id


@pytest.mark.integration
def test_update_transactions_sandbox_real_patch(sandbox_client, patch_client, sandbox_budget_id, live_budget_id):
    """Seed 3 real txns in Sandbox via sandbox_client.create_transactions,
    then batch-PATCH them via patch_client.update_transactions (which has sandbox_mode=False).
    Verify response order is not guaranteed but all txns are present.
    """
    from ynab_client import dollars_to_milliunits

    categories = patch_client.get_categories(sandbox_budget_id)
    cat_id = None
    for group in categories:
        for cat in group.get("categories", []):
            if not cat.get("hidden"):
                cat_id = cat["id"]
                break
        if cat_id:
            break
    assert cat_id, "No visible category found in Sandbox budget"

    txns_to_create = [
        {
            "date": "2026-05-10",
            "amount": dollars_to_milliunits(-15.50),
            "payee_name": "Test Batch PATCH Target 1",
            "memo": "PYTEST-BATCH-1",
            "cleared": "uncleared",
            "approved": False,
            "account_id": "will-be-overridden",
        },
        {
            "date": "2026-05-11",
            "amount": dollars_to_milliunits(-20.00),
            "payee_name": "Test Batch PATCH Target 2",
            "memo": "PYTEST-BATCH-2",
            "cleared": "uncleared",
            "approved": False,
            "account_id": "will-be-overridden",
        },
        {
            "date": "2026-05-12",
            "amount": dollars_to_milliunits(-10.75),
            "payee_name": "Test Batch PATCH Target 3",
            "memo": "PYTEST-BATCH-3",
            "cleared": "uncleared",
            "approved": False,
            "account_id": "will-be-overridden",
        },
    ]
    created_result = sandbox_client.create_transactions(live_budget_id, txns_to_create)
    created_txns = created_result.get("transactions", [])
    assert len(created_txns) == 3
    txn_ids = [t["id"] for t in created_txns]

    updates = [
        {"id": txn_id, "memo": "PYTEST-BATCH-updated"}
        for txn_id in txn_ids
    ]
    response = patch_client.update_transactions(sandbox_budget_id, updates)

    assert "transactions" in response
    response_txns = response["transactions"]
    assert len(response_txns) == 3

    response_by_id = {t["id"]: t for t in response_txns}
    for txn_id in txn_ids:
        assert txn_id in response_by_id, f"txn {txn_id} not in response"
        assert response_by_id[txn_id]["memo"] == "PYTEST-BATCH-updated"

    txns, _ = patch_client.get_account_transactions(
        sandbox_budget_id,
        sandbox_client._sandbox_account_id,
    )
    for txn_id in txn_ids:
        found = next((t for t in txns if t["id"] == txn_id), None)
        assert found is not None, f"txn {txn_id} not found in account"
        assert found["memo"] == "PYTEST-BATCH-updated"
