# E5 denoising - 2026-10-27_19-34-50_cress_h2o2

- noise model: 153.7 ADU/count, offset 484.1 ADU, fit residual 2.3%
- transients 4.65% of pixel-frames, 1.48% of pixels left out
- GAT noise std 1.009 (range 0.98-1.05)
- best single-frame method: GAT + Noise2Noise

| method | held-out error | ~frames averaged |
|---|---|---|
| raw frame | 1.036 | 1.0 |
| mean of 4 nearby frames | 0.268 | 3.7 |
| mean of 21 frames | 0.079 | 12.7 |
| GAT + DRUNet | 0.140 | 7.1 |
| DRUNet, no GAT | 0.168 | 5.9 |
| GAT + Noise2Noise | 0.138 | 7.3 |

Figures in `figures/`, images in counts as float32 TIFF. Settings and numbers in `summary.json`.
