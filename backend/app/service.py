"""业务编排：修订版 payload → 优化器输入；求解 → 持久化 Solution（含 trace）。

所有求解都从「已发布修订版」的钉住快照（assay_id/cost_id）取引用，
因此切换生效化验/成本后，旧修订与旧解的追溯内容保持不变。
"""
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .chemistry import ChemistryError, OXIDE_LABELS
from .optimizer import OptMaterial, OptTargets, solve

ANALYTES = ("cao", "sio2", "al2o3", "fe2o3", "mgo", "so3", "k2o", "na2o", "cl", "loi")


def _scalar(p: dict, key: str):
    return p[key]


def build_inputs_from_payload(db: Session, scenario: models.Scenario,
                              revision: models.ScenarioRevision,
                              payload: dict, profile: str = "base"):
    """从修订 payload 组装优化器输入，引用钉住的 assay_id/cost_id。"""
    rain = payload.get("rain_overrides", {}) or {}
    extra = payload.get("rain_extra_cost", {}) or {}
    overrides = rain if profile == "rain" else None
    extra_cost = extra if profile == "rain" else None

    opt_ms, version_map = [], {}
    for row in payload["materials"]:
        m = db.scalar(select(models.Material).where(models.Material.code == row["code"]))
        if m is None:
            raise ChemistryError(f"原料 {row['code']} 已不存在", "MATERIAL_GONE",
                                 {"material": row["code"]})
        assay = db.get(models.Assay, row.get("assay_id"))
        if assay is None or assay.material_id != m.id:
            raise ChemistryError(
                f"原料 {m.code} 在修订 r{revision.revision_no} 钉住的化验版本已不存在",
                "PINNED_ASSAY_GONE", {"material": m.code, "assay_id": row.get("assay_id")})
        cost = db.get(models.Cost, row.get("cost_id"))
        if cost is None or cost.material_id != m.id:
            raise ChemistryError(
                f"原料 {m.code} 在修订 r{revision.revision_no} 钉住的成本版本已不存在",
                "PINNED_COST_GONE", {"material": m.code, "cost_id": row.get("cost_id")})
        avail = m.availability

        scenario_max = row["max_pct"] if row["max_pct"] is not None else 100.0
        avail_max = avail.max_fraction_pct if avail and avail.max_fraction_pct >= 0 else 100.0
        max_pct = min(scenario_max, avail_max)
        if row["min_pct"] > max_pct + 1e-9:
            raise ChemistryError(
                f"原料 {m.code} 最低掺量 {row['min_pct']}% 高于可用量上限 {max_pct}%",
                "MIN_EXCEEDS_AVAIL",
                {"material": m.code, "min_pct": row["min_pct"], "max_pct": max_pct})

        comp = {a: getattr(assay, a) for a in ANALYTES}
        opt_ms.append(OptMaterial(
            code=m.code, name=m.name, composition=comp,
            moisture_pct=assay.moisture_pct, price_wet_t=cost.price_wet_t,
            min_frac=row["min_pct"] / 100.0, max_frac=max_pct / 100.0,
            cheap=bool(row.get("preferred_cheap", False)),
        ))
        version_map[m.code] = {
            "material": m.code, "name": m.name,
            "assay_version": assay.version, "assay_id": assay.id,
            "lab_note": assay.lab_note,
            "cost_id": cost.id, "price_wet_t": cost.price_wet_t,
            "min_pct": row["min_pct"], "max_pct": max_pct,
            "availability_note": avail.supply_note if avail else "",
            "composition_dry_pct": {a: comp[a] for a in ANALYTES},
            "composition_label": OXIDE_LABELS,
        }

    if sum(m.min_frac for m in opt_ms) > 1.0 + 1e-9:
        raise ChemistryError(
            f"最低掺量之和 {100*sum(m.min_frac for m in opt_ms):.1f}% 超过 100%，无可行空间",
            "MIN_SUM_EXCEEDS_100",
            {"min_sum_pct": 100 * sum(m.min_frac for m in opt_ms)})

    targets = OptTargets(
        kh_min=_scalar(payload, "kh_min"), kh_max=_scalar(payload, "kh_max"),
        sm_min=_scalar(payload, "sm_min"), sm_max=_scalar(payload, "sm_max"),
        im_min=_scalar(payload, "im_min"), im_max=_scalar(payload, "im_max"),
        mgo_max=payload["mgo_max"], so3_max=payload["so3_max"],
        alkali_eq_max=payload["alkali_eq_max"], cl_max=payload["cl_max"],
        denom_floor=payload["denom_floor"],
    )
    return opt_ms, targets, overrides, extra_cost, version_map


def run_revision(db: Session, scenario: models.Scenario,
                 revision: models.ScenarioRevision, profile: str,
                 persist: bool = True):
    """对已发布修订版求解，结果绑定 revision id/no。"""
    payload = json.loads(revision.payload_json)
    opt_ms, targets, overrides, extra_cost, version_map = build_inputs_from_payload(
        db, scenario, revision, payload, profile)
    result = solve(opt_ms, targets, moisture_overrides=overrides, extra_cost=extra_cost)

    provenance = {
        "scenario": {"id": scenario.id, "name": payload["name"],
                     "description": payload.get("description", "")},
        "revision": {"revision_id": revision.id, "revision_no": revision.revision_no,
                     "status": revision.status,
                     "published_at": revision.published_at.isoformat()
                                     if revision.published_at else None},
        "profile": profile,
        "profile_note": ("雨季：套用修订版含水率覆盖与附加成本" if profile == "rain"
                         else "基线：使用该修订版钉住化验版本的含水率"),
        "assay_versions": version_map,
        "basis": {
            "mix_variable": "x_i = 原料 i 占干生料质量分数，Σx_i=1",
            "composition": "干基质量百分数，合成 = Σ x_i·c_i（质量守恒）",
            "indicators": "SM=SiO2/(Al2O3+Fe2O3)，IM=Al2O3/Fe2O3，"
                          "KH=(CaO-1.65Al2O3-0.35Fe2O3)/(2.8SiO2)",
            "wet_dry_conversion": "湿吨=干吨/(1-含水率)；干基吨成本=湿吨价/(1-含水率)",
            "missing_policy": "必需氧化物缺测(NULL)拒绝计算；有害组分缺测按未检出0处理并记录",
            "revision_binding": "求解绑定已发布修订版与钉住的 assay_id/cost_id；"
                                "切换生效版本不改变旧修订、旧解",
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

    def _new_sol(**kw):
        return models.Solution(scenario_id=scenario.id,
                                scenario_revision_id=revision.id,
                                revision_no=revision.revision_no, **kw)

    saved = []
    for r in result["modes"]:
        if r["status"] != "feasible":
            sol = _new_sol(
                profile=profile, mode=r["mode"], status=r["status"],
                trace_json=json.dumps(
                    {"provenance": provenance, "reason": r.get("reason"),
                     "error_code": r.get("error_code"), "details": r.get("details")},
                    ensure_ascii=False),
                conflict_json=json.dumps([]))
        else:
            t = r["trace"]
            ind = t["indicators"]
            sol = _new_sol(
                profile=profile, mode=r["mode"], status="feasible",
                cost_dry_t=t["cost_dry_t"], cost_wet_t=t["cost_wet_equiv_t"],
                kh=ind["kh"], sm=ind["sm"], im=ind["im"],
                mix_json=json.dumps(
                    {m.code: round(f * 100, 4) for m, f in zip(opt_ms, r["fractions_dry"])},
                    ensure_ascii=False),
                trace_json=json.dumps(
                    {"provenance": provenance, "trace": t, "duplicate": r["duplicate"]},
                    ensure_ascii=False),
                conflict_json="[]")
        if persist:
            db.add(sol); db.flush()
        saved.append(sol)

    if result["status"] != "feasible":
        sol = _new_sol(
            profile=profile, mode="diagnosis", status="infeasible",
            trace_json=json.dumps({"provenance": provenance}, ensure_ascii=False),
            conflict_json=json.dumps(result["conflicts"], ensure_ascii=False))
        if persist:
            db.add(sol); db.flush()
        saved.append(sol)

    if persist:
        db.commit()
    return result, saved


# 兼容旧调用名（内置/测试中仍可能直接传入场景对象）
def run_scenario(db: Session, scenario: models.Scenario, profile: str,
                 persist: bool = True):
    revision = db.get(models.ScenarioRevision, scenario.published_revision_id)
    if revision is None:
        raise ChemistryError("场景没有已发布修订版，不能求解", "NO_PUBLISHED_REVISION")
    return run_revision(db, scenario, revision, profile, persist=persist)
