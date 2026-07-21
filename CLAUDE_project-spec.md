# YNAB Budget Assistant

## What This Project Does
This project connects your financial life to YNAB via the YNAB API. It handles:
1. Enriching Amazon credit card transactions with actual item descriptions and categories
2. Categorizing ambiguous transactions using Claude
3. Surfacing spending insights and anomalies

## Setup (Run Once)
1. Generate a YNAB API token at https://app.ynab.com/settings/developer
2. Copy `.env.example` to `.env` and fill in:
   - `YNAB_API_TOKEN` — your YNAB Personal Access Token
   - `ANTHROPIC_API_KEY` — your Anthropic API key
   - `YNAB_DEFAULT_BUDGET` — the **name** of your budget as it appears in YNAB (resolved to UUID at runtime)
3. Optionally create a `config.json` for the `amazon.date_window_days` override (defaults to 3 if absent).

## Project Structure
```
ynab-assistant/
├── CLAUDE.md               ← you are here
├── pyproject.toml          ← project metadata and dependencies (uv)
├── .env                    ← secrets + YNAB_DEFAULT_BUDGET — gitignored
├── .env.example            ← template for .env (safe to commit)
├── config.json             ← optional; only `amazon` section read — gitignored
├── ynab_client.py          ← YNAB API wrapper
├── amazon_matcher.py       ← match Amazon orders to YNAB transactions
├── categorizer.py          ← Claude-powered transaction categorization
├── insights.py             ← spending analysis and anomaly detection
├── tests/
│   ├── test_ynab_client.py
│   ├── test_amazon_matcher.py
│   ├── test_categorizer.py
│   └── test_insights.py
└── data/
    ├── imports/            ← drop Amazon Order History CSVs here
    ├── fixtures/           ← static test data (fake transactions, sample CSVs)
    └── cache/              ← local transaction cache to avoid redundant API calls
```

## Test-Driven Development (ENFORCED)
**This project follows strict TDD. No production code is written before a failing test exists.**

### Rules
1. **Red → Green → Refactor.** Write the test first. Watch it fail. Then write the minimum code to pass it. Then clean up.
2. **No function is "done" without tests.** If Claude Code writes a function, it writes the tests first or in the same turn — never after.
3. **Never mock the YNAB write API in a way that could hide bugs.** Use realistic fake responses that mirror the actual YNAB API contract.
4. **All tests must pass before any YNAB write operation is attempted against the real API.** Run `pytest` clean before touching live data.
5. **Fixtures over live data.** Tests use static fake transactions in `data/fixtures/` — never hit the real YNAB API or Claude API in unit tests.

### What to Test (Priority Order)
- `ynab_client.py`: milliunit conversions, date handling, response parsing, error handling on bad API responses
- `amazon_matcher.py`: matching logic (exact, fuzzy, ambiguous, unmatched cases), edge cases (same-day same-amount orders, split shipments, missing fields in CSV)
- `categorizer.py`: payee normalization, history lookup (high/low confidence), fuzzy match threshold behavior, tier routing logic
- `insights.py`: aggregation correctness, anomaly detection (known z-score cases), month boundary handling

### Running Tests
```
uv run pytest tests/ -v                  # run all tests
uv run pytest tests/test_categorizer.py  # run one module
uv run pytest tests/ --cov              # with coverage report
```

### Coverage Targets
- `ynab_client.py` and `amazon_matcher.py`: >90% — these touch real money
- `categorizer.py`: >85%
- `insights.py`: >80% (read-only, lower risk)

## Your YNAB Setup
- **Budget name**: [RUN setup.py TO POPULATE]
- **Budget ID**: [RUN setup.py TO POPULATE]
- **Amazon credit card account name**: [FILL IN — e.g. "Amazon Visa"]
- **Amazon credit card account ID**: [RUN setup.py TO POPULATE]

## Your Category Structure
[RUN setup.py TO POPULATE — will list all category groups and categories with IDs]

Typical Amazon purchases should map to categories like:
- Household supplies → [your category name]
- Books / media → [your category name]
- Electronics → [your category name]
- Clothing → [your category name]
- Groceries → [your category name]
- Pet supplies → [your category name]
- Health / personal care → [your category name]

## Configuration

### Environment (`.env`, gitignored)

| Var | Required | Purpose |
|---|---|---|
| `YNAB_API_TOKEN` | Yes | YNAB Personal Access Token. |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude categorization. |
| `YNAB_DEFAULT_BUDGET` | Yes | YNAB **budget name** (the human-readable name shown in YNAB, e.g. `"Family"`). Resolved to a UUID at runtime via `/budgets`. Use a UUID directly only if you have a budget literally named like a UUID. |

Tokens stay in `.env` because they're secrets. Budget name is in `.env` because it's not a secret but isn't public information either.

### `config.json` (optional, gitignored)

Only the `amazon` section is read. Default values apply when absent:

```json
{
  "amazon": {
    "account_last4": {
      "ynab-account-uuid-1": "0804",
      "ynab-account-uuid-2": "3719"
    },
    "date_window_days": 3
  }
}
```

| Key | Required | Default | Purpose |
|---|---|---|---|
| `amazon.account_last4` | No | `{}` | Maps YNAB account IDs to last 4 digits of the card. **Currently informational only** — the matcher does not consume this for matching. Reserved for future disambiguation logic. |
| `amazon.date_window_days` | No | `3` | Days of slack between Amazon ship date and YNAB transaction date for matching. |

If `config.json` is missing, all three CLIs run with defaults.

## Canonical Entry Point: `tag.py`

**For routine use, run `tag.py`.**

```
uv run python tag.py --days 30
```

This is the unified workflow that handles both Amazon and non-Amazon uncategorized transactions in a single pass:

1. **Fetches** uncategorized transactions from the last N days (one YNAB API call)
2. **Filters** by writability: uncategorized + cleared/not-reconciled + not deleted
3. **Partitions** into Amazon vs non-Amazon payees
4. **For Amazon txns:**
   - Loads the latest Amazon Order History dump from `data/imports/`
   - Matches shipments to YNAB transactions by date + amount (within a configurable window)
   - Runs item-level categorization via Claude, producing split proposals
5. **For non-Amazon txns:**
   - Runs flat (whole-transaction) categorization via Claude
6. **Writes** a unified changeset (markdown + JSON) to `data/cache/enrich-changeset-*.{md,json}`
7. **Prints** a summary of all proposed changes

The unified approach:
- Cuts YNAB API calls in half (one fetch instead of two per CLI)
- Batches all novel payees into a single Claude call for better amortization
- Produces one clear changeset for review

### Writability Filter
A transaction is included if ALL of:
- No category assigned yet (`category_id = null`)
- Not reconciled (`cleared != "reconciled"`)
- Not deleted (`deleted = false`)

### Exit Codes
- `0`: Success (even if some txns were unmatched — unmatched is expected)
- `1`: Config or environment error (missing token, missing config, malformed JSON)
- `2`: Missing Amazon dump when Amazon txns are present

### Known Limitations
- `categorizer.py` when run standalone has a latent bug: it does NOT apply the full writability filter. Use `tag.py` for correct behavior.
- Reconciled/deleted transactions are silently skipped — they should be, but the API doesn't guarantee they won't reappear in a later run. This is a YNAB API contract issue, not a bug in our code.

## Categorization Strategy (Token Efficient)
Categorization runs three tiers before touching the Claude API:
1. **History lookup** — payees seen before are resolved from a local cache of your transaction history. Free.
2. **Fuzzy match** — catches payee name variations (store numbers, trailing codes). Free.
3. **Claude (Haiku, batched)** — only for genuinely novel payees, all batched into one API call.

The payee lookup lives in `data/cache/payee_lookup.json` and is rebuilt after every confirmed categorization run. Over time, Claude is called less and less frequently as the lookup matures.

## Amazon Matcher Workflow: Changeset Review (Current State as of 2026-05-08)

**What works:** You can now generate a changeset of proposed Amazon splits:

```bash
uv run python amazon_matcher.py --days 60 --out-dir data/cache
```

This produces two files:
- `data/cache/amazon-changeset-YYYYMMDD-HHMMSS.md` — human-readable summary and review document
- `data/cache/amazon-changeset-YYYYMMDD-HHMMSS.json` — machine-readable changeset (for future automation)

**The workflow:**
1. Run the command above
2. Open the `.md` file and review the proposed splits for each multi-item Amazon order
3. Check the counts in the Summary table:
   - Proposed splits (multi-item)
   - Proposed single categorizations
   - Unmatched YNAB transactions (unexpected matches)
   - Unmatched Amazon shipments (shipments not found in YNAB)
   - Excluded shipments (cancelled, missing date, etc)
   - Parse errors (malformed CSV rows)

**What is NOT yet implemented:** 

The `--confirm` step that applies splits to YNAB:
```bash
# Not yet implemented (see issue #55)
uv run python amazon_matcher.py --confirm data/cache/amazon-changeset-YYYYMMDD-HHMMSS.json
```

Until #55 is done, **manually apply the splits** via the YNAB web interface:
1. For each proposed split, go to the YNAB transaction
2. Click "Split" and create subtransactions matching the changeset
3. Assign categories as shown in the changeset

When #55 is implemented, `--confirm` will automate step 1-3 via the YNAB API (PATCH /transactions with subtransactions).

## Diagnostic and Specialized CLIs

These tools remain available for single-purpose workflows or diagnostics, but **are not the recommended entry point** — use `tag.py` instead.

### Amazon Matcher (Diagnostic)
```
uv run python amazon_matcher.py --days 30
```
- Matches Amazon order history to YNAB transactions by date + amount
- Produces item-level split proposals for matched shipments
- Writes a separate changeset in `data/cache/amazon-changeset-*.{md,json}`
- **Limitation:** Does not categorize non-Amazon txns; for a complete workflow, use `tag.py`

### Categorizer (Diagnostic)
```
uv run python categorizer.py --days 30
```
- Categorizes uncategorized txns using Claude
- Produces flat (non-split) category proposals
- Writes a changeset in `data/cache/categorizer-changeset-*.{md,json}`
- **Limitation:** Does not handle Amazon item-level enrichment; for full enrichment, use `tag.py`
- **Known bug:** Does not apply the full writability filter (use `tag.py` for correct behavior)

### Spending Insights
```
uv run python insights.py --months 3
```
- Summarizes spending by category over the last N months
- Flags categories over budget
- Detects anomalous transactions (statistical outliers within each category)
- Shows month-over-month trends

## Conventions and Preferences
- Never write to YNAB without a human review step — always print a proposed changeset and ask for confirmation
- Cache API responses locally in data/cache/ to avoid hammering the API during development
- YNAB API rate limit is 200 requests/hour — batch where possible
- When Claude categorizes transactions, always include a confidence score and a brief rationale
- Amazon order history source: **UNRESOLVED — see decision point below**
- Prefer OFX/QFX import format for any future bank imports (most structured)
- All dates in ISO 8601 (YYYY-MM-DD) internally
- Amount convention: YNAB uses milliunits (integer, 1000 = $1.00) — convert at the boundary

## Claude Code Usage Notes
You can ask me things like:
- "Import my latest Amazon orders from data/imports/"
- "Categorize uncategorized transactions from the last 2 weeks"
- "What did I spend on restaurants last month vs the month before?"
- "Show me any unusually large transactions this month"
- "What Amazon order was the $47.23 charge on March 3rd?"

I have full context on your budget structure, account IDs, and category names.
Always confirm before writing anything back to YNAB.

## Resolved Decisions

### Amazon Order History Source — RESOLVED
**Decision: Option 1 (Amazon Order History Reporter Firefox extension)**

Rationale: Real fixture data (9856-row CSV dump) demonstrates the extension works and produces a parseable format. Implementation proceeded with this source.

Extension: https://addons.mozilla.org/en-US/firefox/addon/amazon-order-history-reporter/
