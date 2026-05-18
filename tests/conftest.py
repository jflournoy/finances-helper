import os
import pytest
from dotenv import load_dotenv
from ynab_client import YNABClient


@pytest.fixture(scope="session")
def live_client():
    """YNABClient with sandbox_mode forced OFF — for read-only tests against the
    user's real budget. Even when .env has YNAB_SANDBOX_MODE=1 (for write
    safety), reads don't need redirection and the 'live' name should not be
    misleading."""
    load_dotenv()
    client = YNABClient()
    client.sandbox_mode = False
    return client


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
    if os.environ.get("YNAB_SANDBOX_MODE") != "1":
        pytest.skip("YNAB_SANDBOX_MODE=1 required in .env for write integration tests")
    return YNABClient()


@pytest.fixture(scope="session")
def patch_client():
    """YNABClient with sandbox_mode forced OFF — for PATCH against real Sandbox txn IDs.

    update_transaction() refuses to run when sandbox_mode=True (PATCH cannot be safely
    redirected). For integration tests that exercise PATCH, callers must use a
    non-sandbox client pointed directly at the Sandbox budget ID.
    """
    load_dotenv()
    client = YNABClient()
    client.sandbox_mode = False
    return client


@pytest.fixture(scope="session")
def sandbox_budget_id(sandbox_client):
    """Resolve the Sandbox budget ID (lazy-initialized on first write)."""
    sandbox_client._ensure_sandbox_initialized()
    return sandbox_client._sandbox_budget_id
