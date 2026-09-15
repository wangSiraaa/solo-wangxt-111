"""自定义场景的入站校验。所有规则在任何 ORM 对象创建前执行，非法不落半成品。"""
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
    """轻量迁移：旧库缺 built_in 列时补列，并把已有 S1-S4 标为内置。"""
    insp = inspect(db.bind)
    cols = {c["name"] for c in insp.get_columns("scenario")}
    if "built_in" not in cols:
        with db.bind.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE scenario ADD COLUMN built_in BOOLEAN NOT NULL DEFAULT 0")
            conn.exec_driver_sql("UPDATE scenario SET built_in = 1 WHERE id <= 4")


def validate_scenario(db: Session, body: ScenarioIn, exclude_id: int | None = None) -> dict:
    """返回 {materials: {code: (Material, assay, cost, min_pct, max_pct)}}；
    非法时 raise ValueError(errors_by_field)。exclude_id 用于更新时排除自身重名。"""
    errors: dict[str, str] = {}

    name = (body.name or "").strip()
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

    # 率值区间
    for key, (lo_field, hi_field), label in (
        ("kh", ("kh_min", "kh_max"), "KH"),
        ("sm", ("sm_min", "sm_max"), "SM"),
        ("im", ("im_min", "im_max"), "IM"),
    ):
        lo, hi = getattr(body, lo_field), getattr(body, hi_field)
        blo, bhi = INDICATOR_BOUNDS[key]
        if not (blo <= lo <= bhi and blo <= hi <= bhi):
            errors[lo_field] = f"{label} 目标必须落在物理合理区间 [{blo}, {bhi}]"
        elif lo >= hi:
            errors[lo_field] = f"{label} 下限必须严格小于上限（当前 {lo} ≥ {hi}）"

    # 有害组分与分母地板（百分数）
    if not 0 < body.denom_floor <= 1.0:
        errors["denom_floor"] = "分母保护阈值必须在 (0, 1.0]% 之间"
    for field, label, (lo, hi) in (
        ("mgo_max", "MgO 上限", PCT["mgo"]),
        ("so3_max", "SO₃ 上限", PCT["so3"]),
        ("alkali_eq_max", "碱当量上限", PCT["alkali"]),
        ("cl_max", "Cl⁻ 上限", PCT["cl"]),
    ):
        v = getattr(body, field)
        if not (lo < v <= hi):
            errors[field] = f"{label}必须在 ({lo}, {hi}]% 之间"

    # 雨季覆盖 JSON
    rain = body.rain_overrides or {}
    extra = body.rain_extra_cost or {}
    if not isinstance(rain, dict) or not isinstance(extra, dict):
        errors["rain_overrides"] = "雨季配置必须是 {原料编码: 数值}"
    else:
        for code, mst in rain.items():
            if not isinstance(mst, (int, float)) or not (0.0 <= mst < 100.0):
                errors[f"rain_overrides.{code}"] = f"{code} 雨季含水率必须在 [0,100)% 内"
        for code, v in extra.items():
            if not isinstance(v, (int, float)) or v < 0:
                errors[f"rain_extra_cost.{code}"] = f"{code} 雨季附加成本不能为负"

    # 原料行
    if len(body.materials) < 2:
        errors["materials"] = "至少选择两种参与原料"
    seen: set[str] = set()
    resolved = {}
    min_sum = 0.0
    for i, item in enumerate(body.materials):
        prefix = f"materials[{item.material_code}]"
        if item.material_code in seen:
            errors[prefix] = "原料重复"
            continue
        seen.add(item.material_code)
        m = db.scalar(select(models.Material)
                      .where(models.Material.code == item.material_code))
        if m is None:
            errors[prefix] = f"原料编码 {item.material_code} 不存在"
            continue
        if m.active_assay_id is None:
            errors[prefix] = f"{item.material_code} 没有生效化验版本"
        if m.active_cost_id is None:
            errors[prefix] = f"{item.material_code} 没有生效成本"
        assay = db.get(models.Assay, m.active_assay_id) if m.active_assay_id else None
        if assay is not None:
            missing = [a for a in ANALYTES if getattr(assay, a) is None]
            if missing:
                errors[prefix] = f"{item.material_code} 生效化验缺测 {missing}，不得入场景"
            if not (0.0 <= assay.moisture_pct < 100.0):
                errors[prefix] = f"{item.material_code} 含水率非法"
            if item.material_code in rain and rain[item.material_code] < assay.moisture_pct:
                # 允许等于/更高；更低也允许（季节假设不同），这里只警告级别，不拦截
                pass
        cost = db.get(models.Cost, m.active_cost_id) if m.active_cost_id else None
        if cost is not None and cost.price_wet_t < 0:
            errors[prefix] = f"{item.material_code} 到厂价不能为负"

        mn = item.min_pct
        mx = item.max_pct
        avail = m.availability
        avail_max = avail.max_fraction_pct if avail and avail.max_fraction_pct >= 0 else 100.0
        eff_max = min(mx if mx is not None else 100.0, avail_max)
        if not (0.0 <= mn <= 100.0):
            errors[f"{prefix}.min_pct"] = "最低掺量必须在 [0,100]% 内"
        elif mx is not None and not (0.0 <= mx <= 100.0):
            errors[f"{prefix}.max_pct"] = "场景上限必须在 [0,100]% 内"
        elif mx is not None and mn > mx + 1e-9:
            errors[f"{prefix}.min_pct"] = f"最低掺量 {mn}% 高于场景上限 {mx}%"
        elif mn > avail_max + 1e-9:
            errors[f"{prefix}.min_pct"] = (
                f"最低掺量 {mn}% 高于可用量上限 {avail_max}%")
        min_sum += mn
        resolved[item.material_code] = (m, assay, cost, mn, eff_max)

    if min_sum > 100.0 + 1e-9:
        errors["materials.min_sum"] = (
            f"最低掺量之和 {min_sum:.1f}% 超过 100%，无可行空间")

    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    return {"materials": resolved}
