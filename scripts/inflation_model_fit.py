"""Fit inflation_model_time_varying.stan and summarize results.

Usage:
    uv run python scripts/inflation_model_fit.py

Reads:  data/cache/inflation_model_data.json
        data/cache/inflation_model_items.csv
        scripts/inflation_model_time_varying.stan
Writes: data/cache/inflation_model_fit/   (CmdStan CSV output)
        data/cache/inflation_model_summary.csv
        images/inflation_model_trajectories.html
"""

import json
import numpy as np
import pandas as pd
from datetime import date
from pathlib import Path

import cmdstanpy
import arviz as az


BASE_DATE = date(2020, 12, 1)


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
    print(f"N={stan_data['N']}  I={stan_data['I']} items (before filter)")

    # Filter to items with enough observations to be useful
    MIN_OBS = 3
    keep_idx = set(items.loc[items["n_obs"] >= MIN_OBS, "item_idx"].tolist())
    if keep_idx:
        mask = [stan_data["ii"][n] in keep_idx for n in range(stan_data["N"])]
        # remap item indices to contiguous 1-based
        old_to_new = {old: new + 1 for new, old in enumerate(sorted(keep_idx))}
        stan_data["ii"]    = [old_to_new[stan_data["ii"][n]] for n in range(stan_data["N"]) if mask[n]]
        stan_data["t"]     = [stan_data["t"][n]     for n in range(stan_data["N"]) if mask[n]]
        stan_data["price"] = [stan_data["price"][n] for n in range(stan_data["N"]) if mask[n]]
        stan_data["B"]     = [stan_data["B"][n]     for n in range(stan_data["N"]) if mask[n]]
        stan_data["N"]     = len(stan_data["ii"])
        stan_data["I"]     = len(keep_idx)
        items = items[items["item_idx"].isin(keep_idx)].copy()
        items["item_idx"] = items["item_idx"].map(old_to_new)
        print(f"N={stan_data['N']}  I={stan_data['I']} items (after n_obs >= {MIN_OBS} filter)")

    log_p0_pop_init = float(np.log(np.mean(stan_data["price"])))
    I = stan_data["I"]

    K = stan_data["K"]

    # --- Data-informed initialization via least-squares ---
    # 1) Item base prices: mean log price per item
    # 2) Population spline: regress (log_price - log_p0_i) on B
    # 3) Item-level deviations: small zeros (let the sampler explore)
    print("\nComputing data-informed inits via least-squares...")
    B_arr = np.array(stan_data["B"])              # (N, K)
    log_price_arr = np.log(np.array(stan_data["price"]))  # (N,)
    ii_init = np.array(stan_data["ii"]) - 1       # 0-based item indices

    # Per-item mean log price
    log_p0_i_init = np.zeros(I)
    for i in range(I):
        mask_i = (ii_init == i)
        log_p0_i_init[i] = np.mean(log_price_arr[mask_i]) if mask_i.any() else log_p0_pop_init

    # Population spline init: lstsq fit of (log_price - log_p0_i) on B
    centered = log_price_arr - log_p0_i_init[ii_init]
    beta_pop_init, *_ = np.linalg.lstsq(B_arr, centered, rcond=None)

    # Convert log_p0_i -> z_p0 init via standardization
    sigma_p0_init = max(np.std(log_p0_i_init - log_p0_pop_init), 0.1)
    z_p0_init = (log_p0_i_init - log_p0_pop_init) / sigma_p0_init

    # Estimate sigma from residuals of the LS fit
    pred_init = log_p0_i_init[ii_init] + B_arr @ beta_pop_init
    sigma_init = max(np.std(log_price_arr - pred_init), 0.05)

    print(f"  beta_pop init range: [{beta_pop_init.min():.3f}, {beta_pop_init.max():.3f}]")
    print(f"  log_p0_i init range: [{log_p0_i_init.min():.3f}, {log_p0_i_init.max():.3f}]")
    print(f"  sigma_p0 init: {sigma_p0_init:.3f}")
    print(f"  sigma init: {sigma_init:.3f}")

    def make_inits():
        return {
            "beta_pop":    beta_pop_init,
            "beta_i":      np.zeros((I, K)),
            "log_p0_pop":  log_p0_pop_init,
            "sigma_p0":    sigma_p0_init,
            "sigma_fs":    0.1,
            "sigma":       sigma_init,
            "nu":          10.0,
            "z_p0":        z_p0_init,
            "lambda_pop":  2.0,
            "lambda_i":    4.0,
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

    # --- Time-varying inflation from spline coefficients ---
    beta_pop_draws = fit.stan_variable("beta_pop")  # (draws, K)
    B = np.array(stan_data["B"])  # (N, K) — basis at observed times
    t_arr = np.array(stan_data["t"])  # times of observations
    n_draws = beta_pop_draws.shape[0]

    # For trajectory: evaluate spline at observed times (we have B already)
    # and compute inflation as rolling average of d/dt log(price) over time windows
    t_grid = np.arange(0, int(max(t_arr)) + 1, dtype=float)

    # Compute per-draw inflation rate as mean d/dt over observed range
    # Simple approach: use 6-month rolling window of differences
    inflation_rate_pop = np.zeros(n_draws)
    for draw in range(n_draws):
        log_price_all = B @ beta_pop_draws[draw]  # (N,) log prices at all obs times
        diffs = np.diff(log_price_all)  # consecutive differences
        if len(diffs) > 0:
            inflation_rate_pop[draw] = np.median(diffs)  # median monthly change
        else:
            inflation_rate_pop[draw] = 0.0

    print(f"\nPopulation inflation rate (median monthly log-difference):")
    print(f"  median: {np.median(inflation_rate_pop)*100:.2f}%/month")
    print(f"  annualized: {np.median(inflation_rate_pop)*12*100:.2f}%/year")

    # --- Per-item trajectories for summary ---
    beta_i_draws = fit.stan_variable("beta_i")  # (draws, I, K)

    # Compute item-level inflation as mean monthly log-difference
    ii_arr = np.array(stan_data["ii"])  # item indices (1-based)
    inf_i_draws = np.zeros((n_draws, I))

    for draw in range(n_draws):
        log_price_all = B @ (beta_pop_draws[draw] + beta_i_draws[draw, ii_arr - 1])  # (N,) including item deviations
        # Group by item and compute mean inflation per item
        for i in range(I):
            mask = (ii_arr == i + 1)  # 0-based to 1-based conversion
            if np.sum(mask) > 1:
                idx = np.where(mask)[0]
                diffs = np.diff(log_price_all[idx])
                inf_i_draws[draw, i] = np.median(diffs) * 12 if len(diffs) > 0 else 0.0
            else:
                inf_i_draws[draw, i] = 0.0

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

    # --- Posterior predictive trajectories (post-hoc) ---
    # For visualization, reconstruct prices at observed times and interpolate
    T_max = int(max(stan_data["t"]))
    t_grid = np.arange(0, T_max + 1, dtype=float)
    dates = [months_to_date(t) for t in t_grid]

    sigma_draws  = fit.stan_variable("sigma")        # (draws,)
    nu_draws     = fit.stan_variable("nu")           # (draws,)
    log_p0_i_draws = fit.stan_variable("log_p0_i")  # (draws, I)

    # Build HTML report with trajectories for top items by observation count
    top_items = items.nlargest(20, "n_obs")
    html_sections = []

    for _, row in top_items.iterrows():
        i = int(row["item_idx"]) - 1  # 0-based
        t_item_mask = (ii_arr == i + 1)  # observations for this item
        if np.sum(t_item_mask) < 2:
            continue

        t_item = t_arr[t_item_mask]
        t_item_idx = np.where(t_item_mask)[0]

        # Reconstruct log prices at observed times for this item
        log_price_obs = np.zeros((n_draws, len(t_item_idx)))
        for draw in range(n_draws):
            log_price_obs[draw] = (
                B[t_item_idx] @ (beta_pop_draws[draw] + beta_i_draws[draw, i])
                + log_p0_i_draws[draw, i]
            )

        # Interpolate to grid and take medians/percentiles
        traj_grid = np.zeros((n_draws, len(t_grid)))
        for draw in range(n_draws):
            from scipy.interpolate import interp1d
            if len(t_item) > 1:
                f = interp1d(t_item, log_price_obs[draw], kind='linear', fill_value='extrapolate')
                traj_grid[draw] = f(t_grid)
            else:
                traj_grid[draw] = log_price_obs[draw, 0]

        traj = np.exp(traj_grid)  # (draws, T) prices
        med  = np.median(traj, axis=0)
        p05  = np.percentile(traj, 5,  axis=0)
        p95  = np.percentile(traj, 95, axis=0)

        # prediction interval: add Student-t noise
        from scipy import stats
        noise = stats.t.rvs(
            df=nu_draws[:, None],
            scale=sigma_draws[:, None],
            size=traj.shape,
        )
        traj_rep = np.exp(np.log(traj) + noise)
        pred_p05 = np.percentile(traj_rep, 5,  axis=0)
        pred_p95 = np.percentile(traj_rep, 95, axis=0)

        date_strs = [d.isoformat() for d in dates]
        inf_pct   = row["inflation_median"] * 100
        inf_lo    = row["inflation_p05"]    * 100
        inf_hi    = row["inflation_p95"]    * 100

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
  <strong>Population inflation rate:</strong>
  {np.median(inf_pop)*100:.2f}%/yr
  (90% CI: {np.percentile(inf_pop,5)*100:.2f}% – {np.percentile(inf_pop,95)*100:.2f}%/yr)<br>
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
