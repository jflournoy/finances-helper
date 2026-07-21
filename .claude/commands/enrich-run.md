---
allowed-tools: [Bash]
description: Run tag.py over a recent window — categorize uncategorized YNAB transactions and produce a changeset for review
---

# /enrich-run

Run the unified categorize-and-enrich pipeline over a window of recent YNAB transactions. Produces a JSON changeset and a Markdown summary; **does not write to YNAB**.

## Arguments

- `$ARGUMENTS` — number of days back to process. Defaults to `30` if missing.

## Your Task

1. Parse the day window from `$ARGUMENTS`. If empty, use `30`. If not a positive integer, print an error and stop.

2. **Echo the underlying command** to stdout in a copy-pasteable block, then run it:

   ```bash
   uv run python tag.py --days <N>
   ```

3. Run the command. Capture exit code.

4. After the run:
   - On success (exit 0): find the newest `data/cache/enrich-changeset-*.json` (most recently mtime'd) and print its path. That's what `/confirm-review` and `/confirm-apply` will operate on.
   - On exit 1: print "tag.py: configuration/env error — check .env (YNAB_API_TOKEN, ANTHROPIC_API_KEY, YNAB_DEFAULT_BUDGET)".
   - On exit 2: print "tag.py: Amazon dump missing — put one in data/imports/ or pass --dump".
   - On other non-zero: print the exit code and surface stderr.

5. Print a one-line reminder: "Next: `/confirm-review` to inspect, then `/confirm-apply` to write to YNAB."

## Why echo the command

The user is learning the CLI; the slash command is a thin wrapper. Always print the exact `uv run python ...` invocation before running it so they can run it themselves next time.

## Do NOT

- Run with any flag other than `--days` unless the user explicitly asks (no `--dump`, no `--out-dir`).
- Modify any files. This is read-only-from-YNAB and changeset-write-to-disk only.
- Run `apply.py` afterward — that's a separate, explicit step.
