"""Render an enrich-changeset Markdown summary to a styled HTML sibling file
and open it in the system default browser.

Used by decide.py to give the user a readable, scannable view of the proposed
changeset while the interactive walkthrough runs in the terminal.

Standalone-runnable for ad-hoc viewing:
    uv run python view_changeset.py [path-to-.md-or-.json]
"""
import argparse
import re
import sys
import webbrowser
from pathlib import Path

import markdown


CSS = """
:root { color-scheme: light dark; }
body {
  max-width: 1200px; margin: 2em auto; padding: 0 1.5em;
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  color: #1a1a1a; background: #fff;
}
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.25em; margin-top: 1.5em; }
h1 { font-size: 1.6em; }
h2 { font-size: 1.25em; }
table { border-collapse: collapse; width: 100%; margin: 0.75em 0 1.5em; font-size: 13px; }
thead th {
  background: #f4f5f7; position: sticky; top: 0;
  padding: 8px 10px; text-align: left; border-bottom: 2px solid #d0d4da;
  font-weight: 600; z-index: 1;
}
td { padding: 5px 10px; border-bottom: 1px solid #eee; vertical-align: top; }
tbody tr:nth-child(even) td { background: #fafbfc; }
tbody tr:hover td { background: #fff8e1; }
code { background: #f4f4f4; padding: 1px 5px; border-radius: 3px; font-size: 0.92em; }
.amount { font-family: ui-monospace, "SF Mono", Menlo, monospace; text-align: right; white-space: nowrap; }
.uncat { color: #c00; font-weight: 600; }
.tier-claude { color: #b85c00; font-weight: 600; }
.tier-fuzzy { color: #8a6d00; }
.tier-history { color: #555; }
.tier-amazon-wf { color: #006400; }
.meta { color: #666; font-size: 0.92em; margin-bottom: 1em; }
.banner {
  background: #fff3cd; border: 1px solid #ffe066; color: #664d03;
  padding: 8px 14px; border-radius: 4px; margin-bottom: 1em;
  font-size: 0.95em;
}
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #1a1a1a; }
  h1, h2 { border-color: #333; }
  thead th { background: #2a2a2a; border-color: #444; }
  td { border-color: #2a2a2a; }
  tbody tr:nth-child(even) td { background: #222; }
  tbody tr:hover td { background: #2d2a1a; }
  code { background: #2a2a2a; }
  .uncat { color: #ff6b6b; }
  .tier-claude { color: #ffa94d; }
  .tier-fuzzy { color: #ffd43b; }
  .tier-history { color: #999; }
  .tier-amazon-wf { color: #51cf66; }
  .meta { color: #999; }
  .banner { background: #3d3520; border-color: #6b5a2a; color: #ffd966; }
}
"""


def _annotate_amount_cells(html: str) -> str:
    """Add class='amount' to <td> cells whose text looks like a number/dollar value.

    Matches: integers, decimals, optional leading $, optional leading -.
    """
    pattern = re.compile(
        r"<td>(\s*-?\$?\d[\d,]*(?:\.\d+)?\s*)</td>"
    )
    return pattern.sub(r'<td class="amount">\1</td>', html)


def _annotate_uncategorized(html: str) -> str:
    return html.replace(
        "[UNCATEGORIZED]",
        '<span class="uncat">[UNCATEGORIZED]</span>',
    )


def _annotate_tiers(html: str) -> str:
    """Wrap tier values in <td>history</td> / <td>fuzzy</td> / <td>claude</td> / <td>amazon-wf</td>."""
    for tier in ("claude", "fuzzy", "history", "amazon-wf"):
        html = re.sub(
            rf"<td>\s*{re.escape(tier)}\s*</td>",
            f'<td class="tier-{tier}">{tier}</td>',
            html,
        )
    return html


def _resolve_md_path(input_path: Path) -> Path:
    """Accept a .md or .json changeset path; return the .md path.

    Raises FileNotFoundError if the .md cannot be located.
    """
    if input_path.suffix == ".md":
        if not input_path.exists():
            raise FileNotFoundError(f"markdown file not found: {input_path}")
        return input_path

    if input_path.suffix == ".json":
        md_path = input_path.with_suffix(".md")
        if not md_path.exists():
            raise FileNotFoundError(
                f"no sibling .md for {input_path.name} (expected {md_path})"
            )
        return md_path

    raise ValueError(
        f"unsupported file type {input_path.suffix!r}: expected .md or .json"
    )


_STALE_BANNER = (
    "This view shows the <strong>original proposed changeset</strong>. "
    "Any decisions you've made in the terminal (accept / recategorize / skip) "
    "are not reflected here — check the terminal for current review state."
)


def render_html(md_path: Path, *, banner_html_unsafe: str | None = None) -> Path:
    """Render a changeset .md to a styled .html sibling, return the .html path.

    The output file lives next to the input, with .html extension. Overwrites
    any existing .html sibling. If `banner_html_unsafe` is provided, it is
    inserted RAW into the HTML above the rendered markdown — the caller is
    responsible for HTML-escaping any user-derived content (payee names,
    transaction memos, etc.). The current callers only pass module-level
    constants, hence raw insertion is safe; do not pass arbitrary user input
    without escaping.
    """
    md_text = md_path.read_text()
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code"],
    )
    body = _annotate_amount_cells(body)
    body = _annotate_uncategorized(body)
    body = _annotate_tiers(body)

    banner_block = (
        f'<div class="banner">{banner_html_unsafe}</div>' if banner_html_unsafe else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{md_path.stem}</title>
<style>{CSS}</style>
</head>
<body>
{banner_block}
<div class="meta">Rendered from <code>{md_path.name}</code></div>
{body}
</body>
</html>
"""

    html_path = md_path.with_suffix(".html")
    html_path.write_text(html)
    return html_path


def render_and_open(
    input_path: Path,
    *,
    banner_html_unsafe: str | None = _STALE_BANNER,
) -> Path:
    """Resolve the .md, render to .html, open in default browser. Return .html path.

    Defaults to including the stale-state banner since this entry point is used
    by decide.py — the user is about to make decisions whose results won't be
    reflected in the browser view. See `render_html` docstring re: escaping.

    Prints a refresh-tab hint to stdout — the renderer overwrites the .html
    file each call, so an already-open browser tab will keep showing the
    previous render until the user refreshes.
    """
    md_path = _resolve_md_path(input_path)
    html_path = render_html(md_path, banner_html_unsafe=banner_html_unsafe)
    webbrowser.open(f"file://{html_path.resolve()}")
    print("(refresh the browser tab if you already had it open)")
    return html_path


def _newest_changeset_md() -> Path:
    """Find the newest data/cache/enrich-changeset-*.md by mtime."""
    candidates = sorted(
        Path("data/cache").glob("enrich-changeset-*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            "no enrich-changeset-*.md found in data/cache/. Run /tag first."
        )
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render an enrich-changeset markdown summary to HTML and open it."
    )
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help="Path to changeset .md or .json. Defaults to newest in data/cache/.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Render the HTML file but do not launch the browser.",
    )
    args = parser.parse_args()

    try:
        if args.path is None:
            md_path = _newest_changeset_md()
        else:
            md_path = _resolve_md_path(args.path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    html_path = render_html(md_path, banner_html_unsafe=None)
    print(f"Rendered: {html_path}")
    print("(refresh the browser tab if you already had it open)")

    if not args.no_open:
        webbrowser.open(f"file://{html_path.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
