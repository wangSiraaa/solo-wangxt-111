"""优化器测试：可行解、含水率成本变化、廉价原料超限冲突、缺测前置拦截。"""
import pytest

from app.chemistry import ChemistryError
from app.optimizer import OptMaterial, OptTargets, solve

COMP = dict(
    LS_H=dict(cao=50.5, sio2=4.2, al2o3=1.3, fe2o3=0.6, mgo=1.2, so3=0.20,
              k2o=0.25, na2o=0.05, cl=0.005, loi=41.2),
    LS_L=dict(cao=45.5, sio2=9.5, al2o3=2.4, fe2o3=1.0, mgo=2.0, so3=0.30,
              k2o=0.45, na2o=0.08, cl=0.008, loi=38.0),
    SST=dict(cao=1.2, sio2=82.5, al2o3=7.8, fe2o3=3.0, mgo=0.8, so3=0.10,
             k2o=1.10, na2o=0.25, cl=0.010, loi=2.8),
    SH=dict(cao=6.5, sio2=58.0, al2o3=17.0, fe2o3=7.5, mgo=2.0, so3=0.40,
            k2o=2.60, na2o=0.70, cl=0.015, loi=4.5),
    FA=dict(cao=4.5, sio2=52.0, al2o3=28.0, fe2o3=7.5, mgo=1.4, so3=0.80,
            k2o=1.50, na2o=0.60, cl=0.010, loi=4.0),
    FE=dict(cao=5.0, sio2=22.0, al2o3=9.0, fe2o3=52.0, mgo=2.5, so3=0.50,
            k2o=0.30, na2o=0.10, cl=0.020, loi=1.5),
    CG=dict(cao=3.0, sio2=55.0, al2o3=20.0, fe2o3=6.0, mgo=1.5, so3=1.20,
            k2o=3.20, na2o=0.80, cl=0.040, loi=8.0),
)

TARGETS = OptTargets(kh_min=0.86, kh_max=0.96, sm_min=2.3, sm_max=2.8,
                     im_min=1.2, im_max=1.8)


def om(code, moisture, price, mn=0.0, mx=1.0, cheap=False):
    # mn 以百分数传入；mx 已是 0~1 的分数（1.0 = 不限制）
    return OptMaterial(code=code, name=code, composition=COMP[code],
                       moisture_pct=moisture, price_wet_t=price,
                       min_frac=mn / 100, max_frac=mx, cheap=cheap)


def base_set():
    return [
        om("LS_H", 2.0, 38, mn=55),
        om("LS_L", 3.0, 22, cheap=True),
        om("SST", 5.0, 72),
        om("SH", 8.0, 45),
        om("FA", 18.0, 55, mx=0.12),
        om("FE", 12.0, 210, mx=0.05),
    ]


def test_base_scenario_feasible_and_indicators_in_range():
    r = solve(base_set(), TARGETS)
    assert r["status"] == "feasible"
    feasible = [m for m in r["modes"] if m["status"] == "feasible"]
    assert len(feasible) >= 2
    for m in feasible:
        t = m["trace"]
        ind = t["indicators"]
        assert 0.86 - 1e-6 <= ind["kh"] <= 0.96 + 1e-6
        assert 2.3 - 1e-6 <= ind["sm"] <= 2.8 + 1e-6
        assert 1.2 - 1e-6 <= ind["im"] <= 1.8 + 1e-6
        fracs = m["fractions_dry"]
        assert abs(sum(fracs) - 1.0) < 1e-6
        assert all(f >= -1e-8 for f in fracs)
        # 质量守恒复核：直接用 trace 合成成分
        assert t["composition_dry_pct"]["cao"] > 35  # 石灰石主导


def test_min_cost_uses_cheap_limestone_and_cost_is_lowest():
    r = solve(base_set(), TARGETS)
    by_mode = {m["mode"]: m for m in r["modes"] if m["status"] == "feasible"}
    costs = {k: m["trace"]["cost_dry_t"] for k, m in by_mode.items()}
    assert costs["min_cost"] <= costs["target_center"] + 1e-6
    ls_l_frac = by_mode["min_cost"]["fractions_dry"][1]
    # 低钙石灰石是最便宜的钙源，最低成本方案应明显使用它
    assert ls_l_frac > 0.05


def test_max_cheap_mode_uses_more_cheap_than_center():
    r = solve(base_set(), TARGETS)
    by_mode = {m["mode"]: m for m in r["modes"] if m["status"] == "feasible"}
    cheap_usage = {k: m["fractions_dry"][1] for k, m in by_mode.items()}
    assert cheap_usage.get("max_cheap", 0) >= cheap_usage.get("target_center", 0) - 1e-6


def test_rain_profile_raises_cost():
    ms = base_set()
    base = solve(ms, TARGETS)
    rain = solve(
        ms, TARGETS,
        moisture_overrides={"LS_L": 7.0, "SST": 9.0, "SH": 14.0, "FA": 26.0, "FE": 19.0},
        extra_cost={"LS_L": 3.0},
    )
    assert rain["status"] == "feasible"
    c_base = min(m["trace"]["cost_dry_t"] for m in base["modes"] if m["status"] == "feasible")
    c_rain = min(m["trace"]["cost_dry_t"] for m in rain["modes"] if m["status"] == "feasible")
    assert c_rain > c_base
    # 雨季 trace 中粉煤灰换算因子明显变大
    fa_row = [x for x in rain["modes"][0]["trace"]["materials"] if x["material"] == "FA"][0]
    assert fa_row["dry_factor_t_per_t"] > 1.25


def test_cheap_force_is_infeasible_with_conflict_diagnosis():
    # 低钙石灰石 35% + 煤矸石 15%，收紧 KH 与碱/氯上限 → KH 下限与碱当量上限冲突
    ms = [
        om("LS_L", 3.0, 22, mn=35, cheap=True),
        om("CG", 6.0, 15, mn=15, cheap=True),
        om("SST", 5.0, 72),
        om("FE", 12.0, 210, mx=0.05),
    ]
    t = OptTargets(kh_min=0.90, kh_max=0.96, sm_min=2.5, sm_max=2.9,
                   im_min=1.2, im_max=1.7, alkali_eq_max=0.6, cl_max=0.015)
    r = solve(ms, t)
    assert r["status"] == "infeasible"
    assert r["conflicts"]
    names = " ".join(
        c.get("constraint", "") + c.get("detail", "") +
        " ".join(c.get("constraints", [])) for c in r["conflicts"])
    assert ("KH" in names or "碱" in names or "Cl" in names or "冲突" in names)


def test_min_sum_over_100_caught_by_lp_as_infeasible():
    ms = [om("LS_H", 2.0, 38, mn=70), om("SST", 5.0, 72, mn=40)]
    r = solve(ms, TARGETS)
    assert r["status"] == "infeasible"


def test_missing_analyte_blocks_solver():
    bad = om("SST", 5.0, 72)
    bad.composition = {**bad.composition, "fe2o3": None}
    with pytest.raises(ChemistryError) as ei:
        solve([om("LS_H", 2.0, 38), bad], TARGETS)
    assert ei.value.code == "MISSING_ANALYTES"


def test_feasible_solution_respects_availability():
    r = solve(base_set(), TARGETS)
    for m in r["modes"]:
        if m["status"] != "feasible":
            continue
        # FA 上限 12%，FE 上限 5%
        assert m["fractions_dry"][4] <= 0.12 + 1e-6
        assert m["fractions_dry"][5] <= 0.05 + 1e-6
