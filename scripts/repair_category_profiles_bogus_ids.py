"""Repair non-UUID top-level category entries in category_profiles.json.

Claude's flat categorizer historically echoed a category NAME into the
category_id field instead of its UUID (fixed in categorizer.claude_categorize,
commit 69893de). Before that fix, record_rejection persisted those bad ids as
bogus name-keyed entries in the profile store — duplicating the real,
UUID-keyed category and splitting its correction history across both.

This script merges each bogus name-keyed entry into its UUID-keyed twin:
- Union merchants and item_exemplars (dedup by key/value).
- Move corrections over, remapping any from_category_id/to_category_id that
  point at the bogus key (or any other stale bare-name id) to the UUID.
- Keep the UUID entry's description if non-empty, else take the bogus
  entry's. Mark the merged entry dirty (safe default: prompts a refresh).
- Delete the bogus top-level key.

Deterministic and conservative:
- Only entries whose top-level key is NOT a UUID are touched.
- Each bogus key must resolve to EXACTLY ONE UUID-keyed category with the same
  name. Otherwise: abort, no file written.
- Post-condition: after the merge, zero non-UUID top-level keys and zero
  dangling bare-name references inside any correction. Otherwise: abort.
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

PROFILES_PATH = Path("data/cache/category_profiles.json")


def is_uuid(v):
    return v is not None and bool(UUID_RE.match(str(v)))


def merge_exemplars(dst, src):
    for name, entry in src.items():
        if name not in dst:
            dst[name] = dict(entry)
            continue
        d, s = dst[name], entry
        d["count"] = d.get("count", 0) + s.get("count", 0)
        if s.get("last_seen", "") > d.get("last_seen", ""):
            d["last_seen"] = s["last_seen"]


def remap_correction_ids(corrections, bogus_to_uuid):
    for corr in corrections:
        for field in ("from_category_id", "to_category_id"):
            v = corr.get(field)
            if v in bogus_to_uuid:
                corr[field] = bogus_to_uuid[v]


def main(argv):
    if len(argv) != 1:
        print("usage: repair_category_profiles_bogus_ids.py")
        return 1

    if not PROFILES_PATH.exists():
        print(f"ABORT: {PROFILES_PATH} not found")
        return 1

    profiles = json.loads(PROFILES_PATH.read_text())
    cats = profiles["categories"]

    bogus_keys = [cid for cid in cats if not is_uuid(cid)]
    if not bogus_keys:
        print("No non-UUID category entries found. Nothing to do.")
        return 0

    name_to_uuid_ids = {}
    for cid, cat in cats.items():
        if is_uuid(cid):
            name_to_uuid_ids.setdefault(cat["name"], []).append(cid)

    bogus_to_uuid = {}
    for bk in bogus_keys:
        name = cats[bk]["name"]
        twins = name_to_uuid_ids.get(name, [])
        if len(twins) != 1:
            print(f"ABORT: bogus key {bk!r} (name={name!r}) resolves to {twins} "
                  f"(need exactly one UUID-keyed twin)")
            return 1
        bogus_to_uuid[bk] = twins[0]

    print("Merge plan:")
    for bk, uid in bogus_to_uuid.items():
        print(f"  {bk!r} -> {uid!r}")

    # Merge each bogus entry into its twin.
    for bk, uid in bogus_to_uuid.items():
        bogus = cats[bk]
        real = cats[uid]

        real_merchants = set(real.get("merchants", []))
        for m in bogus.get("merchants", []):
            real_merchants.add(m)
        real["merchants"] = sorted(real_merchants)

        merge_exemplars(real.setdefault("item_exemplars", {}), bogus.get("item_exemplars", {}))

        if not real.get("description") and bogus.get("description"):
            real["description"] = bogus["description"]

        real.setdefault("corrections", []).extend(bogus.get("corrections", []))
        real["dirty"] = True

        del cats[bk]

    # Remap any dangling bare-name references left inside corrections
    # cache-wide (not just the merged categories' own lists).
    for cat in cats.values():
        remap_correction_ids(cat.get("corrections", []), bogus_to_uuid)

    # Dedup corrections that now appear twice (bogus + real both recorded the
    # same subject/from/to before the merge).
    for cat in cats.values():
        seen = set()
        deduped = []
        for corr in cat.get("corrections", []):
            key = (corr.get("subject"), corr.get("from_category_id"), corr.get("to_category_id"))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(corr)
        cat["corrections"] = deduped

    # Post-condition checks.
    remaining_bogus = [cid for cid in cats if not is_uuid(cid)]
    if remaining_bogus:
        print(f"ABORT: non-UUID top-level keys remain after merge: {remaining_bogus}")
        return 1

    dangling = []
    for cid, cat in cats.items():
        for corr in cat.get("corrections", []):
            for field in ("from_category_id", "to_category_id"):
                v = corr.get(field)
                if v is not None and not is_uuid(v):
                    dangling.append((cid, field, v))
    if dangling:
        print(f"ABORT: dangling non-UUID references remain in corrections: {dangling}")
        return 1

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = PROFILES_PATH.with_name(f"category_profiles.backup-{stamp}.json")
    backup_path.write_text(json.dumps(json.loads(PROFILES_PATH.read_text()), indent=2))
    print(f"Backed up original to: {backup_path}")

    PROFILES_PATH.write_text(json.dumps(profiles, indent=2))
    print(f"Repaired {len(bogus_to_uuid)} bogus categor{'y' if len(bogus_to_uuid) == 1 else 'ies'}.")
    print(f"Wrote: {PROFILES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
