# /spend-advice

Run the spending advisor to get behavioral insights and advice on reducing your spending.

## Usage

```bash
/spend-advice
```

This is a convenience wrapper that runs:
```bash
uv run python scripts/spend_advice.py --days 90 --out-dir data/cache
```

## What It Does

1. Fetches your transactions from YNAB for the past 90 days
2. Analyzes spending patterns for:
   - Delivery app overpayment (DoorDash, Uber Eats, etc.)
   - Convenience store markups
   - Monthly subscriptions  
   - High-frequency small charges (e.g., coffee runs)
   - Spending trends across categories
3. Calls Claude Sonnet to synthesize a personalized advisory narrative
4. Generates a `.md` report and `.json` sidecar with all insights and trends

## Output

Two files are written to `data/cache/`:
- `spend-advice-YYYYMMDD-HHMMSS.md` — Full advisory report in Markdown
- `spend-advice-YYYYMMDD-HHMMSS.json` — Structured data (insights, trends, totals)

## Options

For more control, run the script directly:

```bash
uv run python scripts/spend_advice.py --days 90 --out-dir data/cache --budget "My Budget Name"
```

- `--days` — How many days back to fetch (required, default: 90)
- `--out-dir` — Output directory (default: data/cache)
- `--budget` — Budget name override (default: YNAB_DEFAULT_BUDGET env var)

## Requirements

- `YNAB_API_TOKEN` — Set in `.env`
- `ANTHROPIC_API_KEY` — Set in `.env`

The advisor is read-only — it never writes to YNAB.
