"""YNAB API wrapper.

Handles authentication, request batching, milliunit conversions,
date handling, response parsing, and error handling.
"""


def dollars_to_milliunits(amount: float) -> int:
    """Convert dollars to YNAB integer milliunits (1000 = $1.00).

    Truncates sub-milliunit amounts (e.g., $10.9999 → 10999 milliunits).
    """
    return int(amount * 1000)


def milliunits_to_dollars(milliunits: int) -> float:
    """Convert YNAB integer milliunits to dollars."""
    return milliunits / 1000.0
