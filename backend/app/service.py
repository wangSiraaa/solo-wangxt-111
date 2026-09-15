"""业务编排：ORM → 优化器输入；求解 → 持久化 Solution（含 trace）。"""
import json

from sqlalchemy.orm import Session

from . import models
from .chemistry import ChemistryError, OXIDE_LABELS
from .optimizer import OptMaterial, OptTargets, solve


ANALYTES = ("cao", "sio2", "al2o3", "fe2o3", "mgo", "so3", "k2o", "na2o", "cl", "loi")


def _active_assay(m: models.Material, db: Session) -> models.Assay:
    if m.active_assay_id is None:
        raise ChemistryError(f"原料 {m.code} 没有生效化验版本", "NO_ASSAY", {"material": m.code})
    return db.get(models.Assay, m.active_assay_id)


def build_inputs(db: Session, scenario: models.Scenario, profile: str = "base"):
    """组装优化器输入。profile='rain' 时套用场景里的雨季含水率覆盖。"""
    rain = json.loads(scenario.rain_overrides or "{}")
    extra = json.loads(scenario.rain_extra_cost_json or "{}")
    overrides = rain if profile == "rain" else None
    extra_cost = extra if profile == "rain" else None

    opt_ms, trace_ms, version_map = [], [], {}
    for item in scenario.items:
        m = item.material
        assay = _active_assay(m, db)
        if m.active_cost_id is None:
            raise ChemistryError(f"原料 {m.code} 没有生效成本", "NO_COST", {"material": m.code})
        cost = db.get(models.Cost, m.active_cost_id)
        avail = m.availability

        scenario_max = item.max_pct if item.max_pct is not None else 100.0
        avail_max = avail.max_fraction_pct if avail and avail.max_fraction_pct >= 0 else 100.0
        max_pct = min(scenario_max, avail_max)
        if item.min_pct > max_pct + 1e-9:
            raise ChemistryError(
                f"原料 {m.code} 最低掺量 {item.min_pct}% 高于可用量上限 {max_pct}%",
                "MIN_EXCEEDS_AVAIL",
                {"material": m.code, "min_pct": item.min_pct, "max_pct": max_pct},
            )

        comp = {a: getattr(assay, a) for a in ANALYTES}
        om = OptMaterial(
            code=m.code, name=m.name, composition=comp,
            moisture_pct=assay.moisture_pct, price_wet_t=cost.price_wet_t,
            min_frac=item.min_pct / 100.0, max_frac=max_pct / 100.0,
            cheap=item.preferred_cheap,
        )
        opt_ms.append(om)
        version_map[m.code] = {
            "material": m.code, "name": m.name,
            "assay_version": assay.version, "assay_id": assay.id,
            "lab_note": assay.lab_note,
            "cost_id": cost.id, "price_wet_t": cost.price_wet_t,
            "min_pct": item.min_pct, "max_pct": max_pct,
            "availability_note": avail.supply_note if avail else "",
            "composition_dry_pct": {a: comp[a] for a in ANALYTES},
            "composition_label": OXIDE_LABELS,
        }
        trace_ms.append(m)

    if sum(m.min_frac for m in opt_ms) > 1.0 + 1e-9:
        raise ChemistryError(
            f"最低掺量之和 {100*sum(m.min_frac for m in opt_ms):.1f}% 超过 100%，无可行空间",
            "MIN_SUM_EXCEEDS_100",
            {"min_sum_pct": 100 * sum(m.min_frac for m in opt_ms)},
        )

    targets = OptTargets(
        kh_min=scenario.kh_min, kh_max=scenario.kh_max,
        sm_min=scenario.sm_min, sm_max=scenario.sm_max,
        im_min=scenario.im_min, im_max=scenario.im_max,
        mgo_max=scenario.mgo_max, so3_max=scenario.so3_max,
        alkali_eq_max=scenario.alkali_eq_max, cl_max=scenario.cl_max,
        denom_floor=scenario.denom_floor,
    )
    return opt_ms, targets, overrides, extra_cost, version_map


def run_scenario(db: Session, scenario: models.Scenario, profile: str, persist: bool = True):
    opt_ms, targets, overrides, extra_cost, version_map = build_inputs(db, scenario, profile)
    result = solve(opt_ms, targets, moisture_overrides=overrides, extra_cost=extra_cost)

    provenance = {
        "scenario": {"id": scenario.id, "name": scenario.name, "description": scenario.description},
        "profile": profile,
        "profile_note": ("雨季：套用场景含水率覆盖与附加成本" if profile == "rain"
                         else "基线：使用各原料生效化验版本的含水率"),
        "assay_versions": version_map,
        "basis": {
            "mix_variable": "x_i = 原料 i 占干生料质量分数，Σx_i=1",
            "composition": "干基质量百分数，合成 = Σ x_i·c_i（质量守恒）",
            "indicators": "SM=SiO2/(Al2O3+Fe2O3)，IM=Al2O3/Fe2O3，"
                          "KH=(CaO-1.65Al2O3-0.35Fe2O3)/(2.8SiO2)",
            "wet_dry_conversion": "湿吨=干吨/(1-含水率)；干基吨成本=湿吨价/(1-含水率)",
            "missing_policy": "必需氧化物缺测(NULL)拒绝计算；有害组分缺测按未检出0处理并记录",
        },
        "targets": {
            "kh": [targets.kh_min, targets.kh_max], "sm": [targets.sm_min, targets.sm_max],
            "im": [targets.im_min, targets.im_max],
            "mgo_max": targets.mgo_max, "so3_max": targets.so3_max,
            "alkali_eq_max": targets.alkali_eq_max, "cl_max": targets.cl_max,
            "denom_floor_pct": targets.denom_floor,
        },
        "linear_constraints": result["constraint_log"],
    }

    saved = []
    for r in result["modes"]:
        if r["status"] != "feasible":
            sol = models.Solution(
                scenario_id=scenario.id, profile=profile, mode=r["mode"],
                status=r["status"], trace_json=json.dumps(
                    {"provenance": provenance, "reason": r.get("reason"),
                     "error_code": r.get("error_code"), "details": r.get("details")},
                    ensure_ascii=False),
                conflict_json=json.dumps([]),
            )
        else:
            t = r["trace"]
            ind = t["indicators"]
            sol = models.Solution(
                scenario_id=scenario.id, profile=profile, mode=r["mode"], status="feasible",
                cost_dry_t=t["cost_dry_t"], cost_wet_t=t["cost_wet_equiv_t"],
                kh=ind["kh"], sm=ind["sm"], im=ind["im"],
                mix_json=json.dumps(
                    {m.code: round(f * 100, 4) for m, f in zip(opt_ms, r["fractions_dry"])},
                    ensure_ascii=False),
                trace_json=json.dumps(
                    {"provenance": provenance, "trace": t, "duplicate": r["duplicate"]},
                    ensure_ascii=False),
                conflict_json="[]",
            )
        if persist:
            db.add(sol); db.flush()
        saved.append(sol)

    if result["status"] != "feasible":
        sol = models.Solution(
            scenario_id=scenario.id, profile=profile, mode="diagnosis",
            status="infeasible",
            trace_json=json.dumps({"provenance": provenance}, ensure_ascii=False),
            conflict_json=json.dumps(result["conflicts"], ensure_ascii=False),
        )
        if persist:
            db.add(sol); db.flush()
        saved.append(sol)

    if persist:
        db.commit()
    return result, saved
