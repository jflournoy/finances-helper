"""Tests for resolution tier integration into categorize_transactions (Issue B/C)."""
import pytest
from unittest.mock import patch, Mock
from categorizer import (
    categorize_transactions,
    update_cache_from_claude_results,
    CategoryResult,
)
from payee_resolver import ResolutionCandidate, ResolutionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_txn(txn_id, payee_name, import_payee=None):
    return {
        "id": txn_id,
        "payee_name": payee_name,
        "import_payee_name": import_payee,
        "import_payee_name_original": None,
        "amount": -10000,
        "date": "2026-01-01",
    }


def _make_cache_with_canonical(canonical, cat_id="cat1", cat_name="Utilities"):
    return {
        canonical: {
            "total": 5,
            "categories": {cat_id: {"name": cat_name, "count": 5}},
        }
    }


def _categories():
    return [{"id": "g1", "name": "Bills", "categories": [{"id": "cat1", "name": "Utilities"}]}]


# ---------------------------------------------------------------------------
# Issue B: Resolution tier runs after fuzzy miss, before Claude tier
# ---------------------------------------------------------------------------

class TestResolutionTierIntegration:

    def test_resolution_tier_disabled_by_default(self):
        """Without resolve_payees=True, novel payees go straight to Claude."""
        txn = _make_txn("t1", "NEWREZ-SHELLPOIN ID 123")
        cache = {}
        categories = _categories()

        mock_response = Mock()
        mock_response.content = [Mock(text='[{"payee_name": "NEWREZ-SHELLPOIN ID 123", "category_id": "cat1", "category_name": "Utilities", "confidence": 0.8, "rationale": "r", "prior_strength": 5}]')]
        mock_response.stop_reason = "end_turn"

        with patch("categorizer.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            results, _, _, _ = categorize_transactions([txn], cache, categories, "key")

        assert len(results) == 1
        assert results[0].tier == "claude"
        mock_claude.assert_called_once()

    def test_resolution_tier_runs_after_fuzzy_miss(self):
        """With resolve_payees=True, fuzzy-miss payees go through resolver.

        Uses 'SP MTG SVC PYMT 44712' — fuzzy score ~16 against 'newrez-shellpoint',
        well below the 70 threshold, so fuzzy won't catch it but Claude would know
        it's a Shellpoint Mortgage payment.
        """
        raw_bank_string = "SP MTG SVC PYMT 44712"
        txn = _make_txn("t1", raw_bank_string)
        # Cache has the canonical but not an alias for the raw bank string
        cache = _make_cache_with_canonical("newrez-shellpoint")
        categories = _categories()

        resolved = ResolutionResult(
            raw_payee=raw_bank_string,
            resolution=ResolutionCandidate(
                raw_payee=raw_bank_string,
                candidate_canonical="newrez-shellpoint",
                confidence=0.95,
                rationale="SP = Shellpoint, MTG = mortgage servicer",
            ),
        )

        with patch("categorizer.resolve_batch", return_value=[resolved]) as mock_resolve, \
             patch("categorizer.anthropic.Anthropic") as mock_claude:
            results, _, _, _ = categorize_transactions(
                [txn], cache, categories, "key", resolve_payees=True
            )

        assert len(results) == 1
        assert results[0].tier == "resolved"
        assert results[0].category_id == "cat1"
        assert "newrez-shellpoint" in results[0].rationale
        mock_claude.assert_not_called()
        mock_resolve.assert_called_once()

    def test_resolution_low_confidence_falls_through_to_claude(self):
        """Resolution result with confidence < 0.85 does not produce a proposal; falls to Claude."""
        txn = _make_txn("t1", "MYSTERY-VENDOR-99")
        cache = _make_cache_with_canonical("known-vendor")
        categories = _categories()

        low_conf = ResolutionResult(
            raw_payee="MYSTERY-VENDOR-99",
            resolution=ResolutionCandidate(
                raw_payee="MYSTERY-VENDOR-99",
                candidate_canonical="known-vendor",
                confidence=0.60,
                rationale="Weak match",
            ),
        )

        mock_response = Mock()
        mock_response.content = [Mock(text='[{"payee_name": "MYSTERY-VENDOR-99", "category_id": "cat1", "category_name": "Utilities", "confidence": 0.7, "rationale": "r", "prior_strength": 5}]')]
        mock_response.stop_reason = "end_turn"

        with patch("categorizer.resolve_batch", return_value=[low_conf]), \
             patch("categorizer.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            results, _, _, _ = categorize_transactions(
                [txn], cache, categories, "key", resolve_payees=True
            )

        assert len(results) == 1
        assert results[0].tier == "claude"

    def test_resolution_new_payee_falls_through_to_claude(self):
        """Resolution result with resolution=None (new payee) falls to Claude tier."""
        txn = _make_txn("t1", "BRAND-NEW-MERCHANT")
        cache = {}
        categories = _categories()

        new_payee = ResolutionResult(raw_payee="BRAND-NEW-MERCHANT", resolution=None)

        mock_response = Mock()
        mock_response.content = [Mock(text='[{"payee_name": "BRAND-NEW-MERCHANT", "category_id": "cat1", "category_name": "Utilities", "confidence": 0.7, "rationale": "r", "prior_strength": 5}]')]
        mock_response.stop_reason = "end_turn"

        with patch("categorizer.resolve_batch", return_value=[new_payee]), \
             patch("categorizer.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            results, _, _, _ = categorize_transactions(
                [txn], cache, categories, "key", resolve_payees=True
            )

        assert len(results) == 1
        assert results[0].tier == "claude"

    def test_resolution_only_called_for_fuzzy_misses(self):
        """Payees that hit history or fuzzy are not sent to resolver."""
        cached_txn = _make_txn("t1", "known payee")
        novel_txn = _make_txn("t2", "NOVEL-BANK-STRING-XYZ")

        cache = {
            "known payee": {
                "total": 3,
                "categories": {"cat1": {"name": "Utilities", "count": 3}},
            }
        }
        categories = _categories()

        resolved = ResolutionResult(raw_payee="NOVEL-BANK-STRING-XYZ", resolution=None)

        mock_response = Mock()
        mock_response.content = [Mock(text='[{"payee_name": "NOVEL-BANK-STRING-XYZ", "category_id": "cat1", "category_name": "Utilities", "confidence": 0.7, "rationale": "r", "prior_strength": 5}]')]
        mock_response.stop_reason = "end_turn"

        with patch("categorizer.resolve_batch", return_value=[resolved]) as mock_resolve, \
             patch("categorizer.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            results, _, _, _ = categorize_transactions(
                [cached_txn, novel_txn], cache, categories, "key", resolve_payees=True
            )

        # Resolver called with only the novel txn
        call_args = mock_resolve.call_args[0][0]
        assert len(call_args) == 1
        assert call_args[0][0] == "NOVEL-BANK-STRING-XYZ"

    def test_resolution_batched_at_claude_batch_size(self):
        """Multiple fuzzy-miss payees are batched together in one resolve_batch call."""
        txns = [_make_txn(f"t{i}", f"UNKNOWN-VENDOR-{i}") for i in range(5)]
        cache = {}
        categories = _categories()

        new_payees = [
            ResolutionResult(raw_payee=f"UNKNOWN-VENDOR-{i}", resolution=None)
            for i in range(5)
        ]

        mock_response_text = "[" + ",".join(
            f'{{"payee_name": "UNKNOWN-VENDOR-{i}", "category_id": "cat1", "category_name": "Utilities", "confidence": 0.7, "rationale": "r", "prior_strength": 5}}'
            for i in range(5)
        ) + "]"
        mock_response = Mock()
        mock_response.content = [Mock(text=mock_response_text)]
        mock_response.stop_reason = "end_turn"

        with patch("categorizer.resolve_batch", return_value=new_payees) as mock_resolve, \
             patch("categorizer.anthropic.Anthropic") as mock_claude:
            mock_client = Mock()
            mock_claude.return_value = mock_client
            mock_client.messages.create.return_value = mock_response
            results, _, _, _ = categorize_transactions(
                txns, cache, categories, "key", resolve_payees=True
            )

        assert mock_resolve.call_count == 1
        assert len(results) == 5


# ---------------------------------------------------------------------------
# Issue C: update_cache_from_claude_results handles tier="resolved"
# ---------------------------------------------------------------------------

class TestCacheUpdateResolved:

    def test_resolved_result_creates_alias(self):
        """Accepted tier='resolved' result creates alias from raw payee to canonical."""
        canonical = "newrez-shellpoint"
        raw = "NEWREZ-SHELLPOIN ID 123"
        cache = _make_cache_with_canonical(canonical)

        result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Utilities",
            confidence=0.95,
            rationale=f"Resolved to '{canonical}'",
            tier="resolved",
        )
        txn = _make_txn("t1", raw, import_payee=None)
        txn["payee_name"] = raw

        update_cache_from_claude_results(cache, [txn], [result])

        from categorizer import normalize_payee
        raw_norm = normalize_payee(raw)
        assert raw_norm in cache
        assert cache[raw_norm].get("alias_of") == canonical

    def test_resolved_result_does_not_add_category_to_canonical(self):
        """Resolved tier records alias only — does not inflate the canonical's category count."""
        canonical = "newrez-shellpoint"
        cache = _make_cache_with_canonical(canonical, cat_id="cat1")
        original_count = cache[canonical]["categories"]["cat1"]["count"]

        result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Utilities",
            confidence=0.95,
            rationale=f"Resolved to '{canonical}'",
            tier="resolved",
        )
        txn = _make_txn("t1", "NEWREZ-SHELLPOIN ID 123")
        update_cache_from_claude_results(cache, [txn], [result])

        assert cache[canonical]["categories"]["cat1"]["count"] == original_count

    def test_claude_tier_still_processed(self):
        """Existing claude-tier behavior is unchanged alongside resolved-tier results."""
        cache = {}
        categories = [{"id": "cat1", "name": "Utilities"}]

        claude_result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Utilities",
            confidence=0.8,
            rationale="Claude says so",
            tier="claude",
            prior_strength=5,
        )
        txn = _make_txn("t1", "Some New Payee")
        update_cache_from_claude_results(cache, [txn], [claude_result])

        from categorizer import normalize_payee
        assert normalize_payee("Some New Payee") in cache

    def test_history_and_fuzzy_tiers_still_skipped(self):
        """Tiers other than 'claude' and 'resolved' are not written to cache."""
        cache = {}
        for tier in ("history", "fuzzy", "amazon-wf"):
            result = CategoryResult(
                transaction_id="t1",
                category_id="cat1",
                category_name="Utilities",
                confidence=1.0,
                rationale="r",
                tier=tier,
            )
            txn = _make_txn("t1", f"Payee {tier}")
            update_cache_from_claude_results(cache, [txn], [result])

        assert len(cache) == 0
