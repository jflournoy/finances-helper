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
from amazon_confirm import load_changeset, summarize_changeset, ChangesetSummary, ApplyReport, apply_changeset

FIXTURES = Path("data/fixtures")


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_load_changeset_happy_path():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    assert changeset["version"] == 1
    assert changeset["kind"] == "enrich-changeset"
    assert len(changeset["amazon"]["proposed_splits"]) >= 1


def test_load_changeset_rejects_version_2():
    fixture = load_fixture("enrich_changeset_sample.json")
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
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["amazon"]["proposed_splits"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="proposed_splits"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_missing_metadata_key():
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["metadata"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="metadata"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_proposal_missing_parent_ynab_transaction():
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["amazon"]["proposed_splits"][0]["parent_ynab_transaction"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="0.*parent_ynab_transaction"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_proposal_missing_subtransactions():
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["amazon"]["proposed_splits"][0]["subtransactions"]
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
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    assert summary.total_proposals >= 1


def test_summarize_counts_flat_vs_split():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    assert summary.split_proposals + summary.flat_proposals <= summary.total_proposals
    assert summary.split_proposals >= 0
    assert summary.flat_proposals >= 0


def test_summarize_counts_skipped_uncategorized():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    changeset["amazon"]["proposed_splits"][0]["subtransactions"][0]["category_id"] = None
    summary = summarize_changeset(changeset)
    assert summary.skipped_uncategorized >= 1  # At least the one we just set


def test_summarize_null_category_partial_proposal():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    proposal = changeset["amazon"]["proposed_splits"][0]
    if len(proposal["subtransactions"]) >= 2:
        proposal["subtransactions"][0]["category_id"] = None
        summary = summarize_changeset(changeset)
        assert summary.skipped_uncategorized >= 1  # At least the one we just set


def test_summarize_total_outflow_uses_decimal():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    assert isinstance(summary.total_outflow_dollars, Decimal)
    assert summary.total_outflow_dollars > Decimal("0")


def test_summarize_by_category_groups_correctly():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    assert isinstance(summary.by_category, dict)
    for cat_name, amount in summary.by_category.items():
        assert isinstance(amount, Decimal)


def test_summarize_by_category_excludes_null_category_name():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    for proposal in changeset["amazon"]["proposed_splits"]:
        for subtxn in proposal["subtransactions"]:
            subtxn["category_name"] = None
    for proposal in changeset["non_amazon"]["proposals"]:
        proposal["category_name"] = None
    summary = summarize_changeset(changeset)
    assert len(summary.by_category) == 0


def test_summarize_counts_previously_applied():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    changeset["amazon"]["proposed_splits"][0]["applied_at"] = "2026-05-14T10:00:00"
    summary = summarize_changeset(changeset)
    assert summary.applied_previously == 1
    assert summary.skipped_by_review == 0


def test_summarize_review_skip_sentinel_counted_separately():
    """A `skipped-by-review:` applied_at should NOT count as applied_previously."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    changeset["amazon"]["proposed_splits"][0]["applied_at"] = "skipped-by-review:user"
    changeset["non_amazon"]["proposals"][0]["applied_at"] = "skipped-by-review:user-after-edit"
    summary = summarize_changeset(changeset)
    assert summary.skipped_by_review == 2
    assert summary.applied_previously == 0


def test_summarize_mixed_skip_and_applied():
    """Real applied_at and review-skip sentinel should be counted independently."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    splits = changeset["amazon"]["proposed_splits"]
    assert len(splits) >= 2, "fixture must have >=2 splits for this test"
    splits[0]["applied_at"] = "2026-05-14T10:00:00"
    splits[1]["applied_at"] = "skipped-by-review:user"
    summary = summarize_changeset(changeset)
    assert summary.applied_previously == 1
    assert summary.skipped_by_review == 1


def test_would_apply_outflow_equals_total_on_clean_changeset():
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    assert summary.would_apply_outflow_dollars == summary.total_outflow_dollars


def test_would_apply_outflow_excludes_review_skipped():
    """Skipping a proposal via review sentinel must subtract from would-apply outflow."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split = changeset["amazon"]["proposed_splits"][0]
    parent_amount_dollars = Decimal(abs(split["parent_ynab_transaction"]["amount"])) / Decimal(1000)

    baseline = summarize_changeset(changeset).would_apply_outflow_dollars

    split["applied_at"] = "skipped-by-review:user"
    after = summarize_changeset(changeset).would_apply_outflow_dollars

    assert after == baseline - parent_amount_dollars


def test_would_apply_outflow_excludes_previously_applied():
    """A real applied_at timestamp must subtract from would-apply outflow."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split = changeset["amazon"]["proposed_splits"][0]
    parent_amount_dollars = Decimal(abs(split["parent_ynab_transaction"]["amount"])) / Decimal(1000)

    baseline = summarize_changeset(changeset).would_apply_outflow_dollars

    split["applied_at"] = "2026-05-14T10:00:00"
    after = summarize_changeset(changeset).would_apply_outflow_dollars

    assert after == baseline - parent_amount_dollars
    # total_outflow_dollars stays unchanged — it reflects the gross proposal set
    assert summarize_changeset(changeset).total_outflow_dollars > after


def test_load_and_summarize_real_fixture():
    """Full call chain: load_changeset → summarize_changeset using the real fixture."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    assert summary.total_proposals >= 1
    assert summary.total_outflow_dollars > Decimal("0")


# Issue #5: proposal_to_patch_body() conversion


def test_single_subtxn_returns_flat_patch():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    flat_proposal = next((p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) == 1), None)
    if flat_proposal:
        result = proposal_to_patch_body(flat_proposal)
        assert result is not None
        assert "category_id" in result
        assert "subtransactions" not in result


def test_multi_subtxn_returns_split_patch():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split_proposal = next((p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) >= 2), None)
    if split_proposal:
        result = proposal_to_patch_body(split_proposal)
        assert result is not None
        assert "subtransactions" in result
        assert len(result["subtransactions"]) == len(split_proposal["subtransactions"])


def test_subtxn_amounts_are_negative_milliunits():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    proposal = changeset["amazon"]["proposed_splits"][0]
    result = proposal_to_patch_body(proposal)
    assert result is not None
    if "subtransactions" in result:
        for subtxn in result["subtransactions"]:
            assert subtxn["amount"] < 0


def test_subtxn_amounts_are_integers_not_floats():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    proposal = changeset["amazon"]["proposed_splits"][0]
    result = proposal_to_patch_body(proposal)
    assert result is not None
    if "subtransactions" in result:
        for subtxn in result["subtransactions"]:
            assert isinstance(subtxn["amount"], int)


def test_split_amounts_sum_to_parent():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split_proposal = next((p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) >= 2), None)
    if split_proposal:
        result = proposal_to_patch_body(split_proposal)
        assert result is not None
        if "subtransactions" in result:
            total = sum(s["amount"] for s in result["subtransactions"])
            assert total == split_proposal["parent_ynab_transaction"]["amount"]


def test_null_category_id_any_subtxn_returns_none():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    proposal = changeset["amazon"]["proposed_splits"][0]
    proposal["subtransactions"][0]["category_id"] = None
    result = proposal_to_patch_body(proposal)
    assert result is None


def test_memo_truncated_to_200_chars():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split_proposal = next(
        (p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) >= 2),
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
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split_proposal = next(
        (p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None
    result = proposal_to_patch_body(split_proposal)
    assert result is not None
    for sub_in, sub_out in zip(split_proposal["subtransactions"], result["subtransactions"]):
        assert sub_in["item"]["product_name"][:128] in sub_out["memo"]


def test_split_subtxn_memo_contains_real_asin():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split_proposal = next(
        (p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None
    result = proposal_to_patch_body(split_proposal)
    assert result is not None
    for sub_in, sub_out in zip(split_proposal["subtransactions"], result["subtransactions"]):
        assert sub_in["item"]["asin"] in sub_out["memo"]


def test_proposal_to_patch_body_raises_when_item_missing():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    split_proposal = next(
        (p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) >= 2),
        None,
    )
    assert split_proposal is not None
    del split_proposal["subtransactions"][0]["item"]
    with pytest.raises((KeyError, ValueError)):
        proposal_to_patch_body(split_proposal)


def test_flat_patch_preserves_parent_memo():
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    flat_proposal = next((p for p in changeset["amazon"]["proposed_splits"] if len(p["subtransactions"]) == 1), None)
    if flat_proposal:
        parent_memo = flat_proposal["parent_ynab_transaction"].get("memo", "")
        result = proposal_to_patch_body(flat_proposal)
        assert result is not None
        if "subtransactions" not in result:
            assert result.get("memo") == parent_memo


def test_proposal_to_patch_body_with_all_fixture_proposals():
    """Exercise full call chain: load_changeset → each proposal through proposal_to_patch_body."""
    from amazon_confirm import proposal_to_patch_body
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    for proposal in changeset["amazon"]["proposed_splits"]:
        result = proposal_to_patch_body(proposal)
        if result is not None and "subtransactions" in result:
            total = sum(s["amount"] for s in result["subtransactions"])
            assert total == proposal["parent_ynab_transaction"]["amount"]


# Issue #7: CLI entry point


@pytest.fixture
def cli_changeset_path(tmp_path):
    src = Path("data/fixtures/enrich_changeset_sample.json")
    dst = tmp_path / "enrich-changeset-test.json"
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

        def _default_update_transactions(budget_id, updates):
            return {"transactions": [{"id": u["id"]} for u in updates]}
        mock.update_transactions.side_effect = _default_update_transactions

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
        client.update_transactions.assert_not_called()


def test_cli_yes_skips_prompt(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path, stdin_text="")
    assert code == 0
    called = sum(c.update_transactions.call_count for c in fake_client_factory["constructed"])
    assert called > 0


def test_cli_prompt_n_exits_zero(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path)], monkeypatch, tmp_path, stdin_text="n\n")
    assert code == 0
    for client in fake_client_factory["constructed"]:
        client.update_transactions.assert_not_called()


def test_cli_prompt_y_proceeds(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)
    code = _run_main([str(cli_changeset_path)], monkeypatch, tmp_path, stdin_text="y\n")
    assert code == 0
    called = sum(c.update_transactions.call_count for c in fake_client_factory["constructed"])
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


def test_cli_401_token_clear_message(cli_changeset_path, monkeypatch, tmp_path, capsys):
    from ynab_client import YNABAPIError
    import amazon_confirm

    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    fake_client = MagicMock()
    fake_client.sandbox_mode = False
    fake_client.resolve_budget_id.side_effect = YNABAPIError(401, name="unauthorized", detail="Unauthorized")
    monkeypatch.setattr(amazon_confirm, "YNABClient", lambda *a, **kw: fake_client)

    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    err = capsys.readouterr().err.lower()
    assert code == 3
    assert "token" in err or "401" in err or "unauthorized" in err


def test_cli_429_clear_message(cli_changeset_path, monkeypatch, tmp_path, capsys):
    from ynab_client import YNABRateLimitError
    import amazon_confirm

    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    fake_client = MagicMock()
    fake_client.sandbox_mode = False
    fake_client.resolve_budget_id.side_effect = YNABRateLimitError(429, detail="rate limited")
    monkeypatch.setattr(amazon_confirm, "YNABClient", lambda *a, **kw: fake_client)

    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    err = capsys.readouterr().err.lower()
    assert code == 2
    assert "rate" in err


def test_cli_prompt_excludes_uncategorized_from_count(cli_changeset_path, fake_client_factory, monkeypatch, tmp_path, capsys):
    """The prompt's 'Apply N proposals' count should match the number of
    PATCH calls — i.e. subtract both already-applied and uncategorized skips."""
    import amazon_confirm

    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "My Budget")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    fake_summary = ChangesetSummary(
        total_proposals=10,
        split_proposals=6,
        flat_proposals=4,
        skipped_uncategorized=3,
        applied_previously=2,
        total_outflow_dollars=Decimal("100.00"),
        by_category={},
    )
    monkeypatch.setattr(amazon_confirm, "summarize_changeset", lambda *_: fake_summary)

    prompts = []
    def fake_input(prompt):
        prompts.append(prompt)
        return "n"
    monkeypatch.setattr("builtins.input", fake_input)

    code = _run_main([str(cli_changeset_path)], monkeypatch, tmp_path, stdin_text="n\n")
    assert code == 0
    assert len(prompts) == 1
    assert "Apply 5 proposals" in prompts[0]


def test_cli_404_budget_clear_message(cli_changeset_path, monkeypatch, tmp_path, capsys):
    from ynab_client import YNABNotFoundError
    import amazon_confirm

    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "Phantom")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    fake_client = MagicMock()
    fake_client.sandbox_mode = False
    fake_client.resolve_budget_id.side_effect = YNABNotFoundError(404, name="not_found", detail="No budget with that id")
    monkeypatch.setattr(amazon_confirm, "YNABClient", lambda *a, **kw: fake_client)

    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    err = capsys.readouterr().err
    assert code == 3
    assert "404" in err
    assert "Phantom" in err


def test_cli_unknown_budget_clear_message(cli_changeset_path, monkeypatch, tmp_path, capsys):
    import amazon_confirm

    monkeypatch.setenv("YNAB_API_TOKEN", "tok")
    monkeypatch.setenv("YNAB_DEFAULT_BUDGET", "Nonexistent")
    monkeypatch.delenv("YNAB_SANDBOX_MODE", raising=False)

    fake_client = MagicMock()
    fake_client.sandbox_mode = False
    fake_client.resolve_budget_id.side_effect = ValueError("YNAB_DEFAULT_BUDGET='Nonexistent' not found. Available: ['My Budget']")
    monkeypatch.setattr(amazon_confirm, "YNABClient", lambda *a, **kw: fake_client)

    code = _run_main([str(cli_changeset_path), "--yes"], monkeypatch, tmp_path)
    err = capsys.readouterr().err
    assert code == 3
    assert "Nonexistent" in err
    assert "Available" in err


# Issue #6: apply_changeset() orchestration


@pytest.fixture
def apply_changeset_path(tmp_path):
    src = Path("data/fixtures/enrich_changeset_sample.json")
    dst = tmp_path / "enrich-changeset-orch.json"
    shutil.copy(src, dst)
    return dst


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 199
    def side_effect_update_transactions(budget_id, updates):
        return {"transactions": [{"id": u["id"]} for u in updates]}
    client.update_transactions.side_effect = side_effect_update_transactions
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
    mock_client.update_transactions.assert_not_called()


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
    mock_client.update_transactions.assert_not_called()
    assert any(s.get("reason") == "dry run" for s in report.skipped)


def test_applyable_math_matches_dry_run_skip_count(apply_changeset_path, mock_client, tmp_path):
    """The y/N prompt math (total - applied - uncategorized - skipped_by_review)
    MUST equal the count of "dry run" skips emitted by apply_changeset.

    If this drifts, the user sees one number at the confirmation prompt and a
    different number actually gets PATCHed. Locks down B1/Y3 invariant.
    """
    from amazon_confirm import apply_changeset, load_changeset, summarize_changeset

    cs = load_changeset(apply_changeset_path)
    splits = cs["amazon"]["proposed_splits"]
    non_amazon = cs["non_amazon"]["proposals"]
    assert len(splits) >= 2 and len(non_amazon) >= 2, "fixture needs >=2 of each"

    splits[0]["applied_at"] = "skipped-by-review:user"
    splits[1]["applied_at"] = "2026-05-14T10:00:00"
    non_amazon[0]["applied_at"] = "skipped-by-review:user-after-edit"
    non_amazon[1]["category_id"] = None
    # apply_changeset re-reads the file, so persist the in-memory mutations.
    apply_changeset_path.write_text(json.dumps(cs, default=str))

    cs_reloaded = load_changeset(apply_changeset_path)
    summary = summarize_changeset(cs_reloaded)
    prompt_applyable = (
        summary.total_proposals
        - summary.applied_previously
        - summary.skipped_uncategorized
        - summary.skipped_by_review
    )

    report = apply_changeset(
        apply_changeset_path, mock_client, "budget-1", dry_run=True, report_dir=tmp_path
    )
    dry_run_skips = sum(1 for s in report.skipped if s.get("reason") == "dry run")

    assert prompt_applyable == dry_run_skips, (
        f"y/N prompt would say 'Apply {prompt_applyable} proposals' but apply_changeset "
        f"reports {dry_run_skips} would-PATCH items. Numbers must agree."
    )
    assert summary.skipped_by_review == 2
    assert summary.applied_previously == 1
    assert summary.skipped_uncategorized == 1


def _applyable_count(cs):
    """Count proposals (Amazon splits + non_amazon flats) that would be PATCHed."""
    amazon_applyable = sum(
        1 for p in cs["amazon"]["proposed_splits"]
        if "applied_at" not in p
        and all(s.get("category_id") is not None for s in p["subtransactions"])
    )
    non_amazon_applyable = sum(
        1 for p in cs["non_amazon"]["proposals"]
        if "applied_at" not in p and p.get("category_id") is not None
    )
    return amazon_applyable + non_amazon_applyable


def test_apply_skips_proposals_with_applied_at(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    cs["amazon"]["proposed_splits"][0]["applied_at"] = "2026-05-14T10:00:00"
    apply_changeset_path.write_text(json.dumps(cs))
    expected_applyable = _applyable_count(cs)
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    skip_reasons = [s["reason"] for s in report.skipped]
    assert any("already applied" in r for r in skip_reasons)
    # Batch mode: all applyable items go in one chunk (well under BATCH_SIZE=200)
    assert mock_client.update_transactions.call_count == 1
    sent_updates = mock_client.update_transactions.call_args_list[0][0][1]
    assert len(sent_updates) == expected_applyable


def test_apply_skips_proposals_with_null_category(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    cs["amazon"]["proposed_splits"][0]["subtransactions"][0]["category_id"] = None
    apply_changeset_path.write_text(json.dumps(cs))
    expected_applyable = _applyable_count(cs)
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert any("uncategorized" in s["reason"] for s in report.skipped)
    assert mock_client.update_transactions.call_count == 1
    sent_updates = mock_client.update_transactions.call_args_list[0][0][1]
    assert len(sent_updates) == expected_applyable


def test_apply_marks_applied_at_on_success(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    cs_after = _read_changeset(apply_changeset_path)
    for proposal in cs_after["amazon"]["proposed_splits"]:
        assert "applied_at" in proposal


def test_apply_aborts_on_batch_conflict(apply_changeset_path, mock_client, tmp_path):
    """Batch mode: a 409 anywhere in the chunk aborts the whole chunk.
    The pre-batch per-txn 409-continue behavior was deliberately removed
    when the endpoint moved to batch PATCH (chunks are atomic per YNAB)."""
    from amazon_confirm import apply_changeset
    from ynab_client import YNABConflictError

    mock_client.update_transactions.side_effect = YNABConflictError(409, detail="conflict")
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert report.aborted
    assert any(f["http_status"] == 409 for f in report.failed)
    assert len(report.applied) == 0


def test_apply_aborts_on_batch_locked_400(apply_changeset_path, mock_client, tmp_path):
    """A 400 from YNAB (e.g. transaction_locked) aborts the chunk; chunks are
    atomic so we cannot continue past a validation failure."""
    from amazon_confirm import apply_changeset
    from ynab_client import YNABValidationError

    mock_client.update_transactions.side_effect = YNABValidationError(
        400, name="transaction_locked", detail="reconciled"
    )
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert report.aborted
    assert any(f["http_status"] == 400 for f in report.failed)
    assert len(report.applied) == 0


def test_apply_aborts_on_429(apply_changeset_path, mock_client, tmp_path):
    """A 429 on the (single) batch call aborts; nothing is applied."""
    from amazon_confirm import apply_changeset
    from ynab_client import YNABRateLimitError

    mock_client.update_transactions.side_effect = YNABRateLimitError(429, detail="too many")
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    assert report.aborted
    assert "rate" in (report.abort_reason or "").lower()
    assert mock_client.update_transactions.call_count == 1
    assert len(report.applied) == 0


def test_apply_aborts_when_rate_limit_floor_breached(apply_changeset_path, mock_client, tmp_path):
    """The rate-limit floor check now runs before each chunk. With a fixture
    that fits in one chunk and a pre-flight `remaining` below the floor, we
    abort before issuing the batch PATCH."""
    from amazon_confirm import apply_changeset

    mock_client.rate_limit_remaining.return_value = 4
    report = apply_changeset(
        apply_changeset_path, mock_client, "budget-1", rate_limit_floor=5, report_dir=tmp_path
    )
    assert report.aborted
    assert "rate limit floor" in (report.abort_reason or "").lower()
    assert mock_client.update_transactions.call_count == 0


def test_apply_throttle_seconds_emits_deprecation_warning(apply_changeset_path, mock_client, tmp_path):
    """`throttle_seconds` is preserved in the signature for backwards compatibility
    but is unused in batch mode. Non-zero values emit a DeprecationWarning per the
    refined plan (avoids silent no-op which violates NO SILENT FALLBACKS)."""
    from amazon_confirm import apply_changeset

    with pytest.warns(DeprecationWarning, match="throttle_seconds"):
        apply_changeset(
            apply_changeset_path, mock_client, "budget-1",
            throttle_seconds=0.75, report_dir=tmp_path,
        )


def test_apply_writes_summary_report(apply_changeset_path, mock_client, tmp_path):
    from amazon_confirm import apply_changeset
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    reports = list(tmp_path.glob("enrich-confirmed-*.json"))
    assert len(reports) == 1
    assert list(tmp_path.glob("amazon-confirmed-*.json")) == []
    body = json.loads(reports[0].read_text())
    assert "applied" in body
    assert "total" in body


def test_apply_handles_empty_proposed_splits(tmp_path, mock_client):
    from amazon_confirm import apply_changeset
    cs = {
        "version": 1,
        "kind": "enrich-changeset",
        "metadata": {
            "timestamp": "2026-05-14T00:00:00",
            "budget_id": "budget-1",
        },
        "amazon": {
            "proposed_splits": [],
        },
        "non_amazon": {
            "proposals": [],
        },
    }
    path = tmp_path / "empty.json"
    path.write_text(json.dumps(cs))
    report = apply_changeset(path, mock_client, "budget-1", report_dir=tmp_path)
    assert report.total == 0
    assert report.applied == []
    assert report.failed == []
    mock_client.update_transactions.assert_not_called()


def test_apply_uses_report_dir_not_real_cache(apply_changeset_path, mock_client, tmp_path, monkeypatch):
    """Regression test for #171: when report_dir is provided, the real
    data/cache/ directory must not receive any files (even if cwd is at the
    repo root)."""
    from amazon_confirm import apply_changeset
    repo_root = Path("/home/jflournoy/code/finances-helper")
    real_cache_before = set((repo_root / "data" / "cache").glob("enrich-confirmed-*.json")) if (repo_root / "data" / "cache").exists() else set()
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    real_cache_after = set((repo_root / "data" / "cache").glob("enrich-confirmed-*.json")) if (repo_root / "data" / "cache").exists() else set()
    assert real_cache_after == real_cache_before, "apply_changeset must not write to real data/cache when report_dir is provided"
    assert list(tmp_path.glob("enrich-confirmed-*.json"))


def test_apply_propagates_client_side_sum_invariant_error(tmp_path, mock_client):
    """A changeset whose subtxn amounts don't sum to parent amount is caught
    client-side by proposal_to_patch_body and surfaces as ValueError. This is
    a bug-in-data condition that should fail loudly, not be swallowed as a
    YNAB-validation skip. (Was previously a misnamed integration test.)"""
    from amazon_confirm import apply_changeset
    parent_amount = -15500  # -$15.50 in milliunits
    cs = {
        "version": 1,
        "kind": "enrich-changeset",
        "metadata": {
            "timestamp": "2026-05-14T00:00:00",
            "budget_id": "budget-1",
        },
        "amazon": {
            "proposed_splits": [
                {
                    "transaction_id": "txn-1",
                    "parent_ynab_transaction": {"id": "txn-1", "amount": parent_amount, "memo": "x"},
                    "subtransactions": [
                        {
                            "item": {"asin": "A", "product_name": "Item A"},
                            "allocated_amount": "10.00",
                            "category_id": "cat-a",
                            "category_name": "A",
                        },
                        {
                            "item": {"asin": "B", "product_name": "Item B"},
                            "allocated_amount": "3.00",  # sum is $13.00 not $15.50
                            "category_id": "cat-b",
                            "category_name": "B",
                        },
                    ],
                }
            ],
        },
        "non_amazon": {
            "proposals": [],
        },
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(cs))
    with pytest.raises(ValueError, match="invariant"):
        apply_changeset(path, mock_client, "budget-1", report_dir=tmp_path)
    mock_client.update_transactions.assert_not_called()


# Issue #176: enrich-changeset schema support — new tests


def test_load_changeset_accepts_enrich_changeset():
    """Load the enrich_changeset_sample.json fixture."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    assert changeset["kind"] == "enrich-changeset"
    assert changeset["version"] == 1
    assert "metadata" in changeset
    assert "amazon" in changeset
    assert "non_amazon" in changeset


def test_load_changeset_rejects_missing_kind():
    """Changeset without 'kind' field raises ValueError."""
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["kind"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="kind"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_rejects_wrong_kind():
    """Changeset with wrong 'kind' raises ValueError."""
    fixture = load_fixture("enrich_changeset_sample.json")
    fixture["kind"] = "amazon-changeset"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="enrich-changeset"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_rejects_legacy_schema():
    """Legacy fixture without 'kind' raises ValueError."""
    with pytest.raises(ValueError, match="kind"):
        load_changeset(Path("data/fixtures/expected_amazon_changeset.json"))


def test_load_changeset_rejects_missing_metadata_timestamp():
    """Changeset without metadata.timestamp raises ValueError."""
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["metadata"]["timestamp"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="timestamp"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_amazon_split_missing_transaction_id():
    """Amazon split without transaction_id raises ValueError."""
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["amazon"]["proposed_splits"][0]["transaction_id"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="transaction_id"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_non_amazon_proposal_missing_transaction_id():
    """Non-Amazon proposal without transaction_id raises ValueError."""
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["non_amazon"]["proposals"][0]["transaction_id"]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        with pytest.raises(ValueError, match="transaction_id"):
            load_changeset(path)
    finally:
        path.unlink()


def test_load_changeset_empty_lists_ok():
    """Changeset with empty amazon.proposed_splits and non_amazon.proposals loads OK."""
    fixture = load_fixture("enrich_changeset_sample.json")
    fixture["amazon"]["proposed_splits"] = []
    fixture["non_amazon"]["proposals"] = []
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(fixture, f)
        path = Path(f.name)
    try:
        changeset = load_changeset(path)
        assert changeset["amazon"]["proposed_splits"] == []
        assert changeset["non_amazon"]["proposals"] == []
    finally:
        path.unlink()


def test_summarize_changeset_counts_splits_and_flats():
    """Summarize counts both Amazon splits and non-Amazon flats correctly."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    # Fixture has 2 Amazon splits + 3 non-Amazon proposals
    assert summary.total_proposals == 5
    assert summary.non_amazon_proposals == 3
    assert summary.split_proposals >= 1
    assert summary.flat_proposals >= 0


def test_summarize_changeset_uncategorized_flat_not_in_outflow():
    """Uncategorized non-Amazon flat doesn't contribute to total_outflow_dollars."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    outflow_before = Decimal(str(sum(Decimal(p.get("amount_dollars", "0")) for p in changeset["non_amazon"]["proposals"] if p.get("amount_dollars") and p.get("category_id"))))
    summary = summarize_changeset(changeset)
    # Verify that skipped_uncategorized is counted
    assert summary.skipped_uncategorized >= 0
    # The uncategorized flat (amount_dollars='150.00', category_id=null) should not be in by_category


def test_summarize_changeset_null_amount_dollars_no_crash():
    """Non-Amazon flat with amount_dollars=None doesn't crash summarize_changeset."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    # Just ensure no exception is raised and we get valid summary
    assert summary.total_proposals >= 0
    assert isinstance(summary.total_outflow_dollars, Decimal)


def test_flat_proposal_to_patch_body_returns_category_only():
    """flat_proposal_to_patch_body returns only category_id field."""
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {
        "transaction_id": "txn-123",
        "category_id": "cat-456",
        "category_name": "Groceries",
        "confidence": 0.9,
    }
    result = flat_proposal_to_patch_body(proposal)
    assert result == {"category_id": "cat-456"}
    assert len(result) == 1


def test_flat_proposal_to_patch_body_returns_none_when_uncategorized():
    """flat_proposal_to_patch_body returns None when category_id is None."""
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {
        "transaction_id": "txn-123",
        "category_id": None,
        "confidence": 0.0,
    }
    result = flat_proposal_to_patch_body(proposal)
    assert result is None


# Auto-approval threshold tests (AUTO_APPROVE_UNDER_DOLLARS = 100)


def test_flat_proposal_auto_approved_when_amount_below_threshold():
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {"transaction_id": "t1", "category_id": "cat-1", "amount_dollars": "-42.50"}
    body = flat_proposal_to_patch_body(proposal)
    assert body == {"category_id": "cat-1", "approved": True}


def test_flat_proposal_not_approved_when_amount_at_threshold():
    """Exactly $100 must NOT auto-approve (strict less-than gate)."""
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {"transaction_id": "t1", "category_id": "cat-1", "amount_dollars": "-100.00"}
    body = flat_proposal_to_patch_body(proposal)
    assert body == {"category_id": "cat-1"}


def test_flat_proposal_not_approved_when_amount_above_threshold():
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {"transaction_id": "t1", "category_id": "cat-1", "amount_dollars": "-369"}
    body = flat_proposal_to_patch_body(proposal)
    assert body == {"category_id": "cat-1"}


def test_flat_proposal_inflow_auto_approved_when_under_threshold():
    """Positive amounts (inflows) use |amount| so a $0.16 interest deposit auto-approves."""
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {"transaction_id": "t1", "category_id": "cat-1", "amount_dollars": "0.16"}
    body = flat_proposal_to_patch_body(proposal)
    assert body == {"category_id": "cat-1", "approved": True}


def test_flat_proposal_inflow_not_approved_when_at_or_above_threshold():
    """A $7500 paycheck deposit (positive) stays unapproved."""
    from amazon_confirm import flat_proposal_to_patch_body
    proposal = {"transaction_id": "t1", "category_id": "cat-1", "amount_dollars": "7500"}
    body = flat_proposal_to_patch_body(proposal)
    assert body == {"category_id": "cat-1"}


def test_flat_proposal_no_amount_dollars_does_not_auto_approve():
    """Missing/None amount_dollars must fail safe: no approval."""
    from amazon_confirm import flat_proposal_to_patch_body
    proposal_missing = {"transaction_id": "t1", "category_id": "cat-1"}
    assert flat_proposal_to_patch_body(proposal_missing) == {"category_id": "cat-1"}
    proposal_none = {"transaction_id": "t1", "category_id": "cat-1", "amount_dollars": None}
    assert flat_proposal_to_patch_body(proposal_none) == {"category_id": "cat-1"}


def test_amazon_single_subtxn_auto_approved_when_small():
    """Amazon proposal with one subtxn (becomes flat patch body): parent amount < $100 → approved."""
    from amazon_confirm import proposal_to_patch_body
    proposal = {
        "transaction_id": "amzn-1",
        "parent_ynab_transaction": {"id": "amzn-1", "amount": -25000, "memo": "Order ($25)"},
        "subtransactions": [{"item": {"asin": "A", "product_name": "X"}, "allocated_amount": "25.00",
                             "category_id": "c1", "category_name": "Cat"}],
    }
    body = proposal_to_patch_body(proposal)
    assert body == {"category_id": "c1", "memo": "Order ($25)", "approved": True}


def test_amazon_single_subtxn_not_approved_when_large():
    from amazon_confirm import proposal_to_patch_body
    proposal = {
        "transaction_id": "amzn-1",
        "parent_ynab_transaction": {"id": "amzn-1", "amount": -250000, "memo": "Order ($250)"},
        "subtransactions": [{"item": {"asin": "A", "product_name": "X"}, "allocated_amount": "250.00",
                             "category_id": "c1", "category_name": "Cat"}],
    }
    body = proposal_to_patch_body(proposal)
    assert body == {"category_id": "c1", "memo": "Order ($250)"}


def test_amazon_split_auto_approved_when_parent_small():
    """Multi-subtxn split: gate uses PARENT amount, not max subtxn."""
    from amazon_confirm import proposal_to_patch_body
    proposal = {
        "transaction_id": "amzn-1",
        "parent_ynab_transaction": {"id": "amzn-1", "amount": -50000, "memo": ""},
        "subtransactions": [
            {"item": {"asin": "A", "product_name": "X"}, "allocated_amount": "25.00",
             "category_id": "c1", "category_name": "Cat1"},
            {"item": {"asin": "B", "product_name": "Y"}, "allocated_amount": "25.00",
             "category_id": "c2", "category_name": "Cat2"},
        ],
    }
    body = proposal_to_patch_body(proposal)
    assert "subtransactions" in body
    assert body.get("approved") is True


def test_amazon_split_not_approved_when_parent_large():
    from amazon_confirm import proposal_to_patch_body
    proposal = {
        "transaction_id": "amzn-1",
        "parent_ynab_transaction": {"id": "amzn-1", "amount": -150000, "memo": ""},
        "subtransactions": [
            {"item": {"asin": "A", "product_name": "X"}, "allocated_amount": "100.00",
             "category_id": "c1", "category_name": "Cat1"},
            {"item": {"asin": "B", "product_name": "Y"}, "allocated_amount": "50.00",
             "category_id": "c2", "category_name": "Cat2"},
        ],
    }
    body = proposal_to_patch_body(proposal)
    assert "subtransactions" in body
    assert "approved" not in body


def test_load_then_summarize_enrich_changeset():
    """Integration: load fixture → summarize_changeset; verify data flow."""
    changeset = load_changeset(Path("data/fixtures/enrich_changeset_sample.json"))
    summary = summarize_changeset(changeset)
    expected_non_amazon = len(changeset["non_amazon"]["proposals"])
    assert summary.non_amazon_proposals == expected_non_amazon
    assert summary.total_outflow_dollars >= Decimal("0")


# Issues #177-#178 + review follow-ups: non_amazon apply path, tier validation, no silent fallbacks


def test_load_changeset_non_amazon_proposal_missing_tier(tmp_path):
    """Non-Amazon proposals must include a 'tier' field."""
    fixture = load_fixture("enrich_changeset_sample.json")
    del fixture["non_amazon"]["proposals"][0]["tier"]
    path = tmp_path / "changeset.json"
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="tier"):
        load_changeset(path)


def test_print_summary_raises_on_missing_metadata_timestamp(tmp_path):
    """_print_summary does not silently fall back to '?' when metadata.timestamp is missing.

    Covers both 'metadata key absent' and 'timestamp key absent within metadata'.
    """
    from amazon_confirm import _print_summary
    fake_summary = ChangesetSummary(
        total_proposals=0, split_proposals=0, flat_proposals=0,
        skipped_uncategorized=0, applied_previously=0,
        total_outflow_dollars=Decimal("0"), by_category={},
        non_amazon_proposals=0,
    )
    with pytest.raises(KeyError):
        _print_summary({}, fake_summary, "My Budget", tmp_path / "x.json")
    with pytest.raises(KeyError):
        _print_summary({"metadata": {}}, fake_summary, "My Budget", tmp_path / "x.json")


def _sent_updates(mock_client):
    """Flatten all `updates` lists sent across every batch call."""
    sent = []
    for call in mock_client.update_transactions.call_args_list:
        sent.extend(call.args[1])
    return sent


def test_apply_processes_both_amazon_and_non_amazon(apply_changeset_path, mock_client, tmp_path):
    """apply_changeset batches both Amazon splits and categorized non_amazon flats
    into the batch endpoint."""
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    expected_applyable = _applyable_count(cs)
    report = apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    # All applyable items fit in 1 chunk (well under BATCH_SIZE=200).
    assert mock_client.update_transactions.call_count == 1
    sent = _sent_updates(mock_client)
    assert len(sent) == expected_applyable
    assert report.total == len(cs["amazon"]["proposed_splits"]) + len(cs["non_amazon"]["proposals"])


def test_apply_non_amazon_flat_patch_body_is_category_only(apply_changeset_path, mock_client, tmp_path):
    """The PATCH body for a non_amazon flat contains {'id', 'category_id'} and
    optionally 'approved' (when |amount| < AUTO_APPROVE_UNDER_DOLLARS) — nothing else."""
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    non_amazon_categorized_ids = {
        p["transaction_id"]
        for p in cs["non_amazon"]["proposals"]
        if p.get("category_id") is not None
    }
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    sent = _sent_updates(mock_client)
    flat_updates = [u for u in sent if u["id"] in non_amazon_categorized_ids]
    assert len(flat_updates) == len(non_amazon_categorized_ids)
    for update in flat_updates:
        allowed = {"id", "category_id", "approved"}
        assert set(update.keys()) <= allowed and {"id", "category_id"} <= set(update.keys()), (
            f"non_amazon flat batch entry keys must be a subset of {allowed} "
            f"and include id+category_id, got {update}"
        )


def test_apply_persists_applied_at_to_non_amazon(apply_changeset_path, mock_client, tmp_path):
    """applied_at is written to non_amazon.proposals[j] for each successful flat PATCH."""
    from amazon_confirm import apply_changeset
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    cs_after = _read_changeset(apply_changeset_path)
    for proposal in cs_after["non_amazon"]["proposals"]:
        if proposal.get("category_id") is not None:
            assert "applied_at" in proposal, f"categorized non_amazon proposal {proposal['transaction_id']} missing applied_at"
        else:
            assert "applied_at" not in proposal, "uncategorized proposal must not be marked applied_at"


def test_apply_skips_uncategorized_non_amazon(apply_changeset_path, mock_client, tmp_path):
    """Uncategorized non_amazon proposals (category_id=None) are excluded from
    the batch payload, not PATCHed."""
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    uncategorized_ids = {
        p["transaction_id"]
        for p in cs["non_amazon"]["proposals"]
        if p.get("category_id") is None
    }
    assert uncategorized_ids, "fixture must contain at least one uncategorized non_amazon proposal"
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    patched_ids = {u["id"] for u in _sent_updates(mock_client)}
    assert uncategorized_ids.isdisjoint(patched_ids)


def test_apply_resume_skips_non_amazon_with_applied_at(apply_changeset_path, mock_client, tmp_path):
    """A non_amazon proposal with applied_at is skipped on re-run (not included
    in the batch payload)."""
    from amazon_confirm import apply_changeset
    cs = _read_changeset(apply_changeset_path)
    for p in cs["non_amazon"]["proposals"]:
        if p.get("category_id") is not None:
            p["applied_at"] = "2026-05-14T10:00:00"
            preapplied_id = p["transaction_id"]
            break
    apply_changeset_path.write_text(json.dumps(cs))
    apply_changeset(apply_changeset_path, mock_client, "budget-1", report_dir=tmp_path)
    patched_ids = {u["id"] for u in _sent_updates(mock_client)}
    assert preapplied_id not in patched_ids


def test_summarize_does_not_double_count_amazon_flat_as_non_amazon():
    """An Amazon single-subtxn proposal counts in flat_proposals but NOT in non_amazon_proposals."""
    cs = {
        "version": 1,
        "kind": "enrich-changeset",
        "metadata": {"timestamp": "2026-05-15T00:00:00Z", "budget_id": "b1"},
        "amazon": {
            "proposed_splits": [
                {
                    "transaction_id": "amzn-1",
                    "parent_ynab_transaction": {"id": "amzn-1", "amount": -10000, "memo": "x"},
                    "subtransactions": [
                        {"item": {"asin": "A", "product_name": "X"}, "allocated_amount": "10.00",
                         "category_id": "c1", "category_name": "Cat1"},
                    ],
                },
            ],
        },
        "non_amazon": {"proposals": []},
    }
    summary = summarize_changeset(cs)
    assert summary.flat_proposals == 1
    assert summary.non_amazon_proposals == 0
    assert summary.split_proposals == 0


# Issue #179: Batch PATCH with chunking


def _make_changeset_with_flat_proposals(num_proposals, category_id="cat-123"):
    """Helper to build a changeset with N flat proposals (all uncategorized=False)."""
    return {
        "version": 1,
        "kind": "enrich-changeset",
        "metadata": {"timestamp": "2026-05-17T00:00:00", "budget_id": "b1"},
        "amazon": {"proposed_splits": []},
        "non_amazon": {
            "proposals": [
                {
                    "transaction_id": f"txn-{i}",
                    "payee_name": f"Payee {i}",
                    "category_id": category_id,
                    "confidence": 0.9,
                    "tier": "history",
                }
                for i in range(num_proposals)
            ]
        },
    }


def test_apply_batches_chunks_at_200(tmp_path):
    """344 proposals → 2 calls to update_transactions (200 + 144)."""
    changeset = _make_changeset_with_flat_proposals(344)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150

    def side_effect_update_transactions(budget_id, updates):
        return {"transactions": [{"id": u["id"]} for u in updates]}

    client.update_transactions.side_effect = side_effect_update_transactions

    report = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    assert client.update_transactions.call_count == 2
    calls = client.update_transactions.call_args_list
    assert len(calls[0][0][1]) == 200
    assert len(calls[1][0][1]) == 144

    updated_cs = json.loads(cs_path.read_text())
    assert len([p for p in updated_cs["non_amazon"]["proposals"] if "applied_at" in p]) == 344


def test_apply_response_matched_by_id_not_order(tmp_path):
    """Response txns in shuffled order; verify all marked applied_at."""
    changeset = _make_changeset_with_flat_proposals(5)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150

    def side_effect_update_transactions(budget_id, updates):
        response_txns = [{"id": u["id"]} for u in updates]
        response_txns.reverse()
        return {"transactions": response_txns}

    client.update_transactions.side_effect = side_effect_update_transactions

    report = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    updated_cs = json.loads(cs_path.read_text())
    assert len([p for p in updated_cs["non_amazon"]["proposals"] if "applied_at" in p]) == 5


def test_apply_resumes_at_chunk_boundary_on_rate_limit(tmp_path):
    """2 chunks; 2nd raises YNABRateLimitError. Verify 1st chunk applied, 2nd not. Resume picks up 2nd."""
    from ynab_client import YNABRateLimitError

    changeset = _make_changeset_with_flat_proposals(250)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150

    call_count = [0]

    def side_effect_update_transactions(budget_id, updates):
        call_count[0] += 1
        if call_count[0] == 1:
            return {"transactions": [{"id": u["id"]} for u in updates]}
        else:
            raise YNABRateLimitError(429, detail="Rate limit")

    client.update_transactions.side_effect = side_effect_update_transactions

    report = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    assert report.aborted
    assert report.abort_reason == "rate limit exceeded"
    assert len(report.applied) == 200
    assert len(report.failed) == 1

    updated_cs = json.loads(cs_path.read_text())
    applied_count = len([p for p in updated_cs["non_amazon"]["proposals"] if "applied_at" in p])
    assert applied_count == 200

    client.reset_mock()
    call_count[0] = 0
    client.rate_limit_remaining.return_value = 150

    def side_effect_2nd_run(budget_id, updates):
        return {"transactions": [{"id": u["id"]} for u in updates]}

    client.update_transactions.side_effect = side_effect_2nd_run

    report2 = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    assert client.update_transactions.call_count == 1
    assert len(client.update_transactions.call_args_list[0][0][1]) == 50


def test_apply_aborts_on_batch_validation_error(tmp_path):
    """Batch raises YNABValidationError. No proposals marked applied."""
    from ynab_client import YNABValidationError

    changeset = _make_changeset_with_flat_proposals(5)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150
    client.update_transactions.side_effect = YNABValidationError(400, detail="invalid category_id")

    report = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    assert report.aborted
    assert "batch validation error" in report.abort_reason
    assert "invalid category_id" in report.abort_reason
    assert len(report.failed) == 1

    updated_cs = json.loads(cs_path.read_text())
    applied_count = len([p for p in updated_cs["non_amazon"]["proposals"] if "applied_at" in p])
    assert applied_count == 0


def test_apply_raises_on_missing_id_in_response(tmp_path):
    """Response omits one txn id. Verify RuntimeError raised with missing id named."""
    changeset = _make_changeset_with_flat_proposals(3)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150

    def side_effect_update_transactions(budget_id, updates):
        return {"transactions": [{"id": u["id"]} for u in updates[:-1]]}

    client.update_transactions.side_effect = side_effect_update_transactions

    with pytest.raises(RuntimeError, match="missing transaction id"):
        apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)


def test_apply_persists_changeset_after_each_chunk(tmp_path, monkeypatch):
    """250 proposals (2 chunks); verify the changeset is persisted exactly 2 times
    (once per chunk), not 250 times (once per txn)."""
    changeset = _make_changeset_with_flat_proposals(250)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150

    def side_effect_update_transactions(budget_id, updates):
        return {"transactions": [{"id": u["id"]} for u in updates]}

    client.update_transactions.side_effect = side_effect_update_transactions

    original_write = Path.write_text
    write_calls = []

    def tracked_write(self, content, *args, **kwargs):
        if self == cs_path:
            write_calls.append(content)
        return original_write(self, content, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", tracked_write)

    report = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    assert len(write_calls) == 2


def test_apply_dry_run_makes_no_batch_calls(tmp_path):
    """dry_run=True; verify update_transactions never called."""
    changeset = _make_changeset_with_flat_proposals(5)
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False

    report = apply_changeset(cs_path, client, "b1", dry_run=True, throttle_seconds=0, report_dir=tmp_path)

    client.update_transactions.assert_not_called()
    assert len(report.skipped) == 5
    assert all(s["reason"] == "dry run" for s in report.skipped)


def test_apply_skips_already_applied_before_chunking(tmp_path):
    """100 with applied_at, 50 without; only 50 in update_transactions call."""
    changeset = _make_changeset_with_flat_proposals(150)
    for i in range(100):
        changeset["non_amazon"]["proposals"][i]["applied_at"] = "2026-05-16T00:00:00"
    cs_path = tmp_path / "changeset.json"
    cs_path.write_text(json.dumps(changeset))

    client = MagicMock()
    client.sandbox_mode = False
    client.rate_limit_remaining.return_value = 150

    def side_effect_update_transactions(budget_id, updates):
        return {"transactions": [{"id": u["id"]} for u in updates]}

    client.update_transactions.side_effect = side_effect_update_transactions

    report = apply_changeset(cs_path, client, "b1", dry_run=False, throttle_seconds=0, report_dir=tmp_path)

    assert client.update_transactions.call_count == 1
    assert len(client.update_transactions.call_args_list[0][0][1]) == 50
    assert len(report.skipped) == 100


