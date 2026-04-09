"""Tests for ynab_client.py — milliunit conversions, date handling, response parsing."""
import os
import pytest
import requests
from unittest.mock import patch
from ynab_client import dollars_to_milliunits, milliunits_to_dollars, YNABClient


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
