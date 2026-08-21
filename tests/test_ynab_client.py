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
    YNABValidationError,
    YNABConflictError,
)

FIXTURES = Path("data/fixtures")


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def mock_get(client, fixture_data):
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture_data
    mock_resp.headers = {}
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
    mock_resp.headers = {}
    return mock_resp


@pytest.fixture
def client():
    c = YNABClient(token="test-token")
    c.sandbox_mode = False
    return c


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
    mock_resp.headers = {}
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
    mock_resp.headers = {}
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID, since_date="2026-03-01")
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("since_date") == "2026-03-01"


def test_get_transactions_type_param_passed(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.headers = {}
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID, type="uncategorized")
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("type") == "uncategorized"


def test_get_transactions_none_params_omitted(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.headers = {}
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
    mock_resp.headers = {}
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_account_transactions(BUDGET_ID, ACCOUNT_ID)
    call_url = mock_session_get.call_args[0][0]
    assert f"/accounts/{ACCOUNT_ID}/transactions" in call_url


def test_get_transactions_delta_sync_param(client):
    fixture = load_fixture("ynab_transactions.json")
    mock_resp = Mock(status_code=200)
    mock_resp.headers = {}
    mock_resp.json.return_value = fixture
    with patch.object(client.session, "get", return_value=mock_resp) as mock_session_get:
        client.get_transactions(BUDGET_ID, last_knowledge_of_server=300)
    call_kwargs = mock_session_get.call_args
    params = call_kwargs[1].get("params", {})
    assert params.get("last_knowledge_of_server") == 300


def test_get_transactions_injects_amount_dollars(client):
    """Each returned transaction must have amount_dollars derived from amount milliunits."""
    fixture = load_fixture("ynab_transactions.json")
    with mock_get(client, fixture):
        txns, _ = client.get_transactions(BUDGET_ID)
    assert len(txns) >= 1
    for t in txns:
        assert "amount_dollars" in t, "amount_dollars must be injected at the boundary"
        assert t["amount_dollars"] == t["amount"] / 1000.0


def test_get_transactions_injects_amount_dollars_into_subtransactions(client):
    """Subtransactions must also have amount_dollars injected."""
    fixture = {
        "data": {
            "transactions": [
                {
                    "id": "p1", "date": "2026-04-01", "amount": -10000, "deleted": False,
                    "subtransactions": [
                        {"id": "s1", "amount": -3000, "deleted": False},
                        {"id": "s2", "amount": -7000, "deleted": False},
                    ],
                },
            ],
            "server_knowledge": 1,
        }
    }
    with mock_get(client, fixture):
        txns, _ = client.get_transactions(BUDGET_ID)
    assert len(txns) == 1
    assert txns[0]["amount_dollars"] == -10.0
    subs = txns[0]["subtransactions"]
    assert len(subs) == 2
    assert subs[0]["amount_dollars"] == -3.0
    assert subs[1]["amount_dollars"] == -7.0


def test_get_account_transactions_injects_amount_dollars(client):
    """get_account_transactions must also inject amount_dollars."""
    fixture = load_fixture("ynab_transactions.json")
    with mock_get(client, fixture):
        txns, _ = client.get_account_transactions(BUDGET_ID, ACCOUNT_ID)
    for t in txns:
        assert "amount_dollars" in t
        assert t["amount_dollars"] == t["amount"] / 1000.0


class TestFilterUncategorizedWritable:
    """Test filter_uncategorized_writable for writability constraints."""

    def test_excludes_approved(self):
        """Transaction with approved=True is excluded."""
        txn = {"id": "t1", "approved": True, "cleared": "cleared", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_excludes_reconciled(self):
        """Transaction with cleared == 'reconciled' is excluded."""
        txn = {"id": "t1", "approved": False, "cleared": "reconciled", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_excludes_deleted(self):
        """Transaction with deleted == True is excluded."""
        txn = {"id": "t1", "approved": False, "cleared": "cleared", "deleted": True}
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_includes_unapproved_cleared(self):
        """Unapproved, cleared, not deleted is included."""
        txn = {"id": "t1", "approved": False, "cleared": "cleared", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == [txn]

    def test_includes_unapproved_uncleared(self):
        """Unapproved, uncleared, not deleted is included."""
        txn = {"id": "t1", "approved": False, "cleared": "uncleared"}
        result = filter_uncategorized_writable([txn])
        assert result == [txn]

    def test_includes_categorized_unapproved(self):
        """Categorized but unapproved transaction is included (auto-rule assigned category)."""
        txn = {"id": "t1", "approved": False, "category_id": "cat-123", "cleared": "cleared", "deleted": False}
        result = filter_uncategorized_writable([txn])
        assert result == [txn]

    def test_empty_input(self):
        """Empty list returns empty list."""
        result = filter_uncategorized_writable([])
        assert result == []

    def test_mixed_states(self):
        """Multiple txns with mixed states filters correctly."""
        txns = [
            {"id": "t1", "approved": False, "cleared": "cleared", "deleted": False},  # include
            {"id": "t2", "approved": True, "cleared": "cleared", "deleted": False},   # exclude (approved)
            {"id": "t3", "approved": False, "cleared": "reconciled", "deleted": False},  # exclude (reconciled)
            {"id": "t4", "approved": False, "cleared": "cleared", "deleted": True},   # exclude (deleted)
            {"id": "t5", "approved": False, "cleared": "uncleared"},                  # include
        ]
        result = filter_uncategorized_writable(txns)
        assert len(result) == 2
        assert result[0]["id"] == "t1"
        assert result[1]["id"] == "t5"

    def test_handles_missing_keys(self):
        """Does not raise on missing keys, uses .get()."""
        txn = {"id": "t1"}  # missing approved, cleared, deleted
        result = filter_uncategorized_writable([txn])
        # approved missing → not True → included
        assert result == [txn]

    def test_excludes_existing_split(self):
        """A transaction already split in YNAB is excluded.

        YNAB reports a split parent with category_id=None, which otherwise looks
        like an uncategorized transaction. Re-proposing a split for it produces a
        400 ("subtransactions cannot be updated on an existing split transaction")
        that aborts the whole batch PATCH.
        """
        txn = {
            "id": "t1", "approved": False, "cleared": "cleared", "deleted": False,
            "category_id": None,
            "subtransactions": [
                {"id": "s1", "amount": -1000, "category_id": "cat-a", "deleted": False},
                {"id": "s2", "amount": -2000, "category_id": "cat-b", "deleted": False},
            ],
        }
        result = filter_uncategorized_writable([txn])
        assert result == []

    def test_includes_txn_whose_split_was_removed(self):
        """Deleted subtransactions do not count: an un-split txn is writable again."""
        txn = {
            "id": "t1", "approved": False, "cleared": "cleared", "deleted": False,
            "subtransactions": [
                {"id": "s1", "amount": -1000, "category_id": "cat-a", "deleted": True},
            ],
        }
        result = filter_uncategorized_writable([txn])
        assert result == [txn]

    def test_includes_txn_with_empty_subtransactions(self):
        """An empty subtransactions list is the normal shape for a flat txn."""
        txn = {"id": "t1", "approved": False, "cleared": "cleared", "deleted": False, "subtransactions": []}
        result = filter_uncategorized_writable([txn])
        assert result == [txn]


# Issue #155: Sandbox initialization refactor

def test_init_does_not_call_api_when_sandbox_mode_set(monkeypatch):
    """Constructor must NOT make any API calls, even with YNAB_SANDBOX_MODE=1."""
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    monkeypatch.setenv("YNAB_API_TOKEN", "test-tok")
    with patch.object(requests.Session, "get", side_effect=AssertionError("no API call during __init__")):
        client = YNABClient()
    assert client.sandbox_mode is True
    assert client._sandbox_budget_id is None
    assert client._sandbox_account_id is None


def test_init_does_not_print_when_sandbox_mode_set(monkeypatch, capsys):
    """Constructor must be silent — no SANDBOX MODE banner at construction time."""
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    monkeypatch.setenv("YNAB_API_TOKEN", "test-tok")
    YNABClient()
    captured = capsys.readouterr()
    assert "SANDBOX MODE" not in captured.out


def test_sandbox_init_runs_on_first_create_transactions(monkeypatch):
    """First call to create_transactions in sandbox mode triggers _init_sandbox()."""
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    client = YNABClient(token="tok")

    monkeypatch.setattr(client, "get_budgets", lambda: [{"id": "sb-budget-id", "name": "Sandbox"}])
    monkeypatch.setattr(client, "get_accounts", lambda budget_id: [{"id": "sb-account-id", "name": "Sandbox"}])

    post_called_with = {}
    def fake_post(path, payload):
        post_called_with["path"] = path
        post_called_with["payload"] = payload
        return {"transactions": []}
    monkeypatch.setattr(client, "_post", fake_post)

    client.create_transactions("real-budget", [{"account_id": "real-acct", "amount": -1000, "date": "2026-05-08"}])

    assert client._sandbox_budget_id == "sb-budget-id"
    assert client._sandbox_account_id == "sb-account-id"
    assert post_called_with["path"] == "/budgets/sb-budget-id/transactions"
    assert post_called_with["payload"]["transactions"][0]["account_id"] == "sb-account-id"


def test_sandbox_init_runs_only_once_across_multiple_writes(monkeypatch):
    """_init_sandbox must not run a second time on subsequent create_transactions calls."""
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    client = YNABClient(token="tok")
    monkeypatch.setattr(client, "get_budgets", lambda: [{"id": "sb-b", "name": "Sandbox"}])
    monkeypatch.setattr(client, "get_accounts", lambda budget_id: [{"id": "sb-a", "name": "Sandbox"}])
    monkeypatch.setattr(client, "_post", lambda path, payload: {"transactions": []})

    init_calls = []
    real_init = client._init_sandbox
    def counting_init():
        init_calls.append(1)
        real_init()
    monkeypatch.setattr(client, "_init_sandbox", counting_init)

    txn = {"account_id": "x", "amount": -100, "date": "2026-05-08"}
    client.create_transactions("b1", [txn])
    client.create_transactions("b2", [txn])
    client.create_transactions("b3", [txn])

    assert len(init_calls) == 1


def test_sandbox_init_allows_retry_after_failure(monkeypatch):
    """If _init_sandbox raises, _sandbox_initialized stays False so the next write retries."""
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    client = YNABClient(token="tok")
    monkeypatch.setattr(client, "get_budgets", lambda: [])  # no Sandbox budget — init will raise

    txn = {"account_id": "x", "amount": -100, "date": "2026-05-08"}
    with pytest.raises(ValueError, match="not found"):
        client.create_transactions("b1", [txn])
    assert client._sandbox_initialized is False


def test_non_sandbox_create_transactions_unaffected(monkeypatch):
    """When YNAB_SANDBOX_MODE is not set, create_transactions hits the given budget directly."""
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    client = YNABClient(token="tok")
    post_called_with = {}
    monkeypatch.setattr(client, "_post", lambda path, payload: post_called_with.update({"path": path, "payload": payload}) or {"transactions": []})
    txn = {"account_id": "user-acct", "amount": -500, "date": "2026-05-08"}
    client.create_transactions("user-budget", [txn])
    assert post_called_with["path"] == "/budgets/user-budget/transactions"
    assert post_called_with["payload"]["transactions"][0]["account_id"] == "user-acct"


# Issue #1: _patch() method and error classes


def test_patch_success_returns_data_dict(client):
    fixture = {"data": {"transaction": {"id": "t1", "amount": -5000}}}
    with patch.object(client.session, "patch", return_value=make_response(200, fixture)):
        result = client._patch("/budgets/b1/transactions/t1", {"category_id": "cat-123"})
    assert result == fixture["data"]


def test_patch_uses_patch_http_method(client):
    fixture = {"data": {"transaction": {"id": "t1"}}}
    with patch.object(client.session, "patch", return_value=make_response(200, fixture)) as mock_patch:
        client._patch("/budgets/b1/transactions/t1", {"category_id": "cat-123"})
    mock_patch.assert_called_once()


def test_patch_sends_json_payload(client):
    fixture = {"data": {"transaction": {"id": "t1"}}}
    payload = {"category_id": "cat-123", "memo": "updated"}
    with patch.object(client.session, "patch", return_value=make_response(200, fixture)) as mock_patch:
        client._patch("/budgets/b1/transactions/t1", payload)
    call_kwargs = mock_patch.call_args[1]
    assert call_kwargs["json"] == payload


def test_patch_400_raises_validation_error(client):
    from ynab_client import YNABValidationError
    body = {"error": {"id": "400", "name": "bad_request", "detail": "Split sum mismatch"}}
    with patch.object(client.session, "patch", return_value=make_response(400, body)):
        with pytest.raises(YNABValidationError) as exc:
            client._patch("/budgets/b1/transactions/t1", {})
    assert "Split sum mismatch" in str(exc.value)


def test_patch_404_raises_not_found(client):
    body = {"error": {"id": "404", "name": "resource_not_found", "detail": "Transaction not found"}}
    with patch.object(client.session, "patch", return_value=make_response(404, body)):
        with pytest.raises(YNABNotFoundError):
            client._patch("/budgets/b1/transactions/bad-id", {})


def test_patch_409_raises_conflict_error(client):
    from ynab_client import YNABConflictError
    body = {"error": {"id": "409", "name": "conflict", "detail": "Transaction was modified"}}
    with patch.object(client.session, "patch", return_value=make_response(409, body)):
        with pytest.raises(YNABConflictError):
            client._patch("/budgets/b1/transactions/t1", {})


def test_patch_429_raises_rate_limit(client):
    body = {"error": {"id": "429", "name": "too_many_requests", "detail": "Rate limit exceeded"}}
    with patch.object(client.session, "patch", return_value=make_response(429, body)):
        with pytest.raises(YNABRateLimitError):
            client._patch("/budgets/b1/transactions/t1", {})


def test_patch_500_raises_api_error(client):
    body = {"error": {"id": "500", "name": "internal_server_error", "detail": "Server error"}}
    with patch.object(client.session, "patch", return_value=make_response(500, body)):
        with pytest.raises(YNABAPIError) as exc:
            client._patch("/budgets/b1/transactions/t1", {})
    assert exc.value.status_code == 500


def test_patch_malformed_json_raises_api_error(client):
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("No JSON")
    mock_resp.text = "not json"
    mock_resp.headers = {}
    with patch.object(client.session, "patch", return_value=mock_resp):
        with pytest.raises(YNABAPIError, match="Malformed"):
            client._patch("/budgets/b1/transactions/t1", {})


def test_patch_missing_data_key_raises_api_error(client):
    body = {"something_else": {}}
    with patch.object(client.session, "patch", return_value=make_response(200, body)):
        with pytest.raises(YNABAPIError, match="data"):
            client._patch("/budgets/b1/transactions/t1", {})


def test_validation_error_is_subclass_of_api_error():
    from ynab_client import YNABValidationError
    assert issubclass(YNABValidationError, YNABAPIError)


def test_conflict_error_is_subclass_of_api_error():
    from ynab_client import YNABConflictError
    assert issubclass(YNABConflictError, YNABAPIError)


# Issue #2: update_transaction() method


def test_update_transaction_wraps_body_under_transaction_key(client):
    fixture = {"data": {"transaction": {"id": "t1", "amount": -5000}}}
    patch_dict = {"category_id": "cat-123"}
    with patch.object(client, "_patch", return_value=fixture["data"]) as mock_patch:
        client.update_transaction("b1", "t1", patch_dict)
    call_args = mock_patch.call_args
    assert call_args[0][1] == {"transaction": patch_dict}


def test_update_transaction_uses_correct_url(client):
    fixture = {"data": {"transaction": {"id": "t1"}}}
    with patch.object(client, "_patch", return_value=fixture["data"]) as mock_patch:
        client.update_transaction("b1", "t1", {})
    call_args = mock_patch.call_args
    assert call_args[0][0] == "/budgets/b1/transactions/t1"


def test_update_transaction_returns_unwrapped_transaction(client):
    txn_data = {"id": "t1", "amount": -10000, "category_id": "cat-456"}
    with patch.object(client, "_patch", return_value={"transaction": txn_data}):
        result = client.update_transaction("b1", "t1", {})
    assert result == txn_data


def test_update_transaction_sandbox_mode_raises_notimplemented():
    client = YNABClient(token="tok")
    client.sandbox_mode = True
    with pytest.raises(NotImplementedError) as exc:
        client.update_transaction("b1", "t1", {})
    assert "create_transactions" in str(exc.value)
    assert "seed" in str(exc.value)


def test_update_transaction_propagates_404(client):
    with patch.object(client, "_patch", side_effect=YNABNotFoundError(404, detail="Not found")):
        with pytest.raises(YNABNotFoundError):
            client.update_transaction("b1", "bad-id", {})


def test_update_transaction_propagates_409(client):
    with patch.object(client, "_patch", side_effect=YNABConflictError(409, detail="Conflict")):
        with pytest.raises(YNABConflictError):
            client.update_transaction("b1", "t1", {})


def test_update_transaction_propagates_400_with_detail(client):
    with patch.object(client, "_patch", side_effect=YNABValidationError(400, detail="sum mismatch")):
        with pytest.raises(YNABValidationError) as exc:
            client.update_transaction("b1", "t1", {})
    assert "sum mismatch" in str(exc.value)


def test_update_transaction_with_subtransactions_preserves_amounts(client):
    patch_dict = {"subtransactions": [{"amount": -37090, "category_id": "abc"}]}
    with patch.object(client, "_patch", return_value={"transaction": {}}) as mock_patch:
        client.update_transaction("b1", "t1", patch_dict)
    call_args = mock_patch.call_args
    passed_patch = call_args[0][1]
    assert passed_patch["transaction"]["subtransactions"][0]["amount"] == -37090


# Issue #3: Rate-limit header parsing


def test_rate_limit_attributes_initialized_to_none():
    client = YNABClient(token="tok")
    assert client.rate_limit_used is None
    assert client.rate_limit_max is None


def test_rate_limit_header_parsed_from_get(client):
    fixture = {"data": {"budgets": []}}
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.headers = {"X-Rate-Limit": "42/200"}
    with patch.object(client.session, "get", return_value=mock_resp):
        client._get("/budgets")
    assert client.rate_limit_used == 42
    assert client.rate_limit_max == 200


def test_rate_limit_header_parsed_from_post(client):
    fixture = {"data": {"transactions": []}}
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.headers = {"X-Rate-Limit": "150/200"}
    with patch.object(client.session, "post", return_value=mock_resp):
        client._post("/budgets/b1/transactions", {"transactions": []})
    assert client.rate_limit_used == 150
    assert client.rate_limit_max == 200


def test_rate_limit_header_parsed_from_patch(client):
    fixture = {"data": {"transaction": {"id": "t1"}}}
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.headers = {"X-Rate-Limit": "99/200"}
    with patch.object(client.session, "patch", return_value=mock_resp):
        client._patch("/budgets/b1/transactions/t1", {})
    assert client.rate_limit_used == 99
    assert client.rate_limit_max == 200


def test_rate_limit_header_missing_leaves_values_unchanged(client):
    client.rate_limit_used = 10
    client.rate_limit_max = 200
    fixture = {"data": {"budgets": []}}
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.headers = {}
    with patch.object(client.session, "get", return_value=mock_resp):
        client._get("/budgets")
    assert client.rate_limit_used == 10
    assert client.rate_limit_max == 200


def test_rate_limit_header_malformed_leaves_values_unchanged(client):
    client.rate_limit_used = 10
    client.rate_limit_max = 200
    fixture = {"data": {"budgets": []}}
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = fixture
    mock_resp.headers = {"X-Rate-Limit": "garbage"}
    with patch.object(client.session, "get", return_value=mock_resp):
        client._get("/budgets")
    assert client.rate_limit_used == 10
    assert client.rate_limit_max == 200


def test_rate_limit_remaining_returns_none_when_unknown():
    client = YNABClient(token="tok")
    assert client.rate_limit_remaining() is None


def test_rate_limit_remaining_arithmetic():
    client = YNABClient(token="tok")
    client.rate_limit_used = 190
    client.rate_limit_max = 200
    assert client.rate_limit_remaining() == 10


def test_rate_limit_remaining_at_zero():
    client = YNABClient(token="tok")
    client.rate_limit_used = 200
    client.rate_limit_max = 200
    assert client.rate_limit_remaining() == 0


# Issue #179: update_transactions() batch PATCH


def test_update_transactions_empty_list_raises(client):
    with pytest.raises(ValueError, match="must not be empty"):
        client.update_transactions("b1", [])


def test_update_transactions_missing_id_raises(client):
    with pytest.raises(ValueError, match=r"updates\[1\].*missing.*id"):
        client.update_transactions("b1", [{"id": "t1"}, {"memo": "no-id"}])


def test_update_transactions_correct_url(client):
    with patch.object(client, "_patch", return_value={"transactions": []}) as mock_patch:
        client.update_transactions("b1", [{"id": "t1"}])
    call_args = mock_patch.call_args
    assert call_args[0][0] == "/budgets/b1/transactions"


def test_update_transactions_wraps_under_transactions_key(client):
    updates = [{"id": "t1", "memo": "updated"}, {"id": "t2", "category_id": "cat-456"}]
    with patch.object(client, "_patch", return_value={"transactions": []}) as mock_patch:
        client.update_transactions("b1", updates)
    call_args = mock_patch.call_args
    assert call_args[0][1] == {"transactions": updates}


def test_update_transactions_returns_full_response_data(client):
    response_data = {
        "transactions": [{"id": "t1", "memo": "updated"}],
        "server_knowledge": 42,
    }
    with patch.object(client, "_patch", return_value=response_data):
        result = client.update_transactions("b1", [{"id": "t1", "memo": "updated"}])
    assert result == response_data
    assert result["server_knowledge"] == 42


def test_update_transactions_sandbox_mode_raises(client):
    client.sandbox_mode = True
    with pytest.raises(NotImplementedError, match="create_transactions"):
        client.update_transactions("b1", [{"id": "t1"}])


def test_update_transactions_propagates_rate_limit(client):
    with patch.object(client, "_patch", side_effect=YNABRateLimitError(429, detail="Rate limit")):
        with pytest.raises(YNABRateLimitError):
            client.update_transactions("b1", [{"id": "t1"}])
