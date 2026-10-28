# E3 - Andor EMCCD, long exposures over time

Back-to-back 30 min exposures of an injured leaf in 2 % H2O2 on the Andor iXon DU888 EMCCD (4x4 binning, EM gain 300, 1 MHz, deep cooling) until the leaf dries out (~12 h). Same protocol as E4 on the ToupTek. **Run this first**: it answers whether the sample glows above background and how strongly, which sets the frame time for E1 and tells us whether the ToupTek has a chance.

| | |
|---|---|
| Run | `2026-10-27_19-30-18_cress_h2o2` |
| Experiment | E3 |
| Server / detector | http://192.168.178.50:80/imswitch/api / Andor |
| Frames | 3 |
| First / last frame | 2026-10-27T19:30:24 / 2026-10-27T19:30:44 |
| Sample species | Lepidium sativum (garden cress) |
| Sample treatment | injured + 2 % H2O2 |
| **Dry run** | short test exposures, not data |

## Summary

- **frames:** 3
- **mode:** integrated
- **unit:** ADU / px / s
- **total_exposure_h:** 0.004
- **time_axis:** since run start
- **rois:** {'sample': None, 'background': None}
- **warning:** no sample ROI set: whole frame used
- **warning:** no background ROI or offset: rates include the camera offset
- **rate:** 102.7
- **rate_error:** 0.2024
- **camera_temperature_c_min_max:** [-82.98, -79.81]

## Figures

![environment](figures/environment.png)
![images](figures/images.png)
![timecourse](figures/timecourse.png)

## Settings

```json
{
  "kind": "timecourse",
  "experiment": "E3",
  "sample": {
    "species": "Lepidium sativum (garden cress)",
    "treatment": "injured + 2 % H2O2",
    "prepared_at": null,
    "notes": ""
  },
  "server": "http://192.168.178.50:80/imswitch/api",
  "detector": "Andor",
  "dry_run": true,
  "camera_settings": {
    "output_amplifier": "Electron Multiplying",
    "readout_rate": "1 MHz",
    "gain": 300,
    "binning": 4,
    "cooler": true,
    "target_temperature": -80
  },
  "camera_readback": {
    "output_amplifier": "Electron Multiplying",
    "readout_rate": "1 MHz",
    "gain": 300,
    "binning": 4,
    "cooler": true,
    "target_temperature": -80
  },
  "steps": [
    {
      "label": "frame",
      "exposure_ms": 5000,
      "n_frames": 3,
      "duration_s": null
    }
  ],
  "analysis": {
    "mode": "integrated",
    "label": "frame",
    "window_min": 60,
    "e_per_adu": null,
    "em_gain": 300
  },
  "rois": {
    "sample": null,
    "background": null
  }
}
```

## Files

- `config.json` settings, `events.log` timeline, `frames.csv` one row per frame
- `environment.csv` camera and enclosure log, `analysis.csv` per-frame results
- `raw/` 3 full bit-depth TIFFs
- `report.ipynb` redraws every figure from this folder (run all cells)
