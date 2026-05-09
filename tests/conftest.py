import os
import pytest
from dotenv import load_dotenv
from ynab_client import YNABClient


@pytest.fixture(scope="session")
def live_client():
    load_dotenv()
    return YNABClient()


@pytest.fixture(scope="session")
def live_budget_id(live_client):
    load_dotenv()
    budget_name = os.environ.get("YNAB_DEFAULT_BUDGET")
    assert budget_name, "YNAB_DEFAULT_BUDGET not set in .env"
    return live_client.resolve_budget_id(budget_name)


@pytest.fixture(scope="session")
def sandbox_client():
    """YNABClient with sandbox mode enforced — safe for write tests."""
    load_dotenv()
    assert os.environ.get("YNAB_SANDBOX_MODE") == "1", (
        "YNAB_SANDBOX_MODE=1 must be set in .env before running write integration tests"
    )
    return YNABClient()
