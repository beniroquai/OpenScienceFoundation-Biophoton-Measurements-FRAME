"""Checks for denoising.py on synthetic data with a known truth.  python test_denoising.py"""
import math

import numpy as np

import denoising as dn

RNG = np.random.default_rng(1)


def scene(h=192, w=192, peak=6.0, floor=1.5):
    """Smooth leaf-like blob with an edge and a few fine features, in counts per frame."""
    yy, xx = np.mgrid[0:h, 0:w] / max(h, w)
    blob = np.exp(-(((xx - 0.55) / 0.22) ** 2 + ((yy - 0.45) / 0.3) ** 2))
    edge = (xx + 0.3 * yy > 0.75).astype(float) * 0.4
    dots = np.zeros((h, w))
    dots[40:44, 30:34] = 1.5
    return floor + peak * blob * (1 + edge) + dots


def stack_adu(x, n=16, gain=99.0, offset=870.0, read=110.0, decay=0.01):
    frames = []
    for k in range(n):
        lam = x * (1 - decay) ** k
        frames.append(gain * RNG.poisson(lam) + offset + RNG.normal(0, read, x.shape))
    return np.stack(frames)


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name} {detail}")
    return ok


def main():
    results = []
    x = scene()
    gain, offset, read = 99.0, 870.0, 110.0
    st = stack_adu(x, gain=gain, offset=offset, read=read)

    # 1 noise model from consecutive frames
    model = dn.fit_noise(st, read_noise_adu=read)
    g = model["gain_adu_per_count"]
    results.append(check("gain from run", abs(g / gain - 1) < 0.05, f"{g:.1f} vs {gain}"))
    results.append(check("offset from run", abs(model["offset_adu"] - offset) < 0.3 * gain,
                         f"{model['offset_adu']:.0f} vs {offset}"))
    model0 = dn.fit_noise(st)                       # default: read noise folded into the offset
    eff = offset - read ** 2 / gain
    results.append(check("effective offset", abs(model0["offset_adu"] - eff) < 0.3 * gain,
                         f"{model0['offset_adu']:.0f} vs {eff:.0f}"))

    # 1b streaks like the EMCCD cosmic rays: found, and the fit survives them
    hit = np.zeros(st.shape, bool)
    streaky = st.copy()
    for f in range(len(st)):
        for _ in range(25):
            r, c, ln = RNG.integers(0, x.shape[0]), RNG.integers(0, x.shape[1] - 25), RNG.integers(5, 25)
            streaky[f, r, c:c + ln] += gain * RNG.uniform(15, 60)
            hit[f, r, c:c + ln] = True
    tm = dn.transients(streaky, dilate=(1, 3))
    found, false = tm[hit].mean(), tm[~hit].mean()
    results.append(check("streaks found", found > 0.97 and false < 0.03,
                         f"{found:.3f} found, {false:.3f} false"))
    gs = dn.fit_noise(streaky, tmask=tm, read_noise_adu=read)["gain_adu_per_count"]
    gn = dn.fit_noise(streaky, read_noise_adu=read)["gain_adu_per_count"]
    results.append(check("gain with streaks masked", abs(gs / gain - 1) < 0.06,
                         f"{gs:.1f} (unmasked {gn:.1f}) vs {gain}"))

    # 2 GAT stabilises to std ~1 above ~1 count
    # from here on the default path: read noise folded into the offset, GAT with sigma 0
    n = dn.to_counts(st, model0)
    sc = dn.stabilisation_check(n)
    s = np.array(sc["z_std"])
    results.append(check("GAT std ~ 1", np.all(np.abs(s - 1) < 0.12),
                         f"{s.min():.2f}..{s.max():.2f}"))

    # 3 inverse GAT is unbiased on the mean of GAT(Poisson)
    worst = 0.0
    for lam in (1.0, 2.0, 5.0, 20.0):
        z = dn.gat(RNG.poisson(lam, 400_000)).mean()
        worst = max(worst, abs(dn.igat(z) / lam - 1))
    results.append(check("inverse GAT unbiased (>= 1 count)", worst < 0.03, f"max {worst:.3f}"))

    # 4 tiling matches the untiled result away from tile seams
    blur = dn.gaussian_blur(1.5)
    img = RNG.random((300, 260))
    d = np.abs(dn.tiled(blur, img, None, tile=128, overlap=32) - blur(img, None))
    results.append(check("tiling", d[10:-10, 10:-10].max() < 1e-2, f"max dev {d.max():.2e}"))

    # 5 held-out error tracks the true MSE
    k = 5
    z_true = dn.gat(x * 0.99 ** k + (read / model0["gain_adu_per_count"]) ** 2)
    for label, est in (("raw", n[k]), ("blur", dn.igat(blur(dn.gat(n[k]), None)))):
        h = dn.heldout_error(est, n, k)
        true_mse = np.mean((dn.gat(est) - z_true) ** 2)
        results.append(check(f"held-out error, {label}", abs(h - true_mse) < 0.1,
                             f"{h:.3f} vs true {true_mse:.3f}"))

    # 6 GAT + denoiser beats raw; ROI mean preserved
    est, info = dn.denoise_counts(n[k], dn.gaussian_blur(2.0))
    h = dn.heldout_error(est, n, k)
    results.append(check("GAT + blur better than raw", h < 0.6, f"{h:.3f}"))
    roi = {"sample": [90, 60, 50, 50]}
    r_raw = dn.roi_means(n[k], roi)["sample"]
    r_den = dn.roi_means(est, roi)["sample"]
    results.append(check("ROI mean preserved", abs(r_den / r_raw - 1) < 0.03,
                         f"{r_den:.3f} vs {r_raw:.3f}"))

    # 7 Noise2Noise learns something in a short run (needs torch + deepinv)
    try:
        import deepinv  # noqa: F401
    except ImportError:
        print("skip Noise2Noise (no deepinv)")
    else:
        z = dn.gat(n)
        lo, scale = dn.gat_scaling(z)
        net = dn.train_noise2noise((z - lo) / scale, steps=300, batch=8, depth=6, log_every=0)
        est, _ = dn.denoise_counts(n[k], net, scaling=(lo, scale))
        h = dn.heldout_error(est, n, k)
        results.append(check("Noise2Noise better than raw", h < 0.8, f"{h:.3f}"))

    print(f"\n{sum(results)}/{len(results)} passed")
    return all(results)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
