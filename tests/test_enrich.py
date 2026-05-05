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
