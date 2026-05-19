# CLAUDE.md - Project AI Guidelines

> **Note to Claude:** This file contains critical rules and commands that apply to all work. For task-specific guidance, consult the guides listed below (TDD, standards, workflow). Load them based on what you're working on—don't assume you need everything.

## Critical Rules (Always Apply)

**These rules are non-negotiable and apply to all work.**

## Running commands

**CRITICAL: Never include comments in bash command blocks. Run commands without inline or preceding comments.**

Doing so makes it hard to approve or deny commands in settings.json

### ALWAYS write Python/shell code to a file before running it

Never use `python -c "..."`, `python3 -c "..."`, `uv run python -c "..."`, `bash -c "..."`, or heredoc-piped scripts (`python <<EOF ... EOF`) for anything beyond a single trivial expression. Even quick diagnostics, sanity checks, and one-off data peeks must be written to a file first and then executed.

**Why**: inline `-c` invocations and heredocs cannot be allowlisted in `settings.json` — each one prompts the user for approval individually. Writing to a file (e.g. `scripts/scratch_check_tags.py`) lets the user pre-approve `uv run python scripts/*.py` once and run as many checks as needed without re-prompting.

**How to apply**:
- For ad-hoc diagnostics, write to `scripts/scratch_*.py` (already gitignored by convention or short-lived enough to delete after).
- For reusable utilities, write to `scripts/<descriptive_name>.py`.
- Only acceptable inline use: a single literal command with no logic, e.g. `python3 -c "import sys; print(sys.version)"`. Anything with a loop, import chain, or multi-line body goes in a file.

### NO SILENT FALLBACKS — THIS IS A HARD RULE

Silent fallbacks are among the most dangerous patterns in software. They mask bugs, produce incorrect results quietly, and make debugging nearly impossible.

- **Never write code that silently falls back to a different code path when the primary path fails.**
- If data is missing → **error loudly** with a clear, actionable message.
- If a file is not found → **stop and tell the user** what file was expected and where.
- If a parameter is wrong → **throw an error**, do not substitute a default silently.
- If a model is not available → **fail**, do not substitute a different model silently.
- **Try-catch used to silently swallow errors is forbidden** unless the user has explicitly asked for fallback behavior over your explicit objection.
- **Optional parameters that silently change behavior are forbidden.** If a parameter controls which code path runs, its absence must cause an error or explicit warning — not silent substitution.

The only acceptable fallback pattern is one where:
1. The user has explicitly requested it in this conversation, AND
2. You have raised your objection to it on the record, AND
3. The fallback produces a **visible, logged warning** every time it fires.

### ALWAYS use `date` command for dates

Never assume or guess dates. Always run `date "+%Y-%m-%d"` when you need the current date for documentation, commits, or any other purpose.

### AI Integrity Principles

**Always provide honest, objective recommendations based on technical merit, not user bias.**

- **Never agree with users by default** - evaluate each suggestion independently
- **Challenge bad ideas directly** - if something is technically wrong, say so clearly
- **Recommend best practices** even if they contradict user preferences
- **Explain trade-offs honestly** - don't hide downsides of approaches
- **Prioritize code quality** over convenience when they conflict
- **Question requirements** that seem technically unsound
- **Suggest alternatives** when user's first approach has issues
- **Disagree when necessary** — silence is complicity. If you spot a bug, design flaw, security issue, or bad pattern, name it.

Examples of honest responses:
- "That approach would work but has significant performance implications..."
- "I'd recommend against that pattern because..."
- "While that's possible, a better approach would be..."
- "That's technically feasible but violates [principle] because..."
- "I'm concerned about [issue]. Let me explain why this won't work as written..."

## Quick Command Reference

**Core Workflow**
- `/tdd` - Test-driven development cycle
- `/commit` - Quality-checked atomic commits
- `/push` - Push commits to remote

**Development & Code Quality**
- `/hygiene` - Project health check
- `/todo` - Task management via GitHub Issues
- `/markdown-lint` - Validate and fix markdown files
- `/refactor` - Deep refactoring analysis
- `/maintainability` - Code maintainability review

**Analysis & Planning**
- `/next` - AI-recommended priorities
- `/plan-execute` - Multi-model plan-and-execute workflow
- `/causal-design` - Causal study design workflow
- `/timeline` - Generate timeline from git history

**Documentation & Learning**
- `/learn` - Capture insights and learnings
- `/docs` - Update and validate documentation
- `/docs-explain` - Educational documentation guide
- `/reflect` - Pause and reflect on current work
- `/retrospective` - Capture session metadata for analysis
- `/session-history` - Save and manage conversation transcripts

**Utilities**
- `/render-report` - Render analysis reports for GitHub Pages
- `/monitor` - Monitor GitHub repository for test failures/PRs
- `/continue` - Efficiently resume work from prior session
- `/condense` - Archive old content from current status
- `/clean-state` - Reset state tracking file for fresh session
- `/fix-permissions` - Fix Docker file ownership permissions

See [CLAUDE_workflow.md](CLAUDE_workflow.md) for full collaboration guidelines.

## When to Consult Each Guide

### 🔴 Load for Feature/Bug Work

- [CLAUDE_tdd.md](CLAUDE_tdd.md) — When implementing features, fixing bugs, or refactoring
  - Defines how to write tests first, then code
  - Required for any non-trivial code change

### 📋 Load for General Development

- [CLAUDE_standards.md](CLAUDE_standards.md) — Code quality expectations, testing strategy
  - Consult when: running tests, committing code, reviewing architecture
  - Covers: complexity limits, test standards, markdown validation, architecture principles

## Project-Specific Rules

### Toolchain

- **Python**: Use `uv` for all Python operations. Run scripts with `uv run python ...`, tests with `uv run pytest ...`. Never use bare `python` or `pip`.
- **npm**: Used for development scripts (markdown linting, quality checks). See `package.json`.
- **Secrets**: All secrets (API tokens) live in `.env` and are loaded via `python-dotenv` or equivalent. Never hardcode tokens. Never commit `.env`.

### YNAB API Safety

- **Never write to YNAB without a human review step.** Always print a proposed changeset and ask for confirmation before any write operation.
- YNAB API rate limit is 200 requests/hour — batch where possible.
- Cache API responses locally in `data/cache/` to avoid redundant API calls during development.
- YNAB uses milliunits (integer, 1000 = $1.00) — convert at the boundary, never in business logic.
- All dates in ISO 8601 (YYYY-MM-DD) internally.

### Testing Priorities and Coverage Targets

Tests should be prioritized by financial risk:

| Module | Coverage Target | Rationale |
|---|---|---|
| `ynab_client.py` | >90% | Touches real money — milliunit conversions, date handling, response parsing, error handling |
| `amazon_matcher.py` | >90% | Touches real money — matching logic, edge cases (same-day/same-amount, split shipments, missing CSV fields) |
| `categorizer.py` | >85% | Payee normalization, history lookup, fuzzy match thresholds, tier routing |
| `insights.py` | >80% | Read-only, lower risk — aggregation, anomaly detection, month boundaries |

**Test fixtures live in `data/fixtures/`.** Tests must never hit the real YNAB API or Claude API. Use realistic fake responses that mirror the actual API contracts — do not mock in ways that could hide bugs.

### Categorization Tiers

Categorization runs three tiers before touching the Claude API (token-efficient by design):

1. **History lookup** — payees seen before resolve from `data/cache/payee_lookup.json`. Free.
2. **Fuzzy match** — catches payee name variations (store numbers, trailing codes). Free.
3. **Claude (Haiku, batched)** — only genuinely novel payees, batched into one API call.

The payee lookup is rebuilt after every confirmed categorization run. Over time, Claude is called less frequently as the lookup matures.

When Claude categorizes transactions, always include a **confidence score** and a **brief rationale**.

### Open Decision Points

**Amazon Order History Source** — decision needed before implementing `amazon_matcher.py`:
1. Amazon Order History Reporter (Firefox extension) — fast CSV, but requires trusting third-party extension
2. Amazon "Request My Data" — official but slow (3-5 days), JSON format
3. Skip item-level enrichment — route all Amazon to a single category via payee history

See [CLAUDE_project-spec.md](CLAUDE_project-spec.md) for full details on trade-offs.
