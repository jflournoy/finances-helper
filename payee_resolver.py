"""Claude-powered payee entity resolution.

Resolves unknown raw payee strings (especially from bank imports) to known
canonical payees using Claude's world knowledge of merchant names and bank
string formats.

This tier runs between fuzzy match (tier-2) and Claude categorize (tier-3),
handling payees that fuzzy matching couldn't resolve.
"""
import json
from dataclasses import dataclass
import anthropic
from thefuzz import process, fuzz


@dataclass
class ResolutionCandidate:
    """A proposed resolution of a raw payee string to a canonical payee."""
    raw_payee: str
    candidate_canonical: str
    confidence: float  # 0.0-1.0
    rationale: str


@dataclass
class ResolutionResult:
    """Outcome of resolution for a single raw payee."""
    raw_payee: str
    resolution: ResolutionCandidate | None  # None = "this is a new payee"


def shortlist_candidates(
    raw_payee: str,
    canonical_payees: list[str],
    n: int = 30,
) -> list[str]:
    """Return up to n canonical payees most likely to match raw_payee.

    Uses token_sort_ratio with a permissive threshold (40) to identify
    candidates. Filters to return only canonical keys (not aliases).

    Args:
        raw_payee: Raw payee string to shortlist candidates for
        canonical_payees: List of canonical payee keys from cache
        n: Max number of candidates to return (default 30)

    Returns:
        List of canonical payee keys, sorted by fuzzy score descending.
        Empty list if canonical_payees is empty.
    """
    if not canonical_payees:
        return []

    # Extract top N matches, then filter by score threshold
    matches = process.extract(
        raw_payee,
        canonical_payees,
        scorer=fuzz.token_sort_ratio,
        limit=n,
    )

    # matches is [(key, score), ...] sorted by score descending
    # Filter to threshold of 40
    filtered = [(key, score) for key, score in matches if score >= 40]
    return [key for key, _score in filtered]


def resolve_batch(
    unresolved: list[tuple[str, list[str]]],
    api_key: str,
) -> list[ResolutionResult]:
    """Resolve a batch of raw payees to canonical payees via Claude.

    Each unresolved payee comes with a pre-computed shortlist of candidate
    canonical payees. Claude sees all N payees at once (up to 25) and returns
    either a canonical match (with confidence/rationale) or 'new payee'.

    Args:
        unresolved: List of (raw_payee, shortlist) tuples.
                   shortlist is a list of candidate canonical keys.
        api_key: Anthropic API key

    Returns:
        List of ResolutionResult, one per input, in input order.

    Raises:
        ValueError: On unparseable response, length mismatch, max_tokens,
                   invalid confidence, or unknown canonical in response.
        RuntimeError: On API errors.
    """
    if not unresolved:
        return []

    # Build user message with all payees and shortlists
    payee_text = ""
    for i, (raw, shortlist) in enumerate(unresolved, 1):
        shortlist_str = ", ".join(f"'{c}'" for c in shortlist)
        payee_text += f"{i}. Raw: {raw}\n   Candidates: {shortlist_str}\n"

    system_prompt = """You are resolving raw bank payee strings to canonical merchant names.

For each raw payee string, you will:
1. Look at the provided list of candidate canonical payees
2. Decide if the raw string matches any candidate (they represent the same merchant)
3. Return either the matching canonical payee name or indicate it's a new payee not in the list

Consider:
- Abbreviations and truncations (NEWREZ-SHELLPOIN vs newrez-shellpoint)
- Case differences and punctuation
- Bank-specific formatting (ID codes, status strings appended)
- Your world knowledge of merchant names and brands

Respond with a JSON array, one object per raw payee, in the same order as provided:
[
  {
    "raw_payee": "NEWREZ-SHELLPOIN ID: 6371542226",
    "resolved_to": "newrez-shellpoint",
    "confidence": 0.95,
    "rationale": "Bank abbreviation matches known lender"
  },
  {
    "raw_payee": "UNKNOWN-VENDOR-XYZ",
    "resolved_to": null,
    "confidence": 0.0,
    "rationale": "No match in candidates; appears to be a new payee"
  },
  ...
]

If resolved_to is null, it means the raw payee did not match any candidate and is genuinely new.
Confidence is a float 0.0-1.0 indicating your certainty in the match (or 0.0 if new payee).
Only return the JSON array, no other text."""

    user_message = f"Resolve these raw payees:\n{payee_text}"

    # Call Claude Haiku
    max_tokens = max(1024, len(unresolved) * 200)
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    # Validate response
    if not response.content:
        raise ValueError(
            f"Claude returned empty content. stop_reason={response.stop_reason}, "
            f"usage={response.usage}"
        )

    response_text = response.content[0].text
    if not response_text or not response_text.strip():
        raise ValueError(
            f"Claude returned empty text. stop_reason={response.stop_reason}, "
            f"usage={response.usage}"
        )

    if response.stop_reason == "max_tokens":
        raise ValueError(
            f"Claude response truncated (max_tokens reached). "
            f"Batch had {len(unresolved)} payees, max_tokens={max_tokens}. "
            f"Response (last 200 chars): ...{response_text[-200:]}"
        )

    # Parse JSON
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
            f"Response (first 500 chars): {response_text[:500]}"
        )

    if not isinstance(data, list):
        raise ValueError("Claude response is not a JSON array")

    if len(data) != len(unresolved):
        raise ValueError(
            f"Claude returned {len(data)} results for {len(unresolved)} payees"
        )

    # Build results
    results = []
    for i, (raw, shortlist) in enumerate(unresolved):
        item = data[i]

        # Validate required fields
        if "raw_payee" not in item or "resolved_to" not in item or "confidence" not in item:
            raise ValueError(f"Response item {i} missing required fields")

        resolved_to = item["resolved_to"]
        confidence = item.get("confidence", 0.0)

        # Validate confidence range
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            raise ValueError(
                f"Response item {i} has invalid confidence: {confidence!r} (must be float 0.0-1.0)"
            )

        # If resolved, validate that it's in the shortlist
        if resolved_to is not None:
            if resolved_to not in shortlist:
                raise ValueError(
                    f"Response item {i} resolved to '{resolved_to}' which is not in its shortlist"
                )

            candidate = ResolutionCandidate(
                raw_payee=raw,
                candidate_canonical=resolved_to,
                confidence=confidence,
                rationale=item.get("rationale", ""),
            )
            results.append(ResolutionResult(raw_payee=raw, resolution=candidate))
        else:
            # New payee
            results.append(ResolutionResult(raw_payee=raw, resolution=None))

    return results
