"""Claude-powered transaction categorization.

Three-tier categorization strategy:
1. History lookup — resolve from payee cache
2. Fuzzy match — catch payee name variations
3. Claude (Haiku, batched) — genuinely novel payees only
"""
import re
from dataclasses import dataclass


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
