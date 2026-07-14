"""Repair non-UUID category_id keys inside payee_lookup.json entries.

Claude's flat categorizer historically echoed a category NAME into the
category_id field instead of its UUID (fixed in categorizer.claude_categorize,
commit 69893de). Before that fix, record_categorization persisted those bad
ids as bogus name-keyed entries inside a payee's "categories" map — sitting
alongside (or instead of) the real UUID-keyed entry for the same category.

This script remaps every bare-name category_id to its UUID, using
category_profiles.json as the name->id source of truth (each name must
resolve to exactly one UUID-keyed category there — same precondition the
sibling category_profiles repair script enforces). When a payee already has
BOTH a bare-name and a UUID entry for the same category, their counts are
merged (added together), not overwritten, so no observation history is lost.

Deterministic and conservative:
- Only category_id keys that are NOT a UUID are touched.
- Each bogus name must resolve to EXACTLY ONE UUID-keyed category in
  category_profiles.json. Otherwise: abort, no file written.
- Post-condition: after remapping, zero non-UUID category_id keys remain in
  any payee's categories map. Otherwise: abort.
- Writes a timestamped backup of the original file before overwriting it.
"""
import datetime
import json
import re
import sys
from pathlib import Path

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

PAYEE_LOOKUP_PATH = Path("data/cache/payee_lookup.json")
PROFILES_PATH = Path("data/cache/category_profiles.json")


def is_uuid(v):
    return v is not None and bool(UUID_RE.match(str(v)))


def main(argv):
    if len(argv) != 1:
        print("usage: repair_payee_lookup_bogus_ids.py")
        return 1

    if not PAYEE_LOOKUP_PATH.exists():
        print(f"ABORT: {PAYEE_LOOKUP_PATH} not found")
        return 1
    if not PROFILES_PATH.exists():
        print(f"ABORT: {PROFILES_PATH} not found (needed as name->id source of truth)")
        return 1

    profiles = json.loads(PROFILES_PATH.read_text())
    name_to_uuid_ids = {}
    for cid, cat in profiles["categories"].items():
        if is_uuid(cid):
            name_to_uuid_ids.setdefault(cat["name"], []).append(cid)

    cache = json.loads(PAYEE_LOOKUP_PATH.read_text())

    bogus_names = set()
    for payee, entry in cache.items():
        if payee.startswith("_") or not isinstance(entry, dict) or "alias_of" in entry:
            continue
        for cat_id in entry.get("categories", {}):
            if not is_uuid(cat_id):
                bogus_names.add(cat_id)

    if not bogus_names:
        print("No non-UUID category_id keys found. Nothing to do.")
        return 0

    name_to_uuid = {}
    for name in sorted(bogus_names):
        twins = name_to_uuid_ids.get(name, [])
        if len(twins) != 1:
            print(f"ABORT: bogus category name {name!r} resolves to {twins} "
                  f"(need exactly one UUID-keyed category in {PROFILES_PATH})")
            return 1
        name_to_uuid[name] = twins[0]

    print("Remap plan:")
    for name, uid in name_to_uuid.items():
        print(f"  {name!r} -> {uid!r}")

    remapped_payees = 0
    merged_collisions = 0
    for payee, entry in cache.items():
        if payee.startswith("_") or not isinstance(entry, dict) or "alias_of" in entry:
            continue
        categories = entry.get("categories", {})
        bogus_keys = [k for k in categories if k in name_to_uuid]
        if not bogus_keys:
            continue

        for bogus_key in bogus_keys:
            uid = name_to_uuid[bogus_key]
            bogus_info = categories.pop(bogus_key)
            if uid in categories:
                categories[uid]["count"] += bogus_info["count"]
                merged_collisions += 1
            else:
                categories[uid] = {"name": bogus_info["name"], "count": bogus_info["count"]}
        remapped_payees += 1

    # Post-condition.
    remaining = []
    for payee, entry in cache.items():
        if payee.startswith("_") or not isinstance(entry, dict) or "alias_of" in entry:
            continue
        for cat_id in entry.get("categories", {}):
            if not is_uuid(cat_id):
                remaining.append((payee, cat_id))
    if remaining:
        print(f"ABORT: non-UUID category_id keys remain after remap: {remaining}")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = PAYEE_LOOKUP_PATH.with_name(f"payee_lookup.backup-{stamp}.json")
    backup_path.write_text(json.dumps(json.loads(PAYEE_LOOKUP_PATH.read_text()), indent=2))
    print(f"Backed up original to: {backup_path}")

    PAYEE_LOOKUP_PATH.write_text(json.dumps(cache, indent=2))
    print(f"Remapped {len(bogus_names)} bogus category name(s) across {remapped_payees} payee(s) "
          f"({merged_collisions} count-merge collision(s)).")
    print(f"Wrote: {PAYEE_LOOKUP_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
