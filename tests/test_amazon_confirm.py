"""Tests for amazon_confirm.py — changeset loading and summarization."""
import json
import pytest
import tempfile
from pathlib import Path
from decimal import Decimal
from amazon_confirm import load_changeset, summarize_changeset, ChangesetSummary

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
