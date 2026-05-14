"""Integration tests — hit the real YNAB API.

Run with: uv run pytest tests/test_ynab_client_integration.py -v -m integration

Read tests use live_client + live_budget_id (My Budget, read-only).
Write tests use sandbox_client — requires YNAB_SANDBOX_MODE=1 in .env,
which redirects all writes to the Sandbox budget/account.

Requires YNAB_API_TOKEN and YNAB_DEFAULT_BUDGET in .env.
"""
import os
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


def test_sandbox_client_construction_makes_no_api_calls():
    """Construction with YNAB_SANDBOX_MODE=1 must make zero API calls (lazy init)."""
    from dotenv import load_dotenv
    load_dotenv()
    assert os.environ.get("YNAB_SANDBOX_MODE") == "1", "requires YNAB_SANDBOX_MODE=1 in .env"
    client = YNABClient()
    assert client.sandbox_mode is True
    assert client._sandbox_budget_id is None
    assert client._sandbox_account_id is None
    assert client._sandbox_initialized is False
