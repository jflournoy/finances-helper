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


class YNABConflictError(YNABAPIError):
    """Raised when YNAB rejects a write due to concurrent edit (409)."""
    pass


class YNABValidationError(YNABAPIError):
    """Raised when YNAB rejects a write due to validation (400):
    split sum mismatch, locked transaction, invalid category, etc.
    """
    pass


def dollars_to_milliunits(amount: float) -> int:
    """Convert dollars to YNAB integer milliunits (1000 = $1.00).

    Truncates sub-milliunit amounts (e.g., $10.9999 → 10999 milliunits).
    """
    return int(amount * 1000)


def milliunits_to_dollars(milliunits: int) -> float:
    """Convert YNAB integer milliunits to dollars."""
    return milliunits / 1000.0


def _with_amount_dollars(txn: dict) -> dict:
    """Return a shallow copy of a YNAB transaction with amount_dollars injected.

    Converts the milliunit `amount` to dollars at the API boundary so downstream
    code never has to do milliunit math. Recurses into `subtransactions` so each
    split also gets `amount_dollars`.
    """
    out = {**txn, "amount_dollars": milliunits_to_dollars(txn.get("amount", 0))}
    subs = txn.get("subtransactions")
    if isinstance(subs, list):
        out["subtransactions"] = [
            {**s, "amount_dollars": milliunits_to_dollars(s.get("amount", 0))}
            for s in subs
        ]
    return out


def filter_uncategorized_writable(ynab_txns: list[dict]) -> list[dict]:
    """Return unapproved transactions the YNAB API allows us to write to.

    Excludes:
    - approved is True (already human-reviewed)
    - cleared == "reconciled" (locked, API rejects edits)
    - deleted is True
    """
    return [
        t for t in ynab_txns
        if t.get("approved") is not True
        and t.get("cleared") != "reconciled"
        and t.get("deleted") is not True
    ]


SANDBOX_BUDGET_NAME = "Sandbox"
SANDBOX_ACCOUNT_NAME = "Sandbox"


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

        self.sandbox_mode = os.environ.get("YNAB_SANDBOX_MODE", "").strip() == "1"
        self._sandbox_budget_id: str | None = None
        self._sandbox_account_id: str | None = None
        self._sandbox_initialized = False

    def _init_sandbox(self) -> None:
        """Resolve and cache the Sandbox budget and account IDs.

        Raises:
            ValueError: If the Sandbox budget or account cannot be found.
        """
        self._sandbox_budget_id = self.resolve_budget_id(SANDBOX_BUDGET_NAME)
        accounts = self.get_accounts(self._sandbox_budget_id)
        matches = [a for a in accounts if a.get("name") == SANDBOX_ACCOUNT_NAME]
        if not matches:
            available = [a.get("name") for a in accounts]
            raise ValueError(
                f"Sandbox account {SANDBOX_ACCOUNT_NAME!r} not found in budget {SANDBOX_BUDGET_NAME!r}. "
                f"Available accounts: {available}"
            )
        self._sandbox_account_id = matches[0]["id"]
        print(
            f"[SANDBOX MODE] All writes redirected to budget={SANDBOX_BUDGET_NAME!r} "
            f"account={SANDBOX_ACCOUNT_NAME!r} ({self._sandbox_account_id})"
        )

    def _ensure_sandbox_initialized(self) -> None:
        """Run sandbox init exactly once. Idempotent and safe to call before any write.

        Raises:
            ValueError: If the Sandbox budget or account cannot be found in YNAB.
        """
        if self._sandbox_initialized:
            return
        self._init_sandbox()
        self._sandbox_initialized = True

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

    def _post(self, path: str, payload: dict) -> dict:
        """Make a POST request to the YNAB API.

        Callers that write in sandbox mode must call _ensure_sandbox_initialized() before
        calling _post(), so that budget/account IDs are redirected. Currently only
        create_transactions() does this. Add the call to any future write methods too.

        Raises:
            YNABNotFoundError: On 404
            YNABRateLimitError: On 429
            YNABAPIError: On other errors
        """
        url = f"{self.base_url}{path}"
        response = self.session.post(url, json=payload)

        try:
            body = response.json()
        except ValueError:
            raise YNABAPIError(response.status_code, detail=f"Malformed JSON response: {response.text}")

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

        if "data" not in body:
            raise YNABAPIError(response.status_code, detail="Response missing 'data' key")

        return body["data"]

    def _patch(self, path: str, payload: dict) -> dict:
        """Make a PATCH request to the YNAB API.

        Raises:
            YNABValidationError: On 400 (bad request, split mismatch, locked transaction, etc.)
            YNABConflictError: On 409 (concurrent edit)
            YNABNotFoundError: On 404
            YNABRateLimitError: On 429
            YNABAPIError: On other errors
        """
        url = f"{self.base_url}{path}"
        response = self.session.patch(url, json=payload)
        self._record_rate_limit(response)

        try:
            body = response.json()
        except ValueError:
            raise YNABAPIError(response.status_code, detail=f"Malformed JSON response: {response.text}")

        if response.status_code >= 400:
            error_data = body.get("error", {})
            status = response.status_code
            error_id = error_data.get("id", "")
            error_name = error_data.get("name", "")
            error_detail = error_data.get("detail", "")

            if status == 400:
                raise YNABValidationError(status, error_id, error_name, error_detail)
            elif status == 404:
                raise YNABNotFoundError(status, error_id, error_name, error_detail)
            elif status == 409:
                raise YNABConflictError(status, error_id, error_name, error_detail)
            elif status == 429:
                raise YNABRateLimitError(status, error_id, error_name, error_detail)
            else:
                raise YNABAPIError(status, error_id, error_name, error_detail)

        if "data" not in body:
            raise YNABAPIError(response.status_code, detail="Response missing 'data' key")

        return body["data"]

    def _record_rate_limit(self, response) -> None:
        """Record rate limit information from response headers.

        Stub for Phase 3 (rate-limit header parsing). Currently a no-op.
        """
        pass

    def create_transactions(self, budget_id: str, transactions: list[dict]) -> dict:
        """Create one or more transactions in a budget.

        Each transaction dict must include at minimum: account_id, date, amount.
        In sandbox mode, budget_id and every account_id are redirected to the
        Sandbox budget/account and a warning is printed for each call. Sandbox budget/account
        IDs are lazily resolved on first write.

        Args:
            budget_id: Target budget ID (ignored in sandbox mode).
            transactions: List of transaction dicts per YNAB API schema.

        Returns:
            The YNAB API response data dict (contains 'transactions', 'duplicate_import_ids', etc.)
        """
        if not transactions:
            raise ValueError("transactions must not be empty")

        write_budget_id = budget_id
        if self.sandbox_mode:
            self._ensure_sandbox_initialized()
            write_budget_id = self._sandbox_budget_id
            print(
                f"[SANDBOX MODE] Redirecting write: budget {budget_id} → {write_budget_id}, "
                f"account(s) → {self._sandbox_account_id}"
            )
            transactions = [
                {**t, "account_id": self._sandbox_account_id}
                for t in transactions
            ]

        return self._post(f"/budgets/{write_budget_id}/transactions", {"transactions": transactions})

    def get_budgets(self) -> list:
        """Get all budgets."""
        data = self._get("/budgets")
        return data.get("budgets", [])

    def get_budget(self, budget_id: str) -> dict:
        """Get a single budget by ID."""
        data = self._get(f"/budgets/{budget_id}")
        return data.get("budget", {})

    def resolve_budget_id(self, name_or_id: str) -> str:
        """Resolve YNAB_DEFAULT_BUDGET to a budget UUID.

        Treats the input as a name and looks it up against /budgets. If exactly
        one budget matches, returns its id. Raises ValueError on no match or
        multiple matches with a list of available names so the user can fix
        their .env.

        A bare UUID is treated as a name too — if the user has a budget literally
        named like a UUID this works; otherwise it falls through to the
        not-found path with the available-names list.
        """
        budgets = self.get_budgets()
        matches = [b for b in budgets if b.get("name") == name_or_id]
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            available = [b.get("name") for b in budgets]
            raise ValueError(
                f"YNAB_DEFAULT_BUDGET={name_or_id!r} not found. Available: {available}"
            )
        raise ValueError(
            f"YNAB_DEFAULT_BUDGET={name_or_id!r} matches {len(matches)} budgets — "
            "rename one in YNAB to disambiguate."
        )

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
        filtered = [_with_amount_dollars(t) for t in transactions if not t.get("deleted", False)]
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
        filtered = [_with_amount_dollars(t) for t in transactions if not t.get("deleted", False)]
        server_knowledge = data.get("server_knowledge", 0)

        return filtered, server_knowledge
