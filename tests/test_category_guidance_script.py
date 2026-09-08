"""Tests for scripts/category_guidance.py — the durable-guidance CLI.

Guidance is the one profile field the user writes by hand, so the CLI must never
guess which category was meant, never write without a backup, and never lose an
existing rule silently.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from category_guidance import resolve_category, main
from category_profiles import PROFILES_VERSION, load_profiles, get_guidance


def _store(cats):
    """cats: {cat_id: (name, guidance)}"""
    return {
        "_version": PROFILES_VERSION,
        "_backfilled": {},
        "categories": {
            cid: {
                "name": name,
                "description": "",
                "guidance": guidance,
                "merchants": [],
                "item_exemplars": {},
                "corrections": [],
                "dirty": False,
            }
            for cid, (name, guidance) in cats.items()
        },
    }


def _write(tmp_path, cats):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(_store(cats)))
    return path


# --- resolve_category -------------------------------------------------------

def test_resolve_matches_exact_id():
    profiles = _store({"id-a": ("Home goods", "")})
    assert resolve_category(profiles, "id-a") == "id-a"


def test_resolve_matches_name_substring_case_insensitively():
    profiles = _store({"id-a": ("Home goods (roll over)", "")})
    assert resolve_category(profiles, "home goods") == "id-a"


def test_resolve_rejects_ambiguous_name():
    """NO SILENT FALLBACK: never guess between two plausible categories."""
    profiles = _store({
        "id-a": ("Home goods", ""),
        "id-b": ("Home Renovation", ""),
    })
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_category(profiles, "home")


def test_resolve_rejects_unknown_name():
    profiles = _store({"id-a": ("Home goods", "")})
    with pytest.raises(ValueError, match="No category matches"):
        resolve_category(profiles, "Zebra")


# --- set --------------------------------------------------------------------

def test_set_writes_guidance_and_backup(tmp_path):
    path = _write(tmp_path, {"id-a": ("Home goods", "")})

    rc = main(["--path", str(path), "set", "Home goods", "Tools belong here."])

    assert rc == 0
    assert get_guidance(load_profiles(str(path)), "id-a") == "Tools belong here."
    assert len(list(tmp_path.glob("profiles.backup-guidance-*.json"))) == 1


def test_set_reports_the_replaced_rule(tmp_path, capsys):
    """Overwriting an existing rule must show what was lost."""
    path = _write(tmp_path, {"id-a": ("Home goods", "Old rule.")})

    main(["--path", str(path), "set", "Home goods", "New rule."])

    out = capsys.readouterr().out
    assert "Old rule." in out
    assert "New rule." in out


def test_set_on_ambiguous_name_errors_without_writing(tmp_path, capsys):
    path = _write(tmp_path, {"id-a": ("Home goods", ""), "id-b": ("Home Renovation", "")})
    original = path.read_text()

    rc = main(["--path", str(path), "set", "home", "Tools belong here."])

    assert rc == 1
    assert path.read_text() == original
    assert "ambiguous" in capsys.readouterr().err


def test_set_rejects_empty_rule(tmp_path, capsys):
    path = _write(tmp_path, {"id-a": ("Home goods", "")})
    rc = main(["--path", str(path), "set", "Home goods", "   "])
    assert rc == 1
    assert "empty" in capsys.readouterr().err


# --- clear ------------------------------------------------------------------

def test_clear_removes_rule(tmp_path):
    path = _write(tmp_path, {"id-a": ("Home goods", "Tools belong here.")})

    rc = main(["--path", str(path), "clear", "Home goods"])

    assert rc == 0
    assert get_guidance(load_profiles(str(path)), "id-a") == ""


def test_clear_without_existing_rule_is_a_noop(tmp_path, capsys):
    path = _write(tmp_path, {"id-a": ("Home goods", "")})

    rc = main(["--path", str(path), "clear", "Home goods"])

    assert rc == 0
    assert "Nothing to do" in capsys.readouterr().out
    assert not list(tmp_path.glob("*backup-guidance*"))


# --- list -------------------------------------------------------------------

def test_list_shows_only_categories_with_guidance(tmp_path, capsys):
    path = _write(tmp_path, {
        "id-a": ("Home goods", "Tools belong here."),
        "id-b": ("Groceries", ""),
    })

    main(["--path", str(path), "list"])

    out = capsys.readouterr().out
    assert "Home goods" in out
    assert "Groceries" not in out


def test_list_all_shows_every_category(tmp_path, capsys):
    path = _write(tmp_path, {
        "id-a": ("Home goods", "Tools belong here."),
        "id-b": ("Groceries", ""),
    })

    main(["--path", str(path), "list", "--all"])

    out = capsys.readouterr().out
    assert "Home goods" in out
    assert "Groceries" in out


def test_missing_file_errors_loudly(tmp_path, capsys):
    rc = main(["--path", str(tmp_path / "nope.json"), "list"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err
