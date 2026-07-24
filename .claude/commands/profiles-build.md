---
allowed-tools: [Bash]
description: Build or refresh category profiles — the learned per-category descriptions that tell Claude what each YNAB category means in this budget
---

# /profiles-build

Generate the **category profile store** (`data/cache/category_profiles.json`): a
one-line learned description per budget category, built from the merchants you
file under each category. These descriptions are injected into both Claude
categorization tiers (novel payees and Amazon item splits) so Claude understands
what a category *means* here — e.g. "USB cable" → Home Goods, not Computer.

This makes Claude calls (one batched call per build) and writes only to
`data/cache/category_profiles.json`. It **does not touch YNAB**.

## Arguments

- `$ARGUMENTS` — `rebuild` (default) or `refresh`.
  - `rebuild` — regenerate **all** category descriptions. Use for first-time
    setup or a full rebuild.
  - `refresh` — regenerate **only** categories flagged stale: brand-new
    categories, or ones where you overrode Claude during review (a rejection).
    Cheap; skips categories whose descriptions are still working.

## Your Task

1. Parse `$ARGUMENTS`. If empty, use `rebuild`. If it is not exactly `rebuild`
   or `refresh`, print an error listing the two valid values and stop.

2. **Echo the underlying command** in a copy-pasteable block, then run it:

   For `rebuild`:
   ```bash
   uv run python tag.py --rebuild-profiles
   ```
   For `refresh`:
   ```bash
   uv run python tag.py --refresh-profiles
   ```

3. Run the command. Capture the exit code.

4. After the run:
   - On success (exit 0): print the path `data/cache/category_profiles.json` and
     state how many category descriptions were (re)generated (read it from the
     command's stdout — it prints "Generated category descriptions for N
     categories." or "No categories needed description regeneration.").
   - On exit 1: print "tag.py: configuration/env error — check .env
     (YNAB_API_TOKEN, ANTHROPIC_API_KEY, YNAB_DEFAULT_BUDGET)".
   - On other non-zero: print the exit code and surface stderr.

5. Print a one-line reminder: "Profiles now inform both Claude tiers on the next
   `/tag`. Re-run `/profiles-build refresh` after you override Claude's
   guesses during review."

## Why echo the command

The slash command is a thin wrapper. Always print the exact `uv run python ...`
invocation before running it so the user can run it themselves next time.

## Do NOT

- Pass any flag other than `--rebuild-profiles` / `--refresh-profiles`.
- Run a full `/tag` or write to YNAB — this only builds the profile store.
- Modify any file other than via the command itself (it writes
  `data/cache/category_profiles.json` and may refresh the payee cache).
