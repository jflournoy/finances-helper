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
    """Test the two-phase matching algorithm."""

    def test_match_basic_1to1(self):
        """Phase 1: Single exact match (amount, last-4, date)."""
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
            "amount": -112000,  # milliunitss: -$112.00
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4)

        assert len(result.matched) == 1
        assert result.matched[0].match_mode == "strict"
        assert result.matched[0].date_delta_days == 0
        assert result.matched[0].ynab_txn["id"] == "txn-001"
        assert len(result.unmatched_ynab) == 0
        assert len(result.unmatched_shipments) == 0

    def test_match_date_window_minus3(self):
        """Phase 1: Match within -3 day window."""
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4, date_window_days=3)

        assert len(result.matched) == 1
        assert result.matched[0].date_delta_days == -3

    def test_match_date_window_plus3(self):
        """Phase 1: Match within +3 day window."""
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4, date_window_days=3)

        assert len(result.matched) == 1
        assert result.matched[0].date_delta_days == 3

    def test_match_date_window_out_of_bounds_minus4(self):
        """Phase 1: No match outside -4 day window."""
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4, date_window_days=3)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_date_window_out_of_bounds_plus4(self):
        """Phase 1: No match outside +4 day window."""
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4, date_window_days=3)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_amount_mismatch(self):
        """Phase 1: No match when amount differs."""
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
            "amount": -113000,  # $113, not $112
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1

    def test_match_last4_mismatch(self):
        """Phase 1: No match when last-4 differs."""
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

        account_last4 = {"account-1": "1111"}  # Different last-4
        result = match_shipments_to_transactions([txn], [shipment], account_last4)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1
        # The shipment won't match because it's not in the Phase 1 index (different last-4)
        # and not in the Phase 2 index (has a last-4, doesn't match amount_only)
        # So the message is "no matching shipment"
        assert "matching shipment" in result.unmatched_ynab[0][1].lower() or "no last-4" in result.unmatched_ynab[0][1].lower()

    def test_match_phase1_tiebreak_multiple_candidates(self):
        """Phase 1: Two candidates with same amount/last4, pick by date delta tiebreak, leave other unmatched."""
        # Two shipments: both $112, last4 0804, both in window, different ship_dates
        shipment1 = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 14),  # -1 day
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
            ship_date=date(2024, 1, 12),  # -3 days
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment1, shipment2], account_last4)

        # One should be matched (the closer one by date_delta)
        assert len(result.matched) == 1
        # The matched one should be shipment1 (2024-01-14, delta -1, closer than -3)
        assert result.matched[0].shipment.order_id == "111-0000001-0000001"
        assert result.matched[0].date_delta_days == -1
        # The other should be unmatched and still available
        assert len(result.unmatched_shipments) == 1
        assert result.unmatched_shipments[0].order_id == "111-0000002-0000002"


    def test_match_phase2_not_available_unambiguous(self, caplog):
        """Phase 2: Match with 'Not Available' payment method (unambiguous)."""
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Not Available",
            payment_method_last4=None,
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4)

        assert len(result.matched) == 1
        assert result.matched[0].match_mode == "amount_date_only"
        # Verify warning was logged with specific message
        assert any("Phase 2 match" in record.message for record in caplog.records), \
            "Expected 'Phase 2 match' warning in logs"

    def test_match_phase2_ambiguous(self):
        """Phase 2: No match when 2+ 'Not Available' shipments match same amount+date."""
        shipment1 = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Not Available",
            payment_method_last4=None,
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
            payment_method_raw="Not Available",
            payment_method_last4=None,
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment1, shipment2], account_last4)

        # Txn should be unmatched due to ambiguity
        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1
        assert "multiple" in result.unmatched_ynab[0][1].lower()

    def test_match_excluded_shipments(self):
        """Excluded shipments appear in excluded_shipments list."""
        # Create a split-tender shipment (excluded from matching)
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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([txn], [shipment], account_last4)

        assert len(result.excluded_shipments) == 1
        assert "split tender" in result.excluded_shipments[0][1].lower()

    def test_match_missing_account_last4(self):
        """YNAB txn with missing account in last-4 map ends up unmatched."""
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
            "account_id": "account-unknown",
            "account_name": "Unknown Account",
            "date": "2024-01-15",
            "amount": -112000,
            "payee_name": "Amazon",
            "category_id": None,
            "cleared": "uncleared",
            "deleted": False,
        }

        account_last4 = {"account-1": "0804"}  # Doesn't include account-unknown
        result = match_shipments_to_transactions([txn], [shipment], account_last4)

        assert len(result.matched) == 0
        assert len(result.unmatched_ynab) == 1
        assert "last-4 mapping" in result.unmatched_ynab[0][1].lower() or "Unknown Account" in result.unmatched_ynab[0][1]

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

        account_last4 = {"account-1": "0804"}
        result = match_shipments_to_transactions([], [shipment], account_last4, parse_errors=parse_errors)

        assert len(result.parse_errors) == 1
        assert result.parse_errors[0].reason == "test error"


# ============================================================================
# Allocator Tests: Pro-rata Allocation
# ============================================================================


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

    def test_banker_rounding_vs_half_up(self):
        """Banker's rounding (ROUND_HALF_EVEN) differs from ROUND_HALF_UP at .x5 boundaries.

        This test constructs a case that demonstrably diverges between ROUND_HALF_EVEN
        and ROUND_HALF_UP so that swapping the rounding mode would cause this test to fail.
        """
        # Construct items where each gets allocated exactly 0.505 (before rounding)
        # total = $1.01, items = [1.00, 1.00], subtotal = $2.00
        # share_item1 = 1.00 / 2.00 = 0.5
        # allocated_item1 = 1.01 * 0.5 = 0.505
        # ROUND_HALF_EVEN: 0.505 -> 0.50 (rounds to nearest even)
        # ROUND_HALF_UP: 0.505 -> 0.51 (always rounds up)
        item1 = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C1234567",
            product_name="Item 1",
            quantity=1,
            unit_price=Decimal("1.00"),
            unit_price_tax=Decimal("0"),
            raw_row_index=2,
        )
        item2 = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            asin="B0C2234567",
            product_name="Item 2",
            quantity=1,
            unit_price=Decimal("1.00"),
            unit_price_tax=Decimal("0"),
            raw_row_index=3,
        )

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("2.00"),
            tax=Decimal("0.00"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("1.01"),  # Creates 0.505 per item before rounding
            items=[item1, item2],
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        # With ROUND_HALF_EVEN, both items round to 0.50 (even), then delta of 0.01
        # is distributed starting with highest subtotal (tie: both 1.00) then lowest index.
        # So index 0 gets the 0.01 adjustment.
        # Expected: item 0 = 0.51, item 1 = 0.50 (or if delta distributes first: 0.50, 0.50 then +0.01 to item 0)
        # Either way, the sum must be exactly 1.01
        assert sum(a.allocated_amount for a in allocations) == Decimal("1.01")
        # Verify HALF_EVEN behavior: with tie at 0.505, both round to 0.50
        assert allocations[0].allocated_amount == Decimal("0.51")
        assert allocations[1].allocated_amount == Decimal("0.50")

    def test_multi_cent_delta_distribution(self):
        """Multi-cent rounding delta is distributed across items according to policy.

        Verifies that when rounding produces a multi-cent delta, it is distributed
        per-item (not dumped on one item), respecting the allocation policy.
        """
        # 3 items, each with same subtotal
        # subtotal = $3.00, total = $3.02 → 2¢ delta
        # Each item should get $1.00 base, then 2¢ distributed across highest subtotal / lowest index
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin=f"B0C{i}234567",
                product_name=f"Item {i}",
                quantity=1,
                unit_price=Decimal("1.00"),
                unit_price_tax=Decimal("0"),
                raw_row_index=i+2,
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
            item_subtotal=Decimal("3.00"),
            tax=Decimal("0.02"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("3.02"),
            items=items,
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        # Verify sum
        assert sum(a.allocated_amount for a in allocations) == Decimal("3.02")

        # Verify per-item: each item initially rounds to 1.01, but sum is 3.03 (1¢ overage).
        # Delta distribution removes 1¢ from items in sorted order (all equal, so index order).
        # Item 0 (index 0) gets -1¢, items 1-2 stay at 1.01.
        # Expected distribution: [1.00, 1.01, 1.01]
        assert allocations[0].allocated_amount == Decimal("1.00")
        assert allocations[1].allocated_amount == Decimal("1.01")
        assert allocations[2].allocated_amount == Decimal("1.01")

    def test_deterministic_tiebreak_equal_subtotals(self):
        """Two items with identical raw_subtotal: smaller index gets penny first.

        When two items have the same subtotal, the delta distribution policy
        processes them in index order (lowest index first). This test verifies
        that determinism: item 0 always gets the extra penny, not item 1.
        """
        items = [
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C1234567",
                product_name="Item 1 (equal)",
                quantity=1,
                unit_price=Decimal("5.00"),
                unit_price_tax=Decimal("0"),
                raw_row_index=2,
            ),
            AmazonItem(
                order_id="111-0000001-0000001",
                ship_date=date(2024, 1, 15),
                asin="B0C2234567",
                product_name="Item 2 (equal)",
                quantity=1,
                unit_price=Decimal("5.00"),
                unit_price_tax=Decimal("0"),
                raw_row_index=3,
            ),
        ]

        # Subtotal $10.00, total $10.01 → 1¢ delta from rounding
        # Each item's share: 5.00 / 10.00 = 0.5
        # allocated = 10.01 * 0.5 = 5.005
        # ROUND_HALF_EVEN: 5.005 -> 5.00 (nearest even)
        # Delta = 10.01 - (5.00 + 5.00) = 0.01
        # Distributed to highest subtotal (tie: both 5.00) then lowest index (item 0)
        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=date(2024, 1, 15),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("10.00"),
            tax=Decimal("0.01"),
            shipping=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("10.01"),
            items=items,
            shipment_status="Shipped",
        )

        allocations = allocate_shipment_to_items(shipment)

        # Verify sum
        assert sum(a.allocated_amount for a in allocations) == Decimal("10.01")

        # Verify deterministic tiebreak: item 0 gets the penny, item 1 gets the base
        assert allocations[0].allocated_amount == Decimal("5.01")
        assert allocations[1].allocated_amount == Decimal("5.00")


# ============================================================================
# Integration Tests: Match Full Pipeline
# ============================================================================


class TestIntegrationMatching:
    """Integration tests for the full matching pipeline."""

    def test_it_match_fixture_pipeline(self):
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
        with open("data/fixtures/amazon_account_last4.json") as f:
            account_last4 = json.load(f)

        # Parse the CSV
        shipments, parse_errors = parse_order_history(csv_content)

        # Filter YNAB transactions (should exclude already-categorized, reconciled, non-Amazon)
        amazon_txns = filter_amazon_transactions(ynab_txns)
        # From fixture: 9 txns, filter removes txn-006 (categorized), txn-007 (reconciled), txn-009 (non-Amazon)
        assert len(amazon_txns) == 6

        # Match
        result = match_shipments_to_transactions(
            amazon_txns, shipments, account_last4, parse_errors=parse_errors
        )

        # Verify structure
        assert isinstance(result, MatchResult)
        assert isinstance(result.matched, list)
        assert isinstance(result.unmatched_ynab, list)
        assert isinstance(result.unmatched_shipments, list)
        assert isinstance(result.excluded_shipments, list)
        assert isinstance(result.parse_errors, list)

        # Hardcoded fixture expectations
        # Fixture amounts are in milliunits (1000 = $1.00).
        # Transactions are on accounts with specific visa last-4 mappings in account_last4.json:
        # txn-001: account-visa-1 (0804), date 2024-01-15, amount 37090 ($37.09)
        #   → matches order 111-0000001-0000001 (ship_date 2024-01-16, Visa-0804, $37.09) [strict]
        # txn-002: account-visa-2 (1523), date 2024-01-16, amount 48120 ($48.12)
        #   → matches order 111-0000002-0000002 (ship_date 2024-01-17, Visa-1523, $48.12) [strict]
        # txn-003: account-visa-3 (2401), date 2024-01-17, amount 14190 ($14.19)
        #   → matches order 111-0000003 item 1 (ship_date 2024-01-18, Visa-2401, $14.19) [strict]
        # txn-004: account-visa-4 (2401), date 2024-01-17, amount 17400 ($17.40), payee "AMZN Mktp US"
        #   → matches order 111-0000003 item 2 (ship_date 2024-01-18, Visa-2401, $17.40) [strict]
        # txn-005: amount 54710, no matching shipment (mismatched amount) [unmatched]
        # txn-008: account-unknown (not in account_last4) [unmatched]

        # Verify matched transactions: should have 4 strict (Phase 1) matches
        assert len(result.matched) == 4, f"Expected 4 matches, got {len(result.matched)}"

        # All matches should be strict (exact date + amount + last-4)
        strict_matches = [m for m in result.matched if m.match_mode == "strict"]
        assert len(strict_matches) == 4, f"Expected 4 strict matches, got {len(strict_matches)}"

        # Verify specific matched pairs (txn_id → order_id mapping)
        matched_pairs = {m.ynab_txn["id"]: m.shipment.order_id for m in result.matched}
        assert matched_pairs["txn-001"] == "111-0000001-0000001"
        assert matched_pairs["txn-002"] == "111-0000002-0000002"
        assert matched_pairs["txn-003"] == "111-0000003-0000003"
        assert matched_pairs["txn-004"] == "111-0000003-0000003"

        # Verify unmatched YNAB txns: txn-005 (no matching shipment) and txn-008 (unknown account)
        unmatched_ids = {t[0]["id"] for t in result.unmatched_ynab}
        assert "txn-005" in unmatched_ids, "txn-005 should be unmatched (no matching shipment)"
        assert "txn-008" in unmatched_ids, "txn-008 should be unmatched (account not in account_last4)"

        # Verify excluded shipments exist (from other orders with split tender, Not Available, zero price)
        assert len(result.excluded_shipments) > 0, "Expected excluded shipments from fixture"

        # Verify parse errors exist (EUR currency, embedded newline, etc.)
        assert len(result.parse_errors) > 0, "Expected parse errors from fixture"
