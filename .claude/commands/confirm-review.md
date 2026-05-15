---
allowed-tools: [Bash]
description: Review the latest changeset via amazon_confirm.py --dry-run (no YNAB writes)
---

# /confirm-review

Inspect the latest Amazon changeset before applying. Runs `amazon_confirm.py --dry-run` so no YNAB writes happen, but the full pre-flight runs (budget resolution, schema validation, summary, by-category breakdown).

## Arguments

- `$ARGUMENTS` — optional explicit changeset path. If empty, the newest `data/cache/enrich-changeset-*.json` is used.

## Your Task

1. Resolve the changeset path:
   - If `$ARGUMENTS` is non-empty, use it as-is.
   - Otherwise: `ls -t data/cache/enrich-changeset-*.json 2>/dev/null | head -1`.
   - If none found, print "No changeset found. Run /enrich-run first." and stop.

2. **Echo the underlying command** in a copy-pasteable block, then run it:

   ```bash
   uv run python amazon_confirm.py <PATH> --dry-run --yes
   ```

   (Use `--yes` so no prompt fires; `--dry-run` ensures no YNAB writes.)

3. After the run:
   - On exit 0: print "Looks good. Run `/confirm-apply` (or the matching `amazon_confirm.py` command without --dry-run) to actually write to YNAB."
   - On exit 2: `YNAB_SANDBOX_MODE=1` is set. Surface verbatim — `amazon_confirm.py` refuses sandbox mode because PATCH operates on real transaction IDs from the source budget, which would 404 against the redirected sandbox budget. Tell the user to unset it before retrying:
     ```bash
     unset YNAB_SANDBOX_MODE
     ```
     or comment out `YNAB_SANDBOX_MODE=1` in `.env`. Do NOT strip the var from the subprocess env yourself — sandbox mode is a deliberate safety setting and must stay user-controlled.
   - On exit 3: pre-flight failure (missing env, bad token, malformed changeset). Surface the stderr message verbatim.

## Why dry-run

Dry-run runs the full pipeline up to but not including the PATCH call. Catches schema errors, missing env vars, uncategorized items, sum-invariant violations — all the things that would otherwise abort a real run mid-batch.

## Do NOT

- Pass `--throttle` or any flag besides `--dry-run --yes` here.
- Run without `--dry-run`. That's `/confirm-apply`'s job.
- Modify the changeset file.
