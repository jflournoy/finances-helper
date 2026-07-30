"""Learned category profiles for item-level Amazon categorization.

Problem this solves
-------------------
When Claude categorizes individual Amazon items, it historically saw only the
*names* of the user's YNAB categories (e.g. "Computer", "Home Goods"). With no
sense of what those categories mean in this particular budget, it reasons from
the generic English meaning of the words: "USB cable" sounds computer-ish, so it
lands in "Computer" — when in this budget accessories belong in "Home Goods" and
"Computer" is reserved for core components.

A category profile teaches Claude what each category *means* here, at two levels:

1. **Bootstrap descriptions** — for each category, we already know which
   *merchants* the user files under it (from the payee frequency cache). We ask
   Claude once to summarize "what kinds of purchases is this?" and cache the
   one-line description.

2. **Item exemplars** — as Amazon splits are confirmed, we accumulate concrete
   (item -> category) examples with frequency counts. The dominant category wins
   (recent confirmations break ties), mirroring the payee frequency cache. These
   exemplars are injected alongside the description so Claude learns the boundary
   ("USB cable -> Home Goods, GPU -> Computer").

Refresh policy: descriptions are an explicit, expensive asset. They are built
once at setup, then regenerated only when a category is *stale* — it has no
description yet, or a rejection flagged it dirty (the user overrode Claude's
choice for that category). Agreeing confirmations never trigger a refresh.

This module follows the same conventions as categorizer.py's payee cache:
versioned JSON, loud errors, NO silent fallbacks.
"""
import re
import json
import logging
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

PROFILES_VERSION = 1
DEFAULT_PROFILES_PATH = "data/cache/category_profiles.json"

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _is_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value))

# How many exemplars per category to surface in the prompt (highest count first).
MAX_EXEMPLARS_PER_CATEGORY = 8

# Model used for the cheap bootstrap/refresh summarization call.
_BOOTSTRAP_MODEL = "claude-haiku-4-5-20251001"

# Max categories per bootstrap Claude call. Asking Haiku for a single JSON array
# covering many categories makes it likely to silently drop a few entries (not a
# truncation — the model just emits an incomplete list). Batching keeps each
# array small enough that every requested category reliably comes back.
_BOOTSTRAP_BATCH_SIZE = 20


# ---------------------------------------------------------------------------
# Item name normalization
# ---------------------------------------------------------------------------

# Trailing pack/quantity noise that fragments otherwise-identical product names.
_PACK_NOISE_RE = re.compile(
    r"\s*\((?:pack of|set of|count of|qty)\s*\d+\)\s*$",
    re.IGNORECASE,
)


def normalize_item_name(name: str) -> str:
    """Normalize an Amazon product name into a stable exemplar key.

    Lowercases, collapses whitespace, and strips trailing pack/quantity noise so
    that "AmazonBasics USB Cable (Pack of 2)" and "(Pack of 6)" share one key.

    Raises ValueError if the name is None, empty, or whitespace-only.
    """
    if name is None:
        raise ValueError("Item name cannot be None")

    name = name.strip()
    if not name:
        raise ValueError("Item name cannot be empty or whitespace-only")

    name = name.lower()
    name = _PACK_NOISE_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        raise ValueError("Item name is empty after normalization")

    return name


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def _empty_profiles() -> dict:
    return {"_version": PROFILES_VERSION, "categories": {}, "_backfilled": {}}


def load_profiles(path: str = DEFAULT_PROFILES_PATH) -> dict:
    """Load the category profile store from disk.

    Returns an empty versioned store if the file doesn't exist (first-run case).

    Raises ValueError if the file exists but contains invalid JSON.
    """
    profiles_path = Path(path)
    if not profiles_path.exists():
        return _empty_profiles()

    try:
        profiles = json.loads(profiles_path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in category profiles file {path}: {e}")

    if profiles.get("_version") != PROFILES_VERSION:
        raise ValueError(
            f"Unsupported category profiles version {profiles.get('_version')!r} "
            f"in {path} (expected {PROFILES_VERSION})"
        )
    profiles.setdefault("categories", {})
    # Ledger of already-ingested history subtransactions (see
    # backfill_from_ynab_subtransactions). Absent in stores written before
    # backfill became idempotent.
    profiles.setdefault("_backfilled", {})
    return profiles


def save_profiles(profiles: dict, path: str = DEFAULT_PROFILES_PATH) -> None:
    """Save the category profile store to disk."""
    profiles_path = Path(path)
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    profiles_path.write_text(json.dumps(profiles, indent=2, sort_keys=True))


def _ensure_category(profiles: dict, category_id: str, category_name: str) -> dict:
    """Return the profile entry for category_id, creating it if absent."""
    cats = profiles["categories"]
    if category_id not in cats:
        cats[category_id] = {
            "name": category_name,
            "description": "",
            "merchants": [],
            "item_exemplars": {},
            "corrections": [],
            "dirty": False,
        }
    elif category_name and not cats[category_id].get("name"):
        cats[category_id]["name"] = category_name
    return cats[category_id]


# ---------------------------------------------------------------------------
# Item exemplar learning (frequency-weighted, dominant wins, recent breaks ties)
# ---------------------------------------------------------------------------

def record_item_categorization(
    profiles: dict,
    product_name: str,
    category_id: str,
    category_name: str,
) -> None:
    """Record one confirmed (item -> category) exemplar.

    Adds a frequency count under the normalized item name, mirroring the payee
    frequency cache. This is pure data that keeps dominant_item_category accurate;
    it does NOT mark the category stale. Agreement is not a refresh signal —
    descriptions are regenerated only when the user *rejects* a categorization
    (see record_rejection). A confirmation that matched what Claude proposed tells
    us the description is already working.

    The exemplar count lives on the source category and is globally resolvable
    via dominant_item_category.

    Modifies `profiles` in place.
    """
    norm = normalize_item_name(product_name)
    cat = _ensure_category(profiles, category_id, category_name)

    exemplars = cat["item_exemplars"]
    if norm not in exemplars:
        exemplars[norm] = {}
    if category_id not in exemplars[norm]:
        exemplars[norm][category_id] = {"name": category_name, "count": 0}
    exemplars[norm][category_id]["count"] += 1


def _all_exemplar_counts(profiles: dict, norm_item: str) -> dict:
    """Aggregate counts for one item across every category that has it.

    Item exemplars are stored under the category they were filed in, so the same
    item can appear under multiple categories. This merges them into a single
    {category_id: {"name", "count"}} view for dominance resolution.
    """
    merged: dict = {}
    for cat in profiles["categories"].values():
        entry = cat.get("item_exemplars", {}).get(norm_item)
        if not entry:
            continue
        for cat_id, info in entry.items():
            if cat_id not in merged:
                merged[cat_id] = {"name": info["name"], "count": 0}
            merged[cat_id]["count"] += info["count"]
    return merged


def dominant_item_category(profiles: dict, product_name: str) -> tuple:
    """Return (category_id, category_name) for an item's dominant category.

    Highest total count wins; ties broken by alphabetical category_id (matching
    _dominant_category in categorizer.py — deterministic, recent confirmations
    push the count up so the latest consistent signal dominates).

    Returns (None, None) if the item has never been seen.
    """
    norm = normalize_item_name(product_name)
    merged = _all_exemplar_counts(profiles, norm)
    if not merged:
        return (None, None)

    max_count = -1
    dom_id = None
    dom_name = None
    for cat_id, info in sorted(merged.items()):
        if info["count"] > max_count:
            max_count = info["count"]
            dom_id = cat_id
            dom_name = info["name"]
    return (dom_id, dom_name)


def _backfill_key(txn: dict, sub: dict, memo: str) -> str:
    """Stable identity for one already-ingested historical subtransaction.

    Keyed on the YNAB subtransaction id when present (the real identity), falling
    back to parent txn id + memo for fixture/legacy rows that carry no sub id.

    The category is deliberately NOT part of the key: one subtransaction is one
    observation whose category can change. The recorded category is stored as the
    ledger's value so a recategorization in YNAB is detected as a *revision* —
    the stale count is retracted rather than left to compete with the new one.
    """
    sub_id = sub.get("id") or f"{txn.get('id')}:{memo}"
    return str(sub_id)


def _retract_item_categorization(
    profiles: dict, product_name: str, category_id: str
) -> None:
    """Undo one previously recorded (item -> category) exemplar observation.

    Used when a history subtransaction is re-read with a different category than
    it was ingested under: the old observation is no longer true and must not
    linger as a competing vote. Prunes emptied structures so a corrected item
    leaves no zero-count residue.
    """
    norm = normalize_item_name(product_name)
    for cat in profiles.get("categories", {}).values():
        entry = cat.get("item_exemplars", {}).get(norm)
        if not entry or category_id not in entry:
            continue
        entry[category_id]["count"] -= 1
        if entry[category_id]["count"] <= 0:
            del entry[category_id]
        if not entry:
            del cat["item_exemplars"][norm]


def backfill_from_ynab_subtransactions(profiles: dict, transactions: list) -> int:
    """Seed item exemplars from already-split Amazon transactions in YNAB history.

    For each Amazon transaction that has been split, every subtransaction with a
    category and a memo (the memo holds the item description this tool writes)
    becomes one (item -> category) exemplar. These are weak priors: each counts
    once, so a handful of fresh confirmations outvote a stale historical split,
    consistent with the "frequency-weighted, latest wins ties" policy.

    IDEMPOTENT: build_profiles() runs this on every tag.py invocation over the
    same YNAB history, so each ingested subtransaction is remembered in
    profiles["_backfilled"] and re-observing it records nothing. Without this the
    counts compound once per run — the real store reached count=42 for backfilled
    items while genuinely confirmed splits sat at 1, exactly inverting the
    weak-prior invariant above and letting stale history outvote the user's own
    corrections.

    Two SEPARATE historical splits of the same item remain two observations; only
    re-reading the SAME subtransaction is suppressed. Recategorizing a split in
    YNAB changes its key, so the corrected category is picked up.

    Non-Amazon transactions and subtransactions missing a category or memo are
    skipped.

    Returns the number of exemplars newly recorded. Modifies `profiles` in place.
    """
    # Imported lazily to avoid a hard import cycle at module load.
    from amazon_matcher import is_amazon_payee

    seen = profiles.setdefault("_backfilled", {})

    recorded = 0
    for txn in transactions:
        if not is_amazon_payee(txn.get("payee_name")):
            continue
        for sub in txn.get("subtransactions") or []:
            cat_id = sub.get("category_id")
            cat_name = sub.get("category_name")
            memo = (sub.get("memo") or "").strip()
            if not cat_id or not memo:
                continue
            key = _backfill_key(txn, sub, memo)
            prior_cat_id = seen.get(key)
            if prior_cat_id == cat_id:
                continue
            if prior_cat_id is not None:
                # Same row, different category: the user recategorized this split
                # in YNAB. Retract the stale vote before recording the new one.
                _retract_item_categorization(profiles, memo, prior_cat_id)
            record_item_categorization(profiles, memo, cat_id, cat_name or "")
            seen[key] = cat_id
            recorded += 1
    return recorded


def record_confirmed_splits(profiles: dict, applied_splits: list) -> int:
    """Learn item exemplars from Amazon splits the user actually applied to YNAB.

    This is the strongest signal available: each subtransaction here was reviewed
    and written to a real budget, so it carries full frequency weight. As these
    accumulate they outvote weaker historical/bootstrap priors — this is how a
    user correction ("USB cable belongs in Home Goods, not Computer") propagates
    into future categorization.

    Args:
        applied_splits: Changeset proposed_split dicts that were applied in this
                       run (each with a "subtransactions" list whose entries carry
                       item.product_name, category_id, category_name).

    Subtransactions without a category_id are skipped (an uncategorized split is
    never applied, but we guard defensively).

    Returns the number of exemplars recorded. Modifies `profiles` in place.
    """
    recorded = 0
    for split in applied_splits:
        for sub in split.get("subtransactions") or []:
            cat_id = sub.get("category_id")
            if not cat_id:
                continue
            item = sub.get("item") or {}
            product_name = (item.get("product_name") or "").strip()
            if not product_name:
                continue
            record_item_categorization(
                profiles, product_name, cat_id, sub.get("category_name") or ""
            )
            recorded += 1
    return recorded


def _format_correction(viewing_cat_id: str, corr: dict) -> str:
    """Render one stored correction from the perspective of the category viewing it.

    Each correction records subject, from_category (over-claimed) and to_category
    (the user's choice). A category sees it as either "I wrongly claimed X" or
    "X actually belongs to me, was mis-sent to Y".
    """
    subject = corr.get("subject", "?")
    if viewing_cat_id == corr.get("from_category_id"):
        return f'"{subject}" does NOT belong here — user moved it to "{corr.get("to_category_name", "?")}"'
    if viewing_cat_id == corr.get("to_category_id"):
        return f'"{subject}" DOES belong here — was mis-categorized as "{corr.get("from_category_name", "?")}"'
    return ""


def record_rejection(
    profiles: dict,
    subject: str,
    from_category_id: str,
    from_category_name: str,
    to_category_id: str,
    to_category_name: str,
) -> None:
    """Record that the user overrode an automated categorization.

    The user moved `subject` from from_category (what the categorizer guessed)
    to to_category (where it belongs). This is the ONLY signal that flags a
    description for regeneration: both categories got the boundary wrong — the
    'from' category over-claimed, the 'to' category should have claimed it — so
    both are marked dirty and both store the correction for the next refresh.

    A no-op move (from == to) is ignored. Modifies `profiles` in place.

    Raises ValueError if either non-null category_id is not a YNAB UUID. Claude's
    flat categorizer has historically echoed the category NAME into the id field
    (fixed in categorizer.claude_categorize); an old applied changeset can still
    carry that bad id in original_category_id. Without this check it would get
    silently persisted as a bogus name-keyed category entry (NO SILENT FALLBACK).
    """
    if from_category_id == to_category_id:
        return

    for cat_id in (from_category_id, to_category_id):
        if cat_id and not _is_uuid(cat_id):
            raise ValueError(
                f"record_rejection: category_id {cat_id!r} is not a UUID — "
                f"likely a stale pre-fix changeset that stored the category name "
                f"instead of its id; refusing to persist"
            )

    correction = {
        "subject": subject,
        "from_category_id": from_category_id,
        "from_category_name": from_category_name,
        "to_category_id": to_category_id,
        "to_category_name": to_category_name,
    }

    for cat_id, cat_name in (
        (from_category_id, from_category_name),
        (to_category_id, to_category_name),
    ):
        if not cat_id:
            continue
        cat = _ensure_category(profiles, cat_id, cat_name or "")
        cat.setdefault("corrections", []).append(correction)
        cat["dirty"] = True


def record_rejections_from_applied(
    profiles: dict,
    applied_non_amazon: list,
    applied_amazon_splits: list,
) -> int:
    """Extract and record category rejections from proposals applied this run.

    A rejection is the user overriding an automated categorization during review:

    - Non-Amazon proposal: review.decision == "recategorize" AND the proposal's
      tier was "claude" (only Claude-tier overrides signal a category-MEANING
      error; history/fuzzy overrides are lookup/match errors, not meaning).
      Subject = payee_name.
    - Amazon subtransaction: carries original_category_id (stashed by review when
      the user recategorized the item). Item categorization is always Claude-tier.
      Subject = item.product_name.

    Returns the number of rejections recorded. Modifies `profiles` in place.
    """
    recorded = 0

    for proposal in applied_non_amazon:
        review = proposal.get("review") or {}
        if review.get("decision") != "recategorize":
            continue
        if proposal.get("tier") != "claude":
            continue
        orig_id = review.get("original_category_id")
        final_id = proposal.get("category_id")
        if not orig_id or not final_id or orig_id == final_id:
            continue
        record_rejection(
            profiles,
            proposal.get("payee_name") or "?",
            orig_id, review.get("original_category_name") or "",
            final_id, proposal.get("category_name") or "",
        )
        recorded += 1

    for split in applied_amazon_splits:
        for sub in split.get("subtransactions") or []:
            orig_id = sub.get("original_category_id")
            final_id = sub.get("category_id")
            if not orig_id or not final_id or orig_id == final_id:
                continue
            item = sub.get("item") or {}
            subject = (item.get("product_name") or sub.get("memo") or "?")
            record_rejection(
                profiles,
                subject,
                orig_id, sub.get("original_category_name") or "",
                final_id, sub.get("category_name") or "",
            )
            recorded += 1

    return recorded


def mark_dirty(profiles: dict, category_id: str) -> None:
    """Flag a category's description as needing regeneration."""
    cat = profiles["categories"].get(category_id)
    if cat is None:
        raise ValueError(f"mark_dirty: unknown category_id {category_id!r}")
    cat["dirty"] = True


def is_stale(cat: dict) -> bool:
    """True if a category's description should be (re)generated.

    A category is stale when it has no description yet, or it has been flagged
    dirty by a rejection (the user overrode Claude's choice involving this
    category). Volume of agreeing confirmations is NOT a staleness signal —
    descriptions refresh on divergence, not on use.
    """
    if not cat.get("description"):
        return True
    return bool(cat.get("dirty"))


def count_stale(profiles: dict) -> tuple:
    """Count categories whose descriptions are out of date, split by reason.

    Returns (n_undescribed, n_dirty):
    - n_undescribed: categories with no learned description yet (never built, or
      no merchant history to bootstrap from). These fall back to bare names.
    - n_dirty: categories that HAVE a description but were flagged by a rejection
      (the user overrode Claude). Their description no longer matches the user's
      decisions until refreshed.

    A category counts in at most one bucket (undescribed takes precedence).
    Used to nudge the user toward --refresh-profiles when corrections pile up.
    """
    n_undescribed = 0
    n_dirty = 0
    for cat in profiles.get("categories", {}).values():
        if not cat.get("description"):
            n_undescribed += 1
        elif cat.get("dirty"):
            n_dirty += 1
    return (n_undescribed, n_dirty)


# ---------------------------------------------------------------------------
# Merchant map — derive per-category merchants from the payee frequency cache
# ---------------------------------------------------------------------------

def build_merchant_map_from_cache(payee_cache: dict) -> dict:
    """Group payees by their dominant category from the payee frequency cache.

    Returns {category_id: {"name": str, "merchants": [payee, ...]}}.

    Alias entries (those with 'alias_of') and the cache's metadata keys are
    skipped. Merchants are sorted by descending observation count so the most
    representative ones lead.

    Raises ValueError if any category_id in the cache is not a UUID. Claude's
    flat categorizer has historically echoed the category NAME into the id
    field (fixed in categorizer.claude_categorize); a payee_lookup.json built
    before that fix can still carry the bad id. Silently syncing it here would
    re-create a bogus name-keyed category profile on every rebuild (NO SILENT
    FALLBACK) — run scripts/repair_payee_lookup_bogus_ids.py to clean the cache.
    """
    by_category: dict = {}
    scored: dict = {}  # category_id -> list[(count, payee)]

    for key, entry in payee_cache.items():
        if key.startswith("_"):
            continue
        if not isinstance(entry, dict) or "alias_of" in entry:
            continue
        categories = entry.get("categories")
        if not categories:
            continue

        # Dominant category for this payee (highest count, alpha tiebreak).
        max_count = -1
        dom_id = None
        dom_name = None
        for cat_id, info in sorted(categories.items()):
            if not _is_uuid(cat_id):
                raise ValueError(
                    f"build_merchant_map_from_cache: payee {key!r} has non-UUID "
                    f"category_id {cat_id!r} in payee_lookup.json — likely a "
                    f"stale pre-fix entry that stored the category name instead "
                    f"of its id; refusing to sync"
                )
            if info["count"] > max_count:
                max_count = info["count"]
                dom_id = cat_id
                dom_name = info["name"]
        if dom_id is None:
            continue

        if dom_id not in by_category:
            by_category[dom_id] = {"name": dom_name, "merchants": []}
            scored[dom_id] = []
        scored[dom_id].append((max_count, key))

    for cat_id, pairs in scored.items():
        pairs.sort(key=lambda p: (-p[0], p[1]))
        by_category[cat_id]["merchants"] = [payee for _, payee in pairs]

    return by_category


def sync_merchants_into_profiles(profiles: dict, merchant_map: dict) -> None:
    """Refresh each category's merchant list from a merchant map.

    Creates category entries that don't exist yet. A category whose merchant set
    changes is marked dirty so its description gets refreshed.

    Modifies `profiles` in place.
    """
    for cat_id, info in merchant_map.items():
        cat = _ensure_category(profiles, cat_id, info["name"])
        new_merchants = info["merchants"]
        if cat.get("merchants") != new_merchants:
            cat["merchants"] = new_merchants
            cat["dirty"] = True


# ---------------------------------------------------------------------------
# Bootstrap / regenerate descriptions via Claude
# ---------------------------------------------------------------------------

def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.index("\n")
        stripped = stripped[first_newline + 1:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    return stripped


def bootstrap_descriptions(profiles: dict, category_ids: list, api_key: str) -> None:
    """Generate one-line descriptions for the given categories via Claude.

    For each target category we feed Claude the merchants the user files under it
    (and any item exemplars) and ask what kinds of purchases it represents. The
    returned descriptions are written back and the categories' dirty flags /
    counters reset.

    Categories are processed in batches of at most ``_BOOTSTRAP_BATCH_SIZE`` per
    Claude call; a single huge array prompts Haiku to silently drop entries.

    No-op (no API call) if `category_ids` is empty.

    Raises ValueError on a malformed response: bad JSON, truncation, an unknown
    category_id, or a missing description (NO silent fallback).
    """
    if not category_ids:
        return

    for start in range(0, len(category_ids), _BOOTSTRAP_BATCH_SIZE):
        batch = category_ids[start : start + _BOOTSTRAP_BATCH_SIZE]
        _bootstrap_descriptions_batch(profiles, batch, api_key)


def _bootstrap_descriptions_batch(profiles: dict, category_ids: list, api_key: str) -> None:
    """Generate descriptions for one batch of categories in a single Claude call.

    Validates that every requested category comes back with a non-empty
    description and that no unexpected category_id appears (NO silent fallback).
    """
    if not category_ids:
        return

    requested = set(category_ids)
    lines = []
    for cat_id in category_ids:
        cat = profiles["categories"].get(cat_id)
        if cat is None:
            raise ValueError(f"bootstrap_descriptions: unknown category_id {cat_id!r}")
        merchants = ", ".join(cat.get("merchants", [])[:20]) or "(no known merchants)"
        exemplar_names = list(cat.get("item_exemplars", {}).keys())[:15]
        exemplars = ", ".join(exemplar_names) if exemplar_names else "(none yet)"
        entry = (
            f'- category_id "{cat_id}" | name "{cat["name"]}"\n'
            f"    merchants: {merchants}\n"
            f"    example items: {exemplars}"
        )
        corrections = cat.get("corrections", [])
        if corrections:
            # Surface the user's recent overrides so the new description fixes
            # the exact boundary that was getting mis-drawn.
            corr_lines = [_format_correction(cat_id, c) for c in corrections[-10:]]
            entry += "\n    user corrections:\n" + "\n".join(
                f"      - {line}" for line in corr_lines if line
            )
        lines.append(entry)
    catalog = "\n".join(lines)

    system_prompt = """You summarize what a personal-budget spending category MEANS,
based on the merchants and example purchases the user files under it.

For each category, write ONE concise sentence (max ~20 words) describing the
kinds of purchases that belong in it — concrete enough to disambiguate similar
categories (e.g. distinguish "Computer" core hardware from "Home Goods"
accessories). Do not just restate the category name.

Some categories include "user corrections": cases where an automated
categorizer guessed this category but the user moved the item elsewhere (or
moved it INTO this category from elsewhere). Treat these as authoritative
boundary corrections — the new description MUST be consistent with them.

Respond with ONLY a JSON array, one object per category, in any order:
[{"category_id": "<id>", "description": "<one sentence>"}]"""

    user_message = (
        "Summarize each of these budget categories:\n\n" + catalog
    )

    max_tokens = max(1024, len(category_ids) * 120)
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=_BOOTSTRAP_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    if not response.content:
        raise ValueError(
            f"Claude returned empty content for category descriptions. "
            f"stop_reason={response.stop_reason}"
        )
    text = response.content[0].text or ""
    if not text.strip():
        raise ValueError(
            f"Claude returned empty text for category descriptions. "
            f"stop_reason={response.stop_reason}"
        )
    if response.stop_reason == "max_tokens":
        raise ValueError(
            f"Claude category-description response truncated (max_tokens={max_tokens}, "
            f"categories={len(category_ids)}). Last 200 chars: ...{text[-200:]}"
        )

    text = _strip_code_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Claude returned unparseable category-description JSON: {e}. "
            f"First 500 chars: {text[:500]}"
        )
    if not isinstance(data, list):
        raise ValueError("Claude category-description response is not a JSON array")

    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise ValueError(f"Category-description entry is not an object: {item!r}")
        cat_id = item.get("category_id")
        desc = item.get("description")
        if cat_id not in requested:
            raise ValueError(
                f"Claude returned unknown category_id {cat_id!r} "
                f"(not among requested categories)"
            )
        if not desc or not str(desc).strip():
            raise ValueError(f"Claude returned empty description for category_id {cat_id!r}")

        cat = profiles["categories"][cat_id]
        cat["description"] = str(desc).strip()
        cat["dirty"] = False
        cat["corrections"] = []  # consumed by this regeneration; clear the queue
        seen.add(cat_id)

    missing = requested - seen
    if missing:
        raise ValueError(
            f"Claude omitted descriptions for category_ids: {sorted(missing)}"
        )


def regenerate_stale(profiles: dict, api_key: str) -> list:
    """Regenerate descriptions for all stale categories.

    Returns the list of category_ids that were regenerated (empty if none were
    stale — in which case no Claude call is made).
    """
    stale_ids = [
        cat_id
        for cat_id, cat in profiles["categories"].items()
        if is_stale(cat)
    ]
    if not stale_ids:
        return []
    bootstrap_descriptions(profiles, stale_ids, api_key)
    return stale_ids


# ---------------------------------------------------------------------------
# Prompt formatting — what Claude sees during item categorization
# ---------------------------------------------------------------------------

def format_profiles_for_prompt(
    categories: list, profiles: dict, *, include_exemplars: bool = True
) -> str:
    """Build the category block for a Claude categorization prompt.

    For each category we emit its name, id, and learned description. When
    `include_exemplars` is True (the Amazon item tier), we also list the items
    for which THIS category is the dominant choice, so contradictory weak signals
    don't leak across categories. The payee tier passes include_exemplars=False:
    item-level exemplars describe products, not merchants, and add no signal to a
    merchant-level decision.

    NO SILENT FALLBACK: a category without a learned description is still listed
    (so categorization can proceed) but emits a logged warning — an undescribed
    category is exactly the "USB cable -> Computer" failure mode this module
    exists to fix, and the user should see that it hasn't been learned yet.

    Args:
        categories: YNAB category groups (filtered to recently-used).
        profiles: The category profile store.
        include_exemplars: Whether to append per-category item examples.

    Returns:
        A formatted multi-line string for inclusion in the system prompt.
    """
    cat_profiles = profiles.get("categories", {})

    # Precompute, per category, the exemplar item names where it is dominant.
    dominant_items: dict = {}
    if include_exemplars:
        for cat in cat_profiles.values():
            for norm_item in cat.get("item_exemplars", {}):
                dom_id, _ = dominant_item_category(profiles, norm_item)
                if dom_id is None:
                    continue
                counts = _all_exemplar_counts(profiles, norm_item)
                total = counts[dom_id]["count"]
                dominant_items.setdefault(dom_id, []).append((total, norm_item))

    out_lines = []
    for group in categories:
        for cat in group.get("categories", []):
            cat_id = cat["id"]
            name = cat["name"]
            profile = cat_profiles.get(cat_id)
            description = profile.get("description") if profile else None

            if not description:
                logger.warning(
                    "Category %r (id %s) has no learned profile description; "
                    "categorization will rely on the bare name. Run a "
                    "profile bootstrap to teach Claude what this category means.",
                    name, cat_id,
                )
                out_lines.append(f"- {name} (ID: {cat_id})")
            else:
                out_lines.append(f"- {name} (ID: {cat_id}): {description}")

            if include_exemplars:
                examples = dominant_items.get(cat_id, [])
                if examples:
                    examples.sort(key=lambda p: (-p[0], p[1]))
                    top = [n for _, n in examples[:MAX_EXEMPLARS_PER_CATEGORY]]
                    out_lines.append(f"    examples: {', '.join(top)}")

    return "\n".join(out_lines)
