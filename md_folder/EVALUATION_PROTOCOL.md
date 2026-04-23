# Evaluation Protocol

## Scope

Protocol nay khoa phan evaluation cho paper theo huong:

- Claim chinh: `adaptive switching` giup duy tri glare suppression trong occlusion/degradation interval.
- Baseline doi chieu chinh: `b3_detector_kf3d_fixed` vs `b4_full_aurabeam`.
- Khong dung full experiment matrix lam bang chung chinh cho claim nay.

## Fixed suites

### 1. Occlusion screening

Su dung file:

- `configs/occlusion_screening_suite.json`

Muc dich:

- Chay 1 lan `c3_fixed_observation` va `c4_adaptive_switching`
- Chi tren 3 case: `rain_1`, `thunder_1`, `fogging_1`
- Dung de chon `occlusion_interval`

### 2. Final evaluation

Su dung file:

- `configs/final_evaluation_suite.json`

Muc dich:

- So sanh `b3_detector_kf3d_fixed` vs `b4_full_aurabeam`
- Tren 5 case:
  - `night_1`: negative control
  - `norm_1`: negative control
  - `rain_1`: realistic adverse weather
  - `thunder_1`: abrupt visual collapse
  - `fogging_1`: sustained visual degradation

## Scenario roles

- `night_1`: negative control, canh dem on dinh de kiem tra switching khong gay side effect.
- `norm_1`: negative control, kiem tra switching khong lam xau case de.
- `rain_1`: failure mode trung gian, detector bat on va dropout ngat quang.
- `thunder_1`: failure mode manh nhat, detector collapse dot ngot.
- `fogging_1`: suy giam contrast keo dai, khong giong thunder.
- `norm_2`: khong nam trong bang chinh; chi dung cho discussion/limitation neu can.

## Occlusion interval rules

- File interval: `configs/occlusion_intervals.json`
- Interval duoc hieu theo `video_frame`
- Moi case occlusion phai co it nhat 1 khoang `start/end`
- `night_1` va `norm_1` khong can occlusion interval

## Primary metrics

### Main metrics for the core claim

- `occlusion_gssr_percent`
- `occlusion_missed_glare_rate_percent`
- `occlusion_false_darkening_rate_percent`

### Supporting metrics

- `occlusion_rmse_xy_px`
- `gssr_percent`
- `missed_glare_rate_percent`
- `false_darkening_rate_percent`

## Run policy

- Screening stage: `n = 1`
- Final reported stage: `n >= 3`
- Khong chay `n = 3` truoc khi khoa xong `occlusion_interval`

## Commands

### Screening

```powershell
venv\Scripts\python.exe scripts\evaluation\run_experiment_suite.py --config configs\occlusion_screening_suite.json
```

### Final evaluation

```powershell
venv\Scripts\python.exe scripts\evaluation\run_experiment_suite.py --config configs\final_evaluation_suite.json --run-name run_01
venv\Scripts\python.exe scripts\evaluation\run_experiment_suite.py --config configs\final_evaluation_suite.json --run-name run_02
venv\Scripts\python.exe scripts\evaluation\run_experiment_suite.py --config configs\final_evaluation_suite.json --run-name run_03
```

### Aggregate

```powershell
venv\Scripts\python.exe scripts\evaluation\aggregate_results.py --results-root artifacts\results_final_eval --output-csv artifacts\tables\final_evaluation_results.csv
```

## Freeze rule

Sau khi da chot `occlusion_intervals.json`, khong thay doi:

- danh sach scenario trong final suite
- baseline chinh `b3` vs `b4`
- main metrics

Neu can them case moi, phai dua vao screening truoc, khong chen thang vao final suite.
