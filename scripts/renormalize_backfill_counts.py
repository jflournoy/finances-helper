"""Repair exemplar counts inflated by the non-idempotent backfill bug.

Background
----------
``backfill_from_ynab_subtransactions`` used to re-record every historical Amazon
subtransaction on EVERY ``tag.py`` run (``build_profiles`` calls it
unconditionally). Counts therefore compounded once per run: backfilled items
reached count=42 while genuinely confirmed splits — the strongest signal the
system has — sat at count=1.

That inverts the invariant the module documents ("history is a weak prior; a
handful of fresh confirmations outvote a stale historical split") and it feeds
``format_profiles_for_prompt``, which surfaces the top-N exemplars by count. The
prompt was therefore teaching Claude that e.g. Household supplies means painter's
tape, zip ties and IKEA totes — which is why durable goods land there.

The compounding itself is fixed in category_profiles.py. This script repairs the
already-corrupt store.

What it does
------------
Collapses every inflated exemplar count to 1, the weight a historical prior is
supposed to carry. Confirmed splits already sit at 1 and are left untouched, so
afterwards history and confirmations weigh the same and any FUTURE confirmation
correctly outvotes history.

Detection is structural, not a hardcoded 42: any count above ``--prior-weight``
is treated as inflated. Counts at or below it are left alone.

The script cannot distinguish a backfilled exemplar from a genuinely repeated
confirmation, because the old code recorded both identically. Collapsing to 1 is
the conservative choice: it discards accumulated weight rather than preserving
a value that is mostly duplication artifact. Re-confirmations rebuild real weight.

Usage
-----
    uv run python scripts/renormalize_backfill_counts.py            # dry run
    uv run python scripts/renormalize_backfill_counts.py --apply    # write

A timestamped backup is written next to the store before any modification.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from category_profiles import DEFAULT_PROFILES_PATH, PROFILES_VERSION


def find_inflated(profiles: dict, prior_weight: int) -> list:
    """Return [(category_name, item, category_id, count), ...] for counts > prior_weight."""
    inflated = []
    for cat in profiles.get("categories", {}).values():
        for item, entry in cat.get("item_exemplars", {}).items():
            for cat_id, info in entry.items():
                if info["count"] > prior_weight:
                    inflated.append((cat.get("name", "?"), item, cat_id, info["count"]))
    return inflated


def renormalize(profiles: dict, prior_weight: int) -> int:
    """Clamp every exemplar count down to prior_weight. Returns number changed."""
    changed = 0
    for cat in profiles.get("categories", {}).values():
        for entry in cat.get("item_exemplars", {}).values():
            for info in entry.values():
                if info["count"] > prior_weight:
                    info["count"] = prior_weight
                    changed += 1
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=DEFAULT_PROFILES_PATH)
    parser.add_argument("--prior-weight", type=int, default=1)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args(argv)

    if args.prior_weight < 1:
        parser.error("--prior-weight must be >= 1")

    path = Path(args.path)
    if not path.exists():
        print(f"ERROR: category profiles not found at {path}", file=sys.stderr)
        return 1

    profiles = json.loads(path.read_text())
    if profiles.get("_version") != PROFILES_VERSION:
        print(
            f"ERROR: unsupported profiles version {profiles.get('_version')!r} "
            f"(expected {PROFILES_VERSION})",
            file=sys.stderr,
        )
        return 1

    inflated = find_inflated(profiles, args.prior_weight)
    if not inflated:
        print(f"No exemplar counts above {args.prior_weight}. Nothing to do.")
        return 0

    inflated.sort(key=lambda r: (-r[3], r[0], r[1]))
    print(f"Found {len(inflated)} exemplar(s) with count > {args.prior_weight}:\n")
    for cat_name, item, _cat_id, count in inflated:
        print(f"  {count:>4}  {cat_name:<28} {item[:62]}")

    if not args.apply:
        print(f"\nDry run. Re-run with --apply to clamp these to {args.prior_weight}.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.backup-prerenorm-{stamp}{path.suffix}")
    shutil.copy2(path, backup)
    print(f"\nBackup written: {backup}")

    changed = renormalize(profiles, args.prior_weight)
    path.write_text(json.dumps(profiles, indent=2, sort_keys=True))
    print(f"Clamped {changed} exemplar count(s) to {args.prior_weight}.")
    print(f"Wrote {path}")
    print(
        "\nNext: `uv run python tag.py --refresh-profiles` to regenerate "
        "descriptions from your queued corrections. That path no longer "
        "backfills, so it will not disturb these counts."
    )
    if not profiles.get("_backfilled"):
        print(
            "\nNote: the _backfilled ledger is empty (this store predates the "
            "idempotency fix), so the next normal `tag.py --days N` run re-ingests "
            "history once, taking these exemplars from 1 to 2. That is a one-time "
            "step — the ledger is populated afterward and counts stay put. Re-run "
            "this script with --apply after that run if you want them back at 1."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
