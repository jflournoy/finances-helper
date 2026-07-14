"""Tests for enrich.py — unified categorize+enrich workflow."""
import pytest
import json
from pathlib import Path
from unittest.mock import patch, Mock
from decimal import Decimal
from enrich import main
from zipfile import ZipFile
from datetime import datetime, timedelta


class _ITEnrichAnthropicMock:
    """Mock anthropic.Anthropic for IT-ENRICH integration tests."""

    def __init__(self, *args, **kwargs):
        self.messages = self

    def create(self, *, model, max_tokens, system, messages, **kwargs):
        import re
        user_text = messages[0]["content"]

        # Pattern 1: flat transactions "N. Payee: ..."
        flat_lines = re.findall(r'^\d+\. Payee:', user_text, flags=re.MULTILINE)
        n_flat = len(flat_lines)

        # Pattern 2: Amazon items "N. "description" ..." (for item-level categorization)
        item_lines = re.findall(r'^(\d+)\. "', user_text, flags=re.MULTILINE)
        n_items = len(item_lines)

        is_item_call = n_items > 0 and n_flat == 0

        if is_item_call:
            # Amazon item categorization response (needs item_index, 1-based)
            # Use category from the fixture to ensure it's valid
            response_items = [
                {
                    "item_index": i + 1,
                    "category_id": "dddddddd-0000-0000-0000-000000000001",
                    "category_name": "Rent",
                    "confidence": 0.95,
                    "rationale": f"Mock Claude rationale for item {i + 1}",
                }
                for i in range(n_items)
            ]
        else:
            # Flat transaction categorization (needs prior_strength instead of item_index)
            response_items = [
                {
                    "category_id": "dddddddd-0000-0000-0000-000000000001",
                    "category_name": "Rent",
                    "confidence": 0.95,
                    "rationale": f"Mock Claude rationale for item {i + 1}",
                    "prior_strength": 1,
                }
                for i in range(n_flat)
            ]

        text = json.dumps(response_items)

        class _Resp:
            def __init__(self, t):
                self.content = [type("B", (), {"text": t})()]
                self.stop_reason = "end_turn"
                self.usage = None

        return _Resp(text)


@pytest.fixture(autouse=True)
def patch_load_dotenv():
    """Patch load_dotenv for all tests."""
    with patch("enrich.load_dotenv"):
        yield


class TestEnrichMain:
    """Test enrich.py main() CLI scaffold."""

    def test_missing_ynab_token(self, tmp_path, monkeypatch):
        """Missing YNAB_API_TOKEN exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("YNAB_API_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        result = main(["--days", "30"])
        assert result == 1

    def test_missing_anthropic_key(self, tmp_path, monkeypatch):
        """Missing ANTHROPIC_API_KEY exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        result = main(["--days", "30"])
        assert result == 1

    def test_missing_budget_env(self, tmp_path, monkeypatch):
        """Missing YNAB_DEFAULT_BUDGET exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.delenv("YNAB_DEFAULT_BUDGET", raising=False)

        result = main(["--days", "30"])
        assert result == 1

    def test_malformed_config(self, tmp_path, monkeypatch):
        """Malformed config.json exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")
        tmp_path.joinpath("config.json").write_text("{invalid json")

        result = main(["--days", "30"])
        assert result == 1

    def test_no_writable_txns(self, tmp_path, monkeypatch, capsys):
        """No writable txns exits 0, prints message, no changeset."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={}):
                result = main(["--days", "30"])

        assert result == 0
        captured = capsys.readouterr()
        assert "No uncategorized writable transactions found" in captured.out

    def test_no_writable_txns_with_cold_cache(self, tmp_path, monkeypatch):
        """No writable txns with cold cache triggers bootstrap fetch."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value=None):
                with patch("enrich.build_cache_from_transactions", return_value={}):
                    with patch("enrich.save_payee_cache"):
                        result = main(["--days", "30"])

        assert result == 0
        # Bootstrap fetch should happen even with no writable txns
        assert mock_client.get_transactions.call_count == 3, f"Expected 3 calls (window, k_window, bootstrap), got {mock_client.get_transactions.call_count}"

    def test_ynab_call_count_warm_cache(self, tmp_path, monkeypatch):
        """YNAB get_transactions called exactly twice for warm cache."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={"_version": 2}):
                with patch("enrich.categorize_transactions", return_value=([], [], [], [])):
                    result = main(["--days", "30"])

        assert result == 0
        # get_transactions called twice (window + k-window)
        assert mock_client.get_transactions.call_count == 2

    def test_ynab_call_count_cold_cache(self, tmp_path, monkeypatch):
        """YNAB get_transactions called exactly 3 times for cold cache."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        # Provide at least one writable transaction to avoid early return
        writable_txn = {"id": "t1", "payee_name": "Test Store", "category_id": None, "cleared": "cleared", "deleted": False}

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([writable_txn], 0)  # return 1 txn for all calls
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value=None):
                with patch("enrich.build_cache_from_transactions", return_value={}):
                    with patch("enrich.categorize_transactions", return_value=([], [], [], [])):
                        result = main(["--days", "30"])

        assert result == 0
        # get_transactions called 3 times (window + k-window + bootstrap)
        assert mock_client.get_transactions.call_count == 3

    def test_missing_required_days_arg(self):
        """Missing --days argument causes argparse error (exit 2)."""
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_success_exit_code(self, tmp_path, monkeypatch):
        """Successful run exits 0."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={}):
                result = main(["--days", "30"])

        assert result == 0

    def test_rebuild_profiles_flag_rebuilds_and_exits(self, tmp_path, monkeypatch):
        """--rebuild-profiles regenerates all descriptions (regenerate='all'), then exits 0 without categorizing."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={"_version": 2}):
                with patch("enrich.build_profiles") as mock_build:
                    with patch("enrich.categorize_transactions") as mock_cat:
                        result = main(["--rebuild-profiles"])

        assert result == 0
        mock_build.assert_called_once()
        assert mock_build.call_args.kwargs.get("regenerate") == "all"
        mock_cat.assert_not_called()

    def test_refresh_profiles_flag_regenerates_stale_and_exits(self, tmp_path, monkeypatch):
        """--refresh-profiles regenerates only flagged descriptions (regenerate='stale'), then exits 0."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={"_version": 2}):
                with patch("enrich.build_profiles") as mock_build:
                    with patch("enrich.categorize_transactions") as mock_cat:
                        result = main(["--refresh-profiles"])

        assert result == 0
        mock_build.assert_called_once()
        assert mock_build.call_args.kwargs.get("regenerate") == "stale"
        mock_cat.assert_not_called()

    def test_days_required_without_profile_command(self, monkeypatch):
        """--days is required for a normal run, optional for profile-only commands."""
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_profiles_loaded_without_regeneration_on_normal_run(self, tmp_path, monkeypatch):
        """A normal run loads profiles with regenerate='none' (no mid-run Claude bootstrap)
        and threads them into categorization."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        txn = {
            "id": "t1", "payee_name": "Some Store", "category_id": None,
            "cleared": "cleared", "deleted": False, "approved": False,
        }
        mock_client = Mock()
        mock_client.get_transactions.return_value = ([txn], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        sentinel_profiles = {"_version": 1, "categories": {}}

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={"_version": 2}):
                with patch("enrich.build_profiles", return_value=sentinel_profiles) as mock_build:
                    with patch("enrich.categorize_transactions", return_value=([], [], [], [])) as mock_cat:
                        result = main(["--days", "30"])

        assert result == 0
        mock_build.assert_called_once()
        assert mock_build.call_args.kwargs.get("regenerate") == "none"
        # profiles threaded into categorize_transactions
        assert mock_cat.call_args.kwargs.get("profiles") is sentinel_profiles

    def test_normal_run_nudges_when_descriptions_stale(self, tmp_path, monkeypatch, capsys):
        """A normal run prints a refresh nudge when dirty/undescribed categories exist."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        txn = {
            "id": "t1", "payee_name": "Some Store", "category_id": None,
            "cleared": "cleared", "deleted": False, "approved": False,
        }
        mock_client = Mock()
        mock_client.get_transactions.return_value = ([txn], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        stale_profiles = {
            "_version": 1,
            "categories": {
                "a": {"name": "A", "description": "old", "dirty": True},
                "b": {"name": "B", "description": "", "dirty": False},
            },
        }

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={"_version": 2}):
                with patch("enrich.build_profiles", return_value=stale_profiles):
                    with patch("enrich.categorize_transactions", return_value=([], [], [], [])):
                        result = main(["--days", "30"])

        assert result == 0
        out = capsys.readouterr().out
        assert "--refresh-profiles" in out
        assert "--rebuild-profiles" in out

    def test_normal_run_no_nudge_when_profiles_fresh(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "b123")

        txn = {
            "id": "t1", "payee_name": "Some Store", "category_id": None,
            "cleared": "cleared", "deleted": False, "approved": False,
        }
        mock_client = Mock()
        mock_client.get_transactions.return_value = ([txn], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []
        mock_client.resolve_budget_id.return_value = "b123"

        fresh_profiles = {
            "_version": 1,
            "categories": {"a": {"name": "A", "description": "good", "dirty": False}},
        }

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={"_version": 2}):
                with patch("enrich.build_profiles", return_value=fresh_profiles):
                    with patch("enrich.categorize_transactions", return_value=([], [], [], [])):
                        result = main(["--days", "30"])

        assert result == 0
        out = capsys.readouterr().out
        assert "--refresh-profiles" not in out


class TestWriteUnifiedChangeset:
    """Test write_unified_changeset function."""

    def test_minimal_json_structure(self, tmp_path):
        """Minimal valid changeset has version, kind, and metadata."""
        from enrich import write_unified_changeset
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        assert json_path.exists()
        assert md_path.exists()
        payload = json.loads(json_path.read_text())
        assert payload["version"] == 1
        assert payload["kind"] == "enrich-changeset"
        assert payload["metadata"]["budget_id"] == "b123"
        assert payload["metadata"]["since_date"] == "2026-03-28"
        assert payload["metadata"]["days_back"] == 30
        assert payload["metadata"]["K"] == 312
        assert payload["metadata"]["confidence_threshold"] == 0.0096
        assert payload["metadata"]["dump_path"] is None

    def test_file_naming_with_timestamp(self, tmp_path):
        """File naming uses YYYYMMDD-HHMMSS format."""
        from enrich import write_unified_changeset
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 5)

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        assert "enrich-changeset-20260427-143005.json" in str(json_path)
        assert "enrich-changeset-20260427-143005.md" in str(md_path)

    def test_summary_labels_present(self, tmp_path):
        """Markdown output contains all required summary labels when there's content."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Groceries",
            tier="claude",
            confidence=0.95,
            rationale="Store",
            prior_strength=1,
        )

        source_txns = [
            {"id": "t1", "payee_name": "Store", "amount_dollars": "50.00", "date": "2026-03-30"}
        ]

        skipped_txn = {
            "id": "t_skip",
            "payee_name": "Transfer : Savings",
            "amount_dollars": "100.00",
            "date": "2026-03-30",
        }

        unmatched = [
            (
                {"id": "t_unmatched", "payee_name": "Amazon.com", "date": "2026-03-01"},
                "date_out_of_window"
            ),
        ]

        md_path, json_path = write_unified_changeset(
            flat_results=[result],
            skipped=[skipped_txn],
            unmatched_amazon=unmatched,
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        markdown = md_path.read_text()
        required_labels = [
            "## Dump",
            "## Days back",
            "## K",
            "## Writable txns",
            "## Non-Amazon",
            "## Skipped",
            "## Unmatched Amazon txns",
            "## Changeset",
        ]
        for label in required_labels:
            assert label in markdown, f"Missing label: {label}"


    def test_amount_dollars_str_exact_from_milliunits(self):
        """_amount_dollars_str converts int milliunits to exact decimal string.

        Regression test for the float-precision drift bug: a txn whose dollar
        amount would round to 0.30000000000000004 via float arithmetic must
        emit exactly '0.30' (or equivalent exact decimal) when derived from
        milliunits.
        """
        from enrich import _amount_dollars_str
        from decimal import Decimal
        assert Decimal(_amount_dollars_str({"amount": 300})) == Decimal("0.3")
        assert Decimal(_amount_dollars_str({"amount": -50000})) == Decimal("-50")
        assert Decimal(_amount_dollars_str({"amount": 0})) == Decimal(0)
        s = _amount_dollars_str({"amount": 4250})
        assert "0000000" not in s
        assert Decimal(s) == Decimal("4.25")
        assert _amount_dollars_str({}) is None
        assert _amount_dollars_str({"amount_dollars": "42.50"}) == "42.50"
        with pytest.raises(TypeError, match="precision drift"):
            _amount_dollars_str({"amount_dollars": 0.1 + 0.2})
        # bool is a subclass of int but must not be treated as milliunits
        assert _amount_dollars_str({"amount": True}) is None
        assert _amount_dollars_str({"amount": False}) is None
        assert _amount_dollars_str({"amount": True, "amount_dollars": "1.00"}) == "1.00"

    def test_write_unified_changeset_amount_dollars_is_exact_decimal(self, tmp_path):
        """amount_dollars in written changeset is derived from integer milliunits.

        Specifically: a txn with amount=-300 milliunits emits '-0.300', not
        '-0.30000000000000004'. This catches float-to-decimal drift.
        """
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="X",
            tier="history",
            confidence=0.9,
            rationale="r",
            prior_strength=1,
        )
        source_txns = [
            {"id": "t1", "payee_name": "P", "amount": -300, "date": "2026-04-01"},
        ]
        _, json_path = write_unified_changeset(
            flat_results=[result], skipped=[], unmatched_amazon=[], split_proposals=[],
            source_txns=source_txns, budget_id="b", since_date="2026-03-01",
            days_back=30, K=10, confidence_threshold=0.5, dump_path=None,
            out_dir=tmp_path, now=datetime(2026, 4, 27, 14, 30, 0),
        )
        payload = json.loads(json_path.read_text())
        amt = payload["non_amazon"]["proposals"][0]["amount_dollars"]
        assert "0000000" not in amt, f"float-drift artifact in amount_dollars: {amt!r}"
        assert Decimal(amt) == Decimal("-0.3")

    def test_write_unified_changeset_serializes_flat_results(self, tmp_path):
        """flat_results CategoryResult objects are serialized into non_amazon.proposals."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        result1 = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Groceries",
            tier="claude",
            confidence=0.95,
            rationale="Whole Foods store",
            prior_strength=1,
        )
        result2 = CategoryResult(
            transaction_id="t2",
            category_id="cat2",
            category_name="Utilities",
            tier="history",
            confidence=0.99,
            rationale="From cache",
            prior_strength=2,
        )

        source_txns = [
            {"id": "t1", "payee_name": "Whole Foods", "amount_dollars": "50.00", "date": "2026-03-30"},
            {"id": "t2", "payee_name": "Electric Co", "amount_dollars": "120.00", "date": "2026-03-31"},
        ]

        md_path, json_path = write_unified_changeset(
            flat_results=[result1, result2],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        proposals = payload["non_amazon"]["proposals"]
        assert len(proposals) == 2
        assert proposals[0]["transaction_id"] == "t1"
        assert proposals[0]["payee_name"] == "Whole Foods"
        assert proposals[0]["amount_dollars"] == "50.00"
        assert proposals[0]["date"] == "2026-03-30"
        assert proposals[0]["category_id"] == "cat1"
        assert proposals[0]["category_name"] == "Groceries"
        assert proposals[0]["tier"] == "claude"
        assert proposals[0]["confidence"] == 0.95
        assert proposals[1]["transaction_id"] == "t2"
        assert proposals[1]["payee_name"] == "Electric Co"
        assert proposals[1]["amount_dollars"] == "120.00"
        assert proposals[1]["date"] == "2026-03-31"
        assert proposals[1]["tier"] == "history"

    def test_write_unified_changeset_raises_on_missing_txn(self, tmp_path):
        """Missing source txn for a proposal raises loudly."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        result1 = CategoryResult(
            transaction_id="t_missing",
            category_id="cat1",
            category_name="Groceries",
            tier="claude",
            confidence=0.95,
            rationale="Store",
            prior_strength=1,
        )

        source_txns = [
            {"id": "t1", "payee_name": "Store", "amount_dollars": "50.00", "date": "2026-03-30"},
        ]

        with pytest.raises(ValueError, match="t_missing"):
            write_unified_changeset(
                flat_results=[result1],
                skipped=[],
                unmatched_amazon=[],
                split_proposals=[],
                source_txns=source_txns,
                budget_id="b123",
                since_date="2026-03-28",
                days_back=30,
                K=312,
                confidence_threshold=0.0096,
                dump_path=None,
                out_dir=out_dir,
                now=now,
            )

    def test_write_unified_changeset_serializes_skipped(self, tmp_path):
        """skipped transactions are serialized into non_amazon.skipped_transfers."""
        from enrich import write_unified_changeset
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        skipped_txn = {
            "id": "t_skip",
            "payee_name": "Transfer : Savings",
            "amount_dollars": "100.00",
            "date": "2026-03-30",
        }

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[skipped_txn],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        skipped_list = payload["non_amazon"]["skipped_transfers"]
        assert len(skipped_list) == 1
        assert skipped_list[0]["id"] == "t_skip"
        assert skipped_list[0]["payee_name"] == "Transfer : Savings"

    def test_write_unified_changeset_sort_by_date(self, tmp_path):
        """Proposals are sorted by date then transaction_id."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        results = [
            CategoryResult(
                transaction_id="t3",
                category_id="cat1",
                category_name="Cat1",
                tier="claude",
                confidence=0.95,
                rationale="Store",
                prior_strength=1,
            ),
            CategoryResult(
                transaction_id="t1",
                category_id="cat1",
                category_name="Cat1",
                tier="claude",
                confidence=0.95,
                rationale="Store",
                prior_strength=1,
            ),
            CategoryResult(
                transaction_id="t2",
                category_id="cat1",
                category_name="Cat1",
                tier="claude",
                confidence=0.95,
                rationale="Store",
                prior_strength=1,
            ),
        ]

        source_txns = [
            {"id": "t1", "payee_name": "Store1", "amount_dollars": "10.00", "date": "2026-03-15"},
            {"id": "t2", "payee_name": "Store2", "amount_dollars": "20.00", "date": "2026-03-20"},
            {"id": "t3", "payee_name": "Store3", "amount_dollars": "30.00", "date": "2026-03-10"},
        ]

        md_path, json_path = write_unified_changeset(
            flat_results=results,
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-01",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        proposals = payload["non_amazon"]["proposals"]
        assert proposals[0]["transaction_id"] == "t3"
        assert proposals[0]["date"] == "2026-03-10"
        assert proposals[1]["transaction_id"] == "t1"
        assert proposals[1]["date"] == "2026-03-15"
        assert proposals[2]["transaction_id"] == "t2"
        assert proposals[2]["date"] == "2026-03-20"

    def test_write_unified_changeset_serializes_match_result(self, tmp_path):
        """match_result shipments and parse_errors are serialized into amazon sections."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from amazon_matcher import AmazonShipment, MatchResult

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        shipment = AmazonShipment(
            order_id="111-0000001-0000001",
            ship_date=datetime(2026, 3, 25).date(),
            payment_method_raw="Visa - XXXX",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("50.00"),
            tax=Decimal("2.50"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("52.50"),
            items=[],
            shipment_status="Shipped",
        )

        match_result = MatchResult(
            matched=[],
            unmatched_ynab=[],
            unmatched_shipments=[shipment],
            excluded_shipments=[],
            parse_errors=[{"line": 5, "error": "Invalid date"}],
        )

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            match_result=match_result,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        assert len(payload["amazon"]["unmatched_shipments"]) == 1
        assert payload["amazon"]["unmatched_shipments"][0]["order_id"] == "111-0000001-0000001"
        assert len(payload["amazon"]["parse_errors"]) == 1
        assert payload["amazon"]["parse_errors"][0]["error"] == "Invalid date"

    def test_write_unified_changeset_serializes_unmatched_amazon(self, tmp_path):
        """unmatched_amazon tuples are serialized into amazon.unmatched_ynab."""
        from enrich import write_unified_changeset
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        unmatched = [
            (
                {"id": "t_unmatched", "payee_name": "Amazon.com", "date": "2026-03-01"},
                "date_out_of_window"
            ),
        ]

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=unmatched,
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        unmatched_list = payload["amazon"]["unmatched_ynab"]
        assert len(unmatched_list) == 1
        assert unmatched_list[0]["transaction_id"] == "t_unmatched"
        assert unmatched_list[0]["reason"] == "date_out_of_window"

    def test_write_unified_changeset_markdown_per_txn_tables(self, tmp_path):
        """Markdown output includes per-txn tables with payee names and amounts."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Groceries",
            tier="claude",
            confidence=0.95,
            rationale="Store",
            prior_strength=1,
        )

        source_txns = [
            {"id": "t1", "payee_name": "Whole Foods", "amount_dollars": "50.00", "date": "2026-03-30"}
        ]

        md_path, json_path = write_unified_changeset(
            flat_results=[result],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        markdown = md_path.read_text()
        assert "Whole Foods" in markdown
        assert "50.00" in markdown
        assert "Groceries" in markdown

    def test_write_unified_changeset_markdown_amazon_splits_table(self, tmp_path):
        """Markdown output includes per-shipment Amazon splits table."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import AmazonSplitProposal

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        class MockShipment:
            def __init__(self, order_id, ship_date):
                self.order_id = order_id
                self.ship_date = ship_date

        class MockTxn:
            def __init__(self, txn_id, date):
                self.id = txn_id
                self.date = date

        split1 = AmazonSplitProposal(
            parent_ynab_txn=MockTxn("t1", "2026-03-25"),
            shipment=MockShipment("111-0000001-0000001", datetime(2026, 3, 25).date()),
            subtransactions=[],
        )
        split2 = AmazonSplitProposal(
            parent_ynab_txn=MockTxn("t2", "2026-03-20"),
            shipment=MockShipment("111-0000002-0000002", datetime(2026, 3, 20).date()),
            subtransactions=[],
        )

        md_path, _ = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[split1, split2],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-18",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        markdown = md_path.read_text()
        assert "## Amazon splits" in markdown
        assert "2" in markdown  # 2 splits mentioned
        assert "Order ID" in markdown or "order_id" in markdown  # Table header or JSON field
        assert "111-0000001-0000001" in markdown
        assert "111-0000002-0000002" in markdown

    def test_write_unified_changeset_markdown_uncategorized_amazon_item(self, tmp_path):
        """Amazon split items with no category render as [UNCATEGORIZED] in the markdown table."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from decimal import Decimal
        from categorizer import AmazonSplitProposal, ItemCategoryResult
        from amazon_matcher import AmazonItem

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        class MockShipment:
            def __init__(self, order_id, ship_date):
                self.order_id = order_id
                self.ship_date = ship_date

        item_uncategorized = AmazonItem(
            order_id="111-9999999-9999999", asin="B000NOCAT", product_name="Mystery Widget",
            quantity=1, unit_price=Decimal("9.99"), unit_price_tax=Decimal("0.00"),
            ship_date=datetime(2026, 4, 14).date(), raw_row_index=1,
        )
        sub_uncategorized = ItemCategoryResult(
            ynab_transaction_id="t-uncat",
            item=item_uncategorized,
            allocated_amount=Decimal("9.99"),
            category_id=None,
            category_name=None,
            confidence=0.0,
            rationale="no category assigned",
        )
        item_categorized = AmazonItem(
            order_id="111-9999999-9999999", asin="B000HASCAT", product_name="Soap",
            quantity=1, unit_price=Decimal("4.50"), unit_price_tax=Decimal("0.00"),
            ship_date=datetime(2026, 4, 14).date(), raw_row_index=2,
        )
        sub_categorized = ItemCategoryResult(
            ynab_transaction_id="t-uncat",
            item=item_categorized,
            allocated_amount=Decimal("4.50"),
            category_id="cat-1",
            category_name="Household supplies",
            confidence=0.9,
            rationale="cleaning",
        )

        proposal_all_uncategorized = AmazonSplitProposal(
            parent_ynab_txn={"id": "t-all-uncat", "date": "2026-04-14", "payee_name": "Amazon"},
            shipment=MockShipment("111-1111111-1111111", datetime(2026, 4, 14).date()),
            subtransactions=[sub_uncategorized],
        )
        proposal_mixed = AmazonSplitProposal(
            parent_ynab_txn={"id": "t-mixed", "date": "2026-04-15", "payee_name": "Amazon"},
            shipment=MockShipment("111-2222222-2222222", datetime(2026, 4, 15).date()),
            subtransactions=[sub_uncategorized, sub_categorized],
        )

        md_path, _ = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[proposal_all_uncategorized, proposal_mixed],
            source_txns=[],
            budget_id="b123",
            since_date="2026-04-01",
            days_back=30,
            K=70,
            confidence_threshold=0.05,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        markdown = md_path.read_text()
        all_uncat_line = next(
            (line for line in markdown.splitlines() if "111-1111111-1111111" in line), None
        )
        assert all_uncat_line is not None, f"all-uncategorized split row not in markdown:\n{markdown}"
        assert "[UNCATEGORIZED]" in all_uncat_line, (
            f"row for shipment with no categories should contain '[UNCATEGORIZED]', got: {all_uncat_line!r}"
        )

        mixed_line = next(
            (line for line in markdown.splitlines() if "111-2222222-2222222" in line), None
        )
        assert mixed_line is not None
        assert "[UNCATEGORIZED]" in mixed_line, (
            f"mixed row should contain [UNCATEGORIZED] for the uncategorized item, got: {mixed_line!r}"
        )
        assert "Household supplies" in mixed_line, (
            f"mixed row should also contain the categorized item's category, got: {mixed_line!r}"
        )

    def test_write_unified_changeset_markdown_no_amazon_splits(self, tmp_path):
        """Markdown output omits Amazon section when no splits."""
        from enrich import write_unified_changeset
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        md_path, _ = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            now=now,
        )

        markdown = md_path.read_text()
        assert "## Amazon splits" not in markdown

    def test_write_unified_changeset_deterministic(self, tmp_path):
        """Same inputs produce byte-for-byte identical JSON output."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import CategoryResult

        result = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Groceries",
            tier="claude",
            confidence=0.95,
            rationale="Store",
            prior_strength=1,
        )

        source_txns = [
            {"id": "t1", "payee_name": "Store", "amount_dollars": "50.00", "date": "2026-03-30"}
        ]

        now = datetime(2026, 4, 27, 14, 30, 0)
        out_dir1 = tmp_path / "changesets1"
        out_dir2 = tmp_path / "changesets2"

        md1, json1 = write_unified_changeset(
            flat_results=[result],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir1,
            now=now,
        )

        md2, json2 = write_unified_changeset(
            flat_results=[result],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir2,
            now=now,
        )

        json1_content = json1.read_bytes()
        json2_content = json2.read_bytes()
        assert json1_content == json2_content, "JSON outputs differ on identical inputs"

        md1_content = md1.read_bytes()
        md2_content = md2.read_bytes()
        assert md1_content == md2_content, "Markdown outputs differ on identical inputs"

    def test_write_unified_changeset_parse_errors_deterministic(self, tmp_path):
        """parse_errors are sorted deterministically."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from amazon_matcher import MatchResult, ParseError
        import json

        source_txns = []
        now = datetime(2026, 4, 27, 14, 30, 0)

        parse_errors = [
            ParseError(row_index=3, reason="Bad CSV"),
            ParseError(row_index=1, reason="Missing field"),
            ParseError(row_index=2, reason="Invalid date"),
        ]

        match_result = MatchResult(
            matched=[],
            unmatched_ynab=[],
            unmatched_shipments=[],
            excluded_shipments=[],
            parse_errors=parse_errors,
        )

        out_dir1 = tmp_path / "changesets1"
        out_dir2 = tmp_path / "changesets2"

        md1, json1 = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            match_result=match_result,
            out_dir=out_dir1,
            now=now,
        )

        md2, json2 = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=source_txns,
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            match_result=match_result,
            out_dir=out_dir2,
            now=now,
        )

        json1_content = json.loads(json1.read_text())
        json2_content = json.loads(json2.read_text())

        assert json1_content["amazon"]["parse_errors"] == json2_content["amazon"]["parse_errors"], \
            "parse_errors not deterministic: order changed between runs"

    def test_excluded_shipments_unpacks_tuple_with_reason(self, tmp_path):
        """Issue #142: excluded_shipments is list[tuple[shipment, reason]] — must unpack."""
        from enrich import write_unified_changeset
        from amazon_matcher import AmazonShipment, MatchResult
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        shipment = AmazonShipment(
            order_id="111-EXCL-0000001",
            ship_date=datetime(2026, 3, 25).date(),
            payment_method_raw="Visa - XXXX",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("50.00"),
            tax=Decimal("2.50"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("52.50"),
            items=[],
            shipment_status="Not Available",
        )

        match_result = MatchResult(
            matched=[],
            unmatched_ynab=[],
            unmatched_shipments=[],
            excluded_shipments=[(shipment, "shipment_status=Not Available")],
            parse_errors=[],
        )

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            match_result=match_result,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        excluded = payload["amazon"]["excluded_shipments"]
        assert len(excluded) == 1
        entry = excluded[0]
        assert entry["reason"] == "shipment_status=Not Available"
        assert entry["shipment"]["order_id"] == "111-EXCL-0000001"
        assert entry["shipment"]["payment_method_last4"] == "0804"
        assert entry["shipment"]["total_amount"] == "52.50"

    def test_unmatched_shipments_mixed_none_dates_sorts(self, tmp_path):
        """Issue #143: ship_date None alongside real dates must sort without TypeError."""
        from enrich import write_unified_changeset
        from amazon_matcher import AmazonShipment, MatchResult
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        ship_with_date = AmazonShipment(
            order_id="111-AAA",
            ship_date=datetime(2026, 4, 1).date(),
            payment_method_raw="Visa - XXXX",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("10.00"),
            tax=Decimal("0"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("10.00"),
            items=[],
            shipment_status="Shipped",
        )
        ship_without_date = AmazonShipment(
            order_id="111-BBB",
            ship_date=None,
            payment_method_raw="Visa - XXXX",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("20.00"),
            tax=Decimal("0"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("20.00"),
            items=[],
            shipment_status="Not Available",
        )

        match_result = MatchResult(
            matched=[],
            unmatched_ynab=[],
            unmatched_shipments=[ship_without_date, ship_with_date],
            excluded_shipments=[
                (ship_without_date, "no_date"),
                (ship_with_date, "no_date"),
            ],
            parse_errors=[],
        )

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
            source_txns=[],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            match_result=match_result,
            now=now,
        )

        payload = json.loads(json_path.read_text())
        unmatched = payload["amazon"]["unmatched_shipments"]
        assert len(unmatched) == 2
        # Real-date shipment sorts before None-date shipment (None last) per amazon_matcher pattern
        assert unmatched[0]["order_id"] == "111-AAA"
        assert unmatched[1]["order_id"] == "111-BBB"

        excluded = payload["amazon"]["excluded_shipments"]
        assert len(excluded) == 2
        assert excluded[0]["shipment"]["order_id"] == "111-AAA"
        assert excluded[1]["shipment"]["order_id"] == "111-BBB"

    def test_proposed_splits_schema_and_account_name(self, tmp_path):
        """Issue #145 + #144: amazon split schema fields + account_name_lookup resolution."""
        from enrich import write_unified_changeset
        from datetime import datetime
        from categorizer import AmazonSplitProposal, ItemCategoryResult
        from amazon_matcher import AmazonShipment, AmazonItem

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        item = AmazonItem(
            order_id="111-SPLIT-0000001",
            ship_date=datetime(2026, 3, 25).date(),
            asin="B000TEST",
            product_name="Test Product",
            quantity=1,
            unit_price=Decimal("50.00"),
            unit_price_tax=Decimal("2.50"),
            raw_row_index=2,
        )
        shipment = AmazonShipment(
            order_id="111-SPLIT-0000001",
            ship_date=datetime(2026, 3, 25).date(),
            payment_method_raw="Visa - 0804",
            payment_method_last4="0804",
            is_split_tender=False,
            currency="USD",
            item_subtotal=Decimal("50.00"),
            tax=Decimal("2.50"),
            shipping=Decimal("0"),
            discounts=Decimal("0"),
            total_amount=Decimal("52.50"),
            items=[item],
            shipment_status="Shipped",
        )
        sub = ItemCategoryResult(
            ynab_transaction_id="txn-amz-1",
            item=item,
            allocated_amount=Decimal("52.50"),
            category_id="cat-house",
            category_name="House",
            confidence=0.9,
            rationale="Test rationale",
        )
        parent_txn = {
            "id": "txn-amz-1",
            "date": "2026-03-25",
            "amount": -52500,
            "payee_name": "Amazon.com",
            "account_id": "acct-1",
        }
        proposal = AmazonSplitProposal(
            parent_ynab_txn=parent_txn,
            shipment=shipment,
            subtransactions=[sub],
        )

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[proposal],
            source_txns=[parent_txn],
            budget_id="b123",
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            dump_path=None,
            out_dir=out_dir,
            account_name_lookup={"acct-1": "Amazon Visa 0804"},
            now=now,
        )

        payload = json.loads(json_path.read_text())
        # #145: payload uses 'proposed_splits' (matching amazon_matcher schema)
        assert "proposed_splits" in payload["amazon"]
        splits = payload["amazon"]["proposed_splits"]
        assert len(splits) == 1
        s = splits[0]
        assert s["transaction_id"] == "txn-amz-1"
        assert s["parent_ynab_transaction"]["id"] == "txn-amz-1"
        assert s["parent_ynab_transaction"]["amount"] == -52500
        # #144: account_name resolved via lookup
        assert s["account_name"] == "Amazon Visa 0804"
        # #145: shipment block has full fields
        assert s["shipment"]["order_id"] == "111-SPLIT-0000001"
        assert s["shipment"]["payment_method_last4"] == "0804"
        assert s["shipment"]["total_amount"] == "52.50"
        assert s["shipment"]["item_count"] == 1
        # #145: subtransactions have allocated_amount, confidence, rationale, item block
        assert len(s["subtransactions"]) == 1
        sub_out = s["subtransactions"][0]
        assert sub_out["allocated_amount"] == "52.50"
        assert sub_out["category_id"] == "cat-house"
        assert sub_out["category_name"] == "House"
        assert sub_out["confidence"] == 0.9
        assert sub_out["rationale"] == "Test rationale"
        assert sub_out["item"]["asin"] == "B000TEST"
        assert sub_out["item"]["product_name"] == "Test Product"
        assert sub_out["item"]["quantity"] == 1


class TestSummarizeSplitItems:
    """Items-by-category formatter used in the Amazon splits markdown table."""

    def _sub(self, category_name, product_name):
        from amazon_matcher import AmazonItem
        from categorizer import ItemCategoryResult

        item = AmazonItem(
            order_id="111-0000001-0000001",
            ship_date=None,
            asin="B0TEST",
            product_name=product_name,
            quantity=1,
            unit_price=Decimal("5.00"),
            unit_price_tax=Decimal("0"),
            raw_row_index=2,
        )
        return ItemCategoryResult(
            ynab_transaction_id="t",
            item=item,
            allocated_amount=Decimal("5.00"),
            category_id="cid",
            category_name=category_name,
            confidence=0.9,
            rationale="r",
        )

    def test_single_sub_shows_category_and_first_item(self):
        from enrich import _summarize_split_items

        out = _summarize_split_items([self._sub("Computer", "Logitech MX Mouse")])
        assert out == "Computer (Logitech MX Mouse)"

    def test_groups_by_category_with_count_and_first_item(self):
        from enrich import _summarize_split_items

        subs = [
            self._sub("Groceries", "Organic Spinach"),
            self._sub("Groceries", "Whole Foods Coffee"),
            self._sub("Groceries", "Sliced Turkey"),
            self._sub("Household supplies", "Paper Towels"),
            self._sub("Household supplies", "Dish Soap"),
        ]
        out = _summarize_split_items(subs)
        assert out == "Groceries ×3 (Organic Spinach), Household supplies ×2 (Paper Towels)"

    def test_long_product_name_is_truncated(self):
        from enrich import _summarize_split_items

        long_name = "X" * 100
        out = _summarize_split_items([self._sub("Groceries", long_name)])
        assert long_name not in out
        assert "..." in out

    def test_empty_subs_returns_empty_string(self):
        from enrich import _summarize_split_items

        assert _summarize_split_items([]) == ""


class TestDumpFreshnessWarning:
    """Test _dump_freshness_warning function."""

    def test_empty_shipments_returns_warning(self):
        """Empty shipments list returns 0-shipments message."""
        from enrich import _dump_freshness_warning

        result = _dump_freshness_warning([], "2026-03-28", 30)
        assert result is not None
        assert "0 shipments" in result

    def test_no_ship_dates_returns_warning(self):
        """All shipments with None ship_date returns unparseable message."""
        from enrich import _dump_freshness_warning
        from amazon_matcher import AmazonShipment

        shipments = [
            AmazonShipment(
                order_id="111-0000001-0000001",
                ship_date=None,
                payment_method_raw="Visa - XXXX",
                payment_method_last4="0804",
                is_split_tender=False,
                currency="USD",
                item_subtotal=Decimal("10.00"),
                tax=Decimal("0.50"),
                shipping=Decimal("0"),
                discounts=Decimal("0"),
                total_amount=Decimal("10.50"),
                items=[],
                shipment_status="Not Available",
            )
        ]

        result = _dump_freshness_warning(shipments, "2026-03-28", 30)
        assert result is not None
        assert "no parseable ship dates" in result

    def test_stale_dump_returns_warning(self):
        """Ship date before window start returns stale dump warning."""
        from enrich import _dump_freshness_warning
        from amazon_matcher import AmazonShipment
        from datetime import date

        shipments = [
            AmazonShipment(
                order_id="111-0000001-0000001",
                ship_date=date(2026, 3, 20),
                payment_method_raw="Visa - XXXX",
                payment_method_last4="0804",
                is_split_tender=False,
                currency="USD",
                item_subtotal=Decimal("10.00"),
                tax=Decimal("0.50"),
                shipping=Decimal("0"),
                discounts=Decimal("0"),
                total_amount=Decimal("10.50"),
                items=[],
                shipment_status="Shipped",
            )
        ]

        result = _dump_freshness_warning(shipments, "2026-03-28", 30)
        assert result is not None
        assert "before" in result.lower()
        assert "2026-03-28" in result

    def test_fresh_dump_returns_none(self):
        """Ship date on/after window start returns None."""
        from enrich import _dump_freshness_warning
        from amazon_matcher import AmazonShipment
        from datetime import date

        shipments = [
            AmazonShipment(
                order_id="111-0000001-0000001",
                ship_date=date(2026, 3, 28),
                payment_method_raw="Visa - XXXX",
                payment_method_last4="0804",
                is_split_tender=False,
                currency="USD",
                item_subtotal=Decimal("10.00"),
                tax=Decimal("0.50"),
                shipping=Decimal("0"),
                discounts=Decimal("0"),
                total_amount=Decimal("10.50"),
                items=[],
                shipment_status="Shipped",
            )
        ]

        result = _dump_freshness_warning(shipments, "2026-03-28", 30)
        assert result is None

    def test_fresh_dump_after_returns_none(self):
        """Ship date after window start returns None."""
        from enrich import _dump_freshness_warning
        from amazon_matcher import AmazonShipment
        from datetime import date

        shipments = [
            AmazonShipment(
                order_id="111-0000001-0000001",
                ship_date=date(2026, 3, 30),
                payment_method_raw="Visa - XXXX",
                payment_method_last4="0804",
                is_split_tender=False,
                currency="USD",
                item_subtotal=Decimal("10.00"),
                tax=Decimal("0.50"),
                shipping=Decimal("0"),
                discounts=Decimal("0"),
                total_amount=Decimal("10.50"),
                items=[],
                shipment_status="Shipped",
            )
        ]

        result = _dump_freshness_warning(shipments, "2026-03-28", 30)
        assert result is None


class TestPrintUnifiedSummary:
    """Test _print_unified_summary function."""

    def test_print_unified_summary_labels(self, capsys):
        """Summary output contains all required labels with exact format."""
        from enrich import _print_unified_summary
        import re

        _print_unified_summary(
            dump_path=Path("data/imports/amazon-order-history.zip"),
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            writable_count=10,
            amazon_splits_count=2,
            non_amazon_count=5,
            skipped_count=2,
            unmatched_amazon_count=1,
            md_path=Path("data/cache/enrich-changeset-20260427-143000.md"),
            json_path=Path("data/cache/enrich-changeset-20260427-143000.json"),
        )

        captured = capsys.readouterr()
        text = captured.out

        required_labels = [
            "Dump:",
            "Days back:",
            "K:",
            "Amazon splits:",
            "Non-Amazon:",
            "Skipped:",
            "Unmatched:",
            "Changeset:",
        ]

        for label in required_labels:
            assert label in text, f"Missing label: '{label}'"

        assert "Unmatched Amazon txns:" not in text, "Old label 'Unmatched Amazon txns:' should not appear; use 'Unmatched:'"

    def test_print_unified_summary_counts(self, capsys):
        """Summary output includes correct counts."""
        from enrich import _print_unified_summary

        _print_unified_summary(
            dump_path=Path("data/imports/amazon-order-history.zip"),
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            writable_count=10,
            amazon_splits_count=2,
            non_amazon_count=5,
            skipped_count=2,
            unmatched_amazon_count=1,
            md_path=Path("data/cache/enrich-changeset-20260427-143000.md"),
            json_path=Path("data/cache/enrich-changeset-20260427-143000.json"),
        )

        captured = capsys.readouterr()
        assert "10" in captured.out
        assert "2" in captured.out
        assert "5" in captured.out

    def test_print_unified_summary_no_emojis(self, capsys):
        """Summary output contains no emoji characters."""
        from enrich import _print_unified_summary

        _print_unified_summary(
            dump_path=Path("data/imports/amazon-order-history.zip"),
            since_date="2026-03-28",
            days_back=30,
            K=312,
            confidence_threshold=0.0096,
            writable_count=10,
            amazon_splits_count=2,
            non_amazon_count=5,
            skipped_count=2,
            unmatched_amazon_count=1,
            md_path=Path("data/cache/enrich-changeset-20260427-143000.md"),
            json_path=Path("data/cache/enrich-changeset-20260427-143000.json"),
        )

        captured = capsys.readouterr()
        # Check for common emoji patterns (simplified check)
        assert "🚀" not in captured.out
        assert "✅" not in captured.out
        assert "❌" not in captured.out


class TestITEnrich:
    """Integration tests for enrich.py — exercises real categorize_transactions engine.

    The comprehensive test (test_it_enrich_full_fixture) validates all 17 assertions
    from the refined plan and exercises the full wiring end-to-end.
    """

    def test_it_enrich_full_fixture_warm_cache(self, tmp_path, monkeypatch, capsys):
        """IT-ENRICH: Full fixture with warm cache (17 key assertions on wiring)."""
        import ynab_client as _ynab_mod
        import categorizer as _categorizer_mod
        import amazon_matcher as _amazon_mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")

        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "budget-uuid-test")
        (tmp_path / "config.json").write_text(json.dumps({
            "amazon": {"account_last4": {"acct-1": "0804"}, "date_window_days": 3}
        }))

        imports_dir = tmp_path / "data" / "imports"
        imports_dir.mkdir(parents=True)
        fixtures_dir = Path(__file__).parent.parent / "data" / "fixtures"

        csv_content = (fixtures_dir / "amazon_order_history_sample.csv").read_text()
        dump_path = imports_dir / "amazon-order-history-2026-04-13.zip"
        with ZipFile(dump_path, "w") as zf:
            zf.writestr("Your Amazon Orders/Order History.csv", csv_content)

        cache_dir = tmp_path / "data" / "cache"
        cache_dir.mkdir(parents=True)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        today = datetime(2026, 4, 13)
        window_start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        k_start = (today - timedelta(days=548)).strftime("%Y-%m-%d")

        categories_data = json.loads((fixtures_dir / "ynab_categories.json").read_text())
        categories = categories_data["data"]["category_groups"]
        accounts = [{"id": "acct-1", "name": "Amazon Visa 0804"}]

        ynab_txns = [
            {"id": "amz1", "payee_name": "Amazon.com", "amount_dollars": "10.50", "date": "2026-04-13", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "account_id": "acct-1"},
            {"id": "amz2", "payee_name": "Amazon.com", "amount_dollars": "25.00", "date": "2026-04-12", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "account_id": "acct-1"},
            {"id": "non_amz1", "payee_name": "Whole Foods", "amount_dollars": "50.00", "date": "2026-04-13", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False},
            {"id": "non_amz2", "payee_name": "Whole Foods", "amount_dollars": "30.00", "date": "2026-04-12", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False},
            {"id": "non_amz_novel", "payee_name": "Novel Store", "amount_dollars": "15.00", "date": "2026-04-11", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False},
            {"id": "transfer", "payee_name": "Transfer : Savings", "amount_dollars": "-200.00", "date": "2026-04-10", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False},
            {"id": "reconciled", "payee_name": "Some Store", "amount_dollars": "20.00", "date": "2026-04-09", "category_id": None, "cleared": "reconciled", "deleted": False, "approved": False},
            {"id": "deleted", "payee_name": "Deleted Store", "amount_dollars": "10.00", "date": "2026-04-08", "category_id": None, "cleared": "cleared", "deleted": True, "approved": False},
            {"id": "categorized", "payee_name": "Already Cat", "amount_dollars": "5.00", "date": "2026-04-07", "category_id": "dddddddd-0000-0000-0000-000000000001", "cleared": "cleared", "deleted": False, "approved": True},
        ]

        get_transactions_calls = []
        def mock_get_transactions(self, budget_id, since_date=None):
            get_transactions_calls.append(since_date)
            return ynab_txns, None

        def mock_get_categories(self, budget_id):
            return categories

        def mock_get_accounts(self, budget_id):
            return accounts

        monkeypatch.setattr(_ynab_mod.YNABClient, "get_transactions", mock_get_transactions)
        monkeypatch.setattr(_ynab_mod.YNABClient, "get_categories", mock_get_categories)
        monkeypatch.setattr(_ynab_mod.YNABClient, "get_accounts", mock_get_accounts)
        monkeypatch.setattr(_ynab_mod.YNABClient, "resolve_budget_id", lambda self, name: "budget-uuid-test")
        monkeypatch.setattr(_categorizer_mod.anthropic, "Anthropic", _ITEnrichAnthropicMock)

        payee_cache = {
            "_version": 2,
            "Whole Foods": {"category_id": "cat_groceries", "category_name": "Groceries", "tier": "history", "prior_strength": 5}
        }

        mock_match_result = Mock()
        mock_match_result.matched = [Mock(id="amz1"), Mock(id="amz2")]
        mock_match_result.unmatched_shipments = []
        mock_match_result.unmatched_ynab = []
        mock_match_result.excluded_shipments = []
        mock_match_result.parse_errors = []

        from enrich import main

        from categorizer import CategoryResult
        mock_category_results = [
            CategoryResult(
                transaction_id="non_amz1", tier="history",
                category_id="cat_groceries", category_name="Groceries",
                confidence=0.95, rationale="History tier", prior_strength=5
            ),
            CategoryResult(
                transaction_id="non_amz2", tier="history",
                category_id="cat_groceries", category_name="Groceries",
                confidence=0.95, rationale="History tier", prior_strength=5
            ),
            CategoryResult(
                transaction_id="non_amz_novel", tier="claude",
                category_id="cat_groceries", category_name="Groceries",
                confidence=0.90, rationale="Claude tier", prior_strength=1
            ),
        ]

        with patch("enrich.datetime") as mock_datetime:
            mock_datetime.now.return_value = today
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            with patch("enrich.match_shipments_to_transactions", return_value=mock_match_result):
                with patch("enrich.categorize_transactions", return_value=(mock_category_results, [], [], [])):
                    with patch("enrich.load_payee_cache", return_value=payee_cache):
                        with patch("enrich.save_payee_cache"):
                            result = main(["--days", "30", "--out-dir", str(out_dir)])

        assert result == 0, "Assertion #1: result == 0"

        assert get_transactions_calls.count(window_start) == 1, f"Assertion #2: window call"
        assert get_transactions_calls.count(k_start) == 1, f"Assertion #3: k_window call"
        assert get_transactions_calls.count(None) == 0, f"Assertion #6a: no bootstrap (warm)"

        json_files = list(out_dir.glob("enrich-changeset-*.json"))
        md_files = list(out_dir.glob("enrich-changeset-*.md"))
        assert len(json_files) == 1, f"Assertion #8: exactly 1 JSON"
        assert len(md_files) == 1, f"Assertion #8: exactly 1 MD"

        changeset = json.loads(json_files[0].read_text())
        assert changeset.get("version") == 1, "Assertion #9: version"
        assert changeset.get("kind") == "enrich-changeset", "Assertion #9: kind"

        non_amazon = changeset.get("non_amazon", {}).get("proposals", [])
        assert len(non_amazon) >= 1, f"Assertion #11: non_amazon has proposals (got {len(non_amazon)})"

        # Assertion #13: No txn id appears in both buckets (set intersection empty)
        amazon_splits = changeset.get("amazon", {}).get("proposed_splits", [])
        non_amazon_ids = {t.get("transaction_id") for t in non_amazon if isinstance(t, dict)}
        split_ids = {s.get("transaction_id") for s in amazon_splits if isinstance(s, dict)}
        overlap = non_amazon_ids & split_ids
        assert len(overlap) == 0, f"Assertion #13: no overlap between splits and proposals (found {overlap})"

        # Assertion #14: Reconciled, deleted, already-categorized appear in NO output bucket
        skipped_transfers = changeset.get("non_amazon", {}).get("skipped_transfers", [])
        all_output_ids = non_amazon_ids | split_ids | {t.get("id") for t in skipped_transfers if isinstance(t, dict)}
        excluded_ids = {"reconciled", "deleted", "categorized"}
        for excl_id in excluded_ids:
            assert excl_id not in all_output_ids, f"Assertion #14: {excl_id} excluded from all buckets"

        captured = capsys.readouterr()
        assert "Amazon splits:" in captured.out, "Assertion #15: splits label"
        assert "Non-Amazon:" in captured.out, "Assertion #15: non-amazon label"
        assert "Skipped:" in captured.out, "Assertion #15: skipped label"
        assert "Unmatched:" in captured.out, "Assertion #15: unmatched label"
        assert "Changeset:" in captured.out, "Assertion #15: changeset label"

    def test_it_enrich_full_fixture_cold_cache(self, tmp_path, monkeypatch, capsys):
        """IT-ENRICH: Full fixture with cold cache (bootstrap fetch happens)."""
        import ynab_client as _ynab_mod
        import categorizer as _categorizer_mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")

        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "budget-uuid-test")
        (tmp_path / "config.json").write_text(json.dumps({
            "amazon": {"account_last4": {"acct-1": "0804"}, "date_window_days": 3}
        }))

        imports_dir = tmp_path / "data" / "imports"
        imports_dir.mkdir(parents=True)
        fixtures_dir = Path(__file__).parent.parent / "data" / "fixtures"

        csv_content = (fixtures_dir / "amazon_order_history_sample.csv").read_text()
        dump_path = imports_dir / "amazon-order-history-2026-04-13.zip"
        with ZipFile(dump_path, "w") as zf:
            zf.writestr("Your Amazon Orders/Order History.csv", csv_content)

        cache_dir = tmp_path / "data" / "cache"
        cache_dir.mkdir(parents=True)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        today = datetime(2026, 4, 13)
        window_start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        k_start = (today - timedelta(days=548)).strftime("%Y-%m-%d")

        categories_data = json.loads((fixtures_dir / "ynab_categories.json").read_text())
        categories = categories_data["data"]["category_groups"]
        accounts = [{"id": "acct-1", "name": "Amazon Visa 0804"}]

        ynab_txns = [
            {"id": "non_amz1", "payee_name": "Whole Foods", "amount_dollars": "50.00", "date": "2026-04-13", "category_id": None, "cleared": "cleared", "deleted": False},
            {"id": "non_amz2", "payee_name": "Whole Foods", "amount_dollars": "30.00", "date": "2026-04-12", "category_id": None, "cleared": "cleared", "deleted": False},
        ]

        get_transactions_calls = []
        def mock_get_transactions(self, budget_id, since_date=None):
            get_transactions_calls.append(since_date)
            return ynab_txns, None

        def mock_get_categories(self, budget_id):
            return categories

        def mock_get_accounts(self, budget_id):
            return accounts

        monkeypatch.setattr(_ynab_mod.YNABClient, "get_transactions", mock_get_transactions)
        monkeypatch.setattr(_ynab_mod.YNABClient, "get_categories", mock_get_categories)
        monkeypatch.setattr(_ynab_mod.YNABClient, "get_accounts", mock_get_accounts)
        monkeypatch.setattr(_ynab_mod.YNABClient, "resolve_budget_id", lambda self, name: "budget-uuid-test")
        monkeypatch.setattr(_categorizer_mod.anthropic, "Anthropic", _ITEnrichAnthropicMock)

        from enrich import main
        from categorizer import CategoryResult

        mock_match_result = Mock()
        mock_match_result.matched = []
        mock_match_result.unmatched_shipments = []
        mock_match_result.unmatched_ynab = []
        mock_match_result.excluded_shipments = []
        mock_match_result.parse_errors = []

        mock_category_results = [
            CategoryResult(
                transaction_id="non_amz1", tier="history",
                category_id="cat_groceries", category_name="Groceries",
                confidence=0.95, rationale="History tier", prior_strength=5
            ),
            CategoryResult(
                transaction_id="non_amz2", tier="history",
                category_id="cat_groceries", category_name="Groceries",
                confidence=0.95, rationale="History tier", prior_strength=5
            ),
        ]

        with patch("enrich.datetime") as mock_datetime:
            mock_datetime.now.return_value = today
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            with patch("enrich.match_shipments_to_transactions", return_value=mock_match_result):
                with patch("enrich.categorize_transactions", return_value=(mock_category_results, [], [], [])):
                    with patch("enrich.load_payee_cache", return_value=None):
                        with patch("enrich.build_cache_from_transactions", return_value={"_version": 2}):
                            with patch("enrich.save_payee_cache"):
                                result = main(["--days", "30", "--out-dir", str(out_dir)])

        assert result == 0
        assert get_transactions_calls.count(window_start) == 1, f"Assertion #2: window call"
        assert get_transactions_calls.count(k_start) == 1, f"Assertion #3: k_window call"
        assert get_transactions_calls.count(None) == 1, f"Assertion #6b: bootstrap fetch for cold cache (got {get_transactions_calls.count(None)})"
        assert len(get_transactions_calls) == 3, f"Assertion #7b: cold cache has 3 total calls (got {len(get_transactions_calls)})"

    def test_it_enrich_exercises_real_engine_warm_cache(self, tmp_path, monkeypatch, capsys):
        """IT-ENRICH: Full fixture with REAL categorize_transactions engine (warm cache).

        Exercises the actual engine end-to-end, not a mock. Validates all 17 assertions.
        """
        import ynab_client as _ynab_mod
        import categorizer as _categorizer_mod

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")

        monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "budget-uuid-test")
        (tmp_path / "config.json").write_text(json.dumps({
            "amazon": {"account_last4": {"acct-1": "0804"}, "date_window_days": 3}
        }))

        imports_dir = tmp_path / "data" / "imports"
        imports_dir.mkdir(parents=True)
        fixtures_dir = Path(__file__).parent.parent / "data" / "fixtures"

        csv_content = (fixtures_dir / "amazon_order_history_sample.csv").read_text()
        dump_path = imports_dir / "amazon-order-history-2026-04-13.zip"
        with ZipFile(dump_path, "w") as zf:
            zf.writestr("Your Amazon Orders/Order History.csv", csv_content)

        cache_dir = tmp_path / "data" / "cache"
        cache_dir.mkdir(parents=True)
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        today = datetime(2026, 4, 13)
        window_start = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        k_start = (today - timedelta(days=548)).strftime("%Y-%m-%d")

        categories_data = json.loads((fixtures_dir / "ynab_categories.json").read_text())
        categories = categories_data["data"]["category_groups"]
        accounts = [{"id": "acct-1", "name": "Amazon Visa 0804"}]

        ynab_txns = [
            {"id": "amz1", "payee_name": "Amazon.com", "amount_dollars": "10.50", "date": "2026-04-13", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "account_id": "acct-1", "import_payee_name": None},
            {"id": "amz_unmatch", "payee_name": "Amazon.com", "amount_dollars": "999.00", "date": "2026-04-01", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "account_id": "acct-1", "import_payee_name": None},
            {"id": "non_amz_history", "payee_name": "Whole Foods", "amount_dollars": "50.00", "date": "2026-04-13", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "import_payee_name": None},
            {"id": "non_amz_novel", "payee_name": "Novel Store", "amount_dollars": "15.00", "date": "2026-04-11", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "import_payee_name": None},
            {"id": "transfer", "payee_name": "Transfer : Savings", "amount_dollars": "-200.00", "date": "2026-04-10", "category_id": None, "cleared": "cleared", "deleted": False, "approved": False, "import_payee_name": None},
            {"id": "reconciled", "payee_name": "Some Store", "amount_dollars": "20.00", "date": "2026-04-09", "category_id": None, "cleared": "reconciled", "deleted": False, "approved": False, "import_payee_name": None},
            {"id": "deleted", "payee_name": "Deleted Store", "amount_dollars": "10.00", "date": "2026-04-08", "category_id": None, "cleared": "cleared", "deleted": True, "approved": False, "import_payee_name": None},
            {"id": "categorized", "payee_name": "Already Cat", "amount_dollars": "5.00", "date": "2026-04-07", "category_id": "dddddddd-0000-0000-0000-000000000001", "cleared": "cleared", "deleted": False, "approved": True, "import_payee_name": None},
        ]

        get_transactions_calls = []
        get_categories_calls = []
        get_accounts_calls = []

        def mock_get_transactions(self, budget_id, since_date=None):
            get_transactions_calls.append(since_date)
            return ynab_txns, None

        def mock_get_categories(self, budget_id):
            get_categories_calls.append(budget_id)
            return categories

        def mock_get_accounts(self, budget_id):
            get_accounts_calls.append(budget_id)
            return accounts

        monkeypatch.setattr(_ynab_mod.YNABClient, "get_transactions", mock_get_transactions)
        monkeypatch.setattr(_ynab_mod.YNABClient, "get_categories", mock_get_categories)
        monkeypatch.setattr(_ynab_mod.YNABClient, "get_accounts", mock_get_accounts)
        monkeypatch.setattr(_ynab_mod.YNABClient, "resolve_budget_id", lambda self, name: "budget-uuid-test")
        monkeypatch.setattr(_categorizer_mod.anthropic, "Anthropic", _ITEnrichAnthropicMock)

        # Warm cache with Whole Foods resolved to history tier
        payee_cache = {
            "_version": 2,
            "Whole Foods": {"category_id": "cat_groceries", "category_name": "Groceries", "tier": "history", "prior_strength": 5}
        }

        # Mock only the Amazon matcher, NOT categorize_transactions
        # To avoid complexity with item-level categorization, we'll have NO matched shipments
        # and both Amazon txns unmatched. This simplifies the test to focus on non-Amazon flow.
        mock_match_result = Mock()
        mock_match_result.matched = []
        mock_match_result.unmatched_shipments = []
        mock_match_result.unmatched_ynab = [
            (ynab_txns[0], "No matching shipment"),  # amz1
            (ynab_txns[1], "Amount mismatch"),        # amz_unmatch
        ]
        mock_match_result.excluded_shipments = []
        mock_match_result.parse_errors = []

        with patch("enrich.datetime") as mock_datetime:
            mock_datetime.now.return_value = today
            mock_datetime.side_effect = lambda *args, **kw: datetime(*args, **kw)
            with patch("enrich.match_shipments_to_transactions", return_value=mock_match_result):
                with patch("enrich.load_payee_cache", return_value=payee_cache):
                    # Do NOT mock save_payee_cache so cache is actually saved to disk
                    result = main(["--days", "30", "--out-dir", str(out_dir)])

        # ========== 17 ASSERTIONS ==========

        # Assertion #1: exit code 0
        assert result == 0, "Assertion #1: result == 0"

        # Assertion #2: exactly one window call
        assert get_transactions_calls.count(window_start) == 1, f"Assertion #2: exactly 1 window call (got {get_transactions_calls.count(window_start)})"

        # Assertion #3: exactly one K-window call
        assert get_transactions_calls.count(k_start) == 1, f"Assertion #3: exactly 1 K-window call (got {get_transactions_calls.count(k_start)})"

        # Assertion #4: get_categories called exactly once
        assert len(get_categories_calls) == 1, f"Assertion #4: get_categories called once (got {len(get_categories_calls)})"

        # Assertion #5: get_accounts called exactly once
        assert len(get_accounts_calls) == 1, f"Assertion #5: get_accounts called once (got {len(get_accounts_calls)})"

        # Assertion #6a: no bootstrap for warm cache
        assert get_transactions_calls.count(None) == 0, f"Assertion #6a: no bootstrap (warm), got {get_transactions_calls.count(None)}"

        # Assertion #7a: total calls = 2 for warm cache
        assert len(get_transactions_calls) == 2, f"Assertion #7a: warm cache has 2 total calls (got {len(get_transactions_calls)})"

        # Assertion #8: exactly 1 JSON and 1 MD file
        json_files = list(out_dir.glob("enrich-changeset-*.json"))
        md_files = list(out_dir.glob("enrich-changeset-*.md"))
        assert len(json_files) == 1, f"Assertion #8: exactly 1 JSON (got {len(json_files)})"
        assert len(md_files) == 1, f"Assertion #8: exactly 1 MD (got {len(md_files)})"

        # Assertion #9: JSON has version==1, kind=="enrich-changeset"
        changeset = json.loads(json_files[0].read_text())
        assert changeset.get("version") == 1, "Assertion #9: version == 1"
        assert changeset.get("kind") == "enrich-changeset", "Assertion #9: kind == 'enrich-changeset'"

        # Assertion #10: Amazon split count and unmatched count
        # (Simplified: no matched splits, both Amazon txns unmatched)
        amazon_splits = changeset.get("amazon", {}).get("proposed_splits", [])
        amazon_unmatched = changeset.get("amazon", {}).get("unmatched_ynab", [])
        assert len(amazon_splits) == 0, f"Assertion #10: 0 Amazon splits (got {len(amazon_splits)})"
        assert len(amazon_unmatched) == 2, f"Assertion #10: 2 unmatched Amazon txns (got {len(amazon_unmatched)})"

        # Assertion #11: Non-Amazon flat count (Whole Foods history + Novel Claude)
        # Note: transfer is excluded, so only 2 proposable non-Amazon txns
        non_amazon_proposals = changeset.get("non_amazon", {}).get("proposals", [])
        assert len(non_amazon_proposals) == 2, f"Assertion #11: 2 non-Amazon proposals (1 history, 1 Claude), got {len(non_amazon_proposals)}"

        # Assertion #12: Skipped contains exactly the Transfer txn
        skipped_transfers = changeset.get("non_amazon", {}).get("skipped_transfers", [])
        assert len(skipped_transfers) == 1, f"Assertion #12: exactly 1 skipped (transfer), got {len(skipped_transfers)}"
        assert skipped_transfers[0].get("id") == "transfer", f"Assertion #12: skipped is transfer txn"

        # Assertion #13: No txn id appears in both amazon.splits and non_amazon.proposals
        split_ids = {s.get("transaction_id") for s in amazon_splits}
        proposal_ids = {p.get("transaction_id") for p in non_amazon_proposals}
        overlap = split_ids & proposal_ids
        assert len(overlap) == 0, f"Assertion #13: no overlap between splits and proposals (found {overlap})"

        # Assertion #14: Reconciled, deleted, already-categorized appear in NO output bucket
        all_output_ids = split_ids | proposal_ids | {t.get("id") for t in skipped_transfers}
        excluded_ids = {"reconciled", "deleted", "categorized"}
        for excl_id in excluded_ids:
            assert excl_id not in all_output_ids, f"Assertion #14: {excl_id} excluded from all buckets"

        # Assertion #15: Summary labels present
        captured = capsys.readouterr()
        assert "Amazon splits:" in captured.out, "Assertion #15: splits label"
        assert "Non-Amazon:" in captured.out, "Assertion #15: non-amazon label"
        assert "Skipped:" in captured.out, "Assertion #15: skipped label"
        assert "Unmatched:" in captured.out, "Assertion #15: unmatched label"
        assert "Changeset:" in captured.out, "Assertion #15: changeset label"

        # Assertion #16: Cache contains entry for novel Claude payee (Novel Store)
        cache_path = cache_dir / "payee_lookup.json"
        assert cache_path.exists(), "Assertion #16: cache file exists"
        cache_content = json.loads(cache_path.read_text())
        # Novel Store should have been added by Claude and cached
        assert "Novel Store" in cache_content or any("novel" in k.lower() for k in cache_content.keys()), \
            f"Assertion #16: Novel Store or variant in cache (got {list(cache_content.keys())[:5]})"

        # Assertion #17: Cache does NOT contain Amazon item subcategories
        # (Item-level results should not update payee cache)
        amazon_item_keys = [k for k in cache_content.keys() if k.startswith("Amazon") and "_item_" in k]
        assert len(amazon_item_keys) == 0, f"Assertion #17: no Amazon item keys in cache (found {amazon_item_keys})"
