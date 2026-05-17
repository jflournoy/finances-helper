---
allowed-tools: [Bash]
description: Apply the latest reviewed changeset to YNAB via amazon_confirm.py (writes real money)
---

# /confirm-apply

Pre-flight the reviewed changeset, then hand the actual write off to the user's terminal. **This workflow writes real money.** The final `[y/N]` confirmation must be the user's, typed into their own shell — not anything this assistant can press.

## Arguments

- `$ARGUMENTS` — optional explicit changeset path. If empty, the newest `data/cache/enrich-changeset-*-reviewed.json` is used. Falls back to a non-reviewed `enrich-changeset-*.json` only if no reviewed sidecar exists.

## Your Task

This is a **two-phase, user-driven workflow**. The pre-flight runs from here; the actual write runs in the user's terminal.

### Phase 1 — Pre-flight (assistant-side)

1. Resolve the changeset path:
   - If `$ARGUMENTS` is non-empty, use it.
   - Otherwise prefer the newest reviewed sidecar:
     `ls -t data/cache/enrich-changeset-*-reviewed.json 2>/dev/null | head -1`.
     If none exists, fall back to the newest non-reviewed file and warn:
     "No reviewed sidecar found — applying un-reviewed changeset. Consider running `/confirm-review` first."
   - If neither found, print "No changeset found. Run `/enrich-run` first." and stop.

2. Check sandbox mode. Run `grep -E '^YNAB_SANDBOX_MODE=' .env` and inspect the shell env. If `YNAB_SANDBOX_MODE=1`, tell the user to `unset YNAB_SANDBOX_MODE` (or comment the line in `.env`) and retry. PATCH doesn't support sandbox redirect; the CLI itself will refuse with exit 2. Do NOT strip the var from the subprocess env — sandbox mode is a deliberate safety setting.

3. Run the dry-run pre-flight from here. It's non-interactive (no prompts, no writes) and safe:

   ```bash
   uv run python amazon_confirm.py <PATH> --dry-run --yes
   ```

   - Exit 0: continue to Phase 2.
   - Exit 2: sandbox mode is set. See step 2 above.
   - Exit 3: pre-flight failure. Surface stderr verbatim and stop — the user shouldn't run the real apply if the dry-run is broken.

### Phase 2 — Hand off the apply (user-side)

The `amazon_confirm.py` apply step prompts `Apply N proposals to budget '<name>'? [y/N]:` before any writes. That prompt is the last safety net for real money and must be answered by the user in their own terminal. Don't run it from here — without a TTY the prompt EOFs and the script aborts.

1. Print the by-category summary from the dry-run output so the user can sanity-check the totals before they confirm.

2. Print the exact command for the user to run, plus a short explanation. Example:

   ```
   Pre-flight passed. The apply step needs your terminal because it will prompt
   for [y/N] before writing to YNAB. Run this in your shell:

       uv run python amazon_confirm.py <PATH>

   When it's done (or if it aborts), tell me and I'll surface the report.
   ```

3. Stop and wait. **Do not invoke `amazon_confirm.py` without `--dry-run` via Bash.** The interactive `[y/N]` EOFs in this environment and the run aborts cleanly — which is safe, but wastes the user's setup.

### Phase 3 — Verify the report (after the user reports back)

When the user says it's done:

1. Find the newest `data/cache/amazon-confirmed-*.json` report.
2. Read its summary (applied / skipped / failed counts, aborted flag).
3. Map to outcomes:
   - All applied, none failed: print "Applied N proposals. Report at <path>."
   - Some failed: print "Some transactions failed. The report at <path> has details. Resume is supported — re-run `/confirm-apply` to retry only the unapplied ones."
   - Aborted (rate limit or sandbox): surface `abort_reason` from the report.

## Resume semantics

`amazon_confirm.py` writes an `applied_at` timestamp into each proposal on success. If the run is interrupted, re-running `/confirm-apply` picks up where it left off — already-applied proposals are skipped, only the rest are PATCHed.

A `<changeset>.json.bak` of the original (pre-mutation) file is created on first apply. If something goes badly wrong, `mv <changeset>.json.bak <changeset>.json` restores it.

## Why this is split across the user's terminal and yours

- **The `[y/N]` must be the user's.** It is the only confirmation between a reviewed changeset and live YNAB writes; nothing the assistant types can substitute for it.
- **The dry-run pre-flight does not need a TTY.** `--dry-run --yes` is structural validation only — no writes, no prompts — so the assistant runs it to catch schema/env problems before handing off.
- **Report verification does not need a TTY.** Reading the JSON report after the user's run is exactly the kind of summarizing the assistant should do.

## Do NOT

- Run `amazon_confirm.py` without `--dry-run` from this session. The `[y/N]` will EOF and abort.
- Pass `--yes` to the real apply. The interactive prompt is the last safety net.
- Edit the changeset between pre-flight and apply. If the user wants changes, re-run `/enrich-run` or edit the reviewed sidecar deliberately.
- Suggest deleting the `.bak` file.
