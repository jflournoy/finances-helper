// Hierarchical inflation model on geometric-mean-normalized log-prices.
//
// Data preprocessing (in prep.py):
//   y[n] = log(price[n]) - mean_i(log(price))    # log of price / item geo-mean
//
// Model:
//   y[n] ~ normal(mu[n], sigma)
//   mu[n] = B[n] . (beta_pop + beta_i[i_n])
//
// beta_pop: K-dim population thin-plate spline coefficients (loose prior +
//           smoothness penalty on the wiggle modes via S).
// beta_i:   per-item deviation in the same K-dim basis space, non-centered.
//           Each direction has its own shrinkage scale sigma_fs[k], with the
//           constant-mode prior loose enough to absorb residual per-item
//           level variation (which is non-trivial even after normalization
//           because each item's geo-mean is empirical, computed only over its
//           own observation window).
//
//   beta_pop          ~ N(0, 0.5)
//   target += -0.5 * lambda_pop * quad_form(S, beta_pop)
//   beta_i_raw[i, k]  ~ N(0, 1)
//   beta_i[i, k]      = beta_i_raw[i, k] * sigma_fs[k]
//   sigma_fs[k]       ~ N(0, sigma_fs_prior_sd[k])

data {
    int<lower=1> N;                      // total observations
    int<lower=1> I;                      // number of unique items
    int<lower=1> K;                      // basis dimension
    array[N] int<lower=1, upper=I> ii;   // item index per observation
    vector[N] y;                         // normalized log-price (= log(price/geo_mean_i))
    matrix[N, K] B;                      // population thin-plate basis
    matrix[K, K] S;                      // K x K penalty matrix
    vector<lower=0>[K] sigma_fs_prior_sd; // per-direction prior SD on sigma_fs
}

parameters {
    vector[K] beta_pop;                  // population spline coefficients
    matrix[I, K] beta_i_raw;             // standardized item deviations (non-centered)
    vector<lower=0>[K] sigma_fs;         // per-coefficient shrinkage scale
    real<lower=0> sigma;                 // observation noise on log scale
    real<lower=0> lambda_pop;            // population smoothing parameter
}

transformed parameters {
    matrix[I, K] beta_i = diag_post_multiply(beta_i_raw, sigma_fs);
    vector[N] mu = B * beta_pop + rows_dot_product(B, beta_i[ii]);
}

model {
    sigma       ~ normal(0.2, 0.1);
    lambda_pop  ~ gamma(1.5, 1);
    sigma_fs    ~ normal(0, sigma_fs_prior_sd);

    beta_pop    ~ normal(0, 0.5);
    target += -0.5 * lambda_pop * quad_form(S, beta_pop);

    to_vector(beta_i_raw) ~ normal(0, 1);

    y ~ normal(mu, sigma);
}

generated quantities {
    vector[N] log_lik;
    vector[N] y_rep = to_vector(normal_rng(mu, rep_vector(sigma, N)));
    for (n in 1:N)
        log_lik[n] = normal_lpdf(y[n] | mu[n], sigma);
}
