"""Tests for amazon_confirm.py — changeset loading and summarization."""
import io
import json
import os
import shutil
import pytest
import tempfile
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch, MagicMock
from amazon_confirm import load_changeset, summarize_changeset, ChangesetSummary, ApplyReport

FIXTURES = Path("data/fixtures")


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_load_changeset_happy_path():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    assert changeset["version"] == 1
    assert len(changeset["proposed_splits"]) == 6
    assert "summary" in changeset


def test_load_changeset_rejects_version_2():
    fixture = load_fixture("expected_amazon_changeset.json")
    fixture["version"] = 2
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="version"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_missing_proposed_splits_key():
    fixture = load_fixture("expected_amazon_changeset.json")
    del fixture["proposed_splits"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="proposed_splits"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_missing_summary_key():
    fixture = load_fixture("expected_amazon_changeset.json")
    del fixture["summary"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="summary"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_proposal_missing_parent_ynab_transaction():
    fixture = load_fixture("expected_amazon_changeset.json")
    del fixture["proposed_splits"][0]["parent_ynab_transaction"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="0.*parent_ynab_transaction"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_proposal_missing_subtransactions():
    fixture = load_fixture("expected_amazon_changeset.json")
    del fixture["proposed_splits"][0]["subtransactions"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="0.*subtransactions"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_changeset(Path("/tmp/nonexistent_changeset_xyz.json"))


def test_summarize_counts_total_proposals():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    summary = summarize_changeset(changeset)
    assert summary.total_proposals == 6


def test_summarize_counts_flat_vs_split():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    summary = summarize_changeset(changeset)
    assert summary.split_proposals + summary.flat_proposals == summary.total_proposals
    assert summary.split_proposals >= 0
    assert summary.flat_proposals >= 0


def test_summarize_counts_skipped_uncategorized():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    changeset["proposed_splits"][0]["subtransactions"][0]["category_id"] = None
    summary = summarize_changeset(changeset)
    assert summary.skipped_uncategorized == 1


def test_summarize_null_category_partial_proposal():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    proposal = changeset["proposed_splits"][0]
    if len(proposal["subtransactions"]) >= 2:
        proposal["subtransactions"][0]["category_id"] = None
        summary = summarize_changeset(changeset)
        assert summary.skipped_uncategorized == 1


def test_summarize_total_outflow_uses_decimal():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    summary = summarize_changeset(changeset)
    assert isinstance(summary.total_outflow_dollars, Decimal)
    assert summary.total_outflow_dollars > Decimal("0")


def test_summarize_by_category_groups_correctly():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    summary = summarize_changeset(changeset)
    assert isinstance(summary.by_category, dict)
    for cat_name, amount in summary.by_category.items():
        assert isinstance(amount, Decimal)


def test_summarize_by_category_excludes_null_category_name():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    for proposal in changeset["proposed_splits"]:
        for subtxn in proposal["subtransactions"]:
            subtxn["category_name"] = None
    summary = summarize_changeset(changeset)
    assert len(summary.by_category) == 0


def test_summarize_counts_previously_applied():
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    changeset["proposed_splits"][0]["applied_at"] = "2026-05-14T10:00:00"
    summary = summarize_changeset(changeset)
    assert summary.applied_previously == 1


def test_load_and_summarize_real_fixture():
    """Full call chain: load_changeset → summarize_changeset using the real fixture."""
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    summary = summarize_changeset(changeset)
    assert summary.total_proposals >= 1
    assert summary.total_outflow_dollars > Decimal("0")
    assert summary.split_proposals + summary.flat_proposals == summary.total_proposals


# Issue #5: proposal_to_patch_body() conversion


def test_single_subtxn_returns_flat_patch():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    flat_proposal = next((p for p in changeset["proposed_splits"] if len(p["subtransactions"]) == 1), None)
    if flat_proposal:
        result = proposal_to_patch_body(flat_proposal)
        assert result is not None
        assert "category_id" in result
        assert "subtransactions" not in result


def test_multi_subtxn_returns_split_patch():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    split_proposal = next((p for p in changeset["proposed_splits"] if len(p["subtransactions"]) >= 2), None)
    if split_proposal:
        result = proposal_to_patch_body(split_proposal)
        assert result is not None
        assert "subtransactions" in result
        assert len(result["subtransactions"]) == len(split_proposal["subtransactions"])


def test_subtxn_amounts_are_negative_milliunits():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    proposal = changeset["proposed_splits"][0]
    result = proposal_to_patch_body(proposal)
    assert result is not None
    if "subtransactions" in result:
        for subtxn in result["subtransactions"]:
            assert subtxn["amount"] < 0


def test_subtxn_amounts_are_integers_not_floats():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    proposal = changeset["proposed_splits"][0]
    result = proposal_to_patch_body(proposal)
    assert result is not None
    if "subtransactions" in result:
        for subtxn in result["subtransactions"]:
            assert isinstance(subtxn["amount"], int)


def test_split_amounts_sum_to_parent():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    split_proposal = next((p for p in changeset["proposed_splits"] if len(p["subtransactions"]) >= 2), None)
    if split_proposal:
        result = proposal_to_patch_body(split_proposal)
        assert result is not None
        if "subtransactions" in result:
            total = sum(s["amount"] for s in result["subtransactions"])
            assert total == split_proposal["parent_ynab_transaction"]["amount"]


def test_null_category_id_any_subtxn_returns_none():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    proposal = changeset["proposed_splits"][0]
    proposal["subtransactions"][0]["category_id"] = None
    result = proposal_to_patch_body(proposal)
    assert result is None


def test_memo_truncated_to_200_chars():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    split_proposal = next(
        (p for p in changeset["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None, "fixture has no multi-item splits"
    split_proposal["subtransactions"][0]["item"]["product_name"] = "x" * 250
    result = proposal_to_patch_body(split_proposal)
    assert result is not None
    assert "subtransactions" in result
    for subtxn in result["subtransactions"]:
        assert len(subtxn["memo"]) <= 200


def test_split_subtxn_memo_contains_real_product_name():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    split_proposal = next(
        (p for p in changeset["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None
    result = proposal_to_patch_body(split_proposal)
    assert result is not None
    for sub_in, sub_out in zip(split_proposal["subtransactions"], result["subtransactions"]):
        assert sub_in["item"]["product_name"][:128] in sub_out["memo"]


def test_split_subtxn_memo_contains_real_asin():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    split_proposal = next(
        (p for p in changeset["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None
    result = proposal_to_patch_body(split_proposal)
    assert result is not None
    for sub_in, sub_out in zip(split_proposal["subtransactions"], result["subtransactions"]):
        assert sub_in["item"]["asin"] in sub_out["memo"]


def test_proposal_to_patch_body_raises_when_item_missing():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    split_proposal = next(
        (p for p in changeset["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None
    del split_proposal["subtransactions"][0]["item"]
    with pytest.raises((KeyError, ValueError)):
        proposal_to_patch_body(split_proposal)


def test_flat_patch_preserves_parent_memo():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    flat_proposal = next((p for p in changeset["proposed_splits"] if len(p["subtransactions"]) == 1), None)
    if flat_proposal:
        parent_memo = flat_proposal["parent_ynab_transaction"].get("memo", "")
        result = proposal_to_patch_body(flat_proposal)
        assert result is not None
        if "subtransactions" not in result:
            assert result.get("memo") == parent_memo


def test_proposal_to_patch_body_with_all_fixture_proposals():
    """Exercise full call chain: load_changeset → each proposal through proposal_to_patch_body."""
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))
    for proposal in changeset["proposed_splits"]:
        result = proposal_to_patch_body(proposal)
        if result is not None and "subtransactions" in result:
            total = sum(s["amount"] for s in result["subtransactions"])
            assert total == proposal["parent_ynab_transaction"]["amount"]


# Issue #7: CLI entry point


@pytest.fixture
def cli_changeset_path(tmp_path):
    src = Path("data/fixtures/expected_amazon_changeset.json")
    dst = tmp_path / "amazon-changeset-test.json"
    shutil.copy(src, dst)
    return dst


@pytest.fixture
def fake_client_factory(monkeypatch):
    """Patch amazon_confirm.YNABClient to return a configurable MagicMock."""
    instances = []

    def _make(**overrides):
        mock = MagicMock()
        mock.sandbox_mode = False
        mock.resolve_budget_id.return_value = "budget-uuid-123"
        mock.get_budget.return_value = {"id": "budget-uuid-123", "name": "My Budget"}
        mock.rate_limit_remaining.return_value = 199
        mock.update_transaction.return_value = {"id": "txn-1"}
        for k, v in overrides.items():
            setattr(mock, k, v)
        instances.append(mock)
        return mock

    constructed = []

    def fake_ctor(*args, **kwargs):
        client = _make()
        constructed.append(client)
        return client

    import amazon_confirm
    monkeypatch.setattr(amazon_confirm, "YNABClient", fake_ctor)
    monkeypatch.setattr(amazon_confirm.time, "sleep", lambda *_: None)

    return {"make": _make, "constructed": constructed}


def _run_main(argv, monkeypatch, tmp_path, stdin_text="", env=None):
    """Invoke amazon_confirm.main() with isolated stdin/argv/cwd.

    chdir(tmp_path) so the default report_dir (Path("data/cache")) lands in
    the test's tmp directory rather than polluting the real repo cache.
    """
    import amazon_confirm
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["amazon_confirm.py"] + argv)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    if env is not None:
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
    return amazon_confirm.main()


def test_cli_dry_run_makes_no_writes(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path), "--dry-run", "--yes"], monkeypatch, tmp_path)
    assert code == 0
    for client in fake_client_factory["constructed"]:
        client.update_transaction.assert_not_called()


def test_cli_yes_skips_prompt(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path, stdin_text="")
    assert code == 0
    called = sum(c.update_transaction.call_count for c in fake_client_factory["constructed"])
    assert called > 0


def test_cli_prompt_n_exits_zero(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path)], monkeypatch, tmp_path, stdin_text="n\n")
    assert code == 0
    for client in fake_client_factory["constructed"]:
        client.update_transaction.assert_not_called()


def test_cli_prompt_y_proceeds(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path)], monkeypatch, tmp_path, stdin_text="y\n")
    assert code == 0
    called = sum(c.update_transaction.call_count for c in fake_client_factory["constructed"])
    assert called > 0


def test_cli_sandbox_mode_set_aborts(cli_changeset_path, fake_client_factory, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.setenv("YNAB_SANDBOX_MODE", "1")
    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    assert code == 2
    err = capsys.readouterr().err
    assert "sandbox" in err.lower()


def test_cli_missing_changeset_exits_3(tmp_path, fake_client_factory, monkeypatch):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    missing = tmp_path / "does-not-exist.json"
    code = _run_main([str(missing), "--yes"], monkeypatch, tmp_path)
    assert code == 3


def test_cli_exit_code_reflects_failures(cli_changeset_path, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    import amazon_confirm

    def fake_apply(*args, **kwargs):
        return ApplyReport(
            changeset_path=str(cli_changeset_path),
            completed_at="now",
            total=3,
            applied=[],
            skipped=[],
            failed=[{"txn_id": "t1", "http_status": 400, "error_id": "x", "error_name": "y", "detail": "z"}],
            aborted=False,
            abort_reason=None,
        )

    fake_client = MagicMock()
    fake_client.sandbox_mode = False
    fake_client.resolve_budget_id.return_value = "budget-uuid-123"
    fake_client.get_budget.return_value = {"id": "budget-uuid-123", "name": "My Budget"}
    monkeypatch.setattr(amazon_confirm, "YNABClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(amazon_confirm, "apply_changeset", fake_apply)
    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    assert code == 1


def test_cli_exit_code_reflects_aborted(cli_changeset_path, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    import amazon_confirm

    def fake_apply(*args, **kwargs):
        return ApplyReport(
            changeset_path=str(cli_changeset_path),
            completed_at="now",
            total=3,
            applied=[],
            skipped=[],
            failed=[],
            aborted=True,
            abort_reason="rate limit exceeded",
        )

    fake_client = MagicMock()
    fake_client.sandbox_mode = False
    fake_client.resolve_budget_id.return_value = "budget-uuid-123"
    fake_client.get_budget.return_value = {"id": "budget-uuid-123", "name": "My Budget"}
    monkeypatch.setattr(amazon_confirm, "YNABClient", lambda *a, **kw: fake_client)
    monkeypatch.setattr(amazon_confirm, "apply_changeset", fake_apply)
    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    assert code == 2


# Issue #6: apply_changeset() orchestration


@pytest.fixture
def apply_changeset_path(tmp_path):
    src = Path("data/fixtures/expected_amazon_changeset.json")
    dst = tmp_path / "amazon-changeset-orch.json"
    shutil.copy(src, dst)
    return dst


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 199
    client.update_transaction.return_value = {"id": "txn-1"}
    return client


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import amazon_confirm
    monkeypatch.setattr(amazon_confirm.time, "sleep", lambda *_: None)


def _read_changeset(path):
    return json.loads(path.read_text())


def test_apply_refuses_sandbox_mode(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    mock_client.sandbox_mode = True
    with pytest.raises(RuntimeError, match="sandbox"):
        apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    mock_client.update_transaction.assert_not_called()


def test_apply_creates_backup(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    original = apply_changeset_path.read_text()
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    bak = apply_changeset_path.with_suffix(".json.bak")
    assert bak.exists()
    assert bak.read_text() == original


def test_apply_does_not_duplicate_backup(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    bak = apply_changeset_path.with_suffix(".json.bak")
    bak.write_text('{"sentinel": "pre-existing-backup"}')
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert bak.read_text() == '{"sentinel": "pre-existing-backup"}'


def test_apply_dry_run_makes_no_patch_calls(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", dry_run=True, report_dir=tmp_path)
    mock_client.update_transaction.assert_not_called()
    assert any(s.get("reason") == "dry run" for s in report.skipped)


def test_apply_skips_proposals_with_applied_at(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    cs["proposed_splits"][0]["applied_at"] = "2026-05-14T10:00:00"
    apply_changeset_path.write_text(json.dumps(cs))
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    skip_reasons = [s["reason"] for s in report.skipped]
    assert any("already applied" in r for r in skip_reasons)
    assert mock_client.update_transaction.call_count == len(cs["proposed_splits"]) - 1


def test_apply_skips_proposals_with_null_category(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    cs["proposed_splits"][0]["subtransactions"][0]["category_id"] = None
    apply_changeset_path.write_text(json.dumps(cs))
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert any("uncategorized" in s["reason"] for s in report.skipped)
    assert mock_client.update_transaction.call_count == len(cs["proposed_splits"]) - 1


def test_apply_marks_applied_at_on_success(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    cs_after = _read_changeset(apply_changeset_path)
    for proposal in cs_after["proposed_splits"]:
        assert "applied_at" in proposal


def test_apply_flushes_file_after_each_success(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    from ynab_client import YNABValidationError

    call_count = {"n": 0}
    flushes_seen_at = []

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        cs_on_disk = _read_changeset(apply_changeset_path)
        applied_now = sum(1 for p in cs_on_disk["proposed_splits"] if "applied_at" in p)
        flushes_seen_at.append(applied_now)
        if call_count["n"] == 3:
            raise YNABValidationError(400, detail="boom")
        return {"id": "ok"}

    mock_client.update_transaction.side_effect = side_effect
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert flushes_seen_at[0] == 0
    assert flushes_seen_at[1] == 1
    assert flushes_seen_at[2] == 2


def test_apply_continues_after_409(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    from ynab_client import YNABConflictError

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise YNABConflictError(409, detail="conflict")
        return {"id": "ok"}

    mock_client.update_transaction.side_effect = side_effect
    cs = _read_changeset(apply_changeset_path)
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert len(report.failed) == 1
    assert report.failed[0]["http_status"] == 409
    assert not report.aborted
    assert len(report.applied) == len(cs["proposed_splits"]) - 1


def test_apply_continues_after_400_locked(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    from ynab_client import YNABValidationError

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise YNABValidationError(400, name="transaction_locked", detail="reconciled")
        return {"id": "ok"}

    mock_client.update_transaction.side_effect = side_effect
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert any(f["http_status"] == 400 for f in report.failed)
    assert not report.aborted


def test_apply_aborts_on_429(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    from ynab_client import YNABRateLimitError

    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise YNABRateLimitError(429, detail="too many")
        return {"id": "ok"}

    mock_client.update_transaction.side_effect = side_effect
    cs = _read_changeset(apply_changeset_path)
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert report.aborted
    assert "rate" in (report.abort_reason or "").lower()
    assert mock_client.update_transaction.call_count == 2
    assert len(report.applied) == 1
    assert len(cs["proposed_splits"]) > 2


def test_apply_aborts_when_rate_limit_floor_breached(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset

    remaining_seq = iter([100, 4])
    mock_client.rate_limit_remaining.side_effect = lambda: next(remaining_seq, 4)

    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", rate_limit_floor=5, report_dir=tmp_path)
    assert report.aborted
    assert "rate limit floor" in (report.abort_reason or "").lower()
    assert mock_client.update_transaction.call_count == 2


def test_apply_throttle_sleeps_between_calls(apply_changeset_path, mock_client, monkeypatch, tmp_path):
    from amazon_confirm import apply_changeset
    import amazon_confirm

    sleeps = []
    monkeypatch.setattr(amazon_confirm.time, "sleep", lambda s: sleeps.append(s))
    cs = _read_changeset(apply_changeset_path)
    apply_changeset(apply_changeset_path, mock_client, "budget-1", throttle_seconds=0.75, report_dir=tmp_path)
    assert all(s == 0.75 for s in sleeps)
    assert len(sleeps) == len(cs["proposed_splits"])


def test_apply_writes_summary_report(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    reports = list(tmp_path.glob("amazon-confirmed-*.json"))
    assert len(reports) == 1
    body = json.loads(reports[0].read_text())
    assert "applied" in body
    assert "total" in body


def test_apply_handles_empty_proposed_splits(tmp_path, mock_client):
    from amazon_confirm import apply_changeset
    cs = {
        "version": 1,
        "generated_at": "2026-05-14T00:00:00",
        "summary": {},
        "proposed_splits": [],
    }
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(cs))
    report = apply_changeset(path, mock_client, "budget-1", report_dir=tmp_path)
    assert report.total == 0
    assert report.applied == []
    assert report.failed == []
    mock_client.update_transaction.assert_not_called()


def test_apply_uses_report_dir_not_real_cache(apply_changeset_path, mock_client, tmp_path, monkeypatch):
    """Regression test for #171: when report_dir is provided, the real
    data/cache/ directory must not receive any files (even if cwd is at the
    repo root)."""
    from amazon_confirm import apply_changeset
    repo_root = Path("/home/jflournoy/code/finances-helper")
    real_cache_before = set((repo_root / "data" / "cache").glob("amazon-confirmed-*.json")) if (repo_root / "data" / "cache").exists() else set()
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    real_cache_after = set((repo_root / "data" / "cache").glob("amazon-confirmed-*.json")) if (repo_root / "data" / "cache").exists() else set()
    assert real_cache_after == real_cache_before, "apply_changeset must not write to real data/cache when report_dir is provided"
    assert list(tmp_path.glob("amazon-confirmed-*.json"))
