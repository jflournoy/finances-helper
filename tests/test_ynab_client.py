"""Tests for ynab_client.py — milliunit conversions, date handling, response parsing."""
import json
import os
import pytest
import requests
from pathlib import Path
from unittest.mock import patch, Mock
from ynab_client import (
    dollars_to_milliunits,
    milliunits_to_dollars,
    filter_uncategorized_writable,
    YNABClient,
    YNABAPIError,
    YNABNotFoundError,
    YNABRateLimitError,
)

FIXTURES = Path("data/fixtures")


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def mock_get(client, fixture_data):
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture_data
    return patch.object(client.session, "get", return_value=mock_resp)


def test_dollars_to_milliunits_positive():
    assert dollars_to_milliunits(100.50) == 100500


def test_dollars_to_milliunits_negative():
    assert dollars_to_milliunits(-42.99) == -42990


def test_dollars_to_milliunits_zero():
    assert dollars_to_milliunits(0) == 0


def test_dollars_to_milliunits_truncates_sub_milliunit():
    assert dollars_to_milliunits(10.9999) == 10999


def test_milliunits_to_dollars_positive():
    assert milliunits_to_dollars(100500) == 100.50


def test_milliunits_to_dollars_negative():
    assert milliunits_to_dollars(-42990) == -42.99


def test_milliunits_to_dollars_zero():
    assert milliunits_to_dollars(0) == 0.0


def test_roundtrip_positive():
    assert milliunits_to_dollars(dollars_to_milliunits(47.23)) == pytest.approx(47.23)


def test_roundtrip_negative():
    assert milliunits_to_dollars(dollars_to_milliunits(-12.50)) == pytest.approx(-12.50)


# Issue #2: YNABClient init and authentication

def test_init_with_explicit_token():
    client = YNABClient(token="test-token-123")
    assert client.session.headers["Authorization"] == "Bearer test-token-123"


def test_init_reads_token_from_env():
    with patch.dict(os.environ, {"YNAB_API_TOKEN": "env-token-abc"}):
        client = YNABClient()
    assert client.session.headers["Authorization"] == "Bearer env-token-abc"


def test_init_missing_token_raises():
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("YNAB_API_TOKEN", None)
        with pytest.raises(ValueError, match="YNAB_API_TOKEN"):
            YNABClient()


def test_init_base_url():
    client = YNABClient(token="tok")
    assert client.base_url == "https://api.ynab.com/v1"


def test_init_session_is_requests_session():
    client = YNABClient(token="tok")
    assert isinstance(client.session, requests.Session)


def test_init_session_content_type_header():
    client = YNABClient(token="tok")
    assert client.session.headers.get("Content-Type") == "application/json"


# Issue #3: Core _get() method and custom exceptions

def make_response(status_code, body):
    """Helper: build a mock requests.Response."""
    mock_resp = Mock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = body
    mock_resp.text = json.dumps(body)
    return mock_resp


@pytest.fixture
def client():
    return YNABClient(token="test-token")


def test_get_success_returns_data_dict(client):
    fixture = {"data": {"budgets": [{"id": "abc", "name": "My Budget"}], "server_knowledge": 42}}
    with patch.object(client.session, "get", return_value=make_response(200, fixture)):
        result = client._get("/budgets")
    assert result == fixture["data"]


def test_get_404_raises_not_found(client):
    body = {"error": {"id": "404", "name": "resource_not_found", "detail": "Budget not found"}}
    with patch.object(client.session, "get", return_value=make_response(404, body)):
        with pytest.raises(YNABNotFoundError) as exc:
            client._get("/budgets/bad-id")
    assert "Budget not found" in str(exc.value)


def test_get_429_raises_rate_limit(client):
    body = {"error": {"id": "429", "name": "too_many_requests", "detail": "Rate limit exceeded"}}
    with patch.object(client.session, "get", return_value=make_response(429, body)):
        with pytest.raises(YNABRateLimitError):
            client._get("/budgets")


def test_get_500_raises_api_error(client):
    body = {"error": {"id": "500", "name": "internal_server_error", "detail": "Something went wrong"}}
    with patch.object(client.session, "get", return_value=make_response(500, body)):
        with pytest.raises(YNABAPIError) as exc:
            client._get("/budgets")
    assert exc.value.status_code == 500


def test_get_malformed_json_raises_api_error(client):
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("No JSON")
    mock_resp.text = "not json"
    with patch.object(client.session, "get", return_value=mock_resp):
        with pytest.raises(YNABAPIError, match="Malformed"):
            client._get("/budgets")


def test_get_missing_data_key_raises_api_error(client):
    body = {"something_else": {}}
    with patch.object(client.session, "get", return_value=make_response(200, body)):
        with pytest.raises(YNABAPIError, match="data"):
            client._get("/budgets")


def test_not_found_is_subclass_of_api_error():
    assert issubclass(YNABNotFoundError, YNABAPIError)


def test_rate_limit_is_subclass_of_api_error():
    assert issubclass(YNABRateLimitError, YNABAPIError)


# Issue #4: Budget and account methods

def test_get_budgets_returns_list(client):
    fixture = load_fixture("ynab_budgets.json")
    with mock_get(client, fixture):
        budgets = client.get_budgets()
    assert isinstance(budgets, list)
    assert len(budgets) == 2
    assert budgets[0]["name"] == "My Budget"


def test_get_budgets_empty(client):
    fixture = {"data": {"budgets": [], "default_budget": None}}
    with mock_get(client, fixture):
        budgets = client.get_budgets()
    assert budgets == []


def test_get_budget_single(client):
    fixture = {"data": {"budget": {"id": "aaaaaaaa-0000-0000-0000-000000000001", "name": "My Budget"}}}
    with mock_get(client, fixture):
        budget = client.get_budget("aaaaaaaa-0000-0000-0000-000000000001")
    assert budget["id"] == "aaaaaaaa-0000-0000-0000-000000000001"


def test_resolve_budget_id_match(client):
    fixture = {"data": {"budgets": [
        {"id": "uuid-a", "name": "Personal"},
        {"id": "uuid-b", "name": "Business"},
    ], "default_budget": None}}
    with mock_get(client, fixture):
        assert client.resolve_budget_id("Personal") == "uuid-a"
        assert client.resolve_budget_id("Business") == "uuid-b"


def test_resolve_budget_id_not_found_lists_available(client):
    fixture = {"data": {"budgets": [
        {"id": "uuid-a", "name": "Personal"},
    ], "default_budget": None}}
    with mock_get(client, fixture):
        with pytest.raises(ValueError, match="Personal"):
            client.resolve_budget_id("DoesNotExist")


def test_resolve_budget_id_ambiguous(client):
    fixture = {"data": {"budgets": [
        {"id": "uuid-a", "name": "Same"},
        {"id": "uuid-b", "name": "Same"},
    ], "default_budget": None}}
    with mock_get(client, fixture):
        with pytest.raises(ValueError, match="matches 2"):
            client.resolve_budget_id("Same")


def test_get_accounts_filters_deleted(client):
    fixture = load_fixture("ynab_accounts.json")
    with mock_get(client, fixture):
        accounts = client.get_accounts("aaaaaaaa-0000-0000-0000-000000000001")
    assert len(accounts) == 2
    assert all(not a["deleted"] for a in accounts)


def test_get_accounts_has_expected_fields(client):
    fixture = load_fixture("ynab_accounts.json")
    with mock_get(client, fixture):
        accounts = client.get_accounts("aaaaaaaa-0000-0000-0000-000000000001")
    acct = accounts[0]
    for field in ("id", "name", "type", "on_budget", "balance"):
        assert field in acct, f"Missing field: {field}"


# Issue #5: Category and payee methods

BUDGET_ID = "aaaaaaaa-0000-0000-0000-000000000001"


def test_get_categories_returns_groups(client):
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    # Fixture has 3 groups, 1 deleted → 2 returned
    assert len(groups) == 2
    assert groups[0]["name"] == "Monthly Bills"


def test_get_categories_groups_have_categories_list(client):
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    assert "categories" in groups[0]
    assert isinstance(groups[0]["categories"], list)


def test_get_categories_filters_deleted_categories(client):
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    # Monthly Bills group: 2 categories, 1 deleted → 1 remaining
    monthly_bills = groups[0]
    assert len(monthly_bills["categories"]) == 1
    assert monthly_bills["categories"][0]["name"] == "Rent"


def test_get_categories_includes_hidden_categories(client):
    # Hidden ≠ deleted. Hidden categories must be included.
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    # Rent is not hidden, verify hidden field is preserved
    assert "hidden" in groups[0]["categories"][0]


def test_get_categories_category_fields(client):
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    cat = groups[0]["categories"][0]
    for field in ("id", "category_group_id", "name", "hidden", "budgeted", "activity", "balance"):
        assert field in cat, f"Missing field: {field}"


def test_get_categories_filters_deleted_groups(client):
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    # Fixture has 3 groups, 1 deleted → 2 returned
    assert len(groups) == 2
    assert all(not g.get("deleted", False) for g in groups)


def test_get_categories_preserves_group_fields(client):
    """Verify that extra fields on category groups are not dropped."""
    fixture = load_fixture("ynab_categories.json")
    with mock_get(client, fixture):
        groups = client.get_categories(BUDGET_ID)
    # The fixture groups have "deleted" field — verify it's preserved
    assert "deleted" in groups[0]


def test_get_payees_returns_list(client):
    fixture = load_fixture("ynab_payees.json")
    with mock_get(client, fixture):
        payees = client.get_payees(BUDGET_ID)
    assert len(payees) == 2  # 3 in fixture, 1 deleted


def test_get_payees_filters_deleted(client):
    fixture = load_fixture("ynab_payees.json")
    with mock_get(client, fixture):
        payees = client.get_payees(BUDGET_ID)
    assert all(not p.get("deleted", False) for p in payees)


def test_get_payees_has_id_and_name(client):
    fixture = load_fixture("ynab_payees.json")
    with mock_get(client, fixture):
        payees = client.get_payees(BUDGET_ID)
    assert payees[0]["name"] == "Whole Foods"
    assert "id" in payees[0]


# Issue #6: Transaction methods with delta sync

ACCOUNT_ID = "bbbbbbbb-0000-0000-0000-000000000001"


def test_get_transactions_returns_tuple(client):
    fixture = load_fixture("ynab_transactions.json")
    with mock_get(client, fixture):
        result = client.get_transactions(BUDGET_ID)
    assert isinstance(result, tuple)
    txns, sk = result
    assert isinstance(txns, list)
    assert isinstance(sk, int)


def test_get_transactions_filters_deleted(client):
    fixture = load_fixture("ynab_transactions.json")
    with mock_get(client, fixture):
        txns, _ = client.get_transactions(BUDGET_ID)
    assert len(txns) == 2
    assert all(not t.get("deleted", False) for t in txns)


def test_get_transactions_returns_server_knowledge(client):
    fixture = load_fixture("ynab_transactions.json")
    with mock_get(client, fixture):
        _, sk = client.get_transactions(BUDGET_ID)
    assert sk == 350


def test_get_transactions_since_date_passed_as_param(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID, since_date="2026-03-01")
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("since_date") == "2026-03-01"


def test_get_transactions_type_param_passed(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID, type="uncategorized")
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("type") == "uncategorized"


def test_get_transactions_none_params_omitted(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID)
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params")
    # params should be None or empty dict when no optional params given
    if params is not None:
        assert "since_date" not in params
        assert "type" not in params
        assert "last_knowledge_of_server" not in params


def test_get_transactions_transaction_fields(client):
    fixture = load_fixture("ynab_transactions.json")
    with mock_get(client, fixture):
        txns, _ = client.get_transactions(BUDGET_ID)
    t = txns[0]
    for field in ("id", "date", "amount", "payee_name", "category_id", "memo", "cleared", "approved", "account_id"):
        assert field in t, f"Missing field: {field}"


def test_get_transactions_empty_returns_tuple(client):
    fixture = {"data": {"transactions": [], "server_knowledge": 500}}
    with mock_get(client, fixture):
        txns, sk = client.get_transactions(BUDGET_ID)
    assert txns == []
    assert sk == 500


def test_get_account_transactions_uses_account_path(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_account_transactions(BUDGET_ID, ACCOUNT_ID)
    call_url = mock_session_get.call_args[0][0]
    assert f"/accounts/{ACCOUNT_ID}/transactions" in call_url


def test_get_transactions_delta_sync_param(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID, last_knowledge_of_server=300)
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("last_knowledge_of_server") == 300


class TestFilterUncategorizedWritable:
    """Test filter_uncategorized_writable for writability constraints."""

    def test_excludes_categorized(self):
        """Transaction with category_id set is excluded."""
        txn = {"id": "t1", "category_id": "cat-123", "cleared": "cleared", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_excludes_reconciled(self):
        """Transaction with cleared == 'reconciled' is excluded."""
        txn = {"id": "t1", "category_id": None, "cleared": "reconciled", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_excludes_deleted(self):
        """Transaction with deleted == True is excluded."""
        txn = {"id": "t1", "category_id": None, "cleared": "cleared", "deleted": True}
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_includes_uncategorized_cleared(self):
        """Uncategorized, cleared, not deleted is included."""
        txn = {"id": "t1", "category_id": None, "cleared": "cleared", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == [txn]

    def test_includes_uncategorized_uncleared(self):
        """Uncategorized, uncleared, not deleted is included."""
        txn = {"id": "t1", "category_id": None, "cleared": "uncleared"}
        result = filter_uncategorized_writable([txn])
        assert result == [txn]

    def test_empty_input(self):
        """Empty list returns empty list."""
        result = filter_uncategorized_writable([])
        assert result == []

    def test_mixed_states(self):
        """Multiple txns with mixed states filters correctly."""
        txns = [
            {"id": "t1", "category_id": None, "cleared": "cleared", "deleted": False},  # include
            {"id": "t2", "category_id": "cat-123", "cleared": "cleared", "deleted": False},  # exclude (categorized)
            {"id": "t3", "category_id": None, "cleared": "reconciled", "deleted": False},  # exclude (reconciled)
            {"id": "t4", "category_id": None, "cleared": "cleared", "deleted": True},  # exclude (deleted)
            {"id": "t5", "category_id": None, "cleared": "uncleared"},  # include
        ]
        result = filter_uncategorized_writable(txns)
        assert len(result) == 2
        assert result[0]["id"] == "t1"
        assert result[1]["id"] == "t5"

    def test_handles_missing_keys(self):
        """Does not raise on missing keys, uses .get()."""
        txn = {"id": "t1"}  # missing category_id, cleared, deleted
        result = filter_uncategorized_writable([txn])
        # Should treat missing keys as: category_id=None, cleared!=reconciled, deleted!=True
        assert result == [txn]
