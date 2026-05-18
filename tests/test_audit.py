"""Unit tests for audit.py (Issue D)."""
import json
import pytest
from unittest.mock import patch, Mock
from audit import AuditFinding, audit_batch, filter_auditable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_txn(txn_id, payee_name, category_id="cat1", category_name="Utilities",
              amount=-50000, date="2026-01-15", transfer_account_id=None):
    return {
        "id": txn_id,
        "payee_name": payee_name,
        "category_id": category_id,
        "category_name": category_name,
        "amount": amount,
        "amount_dollars": amount / 1000.0,
        "date": date,
        "transfer_account_id": transfer_account_id,
        "deleted": False,
    }


def _categories():
    return [
        {"id": "g1", "name": "Bills", "categories": [
            {"id": "cat1", "name": "Utilities"},
            {"id": "cat2", "name": "Internet"},
        ]},
        {"id": "g2", "name": "Food", "categories": [
            {"id": "cat3", "name": "Groceries"},
        ]},
    ]


def _flat_categories(categories=None):
    """Return flat {id: name} dict from category groups."""
    cats = categories or _categories()
    out = {}
    for group in cats:
        for cat in group.get("categories", []):
            out[cat["id"]] = cat["name"]
    return out


def _mock_claude_response(findings: list) -> Mock:
    mock_response = Mock()
    mock_response.content = [Mock(text=json.dumps(findings))]
    mock_response.stop_reason = "end_turn"
    return mock_response


# ---------------------------------------------------------------------------
# filter_auditable
# ---------------------------------------------------------------------------

class TestFilterAuditable:

    def test_keeps_categorized_transactions(self):
        txns = [_make_txn("t1", "Netflix")]
        result = filter_auditable(txns)
        assert len(result) == 1

    def test_skips_uncategorized(self):
        txn = _make_txn("t1", "Netflix", category_id=None, category_name=None)
        result = filter_auditable([txn])
        assert result == []

    def test_skips_transfers_by_payee_prefix(self):
        txn = _make_txn("t1", "Transfer : Savings Account")
        result = filter_auditable([txn])
        assert result == []

    def test_skips_payments_by_payee_prefix(self):
        txn = _make_txn("t1", "Payment : Visa")
        result = filter_auditable([txn])
        assert result == []

    def test_skips_transfers_by_transfer_account_id(self):
        txn = _make_txn("t1", "Some Bank", transfer_account_id="acct-99")
        result = filter_auditable([txn])
        assert result == []

    def test_mixed_list(self):
        txns = [
            _make_txn("t1", "Netflix"),
            _make_txn("t2", "Transfer : Savings"),
            _make_txn("t3", "Whole Foods", category_id=None, category_name=None),
            _make_txn("t4", "Spotify"),
        ]
        result = filter_auditable(txns)
        assert [t["id"] for t in result] == ["t1", "t4"]


# ---------------------------------------------------------------------------
# audit_batch
# ---------------------------------------------------------------------------

class TestAuditBatch:

    def test_empty_input_returns_empty_without_api_call(self):
        with patch("audit.anthropic.Anthropic") as mock_claude:
            result = audit_batch([], _flat_categories(), "key")
        assert result == []
        mock_claude.assert_not_called()

    def test_parses_finding_response(self):
        txns = [
            _make_txn("t1", "Netflix", category_id="cat3", category_name="Groceries"),
        ]
        finding = {
            "transaction_id": "t1",
            "payee_name": "Netflix",
            "current_category_id": "cat3",
            "current_category_name": "Groceries",
            "suggested_category_id": None,
            "suggested_category_name": None,
            "confidence": 0.95,
            "rationale": "Netflix is a streaming service, not Groceries",
        }
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([finding])
            results = audit_batch(txns, _flat_categories(), "key")

        assert len(results) == 1
        assert isinstance(results[0], AuditFinding)
        assert results[0].transaction_id == "t1"
        assert results[0].confidence == 0.95
        assert results[0].suggested_category_id is None

    def test_zero_findings_ok(self):
        """Claude returns empty list — no mis-categorizations found."""
        txns = [_make_txn("t1", "Netflix")]
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([])
            results = audit_batch(txns, _flat_categories(), "key")

        assert results == []

    def test_finding_with_suggestion(self):
        txns = [_make_txn("t1", "Whole Foods", category_id="cat1", category_name="Utilities")]
        finding = {
            "transaction_id": "t1",
            "payee_name": "Whole Foods",
            "current_category_id": "cat1",
            "current_category_name": "Utilities",
            "suggested_category_id": "cat3",
            "suggested_category_name": "Groceries",
            "confidence": 0.92,
            "rationale": "Whole Foods is a grocery store",
        }
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([finding])
            results = audit_batch(txns, _flat_categories(), "key")

        assert results[0].suggested_category_id == "cat3"
        assert results[0].suggested_category_name == "Groceries"

    def test_validates_unknown_current_category_id(self):
        """Finding references a current_category_id not in the known set → ValueError."""
        txns = [_make_txn("t1", "Netflix", category_id="cat-unknown")]
        finding = {
            "transaction_id": "t1",
            "payee_name": "Netflix",
            "current_category_id": "cat-unknown",
            "current_category_name": "Whatever",
            "suggested_category_id": None,
            "suggested_category_name": None,
            "confidence": 0.8,
            "rationale": "r",
        }
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([finding])
            with pytest.raises(ValueError, match="unknown category"):
                audit_batch(txns, _flat_categories(), "key")

    def test_validates_unknown_suggested_category_id(self):
        """Finding references a suggested_category_id not in the known set → ValueError."""
        txns = [_make_txn("t1", "Netflix")]
        finding = {
            "transaction_id": "t1",
            "payee_name": "Netflix",
            "current_category_id": "cat1",
            "current_category_name": "Utilities",
            "suggested_category_id": "cat-bogus",
            "suggested_category_name": "Whatever",
            "confidence": 0.8,
            "rationale": "r",
        }
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([finding])
            with pytest.raises(ValueError, match="unknown category"):
                audit_batch(txns, _flat_categories(), "key")

    def test_validates_confidence_range(self):
        """Confidence outside 0.0-1.0 → ValueError."""
        txns = [_make_txn("t1", "Netflix")]
        finding = {
            "transaction_id": "t1",
            "payee_name": "Netflix",
            "current_category_id": "cat1",
            "current_category_name": "Utilities",
            "suggested_category_id": None,
            "suggested_category_name": None,
            "confidence": 1.5,
            "rationale": "r",
        }
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([finding])
            with pytest.raises(ValueError, match="confidence"):
                audit_batch(txns, _flat_categories(), "key")

    def test_raises_on_max_tokens_truncation(self):
        txns = [_make_txn("t1", "Netflix")]
        mock_response = Mock()
        mock_response.content = [Mock(text='[{"partial":')]
        mock_response.stop_reason = "max_tokens"
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            with pytest.raises(ValueError, match="max_tokens"):
                audit_batch(txns, _flat_categories(), "key")

    def test_raises_on_unparseable_json(self):
        txns = [_make_txn("t1", "Netflix")]
        mock_response = Mock()
        mock_response.content = [Mock(text="not json at all")]
        mock_response.stop_reason = "end_turn"
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            with pytest.raises(ValueError, match="unparseable"):
                audit_batch(txns, _flat_categories(), "key")

    def test_strips_markdown_code_fences(self):
        txns = [_make_txn("t1", "Netflix")]
        fenced = "```json\n[]\n```"
        mock_response = Mock()
        mock_response.content = [Mock(text=fenced)]
        mock_response.stop_reason = "end_turn"
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            result = audit_batch(txns, _flat_categories(), "key")
        assert result == []

    def test_validates_unknown_transaction_id_in_finding(self):
        """Finding references a transaction_id not in the input batch → ValueError."""
        txns = [_make_txn("t1", "Netflix")]
        finding = {
            "transaction_id": "t-bogus",
            "payee_name": "Netflix",
            "current_category_id": "cat1",
            "current_category_name": "Utilities",
            "suggested_category_id": None,
            "suggested_category_name": None,
            "confidence": 0.9,
            "rationale": "r",
        }
        with patch("audit.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = _mock_claude_response([finding])
            with pytest.raises(ValueError, match="unknown transaction"):
                audit_batch(txns, _flat_categories(), "key")
