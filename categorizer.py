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
    prior_strength: int | None = None  # Claude's confidence weight (1-20), only set for tier="claude"


FUZZY_THRESHOLD = 70   # thefuzz token_sort_ratio score (0-100), lowered from 85
CLAUDE_BATCH_SIZE = 25


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


def normalize_import_payee(name: str) -> str:
    """Normalize an import payee name (raw bank string) for cache lookups.

    Extends normalize_payee() with bank-specific pattern stripping:
    - ID: <code> patterns
    - CO: <code> patterns
    - ACH <code> patterns

    Raises ValueError if name is None, empty, or whitespace-only after stripping.
    """
    name = normalize_payee(name)
    # Strip bank-specific patterns
    name = re.sub(r'\s*id:\s*\S+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*co:\s*\S+', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s*ach\s+\S+', '', name, flags=re.IGNORECASE)
    # Clean up residual whitespace/punctuation
    name = re.sub(r'\s+', ' ', name).strip()
    name = name.rstrip('.,!?;:-').strip()

    if not name:
        raise ValueError("Import payee name is empty after normalization")

    return name


MIN_OBSERVATIONS = 3  # Trust cache after this many consistent categorizations
DEFAULT_CONFIDENCE_THRESHOLD = 0.02  # Fallback; main() computes dynamically from K


def count_categories_from_transactions(transactions: list[dict]) -> int:
    """Count unique categories actually used in a set of transactions.

    Use this with a time-windowed transaction list (e.g. last 18 months)
    to get K that reflects the current decision space rather than all
    categories that ever existed.

    Args:
        transactions: List of YNAB transaction dicts.

    Returns:
        Number of unique category_ids found.
    """
    return len({t["category_id"] for t in transactions if t.get("category_id")})


def filter_categories_by_usage(category_groups: list[dict], transactions: list[dict]) -> list[dict]:
    """Filter category groups to only include categories seen in the given transactions.

    Returns a new list of category groups with unused categories removed.
    Groups with no remaining categories are excluded.

    Args:
        category_groups: Full list of YNAB category group dicts.
        transactions: Transactions to check for category usage.

    Returns:
        Filtered list of category groups.
    """
    used_ids = {t["category_id"] for t in transactions if t.get("category_id")}
    filtered = []
    for group in category_groups:
        cats = [c for c in group.get("categories", []) if c["id"] in used_ids]
        if cats:
            filtered.append({**group, "categories": cats})
    return filtered


def compute_confidence_threshold(K: int, min_observations: int = MIN_OBSERVATIONS) -> float:
    """Derive a confidence threshold from K and a minimum observation count.

    Computes the Bayesian confidence a payee would have if it were seen
    min_observations times, all in the same category. This makes the
    threshold adapt to the user's category count — more categories means
    a lower threshold (since confidence grows more slowly).

    Args:
        K: Number of categories (from count_categories or count_categories_from_transactions).
        min_observations: Minimum consistent observations to trust (default MIN_OBSERVATIONS).

    Returns:
        Confidence threshold as a float.
    """
    from scipy.stats import beta
    b = K - 1
    if b == 0:
        return 1.0
    return beta.ppf(0.10, min_observations + 1, b)


def count_categories(category_groups: list[dict]) -> int:
    """Count non-deleted categories across non-deleted groups.

    Hidden categories ARE counted (they can still have transactions).
    Uses the same group format returned by YNABClient.get_categories().

    Args:
        category_groups: List of YNAB category group dicts.

    Returns:
        Number of non-deleted categories in non-deleted groups.
    """
    count = 0
    for group in category_groups:
        if group.get("deleted"):
            continue
        for cat in group.get("categories", []):
            if not cat.get("deleted"):
                count += 1
    return count


def compute_confidence(cache_entry: dict, K: int) -> tuple[str, str, float]:
    """Compute Bayesian confidence for a frequency cache entry.

    Uses the Beta distribution lower bound:
        beta.ppf(0.10, c_dominant + 1, (n - c_dominant) + (K - 1))

    Args:
        cache_entry: Frequency cache entry with 'total' and 'categories' keys.
        K: Number of YNAB categories (from count_categories).

    Returns:
        Tuple of (dominant_category_id, dominant_category_name, confidence).

    Raises:
        ValueError: If total == 0 or K < 1.
    """
    from scipy.stats import beta

    total = cache_entry["total"]
    if total == 0:
        raise ValueError("Cannot compute confidence for cache entry with total=0")
    if K < 1:
        raise ValueError(f"K must be >= 1, got {K}")

    categories = cache_entry["categories"]
    max_count = -1
    dominant_id = None
    dominant_name = None

    for cat_id, info in sorted(categories.items()):
        if info["count"] > max_count:
            max_count = info["count"]
            dominant_id = cat_id
            dominant_name = info["name"]

    c = max_count
    n = total
    b = (n - c) + (K - 1)
    if b == 0:
        # Degenerate case: K=1 and all observations in one category.
        # Beta(a, 0) is undefined, but confidence is trivially 1.0.
        confidence = 1.0
    else:
        confidence = beta.ppf(0.10, c + 1, b)

    return (dominant_id, dominant_name, confidence)


def record_categorization(
    cache: dict,
    payee_name: str,
    category_id: str,
    category_name: str,
    source: str,
    prior_strength: int = 1,
    import_names: list[str] | None = None,
) -> None:
    """Record a categorization in the frequency cache.

    Creates or updates the frequency entry for the normalized payee name.
    Adds prior_strength counts to the category (not always 1).
    Optionally creates alias entries for import names.

    Args:
        cache: The payee frequency cache (modified in place).
        payee_name: Raw payee name (will be normalized).
        category_id: YNAB category UUID.
        category_name: Human-readable category name.
        source: Origin tier ("history", "fuzzy", "claude").
        prior_strength: Number of pseudo-observations to add (default 1).
        import_names: Optional list of raw import payee names to create aliases for.
    """
    normalized = normalize_payee(payee_name)

    if normalized not in cache or "alias_of" in cache.get(normalized, {}):
        cache[normalized] = {"total": 0, "categories": {}}

    entry = cache[normalized]
    if category_id not in entry["categories"]:
        entry["categories"][category_id] = {"name": category_name, "count": 0}

    entry["categories"][category_id]["count"] += prior_strength
    entry["total"] += prior_strength

    if import_names:
        for raw_name in import_names:
            if not raw_name:
                continue
            try:
                norm_import = normalize_import_payee(raw_name)
            except ValueError:
                continue
            if norm_import == normalized:
                continue
            if "aliases" not in entry:
                entry["aliases"] = []
            if norm_import not in entry["aliases"]:
                entry["aliases"].append(norm_import)
            cache[norm_import] = {"alias_of": normalized}


def _resolve_alias(cache: dict, key: str) -> str:
    """Resolve an alias to its primary key (max 1 hop).

    If the entry at key has 'alias_of', return the target. Otherwise return key.
    """
    entry = cache.get(key)
    if entry and "alias_of" in entry:
        return entry["alias_of"]
    return key


def _dominant_category(entry: dict) -> tuple[str, str]:
    """Extract the dominant category_id and category_name from a v2 cache entry.

    Returns the category with the highest count. Ties broken by alphabetical
    category_id (matching compute_confidence behavior).
    """
    categories = entry["categories"]
    max_count = -1
    dominant_id = None
    dominant_name = None
    for cat_id, info in sorted(categories.items()):
        if info["count"] > max_count:
            max_count = info["count"]
            dominant_id = cat_id
            dominant_name = info["name"]
    return dominant_id, dominant_name


def _migrate_v1_to_v2(cache: dict) -> dict:
    """Migrate a v1 (flat) cache to v2 (frequency) format.

    V1 entries: {"category_id": "...", "category_name": "..."}
    V2 entries: {"total": N, "categories": {"cat_id": {"name": "...", "count": N}}}
    """
    import logging
    logging.warning("Migrating payee cache from v1 to v2 format")

    new_cache = {"_version": 2}
    for key, entry in cache.items():
        if key == "_version":
            continue
        cat_id = entry["category_id"]
        cat_name = entry["category_name"]
        new_cache[key] = {
            "total": 1,
            "categories": {
                cat_id: {"name": cat_name, "count": 1}
            }
        }
    return new_cache


def load_payee_cache(path: str = "data/cache/payee_lookup.json") -> dict:
    """Load the payee cache from disk. Auto-migrates v1 to v2 format.

    Args:
        path: Path to the cache JSON file

    Returns:
        Cache dict in v2 frequency format.
        Returns empty dict if file doesn't exist (first-run case).

    Raises:
        ValueError: If file exists but contains invalid JSON.
    """
    cache_path = Path(path)
    if not cache_path.exists():
        return {}

    try:
        content = cache_path.read_text()
        cache = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in cache file {path}: {e}")

    if cache.get("_version") != 2:
        cache = _migrate_v1_to_v2(cache)
        cache["_migrated_from_v1"] = True
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2))

    return cache


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
    """Build a v2 frequency payee cache from already-categorized YNAB transactions.

    Filters to transactions with both payee_name and category_id,
    and accumulates category frequency counts per normalized payee name.

    Args:
        transactions: List of YNAB transaction dicts

    Returns:
        Cache dict in v2 frequency format with _version key.
    """
    cache = {"_version": 2}

    for txn in transactions:
        payee = txn.get("payee_name")
        cat_id = txn.get("category_id")
        cat_name = txn.get("category_name")

        if not payee or not cat_id:
            continue

        import_names = []
        for field in ("import_payee_name", "import_payee_name_original"):
            val = txn.get(field)
            if val:
                import_names.append(val)

        record_categorization(
            cache, payee, cat_id, cat_name,
            source="history",
            import_names=import_names or None,
        )

    return cache


def history_lookup(
    payee_name: str,
    cache: dict,
    K: int | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    import_payee_name: str | None = None,
    import_payee_name_original: str | None = None,
) -> CategoryResult | None:
    """Look up a payee in the cache using exact match on normalized name.

    Tries payee_name first, then import_payee_name, then import_payee_name_original.
    All lookups resolve aliases. If K is provided, uses Bayesian confidence;
    otherwise returns confidence=1.0 (legacy behavior).

    Args:
        payee_name: Raw payee name to look up
        cache: V2 frequency cache dict
        K: Number of categories (for Bayesian confidence). None = legacy mode.
        confidence_threshold: Minimum confidence to return a result (only used with K).
        import_payee_name: Optional import payee name to try as fallback.
        import_payee_name_original: Optional original import payee name to try.

    Returns:
        CategoryResult if found (and confidence >= threshold), None otherwise.
    """
    candidates = [normalize_payee(payee_name)]
    for raw in (import_payee_name, import_payee_name_original):
        if raw:
            try:
                candidates.append(normalize_import_payee(raw))
            except ValueError:
                continue

    for candidate in candidates:
        resolved_key = _resolve_alias(cache, candidate)
        if resolved_key not in cache:
            continue
        entry = cache[resolved_key]
        if entry.get("_version") or not entry.get("categories"):
            continue

        if K is not None:
            cat_id, cat_name, confidence = compute_confidence(entry, K)
            if confidence < confidence_threshold:
                continue
        else:
            cat_id, cat_name = _dominant_category(entry)
            confidence = 1.0

        return CategoryResult(
            transaction_id="",
            category_id=cat_id,
            category_name=cat_name,
            confidence=confidence,
            rationale="Exact history match",
            tier="history",
        )

    return None


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


def fuzzy_match(
    payee_name: str,
    cache: dict,
    threshold: int = FUZZY_THRESHOLD,
    K: int | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    import_payee_name: str | None = None,
    import_payee_name_original: str | None = None,
) -> CategoryResult | None:
    """Find a close match in the cache using fuzzy matching.

    Tries all available name variants and picks the best fuzzy score.
    If K is provided, also checks Bayesian confidence on the matched entry.

    Args:
        payee_name: Raw payee name to match
        cache: V2 frequency cache dict
        threshold: Minimum fuzzy score (0-100) to consider a match
        K: Number of categories (for Bayesian confidence). None = legacy mode.
        confidence_threshold: Minimum Bayesian confidence (only used with K).
        import_payee_name: Optional import payee name variant to try.
        import_payee_name_original: Optional original import payee name variant.

    Returns:
        CategoryResult if match found (and confidence >= threshold), None otherwise.
    """
    if not cache:
        return None

    matchable_keys = [
        k for k in cache
        if k != "_version"
        and isinstance(cache.get(k), dict)
        and "alias_of" not in cache[k]
    ]
    if not matchable_keys:
        return None

    # Collect all name variants to try
    variants = [normalize_payee(payee_name)]
    for raw in (import_payee_name, import_payee_name_original):
        if raw:
            try:
                variants.append(normalize_import_payee(raw))
            except ValueError:
                continue

    best_overall_match = None
    best_overall_score = -1
    for variant in variants:
        match, score = process.extractOne(
            variant, matchable_keys, scorer=fuzzy_score
        )
        if score > best_overall_score:
            best_overall_score = score
            best_overall_match = match

    if best_overall_score < threshold:
        return None

    entry = cache[best_overall_match]
    if not entry.get("categories"):
        return None

    if K is not None:
        cat_id, cat_name, bayesian_conf = compute_confidence(entry, K)
        confidence = min(best_overall_score / 100.0, bayesian_conf)
        if confidence < confidence_threshold:
            return None
    else:
        cat_id, cat_name = _dominant_category(entry)
        confidence = best_overall_score / 100.0

    return CategoryResult(
        transaction_id="",
        category_id=cat_id,
        category_name=cat_name,
        confidence=confidence,
        rationale=f"Fuzzy match to '{best_overall_match}' (score: {best_overall_score})",
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
        txn_list += f"{i}. Payee: {payee}, Amount: ${amount:.2f}, Date: {date}"
        import_name = txn.get("import_payee_name_original")
        if import_name:
            txn_list += f", Bank description: {import_name}"
        txn_list += "\n"

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
    "rationale": "brief reason for this category",
    "prior_strength": 15
  }},
  ...
]

prior_strength is an integer from 1 to 20 indicating how confident you are that future transactions from this payee will be in the same category:
- 1-3: Very uncertain (e.g., "Square Payment" could be anything)
- 4-7: Somewhat uncertain (e.g., "Amazon" spans multiple categories)
- 8-12: Fairly confident (e.g., "Starbucks" is almost always Coffee/Dining)
- 13-17: Very confident (e.g., "Netflix" is clearly Entertainment)
- 18-20: Absolutely certain (e.g., "Mortgage Payment" is always Mortgage)

Only use category IDs from the list above. Return ONLY the JSON array, no other text."""

    user_message = f"Categorize these transactions:\n{txn_list}"

    # Call Claude Haiku
    # Each response item is ~150-200 tokens (rationale + prior_strength);
    # scale max_tokens generously to avoid truncation
    max_tokens = max(2048, len(transactions) * 250)
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    # Parse response
    if not response.content:
        raise ValueError(
            f"Claude returned empty content. stop_reason={response.stop_reason}, "
            f"usage={response.usage}"
        )
    response_text = response.content[0].text
    if not response_text or not response_text.strip():
        raise ValueError(
            f"Claude returned empty text. stop_reason={response.stop_reason}, "
            f"usage={response.usage}, content_type={response.content[0].type}"
        )

    if response.stop_reason == "max_tokens":
        raise ValueError(
            f"Claude response truncated (max_tokens reached). "
            f"Batch had {len(transactions)} transactions, max_tokens={max_tokens}. "
            f"Response text (last 200 chars): ...{response_text[-200:]}"
        )

    # Strip markdown code fences if present
    stripped = response_text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n")
        stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        response_text = stripped.strip()

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Claude returned unparseable JSON: {e}\n"
            f"stop_reason={response.stop_reason}, "
            f"Response text (first 500 chars): {response_text[:500]}"
        )

    if not isinstance(data, list):
        raise ValueError("Claude response is not a JSON array")

    if len(data) != len(transactions):
        raise ValueError(f"Claude returned {len(data)} results for {len(transactions)} transactions")

    # Build results
    results = []
    required_fields = {"category_id", "category_name", "confidence", "rationale", "prior_strength"}
    for i, item in enumerate(data):
        missing = required_fields - set(item.keys())
        if missing:
            raise ValueError(f"Response item {i} missing fields: {', '.join(sorted(missing))}")

        ps = item["prior_strength"]
        if not isinstance(ps, int) or ps < 1 or ps > 20:
            raise ValueError(
                f"Response item {i} has invalid prior_strength: {ps!r} (must be integer 1-20)"
            )

        txn = transactions[i]
        results.append(CategoryResult(
            transaction_id=txn["id"],
            category_id=item["category_id"],
            category_name=item["category_name"],
            confidence=item["confidence"],
            rationale=item["rationale"],
            tier="claude",
            prior_strength=ps,
        ))

    return results


def categorize_transactions(
    transactions: list[dict],
    cache: dict,
    categories: list[dict],
    api_key: str,
    K: int | None = None,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> list[CategoryResult]:
    """Orchestrate the three-tier categorization for a list of transactions.

    Args:
        transactions: List of uncategorized YNAB transaction dicts
        cache: Payee cache dict
        categories: List of category group dicts
        api_key: Anthropic API key
        K: Number of categories for Bayesian confidence. None = legacy mode.
        confidence_threshold: Minimum confidence for tiers 1 and 2.

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

        import_payee = txn.get("import_payee_name")
        import_payee_orig = txn.get("import_payee_name_original")

        # Try tier 1
        result = history_lookup(
            payee, cache, K=K, confidence_threshold=confidence_threshold,
            import_payee_name=import_payee,
            import_payee_name_original=import_payee_orig,
        )
        if result is None:
            # Try tier 2
            result = fuzzy_match(
                payee, cache, K=K, confidence_threshold=confidence_threshold,
                import_payee_name=import_payee,
                import_payee_name_original=import_payee_orig,
            )
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

    # Compute K from categories used in the last 18 months
    k_since = (datetime.now() - timedelta(days=548)).strftime("%Y-%m-%d")
    k_txns, _ = client.get_transactions(budget_id, since_date=k_since)
    K = count_categories_from_transactions(k_txns)
    if K < 2:
        K = count_categories(categories)
    confidence_threshold = compute_confidence_threshold(K)
    recent_categories = filter_categories_by_usage(categories, k_txns)
    print(f"K={K} categories (last 18mo), confidence threshold={confidence_threshold:.4f} (min {MIN_OBSERVATIONS} obs)")

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
        payee_count = sum(1 for k in cache if k != "_version")
        if payee_count > 0:
            save_payee_cache(cache)
            print(f"Cache built with {payee_count} payees")
    elif cache.get("_migrated_from_v1"):
        print("Cache migrated from v1. Rebuilding from YNAB history for accurate frequency counts...")
        all_txns, _ = client.get_transactions(budget_id)
        cache = build_cache_from_transactions(all_txns)
        save_payee_cache(cache)
        payee_count = sum(1 for k in cache if k != "_version")
        print(f"Cache rebuilt with {payee_count} payees")

    # Categorize
    results = categorize_transactions(uncategorized, cache, recent_categories, anthropic_key, K=K, confidence_threshold=confidence_threshold)

    # Print changeset
    print(f"\nProposed categorizations ({len(results)} transactions):")
    print("─" * 50)
    for result in results:
        tier_label = result.tier.upper()
        conf = f"{result.confidence:.2f}"
        reason = f" ({result.rationale})" if result.tier == "claude" else ""
        print(f"[{tier_label}] {result.transaction_id} -> {result.category_name} (confidence: {conf}){reason}")

    # Update cache with new payees from Claude
    for result in results:
        if result.tier == "claude":
            txn = next((t for t in uncategorized if t["id"] == result.transaction_id), None)
            if txn:
                import_names = []
                for field in ("import_payee_name", "import_payee_name_original"):
                    val = txn.get(field)
                    if val:
                        import_names.append(val)
                record_categorization(
                    cache, txn["payee_name"],
                    result.category_id, result.category_name,
                    source="claude",
                    prior_strength=result.prior_strength or 1,
                    import_names=import_names or None,
                )

    save_payee_cache(cache)
    print(f"\nCache updated with {sum(1 for r in results if r.tier == 'claude')} new payees")


if __name__ == "__main__":
    main()
