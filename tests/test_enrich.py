"""Tests for enrich.py — unified categorize+enrich workflow."""
import pytest
import json
from pathlib import Path
from unittest.mock import patch, Mock
from decimal import Decimal
from enrich import main


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
        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        result = main(["--days", "30"])
        assert result == 1

    def test_missing_anthropic_key(self, tmp_path, monkeypatch):
        """Missing ANTHROPIC_API_KEY exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        result = main(["--days", "30"])
        assert result == 1

    def test_missing_config(self, tmp_path, monkeypatch):
        """Missing config.json exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")

        result = main(["--days", "30"])
        assert result == 1

    def test_malformed_config(self, tmp_path, monkeypatch):
        """Malformed config.json exits 1 with clear message."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        tmp_path.joinpath("config.json").write_text("{invalid json")

        result = main(["--days", "30"])
        assert result == 1

    def test_missing_budget_id(self, tmp_path, monkeypatch):
        """Missing budget_id in config exits 1."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        tmp_path.joinpath("config.json").write_text(json.dumps({}))

        result = main(["--days", "30"])
        assert result == 1

    def test_no_writable_txns(self, tmp_path, monkeypatch, capsys):
        """No writable txns exits 0, prints message, no changeset."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={}):
                result = main(["--days", "30"])

        assert result == 0
        captured = capsys.readouterr()
        assert "No uncategorized writable transactions found" in captured.out

    def test_ynab_call_count_warm_cache(self, tmp_path, monkeypatch):
        """YNAB get_transactions called exactly twice for warm cache."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")
        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []

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
        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        # Provide at least one writable transaction to avoid early return
        writable_txn = {"id": "t1", "payee_name": "Test Store", "category_id": None, "cleared": "cleared", "deleted": False}

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([writable_txn], 0)  # return 1 txn for all calls
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []

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
        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        mock_client = Mock()
        mock_client.get_transactions.return_value = ([], 0)
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = []

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={}):
                result = main(["--days", "30"])

        assert result == 0


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
        """Markdown output contains all required summary labels."""
        from enrich import write_unified_changeset
        from datetime import datetime

        out_dir = tmp_path / "changesets"
        now = datetime(2026, 4, 27, 14, 30, 0)

        md_path, json_path = write_unified_changeset(
            flat_results=[],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
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
            "## Amazon splits",
            "## Non-Amazon",
            "## Skipped",
            "## Unmatched Amazon txns",
            "## Changeset",
        ]
        for label in required_labels:
            assert label in markdown, f"Missing label: {label}"


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

        md_path, json_path = write_unified_changeset(
            flat_results=[result1, result2],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
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
        assert proposals[0]["category_id"] == "cat1"
        assert proposals[0]["category_name"] == "Groceries"
        assert proposals[0]["tier"] == "claude"
        assert proposals[0]["confidence"] == 0.95
        assert proposals[1]["transaction_id"] == "t2"
        assert proposals[1]["tier"] == "history"

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

        now = datetime(2026, 4, 27, 14, 30, 0)
        out_dir1 = tmp_path / "changesets1"
        out_dir2 = tmp_path / "changesets2"

        md1, json1 = write_unified_changeset(
            flat_results=[result],
            skipped=[],
            unmatched_amazon=[],
            split_proposals=[],
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


class TestITEnrich:
    """Integration tests for enrich.py — full call chain with fixtures."""

    def test_it_enrich_basic_flow(self, tmp_path, monkeypatch):
        """IT-ENRICH: Basic integration test with mocked YNAB."""
        from enrich import main

        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("YNAB_API_TOKEN", "token123")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "key123")

        tmp_path.joinpath("config.json").write_text(json.dumps({"budget_id": "b123"}))

        # Create transaction fixtures (non-Amazon only to avoid dump requirement)
        txn1 = {
            "id": "t1",
            "payee_name": "Whole Foods",
            "amount_dollars": "50.00",
            "date": "2026-03-30",
            "category_id": None,
            "cleared": "cleared",
            "deleted": False,
            "account_id": "acc1",
        }

        txn2 = {
            "id": "t2",
            "payee_name": "Grocery Store",
            "amount_dollars": "30.00",
            "date": "2026-03-30",
            "category_id": None,
            "cleared": "cleared",
            "deleted": False,
            "account_id": "acc1",
        }

        txn_reconciled = {
            "id": "t3",
            "payee_name": "Other Store",
            "amount_dollars": "20.00",
            "date": "2026-03-25",
            "category_id": None,
            "cleared": "reconciled",
            "deleted": False,
        }

        # Mock YNAB client
        mock_client = Mock()
        mock_client.get_transactions.return_value = (
            [txn1, txn2, txn_reconciled],
            0,
        )
        mock_client.get_categories.return_value = []
        mock_client.get_accounts.return_value = [{"id": "acc1", "name": "Checking"}]

        # Categorizer results (real CategoryResult objects)
        from categorizer import CategoryResult

        mock_result1 = CategoryResult(
            transaction_id="t1",
            category_id="cat1",
            category_name="Groceries",
            tier="claude",
            confidence=0.95,
            rationale="Whole Foods",
            prior_strength=1,
        )

        mock_result2 = CategoryResult(
            transaction_id="t2",
            category_id="cat1",
            category_name="Groceries",
            tier="history",
            confidence=0.99,
            rationale="From cache",
            prior_strength=1,
        )

        with patch("enrich.YNABClient", return_value=mock_client):
            with patch("enrich.load_payee_cache", return_value={}):
                with patch("enrich.build_cache_from_transactions", return_value={}):
                    with patch("enrich.categorize_transactions", return_value=(
                        [mock_result1, mock_result2],  # flat_results
                        [txn_reconciled],  # skipped
                        [],  # unmatched_amazon
                        [],  # split_proposals
                    )):
                        result = main(["--days", "30"])

        assert result == 0
        assert (tmp_path / "data/cache").exists()
        # Check that changeset files were created
        changesets = list((tmp_path / "data/cache").glob("enrich-changeset-*.json"))
        assert len(changesets) > 0, "No changeset files created"
