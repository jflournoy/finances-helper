import pytest
from dotenv import load_dotenv
from ynab_client import YNABClient


@pytest.fixture(scope="session")
def live_client():
    load_dotenv()
    return YNABClient()


@pytest.fixture(scope="session")
def live_budget_id(live_client):
    budgets = live_client.get_budgets()
    assert len(budgets) > 0, "No budgets found in live YNAB account"
    return budgets[0]["id"]
