"""Tests for view_changeset.py — markdown→HTML rendering and annotation."""
from pathlib import Path

import pytest

import view_changeset as vc


@pytest.fixture
def sample_md(tmp_path: Path) -> Path:
    md = tmp_path / "enrich-changeset-test.md"
    md.write_text(
        "# Enrich Changeset\n\n"
        "## Non-Amazon\n"
        "| Date | Payee | Amount | Category | Tier | Confidence |\n"
        "|------|-------|--------|----------|------|------------|\n"
        "| 2026-01-01 | Foo | -12.34 | Groceries | history | 0.55 |\n"
        "| 2026-01-02 | Bar | -50 | [UNCATEGORIZED] | claude | 0.80 |\n"
        "| 2026-01-03 | Baz | -7.50 | Dining Out | fuzzy | 0.40 |\n"
    )
    return md


def test_render_html_creates_sibling(sample_md: Path):
    html_path = vc.render_html(sample_md)
    assert html_path == sample_md.with_suffix(".html")
    assert html_path.exists()
    content = html_path.read_text()
    assert "<table>" in content
    assert "<h1>" in content


def test_amount_cells_annotated(sample_md: Path):
    html = vc.render_html(sample_md).read_text()
    assert '<td class="amount">-12.34</td>' in html
    assert '<td class="amount">0.55</td>' in html


def test_uncategorized_highlighted(sample_md: Path):
    html = vc.render_html(sample_md).read_text()
    assert '<span class="uncat">[UNCATEGORIZED]</span>' in html


def test_tier_classes_applied(sample_md: Path):
    html = vc.render_html(sample_md).read_text()
    assert '<td class="tier-history">history</td>' in html
    assert '<td class="tier-claude">claude</td>' in html
    assert '<td class="tier-fuzzy">fuzzy</td>' in html


def test_resolve_md_path_from_json(tmp_path: Path):
    md = tmp_path / "cs.md"
    md.write_text("# x")
    json_path = tmp_path / "cs.json"
    json_path.write_text("{}")
    assert vc._resolve_md_path(json_path) == md


def test_resolve_md_path_missing_sibling(tmp_path: Path):
    json_path = tmp_path / "cs.json"
    json_path.write_text("{}")
    with pytest.raises(FileNotFoundError):
        vc._resolve_md_path(json_path)


def test_resolve_md_path_rejects_other_suffix(tmp_path: Path):
    bad = tmp_path / "foo.txt"
    bad.write_text("")
    with pytest.raises(ValueError):
        vc._resolve_md_path(bad)


def test_render_html_no_banner_by_default(sample_md: Path):
    html = vc.render_html(sample_md).read_text()
    assert 'class="banner"' not in html


def test_render_html_includes_banner_when_provided(sample_md: Path):
    html = vc.render_html(sample_md, banner_html_unsafe="hello banner").read_text()
    assert '<div class="banner">hello banner</div>' in html


def test_render_html_banner_appears_before_table(sample_md: Path):
    html = vc.render_html(sample_md, banner_html_unsafe="warning").read_text()
    banner_idx = html.index('class="banner"')
    table_idx = html.index("<table>")
    assert banner_idx < table_idx
