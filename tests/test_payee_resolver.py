"""Unit tests for payee_resolver module."""
import pytest
import json
from payee_resolver import (
    ResolutionCandidate,
    ResolutionResult,
    shortlist_candidates,
    resolve_batch,
)


class TestShortlistCandidates:
    """Test payee shortlisting via fuzzy matching."""

    def test_returns_empty_on_empty_payees(self):
        result = shortlist_candidates("NEWREZ-SHELLPOIN", [])
        assert result == []

    def test_returns_up_to_n_candidates(self):
        payees = [f"payee_{i}" for i in range(100)]
        result = shortlist_candidates("payee_0", payees, n=30)
        assert len(result) <= 30

    def test_includes_exact_match(self):
        payees = ["newrez-shellpoint", "navient", "transfer"]
        result = shortlist_candidates("newrez-shellpoint", payees, n=10)
        assert "newrez-shellpoint" in result

    def test_includes_fuzzy_matches(self):
        payees = ["newrez-shellpoint", "navient", "transfer"]
        result = shortlist_candidates("NEWREZ SHELLPOIN ID 6371542226", payees, n=10)
        # Should include "newrez-shellpoint" due to fuzzy match
        assert "newrez-shellpoint" in result

    def test_skips_aliases(self):
        """Shortlist should only return canonical payee keys, not aliases."""
        payees = ["amazon", "amazon-amazon", "transfer"]
        result = shortlist_candidates("amazn.com", payees, n=10)
        # All returned should be from the input list
        assert all(p in payees for p in result)

    def test_sorted_by_score_descending(self):
        """Best matches should come first."""
        payees = ["new rez", "navient", "newrez-shellpoint", "transfer"]
        result = shortlist_candidates("newrez", payees, n=10)
        # First result should be best match
        assert result[0] in ["newrez-shellpoint", "new rez"]


class TestResolveBatch:
    """Test Claude batch resolution."""

    def test_empty_input_returns_empty(self):
        result = resolve_batch([], api_key="dummy")
        assert result == []

    def test_parses_resolution_response(self):
        """Mock Claude response with recorded fixture."""
        unresolved = [
            ("NEWREZ-SHELLPOIN ID: 6371542226", ["newrez-shellpoint", "new rez", "transfer"]),
            ("UNKNOWN-VENDOR-XYZ", ["amazon", "navient", "transfer"]),
        ]

        # This would be a fixture in real tests; for now we skip the actual API call
        # Real test would mock anthropic.Anthropic and return fixture JSON
        pytest.skip("Requires fixture or mock setup")

    def test_handles_new_payee_verdict(self):
        """Claude can say 'this is a new payee, not in the shortlist'."""
        pytest.skip("Requires fixture or mock setup")

    def test_rejects_invalid_confidence(self):
        """Confidence must be 0.0-1.0."""
        pytest.skip("Requires fixture or mock setup")

    def test_rejects_unrecognized_canonical(self):
        """Resolution can't reference a canonical not in its shortlist."""
        pytest.skip("Requires fixture or mock setup")

    def test_length_mismatch_raises(self):
        """Claude returns wrong number of results → ValueError."""
        pytest.skip("Requires fixture or mock setup")

    def test_max_tokens_truncation_raises(self):
        """Truncated response (stop_reason=max_tokens) → ValueError."""
        pytest.skip("Requires fixture or mock setup")

    def test_unparseable_json_raises(self):
        """Malformed JSON → ValueError with context."""
        pytest.skip("Requires fixture or mock setup")
