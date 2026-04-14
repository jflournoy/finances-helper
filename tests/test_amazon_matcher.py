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
    extract_order_history_csv,
    find_latest_dump,
    parse_order_history,
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
