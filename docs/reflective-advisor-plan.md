# Plan: Rework the spending advisor into a reflective planning conversation

## Why

The current advisor (`spending_advisor.py` + `scripts/spend_advice.py`) is a
retrospective savings-scold: it detects "premiums" (delivery, convenience,
frequent small charges), estimates dollars you could *save*, and ranks insights
by `estimated_monthly_savings_dollars`. That paradigm is **Mint-style, and
philosophically anti-YNAB.** YNAB is forward-looking and values-first: you give
every dollar a job *that reflects what you actually care about*, before you
spend — it explicitly rejects backward-looking "you overspent" reports.

This rework realigns the advisor with YNAB by changing the question from
*"where did you waste money?"* to *"here's what your spending implies about your
priorities — is that the life you meant to budget for?"* That gap-between-
intended-and-revealed-priorities is exactly what Rule 1 is about.

## Target behavior (decided)

- **Output:** a forward-looking **plan + reflective prompts.** Lead with "based
  on your patterns, you'll likely want to plan ~$X/mo here" then surface signals
  as questions ("this looks like more delivery than you may have intended — does
  that match the life you're budgeting for?"). Keep the dollar *detection*; drop
  the savings/scold framing entirely.
- **Interaction:** a **back-and-forth dialogue.** It shares signals, asks whether
  they match intent, the user answers, it adjusts the plan. Not a one-shot report.
- **Voice:** **warm, non-judgmental.** Curious and gentle. "I notice…, does that
  feel intentional?" Never implies a *should*. Classic therapeutic stance — it
  reflects, it does not prescribe.

## What to keep vs. change

### Keep (the detection layer is still useful as *evidence*)
- `collect_spending_data()` — the aggregation (by category, monthly, payee
  groups) is the factual substrate. Keep as-is.
- `filter_advisory_transactions()`, `join_category_names()` — keep.
- The pattern detectors (`analyze_delivery_premium`, `analyze_convenience_markup`,
  `analyze_frequent_small_charges`, `analyze_trends`) — keep the *detection*, but
  **strip the savings math and the "suggested_action" scold.** They should emit a
  neutral *signal* ("X% of dining spend is delivery; N charges/mo"), not an
  overpay estimate.

### Change
- `SpendingInsight` dataclass: replace `estimated_monthly_savings_dollars` and
  `suggested_action` with neutral fields. Proposed shape:
  ```python
  @dataclass
  class SpendingSignal:
      pattern_type: str
      category: str | None
      summary: str          # neutral description of what's happening
      magnitude_dollars: float   # evidence, not a savings target
      payees_involved: list[str]
      reflective_prompt: str     # the question to pose to the user
  ```
- `ADVISOR_SYSTEM_PROMPT`: rewrite end-to-end. New stance:
  - Surface what the spending *implies* about lived priorities.
  - Propose a forward-looking plan number per major category, framed as a
    starting point to react to, not a budget to obey.
  - Ask whether each signal matches what they intended. Never say "you should",
    "cut", "waste", "overpaid", or estimate "savings".
  - Warm, curious, non-judgmental. One reflective question at a time.
- `synthesize_advisory()`: becomes the first turn of a conversation, not a
  one-shot. See dialogue loop below.

## New: dialogue loop

`scripts/spend_advice.py` currently fetches → analyzes → one Claude call →
writes markdown. Rework the tail into a chat loop:

1. Fetch + aggregate + detect signals (unchanged pipeline up to `context`).
2. **Turn 1 (assistant):** Claude is given the signals + category totals and
   produces: (a) a short reflection on what the pattern suggests, (b) a proposed
   forward-looking plan (per-category starting numbers), (c) *one* reflective
   question.
3. **User responds** in the terminal (free text — "yeah delivery is fine, it
   buys me time", or "no, I didn't realize it was that much").
4. **Turn N (assistant):** Claude adjusts the plan in light of the answer, may
   ask one more question, or wrap up. Maintain the running `messages` list as
   conversation state (Anthropic multi-turn).
5. **Exit:** user types `done`/`quit`, or after a turn cap (e.g. 6). On exit,
   write a transcript + the final agreed plan to `data/cache/`.

Notes:
- Needs a real terminal (like `review.py`). Gate on `sys.stdin.isatty()`.
- Persist conversation state so a session can resume (mirror review.py's
  sidecar pattern). Optional v1; one-session is fine to start.
- Model: keep Sonnet (`claude-sonnet-4-6`) for the conversational turns — voice
  quality matters here.

## Output artifacts
- `data/cache/reflection-<timestamp>.md` — the transcript + final plan, written
  on exit. This doubles as a (sanitized) **showcase artifact** for the README:
  it shows the tool's voice without exposing real numbers.
- Drop the `total_savings` summary line in the CLI — it's the old paradigm.

## Tests
- Detection-layer tests stay (they're pure functions over fixtures).
- New: unit-test the signal-construction (neutral summary/prompt, no savings).
- New: a scripted-dialogue test that feeds canned user turns and asserts the
  loop maintains message state and exits cleanly. Mock the Anthropic client
  (tests never hit the real API — existing convention in `tests/`).

## README implications (separate task)
- Reframe the advisor section: it's no longer "spending advice / savings", it's
  a **reflective planning conversation** that aligns with YNAB's forward-looking,
  values-first philosophy (Rule 1: give every dollar a job that reflects what you
  care about).
- Use a sanitized `reflection-*.md` transcript as the visual the README is
  currently missing.

## Open questions
- Per-category plan numbers: derive from trailing median? trailing mean of
  complete months? Let the user's stated intent override? (Lean: trailing median
  of complete months as the starting proposal, then adjust in dialogue.)
- Do we want the plan to write *back* to YNAB category targets via the API, or
  stay advisory-only? (Lean: advisory-only for v1 — writing budget targets is a
  bigger trust surface and a separate feature.)
