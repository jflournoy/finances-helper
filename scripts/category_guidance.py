"""Read and write durable per-category guidance.

Why this exists
---------------
A category's ``description`` is machine-generated and rewritten wholesale every
time ``tag.py --refresh-profiles`` runs. Hand-editing it does not stick: the next
time that category goes dirty, the edit is gone.

``guidance`` is the durable counterpart. You write it, regeneration never touches
it, and it is injected into both prompt paths — the live categorization prompt
(marked as a rule that overrides Claude's inference) and the description
generator (as authoritative context, so regenerated descriptions agree with it
rather than drifting back).

Use it for boundaries that merchant history alone will never express, e.g.
"multitools, hand tools and portable appliances belong here, not in Household
supplies."

Usage
-----
    # See every category with guidance, and the ids/names to target
    uv run python scripts/category_guidance.py list
    uv run python scripts/category_guidance.py list --all

    # Set a rule (match by name substring, case-insensitive, or exact id)
    uv run python scripts/category_guidance.py set "Home goods" \
        "Multitools, hand tools, and portable appliances belong here."

    # Remove a rule
    uv run python scripts/category_guidance.py clear "Home goods"

Writes are made in place to data/cache/category_profiles.json after a
timestamped backup.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from category_profiles import (
    DEFAULT_PROFILES_PATH,
    load_profiles,
    save_profiles,
    set_guidance,
    clear_guidance,
)


def resolve_category(profiles: dict, needle: str) -> str:
    """Resolve a category id or unique name substring to a category id.

    Raises ValueError if nothing matches, or if a name substring is ambiguous
    (NO SILENT FALLBACK — never guess which category the user meant).
    """
    cats = profiles.get("categories", {})
    if needle in cats:
        return needle

    lowered = needle.lower()
    matches = [
        (cid, c.get("name", "")) for cid, c in cats.items()
        if lowered in (c.get("name") or "").lower()
    ]
    if not matches:
        raise ValueError(
            f"No category matches {needle!r}. Run `list --all` to see the options."
        )
    if len(matches) > 1:
        listing = "\n".join(f"    {name}  ({cid})" for cid, name in sorted(matches, key=lambda m: m[1]))
        raise ValueError(
            f"{needle!r} is ambiguous — matches {len(matches)} categories:\n{listing}\n"
            f"Use the exact name or the category id."
        )
    return matches[0][0]


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.backup-guidance-{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    return backup


def cmd_list(profiles: dict, args) -> int:
    cats = profiles.get("categories", {})
    rows = [
        (c.get("name", "?"), cid, c.get("guidance", ""), c.get("description", ""))
        for cid, c in cats.items()
    ]
    if not args.all:
        rows = [r for r in rows if r[2]]
        if not rows:
            print(
                "No categories have guidance yet.\n"
                "Run with --all to see every category, then:\n"
                '  uv run python scripts/category_guidance.py set "<name>" "<rule>"'
            )
            return 0

    for name, cid, guidance, desc in sorted(rows):
        print(f"\n{name}  ({cid})")
        if desc:
            print(f"    description (generated): {desc}")
        if guidance:
            print(f"    RULE (durable):          {guidance}")
        elif args.all:
            print("    RULE (durable):          —")
    print()
    return 0


def cmd_set(profiles: dict, args, path: Path) -> int:
    cat_id = resolve_category(profiles, args.category)
    name = profiles["categories"][cat_id].get("name", "?")
    previous = profiles["categories"][cat_id].get("guidance", "")

    set_guidance(profiles, cat_id, args.text)

    backup = _backup(path)
    save_profiles(profiles, str(path))

    print(f"Backup written: {backup}")
    if previous:
        print(f"\n{name} ({cat_id})\n  was: {previous}\n  now: {args.text}")
    else:
        print(f"\n{name} ({cat_id})\n  rule: {args.text}")
    print(
        "\nThis rule is now injected into the categorization prompt and will "
        "survive every --refresh-profiles."
    )
    return 0


def cmd_clear(profiles: dict, args, path: Path) -> int:
    cat_id = resolve_category(profiles, args.category)
    name = profiles["categories"][cat_id].get("name", "?")
    previous = profiles["categories"][cat_id].get("guidance", "")
    if not previous:
        print(f"{name} ({cat_id}) has no guidance. Nothing to do.")
        return 0

    clear_guidance(profiles, cat_id)
    backup = _backup(path)
    save_profiles(profiles, str(path))

    print(f"Backup written: {backup}")
    print(f"\nCleared guidance on {name} ({cat_id}):\n  was: {previous}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PROFILES_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show categories with guidance")
    p_list.add_argument("--all", action="store_true", help="include categories with no guidance")

    p_set = sub.add_parser("set", help="attach a durable rule to a category")
    p_set.add_argument("category", help="category name substring or exact id")
    p_set.add_argument("text", help="the rule, in plain English")

    p_clear = sub.add_parser("clear", help="remove a category's rule")
    p_clear.add_argument("category", help="category name substring or exact id")

    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: category profiles not found at {path}", file=sys.stderr)
        return 1

    try:
        profiles = load_profiles(str(path))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        if args.command == "list":
            return cmd_list(profiles, args)
        if args.command == "set":
            return cmd_set(profiles, args, path)
        if args.command == "clear":
            return cmd_clear(profiles, args, path)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
