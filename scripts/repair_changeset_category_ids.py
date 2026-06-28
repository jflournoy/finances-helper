"""Repair non-UUID category_id values in a reviewed enrich-changeset.

Claude's flat categorizer occasionally returned the category NAME in the
category_id field instead of its UUID (fixed going forward in
categorizer.claude_categorize). This repairs an already-reviewed sidecar that
contains such entries, so the user's review decisions don't have to be redone.

Deterministic and conservative:
- Only proposals whose category_id is a non-UUID string are touched.
- The bad value must match exactly ONE category name in category_profiles.json
  AND agree with the proposal's existing category_name field. Otherwise: abort.
- Writes a NEW *-repaired.json file; never mutates the input.
- After repair, EVERY non-null category_id must be a UUID or the script exits 1.
"""
import json
import re
import sys
from collections import defaultdict

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

PROFILES_PATH = "data/cache/category_profiles.json"


def is_uuid(v):
    return v is not None and bool(UUID_RE.match(str(v)))


def main(argv):
    if len(argv) != 2:
        print("usage: repair_changeset_category_ids.py <reviewed-changeset.json>")
        return 1
    src = argv[1]
    if not src.endswith("-reviewed.json"):
        print(f"refusing: expected a *-reviewed.json sidecar, got {src!r}")
        return 1

    profiles = json.load(open(PROFILES_PATH))
    name_to_ids = defaultdict(list)
    for cid, cat in profiles["categories"].items():
        name_to_ids[cat["name"]].append(cid)

    data = json.load(open(src))
    proposals = data["non_amazon"]["proposals"]

    repaired = 0
    for i, p in enumerate(proposals):
        cid = p.get("category_id")
        if cid is None or is_uuid(cid):
            continue
        name = str(cid)
        ids = name_to_ids.get(name, [])
        if len(ids) != 1:
            print(f"ABORT: proposal {i} category_id={name!r} resolves to {ids} "
                  f"(need exactly one match)")
            return 1
        existing_name = p.get("category_name")
        if existing_name != name:
            print(f"ABORT: proposal {i} category_id={name!r} disagrees with "
                  f"category_name={existing_name!r}")
            return 1
        p["category_id"] = ids[0]
        repaired += 1

    # Post-condition: no non-UUID category_id remains.
    remaining = [i for i, p in enumerate(proposals)
                 if p.get("category_id") is not None and not is_uuid(p["category_id"])]
    if remaining:
        print(f"ABORT: {len(remaining)} non-UUID category_id(s) remain after repair: {remaining}")
        return 1

    # Also check amazon splits' subtransactions just in case.
    for i, split in enumerate(data["amazon"]["proposed_splits"]):
        for s in split.get("subtransactions", []) or []:
            if s.get("category_id") is not None and not is_uuid(s["category_id"]):
                print(f"ABORT: amazon split {i} has non-UUID subtxn category_id "
                      f"{s['category_id']!r} (not handled by this repair)")
                return 1

    out = src.replace("-reviewed.json", "-reviewed-repaired.json")
    with open(out, "w") as f:
        json.dump(data, f, indent=2)
    print(f"Repaired {repaired} proposal category_id(s).")
    print(f"Wrote: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
