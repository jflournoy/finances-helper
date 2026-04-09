"""YNAB API wrapper.

Handles authentication, request batching, milliunit conversions,
date handling, response parsing, and error handling.
"""
import os
import requests


def dollars_to_milliunits(amount: float) -> int:
    """Convert dollars to YNAB integer milliunits (1000 = $1.00).

    Truncates sub-milliunit amounts (e.g., $10.9999 → 10999 milliunits).
    """
    return int(amount * 1000)


def milliunits_to_dollars(milliunits: int) -> float:
    """Convert YNAB integer milliunits to dollars."""
    return milliunits / 1000.0


class YNABClient:
    """YNAB API client with authentication and request handling."""

    def __init__(self, token: str | None = None):
        """Initialize the YNAB client.

        Args:
            token: YNAB API token. If None, loads from YNAB_API_TOKEN environment variable.

        Raises:
            ValueError: If no token is provided and YNAB_API_TOKEN is not set.
        """
        if token is None:
            token = os.environ.get("YNAB_API_TOKEN")
            if not token:
                raise ValueError("YNAB_API_TOKEN must be provided as argument or in environment")

        self.base_url = "https://api.ynab.com/v1"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })
