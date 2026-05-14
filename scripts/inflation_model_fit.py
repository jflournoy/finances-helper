"""Fit inflation_model_time_varying.stan and summarize results.

Usage:
    uv run python scripts/inflation_model_fit.py

Reads:  data/cache/inflation_model_data.json
        data/cache/inflation_model_items.csv
        scripts/inflation_model_time_varying.stan
Writes: data/cache/inflation_model_fit/                       (CmdStan CSV output)
        data/cache/inflation_model_summary.csv                (per-item rates)
        data/cache/inflation_model_pop_trajectory.csv         (pop spline + CIs)
        data/cache/inflation_model_pop_trajectory_draws.csv   (long-format draws)
        data/cache/inflation_model_item_effects.csv           (per-item alpha/gamma)
        data/cache/inflation_model_param_draws.csv            (scalar param draws)
        images/inflation_model_trajectories.html
"""

import json
import numpy as np
import pandas as pd
from datetime import date
from pathlib import Path

import cmdstanpy
import arviz as az


BASE_DATE = date(2020, 9, 1)


def months_to_date(t: float) -> date:
    total_months = int(round(t))
    year = BASE_DATE.year + (BASE_DATE.month - 1 + total_months) // 12
    month = (BASE_DATE.month - 1 + total_months) % 12 + 1
    return date(year, month, 1)


def main() -> None:
    cache_dir = Path("data/cache")
    images_dir = Path("images")
    images_dir.mkdir(exist_ok=True)

    data_path = cache_dir / "inflation_model_data.json"
    if not data_path.exists():
        raise FileNotFoundError(
            f"{data_path} not found — run inflation_model_prep.py first"
        )

    stan_data = json.loads(data_path.read_text())
    items = pd.read_csv(cache_dir / "inflation_model_items.csv")
    K = stan_data["K"]
    print(f"N={stan_data['N']}  I={stan_data['I']} items (before filter)  K={K}")

    # Filter to items with enough observations to estimate a slope (>= 2 obs).
    # Note: B is N x K (population basis), so we only filter rows.
    MIN_OBS = 2
    keep_old_idx = sorted(items.loc[items["n_obs"] >= MIN_OBS, "item_idx"].tolist())
    if keep_old_idx and len(keep_old_idx) < stan_data["I"]:
        old_to_new = {old: new + 1 for new, old in enumerate(keep_old_idx)}
        keep_set = set(keep_old_idx)
        row_mask = np.array([ii in keep_set for ii in stan_data["ii"]])

        B_full = np.array(stan_data["B"])
        log_mean_full = np.array(stan_data["log_mean_per_item"])

        stan_data["ii"]    = [old_to_new[stan_data["ii"][n]]
                              for n in range(stan_data["N"]) if row_mask[n]]
        stan_data["t"]     = [stan_data["t"][n]
                              for n in range(stan_data["N"]) if row_mask[n]]
        stan_data["price"] = [stan_data["price"][n]
                              for n in range(stan_data["N"]) if row_mask[n]]
        stan_data["y"]     = [stan_data["y"][n]
                              for n in range(stan_data["N"]) if row_mask[n]]
        stan_data["B"]     = B_full[row_mask].tolist()
        stan_data["log_mean_per_item"] = log_mean_full[[i - 1 for i in keep_old_idx]].tolist()
        stan_data["N"]     = len(stan_data["ii"])
        stan_data["I"]     = len(keep_old_idx)
        items = items[items["item_idx"].isin(keep_old_idx)].copy()
        items["item_idx"] = items["item_idx"].map(old_to_new)
        print(f"N={stan_data['N']}  I={stan_data['I']} items "
              f"(after n_obs >= {MIN_OBS} filter)")

    I = stan_data["I"]
    K = stan_data["K"]
    B = np.array(stan_data["B"])                       # (N, K)
    t_arr = np.array(stan_data["t"])
    y_arr = np.array(stan_data["y"])                   # centered log-price
    ii_init = np.array(stan_data["ii"]) - 1            # 0-based
    sigma_fs_prior_sd = np.array(stan_data["sigma_fs_prior_sd"])

    # --- Data-informed inits ---
    # The model fits normalized log-prices. We init:
    #   1) beta_pop from LS on all normalized data
    #   2) beta_i from ridge LS on item residuals
    print("\nComputing data-informed inits...")

    # Step 1: population spline beta_pop = lstsq on normalized data
    beta_pop_init, *_ = np.linalg.lstsq(B, y_arr, rcond=None)
    np.clip(beta_pop_init, -1.0, 1.0, out=beta_pop_init)

    # Step 2: per-item beta_i = ridge fit of residuals on B for that item
    pop_pred = B @ beta_pop_init
    resid_after_pop = y_arr - pop_pred
    # Ridge per coefficient direction inversely proportional to prior_sd^2 (so
    # tight priors → strong shrinkage, loose priors → weak shrinkage).
    ridge_diag = np.diag(1.0 / (sigma_fs_prior_sd ** 2 + 1e-8))
    beta_i_init = np.zeros((I, K))
    for i in range(I):
        mask_i = (ii_init == i)
        n_i = int(mask_i.sum())
        if n_i == 0:
            continue
        X_i = B[mask_i]
        y_i = resid_after_pop[mask_i]
        # Ridge: solve (X'X + ridge_diag) beta = X'y
        A = X_i.T @ X_i + ridge_diag
        beta_i_init[i] = np.linalg.solve(A, X_i.T @ y_i)

    # Defensive clip per coefficient direction (roughly 3 sigma from prior)
    for k in range(K):
        bound = 3.0 * sigma_fs_prior_sd[k]
        np.clip(beta_i_init[:, k], -bound, bound, out=beta_i_init[:, k])

    # Init sigma_fs from empirical SD of beta_i across items, clipped to prior magnitude
    sigma_fs_init = np.zeros(K)
    for k in range(K):
        emp = float(np.std(beta_i_init[:, k]))
        sigma_fs_init[k] = max(emp, 0.5 * sigma_fs_prior_sd[k])
        sigma_fs_init[k] = min(sigma_fs_init[k], 1.5 * sigma_fs_prior_sd[k])

    # beta_i_raw = beta_i / sigma_fs (broadcast per-column)
    beta_i_raw_init = beta_i_init / sigma_fs_init[None, :]

    pred_init = pop_pred + np.einsum("nk,nk->n", B, beta_i_init[ii_init])
    sigma_init = max(float(np.std(y_arr - pred_init)), 0.05)

    print(f"  beta_pop init: {beta_pop_init.round(3)}")
    print(f"  beta_i shrunk to per-coef bounds:")
    for k in range(K):
        n_clipped = int(np.sum(np.abs(beta_i_init[:, k]) >= 3.0 * sigma_fs_prior_sd[k] - 1e-6))
        print(f"    coef {k+1}: range=[{beta_i_init[:, k].min():+.3f}, {beta_i_init[:, k].max():+.3f}]  "
              f"std={beta_i_init[:, k].std():.3f}  prior_sd={sigma_fs_prior_sd[k]:.3f}  "
              f"clipped={n_clipped}/{I}")
    print(f"  sigma_fs init: {sigma_fs_init.round(4)}")
    print(f"  sigma init: {sigma_init:.3f}")

    def make_inits():
        return {
            "beta_pop":     beta_pop_init,
            "beta_i_raw":   beta_i_raw_init,
            "sigma_fs":     sigma_fs_init,
            "sigma":        sigma_init,
            "lambda_pop":   2.0,
        }

    model = cmdstanpy.CmdStanModel(
        stan_file="scripts/inflation_model_time_varying.stan",
        cpp_options={"STAN_THREADS": "true"},
    )

    fit_dir = cache_dir / "inflation_model_fit"
    fit_dir.mkdir(exist_ok=True)
    fit = model.sample(
        data=stan_data,
        chains=4,
        parallel_chains=4,
        iter_warmup=1000,
        iter_sampling=1000,
        seed=42,
        inits=make_inits(),
        output_dir=str(fit_dir),
        show_progress=True,
        max_treedepth=12,
        adapt_delta=0.99,
    )

    print(fit.diagnose())

    # --- Posterior trajectories ---
    beta_pop_draws = fit.stan_variable("beta_pop")     # (draws, K)
    beta_i_draws   = fit.stan_variable("beta_i")       # (draws, I, K)
    sigma_draws    = fit.stan_variable("sigma")
    sigma_fs_draws = fit.stan_variable("sigma_fs")     # (draws, K)
    log_mean_per_item = np.array(stan_data["log_mean_per_item"])  # (I,) for un-normalizing
    n_draws        = beta_pop_draws.shape[0]

    # Population trajectory and inflation on a fine grid (integer months 0..T_max)
    T_max = int(np.ceil(t_arr.max()))
    t_grid = np.arange(0, T_max + 1, dtype=float)
    dates  = [months_to_date(t) for t in t_grid]

    # Re-evaluate basis on the grid by linear interpolation from observed-time basis.
    from scipy.interpolate import interp1d
    t_unique, t_unique_idx = np.unique(t_arr, return_index=True)
    B_unique = B[t_unique_idx]
    if t_grid.min() < t_unique.min() or t_grid.max() > t_unique.max():
        t_grid = np.clip(t_grid, t_unique.min(), t_unique.max())
    B_grid = np.zeros((len(t_grid), K))
    for k in range(K):
        f = interp1d(t_unique, B_unique[:, k], kind="linear")
        B_grid[:, k] = f(t_grid)


    # Population log-price trajectory: B_grid @ beta_pop (the constant mode of
    # beta_pop carries the population mean, so no separate intercept).
    pop_log_price = beta_pop_draws @ B_grid.T               # (draws, T)

    # Population inflation rate from numerical derivative
    pop_infl_rate_t = np.diff(pop_log_price, axis=1) * 12   # (draws, T-1) annualized
    pop_infl_rate_overall = np.mean(pop_infl_rate_t, axis=1)

    print("\nPopulation-average annualized inflation rate (mean over time):")
    print(f"  median: {np.median(pop_infl_rate_overall)*100:.2f}%/yr")
    print(f"  90% CI: {np.percentile(pop_infl_rate_overall, 5)*100:.2f}% – "
          f"{np.percentile(pop_infl_rate_overall, 95)*100:.2f}%/yr")

    # --- Posterior predictive checks ---
    # 1) Coverage of the 50% / 90% predictive intervals (should be ~0.5 / ~0.9)
    # 2) Standardized residuals: (y - mu) / sigma should be ~N(0,1)
    # 3) Posterior predictive density vs observed density
    y_rep = fit.stan_variable("y_rep")                  # (draws, N)
    mu_draws = fit.stan_variable("mu")                  # (draws, N)
    y_obs = np.array(stan_data["y"])

    p05 = np.percentile(y_rep, 5,  axis=0)
    p25 = np.percentile(y_rep, 25, axis=0)
    p75 = np.percentile(y_rep, 75, axis=0)
    p95 = np.percentile(y_rep, 95, axis=0)
    cover_90 = float(((y_obs >= p05) & (y_obs <= p95)).mean())
    cover_50 = float(((y_obs >= p25) & (y_obs <= p75)).mean())

    mu_med = np.median(mu_draws, axis=0)
    sigma_med = float(np.median(sigma_draws))
    std_resid = (y_obs - mu_med) / sigma_med

    print("\nPosterior predictive checks:")
    print(f"  Empirical coverage of 50% predictive interval: {cover_50:.3f}  (nominal 0.50)")
    print(f"  Empirical coverage of 90% predictive interval: {cover_90:.3f}  (nominal 0.90)")
    print(f"  Standardized residual (y - mu) / sigma:")
    print(f"    mean: {std_resid.mean():+.3f}  (nominal 0.0)")
    print(f"    sd:   {std_resid.std():.3f}  (nominal 1.0)")
    print(f"    %|>2sd|:  {(np.abs(std_resid) > 2).mean()*100:.1f}%  (nominal ~5%)")
    print(f"    %|>3sd|:  {(np.abs(std_resid) > 3).mean()*100:.2f}%  (nominal ~0.27%)")

    # Save PPC summary for downstream R-friendly analysis
    ppc_df = pd.DataFrame({
        "y_obs":      y_obs,
        "mu_med":     mu_med,
        "y_rep_p05":  p05,
        "y_rep_p25":  p25,
        "y_rep_p75":  p75,
        "y_rep_p95":  p95,
        "std_resid":  std_resid,
        "in_50":      ((y_obs >= p25) & (y_obs <= p75)).astype(int),
        "in_90":      ((y_obs >= p05) & (y_obs <= p95)).astype(int),
    })
    ppc_path = cache_dir / "inflation_model_ppc.csv"
    ppc_df.to_csv(ppc_path, index=False)
    print(f"  Saved PPC table: {ppc_path}")

    # Per-item annualized inflation: average d/dt of (B_grid @ (beta_pop + beta_i[i]))
    # over the time range, which equals population rate + average d/dt(B_grid @ beta_i[i])
    dB_grid = np.diff(B_grid, axis=0) * 12                  # (T-1, K) annualized basis derivative
    item_extra_rate_per_t = beta_i_draws @ dB_grid.T        # (draws, I, T-1) extra rate over time
    item_extra_rate_overall = item_extra_rate_per_t.mean(axis=2)  # (draws, I) avg over time
    inf_i_draws = pop_infl_rate_overall[:, None] + item_extra_rate_overall

    items["inflation_median"] = np.median(inf_i_draws, axis=0)
    items["inflation_p05"]    = np.percentile(inf_i_draws, 5,  axis=0)
    items["inflation_p95"]    = np.percentile(inf_i_draws, 95, axis=0)
    items = items.sort_values("inflation_median", ascending=False)

    summary_path = cache_dir / "inflation_model_summary.csv"
    items.to_csv(summary_path, index=False)
    print(f"\nSaved per-item summary: {summary_path}")
    print(items[["name", "n_obs", "inflation_median", "inflation_p05", "inflation_p95"]]
          .head(20).to_string(index=False))

    # --- LOO ---
    idata = az.from_cmdstanpy(fit, log_likelihood="log_lik")
    loo = az.loo(idata)
    print(f"\nLOO: {loo}")

    # --- R-friendly CSV outputs ---
    # 1) Population trajectory: per grid point, median + 90% CI for log_price,
    #    price_dollars, and annualized inflation rate.
    pop_log_price_med  = np.median(pop_log_price, axis=0)
    pop_log_price_p05  = np.percentile(pop_log_price, 5, axis=0)
    pop_log_price_p95  = np.percentile(pop_log_price, 95, axis=0)
    pop_price_med      = np.exp(pop_log_price_med)
    pop_price_p05      = np.exp(pop_log_price_p05)
    pop_price_p95      = np.exp(pop_log_price_p95)
    # Inflation rate (annualized) at each grid point — central differences of pop_log_price
    pop_infl_t = np.zeros_like(pop_log_price)
    pop_infl_t[:, 1:-1] = (pop_log_price[:, 2:] - pop_log_price[:, :-2]) * 6  # (per-month delta) * 12 / 2
    pop_infl_t[:, 0]    = (pop_log_price[:, 1] - pop_log_price[:, 0]) * 12
    pop_infl_t[:, -1]   = (pop_log_price[:, -1] - pop_log_price[:, -2]) * 12

    pop_traj_df = pd.DataFrame({
        "t_months":            t_grid,
        "date":                [d.isoformat() for d in dates],
        "log_price_med":       pop_log_price_med,
        "log_price_p05":       pop_log_price_p05,
        "log_price_p95":       pop_log_price_p95,
        "price_dollars_med":   pop_price_med,
        "price_dollars_p05":   pop_price_p05,
        "price_dollars_p95":   pop_price_p95,
        "infl_annualized_med": np.median(pop_infl_t, axis=0),
        "infl_annualized_p05": np.percentile(pop_infl_t, 5, axis=0),
        "infl_annualized_p95": np.percentile(pop_infl_t, 95, axis=0),
    })
    pop_traj_path = cache_dir / "inflation_model_pop_trajectory.csv"
    pop_traj_df.to_csv(pop_traj_path, index=False)
    print(f"Saved population trajectory: {pop_traj_path}")

    # 2) Per-item factor-smooth coefficients with CIs
    # Save per-coefficient median + 90% CI of beta_i. Coefficients map onto
    # null-space directions (constant, linear) and wiggle modes; the prep script
    # already established which is which via sigma_fs_prior_sd.
    item_effects = items.copy()
    for k in range(K):
        coef_med = np.median(beta_i_draws[:, :, k], axis=0)
        coef_p05 = np.percentile(beta_i_draws[:, :, k], 5,  axis=0)
        coef_p95 = np.percentile(beta_i_draws[:, :, k], 95, axis=0)
        item_effects[f"beta_i_{k+1}_med"] = coef_med
        item_effects[f"beta_i_{k+1}_p05"] = coef_p05
        item_effects[f"beta_i_{k+1}_p95"] = coef_p95
    # Item-level annualized inflation rate deviation (from population)
    item_effects["item_extra_rate_med"] = np.median(item_extra_rate_overall, axis=0)
    item_effects["item_extra_rate_p05"] = np.percentile(item_extra_rate_overall, 5,  axis=0)
    item_effects["item_extra_rate_p95"] = np.percentile(item_extra_rate_overall, 95, axis=0)
    item_effects["log_geo_mean_price"] = log_mean_per_item
    item_effects_path = cache_dir / "inflation_model_item_effects.csv"
    item_effects.to_csv(item_effects_path, index=False)
    print(f"Saved per-item effects: {item_effects_path}")

    # 3) Posterior draws of scalar parameters in long format
    draws_df_data = {
        "draw":              np.arange(n_draws),
        "sigma":             sigma_draws,
        "lambda_pop":        fit.stan_variable("lambda_pop"),
        "pop_infl_overall":  pop_infl_rate_overall,
    }
    for k in range(K):
        draws_df_data[f"sigma_fs_{k+1}"] = sigma_fs_draws[:, k]
        draws_df_data[f"beta_pop_{k+1}"] = beta_pop_draws[:, k]
    draws_df = pd.DataFrame(draws_df_data)
    draws_path = cache_dir / "inflation_model_param_draws.csv"
    draws_df.to_csv(draws_path, index=False)
    print(f"Saved scalar parameter draws: {draws_path}")

    # 4) Posterior draws of the population trajectory (long format: draw x t)
    pop_traj_draws_df = pd.DataFrame({
        "draw":          np.repeat(np.arange(n_draws), len(t_grid)),
        "t_months":      np.tile(t_grid, n_draws),
        "log_price":     pop_log_price.flatten(),
        "infl_annualized": pop_infl_t.flatten(),
    })
    pop_traj_draws_path = cache_dir / "inflation_model_pop_trajectory_draws.csv"
    pop_traj_draws_df.to_csv(pop_traj_draws_path, index=False)
    print(f"Saved population trajectory draws: {pop_traj_draws_path}")

    # --- Per-item trajectory HTML report ---
    top_items = items.nlargest(20, "n_obs")
    html_sections = []

    for _, row in top_items.iterrows():
        i = int(row["item_idx"]) - 1

        # log_price_i(t) = log_mean_i + B(t) . (beta_pop + beta_i[i])
        item_combined = beta_pop_draws + beta_i_draws[:, i, :]            # (draws, K)
        item_log_price = (
            log_mean_per_item[i]
            + item_combined @ B_grid.T
        )                                                                 # (draws, T)
        traj = np.exp(item_log_price)
        med  = np.median(traj, axis=0)
        p05  = np.percentile(traj, 5, axis=0)
        p95  = np.percentile(traj, 95, axis=0)

        noise = np.random.normal(
            loc=0.0,
            scale=sigma_draws[:, None],
            size=item_log_price.shape,
        )
        traj_rep = np.exp(item_log_price + noise)
        pred_p05 = np.percentile(traj_rep, 5, axis=0)
        pred_p95 = np.percentile(traj_rep, 95, axis=0)

        date_strs = [d.isoformat() for d in dates]
        inf_pct = row["inflation_median"] * 100
        inf_lo  = row["inflation_p05"] * 100
        inf_hi  = row["inflation_p95"] * 100

        html_sections.append(f"""
<div class="item">
  <h3>{row['name'][:80]}</h3>
  <p class="meta">ASIN {row['asin']} &middot; {int(row['n_obs'])} observations &middot;
  inflation {inf_pct:.1f}% (90%&nbsp;CI: {inf_lo:.1f}%&ndash;{inf_hi:.1f}%)/yr</p>
  <table class="traj">
    <thead><tr><th>Date</th><th>Median $</th><th>90% credible</th><th>90% predictive</th></tr></thead>
    <tbody>
""" + "".join(
            f"<tr><td>{date_strs[j]}</td><td>${med[j]:.2f}</td>"
            f"<td>${p05[j]:.2f}–${p95[j]:.2f}</td>"
            f"<td>${pred_p05[j]:.2f}–${pred_p95[j]:.2f}</td></tr>"
            for j in range(0, len(dates), 3)  # quarterly rows
        ) + """
    </tbody>
  </table>
</div>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Inflation Model — Per-item Trajectories</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1.5rem; color: #222; line-height: 1.6; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; color: #444; margin-top: 2rem; }}
  h3 {{ font-size: 0.95rem; margin-bottom: 0.1rem; }}
  .meta {{ font-size: 0.8rem; color: #888; margin-top: 0; margin-bottom: 0.5rem; }}
  .stat {{ background: #f5f8ff; border: 1px solid #c8d8f0; border-radius: 8px; padding: 0.8rem 1.2rem; margin-bottom: 1.5rem; font-size: 0.95rem; }}
  .item {{ border-bottom: 1px solid #eee; margin-bottom: 1.5rem; padding-bottom: 1rem; }}
  table.traj {{ border-collapse: collapse; width: 100%; font-size: 0.82rem; margin-bottom: 0.5rem; }}
  table.traj th {{ background: #f0f0f0; padding: 0.3rem 0.6rem; text-align: left; border-bottom: 2px solid #ccc; }}
  table.traj td {{ padding: 0.25rem 0.6rem; border-bottom: 1px solid #eee; }}
  table.traj tr:nth-child(even) {{ background: #fafafa; }}
  .footer {{ font-size: 0.75rem; color: #aaa; margin-top: 2rem; border-top: 1px solid #eee; padding-top: 0.5rem; }}
</style>
</head>
<body>
<h1>Household Grocery Inflation Model</h1>
<div class="stat">
  <strong>Population-average inflation rate:</strong>
  {np.median(pop_infl_rate_overall)*100:.2f}%/yr
  (90% CI: {np.percentile(pop_infl_rate_overall, 5)*100:.2f}% – {np.percentile(pop_infl_rate_overall, 95)*100:.2f}%/yr)<br>
  <strong>Items:</strong> {stan_data['I']} &middot;
  <strong>Observations:</strong> {stan_data['N']} &middot;
  <strong>Base date:</strong> {BASE_DATE.isoformat()}
</div>
<h2>Per-item trajectories (top 20 by observation count)</h2>
{"".join(html_sections)}
<p class="footer">Generated {date.today().isoformat()} &middot;
finances-helper / scripts/inflation_model_fit.py</p>
</body>
</html>"""

    out = images_dir / "inflation_model_trajectories.html"
    out.write_text(html, encoding="utf-8")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
