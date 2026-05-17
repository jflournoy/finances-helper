"""Interactive review of an enrich-changeset before /confirm-apply.

Walks the user through risky proposals (Amazon splits, fuzzy/Claude tier
non-Amazon proposals), bulk-accepts the safe ones (history tier), and writes
a sidecar `*-reviewed.json` that amazon_confirm.py can operate on.

A skipped proposal is marked by setting `applied_at` to a sentinel string —
amazon_confirm.py already short-circuits on that field, so no changes to the
applier are needed.

A recategorized proposal mutates `category_id` / `category_name` in place and
records the original under a `review` block on the proposal.

Resumable: if the sidecar exists, the next run picks up unreviewed items only.

Usage:
    uv run python review.py [path-to-changeset.json]
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from thefuzz import process as fuzz_process

from changeset_review import REVIEW_SKIP_SENTINEL_PREFIX, is_review_skip
from view_changeset import render_and_open
from ynab_client import YNABClient


SKIP_SENTINEL_PREFIX = REVIEW_SKIP_SENTINEL_PREFIX


def _newest_changeset_json() -> Path:
    candidates = sorted(
        Path("data/cache").glob("enrich-changeset-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    candidates = [c for c in candidates if not c.stem.endswith("-reviewed")]
    if not candidates:
        raise FileNotFoundError(
            "no enrich-changeset-*.json found in data/cache/. Run /enrich-run first."
        )
    return candidates[0]


def _reviewed_sidecar_path(changeset_path: Path) -> Path:
    return changeset_path.with_name(changeset_path.stem + "-reviewed.json")


def _load_changeset(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _save_reviewed(changeset: dict, path: Path) -> None:
    with open(path, "w") as f:
        json.dump(changeset, f, indent=2)


def _flatten_categories(category_groups: list) -> list[dict]:
    """Return a flat list of {id, name, group_name, full_name} for non-hidden categories.

    Excludes internal groups (Internal Master Category, Credit Card Payments).
    """
    excluded_groups = {"Internal Master Category", "Credit Card Payments"}
    flat = []
    for group in category_groups:
        if group.get("hidden") or group.get("name") in excluded_groups:
            continue
        gname = group.get("name", "")
        for cat in group.get("categories", []):
            if cat.get("hidden"):
                continue
            flat.append({
                "id": cat["id"],
                "name": cat["name"],
                "group_name": gname,
                "full_name": f"{gname} / {cat['name']}",
            })
    return flat


def _pick_category(categories: list[dict], current_name: str | None) -> dict | None:
    """Interactive fuzzy-search category picker.

    Returns the chosen category dict, or None if the user bails.
    """
    while True:
        query = input("  search category (empty=cancel): ").strip()
        if not query:
            return None

        matches = fuzz_process.extract(
            query,
            {c["full_name"]: c for c in categories}.keys(),
            limit=8,
        )
        if not matches:
            print("  no matches")
            continue

        by_name = {c["full_name"]: c for c in categories}
        print()
        for i, (full_name, score) in enumerate(matches, start=1):
            cat = by_name[full_name]
            marker = " (current)" if cat["name"] == current_name else ""
            print(f"    {i}. {full_name}{marker}  [{score}]")
        print()

        choice = input("  pick number (or empty to re-search): ").strip()
        if not choice:
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(matches)):
            print("  invalid pick")
            continue
        return by_name[matches[int(choice) - 1][0]]


def _prompt_choice(prompt: str, valid: str) -> str:
    """Single-letter prompt, lowercased. Re-prompts until one of `valid` is entered."""
    while True:
        resp = input(f"  {prompt} [{'/'.join(valid)}]: ").strip().lower()
        if resp in valid:
            return resp
        print(f"  invalid; expected one of: {', '.join(valid)}")


def _has_review_decision(proposal: dict) -> bool:
    return "review" in proposal or is_review_skip(proposal.get("applied_at"))


def _mark_accepted(proposal: dict) -> None:
    proposal["review"] = {
        "decision": "accept",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
    }


def _mark_recategorized(
    proposal: dict,
    new_cat: dict,
    *,
    target_key_prefix: str = "",
) -> None:
    """Mutate category_id/category_name on the proposal (or subtransaction)
    and stash the original under a 'review' block.
    """
    orig_id = proposal.get(f"{target_key_prefix}category_id")
    orig_name = proposal.get(f"{target_key_prefix}category_name")
    proposal[f"{target_key_prefix}category_id"] = new_cat["id"]
    proposal[f"{target_key_prefix}category_name"] = new_cat["name"]
    proposal["review"] = {
        "decision": "recategorize",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "original_category_id": orig_id,
        "original_category_name": orig_name,
    }


def _mark_skipped(proposal: dict, reason: str = "user") -> None:
    proposal["review"] = {
        "decision": "skip",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
    }
    proposal["applied_at"] = f"{SKIP_SENTINEL_PREFIX}:{reason}"


def _walk_non_amazon(
    proposals: list[dict],
    label: str,
    categories: list[dict],
) -> str | None:
    """Walk a list of non-Amazon proposals interactively. Returns 'quit' if the
    user quits, else None.
    """
    if not proposals:
        return None

    print()
    print(f"=== {label} ({len(proposals)} item{'s' if len(proposals) != 1 else ''}) ===")

    for i, p in enumerate(proposals, start=1):
        if _has_review_decision(p):
            continue
        print()
        print(f"[{i}/{len(proposals)}] {p['payee_name']}  ${p['amount_dollars']}  {p['date']}")
        print(f"  Tier: {p['tier']}   Proposed: {p['category_name']}")
        if p.get("rationale"):
            print(f"  Rationale: {p['rationale']}")
        print(f"  Confidence: {p['confidence']:.2f}")

        choice = _prompt_choice("[a]ccept  [r]ecategorize  [s]kip  [q]uit", "arsq")
        if choice == "q":
            return "quit"
        if choice == "a":
            _mark_accepted(p)
        elif choice == "s":
            _mark_skipped(p)
        elif choice == "r":
            new_cat = _pick_category(categories, p.get("category_name"))
            if new_cat is None:
                print("  cancelled — leaving unreviewed; come back later")
                continue
            _mark_recategorized(p, new_cat)
            print(f"  → {new_cat['name']}")
    return None


def _walk_amazon_splits(
    splits: list[dict],
    categories: list[dict],
) -> str | None:
    """Walk Amazon splits, allowing edit of individual subtransaction categories."""
    if not splits:
        return None

    print()
    print(f"=== Amazon splits ({len(splits)}) ===")

    for i, split in enumerate(splits, start=1):
        if _has_review_decision(split):
            continue
        parent = split.get("parent_ynab_transaction", {})
        ship = split.get("shipment", {})
        print()
        print(
            f"[{i}/{len(splits)}] Order {ship.get('order_id', '?')}  "
            f"${parent.get('amount_dollars', '?')}  {parent.get('date', '?')}"
        )
        for j, sub in enumerate(split.get("subtransactions", []), start=1):
            item = sub.get("item", {}) or {}
            name = item.get("product_name", sub.get("memo", "?"))
            print(
                f"    {j}. ${sub.get('allocated_amount', '?'):>8}  "
                f"→ {sub.get('category_name') or '[UNCATEGORIZED]'}  "
                f"(conf {sub.get('confidence', 0):.2f})"
            )
            print(f"        {name[:90]}")

        choice = _prompt_choice("[a]ccept  [e]dit items  [s]kip  [q]uit", "aesq")
        if choice == "q":
            return "quit"
        if choice == "a":
            _mark_accepted(split)
        elif choice == "s":
            _mark_skipped(split)
        elif choice == "e":
            _edit_split_items(split, categories)
            print()
            print("  Items edited. What do you want to do with this split now?")
            followup = _prompt_choice("[a]ccept  [s]kip whole split  [q]uit", "asq")
            if followup == "q":
                return "quit"
            if followup == "s":
                _mark_skipped(split, reason="user-after-edit")
            else:
                _mark_accepted(split)
    return None


def _edit_split_items(split: dict, categories: list[dict]) -> None:
    """For each subtransaction in a split, ask whether to recategorize it."""
    subs = split.get("subtransactions", [])
    for j, sub in enumerate(subs, start=1):
        item = sub.get("item", {}) or {}
        name = item.get("product_name", sub.get("memo", "?"))[:80]
        print(f"    item {j}: {name}")
        print(f"      current: {sub.get('category_name') or '[UNCATEGORIZED]'}")
        choice = _prompt_choice("[k]eep  [r]ecategorize", "kr")
        if choice == "r":
            new_cat = _pick_category(categories, sub.get("category_name"))
            if new_cat is None:
                print("    cancelled; keeping")
                continue
            sub["category_id"] = new_cat["id"]
            sub["category_name"] = new_cat["name"]
            print(f"    → {new_cat['name']}")


def _bulk_accept_history(proposals: list[dict]) -> int:
    """Mark all unreviewed history-tier proposals as accepted. Returns count."""
    count = 0
    for p in proposals:
        if _has_review_decision(p):
            continue
        if p.get("tier") != "history":
            continue
        _mark_accepted(p)
        count += 1
    return count


def _partition_non_amazon(proposals: list[dict]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Return (claude_tier, fuzzy_tier, history_tier, other) lists.

    Lists are views into `proposals` (same dict references) — mutations in the
    walk persist to the underlying changeset.
    """
    claude, fuzzy, history, other = [], [], [], []
    for p in proposals:
        tier = p.get("tier")
        if tier == "claude":
            claude.append(p)
        elif tier == "fuzzy":
            fuzzy.append(p)
        elif tier == "history":
            history.append(p)
        else:
            other.append(p)
    return claude, fuzzy, history, other


def _print_summary(changeset: dict) -> None:
    splits = changeset["amazon"]["proposed_splits"]
    proposals = changeset["non_amazon"]["proposals"]

    decided = {"accept": 0, "recategorize": 0, "skip": 0}
    undecided = 0
    for entry in [*splits, *proposals]:
        review = entry.get("review")
        if review is None:
            undecided += 1
            continue
        d = review.get("decision")
        if d in decided:
            decided[d] += 1

    print()
    print("Review summary:")
    print(f"  accepted:       {decided['accept']}")
    print(f"  recategorized:  {decided['recategorize']}")
    print(f"  skipped:        {decided['skip']}")
    print(f"  undecided:      {undecided}")


def run_review(changeset_path: Path, *, no_browser: bool = False) -> int:
    load_dotenv()

    if not changeset_path.exists():
        print(f"error: changeset not found: {changeset_path}", file=sys.stderr)
        return 1

    sidecar_path = _reviewed_sidecar_path(changeset_path)
    source_path = sidecar_path if sidecar_path.exists() else changeset_path
    if source_path == sidecar_path:
        print(f"Resuming from existing reviewed sidecar: {sidecar_path}")
    changeset = _load_changeset(source_path)

    if not no_browser:
        try:
            html_path = render_and_open(changeset_path)
            print(f"Opened HTML view: {html_path}")
        except Exception as e:
            print(f"warning: could not render HTML view ({e}); continuing without it")

    token = os.environ.get("YNAB_API_TOKEN")
    if not token:
        print("error: YNAB_API_TOKEN not set in .env or environment", file=sys.stderr)
        return 1
    budget_id = changeset.get("metadata", {}).get("budget_id")
    if not budget_id:
        print("error: changeset metadata missing budget_id", file=sys.stderr)
        return 1

    print(f"Fetching categories for budget {budget_id}...")
    client = YNABClient(token=token)
    category_groups = client.get_categories(budget_id)
    categories = _flatten_categories(category_groups)
    print(f"  loaded {len(categories)} categories")

    splits = changeset["amazon"]["proposed_splits"]
    proposals = changeset["non_amazon"]["proposals"]
    claude, fuzzy, history, other = _partition_non_amazon(proposals)

    print()
    print("Plan:")
    print(f"  Amazon splits to walk:        {sum(1 for s in splits if not _has_review_decision(s))} / {len(splits)}")
    print(f"  Claude-tier to walk:          {sum(1 for p in claude if not _has_review_decision(p))} / {len(claude)}")
    print(f"  Fuzzy-tier to walk:           {sum(1 for p in fuzzy if not _has_review_decision(p))} / {len(fuzzy)}")
    print(f"  Other-tier to walk:           {sum(1 for p in other if not _has_review_decision(p))} / {len(other)}")
    print(f"  History-tier to bulk-accept:  {sum(1 for p in history if not _has_review_decision(p))} / {len(history)}")
    print()
    proceed = input("Proceed? [Y/n]: ").strip().lower()
    if proceed and proceed not in ("y", "yes"):
        print("aborted; no changes written")
        return 0

    for bucket_fn, bucket_args in (
        (_walk_amazon_splits, (splits, categories)),
        (_walk_non_amazon, (claude, "Claude-tier (novel payees)", categories)),
        (_walk_non_amazon, (fuzzy, "Fuzzy-tier (payee variants)", categories)),
        (_walk_non_amazon, (other, "Other-tier (amazon-wf, etc.)", categories)),
    ):
        result = bucket_fn(*bucket_args)
        if result == "quit":
            print("\nQuitting mid-review; partial state saved.")
            _save_reviewed(changeset, sidecar_path)
            print(f"Resume later with: uv run python review.py {changeset_path}")
            return 0

    print()
    n_history = sum(1 for p in history if not _has_review_decision(p))
    if n_history:
        total = sum(
            float(p.get("amount_dollars") or 0)
            for p in history
            if not _has_review_decision(p)
        )
        print(f"=== Bulk-accept history-tier ({n_history} proposals, ${total:,.2f} total) ===")
        choice = _prompt_choice("[a]ccept all  [w]alk individually  [q]uit", "awq")
        if choice == "q":
            _save_reviewed(changeset, sidecar_path)
            print(f"Saved partial state to {sidecar_path}")
            return 0
        if choice == "a":
            n = _bulk_accept_history(history)
            print(f"  accepted {n} history-tier proposals")
        elif choice == "w":
            result = _walk_non_amazon(history, "History-tier (walk-through)", categories)
            if result == "quit":
                _save_reviewed(changeset, sidecar_path)
                print(f"Saved partial state to {sidecar_path}")
                return 0

    _save_reviewed(changeset, sidecar_path)
    _print_summary(changeset)
    print()
    print(f"Reviewed changeset written: {sidecar_path}")
    print(f"Next: /confirm-apply (or `uv run python amazon_confirm.py {sidecar_path} --yes`)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Interactively review an enrich-changeset before applying."
    )
    parser.add_argument(
        "path",
        type=Path,
        nargs="?",
        help="Path to changeset .json. Defaults to newest in data/cache/.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Skip the HTML viewer launch.",
    )
    args = parser.parse_args()

    try:
        changeset_path = args.path if args.path else _newest_changeset_json()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return run_review(changeset_path, no_browser=args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
