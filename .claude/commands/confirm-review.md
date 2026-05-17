---
allowed-tools: [Bash]
description: Interactively review the latest enrich-changeset before /confirm-apply (no YNAB writes)
---

# /confirm-review

Prepare the interactive review of the latest enrich-changeset, hand the run off to the user's terminal, then pre-flight the resulting sidecar so `/confirm-apply` can write to YNAB cleanly.

`review.py` is **deliberately interactive in your terminal** — it opens a styled HTML view of the changeset in the browser, then prompts proposal-by-proposal for the risky tiers (Amazon splits, fuzzy/Claude/amazon-wf). History-tier proposals get one bulk-accept prompt. You can accept, recategorize, or skip each item; quitting mid-walk saves partial state for resume. It will not run cleanly inside this assistant session — it needs a real TTY, and the human-in-the-loop decisions need to be yours.

## Arguments

- `$ARGUMENTS` — optional explicit changeset path. If empty, the newest non-reviewed `data/cache/enrich-changeset-*.json` is used.

## Your Task

This is a **two-phase, user-driven workflow**. Do not try to run `review.py` yourself — drive the user through it.

### Phase 1 — Hand off the review

1. Resolve the input changeset path:
   - If `$ARGUMENTS` is non-empty, use it.
   - Otherwise: `ls -t data/cache/enrich-changeset-*.json 2>/dev/null | grep -v -- '-reviewed.json' | head -1`.
   - If none exists, print "No changeset found. Run `/enrich-run` first." and stop.

2. Print the exact command for the user to run in their own terminal, plus a short explanation. Example output:

   ```
   The review step is interactive — please run this in your terminal:

       uv run python review.py <PATH>

   It will open the HTML view in your browser and prompt you proposal-by-proposal.
   When you're done (or if you quit mid-walk), tell me and I'll pre-flight the sidecar.
   ```

3. Stop and wait for the user. **Do not invoke `review.py` via Bash.** The TTY-less environment will EOF the first prompt and abort the script before any decisions are recorded.

### Phase 2 — Pre-flight the sidecar (after the user reports back)

When the user says they're done (or you see a fresh `-reviewed.json` appear):

1. Resolve the sidecar path: newest `data/cache/enrich-changeset-*-reviewed.json` whose mtime is newer than the input changeset. If none exists, print "No reviewed sidecar found — `review.py` may have exited before any decisions were saved." and stop.

2. **Echo and run** the dry-run pre-flight on the sidecar. This is non-interactive and safe to run from here:

   ```bash
   uv run python amazon_confirm.py data/cache/enrich-changeset-<TS>-reviewed.json --dry-run --yes
   ```

3. Pre-flight exit handling:
   - Exit 0: print "Reviewed and pre-flighted. Run `/confirm-apply` to write to YNAB."
   - Exit 2: `YNAB_SANDBOX_MODE=1` is set. Tell the user to `unset YNAB_SANDBOX_MODE` (or comment the line in `.env`) and retry. Do NOT strip it from the subprocess env — sandbox mode is a deliberate safety setting.
   - Exit 3: pre-flight failure (missing env, bad token, malformed changeset). Surface stderr verbatim.

## Why this is split across the user's terminal and yours

- **The decisions must be the user's.** `review.py` is the only human-in-the-loop step between auto-categorization and real-money writes. Running it for them would defeat the point.
- **The script needs a TTY.** It uses `input()` and opens a browser tab; both require an interactive shell.
- **The pre-flight does not need a TTY.** `--dry-run --yes` is structural validation only — no writes, no prompts — so it runs cleanly from this session and is the right job for the assistant to handle.

## Do NOT

- Try to run `review.py` via Bash. It will EOF on the first prompt, exit 1, and produce no sidecar.
- Pass any flags to `review.py` (it's intentionally argument-light).
- Modify the input changeset file. The `-reviewed.json` sidecar is the source of truth after review.
- Skip the pre-flight. The dry-run catches schema/env errors that would otherwise surface mid-apply.
