"""Integration tests — hit the real YNAB API.

Run with: uv run pytest tests/test_ynab_client_integration.py -v -m integration

Requires YNAB_API_TOKEN in .env (or environment).
"""
import pytest

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
    # First fetch — get server_knowledge
    _, sk = live_client.get_transactions(live_budget_id)
    # Second fetch with server_knowledge — should return empty or only new txns
    txns2, sk2 = live_client.get_transactions(live_budget_id, last_knowledge_of_server=sk)
    assert isinstance(txns2, list)
    assert sk2 >= sk
