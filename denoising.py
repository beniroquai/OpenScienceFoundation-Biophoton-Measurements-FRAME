"""Denoising for the biophoton runs (E5).

Poisson-Gaussian noise model measured on the run itself, generalized Anscombe transform (GAT),
a Gaussian denoiser in the stabilised domain, and a check of the result that needs no ground
truth. numpy only, except the denoisers, which import torch/deepinv when they are built.

Conventions
-----------
A frame y [ADU] follows  y = g * n + b + read noise,  n ~ Poisson(x)  ("counts").
g is ADU per count: the conversion gain for the CMOS, conversion gain x EM gain x excess-noise
factor F^2 (~2) for the EMCCD in analog mode, 1 for photon-counting event maps.
The fit from consecutive frames gives  var = g * mean + c.  The GAT only needs g and c, so
counts are taken relative to an effective offset b = (sigma^2 - c) / g. With the default
read_noise_adu = 0 the read noise is folded into b; the forward transform is the same and only
higher-order terms of the inverse differ.
"""
import json
import math
import warnings
from pathlib import Path

import numpy as np
import tifffile

import biophoton as bp


# --------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------
def bin_image(img, b):
    """Sum b x b pixels (drops the edge that does not fit). Works on (..., y, x)."""
    if b == 1:
        return img
    h, w = (s // b * b for s in img.shape[-2:])
    a = img[..., :h, :w]
    return a.reshape(*a.shape[:-2], h // b, b, w // b, b).sum(axis=(-3, -1))


def load_stack(run_dir, label="frame", binning=1, crop=None, frames=None, saturation="auto"):
    """(n, y, x) float64 stack of a run, cropped (x, y, w, h in raw pixels) and then binned.

    Returns (stack, table, saturated): table holds the rows of frames.csv that were loaded,
    saturated is a (y, x) mask of binned pixels that hit full scale in any raw frame (found
    before binning, where full scale is still recognisable).
    """
    run_dir = Path(run_dir)
    tab = bp.read_table(run_dir / "frames.csv")
    sel = np.flatnonzero(tab["label"].astype(str) == label)
    if frames is not None:
        sel = sel[frames]
    imgs, sat = [], None
    for i in sel:
        raw = bp.roi_view(bp.load_image(run_dir / tab["file"][i]), crop)
        level = saturation_level(raw) if saturation == "auto" else saturation
        hit = (raw >= level - 0.5) if level is not None else np.zeros(raw.shape, bool)
        hit = bin_image(hit.astype(np.int32), binning) > 0
        sat = hit if sat is None else sat | hit
        imgs.append(bin_image(raw, binning))
    return np.stack(imgs), {k: v[sel] for k, v in tab.items()}, sat


def event_stack(stack, nsigma=5.0):
    """Photon-counting runs (E1): threshold each raw EMCCD frame into 0/1 events."""
    return np.stack([bp.photon_events(f, nsigma)["mask"].astype(np.float64) for f in stack])


def saturation_level(img, min_pixels=10):
    """Full-scale value if at least `min_pixels` share the maximum (a clipped camera), else None.

    The Andor clips at 65444, the ToupTek at 16383, so the level is read from the data.
    """
    top = float(img.max())
    return top if np.count_nonzero(img >= top - 0.5) >= min_pixels else None


# --------------------------------------------------------------------------------------------
# Transients: cosmic rays, and on the EMCCD their streaks along the readout direction
# --------------------------------------------------------------------------------------------
def frame_scale(stack):
    """Per-frame brightness relative to the temporal median (the leaf fades over the run)."""
    med = np.median(stack, 0)
    return np.median(stack, axis=(1, 2)) / np.median(med), med


def transients(stack, nsigma=5.0, dilate=(1, 15), two_sided=True):
    """(n, y, x) mask of pixel values far above the same pixel in the other frames.

    Residual to the temporal median (scaled per frame), in units of the per-pixel temporal MAD.
    Two-sided by default: on the EMCCD a big hit also leaves dark streaks (undershoot in the
    serial register). `dilate` = (rows, columns) grows each
    hit to catch its tail: (1, 15) for the EMCCD, whose hits smear along the serial register,
    (3, 3) for the CMOS. Needs enough frames for a robust median (>= 8 recommended).
    """
    from scipy.ndimage import binary_dilation
    s, med = frame_scale(stack)
    expect = med[None] * s[:, None, None]
    r = stack - expect
    mad = 1.4826 * np.median(np.abs(r), 0)
    floor = np.percentile(mad[mad > 0], 5) if np.any(mad > 0) else 1.0
    lim = nsigma * np.maximum(mad, floor)[None]
    hit = (np.abs(r) > lim) if two_sided else (r > lim)
    if dilate and max(dilate) > 1:
        hit = binary_dilation(hit, structure=np.ones((1, *dilate), bool))
    return hit


def clean(stack, tmask):
    """Copy of the stack with transient pixels replaced by the scaled temporal median.

    Filled pixels borrow from the other frames, so metrics leave them out (pass `tmask`).
    """
    s, med = frame_scale(stack)
    out = stack.copy()
    fill = med[None] * s[:, None, None]
    out[tmask] = fill[tmask]
    return out


# --------------------------------------------------------------------------------------------
# Noise model
# --------------------------------------------------------------------------------------------
def temporal_moments(stack, tmask=None):
    """Per-pixel mean and temporal variance from second differences, transients left out.

    var = mean((f[k+1] - 2 f[k] + f[k-1])^2) / 6 cancels fixed pattern and any linear drift
    (the leaf drying or fading between frames); it needs at least 3 frames.
    """
    if len(stack) < 3:
        raise ValueError("need at least 3 frames to measure the noise")
    x = np.array(stack, np.float64)
    if tmask is not None:
        x[tmask] = np.nan
    d2 = x[2:] - 2 * x[1:-1] + x[:-2]
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmean(x, 0), np.nanmean(d2 ** 2, 0) / 6


def binned_moments(stack, mask=None, tmask=None, nbins=40, pct=(1.0, 99.0), use_upper=1.0,
                   min_count=30):
    """Mean-variance pairs per intensity bin, free of regression dilution.

    Binning pixels by their own noisy mean biases the slope (the bin picks pixels whose noise
    went up, and the mean of few frames is noisy). Instead the pixels are binned by the mean of
    the even frames and both mean and variance are taken from the odd frames, and vice versa:
    an instrumental variable. Bins hold equal numbers of pixels between the percentiles `pct`,
    so a few bright pixels (where the leaf may change between frames) do not steer the fit.
    `use_upper` keeps the lower fraction of that percentile range. Needs at least 6 frames.
    """
    if len(stack) < 6:
        raise ValueError("need at least 6 frames for the noise fit; use fixed_model() instead")
    good = np.ones(stack.shape[1:], bool) if mask is None else ~mask
    halves = [(stack[i::2], None if tmask is None else tmask[i::2]) for i in (0, 1)]
    bm, bv, bn = [], [], []
    for a, b in ((0, 1), (1, 0)):
        key = temporal_moments(*halves[a])[0]
        mean_b, var_b = temporal_moments(*halves[b])
        ok = good & np.isfinite(key) & np.isfinite(mean_b) & np.isfinite(var_b)
        key, mean_b, var_b = key[ok], mean_b[ok], var_b[ok]
        q = np.linspace(pct[0], pct[0] + use_upper * (pct[1] - pct[0]), nbins + 1)
        edges = np.unique(np.percentile(key, q))       # equal-population bins
        idx = np.digitize(key, edges) - 1
        for i in range(len(edges) - 1):
            sel = idx == i
            if sel.sum() >= min_count:
                v = var_b[sel]
                keep = v <= np.percentile(v, 99.5)      # one stray cosmic ray dominates a bin
                bm.append(float(mean_b[sel][keep].mean()))
                bv.append(float(v[keep].mean()))
                bn.append(int(keep.sum()))
    order = np.argsort(bm)
    return np.array(bm)[order], np.array(bv)[order], np.array(bn)[order]


def bad_pixels(stack, tmask=None, saturated=None, k=6.0):
    """Static (y, x) mask of pixels to leave out of fits and metrics: saturated, hot, stuck.

    Hot = temporal mean far above its 5x5 neighbourhood in units of its own temporal noise.
    """
    mean, var = temporal_moments(stack, tmask)
    mask = ~np.isfinite(var) | (var <= 0)
    if saturated is not None:
        mask |= saturated
    pad = np.pad(np.nan_to_num(mean), 2, mode="reflect")
    win = np.lib.stride_tricks.sliding_window_view(pad, (5, 5))
    local = np.median(win.reshape(*mean.shape, 25), axis=-1)
    mask |= (mean - local) > k * np.sqrt(np.nan_to_num(np.maximum(var, 1e-12), nan=1e-12))
    return mask


def noisy_pixels(stack, model, tmask=None, ratio=3.0):
    """(y, x) mask of pixels whose temporal variance exceeds the model `ratio` times.

    Random-telegraph and fluctuating hot pixels: Poisson-looking in the mean, not in the noise.
    About 2 % of pixels on both cameras. A legitimate pixel exceeds 3x by chance only rarely
    with ~20 frames; on short runs (few frames) raise `ratio`.
    """
    mean, var = temporal_moments(stack, tmask)
    expect = model["gain_adu_per_count"] * mean + model["intercept_adu2"]
    with np.errstate(invalid="ignore"):
        return np.nan_to_num(var / np.maximum(expect, 1e-9), nan=np.inf) > ratio


def fit_noise(stack, mask=None, tmask=None, nbins=40, pct=(1.0, 99.0), use_upper=0.8,
              read_noise_adu=0.0):
    """var = gain * mean + intercept from the run's own frames.

    Line through the binned mean-variance pairs of `binned_moments` (equal-population bins over
    the lower `use_upper` fraction of the percentile range `pct`). Returns a dict that
    `to_counts`, `from_counts` and the GAT use.
    """
    bm, bv, bn = binned_moments(stack, mask, tmask, nbins, pct, use_upper)
    if len(bm) < 3:
        raise ValueError("too few populated intensity bins: signal range too small for a fit")
    gain, intercept = np.polyfit(bm, bv, 1)
    if gain <= 0:
        raise ValueError(f"fitted gain {gain:.3g} <= 0: no shot noise visible in this run")
    s2 = float(read_noise_adu) ** 2
    offset = (s2 - intercept) / gain
    resid = bv - (gain * bm + intercept)
    return {"gain_adu_per_count": float(gain), "intercept_adu2": float(intercept),
            "read_noise_adu": float(read_noise_adu), "offset_adu": float(offset),
            "read_noise_counts": float(read_noise_adu) / gain,
            "fit_rel_residual_rms": float(np.sqrt(np.mean((resid / bv) ** 2))),
            "frames": int(len(stack)),
            "bins_mean": bm.tolist(), "bins_var": bv.tolist(), "bins_n": bn.tolist()}


def fixed_model(gain=1.0, offset=0.0, read_noise_adu=0.0):
    """Known model, e.g. photon-counting event maps: gain 1, offset 0, no read noise."""
    return {"gain_adu_per_count": float(gain), "offset_adu": float(offset),
            "read_noise_adu": float(read_noise_adu), "read_noise_counts": read_noise_adu / gain,
            "intercept_adu2": read_noise_adu ** 2 - gain * offset}


def to_counts(y, model, n_summed=1):
    """ADU -> counts. A sum of n frames carries n offsets and n read-noise variances."""
    return (np.asarray(y, np.float64) - n_summed * model["offset_adu"]) / model["gain_adu_per_count"]


def from_counts(n, model, n_summed=1):
    return np.asarray(n, np.float64) * model["gain_adu_per_count"] + n_summed * model["offset_adu"]


# --------------------------------------------------------------------------------------------
# Generalized Anscombe transform (counts, gain 1): noise std ~1 after the transform
# --------------------------------------------------------------------------------------------
def gat(n, sigma=0.0):
    return 2.0 * np.sqrt(np.maximum(np.asarray(n, np.float64) + 0.375 + sigma ** 2, 0.0))


def igat(z, sigma=0.0):
    """Closed-form approximation of the exact unbiased inverse (Makitalo & Foi 2013)."""
    z = np.maximum(np.asarray(z, np.float64), 0.8)          # the series diverges near 0
    r = math.sqrt(1.5)
    n = 0.25 * z ** 2 + 0.25 * r / z - 1.375 / z ** 2 + 0.625 * r / z ** 3 - 0.125 - sigma ** 2
    return np.maximum(n, 0.0)


def stabilisation_check(counts_stack, sigma=0.0, mask=None, tmask=None, nbins=30):
    """Temporal noise std of GAT(frames) versus level. Should be ~1 everywhere."""
    z = gat(counts_stack, sigma)
    bm, bv, bn = binned_moments(z, mask, tmask, nbins)
    overall = float(np.sqrt(np.sum(bv * bn) / np.sum(bn)))
    return {"z_mean": bm.tolist(), "z_std": np.sqrt(bv).tolist(), "z_std_overall": overall}


# --------------------------------------------------------------------------------------------
# Denoisers: callables f(z01, sigma01) -> z01 on 2-D arrays scaled to ~[0, 1]
# --------------------------------------------------------------------------------------------
def _device(device=None):
    import torch
    return device or ("cuda" if torch.cuda.is_available() else "cpu")


def _wrap_torch(model, device):
    import torch

    def f(img, sigma):
        x = torch.as_tensor(img, dtype=torch.float32, device=device)[None, None]
        with torch.no_grad():
            out = model(x, float(sigma)) if sigma is not None else model(x)
        return out[0, 0].double().cpu().numpy()
    return f


def drunet(device=None, pretrained="download"):
    """Pretrained DRUNet (grey) from deepinv, trained for sigma up to ~0.2 on [0, 1] images."""
    import deepinv as dinv
    dev = _device(device)
    return _wrap_torch(dinv.models.DRUNet(1, 1, pretrained=pretrained, device=dev).eval(), dev)


def bm3d(device=None):
    """BM3D through deepinv (pip install bm3d). Classical, no weights to download."""
    import deepinv as dinv
    return _wrap_torch(dinv.models.BM3D(device="cpu"), "cpu")


def gaussian_blur(px):
    """Baseline: Gaussian smoothing with sigma `px` pixels (ignores the noise level)."""
    from scipy.ndimage import gaussian_filter
    return lambda img, sigma: gaussian_filter(img, px, mode="reflect")


def tiled(fn, img, sigma, tile=512, overlap=48):
    """Apply fn tile by tile with linear cross-fading in the overlaps (bounded memory)."""
    h, w = img.shape
    if h <= tile and w <= tile:
        return fn(img, sigma)
    out = np.zeros_like(img)
    weight = np.zeros_like(img)
    step = tile - overlap
    ys = list(range(0, max(h - tile, 0) + 1, step)) or [0]
    xs = list(range(0, max(w - tile, 0) + 1, step)) or [0]
    if ys[-1] + tile < h:
        ys.append(h - tile)
    if xs[-1] + tile < w:
        xs.append(w - tile)
    for y0 in ys:
        for x0 in xs:
            sl = (slice(y0, min(y0 + tile, h)), slice(x0, min(x0 + tile, w)))
            patch = fn(img[sl], sigma)
            th, tw = patch.shape
            ry = np.minimum(np.arange(th) + 1, np.arange(th)[::-1] + 1)
            rx = np.minimum(np.arange(tw) + 1, np.arange(tw)[::-1] + 1)
            win = np.outer(np.minimum(ry, overlap), np.minimum(rx, overlap)).astype(np.float64)
            out[sl] += patch * win
            weight[sl] += win
    return out / weight


# --------------------------------------------------------------------------------------------
# Denoise in the GAT domain
# --------------------------------------------------------------------------------------------
def gat_scaling(z, pct=(0.05, 99.95)):
    """Map z to ~[0, 1] for the network. Noise std 1 becomes 1 / scale."""
    lo, hi = np.percentile(z, pct)
    return float(lo), float(max(hi - lo, 1e-6))


def denoise_counts(n, denoiser, sigma=0.0, scaling=None, tile=512):
    """counts -> GAT -> scale -> denoiser -> unscale -> inverse GAT -> counts.

    Returns (counts, info). info["sigma01"] is the noise level the denoiser was given; above
    ~0.2 a pretrained DRUNet works outside its training range: bin or sum frames first.
    """
    z = gat(n, sigma)
    lo, scale = scaling or gat_scaling(z)
    s01 = 1.0 / scale
    z_hat = tiled(denoiser, (z - lo) / scale, s01, tile=tile) * scale + lo
    return igat(z_hat, sigma), {"gat_lo": lo, "gat_scale": scale, "sigma01": s01, "z_hat": z_hat}


def denoise_counts_no_gat(n, denoiser, tile=512):
    """Baseline without variance stabilisation: one global noise level sqrt(mean count)."""
    lo, hi = np.percentile(n, [0.05, 99.95])
    scale = max(hi - lo, 1e-6)
    s01 = math.sqrt(max(np.mean(n), 1.0)) / scale
    return tiled(denoiser, (n - lo) / scale, s01, tile=tile) * scale + lo, {"sigma01": s01}


# --------------------------------------------------------------------------------------------
# Quality without ground truth
# --------------------------------------------------------------------------------------------
def heldout_error(estimate_counts, counts_stack, k, sigma=0.0, mask=None, tmask=None):
    """MSE of an estimate of frame k against the truth, measured with frames k-1 and k+1.

    In the GAT domain each frame has noise variance ~1, independent between frames.
    T = (Z[k-1] + Z[k+1]) / 2 has variance 1/2 and cancels a linear drift, so
    E[(GAT(est) - T)^2] = MSE + 1/2. Returned value: MSE in units of the raw noise variance
    (raw frame k scores ~1, a perfect denoiser 0). Negative values are noise in the estimate.
    """
    if not 0 < k < len(counts_stack) - 1:
        raise ValueError("k needs a frame before and after it")
    t = (gat(counts_stack[k - 1], sigma) + gat(counts_stack[k + 1], sigma)) / 2
    e = gat(estimate_counts, sigma)
    good = np.ones(e.shape, bool) if mask is None else ~mask
    if tmask is not None:                     # filled pixels borrow from frames k-1 and k+1
        good &= ~tmask[k - 1:k + 2].any(0)
    return float(np.mean((e[good] - t[good]) ** 2) - 0.5)


def rois_for(rois, crop=None, binning=1):
    """ROIs from config.json (raw pixels) in the coordinates of a cropped, binned stack."""
    cx, cy = (crop[0], crop[1]) if crop else (0, 0)
    return {name: None if r is None else [int((r[0] - cx) // binning), int((r[1] - cy) // binning),
                                          max(1, int(r[2] // binning)), max(1, int(r[3] // binning))]
            for name, r in (rois or {}).items()}


def roi_means(img, rois, mask=None):
    """Mean of img in each ROI (x, y, w, h), bad pixels left out. Whole frame if no ROI."""
    out = {}
    for name, roi in (rois or {"frame": None}).items():
        if roi is None and rois:
            continue
        sub = bp.roi_view(img, roi)
        good = np.ones(sub.shape, bool) if mask is None else ~bp.roi_view(mask, roi)
        out[name] = float(sub[good].mean())
    return out or {"frame": float(img.mean())}


# --------------------------------------------------------------------------------------------
# Self-supervised Noise2Noise in the GAT domain (no clean data, no downloaded weights)
# --------------------------------------------------------------------------------------------
def train_noise2noise(z01_stack, mask=None, steps=1500, patch=64, batch=16, lr=1e-3,
                      depth=10, device=None, seed=0, log_every=250, model=None):
    """Learn E[frame k+1 | frame k] on pairs of consecutive GAT frames scaled to [0, 1].

    Consecutive frames see the same leaf with independent noise, so the L2 target is the clean
    image (Noise2Noise). Uses a small DnCNN from deepinv. Returns a denoiser f(img, sigma).
    """
    import torch
    import deepinv as dinv
    dev = _device(device)
    g = torch.Generator().manual_seed(seed)
    torch.manual_seed(seed)
    net = model or dinv.models.DnCNN(1, 1, depth=depth, nf=48, pretrained=None, device=dev)
    net.train()
    data = torch.as_tensor(z01_stack, dtype=torch.float32)
    n, h, w = data.shape
    p = min(patch, h, w)
    opt = torch.optim.Adam(net.parameters(), lr=lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    valid = None if mask is None else torch.as_tensor(~mask, dtype=torch.float32)
    log = []
    for it in range(steps):
        k = torch.randint(0, n - 1, (batch,), generator=g)
        y0 = torch.randint(0, h - p + 1, (batch,), generator=g)
        x0 = torch.randint(0, w - p + 1, (batch,), generator=g)
        a = torch.stack([data[k[i], y0[i]:y0[i] + p, x0[i]:x0[i] + p] for i in range(batch)])
        b = torch.stack([data[k[i] + 1, y0[i]:y0[i] + p, x0[i]:x0[i] + p] for i in range(batch)])
        swap = torch.rand(batch, generator=g) < 0.5            # both directions
        a, b = torch.where(swap[:, None, None], b, a), torch.where(swap[:, None, None], a, b)
        if torch.rand(1, generator=g) < 0.5:
            a, b = a.flip(-1), b.flip(-1)
        if torch.rand(1, generator=g) < 0.5:
            a, b = a.flip(-2), b.flip(-2)
        a, b = a[:, None].to(dev), b[:, None].to(dev)
        wgt = 1.0
        if valid is not None:
            wgt = torch.stack([valid[y0[i]:y0[i] + p, x0[i]:x0[i] + p]
                               for i in range(batch)])[:, None].to(dev)
        loss = (((net(a) - b) ** 2) * wgt).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if log_every and (it % log_every == 0 or it == steps - 1):
            log.append((it, loss.item()))
            print(f"  step {it:5d}  loss {loss.item():.5f}")
    net.eval()
    f = _wrap_torch(net, dev)
    denoiser = lambda img, sigma: f(img, None)
    denoiser.net, denoiser.log = net, log
    return denoiser


# --------------------------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------------------------
def fig_noise_model(model, unit="ADU"):
    import matplotlib.pyplot as plt
    bm, bv = np.array(model["bins_mean"]), np.array(model["bins_var"])
    g, c = model["gain_adu_per_count"], model["intercept_adu2"]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.2))
    ax = axes[0]
    ax.plot(bm, bv, "o", ms=4, label="binned pixels (instrumental variable)")
    ax.plot(bm, g * bm + c, "-", lw=1.5,
            label=f"var = {g:.4g} * mean {'+' if c >= 0 else '-'} {abs(c):.4g}")
    ax.set_xlabel(f"temporal mean [{unit}]")
    ax.set_ylabel(f"temporal variance [{unit}²]")
    ax.set_title(f"Noise model: {g:.4g} {unit} per count, offset {model['offset_adu']:.4g} {unit}")
    ax.legend()
    ax.grid(alpha=0.3)
    ax = axes[1]
    ax.plot(bm, bv / (g * bm + c) - 1, "o", ms=4)
    ax.axhline(0, color="0.5", lw=0.8)
    ax.set_xlabel(f"temporal mean [{unit}]")
    ax.set_ylabel("variance / fit - 1")
    ax.set_title(f"Fit residual, rms {model['fit_rel_residual_rms']:.1%}")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def fig_stabilisation(check):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(check["z_mean"], check["z_std"], "o", ms=4)
    ax.axhline(1, color="g", lw=1, label="target: 1")
    ax.axhspan(0.95, 1.05, color="g", alpha=0.1)
    ax.set_ylim(0.6, max(1.6, max(check["z_std"]) * 1.05))
    ax.set_xlabel("GAT level (2 sqrt(counts))")
    ax.set_ylabel("temporal noise std after GAT")
    ax.set_title(f"Variance stabilisation (overall {check['z_std_overall']:.3f})")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def fig_compare(images, titles, stretch_from=0, rois=None, residual_of=None, sigma=0.0):
    """Row of images on one shared stretch; optional second row: GAT(raw) - GAT(estimate).

    The residual of a good denoiser is structureless noise with std ~1; leaf edges or dots in
    it are signal the denoiser removed.
    """
    import matplotlib.pyplot as plt
    rows = 2 if residual_of is not None else 1
    n = len(images)
    fig, axes = plt.subplots(rows, n, figsize=(4.2 * n, 4.2 * rows), squeeze=False)
    lo, hi = np.percentile(images[stretch_from], [1, 99.7])
    for i, (img, t) in enumerate(zip(images, titles)):
        ax = axes[0, i]
        ax.imshow(img, cmap="magma", vmin=lo, vmax=hi)
        bp._draw_rois(ax, rois)
        ax.set_title(t, fontsize=10)
        ax.axis("off")
        if rows == 2:
            ax = axes[1, i]
            r = gat(residual_of, sigma) - gat(img, sigma)
            ax.imshow(r, cmap="RdBu_r", vmin=-3, vmax=3)
            ax.set_title(f"GAT(raw) - GAT(est), std {np.std(r):.2f}", fontsize=9)
            ax.axis("off")
    fig.tight_layout()
    return fig


def fig_scores(scores):
    """Bar chart of held-out error (raw noise = 1) per method."""
    import matplotlib.pyplot as plt
    names = list(scores)
    vals = [scores[k] for k in names]
    fig, ax = plt.subplots(figsize=(8, 0.45 * len(names) + 1.2))
    bars = ax.barh(names, vals, color=["0.6" if k.startswith(("raw", "mean of")) else "C0"
                                        for k in names])
    for b, v in zip(bars, vals):
        ax.text(b.get_width(), b.get_y() + b.get_height() / 2, f" {v:.3f}", va="center")
    ax.axvline(1, color="0.4", lw=0.8, ls="--")
    ax.invert_yaxis()
    ax.set_xlabel("held-out error vs truth (raw frame noise = 1, lower is better)")
    fig.tight_layout()
    return fig


def fig_timecourse(t_h, raw, den, unit="counts / px"):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 3.4))
    for name in raw:
        ax.plot(t_h, raw[name], "o", ms=3, alpha=0.5, label=f"{name}, raw")
        ax.plot(t_h, den[name], "-", lw=1.5, label=f"{name}, denoised")
    ax.set_xlabel("time since run start [h]")
    ax.set_ylabel(f"ROI mean [{unit}]")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------------
def out_dir(run_dir, name="denoise"):
    d = Path(run_dir) / name
    (d / "figures").mkdir(parents=True, exist_ok=True)
    return d


def save_tif(path, img):
    tifffile.imwrite(path, np.asarray(img, np.float32))


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, default=float))
