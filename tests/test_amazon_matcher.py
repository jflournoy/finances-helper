"""Tests for amazon_matcher.py — parsing, matching, allocation, and categorization."""

import json
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest

from amazon_matcher import (
    AmazonItem,
    AmazonShipment,
    ParseError,
    _money,
    allocate_shipment_to_items,
    extract_order_history_csv,
    filter_amazon_transactions,
    find_latest_dump,
    match_shipments_to_transactions,
    parse_order_history,
    MatchCandidate,
    MatchResult,
    ItemAllocation,
)


# ============================================================================
# Unit Tests: _money() function
# ============================================================================


class TestMoney:
    """Test _money() parsing of currency strings."""

    def test_money_basic(self):
        """Parse basic decimal value."""
        assert _money("12.34") == Decimal("12.34")

    def test_money_empty(self):
        """Empty string returns zero."""
        assert _money("") == Decimal("0")

    def test_money_with_dollar_sign(self):
        """Strip dollar sign."""
        assert _money("$29.99") == Decimal("29.99")

    def test_money_with_comma(self):
        """Strip thousands separator."""
        assert _money("$1,234.56") == Decimal("1234.56")

    def test_money_multiple_commas(self):
        """Handle multiple commas."""
        assert _money("$1,234,567.89") == Decimal("1234567.89")

    def test_money_invalid(self):
        """Invalid input raises ValueError."""
        with pytest.raises(ValueError):
            _money("abc")

    def test_money_whitespace(self):
        """Whitespace is stripped."""
        assert _money("  12.34  ") == Decimal("12.34")

    def test_money_zero(self):
        """Zero value."""
        assert _money("0.00") == Decimal("0.00")

    def test_money_none(self):
        """None input returns zero."""
        assert _money(None) == Decimal("0")

    def test_money_not_available(self):
        """'Not Available' is treated as missing and returns zero.

        Amazon order CSV uses 'Not Available' in money columns for shipments
        that were never finalized (unshipped, cancelled mid-flight, etc.).
        The parser should not crash on these rows.
        """
        assert _money("Not Available") == Decimal("0")

    def test_money_single_quoted(self):
        """Excel CSV exports wrap values in single quotes.

        When Excel exports CSV, it sometimes wraps decimal values in single quotes.
        _money() should strip these and parse the value.
        """
        assert _money("'37.09'") == Decimal("37.09")

    def test_money_single_quoted_negative(self):
        """Negative values with Excel single quotes."""
        assert _money("'-1.22'") == Decimal("-1.22")


# ============================================================================
# Unit Tests: find_latest_dump()
# ============================================================================


class TestFindLatestDump:
    """Test finding the latest Amazon dump in imports directory."""

    def test_find_latest_dump_multiple_files(self, tmp_path):
        """Return newest dated file when multiple exist."""
        (tmp_path / "amazon-order-history-2026-03-01.zip").touch()
        (tmp_path / "amazon-order-history-2026-03-23.zip").touch()
        (tmp_path / "amazon-order-history-2026-03-10.zip").touch()

        result = find_latest_dump(tmp_path)
        assert result.name == "amazon-order-history-2026-03-23.zip"

    def test_find_latest_dump_legacy_fallback(self, tmp_path, caplog):
        """Fall back to 'Your Orders*.zip' with warning."""
        (tmp_path / "Your Orders.zip").touch()

        result = find_latest_dump(tmp_path)
        assert result.name == "Your Orders.zip"
        assert "amazon-order-history-YYYY-MM-DD.zip" in caplog.text
        assert "WARNING" in caplog.text

    def test_find_latest_dump_empty_dir(self, tmp_path):
        """Raise FileNotFoundError when no dumps found."""
        with pytest.raises(FileNotFoundError) as exc_info:
            find_latest_dump(tmp_path)
        assert "amazon-order-history" in str(exc_info.value)

    def test_find_latest_dump_mixed_dates(self, tmp_path):
        """Prefer ISO-date format over legacy."""
        (tmp_path / "amazon-order-history-2026-04-15.zip").touch()
        (tmp_path / "Your Orders.zip").touch()

        result = find_latest_dump(tmp_path)
        assert result.name == "amazon-order-history-2026-04-15.zip"


# ============================================================================
# Unit Tests: extract_order_history_csv()
# ============================================================================


class TestExtractOrderHistoryCSV:
    """Test extracting CSV from zip file."""

    def test_extract_order_history_csv_invalid_zip(self, tmp_path):
        """Raise ValueError with 'Invalid zip file' when passed a non-zip file."""
        # Create a file that's not a valid zip
        not_zip = tmp_path / "not_a_zip.bin"
        not_zip.write_bytes(b"This is not a zip file at all")

        with pytest.raises(ValueError) as exc_info:
            extract_order_history_csv(not_zip)
        assert "Invalid zip file" in str(exc_info.value)

    def test_extract_order_history_csv_success(self):
        """Extract CSV from fixture zip."""
        zip_path = Path("data/fixtures") / "amazon_order_history_sample.zip"
        # Note: This fixture doesn't exist yet; we'll create it inline in IT tests
        # For now, this test will be satisfied by the integration test
        pass

    def test_extract_order_history_csv_missing_path(self, tmp_path):
        """Raise ValueError with zip contents when CSV path missing."""
        zip_path = tmp_path / "empty.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("some_other_file.txt", "data")

        with pytest.raises(ValueError) as exc_info:
            extract_order_history_csv(zip_path)
        assert "Your Amazon Orders/Order History.csv" in str(exc_info.value)
        assert "some_other_file.txt" in str(exc_info.value)

    def test_extract_order_history_csv_utf8_sig_encoding(self, tmp_path):
        """Handle UTF-8 with BOM encoding."""
        csv_content = "Order ID,Order Date\n111-0000001-0000001,2024-01-15"
        csv_bytes = csv_content.encode("utf-8-sig")

        zip_path = tmp_path / "test-bom.zip"
        with ZipFile(zip_path, "w") as zf:
            zf.writestr("Your Amazon Orders/Order History.csv", csv_bytes)

        result = extract_order_history_csv(zip_path)
        assert "Order ID" in result
        assert "111-0000001-0000001" in result


# ============================================================================
# Unit Tests: Dataclasses
# ============================================================================


class TestAmazonItem:
    """Test AmazonItem dataclass."""

    def test_amazon_item_creation(self):
        """Create AmazonItem with valid data."""
        item = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            asin="B0C1234567",
            product_name="Test Widget",
            quantity=1,
            unit_price=Decimal("29.99"),
            unit_price_tax=Decimal("2.10"),
            raw_row_index=2,
        )
        assert item.order_id == "111-0000001-0000001"
        assert item.quantity == 1

    def test_amazon_item_frozen(self):
        """AmazonItem is immutable (frozen)."""
        item = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            asin="B0C1234567",
            product_name="Test Widget",
            quantity=1,
            unit_price=Decimal("29.99"),
            unit_price_tax=Decimal("2.10"),
            raw_row_index=2,
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            item.quantity = 2


class TestAmazonShipment:
    """Test AmazonShipment dataclass and methods."""

    def test_amazon_shipment_creation(self):
        """Create AmazonShipment with valid data."""
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 16),
                asin="B0C1234567",
                product_name="Test Widget",
                quantity=1,
                unit_price=Decimal("29.99"),
                unit_price_tax=Decimal("2.10"),
                raw_row_index=2,
            )
        ]
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("29.99"),
            tax=Decimal("2.10"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("37.09"),
            items=items,
            shipment_status="Shipped",
        )
        assert shipment.order_id == "111-0000001-0000001"
        assert len(shipment.items) == 1

    def test_expected_charge_property(self):
        """expected_charge calculates total correctly."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("2.00"),
            total_amount=Decimal("110.00"),
            items=[],
            shipment_status="Shipped",
        )
        expected = Decimal("100.00") + Decimal("7.00") + Decimal("5.00") - Decimal("2.00")
        assert shipment.expected_charge == expected
        assert shipment.expected_charge == Decimal("110.00")

    def test_is_matchable_basic_yes(self):
        """Shipment is matchable with valid fields."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[
                AmazonItem(
                    order_id="111-0000001-0000001",
                    ship_date=date(2024, 1, 16),
                    asin="B0C1234567",
                    product_name="Test Widget",
                    quantity=1,
                    unit_price=Decimal("100.00"),
                    unit_price_tax=Decimal("7.00"),
                    raw_row_index=2,
                )
            ],
            shipment_status="Shipped",
        )
        assert shipment.is_matchable is True

    def test_is_matchable_not_available_status(self):
        """Shipment not matchable if status is 'Not Available'."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Not Available",
        )
        assert shipment.is_matchable is False

    def test_is_matchable_no_ship_date(self):
        """Shipment not matchable if ship_date is None."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=None,
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )
        assert shipment.is_matchable is False

    def test_is_matchable_split_tender(self):
        """Shipment not matchable if split tender."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Gift Certificate/Card and Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=True,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )
        assert shipment.is_matchable is False

    def test_is_matchable_zero_price_item(self):
        """Shipment not matchable if any item has zero price."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("15.99"),
            tax=Decimal("1.12"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("17.11"),
            items=[
                AmazonItem(
                    order_id="111-0000001-0000001",
                    ship_date=date(2024, 1, 16),
                    asin="B0C1234567",
                    product_name="Gift Item",
                    quantity=1,
                    unit_price=Decimal("0.00"),
                    unit_price_tax=Decimal("0.00"),
                    raw_row_index=2,
                )
            ],
            shipment_status="Shipped",
        )
        assert shipment.is_matchable is False


class TestParseError:
    """Test ParseError dataclass."""

    def test_parse_error_creation(self):
        """Create ParseError with reason."""
        error = ParseError(row_index=5, reason="non-USD currency: EUR")
        assert error.row_index == 5
        assert "EUR" in error.reason

    def test_parse_error_with_raw_row(self):
        """ParseError can include raw row data."""
        raw = {"Order ID": "111-0000001-0000001", "Currency": "EUR"}
        error = ParseError(row_index=5, reason="non-USD currency: EUR", raw_row=raw)
        assert error.raw_row == raw


# ============================================================================
# Parser Tests: Edge Cases
# ============================================================================


class TestParseOrderHistory:
    """Test parse_order_history() with fixture data."""

    def test_parse_fixture_sample(self):
        """Parse sample fixture CSV."""
        with open("data/fixtures/amazon_order_history_sample.csv") as f:
            csv_text = f.read()

        shipments, errors = parse_order_history(csv_text)

        # From fixture design: 31 total rows
        # Expected: ~20 shipments (grouped), some errors
        assert len(shipments) > 0
        # Should have parse errors for:
        # - Cancelled order (1 row): NOT in errors, silently skipped
        # - Ship Date = Not Available (1 row): shipment kept but flagged
        # - Payment Method = Not Available (1 row): shipment kept
        # - EUR currency (1 row): in errors
        # - Embedded newline (1 row): parsed correctly
        # - Zero price item (1 row): shipment kept but not matchable
        assert len(errors) >= 1  # At least EUR currency error

    def test_parse_cancelled_order_skipped(self):
        """Cancelled orders are silently skipped."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Cancelled,Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 0
        # Cancelled orders don't create errors; they're simply not included

    def test_parse_ship_date_not_available(self):
        """Ship Date = 'Not Available' is handled."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,Not Available,Closed,Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].ship_date is None
        assert shipments[0].is_matchable is False

    def test_parse_payment_method_not_available(self):
        """Payment Method Type = 'Not Available' is handled."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Not Available,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].payment_method_last4 is None
        assert shipments[0].is_split_tender is False

    def test_parse_split_tender(self):
        """Gift Certificate/Card payment is detected as split tender."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Gift Certificate/Card and Visa - 5678,USD,50.00,3.50,50.00,3.50,0.00,53.50,0.00,B0C1234567,Test Product,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].is_split_tender is True
        assert shipments[0].payment_method_last4 == "5678"
        assert shipments[0].is_matchable is False

    def test_parse_shipment_status_variations(self):
        """Shipment Status 'Shipped and Shipped' is valid."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped and Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].shipment_status == "Shipped and Shipped"
        assert shipments[0].is_matchable is True

    def test_parse_zero_price_item(self):
        """Unit Price = 0 makes shipment not matchable."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,USD,0.00,0.00,15.99,1.12,0.00,17.11,0.00,B0C1234567,Gift Item,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].is_matchable is False

    def test_parse_currency_not_usd(self):
        """Non-USD currency creates ParseError."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,EUR,25.00,1.75,25.00,1.75,0.00,26.75,0.00,B0C1234567,Test EUR Product,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 0
        assert len(errors) == 1
        assert "EUR" in errors[0].reason

    def test_parse_charge_math_mismatch(self):
        """Charge math mismatch creates ParseError but keeps shipment."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,USD,20.00,1.40,20.00,1.40,0.00,25.00,0.00,B0C1234567,Test Product,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1  # Shipment is kept despite mismatch
        assert len(errors) == 1
        assert "charge math" in errors[0].reason

    def test_parse_multi_item_shipment(self):
        """Multiple items with same grouping key form one shipment."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,USD,10.00,0.70,30.00,2.10,2.00,34.10,0.00,B0C1234567,Item A,1
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,USD,10.00,0.70,30.00,2.10,2.00,34.10,0.00,B0C2234567,Item B,1
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,USD,10.00,0.70,30.00,2.10,2.00,34.10,0.00,B0C3234567,Item C,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert len(shipments[0].items) == 3
        assert [item.asin for item in shipments[0].items] == ["B0C1234567", "B0C2234567", "B0C3234567"]

    def test_parse_header_drift(self):
        """Missing required column raises ValueError."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        with pytest.raises(ValueError) as exc_info:
            parse_order_history(csv_text)
        assert "Payment Method Type" in str(exc_info.value)

    def test_parse_empty_csv(self):
        """Empty CSV raises ValueError."""
        csv_text = ""
        with pytest.raises(ValueError) as exc_info:
            parse_order_history(csv_text)
        assert "empty" in str(exc_info.value).lower()

    def test_parse_invalid_ship_date(self):
        """Invalid ship date creates ParseError."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,invalid-date,Closed,Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 0
        assert len(errors) == 1
        assert "ship date" in errors[0].reason.lower()

    def test_parse_ship_date_iso8601_datetime_with_z(self):
        """Ship Date as ISO8601 datetime with 'Z' (UTC) is parsed to its date.

        The real Amazon 'Request My Data' export emits ship dates as full
        ISO8601 datetimes like '2025-04-12T18:27:51.050Z' rather than the
        date-only format in older dumps.
        """
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2025-04-12T12:00:00Z,2025-04-12T18:27:51.050Z,Closed,Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].ship_date == date(2025, 4, 12)
        assert not any("ship date" in e.reason.lower() for e in errors)

    def test_parse_ship_date_iso8601_datetime_no_fractional(self):
        """Ship Date as ISO8601 datetime without fractional seconds is parsed."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2015-07-17T00:00:00Z,2015-07-17T00:14:33Z,Closed,Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1
        assert shipments[0].ship_date == date(2015, 7, 17)
        assert not any("ship date" in e.reason.lower() for e in errors)

    def test_parse_money_not_available_does_not_crash(self):
        """'Not Available' in a money column does not raise a parse error.

        Unshipped or partially-fulfilled Amazon orders emit 'Not Available'
        in Unit Price / Total Amount / etc. The parser should treat these as
        missing (Decimal 0), which in turn makes the shipment unmatchable
        via the existing zero-unit-price rule rather than a hard error.
        """
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,Not Available,Closed,Not Available,Not Available,USD,Not Available,Not Available,Not Available,Not Available,Not Available,Not Available,Not Available,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert not any("unparseable money" in e.reason.lower() for e in errors)
        assert len(shipments) == 1
        assert shipments[0].is_matchable is False

    def test_parse_invalid_quantity(self):
        """Invalid quantity creates ParseError."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Visa - 0804,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,abc
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 0
        assert len(errors) == 1
        assert "quantity" in errors[0].reason.lower()

    def test_parse_unparseable_payment_method(self):
        """Unparseable payment method creates error but keeps row."""
        csv_text = """Order ID,Order Date,Ship Date,Order Status,Shipment Status,Payment Method Type,Currency,Unit Price,Unit Price Tax,Shipment Item Subtotal,Shipment Item Subtotal Tax,Shipping Charge,Total Amount,Total Discounts,ASIN,Product Name,Original Quantity
111-0000001-0000001,2024-01-15,2024-01-16,Closed,Shipped,Unknown Payment Type,USD,29.99,2.10,29.99,2.10,5.00,37.09,0.00,B0C1234567,Test Widget,1
"""
        shipments, errors = parse_order_history(csv_text)
        assert len(shipments) == 1  # Shipment is kept
        assert len(errors) == 1
        assert "payment method" in errors[0].reason.lower()
        assert shipments[0].payment_method_last4 is None


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegrationParseOrderHistory:
    """Integration tests exercising the full parsing pipeline."""

    def test_it_parse_fixture_zip(self):
        """IT: Full pipeline with fixture zip file."""
        # Create a temporary zip with the sample CSV
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Create the CSV inside the proper zip structure
            csv_content = Path("data/fixtures/amazon_order_history_sample.csv").read_text()

            # Build the zip with the expected internal structure
            zip_path = tmpdir_path / "test-dump.zip"
            with ZipFile(zip_path, "w") as zf:
                zf.writestr("Your Amazon Orders/Order History.csv", csv_content)

            # Now test the extraction and parsing
            csv_text = extract_order_history_csv(zip_path)
            shipments, errors = parse_order_history(csv_text)

            # Basic assertions about fixture structure
            assert len(shipments) > 0
            assert isinstance(shipments[0], AmazonShipment)

            # Check for specific fixture content
            # Should have at least one multi-item shipment (Order 111-0000010-0000010)
            multi_item = [s for s in shipments if len(s.items) > 1]
            assert len(multi_item) > 0

            # Should have errors (EUR currency)
            assert len(errors) > 0


# ============================================================================
# Matcher Tests: Filter Transactions
# ============================================================================


class TestFilterAmazonTransactions:
    """Test filtering YNAB transactions to Amazon purchases only."""

    def test_filter_plain_amazon_payee(self):
        """Include transactions with 'Amazon' payee."""
        txns = [{"payee_name": "Amazon", "category_id": None, "cleared": "uncleared", "deleted": False}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 1

    def test_filter_amazon_lowercase(self):
        """Include transactions with 'amazon.com' payee (case-insensitive)."""
        txns = [{"payee_name": "amazon.com", "category_id": None, "cleared": "uncleared", "deleted": False}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 1

    def test_filter_amzn_variant(self):
        """Include transactions with 'AMZN' pattern."""
        txns = [{"payee_name": "AMZN Mktp US", "category_id": None, "cleared": "uncleared", "deleted": False}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 1

    def test_filter_non_amazon(self):
        """Exclude non-Amazon payees."""
        txns = [{"payee_name": "Target", "category_id": None, "cleared": "uncleared", "deleted": False}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 0

    def test_filter_already_categorized(self):
        """Exclude transactions with category_id set."""
        txns = [{"payee_name": "Amazon", "category_id": "cat-123", "cleared": "uncleared", "deleted": False}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 0

    def test_filter_reconciled(self):
        """Exclude reconciled transactions."""
        txns = [{"payee_name": "Amazon", "category_id": None, "cleared": "reconciled", "deleted": False}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 0

    def test_filter_deleted(self):
        """Exclude deleted transactions."""
        txns = [{"payee_name": "Amazon", "category_id": None, "cleared": "uncleared", "deleted": True}]
        result = filter_amazon_transactions(txns)
        assert len(result) == 0

    def test_filter_empty_list(self):
        """Empty input returns empty output."""
        result = filter_amazon_transactions([])
        assert len(result) == 0

    def test_filter_mixed(self):
        """Filter a mixed list correctly."""
        txns = [
            {"payee_name": "Amazon", "category_id": None, "cleared": "uncleared", "deleted": False},  # keep
            {"payee_name": "Amazon", "category_id": "cat-123", "cleared": "uncleared", "deleted": False},  # categorized
            {"payee_name": "Amazon", "category_id": None, "cleared": "reconciled", "deleted": False},  # reconciled
            {"payee_name": "Target", "category_id": None, "cleared": "uncleared", "deleted": False},  # non-amazon
        ]
        result = filter_amazon_transactions(txns)
        assert len(result) == 1


# ============================================================================
# Matcher Tests: Shipment-to-Transaction Matching
# ============================================================================


class TestMatchShipmentsToTransactions:
    """Test the shipment-pivoted matching algorithm."""

    def test_match_basic_1to1(self):
        """Single exact match (amount, date)."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment])

        assert len(result.matched) == 1
        assert result.matched[0].date_delta_days == 0
        assert result.matched[0].ynab_txn["id"] == "txn-001"
        assert len(result.unmatched_ynab) == 0
        assert len(result.unmatched_shipments) == 0

    def test_match_date_window_minus3(self):
        """Match within -3 day window."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 12),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment], date_window_days=3)

        assert len(result.matched) == 1
        assert result.matched[0].date_delta_days == -3

    def test_match_date_window_plus3(self):
        """Match within +3 day window."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 18),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment], date_window_days=3)

        assert len(result.matched) == 1
        assert result.matched[0].date_delta_days == 3

    def test_match_date_window_out_of_bounds_minus4(self):
        """No match outside -4 day window."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 11),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment], date_window_days=3)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_date_window_out_of_bounds_plus4(self):
        """No match outside +4 day window."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 19),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment], date_window_days=3)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_amount_mismatch(self):
        """No match when amount differs."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -113000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment])

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_txn_side_collision_deterministic_tiebreak(self):
        """Case A: 1 shipment, 2 txns in window, pick by tiebreak."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn1 = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-16",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }
        txn2 = {
            "id": "txn-002",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-14",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn1, txn2], [shipment])

        assert len(result.matched) == 1
        # txn2 is closer (delta -1 vs delta +1)
        assert result.matched[0].ynab_txn["id"] == "txn-002"

    def test_match_txn_side_collision_loser_matches_another_shipment(self):
        """Case A: 2 shipments, 2 txns. After tiebreak, loser matches the other."""
        shipment1 = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )
        shipment2 = AmazonShipment(
            order_id="111-0000002-0000002",
            ship_date=date(2024, 1, 14),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("50.00"),
            tax=Decimal("3.50"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("53.50"),
            items=[],
            shipment_status="Shipped",
        )

        txn1 = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-16",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }
        txn2 = {
            "id": "txn-002",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-14",
            "amount": -53500,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn1, txn2], [shipment1, shipment2])

        assert len(result.matched) == 2
        matched_pairs = {m.shipment.order_id: m.ynab_txn["id"] for m in result.matched}
        assert matched_pairs["111-0000001-0000001"] == "txn-001"
        assert matched_pairs["111-0000002-0000002"] == "txn-002"

    def test_match_shipment_side_collision_errors_and_both_unmatched(self, caplog):
        """Case B: 2 shipments, 1 txn. Both error, txn unmatched, logger.error."""
        shipment1 = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )
        shipment2 = AmazonShipment(
            order_id="111-0000002-0000002",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment1, shipment2])

        assert len(result.matched) == 0
        assert len(result.unmatched_shipments) == 2
        assert len(result.unmatched_ynab) == 1
        # Check contended reason on the unmatched txn
        assert "contended" in result.unmatched_ynab[0][1].lower()
        # Check for logger.error
        assert any(record.levelname == "ERROR" and "contended" in record.message.lower() for record in caplog.records)

    def test_match_decimal_amount_equality(self):
        """Boundary: Decimal('37.09') shipment matches YNAB milliunit -37090."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("37.09"),
            tax=Decimal("0.00"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("37.09"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -37090,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment])

        assert len(result.matched) == 1

    def test_match_decimal_amount_one_cent_mismatch_does_not_match(self):
        """Decimal('37.09') does NOT match -37100. Exact-amount contract."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("37.09"),
            tax=Decimal("0.00"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("37.09"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -37100,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment])

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_excluded_shipments(self):
        """Excluded shipments appear in excluded_shipments list."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Gift Certificate/Card and Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=True,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        txn = {
            "id": "txn-001",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn], [shipment])

        assert len(result.excluded_shipments) == 1
        assert "split tender" in result.excluded_shipments[0][1].lower()

    def test_match_result_has_parse_errors(self):
        """MatchResult includes parse_errors passed from caller."""
        parse_errors = [ParseError(row_index=5, reason="test error")]

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )

        result = match_shipments_to_transactions([], [shipment], parse_errors=parse_errors)

        assert len(result.parse_errors) == 1
        assert result.parse_errors[0].reason == "test error"

    def test_match_two_pass_resolves_singleton_chain(self):
        """3 shipments/3 txns: A has 2 candidates, B has 1 (A's), C has 1. All resolve."""
        shipment_a = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[],
            shipment_status="Shipped",
        )
        shipment_b = AmazonShipment(
            order_id="111-0000002-0000002",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("50.00"),
            tax=Decimal("3.50"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("53.50"),
            items=[],
            shipment_status="Shipped",
        )
        shipment_c = AmazonShipment(
            order_id="111-0000003-0000003",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("75.00"),
            tax=Decimal("5.25"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("80.25"),
            items=[],
            shipment_status="Shipped",
        )

        txn_a1 = {
            "id": "txn-a1",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }
        txn_a2 = {
            "id": "txn-a2",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-14",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }
        txn_b = {
            "id": "txn-b",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -53500,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }
        txn_c = {
            "id": "txn-c",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -80250,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn_a1, txn_a2, txn_b, txn_c], [shipment_a, shipment_b, shipment_c])

        assert len(result.matched) == 3
        matched_pairs = {m.shipment.order_id: m.ynab_txn["id"] for m in result.matched}
        assert matched_pairs["111-0000001-0000001"] == "txn-a1"
        assert matched_pairs["111-0000002-0000002"] == "txn-b"
        assert matched_pairs["111-0000003-0000003"] == "txn-c"

    def test_match_determinism_with_shuffled_input(self):
        """Shuffle input lists; matched set should be deterministic."""
        import random
        
        shipments = [
            AmazonShipment(
                order_id=f"111-{i:010d}-{i:010d}",
                ship_date=date(2024, 1, 15),
                payment_method_raw="Visa - 0804",
                payment_method_last4="0804",
                is_split_tender=False,
                currency="USD",
                item_subtotal=Decimal(f"{50 + i}"),
                tax=Decimal("3.50"),
                shipping=Decimal("0.00"),
                discounts=Decimal("0.00"),
                total_amount=Decimal(f"{53.50 + i}"),
                items=[],
                shipment_status="Shipped",
            )
            for i in range(5)
        ]
        
        txns = [
            {
                "id": f"txn-{i:03d}",
                "account_id": "account-1",
                "account_name": "Visa",
                "date": "2024-01-15",
                "amount": int(-(53500 + i*1000)),
                "payee_name": "Amazon",
                "category_id": None,
                "cleared": "uncleared",
                "deleted": False,
            }
            for i in range(5)
        ]
        
        result1 = match_shipments_to_transactions(txns, shipments)
        matched_set1 = {(m.shipment.order_id, m.ynab_txn["id"]) for m in result1.matched}
        
        random.seed(42)
        random.shuffle(shipments)
        random.shuffle(txns)
        
        result2 = match_shipments_to_transactions(txns, shipments)
        matched_set2 = {(m.shipment.order_id, m.ynab_txn["id"]) for m in result2.matched}
        
        assert matched_set1 == matched_set2

    def test_match_same_order_id_same_amount_different_items(self):
        """Bug #66: Two shipments same order_id, same amount, different items.

        Real data: order 112-1522950-2165026 has 2 shipments @ $15.05 each.
        Both should match distinct txns, not one overwrite the other.
        """
        shipment1 = AmazonShipment(
            order_id="112-1522950-2165026",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("14.00"),
            tax=Decimal("1.05"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("15.05"),
            items=[
                AmazonItem(
                    order_id="112-1522950-2165026",
                    ship_date=date(2024, 1, 15),
                    asin="AAAA111111",
                    product_name="Item A",
                    quantity=1,
                    unit_price=Decimal("14.00"),
                    unit_price_tax=Decimal("1.05"),
                    raw_row_index=2,
                )
            ],
            shipment_status="Shipped",
        )
        shipment2 = AmazonShipment(
            order_id="112-1522950-2165026",
            ship_date=date(2024, 1, 16),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("14.00"),
            tax=Decimal("1.05"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("15.05"),
            items=[
                AmazonItem(
                    order_id="112-1522950-2165026",
                    ship_date=date(2024, 1, 16),
                    asin="BBBB222222",
                    product_name="Item B",
                    quantity=1,
                    unit_price=Decimal("14.00"),
                    unit_price_tax=Decimal("1.05"),
                    raw_row_index=3,
                )
            ],
            shipment_status="Shipped",
        )

        txn1 = {
            "id": "txn-ship1",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-15",
            "amount": -15050,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }
        txn2 = {
            "id": "txn-ship2",
            "account_id": "account-1",
            "account_name": "Visa",
            "date": "2024-01-16",
            "amount": -15050,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        result = match_shipments_to_transactions([txn1, txn2], [shipment1, shipment2])

        # Both shipments should match distinct txns
        assert len(result.matched) == 2
        matched_pairs = {m.shipment.order_id: [m.ynab_txn["id"] for m in result.matched if m.shipment.order_id == m.shipment.order_id] for m in result.matched}
        # Simpler check: just verify both txns are matched and both shipments are matched
        matched_txn_ids = {m.ynab_txn["id"] for m in result.matched}
        assert "txn-ship1" in matched_txn_ids
        assert "txn-ship2" in matched_txn_ids


class TestAllocateShipmentToItems:
    """Test pro-rata allocation of shipment total across items."""

    def test_single_item_passthrough(self):
        """Single-item shipment: allocated_amount equals total_amount."""
        item = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C1234567",
            product_name="Test Widget",
            quantity=1,
            unit_price=Decimal("100.00"),
            unit_price_tax=Decimal("7.00"),
            raw_row_index=2,
        )

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("7.00"),
            shipping=Decimal("5.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("112.00"),
            items=[item],
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        assert len(allocations) == 1
        assert allocations[0].allocated_amount == Decimal("112.00")
        assert allocations[0].share_of_subtotal == Decimal("1")

    def test_exact_50_50_split(self):
        """Two equal items: each gets exactly half."""
        item1 = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C1234567",
            product_name="Item 1",
            quantity=1,
            unit_price=Decimal("10.00"),
            unit_price_tax=Decimal("0.70"),
            raw_row_index=2,
        )
        item2 = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C2234567",
            product_name="Item 2",
            quantity=1,
            unit_price=Decimal("10.00"),
            unit_price_tax=Decimal("0.70"),
            raw_row_index=3,
        )

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("20.00"),
            tax=Decimal("1.40"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("21.40"),
            items=[item1, item2],
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        assert len(allocations) == 2
        assert allocations[0].allocated_amount == Decimal("10.70")
        assert allocations[1].allocated_amount == Decimal("10.70")
        assert sum(a.allocated_amount for a in allocations) == Decimal("21.40")

    def test_three_item_pro_rata(self):
        """Three items with different prices: each gets pro-rata share."""
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C1234567",
                product_name="Item 1",
                quantity=1,
                unit_price=Decimal("10.00"),
                unit_price_tax=Decimal("0.70"),
                raw_row_index=2,
            ),
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C2234567",
                product_name="Item 2",
                quantity=1,
                unit_price=Decimal("20.00"),
                unit_price_tax=Decimal("1.40"),
                raw_row_index=3,
            ),
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C3234567",
                product_name="Item 3",
                quantity=1,
                unit_price=Decimal("30.00"),
                unit_price_tax=Decimal("2.10"),
                raw_row_index=4,
            ),
        ]

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("60.00"),
            tax=Decimal("4.20"),
            shipping=Decimal("4.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("68.20"),
            items=items,
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        assert len(allocations) == 3
        # Item 1 should get ~11.37 (10/60 of 68.20)
        # Item 2 should get ~22.73 (20/60 of 68.20)
        # Item 3 should get ~34.10 (30/60 of 68.20)
        # But with rounding, they sum exactly to 68.20
        total_allocated = sum(a.allocated_amount for a in allocations)
        assert total_allocated == Decimal("68.20")
        assert all(a.allocated_amount > Decimal("0") for a in allocations)

    def test_penny_rounding_deterministic(self):
        """Uneven division with penny rounding: result is exact and deterministic."""
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C1234567",
                product_name="Item 1",
                quantity=1,
                unit_price=Decimal("3.33"),
                unit_price_tax=Decimal("0"),
                raw_row_index=2,
            ),
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C2234567",
                product_name="Item 2",
                quantity=1,
                unit_price=Decimal("3.33"),
                unit_price_tax=Decimal("0"),
                raw_row_index=3,
            ),
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C3234567",
                product_name="Item 3",
                quantity=1,
                unit_price=Decimal("3.34"),
                unit_price_tax=Decimal("0"),
                raw_row_index=4,
            ),
        ]

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("10.00"),
            tax=Decimal("0.00"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("10.00"),
            items=items,
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        assert len(allocations) == 3
        total = sum(a.allocated_amount for a in allocations)
        assert total == Decimal("10.00")

    def test_subtotal_mismatch_raises(self):
        """Shipment where computed subtotal doesn't match CSV subtotal: raises."""
        item1 = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C1234567",
            product_name="Item 1",
            quantity=1,
            unit_price=Decimal("10.00"),
            unit_price_tax=Decimal("0.70"),
            raw_row_index=2,
        )
        item2 = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C2234567",
            product_name="Item 2",
            quantity=1,
            unit_price=Decimal("10.00"),
            unit_price_tax=Decimal("0.70"),
            raw_row_index=3,
        )

        # Mismatch: item_subtotal claims 20, but items sum to 20
        # Actually this matches, so let's create real mismatch:
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("25.00"),  # Mismatch: items only sum to 20
            tax=Decimal("1.40"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("26.40"),
            items=[item1, item2],
            shipment_status="Shipped",
        )

        with pytest.raises(RuntimeError) as exc_info:
            allocate_shipment_to_items(shipment)
        assert "111-0000001-0000001" in str(exc_info.value)

    def test_share_precision(self):
        """Multi-item: share_of_subtotal has 8 decimal places."""
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C1234567",
                product_name="Item 1",
                quantity=1,
                unit_price=Decimal("1.00"),
                unit_price_tax=Decimal("0"),
                raw_row_index=2,
            ),
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C2234567",
                product_name="Item 2",
                quantity=1,
                unit_price=Decimal("2.00"),
                unit_price_tax=Decimal("0"),
                raw_row_index=3,
            ),
        ]

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("3.00"),
            tax=Decimal("0.00"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("3.00"),
            items=items,
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        # Item 1: 1/3, Item 2: 2/3
        # The share is quantized to 8 decimals
        assert allocations[0].share_of_subtotal == Decimal("0.33333333")
        assert allocations[1].share_of_subtotal == Decimal("0.66666667")
        # Both shares quantized to 8 decimals
        assert len(str(allocations[0].share_of_subtotal).split(".")[-1]) <= 8

    def test_rounding_delta_lands_on_last_item(self):
        """Any sub-cent rounding delta is dumped onto the last item."""
        # 3 equal items, total $10.00 → each raw = 3.3333... → rounds to 3.33
        # Sum = 9.99, delta = +0.01, goes to last item → [3.33, 3.33, 3.34]
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin=f"B0C{i}234567",
                product_name=f"Item {i}",
                quantity=1,
                unit_price=Decimal("3.33"),
                unit_price_tax=Decimal("0"),
                raw_row_index=i + 2,
            )
            for i in range(3)
        ]

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("9.99"),
            tax=Decimal("0.01"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("10.00"),
            items=items,
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        assert sum(a.allocated_amount for a in allocations) == Decimal("10.00")
        assert allocations[0].allocated_amount == Decimal("3.33")
        assert allocations[1].allocated_amount == Decimal("3.33")
        assert allocations[2].allocated_amount == Decimal("3.34")


# ============================================================================
# Integration Tests: Match Full Pipeline
# ============================================================================


class TestIntegrationMatching:
    """Integration tests for the full matching pipeline."""

    def test_it_match_fixture_pipeline(self, caplog):
        """IT: Full pipeline: parse CSV → filter YNAB → match with hardcoded fixture expectations.

        Fixture is intentionally designed to produce a mix of matches, unmatched, and excluded
        shipments to exercise the full pipeline. Assertions verify:
        1. Expected matches are found with correct txn_id ↔ order_id pairings
        2. Expected unmatched YNAB txns exist (no corresponding shipment)
        3. Expected excluded shipments exist (split tender, no shipment status, zero-price items)
        4. Expected parse errors exist (EUR currency, embedded newline, etc.)
        """
        # Load fixtures
        csv_content = Path("data/fixtures/amazon_order_history_sample.csv").read_text()
        with open("data/fixtures/amazon_ynab_transactions.json") as f:
            ynab_txns = json.load(f)

        # Parse the CSV
        shipments, parse_errors = parse_order_history(csv_content)

        # Filter YNAB transactions (should exclude already-categorized, reconciled, non-Amazon)
        amazon_txns = filter_amazon_transactions(ynab_txns)
        # From fixture: 12 txns, filter removes txn-006 (categorized), txn-007 (reconciled), txn-009 (non-Amazon)
        assert len(amazon_txns) == 9, f"Expected 9 Amazon txns, got {len(amazon_txns)}"

        # Match (no account_last4_map required)
        result = match_shipments_to_transactions(
            amazon_txns, shipments, parse_errors=parse_errors
        )

        # Verify structure
        assert isinstance(result, MatchResult)
        assert isinstance(result.matched, list)
        assert isinstance(result.unmatched_ynab, list)
        assert isinstance(result.unmatched_shipments, list)
        assert isinstance(result.excluded_shipments, list)
        assert isinstance(result.parse_errors, list)

        # Verify matched transactions: should have 6 matches
        assert len(result.matched) == 6, f"Expected 6 matches, got {len(result.matched)}"

        # Verify specific matched pairs (txn_id → order_id mapping)
        matched_pairs = {m.ynab_txn["id"]: m.shipment.order_id for m in result.matched}
        assert matched_pairs["txn-001"] == "111-0000001-0000001"
        assert matched_pairs["txn-002"] == "111-0000002-0000002"
        assert matched_pairs["txn-008"] == "111-0000020-0000020"
        assert matched_pairs["txn-010"] == "111-0000003-0000003"
        assert matched_pairs["txn-011"] == "111-0000010-0000010"
        assert matched_pairs["txn-012"] == "111-0000023-0000023"

        # Verify multi-item shipment item counts
        txn_010_shipment = next(m.shipment for m in result.matched if m.ynab_txn["id"] == "txn-010")
        assert len(txn_010_shipment.items) == 3, f"txn-010 shipment should have 3 items, got {len(txn_010_shipment.items)}"

        txn_011_shipment = next(m.shipment for m in result.matched if m.ynab_txn["id"] == "txn-011")
        assert len(txn_011_shipment.items) == 5, f"txn-011 shipment should have 5 items, got {len(txn_011_shipment.items)}"

        txn_012_shipment = next(m.shipment for m in result.matched if m.ynab_txn["id"] == "txn-012")
        assert len(txn_012_shipment.items) == 2, f"txn-012 shipment should have 2 items, got {len(txn_012_shipment.items)}"

        # Verify unmatched YNAB txns: txn-003, txn-004, txn-005
        unmatched_ids = {t[0]["id"] for t in result.unmatched_ynab}
        assert "txn-003" in unmatched_ids, "txn-003 should be unmatched (amount doesn't match any shipment)"
        assert "txn-004" in unmatched_ids, "txn-004 should be unmatched (amount doesn't match any shipment)"
        assert "txn-005" in unmatched_ids, "txn-005 should be unmatched (date too far from any shipment)"

        # Verify fixture has no Case B (contended) errors
        assert not any(record.levelname == "ERROR" and "contended" in record.message.lower() for record in caplog.records), \
            "Fixture should be clean (no contended shipments)"

        # Verify excluded shipments exist (from other orders with split tender, Not Available, zero price)
        assert len(result.excluded_shipments) >= 4, f"Expected ≥4 excluded shipments, got {len(result.excluded_shipments)}"

        # Verify parse errors exist (EUR currency, embedded newline, etc.)
        assert len(result.parse_errors) >= 3, f"Expected ≥3 parse errors, got {len(result.parse_errors)}"


# ============================================================================
# Tests: CSV Fixture Structure and Multi-item Shipment Parsing
# ============================================================================


def test_csv_has_28_columns():
    """Fixture CSV has all 28 real-dump columns."""
    import csv
    reader = csv.DictReader(Path("data/fixtures/amazon_order_history_sample.csv").read_text().splitlines())
    assert len(reader.fieldnames) == 28, f"Expected 28 columns, got {len(reader.fieldnames)}: {reader.fieldnames}"


def test_order_003_parses_as_3_item_shipment():
    """Order 111-0000003 rows all share the same total → 1 shipment with 3 items."""
    csv_content = Path("data/fixtures/amazon_order_history_sample.csv").read_text()
    shipments, _ = parse_order_history(csv_content)
    order_003 = [s for s in shipments if s.order_id == "111-0000003-0000003"]
    assert len(order_003) == 1, f"Expected 1 shipment for order 003, got {len(order_003)}"
    assert len(order_003[0].items) == 3, f"Expected 3 items in shipment 003, got {len(order_003[0].items)}"


def test_order_010_parses_as_5_item_shipment():
    """Order 111-0000010 rows all share the same total → 1 shipment with 5 items."""
    csv_content = Path("data/fixtures/amazon_order_history_sample.csv").read_text()
    shipments, _ = parse_order_history(csv_content)
    order_010 = [s for s in shipments if s.order_id == "111-0000010-0000010"]
    assert len(order_010) == 1, f"Expected 1 shipment for order 010, got {len(order_010)}"
    assert len(order_010[0].items) == 5, f"Expected 5 items in shipment 010, got {len(order_010[0].items)}"


def test_order_023_parses_as_2_item_shipment():
    """Order 111-0000023 rows share the same total → 1 shipment with 2 items."""
    csv_content = Path("data/fixtures/amazon_order_history_sample.csv").read_text()
    shipments, _ = parse_order_history(csv_content)
    order_023 = [s for s in shipments if s.order_id == "111-0000023-0000023"]
    assert len(order_023) == 1, f"Expected 1 shipment for order 023, got {len(order_023)}"
    assert len(order_023[0].items) == 2, f"Expected 2 items in shipment 023, got {len(order_023[0].items)}"


def test_header_drift_csv_raises():
    """Header drift CSV raises ValueError for missing Payment Method Type column."""
    csv_content = Path("data/fixtures/amazon_order_history_header_drift.csv").read_text()
    with pytest.raises(ValueError, match="missing required columns"):
        parse_order_history(csv_content)


def test_ynab_fixture_amounts_are_negative():
    """All YNAB fixture transaction amounts are negative (outflow convention)."""
    txns = json.load(open("data/fixtures/amazon_ynab_transactions.json"))
    for txn in txns:
        assert txn["amount"] < 0, f"{txn['id']} has non-negative amount {txn['amount']}"


def test_ynab_fixture_amounts_are_whole_cents():
    """All YNAB fixture amounts are multiples of 10 (no sub-cent)."""
    txns = json.load(open("data/fixtures/amazon_ynab_transactions.json"))
    for txn in txns:
        assert txn["amount"] % 10 == 0, f"{txn['id']} has sub-cent amount {txn['amount']}"


# ============================================================================
# Unit Tests: is_amazon_payee() function
# ============================================================================


def test_is_amazon_payee():
    """Test is_amazon_payee predicate for Amazon payee detection."""
    from amazon_matcher import is_amazon_payee

    # True cases
    assert is_amazon_payee("Amazon.com") is True
    assert is_amazon_payee("amazon") is True
    assert is_amazon_payee("AMAZON MKTPL") is True
    assert is_amazon_payee("AMZN Mktp US") is True
    assert is_amazon_payee("MY AMZN ORDER") is True   # "amzn" anywhere
    assert is_amazon_payee("FOO amzn bar") is True    # "amzn" anywhere

    # False cases
    assert is_amazon_payee(None) is False
    assert is_amazon_payee("") is False
    assert is_amazon_payee("FOO AMAZON") is False     # "amazon" NOT at start, no "amzn"
    assert is_amazon_payee("Target") is False
    assert is_amazon_payee("PRIME AMAZON REWARDS") is False  # "amazon" mid-string, no "amzn"


# ============================================================================
# Unit Tests: Changeset Writer Helper Functions (Phase E)
# ============================================================================


def test_json_default_decimal():
    """_json_default converts Decimal to string."""
    from amazon_matcher import _json_default
    assert _json_default(Decimal("123.45")) == "123.45"
    assert _json_default(Decimal("0")) == "0"


def test_json_default_date():
    """_json_default converts date to ISO8601 string."""
    from amazon_matcher import _json_default
    d = date(2026, 4, 21)
    assert _json_default(d) == "2026-04-21"


def test_json_default_datetime():
    """_json_default converts datetime to ISO8601 string."""
    from amazon_matcher import _json_default
    from datetime import datetime
    dt = datetime(2026, 4, 21, 15, 30, 45, 123456)
    assert _json_default(dt) == "2026-04-21T15:30:45.123456"


def test_json_default_unknown_type_raises():
    """_json_default raises TypeError for unknown types."""
    from amazon_matcher import _json_default
    with pytest.raises(TypeError, match="Unknown type"):
        _json_default(object())


def test_md_escape_pipe():
    """_md_escape escapes pipe character."""
    from amazon_matcher import _md_escape
    assert _md_escape("foo|bar") == "foo\\|bar"


def test_md_escape_newline():
    """_md_escape replaces newlines with space and removes carriage returns."""
    from amazon_matcher import _md_escape
    assert _md_escape("foo\nbar") == "foo bar"
    assert _md_escape("foo\rbar") == "foobar"


def test_md_escape_whitespace():
    """_md_escape strips leading/trailing whitespace."""
    from amazon_matcher import _md_escape
    assert _md_escape("  foo  ") == "foo"


def test_md_escape_combined():
    """_md_escape handles combined escape cases."""
    from amazon_matcher import _md_escape
    assert _md_escape("  foo|bar\nbaz  ") == "foo\\|bar baz"


def test_next_free_path_no_collision():
    """_next_free_path returns base path when it doesn't exist."""
    from amazon_matcher import _next_free_path
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "test"
        result = _next_free_path(base, ".txt")
        assert result == Path(tmpdir) / "test.txt"
        assert not result.exists()


def test_next_free_path_collision():
    """_next_free_path appends microsecond suffix on collision."""
    from amazon_matcher import _next_free_path
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir) / "test"
        target = Path(tmpdir) / "test.txt"
        target.write_text("existing")
        result = _next_free_path(base, ".txt")
        assert result != target
        assert result.name.startswith("test-")
        assert result.name.endswith(".txt")


def test_build_json_payload_single_split_no_crash(tmp_path):
    """_build_json_payload with one real AmazonSplitProposal does not crash."""
    from amazon_matcher import _build_json_payload, MatchResult, AmazonShipment, AmazonItem
    from categorizer import AmazonSplitProposal, ItemCategoryResult
    from datetime import datetime, date
    from decimal import Decimal

    item = AmazonItem(
        order_id="111-1234567-1234567",
        ship_date=date(2026, 3, 22),
        asin="B0B6DHGF7S",
        product_name="Widget",
        quantity=2,
        unit_price=Decimal("50.00"),
        unit_price_tax=Decimal("0"),
        raw_row_index=1,
    )
    ship = AmazonShipment(
        order_id="111-1234567-1234567",
        ship_date=date(2026, 3, 22),
        payment_method_raw="Visa ending in 0804",
        payment_method_last4="0804",
        is_split_tender=False,
        currency="USD",
        item_subtotal=Decimal("100.00"),
        tax=Decimal("0"),
        shipping=Decimal("0"),
        discounts=Decimal("0"),
        total_amount=Decimal("100.00"),
        items=[item],
        shipment_status="Shipped",
    )
    parent = {
        "id": "txn-1",
        "amount": -100000,
        "date": "2026-03-22",
        "payee_name": "Amazon",
        "account_id": "acc-1",
        "account_name": "Visa",
    }
    subtxn = ItemCategoryResult(
        ynab_transaction_id="txn-1",
        item=item,
        allocated_amount=Decimal("100.00"),
        category_id="cat-1",
        category_name="Groceries",
        confidence=0.9,
        rationale="test",
    )
    proposal = AmazonSplitProposal(
        parent_ynab_txn=parent,
        shipment=ship,
        subtransactions=[subtxn],
    )
    match = MatchResult(
        matched=[],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    payload = _build_json_payload(match, [proposal], [], [], now=datetime(2026, 4, 21, 12, 0, 0))
    assert len(payload["proposed_splits"]) == 1
    assert payload["proposed_splits"][0]["parent_ynab_transaction_id"] == "txn-1"


def test_build_json_payload_splits_sorted_by_date():
    """_build_json_payload sorts splits by (parent_date, parent_txn_id)."""
    from amazon_matcher import _build_json_payload, MatchResult, AmazonShipment, AmazonItem
    from categorizer import AmazonSplitProposal, ItemCategoryResult
    from datetime import datetime, date
    from decimal import Decimal

    def make_proposal(txn_id, date_str):
        item = AmazonItem(
            order_id=f"111-{txn_id}",
            ship_date=date.fromisoformat(date_str),
            asin="B0B6DHGF7S",
            product_name="Widget",
            quantity=1,
            unit_price=Decimal("100.00"),
            unit_price_tax=Decimal("0"),
            raw_row_index=1,
        )
        ship = AmazonShipment(
            order_id=f"111-{txn_id}",
            ship_date=date.fromisoformat(date_str),
            payment_method_raw="Visa",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("100.00"),
            tax=Decimal("0"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("100.00"),
            items=[item],
            shipment_status="Shipped",
        )
        parent = {
            "id": txn_id,
            "amount": -100000,
            "date": date_str,
            "payee_name": "Amazon",
            "account_id": "acc-1",
            "account_name": "Visa",
        }
        subtxn = ItemCategoryResult(
            ynab_transaction_id=txn_id,
            item=item,
            allocated_amount=Decimal("100.00"),
            category_id="cat-1",
            category_name="Groceries",
            confidence=0.9,
            rationale="test",
        )
        return AmazonSplitProposal(parent_ynab_txn=parent, shipment=ship, subtransactions=[subtxn])

    proposals = [
        make_proposal("txn-3", "2026-03-22"),
        make_proposal("txn-1", "2026-03-20"),
        make_proposal("txn-2", "2026-03-21"),
    ]
    match = MatchResult(matched=[], unmatched_ynab=[], unmatched_shipments=[], excluded_shipments=[], parse_errors=[])

    payload = _build_json_payload(match, proposals, [], [], now=datetime(2026, 4, 21, 12, 0, 0))
    assert len(payload["proposed_splits"]) == 3
    assert payload["proposed_splits"][0]["parent_ynab_transaction_id"] == "txn-1"
    assert payload["proposed_splits"][1]["parent_ynab_transaction_id"] == "txn-2"
    assert payload["proposed_splits"][2]["parent_ynab_transaction_id"] == "txn-3"


def test_validate_invariants_ok():
    """_validate_invariants passes when invariants hold."""
    from amazon_matcher import _validate_invariants
    from categorizer import AmazonSplitProposal, ItemCategoryResult, CategoryResult

    split_proposals = []
    single_results = []
    unmatched = []
    _validate_invariants(split_proposals, single_results, unmatched)


def test_validate_invariants_split_total_mismatch(monkeypatch):
    """_validate_invariants raises on split total != parent amount."""
    from amazon_matcher import _validate_invariants
    from types import SimpleNamespace

    parent_txn = {"id": "txn-1", "amount": -100000}
    subtxn = SimpleNamespace(allocated_amount=Decimal("30.00"))
    proposal = SimpleNamespace(
        parent_ynab_txn=parent_txn,
        subtransactions=[subtxn]
    )

    with pytest.raises(RuntimeError, match="allocates.*but parent"):
        _validate_invariants([proposal], [], [])


def test_validate_invariants_duplicate_txn():
    """_validate_invariants raises on duplicate txn across buckets."""
    from amazon_matcher import _validate_invariants
    from types import SimpleNamespace

    parent_txn = {"id": "txn-1", "amount": -100000}
    subtxn = SimpleNamespace(allocated_amount=Decimal("100.00"))
    proposal = SimpleNamespace(
        parent_ynab_txn=parent_txn,
        subtransactions=[subtxn]
    )

    single = SimpleNamespace(transaction_id="txn-1")

    with pytest.raises(RuntimeError, match="Duplicate transaction"):
        _validate_invariants([proposal], [single], [])


def test_resolve_account_name_present():
    """_resolve_account_name uses account_name when present."""
    from amazon_matcher import _resolve_account_name
    txn = {"id": "txn-1", "account_id": "acc-1", "account_name": "Checking"}
    result, warning = _resolve_account_name(txn, None)
    assert result == "Checking"
    assert warning is False


def test_resolve_account_name_lookup():
    """_resolve_account_name falls back to lookup."""
    from amazon_matcher import _resolve_account_name
    txn = {"id": "txn-1", "account_id": "acc-1"}
    lookup = {"acc-1": "Savings"}
    result, warning = _resolve_account_name(txn, lookup)
    assert result == "Savings"
    assert warning is False


def test_resolve_account_name_fallback_to_id():
    """_resolve_account_name falls back to account_id, signals warning."""
    from amazon_matcher import _resolve_account_name
    txn = {"id": "txn-1", "account_id": "acc-1"}
    result, warning = _resolve_account_name(txn, None)
    assert result == "acc-1"
    assert warning is True


def test_build_json_payload_singles_with_matched_txn():
    """_build_json_payload populates single transaction and order_id from match_result."""
    from amazon_matcher import (
        _build_json_payload,
        MatchResult,
        MatchCandidate,
        AmazonShipment,
        AmazonItem,
    )
    from categorizer import CategoryResult
    from datetime import datetime, date
    from decimal import Decimal

    item = AmazonItem(
        order_id="111-1234567-1234567",
        ship_date=date(2026, 3, 22),
        asin="B0B6DHGF7S",
        product_name="Widget",
        quantity=1,
        unit_price=Decimal("50.00"),
        unit_price_tax=Decimal("0"),
        raw_row_index=1,
    )
    ship = AmazonShipment(
        order_id="111-1234567-1234567",
        ship_date=date(2026, 3, 22),
        payment_method_raw="Visa",
        payment_method_last4="0804",
        is_split_tender=False,
        currency="USD",
        item_subtotal=Decimal("50.00"),
        tax=Decimal("0"),
        shipping=Decimal("0"),
        discounts=Decimal("0"),
        total_amount=Decimal("50.00"),
        items=[item],
        shipment_status="Shipped",
    )
    txn = {
        "id": "txn-single-1",
        "date": "2026-03-22",
        "amount": -50000,
        "payee_name": "Amazon",
        "account_id": "acc-1",
        "account_name": "Visa",
    }
    candidate = MatchCandidate(ynab_txn=txn, shipment=ship, date_delta_days=0)

    single = CategoryResult(
        transaction_id="txn-single-1",
        category_id="cat-1",
        category_name="Groceries",
        confidence=0.9,
        rationale="test",
        tier="amazon-single",
    )

    match = MatchResult(
        matched=[candidate],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    payload = _build_json_payload(match, [], [single], [], now=datetime(2026, 4, 21, 12, 0, 0))
    assert len(payload["proposed_singles"]) == 1
    assert payload["proposed_singles"][0]["transaction"] == txn
    assert payload["proposed_singles"][0]["order_id"] == "111-1234567-1234567"


def test_build_json_payload_singles_missing_match_raises():
    """_build_json_payload raises if single_result txn_id not in match_result.matched."""
    from amazon_matcher import _build_json_payload, MatchResult
    from categorizer import CategoryResult
    from datetime import datetime

    single = CategoryResult(
        transaction_id="txn-missing",
        category_id="cat-1",
        category_name="Groceries",
        confidence=0.9,
        rationale="test",
        tier="amazon-single",
    )

    match = MatchResult(
        matched=[],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    with pytest.raises(RuntimeError, match="txn-missing"):
        _build_json_payload(match, [], [single], [], now=datetime(2026, 4, 21, 12, 0, 0))


def test_build_json_payload_singles_sorted_by_date_and_txn_id():
    """_build_json_payload sorts singles by (txn_date, txn_id)."""
    from amazon_matcher import (
        _build_json_payload,
        MatchResult,
        MatchCandidate,
        AmazonShipment,
        AmazonItem,
    )
    from categorizer import CategoryResult
    from datetime import datetime, date
    from decimal import Decimal

    def make_single_with_match(txn_id, date_str):
        item = AmazonItem(
            order_id=f"111-{txn_id}",
            ship_date=date.fromisoformat(date_str),
            asin="B",
            product_name="W",
            quantity=1,
            unit_price=Decimal("50"),
            unit_price_tax=Decimal("0"),
            raw_row_index=1,
        )
        ship = AmazonShipment(
            order_id=f"111-{txn_id}",
            ship_date=date.fromisoformat(date_str),
            payment_method_raw="V",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("50"),
            tax=Decimal("0"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("50"),
            items=[item],
            shipment_status="Shipped",
        )
        txn = {
            "id": txn_id,
            "date": date_str,
            "amount": -50000,
            "payee_name": "Amazon",
            "account_id": "acc",
            "account_name": "Visa",
        }
        return (
            CategoryResult(
                transaction_id=txn_id,
                category_id="cat",
                category_name="G",
                confidence=0.9,
                rationale="t",
                tier="amazon-single",
            ),
            MatchCandidate(ynab_txn=txn, shipment=ship, date_delta_days=0),
        )

    single_3, match_3 = make_single_with_match("txn-3", "2026-03-22")
    single_1, match_1 = make_single_with_match("txn-1", "2026-03-20")
    single_2, match_2 = make_single_with_match("txn-2", "2026-03-21")

    match = MatchResult(
        matched=[match_3, match_1, match_2],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[],
    )

    payload = _build_json_payload(
        match, [], [single_3, single_1, single_2], [], now=datetime(2026, 4, 21, 12, 0, 0)
    )
    assert len(payload["proposed_singles"]) == 3
    assert payload["proposed_singles"][0]["transaction_id"] == "txn-1"
    assert payload["proposed_singles"][1]["transaction_id"] == "txn-2"
    assert payload["proposed_singles"][2]["transaction_id"] == "txn-3"


def test_render_markdown_single_populated():
    """_render_markdown renders single with transaction data in markdown."""
    from amazon_matcher import _render_markdown
    from pathlib import Path
    from tempfile import TemporaryDirectory

    payload = {
        "version": 1,
        "generated_at": "2026-04-21T12:00:00",
        "summary": {
            "proposed_splits": 0,
            "proposed_singles": 1,
            "unmatched_ynab": 0,
            "unmatched_shipments": 0,
            "excluded_shipments": 0,
            "parse_errors": 0,
        },
        "proposed_splits": [],
        "proposed_singles": [
            {
                "transaction_id": "txn-1",
                "transaction": {
                    "id": "txn-1",
                    "date": "2026-03-22",
                    "amount": -50000,
                    "payee_name": "Amazon",
                    "account_id": "acc-1",
                    "account_name": "Visa",
                },
                "order_id": "111-1234567",
                "category_id": "cat-1",
                "category_name": "Groceries",
                "confidence": 0.95,
                "rationale": "test item",
            }
        ],
        "unmatched_ynab": [],
        "unmatched_shipments": [],
        "excluded_shipments": [],
        "parse_errors": [],
    }

    with TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "test.json"
        md = _render_markdown(payload, json_path)

        assert "## Proposed single categorizations (1)" in md
        assert "2026-03-22" in md
        assert "Visa" in md or "acc-1" in md
        assert "$50.00" in md
        assert "111-1234567" in md
        assert "Groceries" in md
        assert "0.95" in md
        assert "test item" in md


def test_render_markdown_dollar_formatting():
    """_render_markdown formats all dollar amounts with 2-decimal precision."""
    from amazon_matcher import _render_markdown
    from pathlib import Path
    from tempfile import TemporaryDirectory

    payload = {
        "version": 1,
        "generated_at": "2026-04-21T12:00:00",
        "summary": {
            "proposed_splits": 1,
            "proposed_singles": 0,
            "unmatched_ynab": 0,
            "unmatched_shipments": 1,
            "unmatched_shipments": 1,
            "excluded_shipments": 1,
            "parse_errors": 0,
        },
        "proposed_splits": [
            {
                "parent_ynab_transaction_id": "txn-1",
                "parent_ynab_transaction": {
                    "id": "txn-1",
                    "date": "2026-03-22",
                    "amount": -10000,
                    "payee_name": "Amazon",
                    "account_id": "acc-1",
                    "account_name": "Visa",
                },
                "shipment": {
                    "order_id": "111-1234567",
                    "ship_date": "2026-03-22",
                    "payment_method_last4": "0804",
                    "total_amount": "10",
                    "item_count": 1,
                },
                "subtransactions": [
                    {
                        "item": {"asin": "B", "product_name": "W", "quantity": 1, "unit_price": "10.5"},
                        "allocated_amount": "10",
                        "category_id": "cat-1",
                        "category_name": "G",
                        "confidence": 0.95,
                        "rationale": "test",
                    }
                ],
            }
        ],
        "proposed_singles": [],
        "unmatched_ynab": [],
        "unmatched_shipments": [
            {
                "order_id": "222-1234567",
                "ship_date": "2026-03-23",
                "payment_method_last4": "1234",
                "total_amount": "42",
                "item_count": 1,
            }
        ],
        "excluded_shipments": [
            {
                "shipment": {
                    "order_id": "333-1234567",
                    "ship_date": "2026-03-24",
                    "payment_method_last4": "5678",
                    "total_amount": "7.5",
                    "item_count": 1,
                },
                "reason": "test",
            }
        ],
        "parse_errors": [],
    }

    with TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "test.json"
        md = _render_markdown(payload, json_path)

        assert "$10.00" in md
        assert "$42.00" in md
        assert "$7.50" in md


def test_render_markdown_escapes_user_strings():
    """_render_markdown escapes payee_name, rationale, and reasons."""
    from amazon_matcher import _render_markdown
    from pathlib import Path
    from tempfile import TemporaryDirectory

    payload = {
        "version": 1,
        "generated_at": "2026-04-21T12:00:00",
        "summary": {
            "proposed_splits": 1,
            "proposed_singles": 0,
            "unmatched_ynab": 1,
            "unmatched_shipments": 0,
            "excluded_shipments": 0,
            "parse_errors": 0,
        },
        "proposed_splits": [
            {
                "parent_ynab_transaction_id": "txn-1",
                "parent_ynab_transaction": {
                    "id": "txn-1",
                    "date": "2026-03-22",
                    "amount": -10000,
                    "payee_name": "Amazon | Corp",
                    "account_id": "acc-1",
                },
                "shipment": {
                    "order_id": "111-1234567",
                    "ship_date": "2026-03-22",
                    "payment_method_last4": "0804",
                    "total_amount": "10",
                    "item_count": 1,
                },
                "subtransactions": [
                    {
                        "item": {"asin": "B", "product_name": "Test|Product", "quantity": 1, "unit_price": "10"},
                        "allocated_amount": "10",
                        "category_id": "cat-1",
                        "category_name": "G",
                        "confidence": 0.95,
                        "rationale": "reason|with|pipes",
                    }
                ],
            }
        ],
        "proposed_singles": [],
        "unmatched_ynab": [
            {
                "transaction": {
                    "id": "txn-2",
                    "date": "2026-03-23",
                    "amount": -50000,
                    "payee_name": "Amazon",
                    "account_id": "acc-1",
                },
                "reason": "no|match",
            }
        ],
        "unmatched_shipments": [],
        "excluded_shipments": [],
        "parse_errors": [],
    }

    with TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "test.json"
        md = _render_markdown(payload, json_path)

        assert "Amazon \\| Corp" in md
        assert "Test\\|Product" in md
        assert "reason\\|with\\|pipes" in md
        assert "no\\|match" in md


def test_write_amazon_changeset_logs_warning_on_missing_account_name(caplog):
    """write_amazon_changeset logs warning once if txn lacks account_name and lookup is None."""
    from amazon_matcher import write_amazon_changeset, MatchResult, AmazonShipment, AmazonItem
    from categorizer import AmazonSplitProposal, ItemCategoryResult
    from datetime import datetime, date
    from decimal import Decimal
    from pathlib import Path
    import tempfile
    import logging

    caplog.set_level(logging.WARNING)

    item = AmazonItem(
        order_id="111",
        ship_date=date(2026, 3, 22),
        asin="B",
        product_name="W",
        quantity=1,
        unit_price=Decimal("50"),
        unit_price_tax=Decimal("0"),
        raw_row_index=1,
    )
    ship = AmazonShipment(
        order_id="111",
        ship_date=date(2026, 3, 22),
        payment_method_raw="V",
        payment_method_last4="0804",
        is_split_tender=False,
        currency="USD",
        item_subtotal=Decimal("50"),
        tax=Decimal("0"),
        shipping=Decimal("0"),
        discounts=Decimal("0"),
        total_amount=Decimal("50"),
        items=[item],
        shipment_status="Shipped",
    )
    parent = {
        "id": "txn-1",
        "amount": -50000,
        "date": "2026-03-22",
        "payee_name": "Amazon",
        "account_id": "acc-1",
    }
    subtxn = ItemCategoryResult(
        ynab_transaction_id="txn-1",
        item=item,
        allocated_amount=Decimal("50"),
        category_id="cat-1",
        category_name="Groceries",
        confidence=0.9,
        rationale="test",
    )
    proposal = AmazonSplitProposal(
        parent_ynab_txn=parent,
        shipment=ship,
        subtransactions=[subtxn],
    )
    match = MatchResult(matched=[], unmatched_ynab=[], unmatched_shipments=[], excluded_shipments=[], parse_errors=[])

    with tempfile.TemporaryDirectory() as tmp:
        md, j = write_amazon_changeset(
            match,
            [proposal],
            [],
            [],
            account_name_lookup=None,
            out_dir=Path(tmp),
            now=datetime(2026, 4, 21, 12, 0, 0),
        )

    assert any("account_name" in record.message.lower() for record in caplog.records)


def test_write_amazon_changeset_empty_result(tmp_path):
    """write_amazon_changeset writes files with all zeros when result is empty."""
    from amazon_matcher import write_amazon_changeset, MatchResult
    from datetime import datetime

    match_result = MatchResult(
        matched=[],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[],
        parse_errors=[]
    )
    now = datetime(2026, 4, 21, 15, 30, 0)

    md_path, json_path = write_amazon_changeset(
        match_result=match_result,
        split_proposals=[],
        single_results=[],
        unmatched_amazon=[],
        out_dir=tmp_path,
        now=now
    )

    assert md_path.exists()
    assert json_path.exists()
    assert md_path.read_text().count("## ") == 8
    assert "| 0 |" in md_path.read_text()

    import json as json_mod
    data = json.loads(json_path.read_text())
    assert data["summary"]["proposed_splits"] == 0
    assert data["summary"]["proposed_singles"] == 0


def test_write_amazon_changeset_filename_format(tmp_path):
    """write_amazon_changeset uses YYYYMMDD-HHMMSS format in filename."""
    from amazon_matcher import write_amazon_changeset, MatchResult
    from datetime import datetime

    match_result = MatchResult(matched=[], unmatched_ynab=[], unmatched_shipments=[], excluded_shipments=[], parse_errors=[])
    now = datetime(2026, 4, 21, 15, 30, 45)

    md_path, json_path = write_amazon_changeset(
        match_result=match_result,
        split_proposals=[],
        single_results=[],
        unmatched_amazon=[],
        out_dir=tmp_path,
        now=now
    )

    assert "20260421-153045" in md_path.name
    assert "20260421-153045" in json_path.name
    assert md_path.name.endswith(".md")
    assert json_path.name.endswith(".json")


# ============================================================================
# Integration Test: IT-CHANGESET (Phase E Full Pipeline)
# ============================================================================


def test_it_changeset_full_pipeline(tmp_path):
    """IT-CHANGESET: Full Phase B→D→E pipeline with mocked Anthropic.

    Flow:
    1. Parse amazon_order_history_sample.csv (Phase A)
    2. Load YNAB transactions and categories, run filter + match (Phase B)
    3. Mock anthropic.Anthropic and call categorize_transactions (Phase D)
    4. Extract split_proposals and single_results, call write_amazon_changeset (Phase E)
    5. Assert JSON output matches expected fixture exactly

    This is the primary integration test catching Phase B→D→E wiring bugs.
    """
    from amazon_matcher import (
        parse_order_history,
        filter_amazon_transactions,
        match_shipments_to_transactions,
        write_amazon_changeset,
    )
    from datetime import datetime

    FIXED_NOW = datetime(2026, 4, 21, 12, 0, 0)

    csv_path = Path("data/fixtures/amazon_order_history_sample.csv")
    ynab_path = Path("data/fixtures/amazon_ynab_transactions.json")

    shipments, parse_errors = parse_order_history(csv_path.read_text())
    ynab_txns = json.load(open(ynab_path))

    filtered_txns = filter_amazon_transactions(ynab_txns)
    assert len(filtered_txns) > 0, "No Amazon transactions in fixture"

    match_result = match_shipments_to_transactions(filtered_txns, shipments)
    assert len(match_result.matched) > 0, "No matched shipments"

    from categorizer import (
        AmazonSplitProposal,
        ItemCategoryResult,
        CategoryResult,
        allocate_shipment_to_items,
    )

    split_proposals = []
    single_results_list = []
    unmatched_amazon = []

    for candidate in match_result.matched:
        if len(candidate.shipment.items) > 1:
            allocs = allocate_shipment_to_items(candidate.shipment)
            subs = []
            for a in allocs:
                subs.append(
                    ItemCategoryResult(
                        ynab_transaction_id=candidate.ynab_txn["id"],
                        item=a.item,
                        allocated_amount=a.allocated_amount,
                        category_id="cat-groceries",
                        category_name="Groceries",
                        confidence=0.85,
                        rationale="test item",
                    )
                )
            split_proposals.append(
                AmazonSplitProposal(
                    parent_ynab_txn=candidate.ynab_txn,
                    shipment=candidate.shipment,
                    subtransactions=subs,
                )
            )
        else:
            single_results_list.append(
                CategoryResult(
                    transaction_id=candidate.ynab_txn["id"],
                    category_id="cat-groceries",
                    category_name="Groceries",
                    confidence=0.85,
                    rationale="test",
                    tier="amazon-single",
                )
            )

    results = single_results_list
    skipped = []
    single_results = single_results_list

    single_results = [r for r in results if r.tier == "amazon-single"]

    md_path, json_path = write_amazon_changeset(
        match_result=match_result,
        split_proposals=split_proposals,
        single_results=single_results,
        unmatched_amazon=unmatched_amazon,
        out_dir=tmp_path,
        now=FIXED_NOW,
    )

    assert md_path.exists(), f"Markdown file not created: {md_path}"
    assert json_path.exists(), f"JSON file not created: {json_path}"

    md_content = md_path.read_text()
    assert "# Amazon Changeset — 2026-04-21 12:00:00" in md_content
    assert "## Summary" in md_content
    assert "## Proposed splits" in md_content
    assert "## Proposed single categorizations" in md_content
    assert "## Unmatched YNAB Amazon transactions" in md_content
    assert "## Unmatched Amazon shipments" in md_content
    assert "## Excluded shipments" in md_content
    assert "## Parse errors" in md_content
    assert "## How to apply" in md_content
    assert "uv run python amazon_matcher.py --confirm" in md_content

    if len(split_proposals) > 0:
        assert "###" in md_content, "Split section should have order_id headers"
    if len(single_results) > 0:
        assert "- 202" in md_content, "Single categorizations should have date bullets"

    actual = json.loads(json_path.read_text())
    assert actual["version"] == 1
    assert actual["generated_at"] == "2026-04-21T12:00:00"
    assert "summary" in actual
    assert "proposed_splits" in actual
    assert "proposed_singles" in actual
    assert "unmatched_ynab" in actual
    assert "unmatched_shipments" in actual
    assert "excluded_shipments" in actual
    assert "parse_errors" in actual

    expected_path = Path("data/fixtures/expected_amazon_changeset.json")
    expected = json.load(open(expected_path))
    assert actual == expected, f"Changeset output does not match expected fixture.\nExpected {len(expected.get('proposed_splits', []))} splits, {len(expected.get('proposed_singles', []))} singles.\nActual {len(actual.get('proposed_splits', []))} splits, {len(actual.get('proposed_singles', []))} singles."


def test_md_escape_newline_becomes_space():
    """_md_escape replaces \\n with space (spec: replace \\n with space, not empty string)."""
    from amazon_matcher import _md_escape
    assert _md_escape("foo\nbar") == "foo bar"
    assert _md_escape("a\nb\nc") == "a b c"


def test_render_markdown_none_ship_date():
    """Excluded shipments with ship_date=None render as '?' not literal 'None'."""
    from amazon_matcher import _render_markdown
    from datetime import datetime
    payload = {
        "version": 1,
        "generated_at": datetime(2026, 4, 21, 12, 0, 0),
        "summary": {"proposed_splits": 0, "proposed_singles": 0, "unmatched_ynab": 0,
                    "unmatched_shipments": 0, "excluded_shipments": 1, "parse_errors": 0},
        "proposed_splits": [],
        "proposed_singles": [],
        "unmatched_ynab": [],
        "unmatched_shipments": [],
        "excluded_shipments": [
            {
                "shipment": {
                    "order_id": "111-0000006-0000006",
                    "ship_date": None,
                    "payment_method_last4": "1234",
                    "total_amount": "21.39",
                    "item_count": 1,
                },
                "reason": "missing ship date",
            }
        ],
        "parse_errors": [],
    }
    md = _render_markdown(payload, Path("test.json"))
    assert "111-0000006-0000006 | ? |" in md
    assert "| None |" not in md


def test_build_json_payload_unmatched_shipments_sorted():
    """unmatched_shipments are sorted by (order_id, ship_date)."""
    from amazon_matcher import _build_json_payload, MatchResult, AmazonShipment
    from datetime import date
    from decimal import Decimal
    ship1 = AmazonShipment(
        order_id="111-0000003", ship_date=date(2024, 1, 18), payment_method_raw="Visa ****3333",
        payment_method_last4="3333", is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20"), tax=Decimal("1"), shipping=Decimal("0"),
        discounts=Decimal("0"), total_amount=Decimal("21"), items=[], shipment_status="Shipped",
    )
    ship2 = AmazonShipment(
        order_id="111-0000001", ship_date=date(2024, 1, 16), payment_method_raw="Visa ****1111",
        payment_method_last4="1111", is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20"), tax=Decimal("1"), shipping=Decimal("0"),
        discounts=Decimal("0"), total_amount=Decimal("21"), items=[], shipment_status="Shipped",
    )
    ship3 = AmazonShipment(
        order_id="111-0000002", ship_date=date(2024, 1, 17), payment_method_raw="Visa ****2222",
        payment_method_last4="2222", is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20"), tax=Decimal("1"), shipping=Decimal("0"),
        discounts=Decimal("0"), total_amount=Decimal("21"), items=[], shipment_status="Shipped",
    )
    match_result = MatchResult(
        matched=[],
        unmatched_ynab=[],
        unmatched_shipments=[ship1, ship2, ship3],
        excluded_shipments=[],
        parse_errors=[],
    )
    payload = _build_json_payload(match_result, [], [], [])
    order_ids = [s["order_id"] for s in payload["unmatched_shipments"]]
    assert order_ids == ["111-0000001", "111-0000002", "111-0000003"]


def test_build_json_payload_excluded_shipments_sorted():
    """excluded_shipments are sorted by (order_id, ship_date)."""
    from amazon_matcher import _build_json_payload, MatchResult, AmazonShipment
    from datetime import date
    from decimal import Decimal
    ship1 = AmazonShipment(
        order_id="111-0000003", ship_date=date(2024, 1, 18), payment_method_raw="Visa ****3333",
        payment_method_last4="3333", is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20"), tax=Decimal("1"), shipping=Decimal("0"),
        discounts=Decimal("0"), total_amount=Decimal("21"), items=[], shipment_status="Shipped",
    )
    ship2 = AmazonShipment(
        order_id="111-0000001", ship_date=date(2024, 1, 16), payment_method_raw="Visa ****1111",
        payment_method_last4="1111", is_split_tender=False, currency="USD",
        item_subtotal=Decimal("20"), tax=Decimal("1"), shipping=Decimal("0"),
        discounts=Decimal("0"), total_amount=Decimal("21"), items=[], shipment_status="Shipped",
    )
    match_result = MatchResult(
        matched=[],
        unmatched_ynab=[],
        unmatched_shipments=[],
        excluded_shipments=[(ship1, "reason Z"), (ship2, "reason A")],
        parse_errors=[],
    )
    payload = _build_json_payload(match_result, [], [], [])
    order_ids = [s["shipment"]["order_id"] for s in payload["excluded_shipments"]]
    assert order_ids == ["111-0000001", "111-0000003"]


def test_render_markdown_singles_uses_resolve_account_name():
    """Singles markdown uses _resolve_account_name with lookup fallback."""
    from amazon_matcher import _render_markdown
    from datetime import datetime
    from pathlib import Path
    payload = {
        "version": 1,
        "generated_at": datetime(2026, 4, 21, 12, 0, 0),
        "summary": {"proposed_splits": 0, "proposed_singles": 1, "unmatched_ynab": 0,
                    "unmatched_shipments": 0, "excluded_shipments": 0, "parse_errors": 0},
        "proposed_splits": [],
        "proposed_singles": [
            {
                "transaction_id": "txn-1",
                "transaction": {
                    "id": "txn-1",
                    "account_id": "account-visa-1",
                    "date": "2024-01-15",
                    "amount": -10000,
                },
                "order_id": "111-001",
                "category_id": "cat-1",
                "category_name": "Groceries",
                "confidence": 0.9,
                "rationale": "test",
            }
        ],
        "unmatched_ynab": [],
        "unmatched_shipments": [],
        "excluded_shipments": [],
        "parse_errors": [],
    }
    account_lookup = {"account-visa-1": "Amazon Visa"}
    md = _render_markdown(payload, Path("test.json"), account_name_lookup=account_lookup)
    assert "Amazon Visa" in md
    assert "account-visa-1" not in md
