"""Match Amazon orders to YNAB transactions.

Matches Amazon order history CSV to YNAB transactions by date and amount,
assigns categories via Claude, and writes item descriptions to memo fields.
"""

import csv
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zipfile import ZipFile

logger = logging.getLogger(__name__)


def _money(s: str) -> Decimal:
    """Parse a currency string to Decimal.

    Strips $, commas, and whitespace. Empty string returns 0.

    Args:
        s: Currency string (e.g., "$1,234.56")

    Returns:
        Decimal value

    Raises:
        ValueError: If the string cannot be parsed as a number
    """
    if s is None or s.strip() == "":
        return Decimal("0")
    cleaned = s.strip().replace("$", "").replace(",", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as e:
        raise ValueError(f"Unparseable money value: {s!r}") from e


def find_latest_dump(imports_dir: Path = Path("data/imports")) -> Path:
    """Find the latest Amazon order history dump in the imports directory.

    Searches for files matching 'amazon-order-history-*.zip' (preferred) or
    'Your Orders*.zip' (legacy fallback with warning).

    Args:
        imports_dir: Directory to search (default: data/imports)

    Returns:
        Path to the latest dump file

    Raises:
        FileNotFoundError: If no dump files are found
    """
    imports_dir = Path(imports_dir)

    # Try preferred naming scheme
    iso_dated = sorted(imports_dir.glob("amazon-order-history-*.zip"))
    if iso_dated:
        return iso_dated[-1]

    # Fall back to legacy naming with warning
    legacy = sorted(imports_dir.glob("Your Orders*.zip"))
    if legacy:
        logger.warning(
            "Found legacy Amazon dump file '%s'. Please rename it to "
            "'amazon-order-history-YYYY-MM-DD.zip' (e.g., "
            "'amazon-order-history-2026-04-14.zip') to follow the standard naming.",
            legacy[-1].name,
        )
        return legacy[-1]

    # Nothing found
    raise FileNotFoundError(
        f"No Amazon order history dump found in {imports_dir}. "
        f"Expected 'amazon-order-history-*.zip' or 'Your Orders*.zip'"
    )


def extract_order_history_csv(zip_path: Path) -> str:
    """Extract the Order History CSV from an Amazon data dump zip.

    Args:
        zip_path: Path to the amazon-order-history-*.zip file

    Returns:
        CSV file contents as a string

    Raises:
        ValueError: If the expected CSV path is missing from the zip
    """
    try:
        with ZipFile(zip_path, "r") as zf:
            csv_path = "Your Amazon Orders/Order History.csv"
            if csv_path not in zf.namelist():
                files = "\n  ".join(zf.namelist())
                raise ValueError(
                    f"Expected CSV path '{csv_path}' not found in {zip_path.name}. "
                    f"Zip contains:\n  {files}"
                )

            csv_bytes = zf.read(csv_path)

            # Try UTF-8, then UTF-8 with BOM, then cp1252
            for encoding in ["utf-8", "utf-8-sig", "cp1252"]:
                try:
                    return csv_bytes.decode(encoding)
                except UnicodeDecodeError:
                    if encoding == "cp1252":
                        raise  # Last attempt failed
                    continue

    except Exception as e:
        if "BadZipFile" in type(e).__name__:
            raise ValueError(f"Invalid zip file: {zip_path}") from e
        raise


@dataclass(frozen=True)
class AmazonItem:
    """A single item from an Amazon order."""

    order_id: str
    ship_date: date | None
    asin: str
    product_name: str
    quantity: int
    unit_price: Decimal
    unit_price_tax: Decimal
    raw_row_index: int  # 1-based CSV row for error reporting


@dataclass
class AmazonShipment:
    """A shipment (may contain multiple items)."""

    order_id: str
    ship_date: date | None
    payment_method_raw: str
    payment_method_last4: str | None
    is_split_tender: bool
    currency: str
    item_subtotal: Decimal
    tax: Decimal
    shipping: Decimal
    discounts: Decimal
    total_amount: Decimal
    items: list[AmazonItem]
    shipment_status: str
    matched_to_ynab_id: str | None = None

    @property
    def expected_charge(self) -> Decimal:
        """Calculate expected total charge."""
        return self.item_subtotal + self.tax + self.shipping - self.discounts

    @property
    def is_matchable(self) -> bool:
        """Check if shipment can be matched to YNAB transactions.

        A shipment is matchable if:
        - shipment_status != "Not Available"
        - ship_date is not None
        - is_split_tender is False
        - all items have non-zero unit_price
        """
        if self.shipment_status == "Not Available":
            return False
        if self.ship_date is None:
            return False
        if self.is_split_tender:
            return False
        if any(item.unit_price == Decimal("0") for item in self.items):
            return False
        return True


@dataclass(frozen=True)
class ParseError:
    """An error encountered while parsing the CSV."""

    row_index: int  # 1-based CSV row
    reason: str
    raw_row: dict[str, str] | None = None


def parse_order_history(csv_text: str) -> tuple[list[AmazonShipment], list[ParseError]]:
    """Parse Amazon Order History CSV into shipments and error list.

    Args:
        csv_text: CSV file contents as string

    Returns:
        Tuple of (list of AmazonShipment, list of ParseError)

    Raises:
        ValueError: If CSV header is missing required columns
    """
    shipments = []
    errors = []

    reader = csv.DictReader(csv_text.splitlines())

    # Validate header
    required_columns = {
        "Order ID",
        "Order Date",
        "Ship Date",
        "Order Status",
        "Shipment Status",
        "Payment Method Type",
        "Currency",
        "Unit Price",
        "Unit Price Tax",
        "Shipment Item Subtotal",
        "Shipment Item Subtotal Tax",
        "Shipping Charge",
        "Total Amount",
        "Total Discounts",
        "ASIN",
        "Product Name",
        "Original Quantity",
    }

    if reader.fieldnames is None:
        raise ValueError("CSV is empty or malformed")

    missing = required_columns - set(reader.fieldnames)
    if missing:
        raise ValueError(f"CSV header missing required columns: {', '.join(sorted(missing))}")

    # Parse rows
    rows_by_group = {}  # group_key -> list of rows
    row_index = 2  # Header is row 1, data starts at row 2

    for row in reader:
        # Skip cancelled orders
        if row["Order Status"] == "Cancelled":
            row_index += 1
            continue

        # Check for empty order status
        if row["Order Status"] == "":
            errors.append(ParseError(row_index=row_index, reason="empty order status"))
            row_index += 1
            continue

        # Check currency
        if row["Currency"] != "USD":
            errors.append(
                ParseError(row_index=row_index, reason=f"non-USD currency: {row['Currency']}")
            )
            row_index += 1
            continue

        # Parse ship date
        ship_date = None
        if row["Ship Date"] != "Not Available":
            try:
                ship_date = date.fromisoformat(row["Ship Date"].split()[0])
            except (ValueError, IndexError):
                errors.append(
                    ParseError(row_index=row_index, reason=f"unparseable ship date: {row['Ship Date']}")
                )
                row_index += 1
                continue

        # Parse payment method and detect split tender
        payment_method_raw = row["Payment Method Type"]
        is_split_tender = payment_method_raw.startswith("Gift Certificate/Card and")
        payment_method_last4 = None

        # Extract last-4 from payment method
        if payment_method_raw != "Not Available":
            import re
            match = re.search(r"-\s*(\d{4})\s*$", payment_method_raw)
            if match:
                payment_method_last4 = match.group(1)
            else:
                errors.append(
                    ParseError(
                        row_index=row_index,
                        reason=f"unparseable payment method: {payment_method_raw}",
                    )
                )
                # Keep the row anyway (may still be useful for fallback matching)

        # Parse money fields
        try:
            item_subtotal = _money(row["Shipment Item Subtotal"])
            tax = _money(row["Shipment Item Subtotal Tax"])
            shipping = _money(row["Shipping Charge"])
            discounts = _money(row["Total Discounts"])
            total_amount = _money(row["Total Amount"])
            unit_price = _money(row["Unit Price"])
            unit_price_tax = _money(row["Unit Price Tax"])
        except ValueError as e:
            errors.append(ParseError(row_index=row_index, reason=f"unparseable money value: {e}"))
            row_index += 1
            continue

        # Parse quantity
        try:
            qty_str = row.get("Original Quantity", "").strip()
            if not qty_str:
                errors.append(
                    ParseError(row_index=row_index, reason="missing quantity")
                )
                row_index += 1
                continue
            quantity = int(qty_str)
        except (ValueError, KeyError, TypeError):
            errors.append(
                ParseError(row_index=row_index, reason="unparseable or missing quantity")
            )
            row_index += 1
            continue

        # Create item
        item = AmazonItem(
            order_id=row["Order ID"],
            ship_date=ship_date,
            asin=row["ASIN"],
            product_name=row["Product Name"],
            quantity=quantity,
            unit_price=unit_price,
            unit_price_tax=unit_price_tax,
            raw_row_index=row_index,
        )

        # Group by shipment key (must use Total Amount as string to avoid Decimal precision issues)
        group_key = (
            row["Order ID"],
            str(ship_date) if ship_date else "N/A",
            row["Shipment Status"],
            row["Total Amount"],
        )

        if group_key not in rows_by_group:
            rows_by_group[group_key] = {
                "rows": [],
                "item_subtotal": item_subtotal,
                "tax": tax,
                "shipping": shipping,
                "discounts": discounts,
                "total_amount": total_amount,
                "payment_method_raw": payment_method_raw,
                "payment_method_last4": payment_method_last4,
                "is_split_tender": is_split_tender,
                "currency": row["Currency"],
                "shipment_status": row["Shipment Status"],
            }

        rows_by_group[group_key]["rows"].append(item)
        row_index += 1

    # Build shipments from groups
    for group_key, group_data in rows_by_group.items():
        order_id, ship_date_str, shipment_status, total_amount_str = group_key
        ship_date = None if ship_date_str == "N/A" else date.fromisoformat(ship_date_str)

        # Get the first row for row_index in the group (for error reporting)
        first_row_index = min(item.raw_row_index for item in group_data["rows"])

        shipment = AmazonShipment(
            order_id=order_id,
            ship_date=ship_date,
            payment_method_raw=group_data["payment_method_raw"],
            payment_method_last4=group_data["payment_method_last4"],
            is_split_tender=group_data["is_split_tender"],
            currency=group_data["currency"],
            item_subtotal=group_data["item_subtotal"],
            tax=group_data["tax"],
            shipping=group_data["shipping"],
            discounts=group_data["discounts"],
            total_amount=group_data["total_amount"],
            items=group_data["rows"],
            shipment_status=shipment_status,
        )

        # Check charge math
        expected = shipment.expected_charge
        if abs(expected - shipment.total_amount) > Decimal("0.02"):
            errors.append(
                ParseError(
                    row_index=first_row_index,
                    reason=f"charge math mismatch: expected {expected}, got {shipment.total_amount}",
                )
            )

        shipments.append(shipment)

    return shipments, errors
