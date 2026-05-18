"""Tests for scripts/audit_categorization.py (Issue E)."""
import json
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, Mock, MagicMock
from audit import AuditFinding


def _import_script():
    import importlib.util
    import sys as _sys
    if "audit_categorization" in _sys.modules:
        return _sys.modules["audit_categorization"]
    script_path = Path(__file__).parent.parent / "scripts" / "audit_categorization.py"
    spec = importlib.util.spec_from_file_location("audit_categorization", script_path)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["audit_categorization"] = mod
    spec.loader.exec_module(mod)
    return mod


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


def _finding(txn_id="t1", payee="Netflix", conf=0.9):
    return AuditFinding(
        transaction_id=txn_id,
        payee_name=payee,
        amount_dollars=-15.0,
        date="2026-01-15",
        current_category_id="cat2",
        current_category_name="Groceries",
        suggested_category_id="cat1",
        suggested_category_name="Entertainment",
        confidence=conf,
        rationale="Netflix is streaming not groceries",
    )


def _categories():
    return [
        {"id": "g1", "name": "Bills", "categories": [
            {"id": "cat1", "name": "Utilities"},
        ]},
    ]


def _env(extra=None):
    base = {
        "YNAB_API_TOKEN": "tok",
        "ANTHROPIC_API_KEY": "ak",
        "YNAB_DEFAULT_BUDGET": "MyBudget",
    }
    if extra:
        base.update(extra)
    return base


# ---------------------------------------------------------------------------
# Helper: run main() with patched YNAB + Claude
# ---------------------------------------------------------------------------

def _run_main(tmp_path, argv, txns, categories, findings):
    mod = _import_script()

    mock_client = MagicMock()
    mock_client.resolve_budget_id.return_value = "budget-id"
    mock_client.get_transactions.return_value = (txns, 0)
    mock_client.get_categories.return_value = categories

    with patch.dict("os.environ", _env()), \
         patch("audit_categorization.YNABClient", return_value=mock_client), \
         patch("audit_categorization.audit_batch", return_value=findings), \
         patch("audit_categorization.load_dotenv"):
        return mod.main(argv + ["--out-dir", str(tmp_path)])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAuditScriptFiltering:

    def test_skips_uncategorized_transactions(self, tmp_path):
        uncategorized = _make_txn("t1", "Netflix", category_id=None, category_name=None)
        categorized = _make_txn("t2", "Spotify")

        mod = _import_script()
        mock_client = MagicMock()
        mock_client.resolve_budget_id.return_value = "budget-id"
        mock_client.get_transactions.return_value = ([uncategorized, categorized], 0)
        mock_client.get_categories.return_value = _categories()

        captured_batch = []

        def fake_audit_batch(txns, flat_cats, api_key):
            captured_batch.extend(txns)
            return []

        with patch.dict("os.environ", _env()), \
             patch("audit_categorization.YNABClient", return_value=mock_client), \
             patch("audit_categorization.audit_batch", side_effect=fake_audit_batch), \
             patch("audit_categorization.load_dotenv"):
            mod.main(["--days", "30", "--out-dir", str(tmp_path)])

        assert all(t["id"] != "t1" for t in captured_batch)
        assert any(t["id"] == "t2" for t in captured_batch)

    def test_skips_transfers(self, tmp_path):
        transfer = _make_txn("t1", "Transfer : Savings")
        normal = _make_txn("t2", "Netflix")

        mod = _import_script()
        mock_client = MagicMock()
        mock_client.resolve_budget_id.return_value = "budget-id"
        mock_client.get_transactions.return_value = ([transfer, normal], 0)
        mock_client.get_categories.return_value = _categories()

        captured_batch = []

        def fake_audit_batch(txns, flat_cats, api_key):
            captured_batch.extend(txns)
            return []

        with patch.dict("os.environ", _env()), \
             patch("audit_categorization.YNABClient", return_value=mock_client), \
             patch("audit_categorization.audit_batch", side_effect=fake_audit_batch), \
             patch("audit_categorization.load_dotenv"):
            mod.main(["--days", "30", "--out-dir", str(tmp_path)])

        assert all(t["id"] != "t1" for t in captured_batch)


class TestAuditScriptOutputFiles:

    def test_writes_markdown_and_json(self, tmp_path):
        txns = [_make_txn("t1", "Netflix")]
        findings = [_finding("t1")]
        _run_main(tmp_path, ["--days", "30"], txns, _categories(), findings)

        md_files = list(tmp_path.glob("audit-report-*.md"))
        json_files = list(tmp_path.glob("audit-findings-*.json"))
        assert len(md_files) == 1
        assert len(json_files) == 1

    def test_markdown_contains_finding_info(self, tmp_path):
        txns = [_make_txn("t1", "Netflix")]
        findings = [_finding("t1")]
        _run_main(tmp_path, ["--days", "30"], txns, _categories(), findings)

        md = list(tmp_path.glob("audit-report-*.md"))[0].read_text()
        assert "Netflix" in md
        assert "Groceries" in md

    def test_json_is_valid_and_contains_findings(self, tmp_path):
        txns = [_make_txn("t1", "Netflix")]
        findings = [_finding("t1")]
        _run_main(tmp_path, ["--days", "30"], txns, _categories(), findings)

        json_path = list(tmp_path.glob("audit-findings-*.json"))[0]
        data = json.loads(json_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["transaction_id"] == "t1"

    def test_handles_zero_findings(self, tmp_path):
        txns = [_make_txn("t1", "Netflix")]
        _run_main(tmp_path, ["--days", "30"], txns, _categories(), [])

        md = list(tmp_path.glob("audit-report-*.md"))[0].read_text()
        json_data = json.loads(
            list(tmp_path.glob("audit-findings-*.json"))[0].read_text()
        )
        assert "0 findings" in md or "No findings" in md or "no findings" in md
        assert json_data == []

    def test_json_has_required_fields(self, tmp_path):
        txns = [_make_txn("t1", "Netflix")]
        findings = [_finding("t1")]
        _run_main(tmp_path, ["--days", "30"], txns, _categories(), findings)

        json_path = list(tmp_path.glob("audit-findings-*.json"))[0]
        data = json.loads(json_path.read_text())
        item = data[0]
        for field in ("transaction_id", "payee_name", "current_category_id",
                      "confidence", "rationale"):
            assert field in item, f"Missing field: {field}"


class TestAuditScriptEnvErrors:

    def test_exits_on_missing_ynab_token(self, tmp_path):
        mod = _import_script()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "ak", "YNAB_DEFAULT_BUDGET": "B"},
                        clear=True), \
             patch("audit_categorization.load_dotenv"):
            rc = mod.main(["--days", "30", "--out-dir", str(tmp_path)])
        assert rc == 1

    def test_exits_on_missing_anthropic_key(self, tmp_path):
        mod = _import_script()
        with patch.dict("os.environ", {"YNAB_API_TOKEN": "tok", "YNAB_DEFAULT_BUDGET": "B"},
                        clear=True), \
             patch("audit_categorization.load_dotenv"):
            rc = mod.main(["--days", "30", "--out-dir", str(tmp_path)])
        assert rc == 1
