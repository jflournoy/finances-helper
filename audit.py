"""Claude-powered categorization audit.

Read-only diagnostic: sends categorized transactions to Claude Haiku and
asks it to flag any that look mis-categorized. Output is advisory only —
never writes to YNAB. Findings flow into a Markdown/JSON report for human
review.
"""
import json
from dataclasses import dataclass
import anthropic


@dataclass
class AuditFinding:
    """A suspected mis-categorization flagged by Claude."""
    transaction_id: str
    payee_name: str
    amount_dollars: float
    date: str
    current_category_id: str
    current_category_name: str
    suggested_category_id: str | None   # None = flag only, no specific suggestion
    suggested_category_name: str | None
    confidence: float                   # 0.0-1.0, Claude's certainty this is wrong
    rationale: str


def filter_auditable(transactions: list[dict]) -> list[dict]:
    """Filter transactions to those eligible for audit.

    Keeps only transactions that:
    - Have a category_id set (already categorized)
    - Are not transfers (payee starts with "Transfer :" or "Payment :",
      or transfer_account_id is set)

    Args:
        transactions: List of YNAB transaction dicts

    Returns:
        Filtered list (subset of input, same order).
    """
    result = []
    for txn in transactions:
        if not txn.get("category_id"):
            continue
        payee = txn.get("payee_name", "")
        if payee.startswith("Transfer :") or payee.startswith("Payment :"):
            continue
        if txn.get("transfer_account_id"):
            continue
        result.append(txn)
    return result


def audit_batch(
    transactions: list[dict],
    flat_categories: dict,
    api_key: str,
) -> list[AuditFinding]:
    """Audit a batch of categorized transactions via Claude Haiku.

    Sends up to 25 transactions and asks Claude to flag any that appear
    mis-categorized. Returns 0 or more findings — Claude only reports
    problems, not one result per transaction.

    Args:
        transactions: List of categorized YNAB transaction dicts (already filtered
                      by filter_auditable). Each must have id, payee_name,
                      category_id, category_name, amount_dollars, date.
        flat_categories: Dict mapping category_id → category_name for validation.
        api_key: Anthropic API key.

    Returns:
        List of AuditFinding objects (may be empty if no issues found).

    Raises:
        ValueError: On max_tokens truncation, unparseable JSON, invalid confidence,
                   unknown category_id, or unknown transaction_id in response.
        RuntimeError: On API errors.
    """
    if not transactions:
        return []

    txn_by_id = {t["id"]: t for t in transactions}

    # Build category list for prompt
    category_text = "\n".join(
        f"  {cat_id}: {cat_name}"
        for cat_id, cat_name in sorted(flat_categories.items(), key=lambda x: x[1])
    )

    # Build transaction list
    txn_text = ""
    for txn in transactions:
        txn_text += (
            f"- ID: {txn['id']}\n"
            f"  Payee: {txn.get('payee_name', 'Unknown')}\n"
            f"  Amount: ${txn.get('amount_dollars', 0):.2f}\n"
            f"  Date: {txn.get('date', 'Unknown')}\n"
            f"  Current category: {txn.get('category_name', 'Unknown')} ({txn.get('category_id', '?')})\n"
        )

    system_prompt = f"""You are auditing personal finance transaction categories for errors.

Review the provided transactions and flag any that appear to be mis-categorized.
Only flag transactions where you are confident (>80%) the category is wrong.
Do NOT flag every transaction — only genuine problems.

Available categories:
{category_text}

Return a JSON array of findings. If everything looks correct, return an empty array [].
Each finding must have these fields:
{{
  "transaction_id": "the transaction ID",
  "payee_name": "payee name from the transaction",
  "current_category_id": "the current category ID",
  "current_category_name": "the current category name",
  "suggested_category_id": "better category ID, or null if unsure",
  "suggested_category_name": "better category name, or null if unsure",
  "confidence": 0.95,
  "rationale": "brief explanation of why this looks wrong"
}}

Only use category IDs from the list above. Return ONLY the JSON array, no other text."""

    user_message = f"Audit these categorized transactions:\n{txn_text}"

    max_tokens = max(2048, len(transactions) * 300)
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    if not response.content:
        raise ValueError(
            f"Claude returned empty content. stop_reason={response.stop_reason}, "
            f"usage={response.usage}"
        )

    response_text = response.content[0].text
    if not response_text or not response_text.strip():
        raise ValueError(
            f"Claude returned empty text. stop_reason={response.stop_reason}"
        )

    if response.stop_reason == "max_tokens":
        raise ValueError(
            f"Claude response truncated (max_tokens reached). "
            f"Batch had {len(transactions)} transactions, max_tokens={max_tokens}. "
            f"Response (last 200 chars): ...{response_text[-200:]}"
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
            f"Response (first 500 chars): {response_text[:500]}"
        )

    if not isinstance(data, list):
        raise ValueError("Claude audit response is not a JSON array")

    findings = []
    for i, item in enumerate(data):
        txn_id = item.get("transaction_id")
        if txn_id not in txn_by_id:
            raise ValueError(
                f"Finding {i} references unknown transaction ID: {txn_id!r}. "
                f"Known IDs: {list(txn_by_id.keys())}"
            )

        txn = txn_by_id[txn_id]
        current_cat_id = item.get("current_category_id")
        if current_cat_id not in flat_categories:
            raise ValueError(
                f"Finding {i} references unknown category ID: {current_cat_id!r} "
                f"(current_category_id)"
            )

        suggested_cat_id = item.get("suggested_category_id")
        if suggested_cat_id is not None and suggested_cat_id not in flat_categories:
            raise ValueError(
                f"Finding {i} references unknown category ID: {suggested_cat_id!r} "
                f"(suggested_category_id)"
            )

        confidence = item.get("confidence", 0.0)
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            raise ValueError(
                f"Finding {i} has invalid confidence: {confidence!r} (must be float 0.0-1.0)"
            )

        findings.append(AuditFinding(
            transaction_id=txn_id,
            payee_name=item.get("payee_name", txn.get("payee_name", "")),
            amount_dollars=txn.get("amount_dollars", 0.0),
            date=txn.get("date", ""),
            current_category_id=current_cat_id,
            current_category_name=item.get("current_category_name", ""),
            suggested_category_id=suggested_cat_id,
            suggested_category_name=item.get("suggested_category_name"),
            confidence=confidence,
            rationale=item.get("rationale", ""),
        ))

    return findings
