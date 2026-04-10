"""Claude-powered transaction categorization.

Three-tier categorization strategy:
1. History lookup — resolve from payee cache
2. Fuzzy match — catch payee name variations
3. Claude (Haiku, batched) — genuinely novel payees only
"""
import re
import json
from dataclasses import dataclass
from pathlib import Path
from thefuzz import fuzz, process
import anthropic
from ynab_client import milliunits_to_dollars


@dataclass
class CategoryResult:
    """Result of categorization for a single transaction."""
    transaction_id: str
    category_id: str
    category_name: str
    confidence: float      # 0.0 to 1.0
    rationale: str         # Brief explanation
    tier: str              # "history", "fuzzy", or "claude"


FUZZY_THRESHOLD = 85   # thefuzz token_set_ratio score (0-100)
CLAUDE_BATCH_SIZE = 50


def normalize_payee(name: str) -> str:
    """Normalize a payee name for consistent cache lookups.

    1. Strip leading/trailing whitespace
    2. Lowercase
    3. Iteratively remove trailing store/location codes (#1234, *1A2B, - NYC)
    4. Collapse multiple spaces to single space
    5. Strip trailing punctuation

    Raises ValueError if name is None, empty, or whitespace-only after stripping.
    """
    if name is None:
        raise ValueError("Payee name cannot be None")

    name = name.strip()
    if not name:
        raise ValueError("Payee name cannot be empty or whitespace-only")

    name = name.lower()

    # Iteratively remove trailing codes (#1234, *1A2B, - NYC)
    prev = None
    while prev != name:
        prev = name
        name = re.sub(r'\s*[#*-]\s*\S+$', '', name)

    # Collapse multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()

    # Remove trailing punctuation
    name = name.rstrip('.,!?;:')

    if not name:
        raise ValueError("Payee name cannot be empty after normalization")

    return name


def load_payee_cache(path: str = "data/cache/payee_lookup.json") -> dict:
    """Load the payee cache from disk.

    Args:
        path: Path to the cache JSON file

    Returns:
        Cache dict mapping normalized payee names to category info.
        Returns empty dict if file doesn't exist (first-run case).

    Raises:
        ValueError: If file exists but contains invalid JSON.
    """
    cache_path = Path(path)
    if not cache_path.exists():
        return {}

    try:
        content = cache_path.read_text()
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {path}: {e}")


def save_payee_cache(cache: dict, path: str = "data/cache/payee_lookup.json") -> None:
    """Save the payee cache to disk.

    Args:
        cache: Cache dict to save
        path: Path to write the cache JSON file
    """
    cache_path = Path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))


def build_cache_from_transactions(transactions: list[dict]) -> dict:
    """Build a payee cache from already-categorized YNAB transactions.

    Filters to transactions with both payee_name and category_id,
    groups by normalized payee name, and takes the most recent
    category assignment for each payee.

    Args:
        transactions: List of YNAB transaction dicts

    Returns:
        Cache dict mapping normalized payee names to category info.
    """
    cache = {}

    # Filter and group by normalized payee name
    for txn in transactions:
        payee = txn.get("payee_name")
        cat_id = txn.get("category_id")
        cat_name = txn.get("category_name")
        date = txn.get("date")

        # Skip uncategorized or missing payee
        if not payee or not cat_id:
            continue

        # Normalize the payee name
        normalized = normalize_payee(payee)

        # Store or update if this is more recent
        if normalized not in cache:
            cache[normalized] = {
                "category_id": cat_id,
                "category_name": cat_name,
                "date": date,  # Track for sorting
            }
        else:
            # Keep the most recent
            if date and cache[normalized].get("date"):
                if date > cache[normalized]["date"]:
                    cache[normalized] = {
                        "category_id": cat_id,
                        "category_name": cat_name,
                        "date": date,
                    }
            elif date:
                cache[normalized] = {
                    "category_id": cat_id,
                    "category_name": cat_name,
                    "date": date,
                }

    # Remove the date field from final cache
    for key in cache:
        del cache[key]["date"]

    return cache


def history_lookup(payee_name: str, cache: dict) -> CategoryResult | None:
    """Look up a payee in the cache using exact match on normalized name.

    Args:
        payee_name: Raw payee name to look up
        cache: Cache dict mapping normalized payee names to category info

    Returns:
        CategoryResult with confidence=1.0 if found, None otherwise.
    """
    normalized = normalize_payee(payee_name)
    if normalized not in cache:
        return None

    entry = cache[normalized]
    return CategoryResult(
        transaction_id="",
        category_id=entry["category_id"],
        category_name=entry["category_name"],
        confidence=1.0,
        rationale="Exact history match",
        tier="history",
    )


def fuzzy_match(payee_name: str, cache: dict, threshold: int = FUZZY_THRESHOLD) -> CategoryResult | None:
    """Find a close match in the cache using fuzzy matching.

    Args:
        payee_name: Raw payee name to match
        cache: Cache dict mapping normalized payee names to category info
        threshold: Minimum score (0-100) to consider a match

    Returns:
        CategoryResult with confidence=score/100.0 if match found, None otherwise.
    """
    if not cache:
        return None

    normalized = normalize_payee(payee_name)
    best_match, score = process.extractOne(
        normalized, cache.keys(), scorer=fuzz.token_set_ratio
    )

    if score < threshold:
        return None

    entry = cache[best_match]
    return CategoryResult(
        transaction_id="",
        category_id=entry["category_id"],
        category_name=entry["category_name"],
        confidence=score / 100.0,
        rationale=f"Fuzzy match to '{best_match}' (score: {score})",
        tier="fuzzy",
    )


def claude_categorize(transactions: list[dict], categories: list[dict], api_key: str) -> list[CategoryResult]:
    """Categorize transactions using Claude Haiku.

    Args:
        transactions: List of YNAB transaction dicts to categorize
        categories: List of YNAB category group dicts (for context)
        api_key: Anthropic API key

    Returns:
        List of CategoryResult objects in same order as input transactions.

    Raises:
        ValueError: If JSON response is unparseable or length mismatch.
    """
    if not transactions:
        return []

    # Build category list for system prompt
    category_text = ""
    for group in categories:
        category_text += f"{group['name']}:\n"
        for cat in group.get("categories", []):
            category_text += f"  - {cat['name']} (ID: {cat['id']})\n"

    # Build user message with transactions
    txn_list = ""
    for i, txn in enumerate(transactions, 1):
        payee = txn.get("payee_name", "Unknown")
        amount = milliunits_to_dollars(txn.get("amount", 0))
        date = txn.get("date", "Unknown")
        txn_list += f"{i}. Payee: {payee}, Amount: ${amount:.2f}, Date: {date}\n"

    system_prompt = f"""You are a financial transaction categorizer. Given a list of transactions, assign each one to the most appropriate budget category.

Available categories:
{category_text}

Respond with a JSON array, one object per transaction, in the same order as provided:
[
  {{
    "payee_name": "the payee name from the transaction",
    "category_id": "the category UUID",
    "category_name": "the category name",
    "confidence": 0.95,
    "rationale": "brief reason for this category"
  }},
  ...
]

Only use category IDs from the list above. Return ONLY the JSON array, no other text."""

    user_message = f"Categorize these transactions:\n{txn_list}"

    # Call Claude Haiku
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    # Parse response
    response_text = response.content[0].text
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned unparseable JSON: {e}")

    if not isinstance(data, list):
        raise ValueError("Claude response is not a JSON array")

    if len(data) != len(transactions):
        raise ValueError(f"Claude returned {len(data)} results for {len(transactions)} transactions")

    # Build results
    results = []
    for i, item in enumerate(data):
        txn = transactions[i]
        results.append(CategoryResult(
            transaction_id=txn["id"],
            category_id=item["category_id"],
            category_name=item["category_name"],
            confidence=item["confidence"],
            rationale=item["rationale"],
            tier="claude",
        ))

    return results
