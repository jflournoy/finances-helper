"""Tests for scripts/renormalize_backfill_counts.py.

The script repairs exemplar counts inflated by the old non-idempotent backfill
(count=42 for backfilled history vs count=1 for real confirmations). It must be
conservative: clamp inflation, never invent or lose exemplars, never write
without an explicit --apply, and always leave a backup.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from renormalize_backfill_counts import find_inflated, renormalize, main
from category_profiles import PROFILES_VERSION


def _store(exemplars):
    """Build a profiles store: {cat_name: {item: {cat_id: count}}}."""
    cats = {}
    for cat_name, items in exemplars.items():
        cat_id = f"id-{cat_name}"
        cats[cat_id] = {
            "name": cat_name,
            "description": "",
            "merchants": [],
            "item_exemplars": {
                item: {cid: {"name": cat_name, "count": c} for cid, c in counts.items()}
                for item, counts in items.items()
            },
            "corrections": [],
            "dirty": False,
        }
    return {"_version": PROFILES_VERSION, "categories": cats, "_backfilled": {}}


def test_find_inflated_reports_only_counts_above_prior_weight():
    profiles = _store({
        "Household supplies": {"painters tape": {"id-Household supplies": 42}},
        "Home goods": {"otterbox case": {"id-Home goods": 1}},
    })
    inflated = find_inflated(profiles, 1)
    assert len(inflated) == 1
    assert inflated[0][1] == "painters tape"
    assert inflated[0][3] == 42


def test_renormalize_clamps_inflated_and_preserves_confirmed():
    profiles = _store({
        "Household supplies": {"painters tape": {"id-Household supplies": 42}},
        "Home goods": {"otterbox case": {"id-Home goods": 1}},
    })
    changed = renormalize(profiles, 1)
    assert changed == 1

    cats = profiles["categories"]
    assert cats["id-Household supplies"]["item_exemplars"]["painters tape"][
        "id-Household supplies"]["count"] == 1
    assert cats["id-Home goods"]["item_exemplars"]["otterbox case"][
        "id-Home goods"]["count"] == 1


def test_renormalize_makes_confirmations_able_to_outvote_history():
    """The whole point: after repair, a real confirmation can win."""
    from category_profiles import record_item_categorization, dominant_item_category

    profiles = _store({"Household supplies": {"leatherman": {"id-Household supplies": 42}}})
    renormalize(profiles, 1)

    record_item_categorization(profiles, "leatherman", "id-Home goods", "Home goods")
    record_item_categorization(profiles, "leatherman", "id-Home goods", "Home goods")

    assert dominant_item_category(profiles, "leatherman") == ("id-Home goods", "Home goods")


def test_renormalize_never_drops_exemplars():
    profiles = _store({"A": {"x": {"id-A": 42}, "y": {"id-A": 1}}})
    before = set(profiles["categories"]["id-A"]["item_exemplars"])
    renormalize(profiles, 1)
    assert set(profiles["categories"]["id-A"]["item_exemplars"]) == before


def test_dry_run_does_not_modify_the_file(tmp_path, capsys):
    path = tmp_path / "profiles.json"
    profiles = _store({"A": {"x": {"id-A": 42}}})
    path.write_text(json.dumps(profiles))
    original = path.read_text()

    rc = main(["--path", str(path)])

    assert rc == 0
    assert path.read_text() == original
    assert "Dry run" in capsys.readouterr().out


def test_apply_writes_backup_and_clamps(tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(_store({"A": {"x": {"id-A": 42}}})))

    rc = main(["--path", str(path), "--apply"])

    assert rc == 0
    written = json.loads(path.read_text())
    assert written["categories"]["id-A"]["item_exemplars"]["x"]["id-A"]["count"] == 1

    backups = list(tmp_path.glob("profiles.backup-prerenorm-*.json"))
    assert len(backups) == 1
    restored = json.loads(backups[0].read_text())
    assert restored["categories"]["id-A"]["item_exemplars"]["x"]["id-A"]["count"] == 42


def test_missing_file_errors_loudly(tmp_path, capsys):
    rc = main(["--path", str(tmp_path / "nope.json")])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_unsupported_version_errors_loudly(tmp_path, capsys):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps({"_version": 999, "categories": {}}))
    rc = main(["--path", str(path)])
    assert rc == 1
    assert "unsupported profiles version" in capsys.readouterr().err


def test_clean_store_is_a_noop(tmp_path, capsys):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(_store({"A": {"x": {"id-A": 1}}})))
    rc = main(["--path", str(path), "--apply"])
    assert rc == 0
    assert "Nothing to do" in capsys.readouterr().out
    assert not list(tmp_path.glob("*backup-prerenorm*"))
