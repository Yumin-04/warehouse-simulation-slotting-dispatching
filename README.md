# Warehouse Simulation: Slotting and Dispatching

Rev. 1 | Created: 2026-10-19 | Updated: 2026-10-19 16:23 CDT

자동화 창고에서 적치(slotting)와 작업할당(dispatching)을 따로 다루던 기존 방식의 한계를 보완하기 위해, 두 규칙을 함께 평가하는 이산 사건 시뮬레이션입니다.

A discrete-event simulation that evaluates slotting and dispatching rules together for an automated warehouse, instead of treating them as separate decisions.

- Project period: 2025.09 ~ 2026.08 (Capstone Design)

---

## 1. Overview

### 1.1 Rule Combinations

Table 1. Simulated rules

| Type | Rules |
|---|---|
| Slotting | random, abc, characteristic, kitting |
| Dispatching | nearest, density, load_balance |

- **[KR]** 슬로팅 4종과 디스패칭 3종을 조합한 12개 전략을 같은 주문 계획으로 시뮬레이션합니다.
- **[EN]** The simulation runs 12 strategies, combining 4 slotting rules and 3 dispatching rules, on the same order plan.
- **[KR]** random 슬로팅과 nearest 디스패칭의 조합을 baseline으로 두고, 이동 거리 등 KPI의 개선율을 비교합니다.
- **[EN]** The random slotting and nearest dispatching combination is the baseline, and KPI improvement such as travel distance is reported against it.

### 1.2 Model

- **[KR]** 3개 블록(통로 18개)이 이어진 창고 layout에서 물류 로봇이 station과 rack 사이를 이동하며 batch 작업을 처리합니다.
- **[EN]** Logistics robots move between stations and racks in a three-block warehouse layout (18 aisles) and process batched tasks.
- **[KR]** 시뮬레이션은 SimPy로 구현했고, 결과는 주간 scoreboard와 KPI scoreboard, 경로 시각화로 확인합니다.
- **[EN]** The model is built with SimPy, and results are reviewed through a weekly scoreboard, a KPI scoreboard, and trajectory visualizations.

---

## 2. Data

- **[KR]** `data/` 폴더의 파일은 구조를 보여 주기 위해 만든 임의의 toy data입니다. 실제 프로젝트 데이터는 포함하지 않습니다.
- **[EN]** The files in `data/` are small synthetic toy data that show the input structure. No real project data is included.
- **[KR]** toy data의 결과는 원 프로젝트의 결과와 다르므로, 수치 비교에 사용하지 마세요.
- **[EN]** Results on the toy data differ from the original project, so do not use them for numeric comparison.

Table 2. Input files

| File | Columns |
|---|---|
| `outbound_plan_toy.csv` | 생산일자, No., 부품코드 |
| `production_plan_toy.csv` | 생산일자, 제품코드 |
| `BOM_toy.xlsx` | product_code, part_code |

---

## 3. Usage

```bash
pip install -r requirements.txt
python src/warehouse_sim.py
```

- **[KR]** 한글 font로 Malgun Gothic을 사용하므로, 다른 OS에서는 `src/warehouse_sim.py`의 font 설정을 바꿔야 할 수 있습니다.
- **[EN]** The plots use the Malgun Gothic font, so you may need to change the font setting in `src/warehouse_sim.py` on other operating systems.

---

## 4. Development History

- **[KR]** `notebooks/warehouse_simulation.ipynb`는 2026년 2월부터 7월까지 작성한 notebook이며, 각 버전은 원본 파일의 마지막 수정 날짜로 git 기록에 남겼습니다. 원본 데이터 파일이 필요해 그대로는 실행되지 않습니다.
- **[EN]** `notebooks/warehouse_simulation.ipynb` was written between February and July 2026, and each version is recorded in git with the last-modified date of the original file. It needs the original data files, so it does not run as is.
- **[KR]** `src/warehouse_sim.py`는 이 notebook을 독립 script로 정리하고 toy data로 실행되게 만든 버전입니다.
- **[EN]** `src/warehouse_sim.py` is the standalone script version that runs on the toy data.

---

## 5. Structure

```text
.
├── data/
│   ├── BOM_toy.xlsx
│   ├── outbound_plan_toy.csv
│   └── production_plan_toy.csv
├── notebooks/
│   └── warehouse_simulation.ipynb
├── src/
│   └── warehouse_sim.py
└── requirements.txt
```
