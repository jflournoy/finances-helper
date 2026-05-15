---
allowed-tools: [Bash]
description: Apply the latest Amazon changeset to YNAB via amazon_confirm.py (writes real money)
---

# /confirm-apply

Apply an Amazon changeset to YNAB. **This writes real money.** Use `/confirm-review` first to dry-run.

## Arguments

- `$ARGUMENTS` — optional explicit changeset path. If empty, the newest `data/cache/enrich-changeset-*.json` is used.

## Your Task

1. Resolve the changeset path:
   - If `$ARGUMENTS` is non-empty, use it as-is.
   - Otherwise: `ls -t data/cache/enrich-changeset-*.json 2>/dev/null | head -1`.
   - If none found, print "No changeset found. Run /enrich-run first." and stop.

2. Refuse to proceed if `YNAB_SANDBOX_MODE=1` is set in the shell env or `.env`. Run `grep -E '^YNAB_SANDBOX_MODE=' .env` to surface the .env value; if the user is in sandbox mode tell them to `unset YNAB_SANDBOX_MODE` (or comment the line in `.env`) and retry. PATCH doesn't support sandbox redirect; the CLI itself will refuse with exit 2. Do NOT strip the var from the subprocess env — sandbox mode is a deliberate safety setting and must stay user-controlled.

3. **Echo the underlying command** in a copy-pasteable block. Do NOT pass `--yes` — let `amazon_confirm.py`'s interactive `[y/N]` prompt run so the user gets one last chance to back out:

   ```bash
   uv run python amazon_confirm.py <PATH>
   ```

4. Run it. The CLI will:
   - Print a summary (total, splits, flats, uncategorized, resume-skip, by-category)
   - Prompt `Apply N proposals to budget '<name>'? [y/N]:`
   - On `y`: PATCH the transactions, write a summary report to `data/cache/amazon-confirmed-<ts>.json`, mark each applied proposal in the changeset file in-place.
   - On anything else: abort cleanly.

5. After the run:
   - Exit 0: clean run. Print the path to the `amazon-confirmed-*.json` report (newest file in `data/cache/`).
   - Exit 1: at least one PATCH failed. Print "Some transactions failed. The report at <path> has details. Resume is supported — re-run `/confirm-apply` to retry only the unapplied ones."
   - Exit 2: aborted (rate limit hit or sandbox mode set). Surface the abort_reason from the report.
   - Exit 3: pre-flight failure. Surface stderr.

## Resume semantics

`amazon_confirm.py` writes an `applied_at` timestamp into each `proposed_splits[i]` on success. If the run is interrupted, re-running this command picks up where it left off — already-applied proposals are skipped, only the rest are PATCHed.

A `<changeset>.json.bak` of the original (pre-mutation) file is created on first apply. If something goes badly wrong, `mv <changeset>.json.bak <changeset>.json` restores it.

## Do NOT

- Pass `--yes`. The interactive prompt is the last safety net for write operations.
- Pass `--dry-run`. That's `/confirm-review`'s job.
- Edit the changeset before applying. If the user wants changes, they should re-run `/enrich-run`.
- Suggest deleting the `.bak` file.
