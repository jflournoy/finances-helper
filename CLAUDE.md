# CLAUDE.md — AI Guidelines

> **Note to Claude:** This file holds rules that apply to all work in scope for it — every
> project when it is installed globally, this project alone when it is vendored into one. For
> task-specific guidance, consult the guides listed at the bottom (TDD, standards, Bayesian
> modeling, writing voice). Load them based on what you're working on—don't assume you need everything.

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

### TEST INTEGRITY — NEVER MAKE A TEST PASS BY WEAKENING IT

**Never change a test solely to make it pass.** A failing test is information. Suppressing it
destroys the information and leaves the bug.

When a test fails after a change:

1. **Stop and report it.** Say which test, and what the failure actually says.
2. **Explain why it is failing** — did the change break behavior, or did it correctly change
   behavior the test still encodes?
3. **Ask which it is.** That judgment is the user's, not yours.

**Allowed without asking:** adding tests for new behavior; renaming or reorganizing tests
without changing what they assert; fixing a test that is itself provably wrong, saying so.

**Forbidden without explicit approval:** changing an expected value; loosening an assertion;
adding a tolerance to make a comparison pass; marking a test skipped, pending, or `.only`
elsewhere; deleting a failing test; wrapping a failing call so the error is swallowed.

This applies with full force when the number is the deliverable. A weakened assertion in
analysis code is a published wrong result with a green check mark next to it.

### ENFORCEMENT BEATS DOCUMENTATION

A rule with no mechanism behind it does not happen — it just looks like it does. If a rule
here matters, prefer a hook, a test, or a script that enforces it over a paragraph asking for
it. And **the enforcement mechanism is production code**: it gets a test like anything else.
An unverified gate is worse than no gate, because people stop checking the thing themselves.

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

## Commands

- `/commit` — atomic commits with quality checks
- `/push` — push, after checking CI is not already red
- `/hygiene` — project health: detects R, Python or Node and uses that project's runner
- `/next` — priorities, via the `next-priorities` agent
- `/refactor` — refactoring analysis for code a human reads
- `/refactor-verified` — refactoring analysis for code nobody reads, where checks replace review

Claude Code now covers natively what the rest of this repo's commands used to do:
transcripts and `--resume` replace session capture, the memory system replaces learning
capture, `TodoWrite` and `gh` replace todo management, `/loop` and `/schedule` replace
monitoring, and `/code-review` and `/simplify` replace the quality commands. They were
removed rather than maintained in parallel.

See [.claude/guides/workflow.md](.claude/guides/workflow.md) for full collaboration guidelines.

## When to Consult Each Guide

### 🔴 Load for Feature/Bug Work

- [.claude/guides/tdd.md](.claude/guides/tdd.md) — When implementing features, fixing bugs, or refactoring
  - Defines how to write tests first, then code
  - Required for any non-trivial code change

### 📋 Load for General Development

- [.claude/guides/standards.md](.claude/guides/standards.md) — Code quality expectations, testing strategy
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

See [.claude/guides/project-spec.md](.claude/guides/project-spec.md) for full details on trade-offs.

  - Covers: complexity limits, test standards, markdown validation, architecture principles

### 📊 Load for Statistical / Modeling Work

- [.claude/guides/bayesian-production.md](.claude/guides/bayesian-production.md) — When working
  on Bayesian models, Stan code, MCMC diagnostics, or time-series inference
  - Covers: Kalman filters, Pathfinder, reparameterization, correlation-matrix priors,
    regularized horseshoe, warm-starting, R̂/ESS thresholds
  - Stan snippets are compile-checked; the R (cmdstanr) and Python (cmdstanpy) calls differ

## Review Agents

Two fire automatically. A `PostToolUse` hook (`hooks/reviewer-dispatch.cjs`) routes an
edited file to its reviewer:

| Edited | Agent |
|---|---|
| `*.stan` | `stan-reviewer` — silent-wrong-answer bugs, geometry, wasted cycles |
| `*.R` `*.Rmd` `*.qmd` | `r-analysis-reviewer` — joins, coercion, non-determinism, claims |

Three are on demand — ask for them by name:

- `statistical-analysis-reviewer` — skeptical peer review of a finished analysis, before it
  is shared. Design, assumptions, inference, and whether the conclusion is supported.
- `determinism-reviewer` — finds work done by model reasoning that tested code could do.
- `voice-authenticator` — checks prose against [.claude/guides/voice.md](.claude/guides/voice.md).

All of them report findings and never edit. Silence them for a session with
`CLAUDE_REVIEWER_DISPATCH=0`. Adding a file type is one entry in `RULES` plus a test.

### 📐 Load for R Work

- [.claude/guides/r-development.md](.claude/guides/r-development.md) — Writing R:
  data.table over tidyverse, targets pipelines, testthat with reference semantics
  - Covers: approved packages, the tidyverse→data.table substitution table, arrow I/O
  - Includes the cmdstanr/posterior rule: the rstan-backed brms accessors can hard-crash
    R with SIGABRT in a container where rstan is broken

### ✍️ Load for Reader-Facing Prose

- [.claude/guides/voice.md](.claude/guides/voice.md) — Before writing or editing any prose a reader will see.
  The guide is the craft; each project declares in its own `CLAUDE.md` which files it covers
  and where they sit on the register dial
  - Report `.qmd` files, figure captions, README and docs pages, supplements
  - Defines the register dial (paper / commentary / conversational) and the rules for each
