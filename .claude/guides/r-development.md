# R Development

data.table and base R, not the tidyverse. The rules below are defaults for new work; a
project that has already committed to something else says so in its own `CLAUDE.md`.

## Non-negotiable

- **`data.table` for all data manipulation** — not dplyr, not base data frames
- **Never `%>%`.** Use `|>`, and only for two steps; past that, name the intermediate
- **Never load tidyverse** or any meta-package
- **Base R** for anything data.table and the approved list do not cover
- **`lapply()` + `rbindlist()`** as the default pattern for building tabular results
- **Modify in place with `:=`** — do not copy a data.table when `dt[, col := ...]` will do
- **Write functions.** Name your transformations; do not chain anonymous steps

## Approved packages

| Package | Purpose |
|---|---|
| `data.table` | All data manipulation |
| `ggplot2` | All visualization |
| `scico`, `viridisLite` | Continuous colour scales |
| `stringr` / `stringi` | String operations |
| `arrow` | Feather and parquet I/O, lazy datasets |
| `duckdb` | Out-of-core SQL |
| `lubridate` | Heavy date/time work only |
| `digest` | Salted hashing, e.g. SHA-256 for deidentification |
| `here` | Project-relative paths in scripts and tests |
| `S7` | OOP in new packages |
| `testthat` | Unit testing |
| `targets` | Pipeline management |
| `parallel` | Multicore, base R |
| `crew` / `crew.cluster` | Parallel backends for targets |
| `brms` | Bayesian model specification, cmdstanr backend |
| `cmdstanr` | The only sampling backend |
| `posterior` | Draw extraction and summaries — not rstan |
| `igraph` | Graph layout only; drawing stays in ggplot2 |

No tidyverse packages (dplyr, purrr, tidyr, readr, tibble, forcats). No fst. Justify anything
not on this list before using it.

## Style

- `snake_case` throughout — variables are nouns, functions are verbs
- Intermediate variables over chains; the name is free documentation
- Validate inputs at user-facing boundaries; skip internal validation
- `fcase()` / `fifelse()`, not `case_when()` / `if_else()`
- `rbindlist()`, not `bind_rows()`
- `melt()` / `dcast()`, not `pivot_longer()` / `pivot_wider()`
- `merge()`, not `left_join()`
- `fread()` / `fwrite()`, not `read_csv()` / `write_csv()`

### tidyverse → here

| Instead of | Write |
|---|---|
| `mutate(dt, z = x + y)` | `dt[, z := x + y]` |
| `filter(dt, x > 0)` | `dt[x > 0]` |
| `group_by() \|> summarise()` | `dt[, .(m = mean(x)), by = g]` |
| `map(x, f) \|> list_rbind()` | `rbindlist(lapply(x, f))` |
| `left_join(a, b)` | `merge(a, b, by = "id", all.x = TRUE)` |

## Testing

- `testthat`, written before the implementation: RED → GREEN → REFACTOR
- Test contracts — inputs, outputs, error conditions — not implementation details
- **For data.table functions, test reference semantics explicitly.** `:=` modifies in place,
  so a test that passes `dt` and then reuses it is testing a mutated object. Pass `copy(dt)`
  where that matters, and assert the mutation where it is the contract
- Tests in `tests/test-*.R`; run with `testthat::test_dir("tests/")`

## Pipelines

- `targets` for any multi-step analysis with slow or expensive steps
- Keep `_targets.R` thin: one function call per `tar_target()`
- All logic in functions under `R/`, tested with testthat
- Track input files with `format = "file"` so targets notices changes on disk

## Data I/O

- Read a CSV once with `fread()`, then write feather (working) or parquet (persistent/shared)
- Never re-read a CSV in an iterative workflow
- `write_feather()` / `read_feather()` for working data
- `write_parquet()` / `read_parquet()` for anything that persists or gets shared
- Wrap in `as.data.table()` after reading back from arrow

## Bayesian models: fit with cmdstanr, read with posterior

**Fit through the `cmdstanr` backend, and extract draws with `posterior` — never the
rstan-backed brms accessors.**

```r
fit <- brms::brm(..., backend = "cmdstanr")
```

This is not a style preference. `rstan` is often not a runtime dependency and may be absent or
broken in a container. The brms accessors below load an rstan *method* internally, and doing
that in an environment where rstan is broken can **hard-crash R with `SIGABRT`** — no error you
can catch, no partial output. `readRDS()` of a saved `brmsfit` pulls rstan into the namespace
for S4 class resolution, which is harmless on its own; invoking its model methods is not.

**Forbidden for post-processing a fitted model** — all route through rstan:

```r
brms::ranef()  brms::fixef()  brms::rhat()  brms::neff_ratio()
brms::nuts_params()  summary(fit)  as.matrix(fit)
```

**Use instead:**

```r
d <- posterior::as_draws_df(fit$fit)
posterior::subset_draws(d, variable = "b_")
posterior::summarise_draws(d, "rhat", "ess_bulk", "ess_tail")
```

Sampler diagnostics that are not in the draws — `divergent__` and friends — come from the
`sampler_params` attribute on `fit$fit@sim$samples`, which is plain R and involves no rstan
method.

**Keep extraction in one layer.** Put the `as_draws_df` calls in a small set of functions that
return plain data.tables, and have reports, scripts and tests consume those. Backend changes
then touch one file instead of every call site.

For model-level guidance — priors, reparameterization, diagnostics, making a model fast enough
to run on a schedule — see `.claude/guides/bayesian-production.md`.

## Running R: scripts, not inline `-e`

- **Never `Rscript -e '...'` for anything non-trivial.** Inline snippets cannot be
  allow-listed in `settings.json`, so each one triggers a permission prompt, and they leave no
  inspectable record of what ran
- **Write the script to a file, then run the file.** `scripts/` for anything reusable, a temp
  path for one-off exploration
- `Rscript path/to/script.R` lets `Rscript` be allow-listed once
- Exception: a genuinely trivial one-liner such as `Rscript -e 'sessionInfo()'`. Anything that
  reads data or has logic goes in a file

## Quarto HTML reports

- **Enable lightbox on every HTML report** so figures are click-to-zoom:
  `format: html: lightbox: true` in the format YAML. Applies to all images; built in since
  Quarto 1.5, no extension needed. Analysis figures are usually dense and unreadable at inline
  size, so this is the default rather than an opt-in.
