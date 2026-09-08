# Stan in Production

Reference for Claude. Two jobs: **write a competent Stan model**, and **make it fast enough
to run on a schedule**. Part 1 is correctness and idiom, Part 2 is performance. When a
technique belongs to both, it is stated in Part 1 and its speed consequence noted in Part 2.

Assumed context: a model that already fits and is already trusted, that now has to run on a
daily budget. Optimize in Part 2's order; do not start at the bottom.

**How to read the markers.** Claims carry their evidence, so you can tell what will rot and
what will not:

| Marker | Means |
|---|---|
| `[M]` | **Measured** — this number came from running it; see Provenance for the setup |
| `[C]` | **Compiles** — verified with `stanc` under the toolchain in Provenance |
| `[D]` | **Documented** — checked against library docs or source, *not executed* |
| `[L]` | **Literature** — from the cited reference |

Unmarked prose is general Stan practice with no single source. `[D]` is the weakest class and
the first to go stale — see Provenance for how to re-check everything.

---

# Part 1 — Writing Competent Stan

## Block structure, and what each block costs

The blocks are not just organization — they determine how often code runs and what gets
written to disk.

| Block | Runs | Written to output |
|---|---|---|
| `data` | once | no |
| `transformed data` | once | no |
| `parameters` | — | yes |
| `transformed parameters` | **every leapfrog step** | **yes, every draw** |
| `model` | every leapfrog step | no |
| `generated quantities` | once per saved draw | yes |

Two consequences worth internalizing:

- **Anything constant belongs in `transformed data`.** Computed once instead of millions of
  times. Standardizing predictors, building index arrays, precomputing `log(x)` — all free
  if hoisted.
- **`transformed parameters` is expensive twice over.** It is on the gradient path *and*
  every element is written to the CSV for every draw. If an intermediate is only needed to
  build the log density, declare it in a local block inside `model` instead:

```stan
model {
  profile("likelihood") {
    vector[N] eta = X * beta;   // local: on the gradient path, never written to disk
    y ~ bernoulli_logit(eta);
  }
}
```

A `matrix[K,K]` in `transformed parameters` with K=50 writes 2500 numbers per draw. At 4000
draws that is 10 million values you probably never read.

## Constraints and priors

**Declare the constraint; Stan handles the transform.** `real<lower=0> sigma` makes Stan
sample `log(sigma)` internally and apply the Jacobian for you. You do not need to do anything
else to get a well-behaved positive parameter.

**Stan has no `half_normal`.** This is a compile error, not a runtime surprise:

```text
Ill-typed arguments to "~"-statement. No function "half_normal_lpdf" was found
when looking for distribution "half_normal".
```
`[C]` — this is the literal `stanc` output, not a paraphrase.

A half-normal is a `<lower=0>` declaration plus `normal(0, s)`. Stan drops the truncation
constant, which does not affect sampling:

```stan
real<lower=0> sigma;
sigma ~ normal(0, 0.1);   // this is half-normal(0, 0.1)
```

**Sampling on the log scale is a prior choice, not a speed trick.** Since `<lower=0>` already
samples in log space, declaring `log_sigma` yourself changes only the prior shape:

```stan
data              { int<lower=1> K; }
parameters        { vector[K] log_sigma; }
transformed parameters { vector<lower=0>[K] sigma = exp(log_sigma); }
model             { log_sigma ~ normal(0, 0.5); }
// implied lognormal: median 1.00, mode 0.78, 90% interval [0.44, 2.28]   [M]
```

A half-normal has maximum density *at* zero; that lognormal has zero density there. Use the
log-scale form when you need to exclude `sigma = 0` — it rescues variance components that
would otherwise collapse. Scale it to your data; `normal(0, 0.5)` on the log scale is fairly
informative and centred on `sigma ≈ 1`.

## Weakly informative priors, informed inits

Both exist to make a hard posterior samplable, and both are ways a fit that looks clean ends up
reporting your own assumptions back to you. They are not the same kind of object: a prior is
part of the model and is allowed to move the posterior; an init is a search heuristic and is
not. They are checked differently, and conflating them is the failure mode.

**A prior is weakly informative only relative to a scale.** `normal(0, 5)` is vague on a
standardized predictor and nearly flat nonsense on raw dollars. Set the scale from the units
the parameter is actually in, then check what the prior implies about the *outcome* by drawing
from the prior predictive. If it simulates reaction times of ten thousand seconds, the prior is
not uninformative, it is wrong.

**Flat is not neutral.** An improper flat prior on a constrained or hierarchical parameter
hands the sampler heavy tails to explore and funnels that never close; on the hand-rolled
Cholesky factor under Correlation matrices below it gives an improper posterior outright,
measured. The prior that makes a difficult model fit is the one that rules out the absurd and
leaves everything the data can speak to.

**Stan's default init is `U(-2, 2)` on the unconstrained scale, not on yours** `[L]`. For
`real<lower=0> sigma` that is `sigma` in `[0.135, 7.39]`; for a `cholesky_factor_corr` it is an
arbitrary rotation. When the data pin `sigma` near 0.01, every chain starts where the gradient
is enormous and warmup is spent crawling back — or the sampler rejects initial values and gives
up. Informed inits start where the answer plausibly is. In increasing order of cost: moments of
the data for location and scale parameters, `mod$optimize()`, `mod$pathfinder()`, or the
previous run's draws. Stage 4 covers warm starts, Stage 6 Pathfinder.

### Checking that neither one is doing the work

|  | Prior | Init |
|---|---|---|
| What it is | part of the model | a search heuristic |
| May move the posterior | yes, by design | never |
| What a problem here hides | the data were not overwhelming | a second mode |
| How you check it | power-scaling sensitivity | dispersed starts, R̂ across chains |

**An init that changes the answer has told you about the posterior, not the sampler.** If
informed and default inits land in different places, you have multimodality or
non-identification, and the informed init hid it by starting every chain in the same basin.
`init = <CmdStanMCMC>` and `init = <CmdStanPathfinder>` are safe here: cmdstanr writes one init
file per chain and the values differ — from a fit, `mu` = 3.253, 3.380, 2.860, 3.122; from a
pathfinder, 2.927, 3.167, 2.800, 2.952 `[M]`. What is not safe is `init = list(one_point)`
recycled across chains, which starts every chain in the same place and makes R̂ blind to a
second mode. When a model needed informed inits to converge at all, confirm once from dispersed
or default starts that it reaches the same posterior, and record that you did.

**Then ask how much of the posterior is prior.** Power-scaling raises the prior — or the
likelihood — to a power α and measures how far the posterior moves, reusing the draws you
already have via importance sampling, so it costs no refits (Kallioinen et al. 2024) `[L]`.

The model has to expose two quantities, and one of them you are already writing for LOO:

```stan
generated quantities {
  vector[N] log_lik;
  real lprior = normal_lpdf(mu | 0, 2.5)
              + normal_lpdf(tau | 0, 1)
              + normal_lpdf(sigma | 0, 1);
  for (n in 1:N) log_lik[n] = normal_lpdf(y[n] | alpha[gg[n]], sigma);
}
```
`[C]` — compiled as part of a complete hierarchical program under stanc3 2.38.0.

**Name it `lprior`.** `powerscale_sensitivity()` defaults to `log_prior_name = "lprior"` and
`log_lik_name = "log_lik"` `[M]`; a variable called `log_prior` is simply not found. That is
brms's name for it, which is why a brms fit needs nothing extra.

Two more things about that block. Truncation and normalizing constants can be left out — they
shift `lprior` by a constant, and a constant cancels in the self-normalized importance weights.
And `z ~ std_normal()` in a non-centered parameterization is deliberately **not** in `lprior`:
it is structure, not a prior you are testing, and power-scaling it rescales the shrinkage you
built on purpose. Put in `lprior` only what you would want interrogated.

```r
library(priorsense)             # [M] priorsense 1.2.0
powerscale_sensitivity(fit)     # one row per variable
```

```text
  variable     prior likelihood                     diagnosis
1       mu 0.3400363  0.4706065 potential prior-data conflict
2    sigma 0.2789211  0.5093180 potential prior-data conflict
```
`[M]` — priorsense's own `example_powerscale_model("univariate_normal")`, 4 chains.

Read the two columns together. The `diagnosis` column is exactly this rule, taken from the
package source `[M]`, against `sensitivity_threshold = 0.05`:

| prior | likelihood | `diagnosis` | What it means |
|---|---|---|---|
| below | below | `-` | nothing flagged |
| below | at or above | `-` | the ordinary case — the data are overwhelming |
| above | below | `potential strong prior / weak likelihood` | the prior is doing the work |
| at or above | at or above | `potential prior-data conflict` | they disagree; the posterior splits the difference |

Note what the rule does *not* flag: low sensitivity to both is reported as `-`, not as a
problem. A parameter the data cannot see and the prior barely constrains reads the same as a
healthy one here, so this check does not replace the diagnostic gate above.

The defaults are `lower_alpha = 0.99`, `upper_alpha = 1.01`, `div_measure = "cjs_dist"` `[M]` —
a ±1% perturbation, so what you get is a local derivative at α = 1, not the effect of throwing
the prior away. It answers "is the posterior leaning on this", not "what would happen without
it".

`powerscale_sequence()` with `powerscale_plot_dens()` shows the whole α trajectory when one
number is not enough to see what is moving, and `create_priorsense_data()` is the explicit
route when your variables are named something else.

## Non-centered parameterization

The standard fix for hierarchical funnels. Rather than sampling `theta ~ normal(mu, sigma)`
directly, sample a standard normal and rescale:

```stan
parameters        { real mu; real<lower=0> sigma; vector[K] z; }
transformed parameters { vector[K] theta = mu + sigma * z; }
model             { z ~ std_normal(); }
```

This decouples the prior geometry from the likelihood and removes most divergences when
`sigma` is small or weakly identified.

**It is not universally better.** Non-centered wins when the data are *weak* relative to the
prior — few observations per group, `sigma` near zero. Centered wins when the data are
*strong*: many observations per group pin each `theta` down, and the non-centered form then
induces a funnel of its own. Betancourt & Girolami (2015) work through both regimes `[L]`. With
uneven group sizes, fit both and compare divergences and ESS.

Coming from `brms` or `rstanarm`: they emit the non-centered form for you. Writing Stan by
hand, it is yours to remember.

## Correlation matrices

**Use the built-in.** It is correct, it carries its Jacobian, and its geometry is good:

```stan
data       { int<lower=1> K; }
parameters { cholesky_factor_corr[K] L; }
model      { L ~ lkj_corr_cholesky(4); }
```

You will sometimes see warmup messages like:

```text
Exception: lkj_corr_cholesky_lpdf: Random variable[7] is 0, but must be positive!
```

**These are usually harmless.** They come from the density, not the geometry:
`lkj_corr_cholesky_lpdf` evaluates `sum(log(diag(L)))`, and when a diagonal element underflows
to exactly 0 that term is `-inf` and the function throws. Measured at K=10, 4 chains,
1000+1000: 26 such warmup messages, and the fit still finished with **0 divergences,
R̂ = 1.00, Bulk-ESS ≈ 1700–2300** `[M]`. Judge the fit by sampling-phase divergences and R̂/ESS, not
by warmup message count.

Hand-rolling the transform buys **no geometric advantage** — Stan's `cholesky_factor_corr`
*is* the tanh + signed stick-breaking map `[L]` (`z = tanh(y)`, then
`x[i,j] = z[i,j] * sqrt(1 - sum_{j'<j} x[i,j']^2)`), so a hand-rolled version samples in
exactly the same space.

The one real reason to hand-roll is to put a prior directly on the unconstrained scale, which
sidesteps `lkj_corr_cholesky_lpdf` entirely. Do that only if warmup exceptions are frequent
enough that adaptation actually fails:

```stan
data { int<lower=1> K; }
transformed data { int n_corr = (K * (K - 1)) %/% 2; }   // note the outer parentheses
parameters { vector[n_corr] z_raw; }                      // exactly K(K-1)/2 values
transformed parameters {
  matrix[K, K] L = rep_matrix(0, K, K);                   // zero the upper triangle
  {
    int pos = 1;
    L[1, 1] = 1;
    for (i in 2:K) {
      real running_prod = 1.0;
      for (j in 1:(i - 1)) {
        real z = tanh(z_raw[pos]);
        pos += 1;
        L[i, j] = z * running_prod;
        running_prod *= sqrt(1.0 - z * z);
      }
      L[i, i] = running_prod;
    }
  }
}
model {
  z_raw ~ normal(0, 0.28);   // K-dependent; see the table
}
```

Three details are load-bearing, and each has bitten a real model:

- **`rep_matrix(0, K, K)`.** Stan neither initializes nor validates an unconstrained `matrix`
  in transformed parameters. Declare it bare, fill only the lower triangle, and the model
  *runs clean* while writing `nan` into every upper-triangle cell — 4500 of them in a
  600-draw K=6 fit `[M]`. A downstream `multi_normal_cholesky(mu, L)` then rejects every proposal.
  Silent wrong answers, not a crash.
- **`vector[n_corr]`, not `matrix[K, K]`.** An oversized parameter block leaves K(K+1)/2
  entries with no prior — an improper posterior. Measured at K=6, one such parameter reached
  **R̂ = 2.1** with a posterior mean of **1.4e+12** `[M]`. Because R̂ is per parameter, this trips
  your convergence check on parameters that never enter the model.
- **The prior scale depends on K.** This parameterization drops the transform's Jacobian, so
  the induced prior on `L` is whatever the unconstrained normal induces — you cannot bolt an
  LKJ prior onto it, and no fixed scale is "LKJ-equivalent" across K. Measured marginal sd of
  an off-diagonal correlation at K=10: `normal(0, 0.5)` gives **0.364**, against **0.243** for
  LKJ(4) and **0.302** for LKJ(1) `[M]`. That is wider than uniform over correlation matrices — the
  opposite of shrinkage.

Simulated, 4000 draws per cell `[M]`:

| K | LKJ(4) marginal sd(r) | matching `normal(0, s)` |
|---|---|---|
| 5 | 0.289 | s ≈ 0.33 |
| 10 | 0.243 | s ≈ 0.28 |
| 20 | 0.192 | s ≈ 0.22 |

Simulate the transform at your actual K rather than reusing a number from this table.

## Identification

A model can be correct and still refuse to mix, because the likelihood does not distinguish
some parameter configurations. The sampler then wanders a ridge and R̂ never settles.

- **Factor and loading matrices are rotation- and sign-invariant.** `Lambda * f` and
  `(Lambda * R) * (R' * f)` give the same likelihood for any orthogonal `R`. Constrain
  `Lambda` — lower-triangular with positive diagonal is the usual choice — or fix an anchor
  item per factor. This is the same identification problem you solve in SEM by fixing a
  loading to 1 or standardizing the factor; Stan will not choose for you.
- **Label switching in mixtures.** Order a parameter (`ordered[K] mu`) or the components are
  exchangeable.
- **Additive constants.** An intercept plus a group mean that both float will trade off
  forever. Sum-to-zero constrain one.

Symptom to recognize: high R̂ and low ESS on a *subset* of parameters, while the log density
itself mixes fine.

## Vectorization is idiom, not just speed

```stan
for (n in 1:N) y[n] ~ normal(mu[n], sigma);   // avoid
y ~ normal(mu, sigma);                        // prefer
```

The vectorized form is clearer *and* builds a much smaller autodiff graph. Prefer it
everywhere; Part 2 quantifies why.

**`~` versus `target +=`.** The sampling statement drops constant terms, which is what you
want while sampling. When you need the true log density — for `log_lik` in
`generated quantities`, say — use the `_lpdf` form. `target += normal_lupdf(...)` is the
explicit "drop constants" version and matches `~`.

```stan
model      { y ~ normal(mu, sigma); }                         // constants dropped
generated quantities {
  vector[N] log_lik;
  for (n in 1:N) log_lik[n] = normal_lpdf(y[n] | mu[n], sigma);  // full density
}
```

Getting this wrong does not break sampling, but it silently corrupts LOO.

## What brms and lavaan were doing for you

Writing Stan by hand means taking back work those packages did silently:

| They handled | You now write |
|---|---|
| Non-centered hierarchical terms | `z ~ std_normal()` plus the transform |
| Weakly-informative default priors | Every prior, explicitly |
| `log_lik` for LOO | A `generated quantities` block |
| Factor identification | The constraint on `Lambda` |
| QR reparameterization of predictors | `qr_thin_Q` / `qr_thin_R` if collinearity bites |
| Sensible parameter naming for `posterior` | Names you choose |

## Validating

**LOO-CV**, computed from draws with no extra fitting:

```r
library(loo)                       # [M] this whole block executed
ll_a  <- fit_a$draws("log_lik")    # iterations x chains x observations
r_eff <- relative_eff(exp(ll_a))   # chains inferred from the array; no chain_id needed
loo_a <- loo(ll_a, r_eff = r_eff)
print(loo_a)                       # read the Pareto-k table, not just the elpd
loo_compare(loo_a, loo_b)
```

```python
import arviz as az
idata = az.from_cmdstanpy(fit, log_likelihood="log_lik")
az.compare({"model_a": idata_a, "model_b": idata_b})
```

Any Pareto-k > 0.7 means importance sampling failed for that observation and the estimate is
untrustworthy — refit those folds (`loo::reloo`) or use K-fold.

**LOO assumes exchangeable observations, so it is the wrong tool for time series** — it lets
the model see the future. Use leave-future-out / rolling-origin CV instead (Bürkner, Gabry &
Vehtari 2020) `[L]`.

For model weights prefer **stacking** over Bayesian model averaging: stacking optimizes
held-out predictive accuracy directly, while BMA weights by marginal likelihood, which is
sharply sensitive to the prior in ways that do not track prediction.
`loo::loo_model_weights(method = "stacking")`.

## The diagnostic gate

```r
fit$summary()[, c("variable", "rhat", "ess_bulk", "ess_tail")]   # [M]
fit$diagnostic_summary()     # [M] num_divergent, num_max_treedepth, ebfmi
```

`fit$summary()$rhat` is exactly `posterior::summarise_draws(draws, "rhat")`, which is the
rank-normalized split-R̂ of Vehtari et al. — `posterior` computes it independently of your
CmdStan version, so you get the modern diagnostic even on an older CmdStan whose
`bin/stansummary` does not `[M]`. Do not call `posterior::rhat()` directly on a multi-chain
`draws_array` expecting the same number; it returns a different value, and
`summarise_draws()` is the one that matches `$summary()` `[M]`.

- **R̂ > 1.01**: chains have not mixed — do not use the posterior. (1.05 is the older,
  now-inadequate threshold; Vehtari et al. 2021 tightened it and the Stan Reference Manual
  follows `[L]`.)
- **Bulk-ESS < 400** (≈100 per chain at 4 chains): not enough for reliable posterior means.
- **Tail-ESS < 400**: Bulk-ESS can look fine while the tails are badly estimated — which is
  exactly where your interval endpoints live. Check both.
- **Divergences > 0**: the sampler hit geometry it could not resolve; the posterior may be
  biased. Reparameterize, tighten priors, raise `adapt_delta`. Do not ignore a handful.
- **Thinning does not raise ESS.** It discards information. Thin only to save disk.

Everything in Part 2 is subject to this gate. A faster model that fails it is not faster, it
is broken.

---

# Part 2 — Making Them Go Brrr

Work down this list in order. Each stage is cheaper and safer than the one below it, and the
early stages often make the later ones unnecessary. Never start at the bottom: swapping in an
approximate posterior to fix a problem that was really an unvectorized loop trades correctness
for nothing.

## Stage 0 — Profile first

Most people optimize the wrong thing. Two measurements decide which lever to pull.

**Where in the model does time go?** Stan has built-in profiling `[C]`. Wrap suspect
regions:

```stan
model {
  profile("priors") {
    z ~ std_normal();
    sigma ~ normal(0, 1);
  }
  profile("likelihood") {
    vector[N] eta = X * beta;
    y ~ bernoulli_logit(eta);
  }
}
```

```r
fit <- mod$sample(data = stan_data, chains = 4, parallel_chains = 4)
fit$profiles()      # [M] columns: name, thread_id, total_time, forward_time, ...
```

This tells you which block to attack, and it is far better than guessing.

**Is it geometry or gradient cost?**

```r
fit$time()                    # [M] $total, and $chains with chain_id/warmup/sampling/total
fit$diagnostic_summary()      # [M] num_divergent, num_max_treedepth, ebfmi
leapfrogs <- sum(fit$sampler_diagnostics()[,,"n_leapfrog__"])   # [M]
sampling_seconds / leapfrogs  # cost per gradient
```

- **Many leapfrogs per iteration** (treedepth saturating at 10 means 1023 gradient
  evaluations *per draw*) → geometry problem. Reparameterize. This is the highest-payoff
  finding on the list.
- **Few leapfrogs but still slow** → each gradient is expensive. Vectorize, use GLM
  primitives, hoist constants.

These call for opposite fixes, so measuring first is not optional.

**Is it warmup or sampling?** `fit$time()` splits them. Warmup is commonly 50–70% of total,
which matters enormously for a scheduled job — see Stage 4.

## Stage 1 — Free wins, no model change

**Compiler flags.** All three confirmed present in CmdStan 2.38's makefile `[D]`:

```r
mod <- cmdstan_model("model.stan", cpp_options = list(
  STAN_CPP_OPTIMS = TRUE,        # extra optimization flags
  STAN_NO_RANGE_CHECKS = TRUE    # removes bounds checks: only once the model is debugged
))
```
All three flags (with `STAN_THREADS`) confirmed present in the CmdStan makefile at both
2.35.0 and 2.38.0 `[D]`.

Reported typically 10–30%; unverified here, so measure it on your model. `STAN_NO_RANGE_CHECKS` removes the guardrails that produce readable index
errors, so enable it only after the model is correct, and turn it off when debugging.

**Run chains in parallel.** Trivial and frequently overlooked:

```r
fit <- mod$sample(data = stan_data, chains = 4, parallel_chains = 4)
```

Four chains run serially on a multi-core box wastes roughly a 4x factor for no reason.

## Stage 2 — Cheap wins, no change to the math

**Stop sampling more than you need.** The default 4×(1000+1000) yields 4000 draws when the
diagnostic threshold is Bulk-ESS and Tail-ESS ≥ 400. If a run reports ESS in the thousands,
you are paying for precision you are not using. Halving `iter_sampling` halves that phase.
Check ESS after cutting, not before.

**Vectorize.** The loop and the vectorized form compute the same number, but the loop builds
N separate autodiff nodes:

```stan
for (n in 1:N) y[n] ~ normal(mu[n], sigma);   // N nodes
y ~ normal(mu, sigma);                        // one
```

**Hoist anything constant into `transformed data`.** Standardization, index arrays,
`log()` of fixed inputs. Computed once instead of once per leapfrog.

**Use the GLM primitives.** These have hand-written analytic gradients instead of autodiff
through the composed expression, and they are often the single largest per-gradient win.
Both compile under 2.38 `[C]`:

```stan
y  ~ ordered_logistic_glm(x, beta, cut);   // ordinal outcomes - IRT, Likert
yb ~ bernoulli_logit_glm(x, alpha, beta);  // binary outcomes
```

Also available, all four compiled `[C]`: `normal_id_glm`, `poisson_log_glm`,
`neg_binomial_2_log_glm`, and `categorical_logit_glm` (note its `alpha` is `vector[C]` and
`beta` is `matrix[K, C]`, not the row_vector shape the others might lead you to expect). If your model builds a linear predictor and feeds it to a link, there
is probably a `_glm` form for it.

**Collapse to sufficient statistics.** Repeated identical rows can be aggregated:
`bernoulli` over many trials becomes one `binomial`; in item-response data with many
respondents and few items, identical response patterns collapse to a pattern plus a count.
This can cut N by an order of magnitude with no change to the posterior.

## Stage 3 — Structural, still exact

**Within-chain threading with `reduce_sum`.** Available in 2.38 `[C]`. Partition the data
sum across threads:

```stan
functions {
  real partial(array[] real slice_y, int start, int end, vector mu, real sigma) {
    return normal_lpdf(to_vector(slice_y) | mu[start:end], sigma);
  }
}
model {
  target += reduce_sum(partial, y, grainsize, mu, sigma);
}
```

```r
mod <- cmdstan_model("model.stan", cpp_options = list(stan_threads = TRUE))
fit <- mod$sample(data = stan_data, chains = 4, parallel_chains = 4, threads_per_chain = 4)
```
Compiled and sampled with threading enabled `[M]`.

Near-linear until memory bandwidth binds — a general property of the approach, not measured
here. Budget cores as
`parallel_chains × threads_per_chain ≤ physical cores`. Start `grainsize` at 1 and let the
scheduler decide.

**Fix the geometry rather than raising `adapt_delta`.** If Stage 0 showed treedepth
saturation, the answer is non-centered parameterization, log-scale priors on positive
parameters, or a QR reparameterization for collinear predictors — not `adapt_delta = 0.999`.
Raising `adapt_delta` shrinks the step size, which *increases* the number of leapfrogs per
iteration. It buys fewer divergences at the price of a slower run, and it treats the symptom.

## Stage 4 — The lever for scheduled runs

A daily job re-learns the same metric every single day. Warmup is usually the largest block of
time, and yesterday's step size and inverse metric are very nearly right for today.

```r
prev <- readRDS("cache/fit_yesterday.rds")

# [M] Executed. $inv_metric(matrix = FALSE) returns a list of length `chains`, each a
# vector of length n_params; metadata()$step_size_adaptation is one value per chain.
# $sample() takes a single vector and a single initial step size, so collapse.
fit <- mod$sample(
  data            = stan_data_today,
  chains          = 4,
  parallel_chains = 4,
  init            = prev,                                  # fit object accepted directly
  step_size       = mean(prev$metadata()$step_size_adaptation),
  inv_metric      = prev$inv_metric(matrix = FALSE)[[1]],
  iter_warmup     = 200                                    # short, but still adapting
)
```

```python
chains = 4
par_names = model.src_info()["parameters"].keys()
inits = [{n: prev.stan_variable(n)[-(i + 1)] for n in par_names} for i in range(chains)]

fit = model.sample(
    data=stan_data_today, chains=chains, inits=inits,
    step_size=prev.step_size.tolist(),   # ndarray -> list[float]
    inv_metric=prev.inv_metric,          # `.metric` is deprecated
    iter_warmup=200,
)
```

**Keep adaptation on with a short warmup rather than setting `adapt_engaged = FALSE`.** If the
new day's data shifted the posterior, a frozen metric is wrong and you get bad sampling with
no warning. A short warmup re-checks cheaply.

**Gate it on diagnostics.** A warm-started run that comes back with R̂ > 1.01 should trigger a
cold refit, not a silent publish. In a scheduled pipeline this check is the difference between
a fast job and a fast wrong job.

Measure the gain on your own model rather than trusting a general figure; it depends entirely
on how much of your runtime is warmup.

## Stage 5 — Scaling when K is large

**Factor models** replace a K×K covariance (O(K²) parameters) with R << K latent factors
(O(R×K)):

```text
y_t = Lambda * f_t + eps_t,   f_t ~ N(0, I_R)
Cov(y_t) = Lambda * Lambda' + diag(psi)
```

K=50, R=5 is 250 parameters instead of 2500. Constrain `Lambda` for identification (Part 1).
This is factor analysis in the SEM sense; multidimensional IRT is its categorical-outcome
counterpart, with discriminations playing the role of loadings.

**Regularized horseshoe** for sparse coefficient matrices — use the regularized form (Piironen
& Vehtari 2017), not the original, whose unbounded Cauchy local scales create a funnel NUTS
handles badly:

```stan
data {
  int<lower=1> P;
  real<lower=0> tau_0;         // from expected sparsity, not arbitrary
}
parameters {
  real<lower=0> tau;           // global scale
  vector<lower=0>[P] lambda;   // local scales
  real<lower=0> c2;            // slab width: bounds how large a "large" coefficient gets
  vector[P] z;
}
transformed parameters {
  vector[P] lambda_tilde = sqrt(c2 * square(lambda)
                                ./ (c2 + square(tau) * square(lambda)));
  vector[P] beta = z .* (tau * lambda_tilde);
}
model {
  z      ~ std_normal();
  lambda ~ cauchy(0, 1);
  tau    ~ cauchy(0, tau_0);
  c2     ~ inv_gamma(2, 8);    // ~ Student-t(4, 0, 2) slab
  // ... likelihood in terms of beta
}
```

Set `tau_0` from the number of coefficients you expect to be nonzero. The Minnesota prior is a
fixed, non-adaptive version of the same idea.

## Stage 6 — Trading exactness, last resort

Only after Stages 0–4. These change the answer, so validate against a full MCMC fit on at
least one representative dataset before shipping, and re-validate periodically.

**Pathfinder.** L-BFGS from multiple starting points, then importance-resampled draws. Cheap.
Best used as an initializer; usable standalone when you need speed over calibration.

```r
pf  <- mod$pathfinder(data = stan_data)
fit <- mod$sample(data = stan_data, chains = 4, init = pf)   # fit object accepted directly [M]
```

```python
approx = model.pathfinder(data=stan_data)
fit = model.sample(data=stan_data, chains=4, inits=approx.create_inits(chains=4))
```

cmdstanr's `init` takes a fit object directly — `CmdStanMCMC`, `CmdStanMLE`, `CmdStanVB`,
`CmdStanPathfinder`, `CmdStanLaplace`, or a `posterior::draws`; passing both a `CmdStanMCMC`
and a `CmdStanPathfinder` is executed and works `[M]`. cmdstanpy's `inits` accepts only a
number, a dict, a JSON/Rdump path, or a list of those, hence `create_inits()` `[D]`.

**Laplace approximation.** Gaussian at the posterior mode, using the Hessian there.

```r
mode <- mod$optimize(data = stan_data, jacobian = TRUE)   # jacobian=TRUE for the true mode
lap  <- mod$laplace(data = stan_data, mode = mode)        # [M]
```

Set `jacobian = TRUE`: you want the mode on the unconstrained scale, which is what the
approximation is built around.

**ADVI last.** Stan prints `EXPERIMENTAL ALGORITHM:` when you run `$variational()`, and it
means it — ADVI fails unpredictably on hierarchical models, often without obvious symptoms.
Prefer Pathfinder or Laplace.

**Prophet, for reference**, defaults to MAP only (`optimize`, L-BFGS falling back to Newton)
`[D]` — its own docstring says *"If 0, will do MAP estimation… only the uncertainty in the
trend"*.
Its intervals come from simulating future changepoints and observation noise; parameter
uncertainty is not propagated unless you set `mcmc_samples > 0`. Its internal
`np.random.laplace` draws changepoint magnitudes from the Laplace *distribution* and is
unrelated to the Laplace approximation.

## Stage 7 — When the structure lets you skip MCMC

**Kalman filter.** Any linear Gaussian ARIMA model is exactly a state-space model, and the
Kalman filter gives the **exact posterior** `p(x_t | y_1..y_t)` in O(K³) per time step — no
sampling, just matrix algebra. One predict step plus one update step per new observation, so a
daily run costs one update rather than a refit.

```text
State:       x_t = B * x_{t-1} + w_t
Observation: y_t = H * x_t     + v_t
```

For VARIMA, stack lagged innovations into the state for MA terms; regressors are known inputs;
Fourier seasonality folds into the state. Breaks on non-Gaussian errors or nonlinear dynamics.

**Particle filters / SMC** generalize this to nonlinear and non-Gaussian models at
O(N_particles) per step. Watch for particle degeneracy: in high dimensions the effective
particle count collapses and the filter reports overconfident results without complaining.

**Conjugate updating.** Gaussian-Gaussian, Beta-Binomial, Gamma-Poisson give closed-form exact
posterior updates at O(1). Limited to specific families, unbeatable when they apply.

**Amortized inference / SBI.** One expensive training run of a neural network mapping data →
posterior parameters, then instant inference on new data from the same generative process.
Worth considering only when you will run inference very many times.

---

## Quick decision guide

| Situation | Do this |
|---|---|
| Model is slow and you have not measured | `fit$profiles()`, `fit$time()`, treedepth — Stage 0 |
| Treedepth saturating | Reparameterize; do not raise `adapt_delta` |
| Slow gradients, few leapfrogs | Vectorize, GLM primitives, hoist to `transformed data` |
| Multi-core box, one chain at a time | `parallel_chains`, then `reduce_sum` |
| ESS in the thousands | Cut `iter_sampling` |
| Scheduled or daily run | Warm-start: `init`, `step_size`, `inv_metric`, short warmup |
| Repeated identical observations | Collapse to sufficient statistics |
| K > 20 series | Factor model or regularized horseshoe |
| Still too slow, exactness negotiable | Pathfinder, then Laplace; ADVI last |
| Model is linear Gaussian | Kalman filter — exact, no MCMC |
| Divergences after any change | Stop; the diagnostic gate outranks the speedup |
| Comparing two models | LOO-CV, read Pareto-k |
| Comparing two time series models | Leave-future-out CV, not LOO |

---

## Provenance and re-verification

**Verified 2026-09-03** in two environments. Claims that hold in both are version-robust
across at least CmdStan 2.35–2.38.

| Environment | Components |
|---|---|
| Container `jflournoy/verse-cmdstan` | R 4.3.2, cmdstanr 0.8.0, CmdStan 2.35.0, posterior 1.6.1, loo 2.9.0.9000, brms 2.23.1 |
| Local host | stanc3 2.38.0, cmdstanpy 1.3.0, prophet 1.3.0 |

The container has no cmdstanpy, so **every Python driver call remains `[D]`** — checked
against cmdstanpy 1.3.0 source, never executed. Every R driver call in this guide was
executed in the container.

**Power-scaling was verified separately, 2026-09-05.** priorsense is not installed in
`jflournoy/verse-cmdstan-hcpd`; it was installed into a throwaway container at **1.2.0** and
run there. `powerscale_sensitivity()` on the package's own
`example_powerscale_model("univariate_normal")` produced the table shown; the `diagnosis`
rule, `sensitivity_threshold = 0.05`, `lower_alpha`/`upper_alpha` and `div_measure` defaults
were read from the function source and signature rather than from the documentation. The
per-chain init values were measured in the same image under cmdstanr. The `generated
quantities` block was compiled by stanc3 2.38.0 on the host, as a complete program.

**Executed R API surface** (cmdstanr 0.8.0 / CmdStan 2.35.0), with what was observed:

| Call | Observed |
|---|---|
| `fit$time()` | `$total`; `$chains` with `chain_id, warmup, sampling, total` |
| `fit$profiles()` | per-block rows; columns `name, thread_id, total_time, forward_time, …` |
| `fit$diagnostic_summary()` | `num_divergent, num_max_treedepth, ebfmi` |
| `fit$summary()` | includes `rhat, ess_bulk, ess_tail` |
| `fit$metadata()$step_size_adaptation` | numeric, length = chains |
| `fit$inv_metric(matrix = FALSE)` | list, length = chains; each a vector of length n_params |
| `fit$sampler_diagnostics()[,,"n_leapfrog__"]` | summable |
| `mod$sample(init = <CmdStanMCMC>)` | accepted; warm start ran |
| `mod$pathfinder()` → `mod$sample(init = pf)` | `CmdStanPathfinder`; accepted as `init` |
| `mod$optimize(jacobian = TRUE)` → `mod$laplace(mode = )` | `CmdStanMLE` → `CmdStanLaplace` |
| `loo::relative_eff(exp(ll))` + `loo::loo()` | ran; Pareto-k reported per observation |

Also confirmed under **both** 2.35.0 and 2.38.0: all four complete Stan programs in this file
compile; the six GLM primitives compile; `reduce_sum` compiles and samples with
`threads_per_chain`; `profile()` blocks work; `STAN_CPP_OPTIMS`, `STAN_THREADS` and
`STAN_NO_RANGE_CHECKS` are present in the makefile.

**How each marker was established.**

- `[C]` — the snippet was written to a file and run through `stanc`. Fragments that are not
  whole programs (illustrative pairs, `functions` blocks) were scaffolded with a minimal
  `data`/`parameters`/`model` before checking.
- `[M]` — a full program was compiled and sampled under CmdStan 2.38.0, and the number was
  read from `stansummary` or from the output CSV. Correlation-prior figures additionally
  cross-checked against an independent simulation of the same transform, which agreed to
  three decimals.
- `[D]` — read from the installed library source (cmdstanpy, prophet) or the published
  reference (cmdstanr `$sample`, `$inv_metric`, `$metadata`; Stan Reference Manual). **Not
  executed.** This is the class most likely to drift as libraries change.
- `[L]` — from the cited paper or manual section.

**Known gaps.**

- **Python is unexecuted.** No cmdstanpy in the container; all `[D]`.
- **The Stage 1 percentages are reported, not measured.** Compiler-flag speedups depend on
  the model; measure yours.
- **Nothing in Part 2 is benchmarked end-to-end.** The *ordering* of the stages is a reasoned
  argument from where time goes, not an empirical ranking on a real workload.
- **`[M]` means the call ran and returned the stated shape**, on small toy models. It does not
  mean the surrounding advice was benchmarked.

**Re-checking the Stan.** Every `[C]` and `[M]` claim rests on code in this file, so it can be
re-verified directly. This extracts each complete program and compiles it:

```bash
python3 - <<'EOF'
import re, subprocess, tempfile, os
STANC = os.path.expanduser("~/.cmdstan/cmdstan-2.38.0/bin/stanc")
src = open("bayesian-production.md").read()
for i, b in enumerate(re.findall(r"```stan\n(.*?)```", src, re.S), 1):
    if not ("parameters" in b and "model" in b and "data" in b):
        print(f"block {i}: fragment (scaffold to check)"); continue
    with tempfile.NamedTemporaryFile("w", suffix=".stan", delete=False) as f:
        f.write(b); path = f.name
    r = subprocess.run([STANC, "--o=/dev/null", path], capture_output=True, text=True)
    err = (r.stdout + r.stderr).strip()
    print(f"block {i}: {'OK' if not err else 'FAIL -> ' + err[:200]}")
    os.unlink(path)
EOF
```

Run this after any edit, and after any CmdStan upgrade. A `[C]` marker that no longer holds is
a bug in this file, not in your model.

**Re-checking the R.** The container is the reference environment, so the R claims are
reproducible without installing anything:

```bash
docker run --rm -v "$PWD":/w -w /w jflournoy/verse-cmdstan:latest Rscript your-check.R
```

Compile any block from this file inside it with
`cmdstan_model(f, compile = FALSE)$check_syntax()`, which is much faster than a full compile.
Note the container is CmdStan **2.35.0** while the host used above is 2.38.0 — if a feature
works in one and not the other, the guide should say which, rather than picking a side.

**When updating this guide:** move a claim's marker down, never up, unless you actually redid
the work. Promoting `[D]` to `[M]` without measuring is how a guide starts lying.

## References

- Stan User's Guide — Efficiency Tuning; Parallelization (`reduce_sum`)
- Stan Reference Manual — Constraint Transforms; Posterior Analysis (R̂, Bulk/Tail-ESS)
- Stan Functions Reference — GLM primitives
- Betancourt, *A Conceptual Introduction to Hamiltonian Monte Carlo* (2017)
- Betancourt & Girolami (2015) — HMC for hierarchical models; when centered beats non-centered
- Gelman et al., *Bayesian Data Analysis* (3rd ed.)
- Durbin & Koopman, *Time Series Analysis by State Space Methods*
- Piironen & Vehtari (2017) — regularized horseshoe
- Vehtari, Gelman, Simpson, Carpenter & Bürkner (2021) — improved R̂, Bulk/Tail-ESS,
  *Bayesian Analysis* 16:667–718
- Vehtari, Gelman & Gabry (2017) — practical Bayesian model evaluation using LOO and WAIC
- Bürkner, Gabry & Vehtari (2020) — approximate leave-future-out CV for time series
- Kallioinen, Paananen, Bürkner & Vehtari (2024) — detecting and diagnosing prior and
  likelihood sensitivity with power-scaling, *Statistics and Computing* 34:57
- Yao et al. (2018) — stacking vs BMA
- Zhang et al. (2022) — Pathfinder algorithm
