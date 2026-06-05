# YNAB Budget Assistant

Connect your financial life to [YNAB](https://www.ynab.com/) via the YNAB API. This project handles:

1. **Amazon transaction enrichment** — match Amazon charges to actual order items and split them into item-level subtransactions with categories
2. **AI-powered categorization** — categorize transactions using a token-efficient tiered approach (history → fuzzy → Claude)
3. **Spending advice** — behavioral spending insights (delivery premiums, convenience markups, frequent small charges, trends)

Every write to YNAB goes through an explicit human review step. Nothing is written without your confirmation.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for Python dependency management
- [Node.js / npm](https://nodejs.org/) for the development scripts (linting, quality checks)
- A YNAB account and API token
- An Anthropic API key (for the Claude categorization tier)

## Setup

1. Install dependencies:
   ```bash
   uv sync
   npm install
   ```
2. Generate a YNAB API token at <https://app.ynab.com/settings/developer>, and an
   Anthropic API key at <https://console.anthropic.com/>.
3. Copy `.env.example` to `.env` and fill in **all three** required values:
   ```bash
   cp .env.example .env
   ```
   ```dotenv
   YNAB_API_TOKEN="your_ynab_token_here"
   YNAB_DEFAULT_BUDGET="Your Budget Name"     # the budget's display name, resolved via the API
   ANTHROPIC_API_KEY="your_anthropic_key_here"
   ```
   `.env` is gitignored — never commit it.

   > `config.json` is **optional**. It is only read for an `amazon.date_window_days`
   > override; the budget is resolved by name from `YNAB_DEFAULT_BUDGET`. You do not
   > need to create it to get started.

4. (Optional) For Amazon enrichment, drop an Amazon order-history export into
   `data/imports/` named `amazon-order-history-YYYY-MM-DD.zip`. See
   [Amazon order history](#amazon-order-history) below.

## Usage

The Amazon + categorization pipeline is a three-step flow. Each step has a thin
slash-command wrapper (shown in parentheses) and an underlying CLI you can run directly.

### 1. Enrich & categorize  (`/enrich-run`)

```bash
uv run python enrich.py --days 30
```

Fetches uncategorized YNAB transactions from the last N days, matches Amazon charges
to your order-history dump, and categorizes everything via three tiers:

1. **History lookup** — payees seen before resolve from a local cache (free)
2. **Fuzzy match** — catches payee name variations like store numbers (free)
3. **Claude (Haiku, batched)** — only genuinely novel payees, batched into one API call

For Amazon **item-level** splits, Claude is also told what each budget category
*means* in your budget — a learned **category profile** (`data/cache/category_profiles.json`)
built from the merchants you file under each category and refined by the item splits
you confirm. This is what keeps "USB cable" landing in Home Goods rather than Computer.
Rebuild the descriptions any time with `enrich.py --rebuild-profiles`.

This **does not write to YNAB**. It produces a reviewable changeset under
`data/cache/enrich-changeset-*.json` (+ a Markdown summary).

### 2. Review  (`/confirm-review`)

```bash
uv run python review.py data/cache/enrich-changeset-<timestamp>.json
```

Interactive (needs a real terminal): opens an HTML view and walks you through the
risky proposals (Amazon splits, fuzzy/Claude matches) one at a time — accept,
recategorize, or skip. History-tier matches get a single bulk-accept. Your decisions
are saved to a `-reviewed.json` sidecar. Quitting mid-walk saves partial progress.

### 3. Apply  (`/confirm-apply`)

```bash
uv run python amazon_confirm.py data/cache/enrich-changeset-<timestamp>-reviewed.json --dry-run --yes
uv run python amazon_confirm.py data/cache/enrich-changeset-<timestamp>-reviewed.json
```

Writes the reviewed changeset back to YNAB. Run `--dry-run` first to validate.
Set `YNAB_SANDBOX_MODE=1` to target a test budget while you experiment.

Applying Amazon splits also records each confirmed item→category into the category
profile store, so your reviewed decisions sharpen future item categorization.

### Spending advice  (`/spend-advice`)

```bash
uv run python scripts/spend_advice.py --months 3
```

Read-only. Summarizes spending and surfaces behavioral insights (delivery premiums,
convenience markups, frequent small charges, category trends), then synthesizes
advice via Claude.

### Categorization audit

```bash
uv run python audit.py
```

Read-only review of how transactions were categorized.

## Amazon order history

Personal Amazon accounts no longer support direct CSV export. Use Amazon's official
**"Request My Data"** flow (Account → Request Your Information → Your Orders), which
emails you a zip within a few days. Place it in `data/imports/` named
`amazon-order-history-YYYY-MM-DD.zip`. The matcher reads
`Your Amazon Orders/Order History.csv` from inside the zip.

Matching is by exact amount + a date window (default ±3 days). Digital charges
(Prime Video, tips, subscriptions) have no shipment row and are reported as
unmatched — that's expected.

## Project structure

```
finances-helper/
├── enrich.py               # Step 1: match + categorize → changeset (no writes)
├── review.py               # Step 2: interactive human review → -reviewed.json
├── amazon_confirm.py       # Step 3: apply reviewed changeset to YNAB
├── ynab_client.py          # YNAB API wrapper (milliunits, dates, budget resolution)
├── amazon_matcher.py       # Match Amazon orders → YNAB transactions
├── categorizer.py          # Three-tier transaction categorization
├── category_profiles.py    # Learned per-category meaning for Amazon item splits
├── payee_resolver.py       # Payee normalization / lookup
├── spending_advisor.py     # Behavioral spending insights
├── audit.py                # Read-only categorization audit
├── view_changeset.py       # Changeset inspection helper
├── scripts/
│   └── spend_advice.py     # Spending-advice CLI
├── .claude/commands/       # Slash-command wrappers (/enrich-run, /confirm-*, /spend-advice, …)
├── tests/                  # Unit + integration tests (fixtures in data/fixtures/)
└── data/
    ├── imports/            # Drop Amazon order-history zips here
    ├── fixtures/           # Static test data
    └── cache/              # Changesets, payee cache, API caches (gitignored)
```

## Conventions

- **Human review required** — no writes to YNAB without confirmation
- **API rate limit** — YNAB allows 200 requests/hour; responses are cached in `data/cache/`
- **Amounts** — YNAB uses milliunits (1000 = $1.00); conversion happens at the boundary
- **Dates** — ISO 8601 (YYYY-MM-DD) internally
- **Sandbox writes** — set `YNAB_SANDBOX_MODE=1` to write to a test budget

## Testing

Tests use static fixtures in `data/fixtures/` and never hit the real YNAB or Claude APIs.

```bash
uv run pytest tests/ -q                   # run all tests
uv run pytest tests/test_categorizer.py   # run one module
uv run pytest tests/ --cov                # with coverage
```

### Coverage targets

| Module | Target | Rationale |
|---|---|---|
| `ynab_client.py` | >90% | Touches real money |
| `amazon_matcher.py` | >90% | Touches real money |
| `categorizer.py` | >85% | Core categorization logic |
| `spending_advisor.py` | >80% | Read-only, lower risk |
