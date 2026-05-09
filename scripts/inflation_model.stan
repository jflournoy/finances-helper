// Hierarchical inflation model for household grocery prices
//
// Model: log(price_it) ~ student_t(nu, mu_it, sigma)
//   mu_it = log_p0_i + (t/12) * r_i
//   r_i   = r_pop + z_r[i] * sigma_r        // non-centered
//   log_p0_i = log_p0_pop + z_p0[i] * sigma_p0  // non-centered
//
// Quantity of interest: exp(r_pop) - 1  (annualized population inflation rate)

data {
    int<lower=1> N;           // total observations
    int<lower=1> I;           // number of unique items (ASINs)
    array[N] int<lower=1, upper=I> ii;  // item index per observation
    vector[N] t;              // months since base date (t=0 at base month)
    vector<lower=0>[N] price; // observed unit prices in dollars
}

transformed data {
    vector[N] log_price;
    real T_max;
    for (n in 1:N)
        log_price[n] = log(price[n]);
    T_max = max(t);
}

parameters {
    // Population-level
    real r_pop;                    // population log(1 + inflation_rate)
    real log_p0_pop;               // population mean log base price
    real<lower=0> sigma_r;         // sd of item-level rate deviations
    real<lower=0> sigma_p0;        // sd of item-level intercept deviations
    real<lower=0> sigma;           // observation noise on log scale
    real<lower=1> nu;              // Student-t degrees of freedom

    // Non-centered item-level effects
    vector[I] z_r;                 // standardized item rate deviations
    vector[I] z_p0;                // standardized item intercept deviations
}

transformed parameters {
    vector[I] r_i     = r_pop    + z_r  * sigma_r;   // item-level log rates
    vector[I] log_p0_i = log_p0_pop + z_p0 * sigma_p0; // item-level log base prices

    vector[N] mu;
    for (n in 1:N)
        mu[n] = log_p0_i[ii[n]] + (t[n] / 12.0) * r_i[ii[n]];
}

model {
    // Priors — population level
    // log(1.04) ~ 0.039; sd=0.05 means roughly ±5pp/yr is one sd
    r_pop       ~ normal(0.039, 0.05);
    log_p0_pop  ~ normal(2, 2);          // base price around exp(2) ~ $7, wide
    // item rates cluster tightly around population rate
    sigma_r     ~ normal(0, 0.03);       // ±3pp/yr between items is one sd
    sigma_p0    ~ normal(0, 1);          // item price levels vary more freely
    sigma       ~ normal(0, 0.2);        // log-scale observation noise
    nu          ~ gamma(2, 0.1);         // heavy tails ok; mean=20, allows low values

    // Non-centered priors
    z_r  ~ normal(0, 1);
    z_p0 ~ normal(0, 1);

    // Likelihood
    for (n in 1:N)
        target += student_t_lpdf(log_price[n] | nu, mu[n], sigma);
}

generated quantities {
    // Inflation rates on natural scale
    real inflation_rate_pop = exp(r_pop) - 1;
    vector[I] inflation_rate_i;
    for (i in 1:I)
        inflation_rate_i[i] = exp(r_i[i]) - 1;

    // LOO inputs
    vector[N] log_lik;
    // Posterior predictive on log scale
    vector[N] log_price_rep;
    for (n in 1:N) {
        log_lik[n]       = student_t_lpdf(log_price[n] | nu, mu[n], sigma);
        log_price_rep[n] = student_t_rng(nu, mu[n], sigma);
    }
}
