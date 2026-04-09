"""Tests for ynab_client.py — milliunit conversions, date handling, response parsing."""
import pytest
from ynab_client import dollars_to_milliunits, milliunits_to_dollars


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
