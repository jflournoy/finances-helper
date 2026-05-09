// Hierarchical inflation model with time-varying inflation via thin plate smooth
//
// Model: log(price_it) ~ student_t(nu, mu_it, sigma)
//   mu_it = log_p0_i + dot_product(B[t], beta_pop + beta_i[i])
//   beta_pop, beta_i: thin plate regression spline coefficients, penalized via S
//   log_p0_i ~ normal(log_p0_pop + z_p0[i] * sigma_p0, ...)  (non-centered)
//
// Quantity of interest: inflation rate as a smooth function of time

data {
    int<lower=1> N;           // total observations
    int<lower=1> I;           // number of unique items (ASINs)
    int<lower=1> K;           // basis dimension for time-varying smooth
    array[N] int<lower=1, upper=I> ii;  // item index per observation
    vector[N] t;              // months since base date (t=0 at base month)
    vector<lower=0>[N] price; // observed unit prices in dollars
    matrix[N, K] B;           // thin plate basis matrix
    matrix[K, K] S;           // thin plate penalty matrix
}

transformed data {
    vector[N] log_price;
    for (n in 1:N)
        log_price[n] = log(price[n]);
}

parameters {
    // Time-varying inflation (spline coefficients)
    vector[K] beta_pop;              // population spline coefficients
    matrix[I, K] beta_i;             // item-level deviations

    // Item base prices
    real log_p0_pop;                 // population mean log base price
    real<lower=0> sigma_p0;          // sd of item intercept deviations
    vector[I] z_p0;                  // standardized item intercept deviations

    // Hyperpriors
    real<lower=0> sigma_fs;          // sd for factor smooth shrinkage
    real<lower=0> lambda_pop;        // smoothing parameter for population spline
    real<lower=0> lambda_i;          // smoothing parameter for item splines
    real<lower=0> sigma;             // observation noise on log scale
    real<lower=1> nu;                // Student-t degrees of freedom
}

transformed parameters {
    vector[I] log_p0_i = log_p0_pop + z_p0 * sigma_p0;  // item-level base prices
    vector[N] mu;
    for (n in 1:N) {
        vector[K] beta_n = beta_pop + to_vector(beta_i[ii[n]]);  // combined coefficients
        mu[n] = log_p0_i[ii[n]] + dot_product(B[n], beta_n);
    }
}

model {
    // Hyperpriors
    log_p0_pop  ~ normal(1.7, 2);     // population base price
    sigma_p0    ~ normal(0, 1);       // item base price variation
    sigma_fs    ~ normal(0, 0.3);     // factor smooth shrinkage
    sigma       ~ normal(0, 0.2);     // observation noise
    nu          ~ gamma(2, 0.1);      // heavy tails
    lambda_pop  ~ gamma(1, 0.1);      // smoothing parameter
    lambda_i    ~ gamma(1, 0.1);      // smoothing parameter

    // Non-centered item intercepts
    z_p0 ~ normal(0, 1);

    // Spline priors with thin plate penalties
    // Population smooth: penalize for smoothness
    target += -0.5 * lambda_pop * quad_form(S, beta_pop);

    // Item splines: penalize for smoothness AND shrink toward zero
    for (i in 1:I) {
        target += -0.5 * lambda_i * quad_form(S, to_vector(beta_i[i]));
        beta_i[i] ~ normal(0, sigma_fs);  // shrink toward population
    }

    // Likelihood
    for (n in 1:N)
        target += student_t_lpdf(log_price[n] | nu, mu[n], sigma);
}

generated quantities {
    // Per-item base prices
    vector[I] inflation_rate_i;  // will be reconstructed post-hoc from spline coefficients

    // Time-varying population inflation at observed t values
    // inflation[t] = d/dt log(exp(B[t] * beta_pop)) = B'[t] * beta_pop (approx numerically)

    // LOO inputs
    vector[N] log_lik;
    vector[N] log_price_rep;
    for (n in 1:N) {
        log_lik[n]       = student_t_lpdf(log_price[n] | nu, mu[n], sigma);
        log_price_rep[n] = student_t_rng(nu, mu[n], sigma);
    }
}
