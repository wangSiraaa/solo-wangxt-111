"""自定义场景与修订版的入站校验。所有规则在任何 ORM 对象创建前执行，非法不落半成品。

核心收集器同时支持两种引用来源：
- 场景建单：使用原料「当前生效」化验/成本；
- 修订版草稿/发布：使用 payload 中钉住的 assay_id/cost_id（旧修订不受后续切换影响）。
"""
from __future__ import annotations

import json

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from . import models
from .schemas import ScenarioIn

# 百分比硬界（均按百分数，非分数）
PCT = {"mgo": (0, 100), "so3": (0, 100), "alkali": (0, 100), "cl": (0, 5)}
INDICATOR_BOUNDS = {"kh": (0, 2.0), "sm": (0.05, 20.0), "im": (0.05, 50.0)}
ANALYTES = ("cao", "sio2", "al2o3", "fe2o3", "loi")


def ensure_schema(db: Session) -> None:
    """轻量迁移：补列、补单草稿部分索引、回填修订链，并修复任何半发布指针。"""
    insp = inspect(db.bind)
    tables = insp.get_table_names()

    if "scenario" in tables:
        cols = {c["name"] for c in insp.get_columns("scenario")}
        with db.bind.begin() as conn:
            if "built_in" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE scenario ADD COLUMN built_in BOOLEAN NOT NULL DEFAULT 0")
                conn.exec_driver_sql("UPDATE scenario SET built_in = 1 WHERE id <= 4")
            if "published_revision_id" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE scenario ADD COLUMN published_revision_id INTEGER")
    if "solution" in tables:
        cols = {c["name"] for c in insp.get_columns("solution")}
        with db.bind.begin() as conn:
            if "scenario_revision_id" not in cols:
                conn.exec_driver_sql(
                    "ALTER TABLE solution ADD COLUMN scenario_revision_id INTEGER")
            if "revision_no" not in cols:
                conn.exec_driver_sql("ALTER TABLE solution ADD COLUMN revision_no INTEGER")


def _validate_parts(
    db: Session, *, name: str, kh_min: float, kh_max: float,
    sm_min: float, sm_max: float, im_min: float, im_max: float,
    mgo_max: float, so3_max: float, alkali_eq_max: float, cl_max: float,
    denom_floor: float, rain: dict, extra: dict,
    rows: list[dict], exclude_id: int | None,
) -> dict[str, str]:
    """rows: [{code, assay_id, cost_id, min_pct, max_pct, preferred_cheap}]。"""
    errors: dict[str, str] = {}

    name = (name or "").strip()
    if not name:
        errors["name"] = "场景名称必填"
    else:
        q = select(models.Scenario).where(models.Scenario.name == name)
        if exclude_id is not None:
            q = q.where(models.Scenario.id != exclude_id)
        if db.scalar(q):
            errors["name"] = f"场景名称「{name}」已存在，禁止同名"
        if len(name) > 128:
            errors["name"] = "场景名称不超过 128 字"

    for key, (lo_v, hi_v), label in (
        ("kh", (kh_min, kh_max), "KH"),
        ("sm", (sm_min, sm_max), "SM"),
        ("im", (im_min, im_max), "IM"),
    ):
        lo_field = {"kh": "kh_min", "sm": "sm_min", "im": "im_min"}[key]
        blo, bhi = INDICATOR_BOUNDS[key]
        if not (blo <= lo_v <= bhi and blo <= hi_v <= bhi):
            errors[lo_field] = f"{label} 目标必须落在物理合理区间 [{blo}, {bhi}]"
        elif lo_v >= hi_v:
            errors[lo_field] = f"{label} 下限必须严格小于上限（当前 {lo_v} ≥ {hi_v}）"

    if not 0 < denom_floor <= 1.0:
        errors["denom_floor"] = "分母保护阈值必须在 (0, 1.0]% 之间"
    for field, label, (lo, hi), val in (
        ("mgo_max", "MgO 上限", PCT["mgo"], mgo_max),
        ("so3_max", "SO₃ 上限", PCT["so3"], so3_max),
        ("alkali_eq_max", "碱当量上限", PCT["alkali"], alkali_eq_max),
        ("cl_max", "Cl⁻ 上限", PCT["cl"], cl_max),
    ):
        if not (lo < val <= hi):
            errors[field] = f"{label}必须在 ({lo}, {hi}]% 之间"

    if not isinstance(rain, dict) or not isinstance(extra, dict):
        errors["rain_overrides"] = "雨季配置必须是 {原料编码: 数值}"
        rain = rain if isinstance(rain, dict) else {}
        extra = extra if isinstance(extra, dict) else {}
    for code, mst in rain.items():
        if not isinstance(mst, (int, float)) or isinstance(mst, bool) or not (0.0 <= mst < 100.0):
            errors[f"rain_overrides.{code}"] = f"{code} 雨季含水率必须在 [0,100)% 内"
    for code, v in extra.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            errors[f"rain_extra_cost.{code}"] = f"{code} 雨季附加成本不能为负"

    if len(rows) < 2:
        errors["materials"] = "至少选择两种参与原料"
    seen: set[str] = set()
    min_sum = 0.0
    for row in rows:
        code = row["code"]
        prefix = f"materials[{code}]"
        if code in seen:
            errors[prefix] = "原料重复"
            continue
        seen.add(code)
        m = db.scalar(select(models.Material).where(models.Material.code == code))
        if m is None:
            errors[prefix] = f"原料编码 {code} 不存在"
            continue
        assay_id, cost_id = row.get("assay_id"), row.get("cost_id")
        assay = db.get(models.Assay, assay_id) if assay_id else None
        if assay_id is None or assay is None or assay.material_id != m.id:
            errors[f"{prefix}.assay"] = f"{code} 没有有效的生效化验版本"
        else:
            missing = [a for a in ANALYTES if getattr(assay, a) is None]
            if missing:
                errors[f"{prefix}.assay"] = f"{code} 生效化验缺测 {missing}，不得入场景"
            elif not (0.0 <= assay.moisture_pct < 100.0):
                errors[f"{prefix}.assay"] = f"{code} 含水率非法"
        cost = db.get(models.Cost, cost_id) if cost_id else None
        if cost_id is None or cost is None or cost.material_id != m.id:
            errors[f"{prefix}.cost"] = f"{code} 没有有效的生效成本"
        elif cost.price_wet_t < 0:
            errors[f"{prefix}.cost"] = f"{code} 到厂价不能为负"

        mn, mx = row["min_pct"], row["max_pct"]
        avail = m.availability
        avail_max = avail.max_fraction_pct if avail and avail.max_fraction_pct >= 0 else 100.0
        if not (0.0 <= mn <= 100.0):
            errors[f"{prefix}.min_pct"] = "最低掺量必须在 [0,100]% 内"
        elif mx is not None and not (0.0 <= mx <= 100.0):
            errors[f"{prefix}.max_pct"] = "场景上限必须在 [0,100]% 内"
        elif mx is not None and mn > mx + 1e-9:
            errors[f"{prefix}.min_pct"] = f"最低掺量 {mn}% 高于场景上限 {mx}%"
        elif mn > avail_max + 1e-9:
            errors[f"{prefix}.min_pct"] = f"最低掺量 {mn}% 高于可用量上限 {avail_max}%"
        min_sum += mn

    for code in rain:
        if code not in seen:
            errors[f"rain_overrides.{code}"] = (
                f"雨季含水率覆盖的原料编码 {code} 不在本次参与原料列表中")
    for code in extra:
        if code not in seen:
            errors[f"rain_extra_cost.{code}"] = (
                f"雨季附加成本的原料编码 {code} 不在本次参与原料列表中")

    if min_sum > 100.0 + 1e-9:
        errors["materials.min_sum"] = f"最低掺量之和 {min_sum:.1f}% 超过 100%，无可行空间"
    return errors


def _rows_from_body(db: Session, body: ScenarioIn) -> list[dict]:
    rows = []
    for item in body.materials:
        m = db.scalar(select(models.Material)
                      .where(models.Material.code == item.material_code))
        rows.append({
            "code": item.material_code,
            "assay_id": m.active_assay_id if m else None,
            "cost_id": m.active_cost_id if m else None,
            "min_pct": item.min_pct, "max_pct": item.max_pct,
            "preferred_cheap": item.preferred_cheap,
        })
    return rows


def validate_scenario(db: Session, body: ScenarioIn, exclude_id: int | None = None) -> None:
    """场景建单校验（引用当前生效化验/成本）。非法 raise ValueError(errors_json)。"""
    errors = _validate_parts(
        db, name=body.name,
        kh_min=body.kh_min, kh_max=body.kh_max, sm_min=body.sm_min, sm_max=body.sm_max,
        im_min=body.im_min, im_max=body.im_max,
        mgo_max=body.mgo_max, so3_max=body.so3_max,
        alkali_eq_max=body.alkali_eq_max, cl_max=body.cl_max,
        denom_floor=body.denom_floor,
        rain=body.rain_overrides or {}, extra=body.rain_extra_cost or {},
        rows=_rows_from_body(db, body), exclude_id=exclude_id)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))


def validate_payload(db: Session, payload: dict, exclude_id: int | None = None) -> None:
    """修订版 payload 校验（引用钉住的 assay_id/cost_id）。"""
    errors = _validate_parts(
        db, name=payload.get("name", ""),
        kh_min=payload["kh_min"], kh_max=payload["kh_max"],
        sm_min=payload["sm_min"], sm_max=payload["sm_max"],
        im_min=payload["im_min"], im_max=payload["im_max"],
        mgo_max=payload["mgo_max"], so3_max=payload["so3_max"],
        alkali_eq_max=payload["alkali_eq_max"], cl_max=payload["cl_max"],
        denom_floor=payload["denom_floor"],
        rain=payload.get("rain_overrides", {}),
        extra=payload.get("rain_extra_cost", {}),
        rows=payload["materials"], exclude_id=exclude_id)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
