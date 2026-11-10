"""Slim protocol for the preprint: S1 calibration, S2 leaf in water then H2O2, S3 summary.

What makes it slim (see README.md in this folder):

- one operating point per camera, one sensor temperature for every run (`CAMERAS`);
- dark frames come from a shutter during the sample run (`Shutter`, interleaved with the light
  frames), so there are no separate dark nights;
- photon transfer uses the beta light in the infinity space, imaged onto the sensor: its profile
  gives a ramp of grey values in every frame, an exposure ladder stretches it (`S1`);
- every leaf is its own control: water baseline, exchange to H2O2 by the pump (`Fluidics`),
  response, all in one unattended run; controls use the same notebook (`CONDITIONS`).

Builds on ../biophoton.py (run folders, ImSwitch access, environment log, acquisition). The
shutter and the pump are reached through callables, so they work as soon as ImSwitch exposes
them (`endpoint`), and until then through the operator (`Operator`). `Simulator` stands in for
ImSwitch, the shutter, the pump and the beta light, for dry runs and for test_slim.py.
"""
import csv
import datetime
import json
import math
import sys
import threading
import time
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import tifffile

HERE = Path(__file__).resolve().parent
try:                                   # a run folder holds its own copy of biophoton.py
    import biophoton as bp
except ImportError:                    # the notebooks in slim/ use the one in the repository
    sys.path.insert(0, str(HERE.parent))
    import biophoton as bp

DATA_ROOT = HERE.parent / "data"
PROTOCOL_COLUMNS = ["iso_time", "unix_time", "action", "value", "note"]
_LOCK = threading.Lock()

# --------------------------------------------------------------------------------------------
# One operating point per camera. Every S1 and S2 run uses it unchanged.
# --------------------------------------------------------------------------------------------
CAMERAS = {
    "toupcam": {
        "label": "ToupTek MTR3CMOS09000KMA (IMX533)",
        "detector": None,                  # None: the server's first detector
        "backend": "recording",
        "settings": {                      # ToupCamManager names, applied in this order
            "exposure_mode": "manual",
            "pixel_format": "mono16",      # before the black level: its range depends on bit depth
            "conversion_gain": "HCG",
            "low_noise": True,
            "gain": 23,                    # maximum analog gain
            "blacklevel": 1000,            # fixed offset, OB auto-adjust off
            "target_temperature": -28,     # a few K above the lowest reachable: TEC keeps headroom
        },
        "cooldown": {"tolerance_c": 1.0, "timeout_s": 45 * 60},
        "settle_s": 20 * 60,               # after reaching the set point (E4 frame 0 was 5 K warm)
        "excess_noise_sq": 1.0,            # F^2: 1 for a CMOS
        "pixel_um": 3.76,                  # pixel pitch on the sensor
        "readout_binning": 1,              # binning applied by the camera (frames as saved)
        "image_binning": 4,                # software binning for the images of the analysis
        "full_scale_adu": 16383,
        "transient_dilate": [3, 3],
    },
    "andor": {
        "label": "Andor iXon Ultra 888 EMCCD",
        "detector": "Andor",
        "backend": "recording",
        "settings": {                      # AndorServerManager names, applied in this order
            "output_amplifier": "Electron Multiplying",   # first: resets readout rate + pre-amp
            "readout_rate": "1 MHz",
            "gain": 300,                   # EM gain
            "binning": 4,                  # 4x4 on chip: 52 µm super-pixels
            "cooler": True,
            "target_temperature": -80,     # stable for 12 h (E3)
        },
        "cooldown": {"tolerance_c": 1.0, "timeout_s": 45 * 60},
        "settle_s": 10 * 60,
        "excess_noise_sq": 2.0,            # EM register: variance doubles
        "pixel_um": 13.0,
        "readout_binning": 4,
        "image_binning": 1,
        "full_scale_adu": 65535,
        "transient_dilate": [1, 15],
    },
}

# What goes into the chamber before and after the exchange, and what is mounted.
CONDITIONS = {
    "leaf": {"sample": "injured leaf", "baseline": "water", "response": "h2o2"},
    "water_control": {"sample": "injured leaf", "baseline": "water", "response": "water"},
    "no_leaf": {"sample": "empty chamber", "baseline": "water", "response": "h2o2"},
    "heat_killed": {"sample": "heat-killed leaf", "baseline": "water", "response": "h2o2"},
}


# --------------------------------------------------------------------------------------------
# Operator, shutter, pump
# --------------------------------------------------------------------------------------------
class Operator:
    """Asks the person at the microscope for a step by hand.

    From the notebook's main thread it waits for Enter. From the background acquisition it
    waits until `confirm()` is called (the "Operator done" cell); `pending` holds the request,
    and `dashboard()` shows it.
    """

    def __init__(self, log=print, dry_run=False, on_ask=None):
        self.log, self.dry_run, self.on_ask = log, dry_run, on_ask
        self.pending, self.asked = None, 0
        self._done = threading.Event()

    def ask(self, message, stop_event=None):
        if self.on_ask:                    # e.g. the simulator moving the beta light
            self.on_ask(message)
        if self.dry_run:
            self.log(f"operator (dry run, not waiting): {message}")
            return
        if threading.current_thread() is threading.main_thread():
            bp.prompt(message, False, self.log)
            return
        self._done.clear()
        self.asked += 1
        self.pending = message
        self.log(f"operator: {message} -> interrupt the dashboard and run the 'Operator done' cell")
        try:
            while not self._done.wait(2):
                if stop_event is not None and stop_event.is_set():
                    raise InterruptedError("stopped while waiting for the operator")
        finally:
            self.pending = None
        self.log("operator: done")

    def confirm(self):
        self._done.set()


def endpoint(client, path, params=None, method="get"):
    """Callable that calls an ImSwitch REST endpoint, e.g. for the shutter once it exists:
    endpoint(client, "UC2ConfigController/setShutter", {"state": 0}). Path and parameter names
    are whatever the ImSwitch function defines."""
    url = f"{client.base_uri}/{path.lstrip('/')}"

    def call():
        fn = client.get_json if method == "get" else client.post_json
        return fn(url, payload=dict(params or {}))
    call.__name__ = f"{method.upper()} {path} {params or ''}"
    return call


class Shutter:
    """The dark-frame shutter between the relay and the sensor (blocks the leaf and the beta light).

    mode "callable": open_fn() / close_fn() move it (e.g. `endpoint(...)`).
    mode "prompt":   the operator does it (fine for darks at the start and end of a run).
    mode "log":      only logged (dry runs).
    """

    def __init__(self, mode="log", open_fn=None, close_fn=None, operator=None, settle_s=1.0,
                 log=print):
        if mode not in ("callable", "prompt", "log"):
            raise ValueError(f"shutter mode {mode!r}")
        if mode == "callable" and not (open_fn and close_fn):
            raise ValueError("shutter mode 'callable' needs open_fn and close_fn")
        self.mode, self.open_fn, self.close_fn = mode, open_fn, close_fn
        self.operator = operator or Operator(log)
        self.settle_s, self.log = settle_s, log
        self.state = None

    @property
    def automatic(self):
        return self.mode in ("callable", "log")

    def set(self, state, stop_event=None):
        if state not in ("open", "closed"):
            raise ValueError(f"shutter state {state!r}")
        if state == self.state:
            return
        if self.mode == "callable":
            (self.open_fn if state == "open" else self.close_fn)()
            time.sleep(self.settle_s)
        elif self.mode == "prompt":
            self.operator.ask("OPEN the shutter (uncap the camera)" if state == "open"
                              else "CLOSE the shutter (cap the camera)", stop_event)
        self.state = state
        self.log(f"shutter {state}")


class Fluidics:
    """Exchanges the medium in the flow chamber: select the reservoir, pump `flush_s`, stop, settle.

    mode "callable": select = {"water": fn, "h2o2": fn}, pump_on(), pump_off().
    mode "prompt":   the operator exchanges the medium (keep the enclosure dark).
    mode "log":      only logged (dry runs).
    """

    def __init__(self, mode="log", select=None, pump_on=None, pump_off=None, flush_s=120,
                 settle_s=300, operator=None, log=print):
        if mode not in ("callable", "prompt", "log"):
            raise ValueError(f"fluidics mode {mode!r}")
        if mode == "callable" and not (select and pump_on and pump_off):
            raise ValueError("fluidics mode 'callable' needs select, pump_on and pump_off")
        self.mode, self.select, self.pump_on, self.pump_off = mode, select or {}, pump_on, pump_off
        self.flush_s, self.settle_s = flush_s, settle_s
        self.operator = operator or Operator(log)
        self.log = log

    def exchange(self, reagent, stop_event=None):
        stop_event = stop_event or threading.Event()
        if self.mode == "callable":
            if reagent not in self.select:
                raise KeyError(f"no reservoir for {reagent!r}: {sorted(self.select)}")
            self.select[reagent]()
            self.pump_on()
            try:
                stop_event.wait(self.flush_s)
            finally:
                self.pump_off()
        elif self.mode == "prompt":
            self.operator.ask(f"EXCHANGE the medium for {reagent} (flush the chamber, keep it dark)",
                              stop_event)
        self.log(f"medium is now {reagent}")
        if self.mode != "log":
            stop_event.wait(self.settle_s)


def log_protocol(run_dir, action, value, note=""):
    """One row in protocol.csv: what was switched when (shutter, medium, light source)."""
    path = Path(run_dir) / "protocol.csv"
    now = time.time()
    with _LOCK:
        new = not path.exists()
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, PROTOCOL_COLUMNS)
            if new:
                w.writeheader()
            w.writerow({"iso_time": datetime.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                        "unix_time": now, "action": action, "value": value, "note": note})


class ProtocolAcquisition(bp.Acquisition):
    """bp.Acquisition plus actions before a step.

    Extra step keys: "prompt" (the operator confirms first), "exchange" (medium; a step without
    "exposure_ms" does only that), "shutter" ("open" / "closed"). Each action goes to
    protocol.csv, so the analysis knows when the medium changed.
    """

    def __init__(self, run, client, detector, steps, shutter, fluidics=None, operator=None,
                 **kwargs):
        super().__init__(run, client, detector, steps, **kwargs)
        self.shutter, self.fluidics = shutter, fluidics
        self.operator = operator or shutter.operator

    def _run_step(self, step_index, step, index):
        d = self.run_folder.dir
        if step.get("prompt"):
            self.state.update(step=step_index, phase="waiting for operator", detail=step["prompt"])
            log_protocol(d, "prompt", step["prompt"])
            self.operator.ask(step["prompt"], self.stop_event)
        if step.get("exchange"):
            if self.fluidics is None:
                raise RuntimeError("step needs a medium exchange but no Fluidics was given")
            self.state.update(step=step_index, phase=f"exchange to {step['exchange']}", detail="")
            log_protocol(d, "exchange_start", step["exchange"])
            self.fluidics.exchange(step["exchange"], self.stop_event)
            log_protocol(d, "exchange_done", step["exchange"])
        if self.stop_event.is_set():
            raise InterruptedError("stopped")
        if not step.get("exposure_ms"):
            return index
        if step.get("shutter"):
            self.shutter.set(step["shutter"], self.stop_event)
            log_protocol(d, "shutter", step["shutter"])
        return super()._run_step(step_index, step, index)


# --------------------------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------------------------
def calibration_schedule(exposures_ms, n_frames=8, n_bias=20, bias_ms=1, reference_ms=None,
                         n_reference=8):
    """S1: bias, then the beta light in the infinity space over an exposure ladder (light and
    dark frames at each exposure), then optionally the beta light at the sample plane."""
    steps = [{"label": "bias", "shutter": "closed", "exposure_ms": bias_ms, "n_frames": n_bias},
             {"label": "source", "prompt": "Put the beta light into the INFINITY SPACE (lens in "
                                            "front of it) and close the enclosure"}]
    for ms in exposures_ms:
        steps += [{"label": "ptc_light", "shutter": "open", "exposure_ms": ms, "n_frames": n_frames},
                  {"label": "ptc_dark", "shutter": "closed", "exposure_ms": ms, "n_frames": n_frames}]
    if reference_ms:
        steps += [{"label": "source", "prompt": "Move the beta light to the SAMPLE PLANE (chamber "
                                                 "position, in focus) and close the enclosure"},
                  {"label": "ref_light", "shutter": "open", "exposure_ms": reference_ms,
                   "n_frames": n_reference},
                  {"label": "ref_dark", "shutter": "closed", "exposure_ms": reference_ms,
                   "n_frames": n_reference}]
    steps.append({"label": "source", "prompt": "Remove the beta light"})
    return steps


def leaf_schedule(exposure_s, baseline_h, response_h, baseline_medium="water",
                  response_medium="h2o2", dark_every=3, start_darks=2, end_darks=2,
                  darks="interleaved", fill_at_start=True):
    """S2: [fill] -> darks -> baseline in `baseline_medium` -> exchange -> response -> darks.

    darks "interleaved": one dark frame after every `dark_every` light frames (needs a shutter
    that moves by itself); "ends": dark frames only at the start and the end of the run.
    Light frames are labelled "baseline" / "response", dark frames "dark".
    """
    if darks not in ("interleaved", "ends"):
        raise ValueError(f"darks {darks!r}")
    ms = exposure_s * 1000
    steps = []
    if fill_at_start:
        steps.append({"label": "fill", "exchange": baseline_medium})
    if start_darks:
        steps.append({"label": "dark", "shutter": "closed", "exposure_ms": ms,
                      "n_frames": start_darks})
    for phase, medium, hours in (("baseline", baseline_medium, baseline_h),
                                 ("response", response_medium, response_h)):
        if phase == "response":
            steps.append({"label": "exchange", "exchange": medium})
        slots, k = max(1, round(hours * 3600 / exposure_s)), 0
        while k < slots:
            m = min(dark_every, slots - k) if darks == "interleaved" else slots - k
            steps.append({"label": phase, "shutter": "open", "exposure_ms": ms, "n_frames": m})
            k += m
            if darks == "interleaved" and k < slots:
                steps.append({"label": "dark", "shutter": "closed", "exposure_ms": ms,
                              "n_frames": 1})
                k += 1
    if end_darks:
        steps.append({"label": "dark", "shutter": "closed", "exposure_ms": ms,
                      "n_frames": end_darks})
    return steps


def describe(steps):
    """Readable schedule with frame counts and the time it takes."""
    total_s, lines = 0.0, []
    for s in steps:
        if s.get("prompt"):
            lines.append(f"  operator: {s['prompt']}")
        if s.get("exchange"):
            lines.append(f"  medium -> {s['exchange']}")
        if s.get("exposure_ms"):
            n = s.get("n_frames") or 0
            total_s += n * s["exposure_ms"] / 1000
            lines.append(f"  {s['label']:10s} {n:3d} x {s['exposure_ms'] / 1000:g} s"
                         f"  (shutter {s.get('shutter', '-')})")
    counts = {}
    for s in steps:
        if s.get("exposure_ms"):
            counts[s["label"]] = counts.get(s["label"], 0) + (s.get("n_frames") or 0)
    lines.append(f"  total {sum(counts.values())} frames {counts}, {total_s / 3600:.2f} h of exposure")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Run setup
# --------------------------------------------------------------------------------------------
REPORT_CELLS = [
    ("markdown", "# Run report\n\nRedraws this run from the files in this folder with the copies "
                 "of `slim.py` and `biophoton.py` stored next to it."),
    ("code", "from pathlib import Path\nimport slim\n\nRUN_DIR = Path.cwd()\n"
             "summary = slim.redraw(RUN_DIR, show=True)\nsummary"),
    ("markdown", "## Change the ROIs and redraw\n\n`[x, y, width, height]` in pixels of the saved "
                 "frame."),
    ("code", "slim.bp.show_frame(RUN_DIR, index=-1);\n"
             "# slim.bp.set_rois(RUN_DIR, {\"sample\": [80, 90, 60, 60], \"background\": [10, 10, 40, 40]})\n"
             "# summary = slim.redraw(RUN_DIR, show=True)"),
]


def _write_report(run_dir):
    cells = [{"cell_type": t, "metadata": {}, "source": s.splitlines(keepends=True),
              **({"outputs": [], "execution_count": None} if t == "code" else {})}
             for t, s in REPORT_CELLS]
    nb = {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3",
                                                      "language": "python", "name": "python3"}},
          "nbformat": 4, "nbformat_minor": 5}
    (Path(run_dir) / "report.ipynb").write_text(json.dumps(nb, indent=1))


def new_run(experiment, name, data_root=DATA_ROOT):
    """bp.Run plus a copy of slim.py and a report notebook that redraws with it."""
    run = bp.Run(experiment, name, data_root)
    (run.dir / "slim.py").write_text(Path(__file__).read_text())
    _write_report(run.dir)
    return run


def prepare(client, camera, experiment, run_name, dry_run=False, data_root=DATA_ROOT,
            other_servers=(), env_period_s=30, wait=None, **config):
    """New run folder, camera settings by name (read back), environment log, cooldown + settle.

    wait: wait for the set point and settle (default: not in a dry run).
    Returns (run, env, detector, preset). Extra keyword arguments go into config.json.
    """
    preset = CAMERAS[camera]
    detector = preset["detector"] or client.settingsManager.getDetectorNames()[0]
    run = new_run(experiment, run_name, data_root)
    readback = bp.apply_settings(client, detector, preset["settings"])
    run.log(f"camera read back: {readback}")
    run.update_config(experiment=experiment, server=client.base_uri, detector=detector,
                      camera=camera, camera_preset=preset, camera_readback=readback,
                      dry_run=dry_run, **config)
    run.save_json("camera_start.json", bp.parameters(client, detector)[1])
    env = bp.EnvLogger(run.dir / "environment.csv",
                       bp.env_sources(client, detector, camera, list(other_servers)),
                       env_period_s).start()
    target = preset["settings"]["target_temperature"]
    if wait is None:
        wait = not dry_run
    if not wait:
        run.log("not waiting for the cooler (dry run or simulation)")
    else:
        bp.wait_for_temperature(client, detector, target, log=run.log, **preset["cooldown"])
        run.log(f"settling {preset['settle_s'] / 60:g} min at the set point")
        time.sleep(preset["settle_s"])
    return run, env, detector, preset


def dashboard(run_dir, acq=None, operator=None, refresh_s=15):
    """bp.dashboard plus what the operator is asked to do. Interrupting (■) stops the display only."""
    from IPython.display import clear_output
    try:
        while True:
            clear_output(wait=True)
            if operator is not None and operator.pending:
                print(f">>> WAITING FOR YOU: {operator.pending}\n"
                      ">>> interrupt this cell (■) and run the 'Operator done' cell\n")
            bp.show_live(run_dir, acq)
            if acq is not None and not acq.running:
                print("acquisition finished:", acq.state["phase"])
                break
            time.sleep(refresh_s)
    except KeyboardInterrupt:
        print("Dashboard stopped, the acquisition keeps running.")


# --------------------------------------------------------------------------------------------
# Analysis helpers
# --------------------------------------------------------------------------------------------
def bin_image(img, b):
    if b <= 1:
        return img
    h, w = (s // b * b for s in img.shape)
    return img[:h, :w].reshape(h // b, b, w // b, b).sum(axis=(1, 3))


def box_smooth(img, k):
    """Mean over a k x k box (edges use the available pixels)."""
    if k <= 1:
        return img
    pad = k // 2
    a = np.pad(img, pad, mode="edge")
    c = np.cumsum(np.cumsum(a, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    h, w = img.shape
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


def _robust_mean_image(stack):
    """Per-pixel mean without the highest value when there are >= 3 frames (cosmic rays)."""
    stack = np.asarray(stack, np.float64)
    if len(stack) >= 3:
        return (stack.sum(0) - stack.max(0)) / (len(stack) - 1)
    return stack.mean(0)


def _mid_hours(frames, idx, t0):
    return ((frames["start_unix"][idx] + frames["end_unix"][idx]) / 2 - t0) / 3600


def _mean_sem(v):
    v = np.asarray(v, np.float64)
    v = v[np.isfinite(v)]
    if not len(v):
        return float("nan"), float("nan")
    return float(v.mean()), (float(v.std(ddof=1) / math.sqrt(len(v))) if len(v) > 1 else float("nan"))


def read_protocol(run_dir):
    return bp.read_table(Path(run_dir) / "protocol.csv")


def pixel_area_mm2(cfg):
    """Area of one saved pixel in the sample plane."""
    p = cfg.get("camera_preset") or {}
    m = (cfg.get("optics") or {}).get("magnification") or 1.0
    if not p.get("pixel_um"):
        return None
    return (p["pixel_um"] * p.get("readout_binning", 1) / m / 1000) ** 2


# --------------------------------------------------------------------------------------------
# S2: leaf in water, then H2O2
# --------------------------------------------------------------------------------------------
def analyse_leaf(run_dir, cfg):
    """Dark-subtracted rates per light frame, baseline and response windows, ratio and images.

    The dark level under each light frame is interpolated in time between the dark frames of
    the run (same ROI, same exposure), so dark-signal non-uniformity and slow drifts cancel.
    """
    run_dir = Path(run_dir)
    frames = read_protocol_frames(run_dir)
    if not frames:
        return {}, {"frames": 0}
    a = cfg.get("analysis", {})
    rois = cfg.get("rois") or {}
    roi_items = {k: v for k, v in rois.items() if v is not None} or {"sample": None}
    labels = frames["label"].astype(str)
    t0 = float(np.nanmin(frames["start_unix"]))
    preset = cfg.get("camera_preset") or {}
    ib = int(a.get("image_binning", preset.get("image_binning", 1)))
    exp_s = frames["exposure_ms"] / 1000

    means = {k: np.full(len(labels), np.nan) for k in roi_items}
    images = {}
    for i in range(len(labels)):
        img = bp.load_image(run_dir / frames["file"][i])
        for k, roi in roi_items.items():
            means[k][i] = bp.clipped_mean(bp.roi_view(img, roi))
        images[i] = bin_image(img, ib).astype(np.float32)

    dark = np.flatnonzero(labels == "dark")
    light = np.flatnonzero(np.isin(labels, ["baseline", "response"]))
    t = _mid_hours(frames, np.arange(len(labels)), t0)
    warnings = []
    if not len(dark):
        warnings.append("no dark frames: rates are not dark-subtracted (offset only if set)")

    def dark_at(k, idx):
        if len(dark) == 0:
            return np.full(len(idx), float(a.get("offset_adu") or 0.0))
        if len(dark) == 1:
            return np.full(len(idx), means[k][dark[0]])
        return np.interp(t[idx], t[dark], means[k][dark])

    proto = read_protocol(run_dir)
    t_exchange = None
    if proto:
        acts = proto["action"].astype(str)
        done = np.flatnonzero(acts == "exchange_done")
        if len(done):                          # the last exchange is the baseline -> response one
            t_exchange = float((proto["unix_time"][done[-1]] - t0) / 3600)
    resp = np.flatnonzero(labels == "response")
    base = np.flatnonzero(labels == "baseline")
    if t_exchange is None and len(resp):
        t_exchange = float((frames["start_unix"][resp[0]] - t0) / 3600)

    e_per_adu = a.get("e_per_adu")
    area = pixel_area_mm2(cfg)
    rows, rate = [], {}
    for k in roi_items:
        rate[k] = (means[k][light] - dark_at(k, light)) / exp_s[light]
    for j, i in enumerate(light):
        row = {"index": int(frames["index"][i]), "label": labels[i], "t_h": float(t[i]),
               "exposure_s": float(exp_s[i])}
        for k in roi_items:
            row[f"{k}_adu"] = float(means[k][i])
            row[f"{k}_dark_adu"] = float(dark_at(k, np.array([i]))[0])
            row[f"{k}_rate_adu_px_s"] = float(rate[k][j])
            if e_per_adu:
                row[f"{k}_rate_e_px_s"] = float(rate[k][j] * e_per_adu)
        rows.append(row)
    bp._write_rows(run_dir / "analysis.csv", rows)

    wb, wr = float(a.get("baseline_window_h", 1.5)), float(a.get("response_window_h", 3.0))
    t_light = t[light]
    in_base = np.isin(light, base)
    in_resp = np.isin(light, resp)
    if in_base.any():
        b_end = t_light[in_base].max()
        win_b = in_base & (t_light >= b_end - wb)
        if win_b.sum() < 2:
            win_b = in_base
    else:
        win_b = in_base
    if in_resp.any():
        start = t_exchange if t_exchange is not None else t_light[in_resp].min()
        win_r = in_resp & (t_light <= start + wr)
        if win_r.sum() < 2:
            win_r = in_resp
    else:
        win_r = in_resp

    summary = {"frames_baseline": int(len(base)), "frames_response": int(len(resp)),
               "frames_dark": int(len(dark)), "exchange_h": t_exchange,
               "baseline_window_h": wb, "response_window_h": wr,
               "unit": "ADU / px / s" + (" (and e- / px / s)" if e_per_adu else ""),
               "rois": rois, "warnings": warnings}
    k0 = "sample" if "sample" in roi_items else next(iter(roi_items))
    for k in roi_items:
        b, b_err = _mean_sem(rate[k][win_b])
        r, r_err = _mean_sem(rate[k][win_r])
        out = {f"{k}_baseline": b, f"{k}_baseline_sem": b_err,
               f"{k}_response": r, f"{k}_response_sem": r_err,
               f"{k}_difference": r - b, f"{k}_difference_sem": math.hypot(b_err, r_err)
               if math.isfinite(b_err) and math.isfinite(r_err) else float("nan")}
        if b > 0:
            ratio = r / b
            out[f"{k}_ratio"] = ratio
            out[f"{k}_ratio_err"] = (ratio * math.hypot(b_err / b, r_err / r)
                                     if r > 0 and math.isfinite(b_err) and math.isfinite(r_err)
                                     else float("nan"))
        if in_resp.any():
            j = np.nanargmax(np.where(in_resp, rate[k], -np.inf))
            out[f"{k}_peak"] = float(rate[k][j])
            out[f"{k}_peak_after_exchange_h"] = (float(t_light[j] - t_exchange)
                                                 if t_exchange is not None else None)
        summary.update(out)
    if roi_items.get(k0) is None and len(roi_items) == 1:
        warnings.append("no sample ROI set: whole frame used")
    if e_per_adu:
        summary["e_per_adu"] = e_per_adu
        for key in [s for s in list(summary) if s.endswith(("_baseline", "_response", "_difference",
                                                            "_baseline_sem", "_response_sem",
                                                            "_difference_sem", "_peak"))]:
            v = summary[key]
            if isinstance(v, float) and math.isfinite(v):
                summary[key + "_e"] = v * e_per_adu
                if area:
                    summary[key + "_e_mm2"] = v * e_per_adu / area
        if area:
            summary["pixel_area_mm2"] = area
    if len(dark):
        d = means[k0][dark]
        summary["dark_level_adu"] = float(np.median(d))
        summary["dark_exposure_s"] = float(np.median(exp_s[dark]))
        if len(dark) >= 2:
            summary["dark_drift_adu"] = float(d[-1] - d[0])

    # images: robust mean of the darks, of the baseline window and of the response window
    def stack(idx):
        return np.stack([images[i] for i in idx]) if len(idx) else None

    master = _robust_mean_image(stack(dark)) if len(dark) else None
    maps = {}
    for name, idx in (("baseline", light[win_b]), ("response", light[win_r])):
        if len(idx):
            m = _robust_mean_image(stack(idx))
            if master is not None:
                m = m - master
            maps[name] = m / float(np.median(exp_s[idx]))
    for name, m in maps.items():
        tifffile.imwrite(run_dir / f"{name}_rate_adu_s.tif", m.astype(np.float32))
    if master is not None:
        tifffile.imwrite(run_dir / "master_dark.tif", master.astype(np.float32))
    figs = {"timecourse": _fig_leaf_timecourse(t, labels, light, dark, rate, means, roi_items,
                                               t_exchange, win_b, win_r, frames, k0)}
    if maps:
        figs["images"] = _fig_leaf_images(maps, rois, ib)
    return figs, summary


def read_protocol_frames(run_dir):
    return bp.read_table(Path(run_dir) / "frames.csv")


def _fig_leaf_timecourse(t, labels, light, dark, rate, means, roi_items, t_exchange, win_b,
                         win_r, frames, k0):
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True,
                             gridspec_kw={"height_ratios": [2, 1]})
    ax = axes[0]
    tl = t[light]
    for k in roi_items:
        ax.plot(tl, rate[k], "o-", ms=4, lw=1, label=k)
    for win, color, name in ((win_b, "C2", "baseline window"), (win_r, "C3", "response window")):
        if win.any():
            ax.axvspan(tl[win].min(), tl[win].max(), color=color, alpha=0.12, label=name)
    if t_exchange is not None:
        for a_ in axes:
            a_.axvline(t_exchange, color="k", ls="--", lw=1)
        ax.text(t_exchange, ax.get_ylim()[1], " exchange", va="top", fontsize=9)
    ax.axhline(0, color="0.6", lw=0.8)
    ax.set_ylabel("light - dark\n[ADU / px / s]")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    ax = axes[1]
    if len(dark):
        ax.plot(t[dark], means[k0][dark], "ks", ms=5, label=f"dark frames, {k0} ROI")
        if len(dark) > 1:
            ax.plot(tl, np.interp(tl, t[dark], means[k0][dark]), "k-", lw=0.8,
                    label="interpolated under the light frames")
    ax.set_ylabel("dark level [ADU]")
    temp = frames.get("camera_temperature_c")
    if temp is not None and np.isfinite(temp).any():
        tw = ax.twinx()
        tw.plot(t, temp, "C1.", ms=3)
        tw.set_ylabel("sensor [°C]", color="C1")
    ax.set_xlabel("time since run start [h]")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def _fig_leaf_images(maps, rois, ib):
    names = list(maps) + (["response - baseline"] if len(maps) == 2 else [])
    fig, axes = plt.subplots(1, len(names), figsize=(5 * len(names), 4.6), squeeze=False)
    lo, hi = np.percentile(np.concatenate([m.ravel() for m in maps.values()]), [1, 99.7])
    scaled = {k: [int(v / ib) for v in r] for k, r in (rois or {}).items() if r is not None}
    for ax, name in zip(axes[0], names):
        if name in maps:
            im = ax.imshow(maps[name], cmap="magma", vmin=lo, vmax=hi)
        else:
            d = maps["response"] - maps["baseline"]
            lim = np.percentile(np.abs(d), 99.5)
            im = ax.imshow(d, cmap="RdBu_r", vmin=-lim, vmax=lim)
        bp._draw_rois(ax, scaled)
        ax.set_title(f"{name} [ADU / px / s]" + (f", {ib}x{ib} binned" if ib > 1 else ""),
                     fontsize=10)
        ax.axis("off")
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------------------------
# S1: calibration with the beta light
# --------------------------------------------------------------------------------------------
def _group_stacks(run_dir, frames, label, step):
    """{exposure_ms: (n, y, x) stack} of one label, every `step`-th pixel."""
    labels = frames["label"].astype(str)
    out = {}
    for i in np.flatnonzero(labels == label):
        img = bp.load_image(Path(run_dir) / frames["file"][i])[::step, ::step]
        out.setdefault(float(frames["exposure_ms"][i]), []).append(img)
    return {k: np.stack(v) for k, v in sorted(out.items())}


def analyse_calibration(run_dir, cfg):
    """Offset and read noise (bias), photon transfer and linearity (beta light in the infinity
    space), dark current over the ladder, and the sample-plane reference total."""
    run_dir = Path(run_dir)
    frames = bp.read_table(run_dir / "frames.csv")
    if not frames:
        return {}, {"frames": 0}
    a = cfg.get("analysis", {})
    preset = cfg.get("camera_preset") or {}
    step = int(a.get("pixel_step", 1))
    f2 = float(preset.get("excess_noise_sq", 1.0))
    full = float(preset.get("full_scale_adu", 65535))
    summary, figs, warnings = {"excess_noise_sq": f2, "pixel_step": step}, {}, []

    bias = _group_stacks(run_dir, frames, "bias", step)
    offset = read_adu = None
    if bias:
        b = next(iter(bias.values()))
        offset = float(np.median(b.mean(0)))
        read_adu = float(np.sqrt(np.median(b.var(0, ddof=1)))) if len(b) > 1 else float("nan")
        summary.update(offset_adu=offset, read_noise_adu=read_adu, bias_frames=int(len(b)))

    light, dark = (_group_stacks(run_dir, frames, lab, step) for lab in ("ptc_light", "ptc_dark"))
    keys, xs, ys, dvars = [], [], [], []
    signal, saturated = {}, {}
    ladder_t, ladder_d = [], []
    for ms in sorted(light):
        L = light[ms]
        D = dark.get(ms)
        if D is None or len(L) < 4 or len(D) < 2:
            warnings.append(f"{ms:g} ms: need >= 4 light and >= 2 dark frames, skipped")
            continue
        sat = (L >= full - 1).any(0)
        d_mean, d_var = D.mean(0), D.var(0, ddof=1)
        dvars.append(float(np.median(d_var)))
        ladder_t.append(ms / 1000)
        ladder_d.append(float(np.median(d_mean)))
        for ka, kb in ((0, 1), (1, 0)):          # bin by one half, measure on the other
            key = L[ka::2].mean(0) - d_mean
            x = L[kb::2].mean(0) - d_mean
            y = L[kb::2].var(0, ddof=1) - d_var
            ok = ~sat & (key > 0) & (key < 0.8 * (full - (offset or 0)))
            keys.append(key[ok])
            xs.append(x[ok])
            ys.append(y[ok])
        signal[ms], saturated[ms] = L.mean(0) - d_mean, sat
    # linearity: the brightest quarter, chosen on the longest exposure without clipping there
    lin_t, lin_s = [], []
    clean = [ms for ms in signal if not saturated[ms].any()] or list(signal)
    if clean:
        ref = signal[max(clean)]
        bright = (ref > np.percentile(ref, 75)) & ~np.any([saturated[ms] for ms in signal], axis=0)
        for ms in sorted(signal):
            lin_t.append(ms / 1000)
            lin_s.append(float(signal[ms][bright].mean()))
    if keys:
        key, x, y = (np.concatenate(v) for v in (keys, xs, ys))
        edges = np.unique(np.percentile(key, np.linspace(0.5, 99.5, 41)))
        idx = np.digitize(key, edges) - 1
        bm, bv = [], []
        for i in range(len(edges) - 1):
            sel = idx == i
            if sel.sum() >= 30:
                v = y[sel]
                keep = v <= np.percentile(v, 99.5)
                bm.append(float(x[sel][keep].mean()))
                bv.append(float(v[keep].mean()))
        bm, bv = np.array(bm), np.array(bv)
        if len(bm) >= 3:
            k, c = np.polyfit(bm, bv, 1)
            noise0 = float(np.median(dvars))           # read + dark variance, already subtracted
            resid = (bv - (k * bm + c)) / (k * bm + c + noise0)
            adu_per_e = k / f2
            summary.update(adu_per_e=float(adu_per_e), e_per_adu=float(1 / adu_per_e),
                           ptc_slope_adu=float(k), ptc_intercept_adu2=float(c),
                           ptc_rel_residual_rms=float(np.sqrt(np.mean(resid ** 2))),
                           ptc_signal_range_adu=[float(bm.min()), float(bm.max())])
            if read_adu is not None and math.isfinite(read_adu):
                summary["read_noise_e"] = float(read_adu / adu_per_e)
            figs["ptc"] = _fig_ptc(bm, bv, k, c, f2, noise0)
        else:
            warnings.append("too few populated signal bins for a photon-transfer fit")
    if len(lin_t) >= 2:
        lt, ls = np.array(lin_t), np.array(lin_s)
        slope = float(np.sum(lt * ls) / np.sum(lt * lt))     # through the origin
        res = (ls - slope * lt) / (slope * lt)
        summary.update(linearity_slope_adu_s=slope,
                       linearity_max_residual_pct=float(100 * np.max(np.abs(res))),
                       linearity_exposures_s=lt.tolist())
        figs["linearity"] = _fig_linearity(lt, ls, slope)
    if len(ladder_t) >= 2:
        if offset is not None:
            ladder_t, ladder_d = [0.0] + ladder_t, [offset] + ladder_d
        dslope = float(np.polyfit(ladder_t, ladder_d, 1)[0])
        summary["dark_current_adu_px_s"] = dslope
        if "e_per_adu" in summary:
            summary["dark_current_e_px_s"] = dslope * summary["e_per_adu"]
        summary["dark_note"] = "from the short ladder; S2 dark frames give it at the run exposure"

    ref_l, ref_d = (_group_stacks(run_dir, frames, lab, step) for lab in ("ref_light", "ref_dark"))
    if ref_l and ref_d:
        ms = next(iter(ref_l))
        m = (_robust_mean_image(ref_l[ms]) - _robust_mean_image(ref_d.get(ms, next(iter(ref_d.values())))))
        rate_map = m / (ms / 1000) * step * step              # per saved pixel, all pixels
        sm = box_smooth(rate_map, 5)
        roi = sm > float(a.get("reference_threshold", 0.2)) * np.percentile(sm, 99.9)
        total = float(rate_map[roi].sum())
        summary.update(ref_exposure_s=ms / 1000, ref_total_adu_s=total,
                       ref_pixels=int(roi.sum() * step * step),
                       ref_peak_adu_px_s=float(np.percentile(sm, 99.9) / step / step))
        if "e_per_adu" in summary:
            summary["ref_total_e_s"] = total * summary["e_per_adu"]
        figs["reference"] = _fig_reference(rate_map / step / step, roi)
    summary["warnings"] = warnings
    return figs, summary


def _fig_ptc(bm, bv, k, c, f2, noise0=0.0):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.3))
    axes[0].plot(bm, bv, "o", ms=4, label="pixel bins (beta-light ramp x exposures)")
    axes[0].plot(bm, k * bm + c, "-", label=f"slope {k:.4g} ADU = F² x {k / f2:.4g} ADU/e-")
    axes[0].set_xlabel("signal above dark [ADU]")
    axes[0].set_ylabel("temporal variance above dark [ADU²]")
    axes[0].legend(fontsize=8)
    axes[1].plot(bm, (bv - (k * bm + c)) / (k * bm + c + noise0), "o", ms=4)
    axes[1].axhline(0, color="0.5", lw=0.8)
    axes[1].set_xlabel("signal above dark [ADU]")
    axes[1].set_ylabel("(variance - fit) / total variance")
    for ax in axes:
        ax.grid(alpha=0.3)
    fig.suptitle("Photon transfer")
    fig.tight_layout()
    return fig


def _fig_linearity(t, s, slope):
    fig, axes = plt.subplots(1, 2, figsize=(12, 4))
    axes[0].plot(t, s, "o")
    tt = np.linspace(0, t.max(), 50)
    axes[0].plot(tt, slope * tt, "-", label="line through the origin")
    axes[0].set_xlabel("exposure [s]")
    axes[0].set_ylabel("signal, brightest quarter [ADU]")
    axes[0].legend()
    axes[1].plot(t, 100 * (s - slope * t) / (slope * t), "o")
    axes[1].axhline(0, color="0.5", lw=0.8)
    axes[1].set_xscale("log")
    axes[1].set_xlabel("exposure [s]")
    axes[1].set_ylabel("residual [%]")
    for ax in axes:
        ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


def _fig_reference(rate_map, roi):
    fig, ax = plt.subplots(figsize=(6, 5))
    im = ax.imshow(rate_map, cmap="magma", **bp._stretch(rate_map))
    ax.contour(roi, levels=[0.5], colors="c", linewidths=0.8)
    ax.set_title("beta light at the sample plane [ADU / px / s]")
    ax.axis("off")
    fig.colorbar(im, ax=ax, fraction=0.046)
    fig.tight_layout()
    return fig


# --------------------------------------------------------------------------------------------
# Redraw one run
# --------------------------------------------------------------------------------------------
def redraw(run_dir, show=False):
    """Recompute figures, summary.json and README.md of an S1 or S2 run from its files."""
    run_dir = Path(run_dir)
    cfg = bp.load_config(run_dir)
    frames, env = bp.read_table(run_dir / "frames.csv"), bp.read_table(run_dir / "environment.csv")
    figs, summary = {}, {}
    if cfg.get("kind") == "slim_leaf":
        figs, summary = analyse_leaf(run_dir, cfg)
    elif cfg.get("kind") == "slim_calibration":
        figs, summary = analyse_calibration(run_dir, cfg)
    if env:
        t0, t_label = bp.time_zero(cfg, frames, env)
        figs["environment"] = bp.fig_environment(env, t0, t_label)
        for col in ("camera_temperature_c", "enclosure_temperature_c", "humidity_pct", "lux"):
            v = env[col][np.isfinite(env[col])]
            if v.size:
                summary[f"{col}_min_max"] = [round(float(v.min()), 3), round(float(v.max()), 3)]
    for name, fig in figs.items():
        fig.savefig(run_dir / "figures" / f"{name}.png", dpi=110)
        if not show:
            plt.close(fig)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    bp.write_readme(run_dir, cfg, summary)
    if show:
        plt.show()
    return summary


# --------------------------------------------------------------------------------------------
# S3: across runs
# --------------------------------------------------------------------------------------------
def _runs(folder, include_dry):
    out = []
    for d in sorted(Path(folder).glob("*")):
        if not d.is_dir() or not (d / "config.json").exists():
            continue
        cfg = bp.load_config(d)
        if cfg.get("dry_run") and not include_dry:
            continue
        s = d / "summary.json"
        out.append((d, cfg, json.loads(s.read_text()) if s.exists() else None))
    return out


def collect(data_root=DATA_ROOT, include_dry=False, redraw_missing=True):
    """(calibration rows, leaf rows) from all S1 and S2 run folders under `data_root`.

    Leaf rates are converted to e- / px / s and e- / mm² / s with the latest S1 run of the same
    camera (gain does not depend on the optics), unless the S2 run carries its own e_per_adu.
    """
    data_root = Path(data_root)
    cal = []
    for d, cfg, s in _runs(data_root / "S1_calibration", include_dry):
        if s is None and redraw_missing:
            s = redraw(d)
        s = s or {}
        cal.append({"run": d.name, "camera": cfg.get("camera"),
                    "optics": (cfg.get("optics") or {}).get("name"),
                    "time": d.name[:19], **{k: s.get(k) for k in (
                        "e_per_adu", "adu_per_e", "read_noise_e", "read_noise_adu", "offset_adu",
                        "dark_current_e_px_s", "linearity_max_residual_pct", "ref_total_adu_s",
                        "ref_total_e_s", "ptc_rel_residual_rms")}})
    leaf = []
    for d, cfg, s in _runs(data_root / "S2_leaf_water_h2o2", include_dry):
        if s is None and redraw_missing:
            s = redraw(d)
        s = s or {}
        camera = cfg.get("camera")
        cands = [c for c in cal if c["camera"] == camera and c.get("e_per_adu")]
        match = cands[-1] if cands else {}
        e_per_adu = (cfg.get("analysis") or {}).get("e_per_adu")
        source = "run" if e_per_adu else None
        if not e_per_adu and match:
            e_per_adu, source = match["e_per_adu"], match["run"]
        area = pixel_area_mm2(cfg)
        row = {"run": d.name, "camera": camera, "optics": (cfg.get("optics") or {}).get("name"),
               "condition": cfg.get("condition"), "sample_id": (cfg.get("sample") or {}).get("id"),
               "baseline_adu": s.get("sample_baseline"), "response_adu": s.get("sample_response"),
               "ratio": s.get("sample_ratio"), "ratio_err": s.get("sample_ratio_err"),
               "difference_adu": s.get("sample_difference"),
               "difference_sem_adu": s.get("sample_difference_sem"),
               "dark_drift_adu": s.get("dark_drift_adu"), "e_per_adu": e_per_adu,
               "calibration": source}
        # dark signal at the run exposure and temperature: in-run darks above the S1 bias level
        lvl, t_d, off = s.get("dark_level_adu"), s.get("dark_exposure_s"), match.get("offset_adu")
        if e_per_adu and lvl is not None and t_d and off is not None:
            row["dark_signal_e_px_s"] = (lvl - off) / t_d * e_per_adu
        for key in ("baseline", "response", "difference"):
            v = row[f"{key}_adu"]
            if e_per_adu and v is not None and math.isfinite(v):
                row[f"{key}_e_px_s"] = v * e_per_adu
                if area:
                    row[f"{key}_e_mm2_s"] = v * e_per_adu / area
        leaf.append(row)
    return cal, leaf


def group_stats(leaf_rows):
    """Per (camera, optics, condition): n, ratio mean and SD, geometric mean, difference."""
    groups = {}
    for r in leaf_rows:
        groups.setdefault((r["camera"], r["optics"], r["condition"]), []).append(r)
    out = []
    for (camera, optics, cond), rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        ratios = np.array([r["ratio"] for r in rows if r.get("ratio") and r["ratio"] > 0], float)
        use_e = all(r.get("difference_e_mm2_s") is not None for r in rows)
        diffs = np.array([r["difference_e_mm2_s"] if use_e else r["difference_adu"] for r in rows
                          if r.get("difference_adu") is not None], float)
        row = {"camera": camera, "optics": optics, "condition": cond, "n": len(rows)}
        if len(ratios):
            row.update(ratio_mean=float(ratios.mean()),
                       ratio_sd=float(ratios.std(ddof=1)) if len(ratios) > 1 else float("nan"),
                       ratio_geomean=float(np.exp(np.log(ratios).mean())))
        if len(diffs):
            unit = "e-/mm²/s" if use_e else "ADU/px/s"
            row.update(difference_mean=float(diffs.mean()),
                       difference_sd=float(diffs.std(ddof=1)) if len(diffs) > 1 else float("nan"),
                       difference_unit=unit)
        out.append(row)
    return out


def fig_paired(leaf_rows):
    """Baseline and response of every run, one panel per condition, lines join a run's pair."""
    conds = list(dict.fromkeys(r["condition"] for r in leaf_rows))
    if not conds:
        return None
    use_e = all(r.get("baseline_e_mm2_s") is not None for r in leaf_rows)
    kb, kr = (("baseline_e_mm2_s", "response_e_mm2_s") if use_e else ("baseline_adu", "response_adu"))
    fig, axes = plt.subplots(1, len(conds), figsize=(3.6 * len(conds), 4.2), sharey=True,
                             squeeze=False)
    for ax, cond in zip(axes[0], conds):
        rows = [r for r in leaf_rows if r["condition"] == cond]
        for r in rows:
            if r.get(kb) is None or r.get(kr) is None:
                continue
            ax.plot([0, 1], [r[kb], r[kr]], "o-", color="C0" if r["camera"] == "andor" else "C1",
                    alpha=0.8)
        ref = CONDITIONS.get(cond, {})
        ax.set_xticks([0, 1], [ref.get("baseline", "baseline"), ref.get("response", "response")])
        ax.set_xlim(-0.4, 1.4)
        ax.set_title(f"{cond} (n = {len(rows)})", fontsize=10)
        ax.grid(alpha=0.3)
    axes[0, 0].set_ylabel("sample, dark-subtracted\n" + ("[e- / mm² / s]" if use_e else "[ADU / px / s]"))
    fig.tight_layout()
    return fig


def fig_reference(cal_rows):
    """Beta light at the sample plane per camera and optics: what the relay collects."""
    rows = [c for c in cal_rows if c.get("ref_total_adu_s")]
    if not rows:
        return None
    use_e = all(c.get("ref_total_e_s") for c in rows)
    fig, ax = plt.subplots(figsize=(7, 3.8))
    names = [f"{c['camera']}\n{c['optics']}\n{c['run'][:10]}" for c in rows]
    vals = [c["ref_total_e_s"] if use_e else c["ref_total_adu_s"] for c in rows]
    ax.bar(range(len(rows)), vals, color=["C0" if c["camera"] == "andor" else "C1" for c in rows])
    ax.set_xticks(range(len(rows)), names, fontsize=8)
    ax.set_ylabel("beta light, total " + ("[e- / s]" if use_e else "[ADU / s]"))
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    return fig


def write_summary(out_dir, cal_rows, leaf_rows, stats):
    out_dir = Path(out_dir)
    (out_dir / "figures").mkdir(parents=True, exist_ok=True)
    bp._write_rows(out_dir / "calibration.csv", cal_rows)
    bp._write_rows(out_dir / "leaves.csv", leaf_rows)
    bp._write_rows(out_dir / "groups.csv", stats)
    lines = ["# Slim protocol summary", "",
             f"{len(cal_rows)} S1 calibration runs, {len(leaf_rows)} S2 runs. "
             "Rates are dark-subtracted with the dark frames of the same run.", "",
             "| camera | optics | condition | n | ratio mean ± SD | geometric mean | difference mean ± SD |",
             "|---|---|---|---|---|---|---|"]
    def pm(m, sd, unit=""):
        if m is None or not math.isfinite(m):
            return "-"
        return f"{m:.3g}" + (f" ± {sd:.2g}" if sd is not None and math.isfinite(sd) else "") + \
            (f" {unit}" if unit else "")

    for s in stats:
        cond = s["condition"]
        ratio = pm(s.get("ratio_mean"), s.get("ratio_sd")) if cond != "no_leaf" else "(no tissue)"
        geo = pm(s.get("ratio_geomean"), None) if cond != "no_leaf" else "-"
        lines.append(f"| {s['camera']} | {s['optics']} | {cond} | {s['n']} | {ratio} | {geo} | "
                     f"{pm(s.get('difference_mean'), s.get('difference_sd'), s.get('difference_unit', ''))} |")
    lines += ["", "Files: `calibration.csv`, `leaves.csv`, `groups.csv`, `figures/`.", ""]
    for name in ("paired", "reference"):
        if (out_dir / "figures" / f"{name}.png").exists():
            lines.append(f"![{name}](figures/{name}.png)")
    (out_dir / "README.md").write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------------------------
# Simulator: ImSwitch, camera, shutter, pump and beta light in one object
# --------------------------------------------------------------------------------------------
class _SimSettings:
    def __init__(self, sim):
        self.sim = sim

    def getDetectorNames(self):
        return [self.sim.detector]

    def _tree(self):
        return {"groups": [{"parameters": [{"name": k, "value": v}
                                           for k, v in self.sim.params.items()]}]}

    def getDetectorParameterTree(self, detector):
        return self._tree()

    def setDetectorParameterValue(self, name, value, detector_name=None):
        self.sim.params[name] = value
        if name == "target_temperature":
            self.sim.temperature = float(value)
        return self._tree()

    def setDetectorBinning(self, detector, binning):
        self.sim.binning = int(binning)

    def getCameraStatus(self, detector):
        return {"binning": self.sim.binning,
                "hardwareParameters": {"temperature_c": self.sim.temperature,
                                       "tec_target_c": self.sim.params.get("target_temperature")}}

    def setDetectorExposureTime(self, detector, ms):
        self.sim.exposure_ms = float(ms)

    def getDetectorParameter(self, name, detector):
        return self.sim.exposure_ms if name == "exposure" else self.sim.params.get(name)


class _SimRecording:
    def __init__(self, sim):
        self.sim = sim

    def snapAndWait(self, name, return_preview=False, poll=5, on_progress=None, stop_event=None):
        self.sim.images[f"{name}.tif"] = self.sim.frame()
        return {"relativeFilePath": f"{name}.tif"}

    def downloadFile(self, rel, local):
        tifffile.imwrite(local, self.sim.images.pop(rel))

    def downloadImage(self, rel):
        return self.sim.images[rel]


class Simulator:
    """Stands in for ImSwitch: one camera, the shutter, the pump and the beta light.

    Frames are computed, not exposed, so a 12 h protocol runs in seconds. A virtual clock moves
    on by each exposure (afterglow of the freshly mounted leaf decays on it). Use it as the
    `client`, and `shutter()`, `fluidics()`, `operator` for the hardware.
    """

    CAMERA_MODELS = {   # conversion gain, offset, read noise, dark current, DSNU, EM excess noise
        "toupcam": dict(adu_per_e=103.0, offset=1000.0, read_adu=120.0, dark_e_s=0.002,
                        dsnu=0.6, em=False, full=16383, cic=0.0),
        "andor": dict(adu_per_e=77.0, offset=500.0, read_adu=60.0, dark_e_s=0.0005,
                      dsnu=0.2, em=True, full=65535, cic=0.005),
    }

    def __init__(self, camera="toupcam", shape=(96, 96), seed=None, leaf=True, leaf_e_s=0.05,
                 media={"water": 1.0, "h2o2": 2.0}, afterglow=1.0, afterglow_tau_h=0.25,
                 beta_e_s=4000.0, throughput=1.0, cosmics_per_px_s=2e-6, offset_drift_adu_h=0.0):
        self.camera, self.model = camera, self.CAMERA_MODELS[camera]
        self.detector = "SimCam" if camera == "toupcam" else "Andor"
        self.base_uri = f"sim://{camera}"
        self.rng = np.random.default_rng(seed)
        self.shape, self.leaf, self.leaf_e_s = shape, leaf, leaf_e_s
        self.media, self.afterglow, self.afterglow_tau_h = dict(media), afterglow, afterglow_tau_h
        self.beta_e_s, self.throughput, self.cosmics = beta_e_s, throughput, cosmics_per_px_s
        self.offset_drift = offset_drift_adu_h
        self.params = {k: None for p in CAMERAS.values() for k in p["settings"] if k != "binning"}
        self.params["exposure"] = 100
        self.binning, self.temperature, self.exposure_ms = 1, 20.0, 100.0
        self.shutter_state, self.medium, self.source = "open", None, None
        self.clock_s, self.images = 0.0, {}
        self.settingsManager, self.recordingManager = _SimSettings(self), _SimRecording(self)
        h, w = shape
        y, x = np.mgrid[0:h, 0:w]
        self.leaf_mask = (((x - 0.45 * w) / (0.25 * w)) ** 2 + ((y - 0.5 * h) / (0.32 * h)) ** 2) < 1
        self.leaf_edge = self.leaf_mask & ~(((x - 0.45 * w) / (0.2 * w)) ** 2
                                            + ((y - 0.5 * h) / (0.27 * h)) ** 2 < 1)
        self.beta = np.exp(-((x - 0.5 * w) / (0.3 * w)) ** 2 - ((y - 0.5 * h) / (0.08 * h)) ** 2)
        self.ref = (((x - 0.5 * w) / (0.08 * w)) ** 2 + ((y - 0.5 * h) / (0.25 * h)) ** 2) < 1
        self.dsnu = self.rng.gamma(1 / self.model["dsnu"] ** 2, self.model["dsnu"] ** 2, shape)
        self.operator = Operator(log=lambda m: None, dry_run=True,
                                 on_ask=self.operator_prompt_handler)
        self.rois = {"sample": [int(0.3 * w), int(0.3 * h), int(0.3 * w), int(0.4 * h)],
                     "background": [int(0.78 * w), int(0.05 * h), int(0.18 * w), int(0.2 * h)]}

    # ImSwitch's generic REST access (enclosure sensors)
    def get_json(self, url, payload=None):
        return {"temperature_c": 21.0, "humidity_pct": 40.0, "lux": 0.0}

    post_json = get_json

    # hardware
    def set_shutter(self, state):
        self.shutter_state = state

    def set_medium(self, medium):
        self.medium = medium

    def shutter(self, log=print):
        return Shutter("callable", lambda: self.set_shutter("open"),
                       lambda: self.set_shutter("closed"), self.operator, settle_s=0, log=log)

    def fluidics(self, log=print):
        return Fluidics("callable", {m: (lambda m=m: self.set_medium(m)) for m in self.media},
                        lambda: None, lambda: None, flush_s=0, settle_s=0,
                        operator=self.operator, log=log)

    def operator_prompt_handler(self, message):
        """The operator in a simulation: the beta-light prompts move the light."""
        m = message.lower()
        if "infinity space" in m:
            self.source = "infinity"
        elif "sample plane" in m:
            self.source = "sample"
        elif "remove the beta light" in m:
            self.source = None

    def frame(self):
        t = self.exposure_ms / 1000
        m = self.model
        self.clock_s += t
        e = self.dsnu * m["dark_e_s"] * t + m["cic"]
        if self.shutter_state == "open":
            if self.source == "infinity":
                e = e + self.beta * self.beta_e_s * t
            elif self.source == "sample":
                e = e + self.ref * self.beta_e_s * 0.05 * self.throughput * t
            elif self.leaf:
                glow = 1 + self.afterglow * math.exp(-self.clock_s / 3600 / self.afterglow_tau_h)
                factor = self.media.get(self.medium, 1.0)
                rate = self.leaf_e_s * self.throughput * (self.leaf_mask * glow
                                                          + self.leaf_edge * (factor - 1) * 3
                                                          + self.leaf_mask * (factor - 1) * 0.5)
                e = e + rate * t
        n = self.rng.poisson(e)
        if m["em"]:
            sig = np.where(n > 0, self.rng.gamma(np.maximum(n, 1), m["adu_per_e"]), 0.0)
        else:
            sig = n * m["adu_per_e"]
        offset = m["offset"] + self.offset_drift * self.clock_s / 3600
        img = offset + sig + self.rng.normal(0, m["read_adu"], self.shape)
        hits = self.rng.random(self.shape) < self.cosmics * t
        img[hits] += self.rng.uniform(20, 200, hits.sum()) * m["adu_per_e"]
        return np.clip(np.round(img), 0, m["full"]).astype(np.uint16)
