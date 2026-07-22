"""Match Amazon orders to YNAB transactions.

Matches Amazon order history CSV to YNAB transactions by date and amount,
assigns categories via Claude, and writes item descriptions to memo fields.
"""

import csv
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from pathlib import Path
from zipfile import ZipFile, BadZipFile
from dotenv import load_dotenv

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

        # Try UTF-8 with BOM (also handles plain UTF-8), then cp1252.
        # utf-8-sig must come before utf-8: plain "utf-8" decodes a BOM's
        # bytes into a literal U+FEFF character instead of erroring, so it
        # would silently corrupt the first header column (e.g. "﻿ASIN").
        for encoding in ["utf-8-sig", "cp1252"]:
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


WF_SITES = frozenset({"PrimeNow-US", "panda01"})
WF_AMAZON_CARRIER_TOKEN = "RABBIT"


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
    website: str = ""
    carrier: str = ""

    @property
    def is_wf(self) -> bool:
        """True for Whole Foods / Amazon Fresh grocery deliveries.

        Canonical rule (matches scripts/inflation_config.py): site is PrimeNow-US
        or panda01, OR Amazon.com shipped via the RABBIT fleet.
        """
        site = (self.website or "").strip()
        if site in WF_SITES:
            return True
        return site == "Amazon.com" and WF_AMAZON_CARRIER_TOKEN in (self.carrier or "")

    @property
    def expected_charge(self) -> Decimal:
        """Calculate expected total charge.

        Real Amazon CSVs put discounts as a NEGATIVE number in 'Total Discounts'
        (e.g. -1.22 means a $1.22 discount). The formula adds discounts so the
        sign already reduces the total; positive discount values would increase
        it, which is the convention some hand-rolled fixtures use.
        """
        return self.item_subtotal + self.tax + self.shipping + self.discounts

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
    # Website and Carrier Name & Tracking Number are read when present (used to
    # detect Whole Foods / Amazon Fresh deliveries via AmazonShipment.is_wf).
    # They're optional so legacy CSV fixtures continue to parse.

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

        # Group by shipment key. Total Amount is per-ITEM in real Amazon CSVs
        # so it must NOT be in the key — otherwise a multi-item shipment with
        # distinct per-item totals fragments into N one-item "shipments".
        # Shipping Charge and Total Discounts are per-row (allocated across items),
        # so they must be summed across rows. item_subtotal / tax are per-shipment
        # (repeated identically on each row) — take from the first row.
        # Carrier tracking number is required to disambiguate two separate
        # shipments that share order_id + ship_date + status (e.g. an order
        # split into two parcels delivered the same day).
        group_key = (
            row["Order ID"],
            str(ship_date) if ship_date else "N/A",
            row["Shipment Status"],
            row.get("Carrier Name & Tracking Number", ""),
        )

        if group_key not in rows_by_group:
            rows_by_group[group_key] = {
                "rows": [],
                "item_subtotal": item_subtotal,
                "tax": tax,
                "shipping": Decimal("0"),
                "discounts": Decimal("0"),
                "total_amount": Decimal("0"),
                "payment_method_raw": payment_method_raw,
                "payment_method_last4": payment_method_last4,
                "is_split_tender": is_split_tender,
                "currency": row["Currency"],
                "shipment_status": row["Shipment Status"],
                "website": row.get("Website", ""),
                "carrier": row.get("Carrier Name & Tracking Number", ""),
            }

        rows_by_group[group_key]["rows"].append(item)
        rows_by_group[group_key]["total_amount"] += total_amount
        rows_by_group[group_key]["shipping"] += shipping
        rows_by_group[group_key]["discounts"] += discounts
        row_index += 1

    # Build shipments from groups
    for group_key, group_data in rows_by_group.items():
        order_id, ship_date_str, shipment_status, _carrier = group_key
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
            website=group_data["website"],
            carrier=group_data["carrier"],
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


def collect_dump_status_values(csv_text: str) -> tuple[set[str], set[str]]:
    """Scan a dump CSV and return the distinct Order Status and Shipment Status values.

    Cheap O(rows) pass used by the schema-drift canary. Does not validate header
    columns — caller should have already invoked parse_order_history (which
    raises on missing required columns). Empty string values are skipped because
    parse_order_history already emits ParseErrors for empty-status rows.

    Returns:
        (order_statuses, shipment_statuses) — sets of non-empty distinct values.
    """
    reader = csv.DictReader(csv_text.splitlines())
    order_statuses: set[str] = set()
    shipment_statuses: set[str] = set()
    for row in reader:
        o = row.get("Order Status", "")
        s = row.get("Shipment Status", "")
        if o:
            order_statuses.add(o)
        if s:
            shipment_statuses.add(s)
    return order_statuses, shipment_statuses


def check_dump_schema_drift(
    csv_text: str,
    baseline_path: Path,
) -> tuple[set[str], set[str]]:
    """Compare a dump's status values to the learned baseline and update it.

    On first run (no baseline file): seeds the baseline silently and returns
    empty drift sets — there is no prior knowledge to compare against, so
    everything is "known" by definition.

    On subsequent runs: returns the set of Order Status / Shipment Status
    values not present in the baseline, then writes the union back so each
    value is only flagged once across runs.

    Args:
        csv_text: Raw CSV contents (same string passed to parse_order_history).
        baseline_path: JSON file at data/cache/amazon_known_statuses.json (or
            equivalent). Parent directory must already exist.

    Returns:
        (new_order_statuses, new_shipment_statuses) — empty sets if no drift.
    """
    current_order, current_shipment = collect_dump_status_values(csv_text)

    first_run = not baseline_path.exists()
    if first_run:
        known_order: set[str] = set()
        known_shipment: set[str] = set()
    else:
        baseline = json.loads(baseline_path.read_text())
        known_order = set(baseline.get("order_statuses", []))
        known_shipment = set(baseline.get("shipment_statuses", []))

    new_order = set() if first_run else current_order - known_order
    new_shipment = set() if first_run else current_shipment - known_shipment

    if first_run or new_order or new_shipment:
        merged = {
            "order_statuses": sorted(known_order | current_order),
            "shipment_statuses": sorted(known_shipment | current_shipment),
        }
        baseline_path.write_text(json.dumps(merged, indent=2) + "\n")

    return new_order, new_shipment


def filter_shipments_to_window(
    shipments: list[AmazonShipment],
    since_date: date,
    date_window_days: int,
) -> list[AmazonShipment]:
    """Restrict shipments to those plausibly matchable to YNAB transactions in the window.

    A shipment is in-window if its ship_date is on or after `since_date - date_window_days`.
    Shipments without a ship_date are dropped. Shipments after today are kept (could be
    drop-shipped before the YNAB charge clears).

    This prevents the changeset from reporting historical excluded/unmatched shipments
    that are outside the user's working window (issue #154).
    """
    cutoff = since_date - timedelta(days=date_window_days)
    return [
        s for s in shipments
        if s.ship_date is not None and s.ship_date >= cutoff
    ]


def is_whole_foods_payee(payee: str | None) -> bool:
    """Return True if payee name looks like a Whole Foods delivery transaction."""
    if not payee:
        return False
    return "whole foods" in payee.lower()


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
    - approved is not True (not yet human-reviewed)
    - cleared != "reconciled" (reconciled cannot be edited via API)
    - deleted is not True

    Args:
        ynab_txns: List of YNAB transaction dicts

    Returns:
        List of filtered transactions
    """
    result = []

    for txn in ynab_txns:
        if not is_amazon_payee(txn.get("payee_name")):
            continue

        if txn.get("approved") is True:
            continue

        if txn.get("cleared") == "reconciled":
            continue

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
    # Indices dropped by Phase 2 for having zero candidates. These leave
    # unresolved_indices immediately, so Phase 4's zero-candidate check (which
    # only inspects what's still in unresolved_indices) can't see them — track
    # separately or they vanish from matched/unmatched_shipments/excluded_shipments.
    zero_candidate_removed_indices = set()

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
                zero_candidate_removed_indices.add(idx)
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

    # Unmatched shipments = those with zero candidates OR Case B contention.
    # Zero-candidate shipments come from two places: any still sitting in
    # unresolved_indices with no candidates (shouldn't happen post-loop, but
    # kept for safety), plus every index Phase 2 already evicted for having
    # zero candidates (zero_candidate_removed_indices) — those left
    # unresolved_indices immediately and would otherwise be lost.
    zero_candidate_indices = {idx for idx in unresolved_indices if len(candidates[idx][1]) == 0}
    zero_candidate_indices |= zero_candidate_removed_indices
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


# ============================================================================
# Phase E: Changeset Writer Helper Functions
# ============================================================================


def _json_default(obj):
    """Handle non-JSON-serializable types in json.dumps().

    Converts:
    - Decimal → string
    - date → ISO8601 string
    - datetime → ISO8601 string with time
    - dataclass instances → dict (via dataclasses.asdict)

    Args:
        obj: Object to serialize

    Returns:
        JSON-serializable form

    Raises:
        TypeError: If object type cannot be serialized
    """
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"Unknown type {type(obj).__name__} in JSON serialization")


def _md_escape(s: str) -> str:
    """Escape user-controlled strings for markdown table cells.

    Replaces:
    - Pipe `|` with `\\|`
    - Newlines with space
    - Carriage returns with nothing (strip)
    - Strips leading/trailing whitespace

    Args:
        s: User-controlled string

    Returns:
        Escaped and trimmed string
    """
    if not s:
        return s
    s = s.replace("|", "\\|").replace("\n", " ").replace("\r", "")
    return s.strip()


def _next_free_path(base: Path, suffix: str) -> Path:
    """Return a non-existent file path, appending microsecond suffix if needed.

    If base.with_suffix(suffix) doesn't exist, returns it. Otherwise appends
    a microsecond suffix (nnnnnn) to avoid collision.

    Args:
        base: Base path without suffix (e.g., Path("out/amazon-changeset-20260421-153000"))
        suffix: File suffix (e.g., ".md" or ".json")

    Returns:
        Path to a non-existent file

    Raises:
        RuntimeError: If unable to find a free path (highly unlikely)
    """
    candidate = base.with_suffix(suffix)
    if not candidate.exists():
        return candidate

    for micro in range(1, 1_000_000):
        candidate = base.parent / f"{base.name}-{micro:06d}{suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Cannot find free path for {base}")


def _reject_nan_inf(label: str, value: Decimal) -> None:
    """Raise RuntimeError if Decimal value is NaN or Infinity."""
    if value.is_nan() or value.is_infinite():
        raise RuntimeError(f"Invalid Decimal {value} at {label}")


def _validate_invariants(
    split_proposals,
    unmatched_amazon,
    *,
    match_result: "MatchResult | None" = None,
) -> None:
    """Validate changeset invariants before writing.

    Checks:
    1. Each split proposal's allocated total equals parent YNAB amount (exact equality)
    2. No transaction ID appears in multiple buckets
    3. No NaN or Infinity Decimals in monetary fields, across:
       - subtxn.allocated_amount
       - shipment.total_amount (split proposals, unmatched, excluded)
       - item.unit_price and item.unit_price_tax (all items in every shipment)
       - parent_ynab_txn["amount"] (parsed as Decimal)

    Args:
        split_proposals: list[AmazonSplitProposal]
        unmatched_amazon: list[tuple[dict, str]]
        match_result: MatchResult (optional). If given, its unmatched_shipments
            and excluded_shipments Decimals are also checked.

    Raises:
        RuntimeError: If any invariant is violated
    """
    for proposal in split_proposals:
        parent_id = proposal.parent_ynab_txn["id"]
        parent_amt_raw = Decimal(str(proposal.parent_ynab_txn["amount"]))
        _reject_nan_inf(f"parent_ynab_txn[{parent_id}].amount", parent_amt_raw)
        parent_amt = abs(parent_amt_raw) / Decimal(1000)

        _reject_nan_inf(
            f"split_proposal[{parent_id}].shipment.total_amount",
            proposal.shipment.total_amount,
        )
        for item in proposal.shipment.items:
            _reject_nan_inf(
                f"split_proposal[{parent_id}].shipment.item[{item.asin}].unit_price",
                item.unit_price,
            )
            _reject_nan_inf(
                f"split_proposal[{parent_id}].shipment.item[{item.asin}].unit_price_tax",
                item.unit_price_tax,
            )

        for subtxn in proposal.subtransactions:
            _reject_nan_inf(
                f"split_proposal[{parent_id}].allocated_amount",
                subtxn.allocated_amount,
            )

        total_allocated = sum(s.allocated_amount for s in proposal.subtransactions)
        if total_allocated != parent_amt:
            raise RuntimeError(
                f"Split proposal for txn {parent_id} "
                f"allocates {total_allocated} but parent is {parent_amt}"
            )

    if match_result is not None:
        for ship in match_result.unmatched_shipments:
            _reject_nan_inf(
                f"unmatched_shipment[{ship.order_id}].total_amount",
                ship.total_amount,
            )
            for item in ship.items:
                _reject_nan_inf(
                    f"unmatched_shipment[{ship.order_id}].item[{item.asin}].unit_price",
                    item.unit_price,
                )
                _reject_nan_inf(
                    f"unmatched_shipment[{ship.order_id}].item[{item.asin}].unit_price_tax",
                    item.unit_price_tax,
                )
        for ship, _ in match_result.excluded_shipments:
            _reject_nan_inf(
                f"excluded_shipment[{ship.order_id}].total_amount",
                ship.total_amount,
            )
            for item in ship.items:
                _reject_nan_inf(
                    f"excluded_shipment[{ship.order_id}].item[{item.asin}].unit_price",
                    item.unit_price,
                )
                _reject_nan_inf(
                    f"excluded_shipment[{ship.order_id}].item[{item.asin}].unit_price_tax",
                    item.unit_price_tax,
                )

    all_txn_ids = (
        {p.parent_ynab_txn["id"] for p in split_proposals}
        | {t["id"] for t, _ in unmatched_amazon}
    )
    expected_count = len(split_proposals) + len(unmatched_amazon)
    if len(all_txn_ids) != expected_count:
        raise RuntimeError(
            f"Duplicate transaction across buckets: {expected_count} entries, "
            f"{len(all_txn_ids)} unique txn IDs"
        )


def _resolve_account_name(
    txn: dict,
    account_name_lookup: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Resolve account name for a transaction.

    Returns account name if present in txn. Falls back to lookup by account_id.
    Finally falls back to account_id itself and signals a warning.

    Args:
        txn: YNAB transaction dict
        account_name_lookup: Optional {account_id: account_name} mapping

    Returns:
        (account_name_or_id, warning_needed)
        - warning_needed is True if fell back to account_id
    """
    if txn.get("account_name"):
        return txn["account_name"], False

    if account_name_lookup and txn.get("account_id") in account_name_lookup:
        return account_name_lookup[txn["account_id"]], False

    return txn.get("account_id", "unknown"), True


def _build_json_payload(
    match_result: MatchResult,
    split_proposals,
    unmatched_amazon,
    account_name_lookup: dict[str, str] | None = None,
    now: datetime | None = None,
) -> dict:
    """Build structured JSON payload for changeset.

    Args:
        match_result: MatchResult from phase B
        split_proposals: list[AmazonSplitProposal]
        unmatched_amazon: list[tuple[dict, str]] (txn, reason)
        account_name_lookup: Optional {account_id: account_name}
        now: Current datetime for generated_at

    Returns:
        Dict ready for JSON serialization via json.dumps with _json_default
    """
    if now is None:
        now = datetime.now()

    payload = {
        "version": 1,
        "generated_at": now,
        "summary": {
            "proposed_splits": len(split_proposals),
            "unmatched_ynab": len(unmatched_amazon),
            "unmatched_shipments": len(match_result.unmatched_shipments),
            "excluded_shipments": len(match_result.excluded_shipments),
            "parse_errors": len(match_result.parse_errors),
        },
        "proposed_splits": [],
        "unmatched_ynab": [],
        "unmatched_shipments": [],
        "excluded_shipments": [],
        "parse_errors": [],
    }

    for proposal in sorted(
        split_proposals, key=lambda p: (p.parent_ynab_txn["date"], p.parent_ynab_txn["id"])
    ):
        subtxns = []
        for sub in proposal.subtransactions:
            subtxns.append({
                "item": asdict(sub.item),
                "allocated_amount": sub.allocated_amount,
                "category_id": sub.category_id,
                "category_name": sub.category_name,
                "confidence": sub.confidence,
                "rationale": sub.rationale,
            })

        payload["proposed_splits"].append({
            "parent_ynab_transaction_id": proposal.parent_ynab_txn["id"],
            "parent_ynab_transaction": proposal.parent_ynab_txn,
            "shipment": {
                "order_id": proposal.shipment.order_id,
                "ship_date": proposal.shipment.ship_date,
                "payment_method_last4": proposal.shipment.payment_method_last4,
                "total_amount": proposal.shipment.total_amount,
                "item_count": len(proposal.shipment.items),
            },
            "subtransactions": subtxns,
        })


    for txn, reason in sorted(unmatched_amazon, key=lambda x: (x[1], x[0]["date"], x[0]["id"])):
        payload["unmatched_ynab"].append({
            "transaction": txn,
            "reason": reason,
        })

    for shipment in sorted(match_result.unmatched_shipments, key=lambda s: (s.order_id, s.ship_date is None, s.ship_date or date.min)):
        payload["unmatched_shipments"].append({
            "order_id": shipment.order_id,
            "ship_date": shipment.ship_date,
            "payment_method_last4": shipment.payment_method_last4,
            "total_amount": shipment.total_amount,
            "item_count": len(shipment.items),
        })

    for shipment, reason in sorted(match_result.excluded_shipments, key=lambda x: (x[0].order_id, x[0].ship_date is None, x[0].ship_date or date.min)):
        payload["excluded_shipments"].append({
            "shipment": {
                "order_id": shipment.order_id,
                "ship_date": shipment.ship_date,
                "payment_method_last4": shipment.payment_method_last4,
                "total_amount": shipment.total_amount,
                "item_count": len(shipment.items),
            },
            "reason": reason,
        })

    for error in match_result.parse_errors:
        payload["parse_errors"].append({
            "row_index": error.row_index,
            "reason": error.reason,
            "raw_row": getattr(error, "raw_row", None),
        })

    return payload


def _render_markdown(
    payload: dict,
    json_path: Path,
    account_name_lookup: dict[str, str] | None = None,
) -> str:
    """Render markdown from JSON payload.

    Args:
        payload: Dict structure from _build_json_payload
        json_path: Path to JSON file (for reference in markdown)
        account_name_lookup: Optional {account_id: account_name} for fallback

    Returns:
        Markdown string
    """
    summary = payload["summary"]
    generated_at = payload["generated_at"]
    if isinstance(generated_at, datetime):
        timestamp_str = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    else:
        timestamp_str = str(generated_at)

    md = f"# Amazon Changeset — {timestamp_str}\n\n"

    md += "## Summary\n\n"
    md += "| Category | Count |\n"
    md += "|---|---|\n"
    md += f"| Proposed splits | {summary['proposed_splits']} |\n"
    md += f"| Unmatched YNAB Amazon transactions | {summary['unmatched_ynab']} |\n"
    md += f"| Unmatched Amazon shipments | {summary['unmatched_shipments']} |\n"
    md += f"| Excluded shipments | {summary['excluded_shipments']} |\n"
    md += f"| Parse errors | {summary['parse_errors']} |\n\n"

    md += f"## Proposed splits ({summary['proposed_splits']})\n\n"
    for split in payload["proposed_splits"]:
        ship = split["shipment"]
        total_amt = Decimal(str(ship["total_amount"]))
        md += f"### {ship['order_id']} — {ship['ship_date']} — ${total_amt:.2f}\n"
        parent = split["parent_ynab_transaction"]
        parent_amount = abs(Decimal(str(parent["amount"]))) / Decimal(1000)
        parent_date = parent.get("date", "?")
        parent_payee = _md_escape(parent.get("payee_name", "?"))
        account_name, _ = _resolve_account_name(parent, account_name_lookup)
        md += f"Parent: {parent_date} | {account_name} | ${parent_amount:.2f} | {parent_payee}\n"
        md += f"Shipment: {ship['order_id']} | {ship['ship_date']} | {ship['item_count']} items\n\n"
        md += "| Item | ASIN | Qty | Allocated | Category | Confidence | Rationale |\n"
        md += "|---|---|---|---|---|---|---|\n"
        for sub in split["subtransactions"]:
            item = sub["item"]
            name = _md_escape(item["product_name"])
            asin = item["asin"]
            qty = item["quantity"]
            alloc = Decimal(str(sub["allocated_amount"]))
            cat = sub["category_name"] if sub["category_id"] else "—"
            conf = sub["confidence"]
            ratio = _md_escape(sub["rationale"])
            prefix = "[UNCATEGORIZED] " if sub["category_id"] is None else ""
            md += f"| {prefix}{name} | {asin} | {qty} | ${alloc:.2f} | {cat} | {conf:.2f} | {ratio} |\n"
        md += "\n"


    md += f"## Unmatched YNAB Amazon transactions ({summary['unmatched_ynab']})\n\n"
    reasons_map = {}
    for entry in payload["unmatched_ynab"]:
        reason = entry["reason"]
        if reason not in reasons_map:
            reasons_map[reason] = []
        reasons_map[reason].append(entry["transaction"])

    for reason in sorted(reasons_map.keys()):
        txns = reasons_map[reason]
        escaped_reason = _md_escape(reason)
        md += f"### {escaped_reason} ({len(txns)})\n"
        for txn in txns:
            date = txn.get("date", "?")
            account_name, _ = _resolve_account_name(txn, account_name_lookup)
            amount = abs(Decimal(str(txn.get("amount", 0)))) / Decimal(1000)
            txn_id = txn.get("id", "?")
            md += f"- {date} | {account_name} | ${amount:.2f} | txn_id: {txn_id}\n"
        md += "\n"

    md += f"## Unmatched Amazon shipments ({summary['unmatched_shipments']})\n\n"
    md += "These are Amazon shipments in the dump with no matching YNAB charge. Usually means YNAB hasn't synced recently or the transaction is in a closed account.\n\n"
    for ship in payload["unmatched_shipments"]:
        order_id = ship["order_id"]
        ship_date = ship["ship_date"] or "?"
        amount = Decimal(str(ship["total_amount"]))
        last4 = ship["payment_method_last4"] or "?????"
        md += f"- {order_id} | {ship_date} | ${amount:.2f} | last-4: {last4}\n"
    md += "\n"

    md += f"## Excluded shipments ({summary['excluded_shipments']})\n\n"
    excluded_map = {}
    for entry in payload["excluded_shipments"]:
        reason = entry["reason"]
        if reason not in excluded_map:
            excluded_map[reason] = []
        excluded_map[reason].append(entry["shipment"])

    for reason in sorted(excluded_map.keys()):
        ships = excluded_map[reason]
        escaped_reason = _md_escape(reason)
        md += f"### {escaped_reason} ({len(ships)})\n"
        for ship in ships:
            amount = Decimal(str(ship["total_amount"]))
            ship_date = ship["ship_date"] or "?"
            md += f"- {ship['order_id']} | {ship_date} | ${amount:.2f}\n"
        md += "\n"

    md += f"## Parse errors ({summary['parse_errors']})\n\n"
    errors = payload["parse_errors"]
    if len(errors) <= 50:
        for error in errors:
            md += f"- row {error['row_index']}: {error['reason']}\n"
    else:
        for error in errors[:50]:
            md += f"- row {error['row_index']}: {error['reason']}\n"
        remainder = len(errors) - 50
        md += f"\n... and {remainder} more parse errors. See JSON for full list.\n"

    md += "\n## How to apply\n\n"
    md += "Review this file carefully. When ready to apply, run:\n\n"
    md += f"    uv run python amazon_matcher.py --confirm {json_path}\n\n"
    md += "(Confirm command not yet implemented — see issue tracking the confirm step.)\n"
    md += f"Corresponding JSON: {json_path}\n"

    return md


def write_amazon_changeset(
    match_result: MatchResult,
    split_proposals,
    unmatched_amazon,
    *,
    account_name_lookup: dict[str, str] | None = None,
    out_dir: Path = Path("data/cache"),
    now: datetime | None = None,
) -> tuple[Path, Path]:
    """Write Amazon changeset to paired markdown + JSON artifacts.

    Output ordering is deterministic:
    - Splits sorted by parent transaction date and ID
    - Unmatched YNAB grouped by reason, then by date and ID
    - Unmatched/excluded shipments sorted by order_id then ship_date (None-safe)
    - Parse errors preserved in input order

    Args:
        match_result: MatchResult from match_shipments_to_transactions.
        split_proposals: list[AmazonSplitProposal] from categorizer. Every
            matched Amazon shipment is routed here regardless of item count.
        unmatched_amazon: list[tuple[dict, str]] (txn, reason) from orchestrator.
        account_name_lookup: Optional {account_id: account_name}.
        out_dir: Directory for output files (created if missing).
        now: Injectable clock for deterministic tests.

    Returns:
        (markdown_path, json_path)

    Raises:
        RuntimeError: If invariants violated or file cannot be written.
    """
    _validate_invariants(
        split_proposals, unmatched_amazon,
        match_result=match_result,
    )

    if now is None:
        now = datetime.now()

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = now.strftime("%Y%m%d-%H%M%S")
    base_path = out_dir / f"amazon-changeset-{timestamp}"

    payload = _build_json_payload(
        match_result,
        split_proposals,
        unmatched_amazon,
        account_name_lookup,
        now,
    )

    json_path = _next_free_path(base_path, ".json")
    md_path = json_path.with_suffix(".md")

    warn_account_name = False
    for proposal in split_proposals:
        if not proposal.parent_ynab_txn.get("account_name") and not account_name_lookup:
            warn_account_name = True
            break
    if not warn_account_name:
        for txn, _ in unmatched_amazon:
            if not txn.get("account_name") and not account_name_lookup:
                warn_account_name = True
                break

    if warn_account_name:
        logger.warning("Some transactions lack account_name and no lookup provided; using account_id")

    json_content = json.dumps(payload, default=_json_default, indent=2)
    json_path.write_text(json_content)

    markdown = _render_markdown(payload, json_path, account_name_lookup)
    md_path.write_text(markdown)

    return md_path, json_path


SUMMARY_LABEL_WIDTH = 16


def _fmt_summary_row(label: str, value: str) -> str:
    if len(label) > SUMMARY_LABEL_WIDTH:
        raise ValueError(
            f"Summary label {label!r} ({len(label)} chars) exceeds "
            f"SUMMARY_LABEL_WIDTH={SUMMARY_LABEL_WIDTH}. "
            f"Shorten the label or bump the constant."
        )
    return f"{label:<{SUMMARY_LABEL_WIDTH}}{value}"


def _print_summary(
    *,
    dump_path: Path,
    since_date: str,
    args_days: int,
    filtered: list,
    shipments: list,
    parse_errors_list: list,
    K: int,
    confidence_threshold: float,
    match_result,
    split_proposals: list,
    unmatched_amazon: list,
    md_path: Path,
    json_path: Path,
) -> None:
    """Print human-readable run summary."""
    n_matched = len(match_result.matched)
    n_unmatched_shipments = len(match_result.unmatched_shipments)
    n_unmatched_ynab = len(unmatched_amazon)
    n_excluded = len(match_result.excluded_shipments)
    n_multi = sum(1 for sp in split_proposals if len(sp.subtransactions) > 1)
    n_single = sum(1 for sp in split_proposals if len(sp.subtransactions) == 1)

    print()
    print("Amazon categorization run complete.")
    print()
    print(_fmt_summary_row("Dump:", str(dump_path)))
    print(_fmt_summary_row(
        "YNAB txns:",
        f"{len(filtered)} Amazon transactions in last {args_days} days (since {since_date})",
    ))
    print(_fmt_summary_row(
        "Shipments:",
        f"{len(shipments)} in dump ({len(parse_errors_list)} parse errors, {n_excluded} excluded)",
    ))
    print()
    print(_fmt_summary_row(
        "K:",
        f"{K} categories (last 18mo); confidence threshold: {confidence_threshold:.4f}",
    ))
    print(_fmt_summary_row("Matches:", f"{n_matched} shipments → YNAB"))
    print(_fmt_summary_row(
        "Splits:",
        f"{n_multi} multi-item, {n_single} single-item proposed",
    ))
    print(_fmt_summary_row(
        "Unmatched:",
        f"{n_unmatched_ynab} YNAB txns; {n_unmatched_shipments} shipments (see changeset)",
    ))
    print()
    print(_fmt_summary_row("Changeset:", str(md_path)))
    print(_fmt_summary_row("", str(json_path)))
    print()
    print("Review the markdown file before running confirm.")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for Amazon order matching."""
    import argparse
    import os

    parser = argparse.ArgumentParser(
        description="Match Amazon orders to YNAB transactions and propose item-level splits"
    )
    validate_group = parser.add_mutually_exclusive_group()
    validate_group.add_argument("--days", type=int, help="Days back to fetch YNAB transactions")
    validate_group.add_argument(
        "--validate-dump", type=Path, metavar="PATH",
        help="Validate dump file schema (Phase G, not yet implemented)"
    )
    parser.add_argument("--dump", type=Path, help="Override auto-detected dump path")
    parser.add_argument("--out-dir", type=Path, default=Path("data/cache"), help="Changeset output dir")

    args = parser.parse_args(argv)

    if args.validate_dump:
        print("--validate-dump not yet implemented (Phase G, issue #54)")
        return 2

    if args.days is None:
        parser.error("--days is required unless --validate-dump is provided")

    load_dotenv()

    ynab_token = os.getenv("YNAB_API_TOKEN")
    if not ynab_token:
        raise ValueError("YNAB_API_TOKEN not set. Add it to .env (see README).")

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY not set. Add it to .env (see README).")

    budget_name = os.getenv("YNAB_DEFAULT_BUDGET")
    if not budget_name:
        raise ValueError("YNAB_DEFAULT_BUDGET environment variable is required (set in .env)")

    # config.json is optional; only the 'amazon' section is read when present
    config_path = Path("config.json")
    config = {}
    if config_path.exists():
        config = json.loads(config_path.read_text())

    amazon_cfg = config.get("amazon", {})
    date_window_days = amazon_cfg.get("date_window_days", 3)

    from ynab_client import YNABClient
    from categorizer import (
        count_categories_from_transactions, count_categories,
        compute_confidence_threshold, filter_categories_by_usage,
        load_payee_cache, build_cache_from_transactions, save_payee_cache,
        record_categorization, categorize_transactions,
    )

    if args.dump:
        dump_path = args.dump
    else:
        try:
            dump_path = find_latest_dump()
        except FileNotFoundError:
            raise FileNotFoundError(
                "No Amazon dump found in data/imports/. "
                "Expected: amazon-order-history-YYYY-MM-DD.zip"
            )
    print(f"Using dump: {dump_path}")

    csv_text = extract_order_history_csv(dump_path)
    shipments, parse_errors_list = parse_order_history(csv_text)
    print(f"Parsed {len(shipments)} shipments ({len(parse_errors_list)} parse errors)")

    client = YNABClient(token=ynab_token)
    budget_id = client.resolve_budget_id(budget_name)
    since_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    txns, _ = client.get_transactions(budget_id, since_date=since_date)

    filtered = filter_amazon_transactions(txns)
    print(f"Found {len(filtered)} uncategorized Amazon transactions since {since_date}")

    categories = client.get_categories(budget_id)
    accounts = client.get_accounts(budget_id)
    account_name_lookup = {a["id"]: a["name"] for a in accounts}

    k_since = (datetime.now() - timedelta(days=548)).strftime("%Y-%m-%d")
    k_txns, _ = client.get_transactions(budget_id, since_date=k_since)
    K = count_categories_from_transactions(k_txns)
    if K < 2:
        K = count_categories(categories)
    confidence_threshold = compute_confidence_threshold(K)
    recent_categories = filter_categories_by_usage(categories, k_txns)

    match_result = match_shipments_to_transactions(
        filtered, shipments,
        parse_errors=parse_errors_list,
        date_window_days=date_window_days,
    )

    cache = load_payee_cache()
    if not cache:
        print("Cache empty — bootstrapping from YNAB history...")
        all_txns, _ = client.get_transactions(budget_id)
        cache = build_cache_from_transactions(all_txns)
        if sum(1 for k in cache if k != "_version") > 0:
            save_payee_cache(cache)
    elif cache.get("_migrated_from_v1"):
        print("Cache migrated from v1 — rebuilding...")
        all_txns, _ = client.get_transactions(budget_id)
        cache = build_cache_from_transactions(all_txns)
        save_payee_cache(cache)

    results, skipped, unmatched_amazon, split_proposals = categorize_transactions(
        filtered, cache, recent_categories, anthropic_key,
        K=K, confidence_threshold=confidence_threshold,
        amazon_matches=match_result,
    )

    md_path, json_path = write_amazon_changeset(
        match_result, split_proposals, unmatched_amazon,
        account_name_lookup=account_name_lookup,
        out_dir=args.out_dir,
    )

    for result in results:
        if result.tier == "claude":
            txn = next((t for t in filtered if t["id"] == result.transaction_id), None)
            if txn:
                import_names = [
                    txn[f] for f in ("import_payee_name", "import_payee_name_original")
                    if txn.get(f)
                ]
                record_categorization(
                    cache, txn["payee_name"],
                    result.category_id, result.category_name,
                    source="claude",
                    prior_strength=result.prior_strength or 1,
                    import_names=import_names or None,
                )
    save_payee_cache(cache)

    _print_summary(
        dump_path=dump_path,
        since_date=since_date,
        args_days=args.days,
        filtered=filtered,
        shipments=shipments,
        parse_errors_list=parse_errors_list,
        K=K,
        confidence_threshold=confidence_threshold,
        match_result=match_result,
        split_proposals=split_proposals,
        unmatched_amazon=unmatched_amazon,
        md_path=md_path,
        json_path=json_path,
    )

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
