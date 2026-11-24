/* Numeric kernels for the metric pipeline.
   Order statistics skip non-finite samples and return None when nothing finite
   remains.  Derivatives follow numpy.gradient (second-order interior, first-
   order edges).  Savitzky–Golay derivatives use a quadratic fit over an odd
   window; edge samples evaluate the first/last full window's polynomial.  */

#[inline]
pub fn sign(x: f64) -> f64 {
    if x > 0.0 {
        1.0
    } else if x < 0.0 {
        -1.0
    } else {
        0.0
    }
}

pub fn finite(xs: &[f64]) -> Vec<f64> {
    xs.iter().copied().filter(|x| x.is_finite()).collect()
}

// Linear-interpolated quantile (numpy default), q in [0, 1].
pub fn quantile(xs: &[f64], q: f64) -> Option<f64> {
    let mut v = finite(xs);
    if v.is_empty() {
        return None;
    }
    v.sort_by(f64::total_cmp);
    let pos = q.clamp(0.0, 1.0) * (v.len() - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = (lo + 1).min(v.len() - 1);
    Some(v[lo] + (pos - lo as f64) * (v[hi] - v[lo]))
}

pub fn median(xs: &[f64]) -> Option<f64> {
    quantile(xs, 0.5)
}

// First sorted sample whose cumulative weight reaches q of the total.
pub fn weighted_quantile(xs: &[f64], ws: &[f64], q: f64) -> Option<f64> {
    let mut pairs: Vec<(f64, f64)> = xs
        .iter()
        .zip(ws)
        .filter(|(x, w)| x.is_finite() && w.is_finite() && **w > 0.0)
        .map(|(&x, &w)| (x, w))
        .collect();
    if pairs.is_empty() {
        return None;
    }
    pairs.sort_by(|a, b| a.0.total_cmp(&b.0));
    let total: f64 = pairs.iter().map(|p| p.1).sum();
    let target = q.clamp(0.0, 1.0) * total;
    let mut acc = 0.0;
    pairs
        .iter()
        .find(|(_, w)| {
            acc += w;
            acc >= target
        })
        .or(pairs.last())
        .map(|p| p.0)
}

pub fn weighted_mean(xs: &[f64], ws: &[f64]) -> Option<f64> {
    let (num, den) = xs
        .iter()
        .zip(ws)
        .filter(|(x, w)| x.is_finite() && w.is_finite())
        .fold((0.0, 0.0), |(n, d), (x, w)| (n + x * w, d + w));
    (den > 0.0).then(|| num / den)
}

pub fn mean(xs: &[f64]) -> Option<f64> {
    let v = finite(xs);
    (!v.is_empty()).then(|| v.iter().sum::<f64>() / v.len() as f64)
}

// Centred moving average; edges average the samples available.
pub fn moving_average(x: &[f64], n: usize) -> Vec<f64> {
    if n <= 1 || x.len() < n {
        return x.to_vec();
    }
    let lo_off = (n - 1) / 2;
    let hi_off = n / 2;
    (0..x.len())
        .map(|i| {
            let lo = i.saturating_sub(lo_off);
            let hi = (i + hi_off).min(x.len() - 1);
            x[lo..=hi].iter().sum::<f64>() / (hi - lo + 1) as f64
        })
        .collect()
}

// Zero-padded centred convolution with a box of n taps (numpy convolve "same").
pub fn box_filter_zero_pad(x: &[f64], n: usize) -> Vec<f64> {
    let half = (n - 1) / 2;
    (0..x.len())
        .map(|i| {
            let lo = i.saturating_sub(half);
            let hi = (i + n - 1 - half).min(x.len() - 1);
            x[lo..=hi].iter().sum::<f64>() / n as f64
        })
        .collect()
}

pub fn gradient(y: &[f64], t: &[f64]) -> Vec<f64> {
    let n = y.len().min(t.len());
    if n < 2 {
        return vec![0.0; n];
    }
    (0..n)
        .map(|i| {
            if i == 0 {
                (y[1] - y[0]) / (t[1] - t[0])
            } else if i == n - 1 {
                (y[n - 1] - y[n - 2]) / (t[n - 1] - t[n - 2])
            } else {
                let hs = t[i] - t[i - 1];
                let hd = t[i + 1] - t[i];
                let a = -hd / (hs * (hs + hd));
                let b = (hd - hs) / (hs * hd);
                let c = hs / (hd * (hs + hd));
                a * y[i - 1] + b * y[i] + c * y[i + 1]
            }
        })
        .collect()
}

// Piecewise-linear interpolation of (xp, fp) at x; xp increasing.  Outside the
// range returns `outside` when given, else the nearest endpoint value.
pub fn interp(x: &[f64], xp: &[f64], fp: &[f64], outside: Option<f64>) -> Vec<f64> {
    let n = xp.len().min(fp.len());
    x.iter()
        .map(|&xi| {
            if n == 0 {
                return f64::NAN;
            }
            if xi < xp[0] {
                return outside.unwrap_or(fp[0]);
            }
            if xi > xp[n - 1] {
                return outside.unwrap_or(fp[n - 1]);
            }
            let hi = xp[..n].partition_point(|&v| v < xi);
            if hi == 0 {
                return fp[0];
            }
            let lo = hi - 1;
            let dx = xp[hi] - xp[lo];
            if dx <= 0.0 {
                fp[hi]
            } else {
                fp[lo] + (xi - xp[lo]) / dx * (fp[hi] - fp[lo])
            }
        })
        .collect()
}

pub fn correlation(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len().min(b.len());
    if n < 2 {
        return f64::NAN;
    }
    let ma = a[..n].iter().sum::<f64>() / n as f64;
    let mb = b[..n].iter().sum::<f64>() / n as f64;
    let (sab, saa, sbb) = a[..n].iter().zip(&b[..n]).fold((0.0, 0.0, 0.0), |(ab, aa, bb), (x, y)| {
        let (dx, dy) = (x - ma, y - mb);
        (ab + dx * dy, aa + dx * dx, bb + dy * dy)
    });
    sab / (saa * sbb).sqrt()
}

// Odd window length nearest `seconds` at `rate_hz`, at least poly + 2 samples.
pub fn savgol_window(rate_hz: f64, seconds: f64, poly: usize) -> usize {
    let n = ((seconds * rate_hz).round() as usize).max(poly + 2);
    if n % 2 == 1 {
        n
    } else {
        n + 1
    }
}

// First derivative of a quadratic Savitzky–Golay fit per unit time.
pub fn savgol_derivative(y: &[f64], window: usize, dt: f64) -> Vec<f64> {
    let n = y.len();
    let window = if n < window { n - (1 - n % 2) } else { window };
    if n < 3 || window < 3 {
        return vec![0.0; n];
    }
    let half = window / 2;
    let offsets: Vec<f64> = (0..window).map(|j| j as f64 - half as f64).collect();
    let s2: f64 = offsets.iter().map(|j| j * j).sum();
    let m2 = s2 / window as f64;
    let s4c: f64 = offsets.iter().map(|j| (j * j - m2).powi(2)).sum();
    // Orthogonal basis {1, j, j^2 - m2}: c1 and c2 project independently.
    let fit = |seg: &[f64]| -> (f64, f64) {
        let c1 = seg.iter().zip(&offsets).map(|(v, j)| v * j).sum::<f64>() / s2;
        let c2 = seg.iter().zip(&offsets).map(|(v, j)| v * (j * j - m2)).sum::<f64>() / s4c;
        (c1, c2)
    };
    let mut out = vec![0.0; n];
    for i in half..n - half {
        out[i] = fit(&y[i - half..=i + half]).0 / dt;
    }
    let (h1, h2) = fit(&y[..window]);
    let (t1, t2) = fit(&y[n - window..]);
    for k in 0..half {
        let x_head = k as f64 - half as f64;
        out[k] = (h1 + 2.0 * h2 * x_head) / dt;
        let x_tail = (k + 1) as f64;
        out[n - half + k] = (t1 + 2.0 * t2 * x_tail) / dt;
    }
    out
}

// Longest run of true values.
pub fn longest_run(mask: impl IntoIterator<Item = bool>) -> usize {
    mask.into_iter()
        .fold((0usize, 0usize), |(best, cur), b| {
            let cur = if b { cur + 1 } else { 0 };
            (best.max(cur), cur)
        })
        .0
}

// Inclusive index runs of true values.
pub fn runs(mask: &[bool]) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut start = None;
    for (i, &b) in mask.iter().enumerate() {
        match (b, start) {
            (true, None) => start = Some(i),
            (false, Some(s)) => {
                out.push((s, i - 1));
                start = None;
            }
            _ => {}
        }
    }
    if let Some(s) = start {
        out.push((s, mask.len() - 1));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn savgol_derivative_is_exact_on_quadratics_including_edges() {
        let dt = 0.01;
        let t: Vec<f64> = (0..50).map(|i| i as f64 * dt).collect();
        let y: Vec<f64> = t.iter().map(|t| 3.0 * t * t - 2.0 * t + 1.0).collect();
        let d = savgol_derivative(&y, 5, dt);
        for (ti, di) in t.iter().zip(&d) {
            assert!((di - (6.0 * ti - 2.0)).abs() < 1e-9, "t={ti} d={di}");
        }
    }

    #[test]
    fn weighted_median_and_quantile_match_numpy_conventions() {
        assert_eq!(quantile(&[1.0, 2.0, 3.0, 4.0], 0.5), Some(2.5));
        assert_eq!(quantile(&[f64::NAN, 5.0], 0.9), Some(5.0));
        assert_eq!(weighted_quantile(&[1.0, 2.0, 3.0], &[1.0, 1.0, 10.0], 0.5), Some(3.0));
        assert_eq!(runs(&[false, true, true, false, true]), vec![(1, 2), (4, 4)]);
    }
}
