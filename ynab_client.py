"""YNAB API wrapper.

Handles authentication, request batching, milliunit conversions,
date handling, response parsing, and error handling.
"""
import os
import requests


# Custom exceptions

class YNABAPIError(Exception):
    """Base exception for YNAB API errors."""

    def __init__(self, status_code: int | None = None, id: str | None = None, name: str | None = None, detail: str | None = None):
        self.status_code = status_code
        self.id = id
        self.name = name
        self.detail = detail
        super().__init__(detail or name or f"YNAB API error (status {status_code})")


class YNABNotFoundError(YNABAPIError):
    """Raised when a requested resource is not found (404)."""
    pass


class YNABRateLimitError(YNABAPIError):
    """Raised when rate limit is exceeded (429)."""
    pass


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

    def _get(self, path: str, params: dict | None = None) -> dict:
        """Make a GET request to the YNAB API.

        Args:
            path: API path (e.g., "/budgets")
            params: Optional query parameters

        Returns:
            The unwrapped response data dict

        Raises:
            YNABNotFoundError: On 404
            YNABRateLimitError: On 429
            YNABAPIError: On other errors
        """
        url = f"{self.base_url}{path}"
        response = self.session.get(url, params=params)

        # Parse response body
        try:
            body = response.json()
        except ValueError:
            raise YNABAPIError(response.status_code, detail=f"Malformed JSON response: {response.text}")

        # Handle error responses
        if response.status_code >= 400:
            error_data = body.get("error", {})
            status = response.status_code
            error_id = error_data.get("id", "")
            error_name = error_data.get("name", "")
            error_detail = error_data.get("detail", "")

            if status == 404:
                raise YNABNotFoundError(status, error_id, error_name, error_detail)
            elif status == 429:
                raise YNABRateLimitError(status, error_id, error_name, error_detail)
            else:
                raise YNABAPIError(status, error_id, error_name, error_detail)

        # Parse success response
        if "data" not in body:
            raise YNABAPIError(response.status_code, detail="Response missing 'data' key")

        return body["data"]

    def get_budgets(self) -> list:
        """Get all budgets."""
        data = self._get("/budgets")
        return data.get("budgets", [])

    def get_budget(self, budget_id: str) -> dict:
        """Get a single budget by ID."""
        data = self._get(f"/budgets/{budget_id}")
        return data.get("budget", {})

    def get_accounts(self, budget_id: str) -> list:
        """Get accounts for a budget, filtering out deleted accounts."""
        data = self._get(f"/budgets/{budget_id}/accounts")
        accounts = data.get("accounts", [])
        return [a for a in accounts if not a.get("deleted", False)]

    def get_categories(self, budget_id: str) -> list:
        """Get category groups for a budget, with nested categories.

        Returns list of category group dicts, each with a 'categories' list.
        Filters out deleted category groups and deleted categories within groups.
        Hidden categories are included (filtering is the caller's job).
        """
        data = self._get(f"/budgets/{budget_id}/categories")
        category_groups = data.get("category_groups", [])

        result = []
        for group in category_groups:
            if group.get("deleted", False):
                continue

            filtered_categories = [c for c in group.get("categories", []) if not c.get("deleted", False)]
            # Return full group dict with only the categories list replaced (filtered)
            group_dict = {**group, "categories": filtered_categories}
            result.append(group_dict)

        return result

    def get_payees(self, budget_id: str) -> list:
        """Get payees for a budget, filtering out deleted payees."""
        data = self._get(f"/budgets/{budget_id}/payees")
        payees = data.get("payees", [])
        return [p for p in payees if not p.get("deleted", False)]

    def get_transactions(
        self,
        budget_id: str,
        since_date: str | None = None,
        type: str | None = None,
        last_knowledge_of_server: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get transactions for a budget, returning (transactions, server_knowledge).

        Args:
            budget_id: Budget ID
            since_date: Optional ISO 8601 date filter
            type: Optional transaction type filter
            last_knowledge_of_server: Optional server knowledge for delta sync

        Returns:
            Tuple of (filtered_transactions, server_knowledge)
        """
        params = {}
        if since_date is not None:
            params["since_date"] = since_date
        if type is not None:
            params["type"] = type
        if last_knowledge_of_server is not None:
            params["last_knowledge_of_server"] = last_knowledge_of_server

        data = self._get(f"/budgets/{budget_id}/transactions", params=params if params else None)
        transactions = data.get("transactions", [])
        filtered = [t for t in transactions if not t.get("deleted", False)]
        server_knowledge = data.get("server_knowledge", 0)

        return filtered, server_knowledge

    def get_account_transactions(
        self,
        budget_id: str,
        account_id: str,
        since_date: str | None = None,
        type: str | None = None,
        last_knowledge_of_server: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get transactions for a specific account, returning (transactions, server_knowledge).

        Args:
            budget_id: Budget ID
            account_id: Account ID
            since_date: Optional ISO 8601 date filter
            type: Optional transaction type filter
            last_knowledge_of_server: Optional server knowledge for delta sync

        Returns:
            Tuple of (filtered_transactions, server_knowledge)
        """
        params = {}
        if since_date is not None:
            params["since_date"] = since_date
        if type is not None:
            params["type"] = type
        if last_knowledge_of_server is not None:
            params["last_knowledge_of_server"] = last_knowledge_of_server

        data = self._get(
            f"/budgets/{budget_id}/accounts/{account_id}/transactions",
            params=params if params else None
        )
        transactions = data.get("transactions", [])
        filtered = [t for t in transactions if not t.get("deleted", False)]
        server_knowledge = data.get("server_knowledge", 0)

        return filtered, server_knowledge
