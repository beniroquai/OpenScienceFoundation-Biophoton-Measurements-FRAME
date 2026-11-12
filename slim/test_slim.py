"""Checks of the slim protocol against the simulator. Run: python test_slim.py (or pytest).

Each test drives the real acquisition code (ProtocolAcquisition, run folders, environment log)
with `slim.Simulator` standing in for ImSwitch, the shutter, the pump and the beta light, then
checks that the analysis recovers what the simulator was told.
"""
import json
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

import slim

QUIET = lambda *a, **k: None    # noqa: E731


def run_protocol(sim, camera, experiment, steps, root, shutter=None, fluidics=None,
                 operator=None, background=False, **config):
    run, env, detector, preset = slim.prepare(sim, camera, experiment, "test", dry_run=True,
                                              data_root=root, env_period_s=3600, **config)
    run.update_config(steps=steps)
    acq = slim.ProtocolAcquisition(run, sim, detector, steps,
                                   shutter or sim.shutter(log=QUIET),
                                   fluidics or sim.fluidics(log=QUIET),
                                   operator=operator or sim.operator,
                                   backend=preset["backend"], poll_s=0)
    if background:
        return run, env, acq.start()
    acq.run()
    env.stop()
    assert acq.state["phase"] == "done", acq.state
    return run, env, acq


def expected_leaf_rates(sim, roi):
    """Electrons per pixel per second in `roi` without afterglow: (water, h2o2)."""
    x, y, w, h = roi
    m = sim.leaf_mask[y:y + h, x:x + w].astype(float)
    e = sim.leaf_edge[y:y + h, x:x + w].astype(float)
    f = sim.media["h2o2"]
    base = sim.leaf_e_s * m.mean()
    resp = sim.leaf_e_s * (m * (1 + (f - 1) * 0.5) + e * (f - 1) * 3).mean()
    return base, resp


def test_leaf_schedule():
    steps = slim.leaf_schedule(1200, 3, 6, dark_every=3)
    labels = [s["label"] for s in steps]
    assert labels[0] == "fill" and steps[0]["exchange"] == "water"
    assert sum(1 for s in steps if s.get("exchange") == "h2o2") == 1
    n = {}
    for s in steps:
        if s.get("exposure_ms"):
            n[s["label"]] = n.get(s["label"], 0) + s["n_frames"]
    assert n["baseline"] + n["response"] + n["dark"] == 9 + 18 + 4, n
    assert n["dark"] == 4 + 2 + 4, n           # 2 + 2 at the ends, 2 in baseline, 4 in response
    ends = slim.leaf_schedule(1200, 3, 6, darks="ends")
    assert sum(s["n_frames"] for s in ends if s["label"] == "dark") == 4
    print(slim.describe(steps))


def check_calibration(camera, exposures_ms, root):
    sim = slim.Simulator(camera, seed=1)
    steps = slim.calibration_schedule(exposures_ms, n_frames=8, n_bias=20, bias_ms=1,
                                      reference_ms=exposures_ms[-1])
    run, _, _ = run_protocol(sim, camera, "S1_calibration", steps, root,
                             kind="slim_calibration", optics={"name": "test", "magnification": 1},
                             analysis={"pixel_step": 1})
    s = slim.redraw(run.dir)
    m = sim.model
    assert abs(s["adu_per_e"] / m["adu_per_e"] - 1) < 0.05, (camera, s["adu_per_e"])
    assert abs(s["read_noise_adu"] / m["read_adu"] - 1) < 0.1, (camera, s["read_noise_adu"])
    assert abs(s["offset_adu"] - m["offset"]) < 0.05 * m["read_adu"] + 2, s["offset_adu"]
    assert s["linearity_max_residual_pct"] < 3, s["linearity_max_residual_pct"]
    expect = (sim.ref * sim.beta_e_s * 0.05).sum()
    assert abs(s["ref_total_e_s"] / expect - 1) < 0.08, (s["ref_total_e_s"], expect)
    print(f"{camera}: {s['adu_per_e']:.1f} ADU/e- (true {m['adu_per_e']}), read noise "
          f"{s['read_noise_adu']:.1f} ADU, linearity {s['linearity_max_residual_pct']:.2f} %, "
          f"reference {s['ref_total_e_s']:.0f} e-/s (true {expect:.0f})")
    return s


def test_calibration():
    with tempfile.TemporaryDirectory() as tmp:
        check_calibration("toupcam", [2, 5, 10, 20, 25], tmp)
        check_calibration("andor", [5, 10, 20, 50, 100], tmp)


def test_leaf_run_interleaved_darks_with_drift():
    """The dark level drifts by 20 ADU/h; interleaved darks still give the right ratio."""
    with tempfile.TemporaryDirectory() as tmp:
        sim = slim.Simulator("toupcam", seed=2, afterglow=0, offset_drift_adu_h=20)
        steps = slim.leaf_schedule(1200, 3, 4, dark_every=3)
        run, _, _ = run_protocol(sim, "toupcam", "S2_leaf_water_h2o2", steps, tmp,
                                 kind="slim_leaf", condition="leaf",
                                 optics={"name": "test", "magnification": 1},
                                 analysis={"e_per_adu": 1 / sim.model["adu_per_e"]})
        slim.bp.set_rois(run.dir, sim.rois)
        s = slim.redraw(run.dir)
        base, resp = expected_leaf_rates(sim, sim.rois["sample"])
        assert abs(s["sample_baseline_e"] / base - 1) < 0.1, (s["sample_baseline_e"], base)
        assert abs(s["sample_ratio"] / (resp / base) - 1) < 0.1, (s["sample_ratio"], resp / base)
        assert abs(s["background_baseline_e"]) < 0.1 * base, s["background_baseline_e"]
        proto = slim.read_protocol(run.dir)
        assert list(proto["value"][proto["action"].astype(str) == "exchange_done"]) == ["water", "h2o2"]
        print(f"leaf: ratio {s['sample_ratio']:.2f} (true {resp / base:.2f}), baseline "
              f"{s['sample_baseline_e']:.4f} e-/px/s (true {base:.4f}), dark drift "
              f"{s['dark_drift_adu']:.0f} ADU")
        return s


def test_controls_and_summary():
    with tempfile.TemporaryDirectory() as tmp:
        cal_sim = slim.Simulator("toupcam", seed=3)
        cal_steps = slim.calibration_schedule([2, 5, 10, 20, 25], n_frames=8, reference_ms=25)
        run_protocol(cal_sim, "toupcam", "S1_calibration", cal_steps, tmp,
                     kind="slim_calibration", optics={"name": "4x/0.20 pair", "magnification": 1})
        ratios = {}
        for cond, leaf, seed in (("leaf", True, 4), ("leaf", True, 5), ("water_control", True, 6),
                                 ("no_leaf", False, 7)):
            c = slim.CONDITIONS[cond]
            sim = slim.Simulator("toupcam", seed=seed, afterglow=0, leaf=leaf)
            steps = slim.leaf_schedule(1200, 2, 2, c["baseline"], c["response"], dark_every=3)
            run, _, _ = run_protocol(sim, "toupcam", "S2_leaf_water_h2o2", steps, tmp,
                                     kind="slim_leaf", condition=cond,
                                     sample={"id": f"leaf{seed}"},
                                     optics={"name": "4x/0.20 pair", "magnification": 1})
            slim.bp.set_rois(run.dir, sim.rois)
            ratios[(cond, seed)] = slim.redraw(run.dir).get("sample_ratio")
            time.sleep(1.1)                # run folders are named by the second
        assert abs(ratios[("water_control", 6)] - 1) < 0.1, ratios
        cal, leaves = slim.collect(tmp, include_dry=True)
        assert len(cal) == 1 and len(leaves) == 4
        assert all(r["calibration"] == cal[0]["run"] for r in leaves)
        no_leaf = next(r for r in leaves if r["condition"] == "no_leaf")
        leaf = next(r for r in leaves if r["condition"] == "leaf")
        assert abs(no_leaf["baseline_e_mm2_s"]) < 0.1 * leaf["baseline_e_mm2_s"], (no_leaf, leaf)
        dark_true = cal_sim.model["dark_e_s"] * cal_sim.dsnu[20:60, 20:60].mean()  # roughly the ROI
        assert 0.5 < leaf["dark_signal_e_px_s"] / dark_true < 1.5, (leaf["dark_signal_e_px_s"], dark_true)
        stats = slim.group_stats(leaves)
        assert next(s for s in stats if s["condition"] == "leaf")["n"] == 2
        out = Path(tmp) / "slim_summary"
        fig = slim.fig_paired(leaves)
        out.joinpath("figures").mkdir(parents=True)
        fig.savefig(out / "figures" / "paired.png")
        slim.write_summary(out, cal, leaves, stats)
        assert (out / "README.md").read_text().count("| toupcam |") == 3
        print("controls:", {k[0]: round(v, 2) for k, v in ratios.items() if v})


def test_operator_in_background():
    """Darks at the ends with a shutter the operator moves, while the acquisition runs in the
    background thread: the run waits for confirm() each time."""
    with tempfile.TemporaryDirectory() as tmp:
        sim = slim.Simulator("toupcam", seed=8, afterglow=0)
        asked = []
        op = slim.Operator(log=asked.append)
        shutter = slim.Shutter("prompt", operator=op, log=QUIET)
        steps = slim.leaf_schedule(600, 0.5, 0.5, darks="ends", start_darks=1, end_darks=1)
        run, env, acq = run_protocol(sim, "toupcam", "S2_leaf_water_h2o2", steps, tmp,
                                     shutter=shutter, operator=op, background=True,
                                     kind="slim_leaf", condition="leaf")
        confirmed, t_end = 0, time.time() + 60
        while acq.running and time.time() < t_end:
            if op.pending and op.asked > confirmed:     # a new request
                confirmed += 1
                op.confirm()
            time.sleep(0.05)
        env.stop()
        assert acq.state["phase"] == "done", acq.state
        assert confirmed == 3, (confirmed, asked)       # close, open, close
        print(f"operator confirmed {confirmed} shutter moves in the background")


class SerialRecorder:
    """Stands in for ImSwitch's UC2ConfigController/writeSerial: records (channel, value)."""

    base_uri = "http://test"

    def __init__(self):
        self.sent = []

    def get_json(self, url, payload=None):
        assert url.endswith("/UC2ConfigController/writeSerial"), url
        cmd = json.loads(payload["payload"])
        assert cmd["task"] == "/laser_act", cmd
        self.sent.append((cmd["LASERid"], cmd["LASERval"]))
        return 1


def test_slider_and_pumps():
    """Dark slider on channels 1/2 and pumps on 3/4: the /laser_act sequence ImSwitch sends."""
    esp = SerialRecorder()
    slider = slim.bp.DarkSlider(esp, travel_s=0, middle_s=0, log=QUIET)
    slider.beta()
    assert esp.sent == [(2, 0), (1, 1023), (1, 0)], esp.sent
    esp.sent.clear()
    slider.dark()          # to the open stop, then back towards the beta light for middle_s
    assert esp.sent == [(1, 0), (2, 1023), (2, 0), (2, 0), (1, 1023), (1, 0)], esp.sent
    assert slider.position == "dark"

    esp.sent.clear()
    pumps = slim.bp.Pumps(esp, {"water": 3, "h2o2": 4}, log=QUIET)
    hooks = pumps.hooks()
    fluidics = slim.Fluidics("callable", hooks["select"], hooks["pump_on"], hooks["pump_off"],
                             flush_s=0, settle_s=0, log=QUIET)
    fluidics.exchange("h2o2")
    assert esp.sent == [(3, 0), (4, 0), (4, 1023), (3, 0), (4, 0)], esp.sent
    try:
        fluidics.exchange("ethanol")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown reservoir accepted")

    steps = slim.calibration_schedule([10, 20], n_frames=2, beta_on_slider=True)
    assert not any(s.get("prompt") for s in steps), steps
    try:
        slim.calibration_schedule([10], reference_ms=10, beta_on_slider=True)
    except ValueError:
        pass
    else:
        raise AssertionError("sample-plane reference accepted with the beta light on the slider")


if __name__ == "__main__":
    import matplotlib
    matplotlib.use("Agg")
    tests = [test_leaf_schedule, test_calibration, test_leaf_run_interleaved_darks_with_drift,
             test_controls_and_summary, test_operator_in_background, test_slider_and_pumps]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}\n")
        except Exception as e:                   # report all, not only the first
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}\n")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)
