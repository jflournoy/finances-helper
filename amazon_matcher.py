"""Match Amazon orders to YNAB transactions.

Matches Amazon order history CSV to YNAB transactions by date and amount,
assigns categories via Claude, and writes item descriptions to memo fields.
"""

import csv
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from zipfile import ZipFile, BadZipFile

logger = logging.getLogger(__name__)


def _money(s: str) -> Decimal:
    """Parse a currency string to Decimal.

    Strips $, commas, whitespace, and Excel single quotes. Empty string returns 0.

    Args:
        s: Currency string (e.g., "$1,234.56" or "'1234.56'" from Excel exports)

    Returns:
        Decimal value

    Raises:
        ValueError: If the string cannot be parsed as a number
    """
    if s is None or s.strip() == "":
        return Decimal("0")
    if s.strip() == "Not Available":
        return Decimal("0")
    cleaned = s.strip().replace("$", "").replace(",", "").replace("'", "")
    try:
        return Decimal(cleaned)
    except InvalidOperation as e:
        raise ValueError(f"Unparseable money value: {s!r}") from e


def _parse_amazon_date(s: str) -> date | None:
    """Parse an Amazon CSV date field to a `date`.

    Accepts both the legacy date-only format (``2024-01-16``) and the
    ISO8601 datetime format emitted by the "Request My Data" export
    (``2025-04-12T18:27:51.050Z``, with optional fractional seconds and
    ``Z`` UTC marker). Returns ``None`` if the value cannot be parsed.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    # Legacy date-only: take the first whitespace-separated token.
    first = s.split()[0]
    try:
        return date.fromisoformat(first)
    except ValueError:
        pass
    # ISO8601 datetime: strip trailing 'Z' (UTC) so fromisoformat accepts it.
    candidate = first[:-1] if first.endswith("Z") else first
    try:
        return datetime.fromisoformat(candidate).date()
    except ValueError:
        return None


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
        ValueError: If the zip file is invalid or the expected CSV path is missing
    """
    try:
        with ZipFile(zip_path, "r") as zf:
            pass
    except BadZipFile as e:
        raise ValueError(f"Invalid zip file: {zip_path}") from e

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
            ship_date = _parse_amazon_date(row["Ship Date"])
            if ship_date is None:
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


# ============================================================================
# Phase B: Matching
# ============================================================================


@dataclass
class MatchCandidate:
    """A potential match between a YNAB transaction and an Amazon shipment."""

    ynab_txn: dict
    shipment: AmazonShipment
    date_delta_days: int  # signed: ship_date - ynab_date


@dataclass
class MatchResult:
    """Result of the matching algorithm."""

    matched: list[MatchCandidate]
    unmatched_ynab: list[tuple[dict, str]]  # (txn, reason)
    unmatched_shipments: list[AmazonShipment]
    excluded_shipments: list[tuple[AmazonShipment, str]]  # (shipment, reason)
    parse_errors: list[ParseError]


def is_amazon_payee(payee: str | None) -> bool:
    """Return True if payee name looks like an Amazon transaction.

    Matches the same logic used by filter_amazon_transactions:
    - Starts with "amazon" (case-insensitive), OR
    - Contains "amzn" (case-insensitive)

    Used by both filter_amazon_transactions and categorizer's Amazon router
    to ensure consistent payee detection across the pipeline.
    """
    if not payee:
        return False
    p = payee.lower()
    return p.startswith("amazon") or "amzn" in p


def filter_amazon_transactions(ynab_txns: list[dict]) -> list[dict]:
    """Filter YNAB transactions to keep only Amazon purchases.

    Keep a transaction if ALL:
    - payee_name matches "amazon" (case-insensitive) or contains "AMZN"
    - category_id is None (not yet categorized)
    - cleared != "reconciled" (reconciled cannot be edited via API)
    - deleted is not True

    Args:
        ynab_txns: List of YNAB transaction dicts

    Returns:
        List of filtered transactions
    """
    result = []

    for txn in ynab_txns:
        # Check payee
        if not is_amazon_payee(txn.get("payee_name")):
            continue

        # Check category
        if txn.get("category_id") is not None:
            continue

        # Check cleared status
        if txn.get("cleared") == "reconciled":
            continue

        # Check deleted
        if txn.get("deleted") is True:
            continue

        result.append(txn)

    return result


def match_shipments_to_transactions(
    ynab_txns: list[dict],
    shipments: list[AmazonShipment],
    parse_errors: list[ParseError] | None = None,
    date_window_days: int = 3,
) -> MatchResult:
    """Match Amazon shipments to YNAB transactions using shipment-pivoted algorithm.
    
    No account_last4_map required. Matches by amount + date window.
    
    Algorithm: Two-pass "resolve-easy-first"
    - Phase 1: Enumerate candidates for each matchable shipment
    - Phase 2: Iteratively resolve singletons (no contention)
    - Phase 3: Case A - tiebreak for multi-candidate shipments
    - Phase 4: Classify remaining unresolved shipments (Case B error)
    """
    if parse_errors is None:
        parse_errors = []

    matched = []
    unmatched_ynab = []
    excluded_shipments_list = []

    # Categorize shipments by matchability
    matchable = [s for s in shipments if s.is_matchable]
    unmatchable = [s for s in shipments if not s.is_matchable]

    # Build excluded_shipments list with reasons
    for shipment in unmatchable:
        if shipment.shipment_status == "Not Available":
            reason = "unshipped (status Not Available)"
        elif shipment.is_split_tender:
            reason = "split tender (gift card + card)"
        elif any(item.unit_price == Decimal("0") for item in shipment.items):
            reason = "contains zero-price item"
        elif shipment.ship_date is None:
            reason = "missing ship date"
        else:
            reason = "excluded (unknown reason)"
        excluded_shipments_list.append((shipment, reason))

    # Phase 1: Enumerate candidates for each matchable shipment
    candidates = []  # list of (shipment, [(txn, date_delta), ...])

    for shipment in matchable:
        shipment_candidates = []

        for txn in ynab_txns:
            txn_amt = abs(Decimal(txn["amount"])) / Decimal(1000)
            txn_date = date.fromisoformat(txn["date"])

            # Check exact amount match and date window
            if txn_amt == shipment.total_amount and shipment.ship_date is not None:
                date_delta = (shipment.ship_date - txn_date).days
                if abs(date_delta) <= date_window_days:
                    shipment_candidates.append((txn, date_delta))

        candidates.append((shipment, shipment_candidates))

    # Phase 2 & 3: Iterative resolution
    consumed_txn_ids = set()
    resolved = {}  # index → (txn, date_delta)
    # Track unresolved by index (not by order_id, since multiple shipments can share order_id)
    unresolved_indices = set(range(len(candidates)))

    max_iterations = 100
    iteration = 0

    while unresolved_indices and iteration < max_iterations:
        iteration += 1
        changed = False

        # Update candidate lists (drop consumed txns)
        for idx in list(unresolved_indices):
            shipment, cands = candidates[idx]
            cands = [(t, d) for t, d in cands if t["id"] not in consumed_txn_ids]
            candidates[idx] = (shipment, cands)

        # Phase 2: Resolve singletons
        for idx in list(unresolved_indices):
            shipment, cands = candidates[idx]

            if len(cands) == 0:
                # No match
                unresolved_indices.remove(idx)
                changed = True
            elif len(cands) == 1:
                # Check for Case B: another unresolved shipment also has this as sole candidate
                txn = cands[0][0]
                case_b = any(
                    idx2 != idx and
                    idx2 in unresolved_indices and
                    len(candidates[idx2][1]) == 1 and
                    candidates[idx2][1][0][0]["id"] == txn["id"]
                    for idx2 in unresolved_indices
                )
                if not case_b:
                    # Safe to consume
                    txn, date_delta = cands[0]
                    resolved[idx] = (txn, date_delta)
                    consumed_txn_ids.add(txn["id"])
                    unresolved_indices.remove(idx)
                    changed = True

        # Phase 3: Tiebreak for multi-candidate shipments
        for idx in list(unresolved_indices):
            shipment, cands = candidates[idx]
            if len(cands) >= 2:
                # Sort: (abs(date_delta), txn_date, txn_id)
                sorted_cands = sorted(
                    cands,
                    key=lambda c: (abs(c[1]), c[0]["date"], c[0]["id"])
                )
                txn, date_delta = sorted_cands[0]
                resolved[idx] = (txn, date_delta)
                consumed_txn_ids.add(txn["id"])
                unresolved_indices.remove(idx)
                changed = True

        if not changed:
            break

    # Phase 4: Classify remaining unresolved shipments
    case_b_shipments = []
    for idx in unresolved_indices:
        shipment, cands = candidates[idx]
        cands = [(t, d) for t, d in cands if t["id"] not in consumed_txn_ids]

        if len(cands) > 0:
            # Case B: contention
            case_b_shipments.append((shipment, cands))

    # Log Case B errors once (not per shipment)
    if case_b_shipments:
        all_txn_ids = set()
        all_shipment_ids = []
        for shipment, cands in case_b_shipments:
            all_shipment_ids.append(shipment.order_id)
            for t, _ in cands:
                all_txn_ids.add(t["id"])
        logger.error(
            f"Contended: {len(case_b_shipments)} shipments share YNAB txn(s) {sorted(all_txn_ids)}. "
            f"Shipments: {all_shipment_ids}. Reason: check for missing charge or duplicate."
        )

    # Build matched list
    for idx, (txn, date_delta) in resolved.items():
        shipment, _ = candidates[idx]
        match = MatchCandidate(
            ynab_txn=txn,
            shipment=shipment,
            date_delta_days=date_delta,
        )
        matched.append(match)

    # Unmatched YNAB transactions
    matched_txn_ids = {m.ynab_txn["id"] for m in matched}
    contended_txn_ids = set()
    for shipment, cands in case_b_shipments:
        for t, _ in cands:
            contended_txn_ids.add(t["id"])

    for txn in ynab_txns:
        if txn["id"] not in matched_txn_ids:
            if txn["id"] in contended_txn_ids:
                reason = f"contended: multiple shipments share this txn — check for missing charge or duplicate"
            else:
                reason = "no matching shipment in dump"
            unmatched_ynab.append((txn, reason))

    # Unmatched shipments = those with zero candidates OR Case B contention
    zero_candidate_indices = [idx for idx in unresolved_indices if len(candidates[idx][1]) == 0]
    zero_candidate_shipments = [candidates[idx][0] for idx in zero_candidate_indices]
    case_b_unmatched = [s for s, _ in case_b_shipments]
    unmatched_shipments = zero_candidate_shipments + case_b_unmatched

    return MatchResult(
        matched=matched,
        unmatched_ynab=unmatched_ynab,
        unmatched_shipments=unmatched_shipments,
        excluded_shipments=excluded_shipments_list,
        parse_errors=parse_errors,
    )



# ============================================================================
# Phase C: Pro-rata Allocation
# ============================================================================


@dataclass
class ItemAllocation:
    """Allocation of shipment total to an individual item."""

    item: AmazonItem
    allocated_amount: Decimal  # quantized to cents
    share_of_subtotal: Decimal  # high-precision share (8 decimal places)


def allocate_shipment_to_items(shipment: AmazonShipment) -> list[ItemAllocation]:
    """Allocate a multi-item shipment's total pro-rata across items.

    Each item gets a share of the total proportional to its subtotal. Any
    sub-cent rounding remainder is dumped onto the last item so the
    allocations sum exactly to shipment.total_amount.

    Args:
        shipment: An AmazonShipment with one or more items

    Returns:
        List of ItemAllocation, one per item

    Raises:
        RuntimeError: If the shipment's computed subtotal doesn't match the CSV
                     item_subtotal by more than 1 cent (parser bug or Amazon
                     inconsistency)
    """
    # Single-item shortcut
    if len(shipment.items) == 1:
        item = shipment.items[0]
        return [
            ItemAllocation(
                item=item,
                allocated_amount=shipment.total_amount,
                share_of_subtotal=Decimal("1"),
            )
        ]

    # Multi-item: validate subtotal consistency
    computed_subtotal = sum(
        item.unit_price * item.quantity for item in shipment.items
    )

    if abs(computed_subtotal - shipment.item_subtotal) > Decimal("0.01"):
        raise RuntimeError(
            f"Subtotal mismatch in shipment {shipment.order_id}: "
            f"computed {computed_subtotal}, CSV has {shipment.item_subtotal}"
        )

    # Compute shares and initial allocations
    allocations = []
    for item in shipment.items:
        raw_subtotal = item.unit_price * item.quantity
        share = (raw_subtotal / shipment.item_subtotal).quantize(
            Decimal("0.00000001")
        )
        allocated = (shipment.total_amount * share).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        allocations.append(
            ItemAllocation(
                item=item,
                allocated_amount=allocated,
                share_of_subtotal=share,
            )
        )

    # Dump any rounding delta onto the last item. A penny or two doesn't matter.
    rounding_delta = shipment.total_amount - sum(a.allocated_amount for a in allocations)
    if rounding_delta != Decimal("0"):
        allocations[-1].allocated_amount += rounding_delta

    return allocations
