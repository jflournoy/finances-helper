"""Interactive review of an enrich-changeset before /apply.

Walks the user through risky proposals (Amazon splits, fuzzy/Claude tier
non-Amazon proposals), bulk-accepts the safe ones (history tier), and writes
a sidecar `*-reviewed.json` that apply.py can operate on.

A skipped proposal is marked by setting `applied_at` to a sentinel string —
apply.py already short-circuits on that field, so no changes to the
applier are needed.

A recategorized proposal mutates `category_id` / `category_name` in place and
records the original under a `review` block on the proposal.

Resumable: if the sidecar exists, the next run picks up unreviewed items only.

Usage:
    uv run python decide.py [path-to-changeset.json]
"""
import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from thefuzz import process as fuzz_process

from amazon_matcher import extract_order_history_csv, parse_order_history
from categorizer import load_payee_cache, record_categorization, save_payee_cache
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
            "no enrich-changeset-*.json found in data/cache/. Run /tag first."
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


def _shipment_index_key(order_id: str, ship_date: str | None, amount_dollars: str) -> tuple:
    """Key for the near-miss item index.

    Order ID alone is not unique: one Amazon order routinely ships as two
    parcels with different tracking numbers, and the dump records each as a
    separate row group. Order ID + ship date is not unique either — those
    two parcels usually leave the warehouse the same day. Including the
    shipment total disambiguates them, and matches what the changeset
    already records per candidate (`amount_dollars`, written by tag.py with
    the same `format(Decimal, "f")`), so no changeset schema change is
    needed and changesets built by older runs still resolve.
    """
    return (order_id, ship_date, amount_dollars)


def _build_shipment_item_index(dump_path: str | None) -> tuple[dict, str | None]:
    """Parse an Amazon dump once into a {shipment key: [AmazonShipment]} index.

    Used to look up item-level detail for a near-miss candidate without
    re-parsing the (potentially 10k+ row) dump per transaction.

    Values are lists, not single shipments: if two shipments still collide
    on the key, both are kept so the caller can say "ambiguous" rather than
    display one shipment's items under another shipment's amount. Silently
    overwriting here put the wrong item list in front of a real-money
    categorization decision.

    Args:
        dump_path: changeset["metadata"]["dump_path"], as recorded when the
            changeset was built. May no longer exist (e.g. renamed/deleted).

    Returns:
        (index, warning). index is {} if dump_path is missing/unreadable —
        callers must handle a miss by falling back to order_id/ship_date-only
        display, not by silently trying a different dump (a re-downloaded
        dump can have different contents than what the changeset was built
        against, which would mislead a real-money categorization decision).
        warning is a user-facing string explaining why the index is empty,
        or None if it built successfully.
    """
    if not dump_path:
        return {}, "changeset has no recorded dump_path — showing order IDs only."

    path = Path(dump_path)
    if not path.exists():
        return {}, f"dump {dump_path} (recorded in this changeset) no longer exists — showing order IDs only."

    try:
        csv_text = extract_order_history_csv(path)
        shipments, _parse_errors = parse_order_history(csv_text)
    except Exception as e:
        return {}, f"could not parse dump {dump_path}: {e} — showing order IDs only."

    index: dict[tuple, list] = {}
    for s in shipments:
        key = _shipment_index_key(
            s.order_id,
            s.ship_date.isoformat() if s.ship_date else None,
            format(s.total_amount, "f"),
        )
        index.setdefault(key, []).append(s)
    return index, None


def _candidate_item_source(item_index: dict, candidate: dict) -> tuple[list, str | None]:
    """Shipments whose items belong to `candidate`, or a note saying why none.

    A whole-order candidate is resolved through its constituent parcels'
    keys, since the fused order has no row of its own in the dump. Any parcel
    that doesn't resolve to exactly one shipment aborts the whole list: a
    partial item list under a whole-order total reads as complete and would
    mislead a real-money categorization.

    Returns (shipments, note). A note is user-facing and means "items
    deliberately not shown"; an empty list with no note means the dump simply
    had nothing for this candidate (item_index_warning already explains why).
    """
    parcel_keys = candidate.get("parcel_keys")
    if parcel_keys:
        resolved = []
        for order_id, ship_date, amount in parcel_keys:
            hits = item_index.get(_shipment_index_key(order_id, ship_date, amount), [])
            if len(hits) != 1:
                what = "is ambiguous in" if hits else "is missing from"
                return [], (
                    f"parcel shipped {ship_date} (${amount}) {what} the dump — "
                    f"item detail is incomplete, not shown"
                )
            resolved.append(hits[0])
        return resolved, None

    hits = item_index.get(
        _shipment_index_key(
            candidate["order_id"], candidate["ship_date"], candidate["amount_dollars"]
        ),
        [],
    )
    if len(hits) > 1:
        return [], (
            f"{len(hits)} shipments in the dump share this order, ship date and "
            f"amount — item detail is ambiguous, not shown"
        )
    return hits, None


def _amazon_order_url(order_id: str) -> str:
    """Best-effort link to an Amazon order's detail page.

    Not load-bearing: Amazon's URL format has changed before and isn't
    verified here. Callers must also print the raw order_id so the user can
    fall back to pasting it into Amazon's own order search if this link is
    stale.
    """
    return f"https://www.amazon.com/gp/css/order-details?orderID={order_id}"


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


BOOST_STRENGTH_NORMAL = 10
BOOST_STRENGTH_STRONG = 20


def _maybe_prompt_boost(proposal: dict, *, enabled: bool) -> None:
    """Optionally stash a cache-boost intent on a freshly-accepted/recategorized
    proposal. Only meaningful for non-Amazon proposals whose decision pins a
    single category to a payee. Skip proposals never reach this code path.

    The intent is stashed on `proposal["review"]["boost"]`. Boosts are applied
    to the payee cache only at the end of a successful review (not on mid-review
    quit), so partial reviews never corrupt the cache.
    """
    if not enabled:
        return
    review = proposal.get("review")
    if review is None or review.get("decision") not in ("accept", "recategorize"):
        return
    cat_name = proposal.get("category_name") or "[UNCATEGORIZED]"
    payee = proposal.get("payee_name") or ""
    if not payee:
        return
    prompt = (
        f"  boost cache for {payee!r} -> {cat_name!r}? "
        f"[n=no / y=+{BOOST_STRENGTH_NORMAL} / s=+{BOOST_STRENGTH_STRONG} (lock-in)]: "
    )
    while True:
        resp = input(prompt).strip().lower()
        if resp in ("", "n", "no"):
            return
        if resp in ("y", "yes"):
            strength = BOOST_STRENGTH_NORMAL
            break
        if resp in ("s", "strong"):
            strength = BOOST_STRENGTH_STRONG
            break
        print("  invalid; expected n / y / s (or empty for no)")
    review["boost"] = {"strength": strength}


def _apply_boosts_to_cache(changeset: dict, *, cache_path: str | None = None) -> int:
    """Apply any pending boost intents from the changeset to the payee cache.

    Walks both Amazon splits and non-Amazon proposals for `review.boost`
    entries. (Amazon splits never get boost intents under current UX, but the
    walker is defensive.) Returns the number of boosts applied. Caller is
    responsible for displaying the count.
    """
    pending: list[tuple[str, str, str, int]] = []
    for p in changeset.get("non_amazon", {}).get("proposals", []):
        boost = (p.get("review") or {}).get("boost")
        if not boost:
            continue
        cat_id = p.get("category_id")
        cat_name = p.get("category_name")
        payee = p.get("payee_name")
        if not (cat_id and cat_name and payee):
            continue
        pending.append((payee, cat_id, cat_name, int(boost["strength"])))

    if not pending:
        return 0

    if cache_path is None:
        cache = load_payee_cache()
    else:
        cache = load_payee_cache(cache_path)
    for payee, cat_id, cat_name, strength in pending:
        record_categorization(
            cache, payee, cat_id, cat_name,
            source="user", prior_strength=strength,
        )
    if cache_path is None:
        save_payee_cache(cache)
    else:
        save_payee_cache(cache, cache_path)
    return len(pending)


def _walk_non_amazon(
    proposals: list[dict],
    label: str,
    categories: list[dict],
    *,
    boost_enabled: bool = False,
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
            _maybe_prompt_boost(p, enabled=boost_enabled)
        elif choice == "s":
            _mark_skipped(p)
        elif choice == "r":
            new_cat = _pick_category(categories, p.get("category_name"))
            if new_cat is None:
                print("  cancelled — leaving unreviewed; come back later")
                continue
            _mark_recategorized(p, new_cat)
            print(f"  → {new_cat['name']}")
            _maybe_prompt_boost(p, enabled=boost_enabled)
    return None


def _claimed_near_misses(changeset: dict) -> tuple[set[tuple], set[str]]:
    """What earlier near-miss picks have already spoken for, read from the changeset.

    A candidate never matches by amount (that's why it's in
    unmatched_shipments at all), so once a user picks it for one txn it would
    otherwise keep reappearing for every other in-window txn forever. Reading
    it back off the proposals makes that hold across a resume, not just
    within one run.

    Two grains, because Amazon captures per shipment:
      - picking one parcel claims that parcel only. Its siblings shipped on
        other days may have been billed separately, making them another
        charge's near miss.
      - picking a whole-order candidate claims the entire order, siblings
        included — that one charge is the whole order.

    Returns (claimed_shipment_keys, claimed_order_ids). Proposals written
    before shipment keys were recorded carry only `near_miss_order_id`; those
    claim the whole order, which is what they meant at the time.
    """
    claimed_keys: set[tuple] = set()
    claimed_orders: set[str] = set()
    for p in changeset["non_amazon"]["proposals"]:
        if p.get("tier") != "near-miss":
            continue
        key = p.get("near_miss_shipment_key")
        if key and p.get("near_miss_parcel_count", 1) == 1:
            claimed_keys.add(tuple(key))
        elif p.get("near_miss_order_id"):
            claimed_orders.add(p["near_miss_order_id"])
    return claimed_keys, claimed_orders


def _candidate_is_claimed(candidate: dict, claimed_keys: set[tuple], claimed_orders: set[str]) -> bool:
    """True if an earlier pick already accounts for this candidate.

    A whole-order candidate is also dead once any one of its parcels is
    claimed: its total includes that parcel, so offering it again would let
    the same dollars be spent twice.
    """
    order_id = candidate["order_id"]
    if order_id in claimed_orders:
        return True
    if _shipment_index_key(order_id, candidate["ship_date"], candidate["amount_dollars"]) in claimed_keys:
        return True
    if candidate.get("parcels", 1) > 1:
        return any(k[0] == order_id for k in claimed_keys)
    return False


def _near_miss_rationale(candidate: dict) -> str:
    """Audit line for a user-confirmed near miss, naming what was actually picked."""
    parcels = candidate.get("parcels", 1)
    if parcels > 1:
        return (
            f"user-confirmed near-miss match to Amazon order {candidate['order_id']} "
            f"(whole order, {parcels} parcels, ${candidate['amount_dollars']})"
        )
    return (
        f"user-confirmed near-miss match to Amazon order {candidate['order_id']} "
        f"(parcel shipped {candidate['ship_date']}, ${candidate['amount_dollars']})"
    )


def _walk_unmatched_near_misses(
    changeset: dict,
    categories: list[dict],
    item_index: dict,
    item_index_warning: str | None,
) -> str | None:
    """Walk unmatched Amazon txns that have near-miss shipment candidates.

    Only entries with a non-empty candidate_shipments list are shown — an
    unmatched txn with no candidate in the date window has nothing useful to
    offer here. For each: list every candidate (shipped within the matcher's
    date window, not Whole Foods, amount deliberately not used to narrow or
    rank them — see tag.py's _candidate_unmatched_shipments) with its items
    (if the dump could be parsed), and let the user open candidates in
    Amazon (repeatedly — opening doesn't consume the turn), choose one to
    categorize the txn against (written into non_amazon.proposals so
    apply.py's existing flat-proposal path PATCHes it), or skip.

    A shipment claimed for one txn is removed from every other txn's
    candidate list for the rest of this walk — it can't be two people's
    near-miss. Skipped entries get a `review` block (same shape as the other
    walks) so resume doesn't re-prompt for them. Returns 'quit' if the user
    quits, else None.
    """
    unmatched = changeset["amazon"]["unmatched_ynab"]
    entries = [u for u in unmatched if u.get("candidate_shipments") and not _has_review_decision(u)]
    if not entries:
        return None

    claimed_keys, claimed_orders = _claimed_near_misses(changeset)

    print()
    print(f"=== Unmatched Amazon txns with near-miss candidates ({len(entries)}) ===")
    if item_index_warning:
        print(f"  Note: {item_index_warning}")

    for i, u in enumerate(entries, start=1):
        candidates = [
            c for c in u["candidate_shipments"]
            if not _candidate_is_claimed(c, claimed_keys, claimed_orders)
        ]
        if not candidates:
            continue

        print()
        print(f"[{i}/{len(entries)}] {u['payee_name']}  ${u['amount_dollars']}  {u['date']}")
        for j, c in enumerate(candidates, start=1):
            parcels = c.get("parcels", 1)
            label = f" [whole order, {parcels} parcels]" if parcels > 1 else ""
            print(
                f"  {j}. order {c['order_id']}{label}, shipped {c['ship_date']}, "
                f"${c['amount_dollars']} (Δ${c['amount_delta_dollars']}, Δ{c['date_delta_days']}d)"
            )
            sources, note = _candidate_item_source(item_index, c)
            for shipment in sources:
                for item in shipment.items:
                    print(f"       - {item.product_name}")
            if note:
                print(f"       ! {note}")

        while True:
            choice = _prompt_choice("[o]pen a candidate  [c]hoose candidate  [s]kip  [q]uit", "ocsq")
            if choice == "q":
                return "quit"
            if choice == "o":
                pick = _prompt_choice(
                    f"  which candidate to open (1-{len(candidates)})",
                    "".join(str(n) for n in range(1, len(candidates) + 1)),
                )
                webbrowser.open(_amazon_order_url(candidates[int(pick) - 1]["order_id"]))
                continue
            break
        if choice == "s":
            _mark_skipped(u)
            continue
        if choice == "c":
            if len(candidates) == 1:
                chosen = candidates[0]
            else:
                pick = _prompt_choice(
                    f"  which candidate (1-{len(candidates)})",
                    "".join(str(n) for n in range(1, len(candidates) + 1)),
                )
                chosen = candidates[int(pick) - 1]
            new_cat = _pick_category(categories, None)
            if new_cat is None:
                print("  cancelled — leaving unreviewed; come back later")
                continue
            proposal = {
                "transaction_id": u["transaction_id"],
                "payee_name": u["payee_name"],
                "amount_dollars": u["amount_dollars"],
                "date": u["date"],
                "category_id": new_cat["id"],
                "category_name": new_cat["name"],
                "tier": "near-miss",
                "confidence": 1.0,
                "rationale": _near_miss_rationale(chosen),
                "near_miss_order_id": chosen["order_id"],
                "near_miss_shipment_key": list(
                    _shipment_index_key(
                        chosen["order_id"], chosen["ship_date"], chosen["amount_dollars"]
                    )
                ),
                "near_miss_parcel_count": chosen.get("parcels", 1),
                "prior_strength": None,
            }
            _mark_accepted(proposal)
            changeset["non_amazon"]["proposals"].append(proposal)
            unmatched.remove(u)
            if chosen.get("parcels", 1) > 1:
                claimed_orders.add(chosen["order_id"])
            else:
                claimed_keys.add(
                    _shipment_index_key(
                        chosen["order_id"], chosen["ship_date"], chosen["amount_dollars"]
                    )
                )
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
            # Stash the original so apply-time learning can detect this as a
            # rejection of the proposed category (mirrors _mark_recategorized for
            # payee-level proposals). Without this, the override is invisible to
            # the category-profile refresh loop.
            sub["original_category_id"] = sub.get("category_id")
            sub["original_category_name"] = sub.get("category_name")
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


def run_review(
    changeset_path: Path,
    *,
    no_browser: bool = False,
    boost_enabled: bool = True,
) -> int:
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
    unmatched = changeset["amazon"]["unmatched_ynab"]
    near_miss_count = sum(
        1 for u in unmatched if u.get("candidate_shipments") and not _has_review_decision(u)
    )

    print()
    print("Plan:")
    print(f"  Amazon splits to walk:        {sum(1 for s in splits if not _has_review_decision(s))} / {len(splits)}")
    print(f"  Claude-tier to walk:          {sum(1 for p in claude if not _has_review_decision(p))} / {len(claude)}")
    print(f"  Fuzzy-tier to walk:           {sum(1 for p in fuzzy if not _has_review_decision(p))} / {len(fuzzy)}")
    print(f"  Other-tier to walk:           {sum(1 for p in other if not _has_review_decision(p))} / {len(other)}")
    print(f"  History-tier to bulk-accept:  {sum(1 for p in history if not _has_review_decision(p))} / {len(history)}")
    print(f"  Unmatched near-misses to try: {near_miss_count} / {len(unmatched)}")
    print()
    proceed = input("Proceed? [Y/n]: ").strip().lower()
    if proceed and proceed not in ("y", "yes"):
        print("aborted; no changes written")
        return 0

    walks = (
        (_walk_amazon_splits, (splits, categories), {}),
        (_walk_non_amazon, (claude, "Claude-tier (novel payees)", categories),
            {"boost_enabled": boost_enabled}),
        (_walk_non_amazon, (fuzzy, "Fuzzy-tier (payee variants)", categories),
            {"boost_enabled": boost_enabled}),
        (_walk_non_amazon, (other, "Other-tier (amazon-wf, etc.)", categories),
            {"boost_enabled": boost_enabled}),
    )
    for bucket_fn, bucket_args, bucket_kwargs in walks:
        result = bucket_fn(*bucket_args, **bucket_kwargs)
        if result == "quit":
            print("\nQuitting mid-review; partial state saved.")
            _save_reviewed(changeset, sidecar_path)
            print(f"Resume later with: uv run python decide.py {changeset_path}")
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
            result = _walk_non_amazon(
                history, "History-tier (walk-through)", categories,
                boost_enabled=boost_enabled,
            )
            if result == "quit":
                _save_reviewed(changeset, sidecar_path)
                print(f"Saved partial state to {sidecar_path}")
                return 0

    if near_miss_count:
        print()
        print(f"{near_miss_count} unmatched Amazon txn(s) have a near-miss shipment candidate.")
        choice = _prompt_choice("Try to match near misses?  [y]es  [n]o  [q]uit", "ynq")
        if choice == "q":
            _save_reviewed(changeset, sidecar_path)
            print(f"Saved partial state to {sidecar_path}")
            return 0
        if choice == "y":
            item_index, item_index_warning = _build_shipment_item_index(
                changeset.get("metadata", {}).get("dump_path")
            )
            result = _walk_unmatched_near_misses(changeset, categories, item_index, item_index_warning)
            if result == "quit":
                _save_reviewed(changeset, sidecar_path)
                print(f"Saved partial state to {sidecar_path}")
                return 0

    n_boosts = _apply_boosts_to_cache(changeset)
    if n_boosts:
        print(f"Applied {n_boosts} cache boost{'s' if n_boosts != 1 else ''} "
              f"to data/cache/payee_lookup.json")

    _save_reviewed(changeset, sidecar_path)
    _print_summary(changeset)
    print()
    print(f"Reviewed changeset written: {sidecar_path}")
    print(f"Next: /apply (or `uv run python apply.py {sidecar_path} --yes`)")
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
    parser.add_argument(
        "--no-boost",
        action="store_true",
        help="Suppress the cache-boost prompt after accept/recategorize.",
    )
    args = parser.parse_args()

    try:
        changeset_path = args.path if args.path else _newest_changeset_json()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    return run_review(
        changeset_path,
        no_browser=args.no_browser,
        boost_enabled=not args.no_boost,
    )


if __name__ == "__main__":
    sys.exit(main())
