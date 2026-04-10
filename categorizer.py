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


FUZZY_THRESHOLD = 70   # thefuzz token_sort_ratio score (0-100), lowered from 85
CLAUDE_BATCH_SIZE = 50


def normalize_payee(name: str) -> str:
    """Normalize a payee name for consistent cache lookups.

    1. Strip leading/trailing whitespace
    2. Lowercase
    3. Strip trailing #codes; strip trailing *codes only if code contains digits
    4. Strip trailing dash+number codes (ASCII and unicode dashes; preserves dash+word)
    5. Collapse multiple spaces to single space
    6. Strip trailing punctuation (including dashes)

    Raises ValueError if name is None, empty, or whitespace-only after stripping.
    """
    if name is None:
        raise ValueError("Payee name cannot be None")

    name = name.strip()
    if not name:
        raise ValueError("Payee name cannot be empty or whitespace-only")

    name = name.lower()

    # Strip trailing # codes (WHOLE FOODS #1234, TARGET #1234)
    name = re.sub(r'\s*#\w+$', '', name)
    # NOTE (#32): Mid-string # codes (e.g. "TRADER JOE'S #123 - Portland") are intentionally
    # NOT stripped. End-anchoring avoids destroying POS prefixes. The fuzzy matcher handles
    # residual store numbers (fuzzy_score gives 91 for #123 vs no-#123 variants).
    # Location variants (Portland vs Seattle) are separate cache entries by design.

    # Strip trailing * codes only if code contains digits (AMZN*1A2B3C)
    # Preserves PP*SPOTIFY, AT&T*WIRELESS (pure alpha after *)
    name = re.sub(r'\s*\*(?=\w*\d)\w+$', '', name)
    # Strip trailing dash+number codes (ASCII + unicode dashes)
    name = re.sub(r'\s+[-\u2013\u2014]{1,2}\s*\d[\w]*$', '', name)

    # Collapse spaces
    name = re.sub(r'\s+', ' ', name).strip()
    # Strip trailing punctuation (including dash for artifacts like "refund -")
    name = name.rstrip('.,!?;:-').strip()

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


def fuzzy_score(query: str, candidate: str) -> int:
    """Score two strings for similarity using token_sort_ratio with length penalty.

    Uses token_sort_ratio (order-independent, full-sequence comparison) with a
    length-ratio penalty when one string is less than half the length of the other.
    This prevents short strings from falsely matching long ones (e.g., "transfer"
    matching "transfer : classic checking").
    """
    score = fuzz.token_sort_ratio(query, candidate)
    len_ratio = min(len(query), len(candidate)) / max(len(query), len(candidate))
    if len_ratio < 0.5:
        score = int(score * len_ratio)
    return score


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
        normalized, cache.keys(), scorer=fuzzy_score
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
    # Each response item is ~80-100 tokens; scale max_tokens with batch size
    max_tokens = max(1024, len(transactions) * 100)
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
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
    required_fields = {"category_id", "category_name", "confidence", "rationale"}
    for i, item in enumerate(data):
        missing = required_fields - set(item.keys())
        if missing:
            raise ValueError(f"Response item {i} missing fields: {', '.join(sorted(missing))}")

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


def categorize_transactions(
    transactions: list[dict],
    cache: dict,
    categories: list[dict],
    api_key: str,
) -> list[CategoryResult]:
    """Orchestrate the three-tier categorization for a list of transactions.

    Args:
        transactions: List of uncategorized YNAB transaction dicts
        cache: Payee cache dict
        categories: List of category group dicts
        api_key: Anthropic API key

    Returns:
        List of CategoryResult objects from all three tiers.

    Raises:
        ValueError: If any transaction lacks a payee_name.
    """
    results = []
    tier3_pending = []

    for txn in transactions:
        payee = txn.get("payee_name")
        if not payee:
            raise ValueError(f"Transaction {txn.get('id')} has no payee_name")

        # Try tier 1
        result = history_lookup(payee, cache)
        if result is None:
            # Try tier 2
            result = fuzzy_match(payee, cache)
        if result is None:
            # Queue for tier 3
            tier3_pending.append(txn)
            continue

        result.transaction_id = txn["id"]
        results.append(result)

    # Batch tier 3 Claude calls in chunks of CLAUDE_BATCH_SIZE
    if tier3_pending:
        for i in range(0, len(tier3_pending), CLAUDE_BATCH_SIZE):
            batch = tier3_pending[i:i + CLAUDE_BATCH_SIZE]
            tier3_results = claude_categorize(batch, categories, api_key)
            results.extend(tier3_results)

    return results


def main():
    """CLI entrypoint for categorizing transactions."""
    import argparse
    import os
    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="Categorize uncategorized YNAB transactions")
    parser.add_argument("--days", type=int, required=True, help="How many days back to fetch")
    args = parser.parse_args()

    # Check environment variables
    ynab_token = os.getenv("YNAB_API_TOKEN")
    if not ynab_token:
        raise ValueError("YNAB_API_TOKEN environment variable is required")

    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")

    # Load config
    config_path = Path("config.json")
    if not config_path.exists():
        raise ValueError("config.json not found. Run setup.py first.")

    config = json.loads(config_path.read_text())
    budget_id = config.get("budget_id")
    if not budget_id:
        raise ValueError("budget_id not found in config.json")

    # Fetch data from YNAB
    from ynab_client import YNABClient
    from datetime import datetime, timedelta

    client = YNABClient(token=ynab_token)
    since_date = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    transactions, _ = client.get_transactions(budget_id, since_date=since_date)
    categories = client.get_categories(budget_id)

    # Filter to uncategorized transactions
    uncategorized = [t for t in transactions if t.get("category_id") is None]

    if not uncategorized:
        print("No uncategorized transactions found.")
        return

    # Load or bootstrap cache
    cache = load_payee_cache()
    if not cache:
        print("Cache is empty. Bootstrapping from existing transactions...")
        all_txns, _ = client.get_transactions(budget_id)
        cache = build_cache_from_transactions(all_txns)
        if cache:
            save_payee_cache(cache)
            print(f"Cache built with {len(cache)} payees")

    # Categorize
    results = categorize_transactions(uncategorized, cache, categories, anthropic_key)

    # Print changeset
    print(f"\nProposed categorizations ({len(results)} transactions):")
    print("─" * 50)
    for result in results:
        tier_label = result.tier.upper()
        conf = f"{result.confidence:.2f}"
        reason = f" ({result.rationale})" if result.tier == "claude" else ""
        print(f"[{tier_label}] {result.transaction_id} → {result.category_name} (confidence: {conf}){reason}")

    # Update cache with new payees from Claude
    for result in results:
        if result.tier == "claude":
            # Find original transaction for payee name
            txn = next((t for t in uncategorized if t["id"] == result.transaction_id), None)
            if txn:
                normalized = normalize_payee(txn["payee_name"])
                cache[normalized] = {
                    "category_id": result.category_id,
                    "category_name": result.category_name,
                }

    save_payee_cache(cache)
    print(f"\nCache updated with {sum(1 for r in results if r.tier == 'claude')} new payees")


if __name__ == "__main__":
    main()
