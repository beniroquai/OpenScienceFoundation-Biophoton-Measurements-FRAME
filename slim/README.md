# Slim protocol for the preprint

The smallest set of measurements that carries the claims of the manuscript, in three notebooks.
It replaces the separate dark nights, the temperature series and the air-phase experiments of
E1-E4 with:

- **one operating point per camera** and one sensor temperature for every run
  (`slim.CAMERAS`: ToupTek gain 23 / HCG / black level 1000 / -28 °C; Andor EM 300 / 1 MHz /
  4x4 / -80 °C);
- **dark frames from a shutter during the sample run**, one after every few light frames, so
  each run carries its own background;
- **photon transfer with the beta light in the infinity space**, imaged in focus onto the sensor:
  its profile is a ramp of grey values in every frame, an exposure ladder stretches the ramp;
- **each leaf its own control**: water baseline, exchange to H2O2 by the pump, response, in one
  unattended run (no air phase).

| Notebook | What | Time | Gives |
|---|---|---|---|
| `S1_calibration.ipynb` | Bias, photon transfer + linearity (beta light in the infinity space), beta light at the sample plane | ~1 h attended, per camera and per optics | e-/ADU, read noise, offset, linearity, total e-/s of the reference |
| `S2_leaf_water_h2o2.ipynb` | One leaf: fill water, darks, baseline, exchange to H2O2, response, darks; darks interleaved | one night per leaf (~10 h) | dark-subtracted rate per frame, baseline vs response, ratio, maps |
| `S3_summary.ipynb` | All S1 and S2 runs | minutes | `data/slim_summary/`: leaves, groups (n, ratio ± SD), calibration, figures |

The same notebooks run the controls (`CONDITION` in S2, `slim.CONDITIONS`):

| Condition | Mounted | Baseline | Response | Rules out |
|---|---|---|---|---|
| `leaf` | injured leaf | water | H2O2 | (the effect) |
| `water_control` | injured leaf | water | fresh water | the exchange itself, time since wounding |
| `no_leaf` | empty chamber | water | H2O2 | reagent and chamber luminescence |
| `heat_killed` | heat-killed leaf | water | H2O2 | chemistry of dead tissue |

## Plan

**Session 1, current relay (2x Plan Apo 4x/0.20)**

1. S1 on each camera, with the beta light at the sample plane (`REFERENCE_MS`). The ToupTek
   reference **must** be taken before the optics change: it is the baseline for the relay
   comparison.
2. S2 on the EMCCD: `leaf` x 5 (or 3 if time is short), `water_control` x 2, `no_leaf` x 1,
   `heat_killed` x 1.
3. S2 on the ToupTek: `leaf` x 2 (detects the stimulated emission, in n of N).

**Session 2, the 50 mm f/0.95 pair on the ToupTek**

1. S1 on the ToupTek with `OPTICS = {"name": "50/0.95 pair", ...}`: the sample-plane reference
   against session 1 is the measured gain of the new relay.
2. S2 `leaf` x 3 on the ToupTek with the new optics.

S3 groups everything by camera, optics and condition. About 12 nights plus two attended S1
sessions; fewer with several chambers per night.

## Before the first real run

- **Shutter**: between the relay and the sensor, so it blocks the leaf and the beta light. Once
  ImSwitch can move it, set `SHUTTER_OPEN` / `SHUTTER_CLOSE` in S1/S2 to `slim.endpoint(client,
  "<Controller>/<function>", {...})` or any Python function. Until then: `DARKS = "ends"` in S2
  (dark frames at the start and the end, you cap the camera when asked).
- **Pump and valves**: `PUMP` in S2 (`select` per reservoir, `pump_on`, `pump_off`). Until then
  the notebook asks you for the exchange: plan `BASELINE_H` so that it falls into working hours.
  Measure the chamber's exchange time with dye and set `FLUSH_S` to at least three volumes.
- When the run waits for you, the dashboard says so: interrupt it (■), run "Operator done".
- **Temperature**: if the ToupTek does not hold -28 °C on the freezer loop, change
  `slim.CAMERAS["toupcam"]["settings"]["target_temperature"]` once, before the first S1, and keep
  it for every run.

## Analysis in short

- S2: the dark level under every light frame is interpolated in time between the run's dark
  frames (same ROI, same exposure), so dark-signal non-uniformity and slow drifts cancel. The
  baseline is the mean of the last `BASELINE_WINDOW_H` in water (the afterglow of mounting decays
  before), the response the mean of the first `RESPONSE_WINDOW_H` after the exchange.
  `ratio_err` and the `_sem` values are frame-to-frame errors within one run; S3 reports the
  spread between leaves.
- S1: the photon-transfer fit bins pixels by the mean of one half of the frames and measures
  mean and variance on the other half, so noise cannot bias the slope; for the EMCCD the slope
  is divided by F² = 2. The dark current from the short ladder is rough: S3 reports the dark
  signal at the run exposure from the S2 dark frames.
- Every run folder holds its own `slim.py`, `biophoton.py` and `report.ipynb`
  (`slim.redraw(RUN_DIR)`), like E1-E4.

## Simulation and tests

`SIMULATE = True` (the default in the notebooks) runs everything against `slim.Simulator`,
which stands in for ImSwitch, the camera, the shutter, the pump and the beta light; its runs go
to `data/simulated/`. Simulated runs take seconds, so their time axes are compressed.

`python test_slim.py` drives the real acquisition code against the simulator and checks that the
analysis recovers the simulated conversion gain, read noise, linearity and sample-plane total
(both cameras), the leaf ratio under a drifting dark level, the controls, the S3 summary, and the
operator handshake from the background thread.
