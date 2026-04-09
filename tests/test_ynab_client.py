"""Tests for ynab_client.py — milliunit conversions, date handling, response parsing."""
import json
import os
import pytest
import requests
from unittest.mock import patch, Mock
from ynab_client import (
    dollars_to_milliunits,
    milliunits_to_dollars,
    YNABClient,
    YNABAPIError,
    YNABNotFoundError,
    YNABRateLimitError,
)


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
