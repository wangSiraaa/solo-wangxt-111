"""并行试验分支的三方合并。

规则（base = 两分支共同来源的已发布修订）：
- 标量边界（KH/SM/IM、有害上限、地板、说明）：base/a/b 三值比较，
  仅一方改动 → 自动采用；两方都改且不同值 → 冲突，不静默选边；两方都改成同值 → 自动。
- 雨季覆盖/附加成本（map<代码,数值>）：按 key 逐值做三方比较。
- 原料掺量：以 base 原料集合为准，对每原料的 min/max/cheap 逐字段三方比较；
  一方新增/删除原料时，若另一方也动了同一原料（增删改掺量）→ 冲突。
- 钉住的 assay_id/cost_id：任一父分支引用的版本已失效（记录不存在/不属于该原料）
  → 定位到具体原料的硬冲突。
合并产物为 status=merged 的候选修订（不可求解），必须经乐观锁发布才冻结。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import models
from .revision_service import RevError, _get_scenario, _guard_custom, _draft, _next_no, fingerprint
from .scenario_validation import validate_payload

SCALAR_LABELS = {
    "kh_min": "KH 下限", "kh_max": "KH 上限", "sm_min": "SM 下限", "sm_max": "SM 上限",
    "im_min": "IM 下限", "im_max": "IM 上限", "mgo_max": "MgO 上限", "so3_max": "SO₃ 上限",
    "alkali_eq_max": "碱当量上限", "cl_max": "Cl⁻ 上限", "denom_floor": "分母地板",
    "description": "说明",
}
MAT_FIELD_LABELS = {"min_pct": "最低掺量", "max_pct": "场景上限", "preferred_cheap": "廉价标记"}


def _rev(db: Session, sc: models.Scenario, no: int) -> models.ScenarioRevision:
    r = db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == sc.id,
        models.ScenarioRevision.revision_no == no))
    if r is None:
        raise RevError(404, {"message": f"修订版 r{no} 不存在"})
    return r


def _three_way(base, a, b):
    """返回 (chosen, status)：status ∈ auto-a / auto-b / auto-same / conflict。"""
    if a == b:
        return a, "auto-same"
    if a == base:
        return b, "auto-b"
    if b == base:
        return a, "auto-a"
    return None, "conflict"


def _merge_scalar(base_p, pa, pb, field, auto, conflicts, decisions):
    label = SCALAR_LABELS[field]
    chosen, st = _three_way(base_p.get(field), pa.get(field), pb.get(field))
    if st == "conflict":
        if field in decisions:
            chosen = decisions[field]
            st = "manual"
        else:
            conflicts.append({"field": field, "label": label, "kind": "scalar",
                              "base": base_p.get(field), "a": pa.get(field),
                              "b": pb.get(field)})
            return
    auto.append({"field": field, "label": label, "resolution": st,
                 "base": base_p.get(field), "a": pa.get(field), "b": pb.get(field),
                 "chosen": chosen})
    return chosen


def _merge_map(base_p, pa, pb, field, label, auto, conflicts, decisions, prefix):
    keys = set(base_p.get(field, {})) | set(pa.get(field, {})) | set(pb.get(field, {}))
    out = dict(base_p.get(field, {}))
    for k in sorted(keys):
        bv = base_p.get(field, {}).get(k)
        av = pa.get(field, {}).get(k)
        bval = pb.get(field, {}).get(k)
        chosen, st = _three_way(bv, av, bval)
        f = f"{prefix}.{k}"
        if st == "conflict":
            if f in decisions:
                chosen = decisions[f]; st = "manual"
            else:
                conflicts.append({"field": f, "label": f"{label} {k}", "kind": "map",
                                  "base": bv, "a": av, "b": bval})
                continue
        auto.append({"field": f, "label": f"{label} {k}", "resolution": st,
                     "base": bv, "a": av, "b": bval, "chosen": chosen})
        if chosen is None:
            out.pop(k, None)
        else:
            out[k] = chosen
    return out


def _materials_index(payload):
    return {m["code"]: m for m in payload["materials"]}


def _check_pinned(db: Session, mat_row, code, conflicts):
    """钉住的化验/成本必须仍存在且属于该原料。"""
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if m is None:
        conflicts.append({"field": f"materials.{code}.ref", "kind": "pinned_ref",
                          "label": f"{code} 原料已不存在"})
        return
    assay = db.get(models.Assay, mat_row.get("assay_id"))
    if assay is None or assay.material_id != m.id:
        conflicts.append({"field": f"materials.{code}.assay_id", "kind": "pinned_ref",
                          "label": f"{code} 钉住的化验版本已失效",
                          "a": mat_row.get("assay_id")})
    cost = db.get(models.Cost, mat_row.get("cost_id"))
    if cost is None or cost.material_id != m.id:
        conflicts.append({"field": f"materials.{code}.cost_id", "kind": "pinned_ref",
                          "label": f"{code} 钉住的成本版本已失效",
                          "a": mat_row.get("cost_id")})


def _merge_materials(db, base_p, pa, pb, auto, conflicts, decisions):
    bm, am, bmm = _materials_index(base_p), _materials_index(pa), _materials_index(pb)
    codes = set(bm) | set(am) | set(bmm)
    out = []
    for code in sorted(codes):
        brow, arow, brrow = bm.get(code), am.get(code), bmm.get(code)
        in_b, in_a, in_b2 = brow is not None, arow is not None, brrow is not None
        if in_a != in_b2 and (in_a != in_b or in_b2 != in_b):
            # 一方新增、另一方删除（或反之）
            f = f"materials.{code}.presence"
            if f in decisions:
                keep = bool(decisions[f])
            else:
                conflicts.append({"field": f, "kind": "presence",
                                  "label": f"原料 {code} 一方保留/一方删除",
                                  "base": in_b, "a": in_a, "b": in_b2})
                continue
            row = (arow if in_a else brrow) if keep else None
            if keep and row: out.append(dict(row))
            continue
        if not in_a and not in_b2:
            continue  # 两方都删除
        row = dict(brow or arow or brrow)
        if not in_b:
            # base 没有：两方都新增（要求内容一致，否则冲突）
            if in_a and in_b2:
                same = True
                for fld in ("min_pct", "max_pct", "preferred_cheap", "assay_id", "cost_id"):
                    if (arow or {}).get(fld) != (brrow or {}).get(fld):
                        same = False
                if same:
                    out.append(dict(arow))
                    auto.append({"field": f"materials.{code}", "label": f"新增原料 {code}",
                                 "resolution": "auto-same", "chosen": "新增"})
                else:
                    conflicts.append({"field": f"materials.{code}", "kind": "add_both",
                                      "label": f"原料 {code} 两方均新增但设置不同",
                                      "a": arow, "b": brrow})
                continue
        if in_a and not in_b2:
            # A 删除，B 是否也改过该原料？
            b_changed = any((brow or {}).get(fld) != (brrow or {}).get(fld)
                            for fld in ("min_pct", "max_pct", "preferred_cheap"))
            if b_changed:
                conflicts.append({"field": f"materials.{code}.presence", "kind": "modify_delete",
                                  "label": f"原料 {code} 一方删除、另一方修改",
                                  "base": True, "a": False, "b": True})
                continue
            continue  # A 删除、B 未改 → 自动删除
        if in_b2 and not in_a:
            a_changed = any((brow or {}).get(fld) != (arow or {}).get(fld)
                            for fld in ("min_pct", "max_pct", "preferred_cheap"))
            if a_changed:
                conflicts.append({"field": f"materials.{code}.presence", "kind": "modify_delete",
                                  "label": f"原料 {code} 一方修改、另一方删除",
                                  "base": True, "a": True, "b": False})
                continue
            continue
        # 三方都在：逐字段合并
        merged_row = dict(brow)
        for fld in ("min_pct", "max_pct", "preferred_cheap"):
            chosen, st = _three_way(brow.get(fld), arow.get(fld), brrow.get(fld))
            f = f"materials.{code}.{fld}"
            if st == "conflict":
                if f in decisions:
                    chosen = decisions[f]; st = "manual"
                else:
                    conflicts.append({"field": f, "kind": "material_field",
                                      "label": f"{code} {MAT_FIELD_LABELS[fld]}",
                                      "base": brow.get(fld), "a": arow.get(fld),
                                      "b": brrow.get(fld)})
                    continue
            auto.append({"field": f, "label": f"{code} {MAT_FIELD_LABELS[fld]}",
                         "resolution": st, "base": brow.get(fld),
                         "a": arow.get(fld), "b": brrow.get(fld), "chosen": chosen})
            merged_row[fld] = chosen
        # assay/cost 钉住引用：取非空且最新的一方；冲突校验在整体阶段做
        for fld in ("assay_id", "cost_id"):
            chosen, st = _three_way(brow.get(fld), arow.get(fld), brrow.get(fld))
            merged_row[fld] = chosen
        out.append(merged_row)

    # 失效引用硬冲突（对最终入选的每一行）
    for row in out:
        _check_pinned(db, row, row["code"], conflicts)
    return out


def compute_merge(db: Session, sc: models.ScenarioRevision,
                  base_no: int, a_no: int, b_no: int,
                  resolutions: dict | None = None) -> dict:
    resolutions = resolutions or {}
    base = _rev(db, sc, base_no)
    ra = _rev(db, sc, a_no)
    rb = _rev(db, sc, b_no)
    for r, label in ((base, "base"), (ra, "分支 A"), (rb, "分支 B")):
        if r.status == "published" and label != "base":
            raise RevError(422, {"message": f"{label} r{r.revision_no} 是已发布修订，不能作为合并父分支"})
        if r.status == "published" and label == "base":
            continue
    if ra.status not in ("draft", "merged") or rb.status not in ("draft", "merged"):
        raise RevError(422, {"message": "只能合并未发布的分支草稿/合并候选"})
    base_p, pa, pb = (json.loads(x.payload_json) for x in (base, ra, rb))
    auto, conflicts = [], []
    decisions = resolutions

    merged: dict = {"name": base_p["name"], "description": ""}
    for f in SCALAR_LABELS:
        v = _merge_scalar(base_p, pa, pb, f, auto, conflicts, decisions)
        if v is not None:
            merged[f] = v
        elif f in base_p:
            merged[f] = base_p[f]
    # 名称恒用 base（场景名不允许在分支里分叉）
    merged["name"] = base_p["name"]
    merged["description"] = merged.get("description", base_p.get("description", ""))
    merged["rain_overrides"] = _merge_map(
        base_p, pa, pb, "rain_overrides", "雨季含水率", auto, conflicts, decisions,
        "rain_overrides")
    merged["rain_extra_cost"] = _merge_map(
        base_p, pa, pb, "rain_extra_cost", "雨季附加成本", auto, conflicts, decisions,
        "rain_extra_cost")
    merged["materials"] = _merge_materials(db, base_p, pa, pb, auto, conflicts, decisions)

    return {
        "base_no": base_no, "a_no": a_no, "b_no": b_no,
        "a_name": ra.branch_name, "b_name": rb.branch_name,
        "auto": auto, "conflicts": conflicts,
        "candidate_payload": merged,
    }


def create_branch(db: Session, scenario_id: int, source_no: int, branch_name: str,
                  idem_key: str | None) -> dict:
    """从任一已发布修订创建命名分支草稿。"""
    sc = _get_scenario(db, scenario_id)
    _guard_custom(sc)
    name = (branch_name or "").strip()
    if not name:
        raise RevError(422, {"message": "分支名称必填"})
    if len(name) > 64:
        raise RevError(422, {"message": "分支名称不超过 64 字"})
    # 分支名在未发布草稿中唯一
    dup = db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == sc.id,
        models.ScenarioRevision.branch_name == name,
        models.ScenarioRevision.status.in_(["draft", "merged"])))
    if dup is not None:
        raise RevError(409, {"message": f"分支名「{name}」已存在"})
    src = _rev(db, sc, source_no)
    if src.status != "published":
        raise RevError(422, {"message": "只能从已发布修订创建分支"})

    fp = fingerprint({"action": "branch", "scenario": sc.id, "source": source_no,
                      "name": name})
    from .revision_service import replay_idempotent, _idempotency, revision_dto
    if (cached := replay_idempotent(db, idem_key, fp)) is not None:
        return cached
    rev = models.ScenarioRevision(
        scenario_id=sc.id, revision_no=_next_no(db, sc), status="draft",
        kind="branch", branch_name=name,
        payload_json=src.payload_json, lock_version=1,
        created_from_revision_no=source_no)
    db.add(rev); db.flush()
    dto = revision_dto(db, rev)
    _idempotency(db, idem_key, "branch", fp, rev.id, 201, dto)
    db.commit()
    return {"replay": False, **dto}


def merge_branches(db: Session, scenario_id: int, body: dict,
                   idem_key: str | None) -> dict:
    """预览或提交三方合并。

    body: {a, b, base?, attempt_id?, resolutions?}
    - 无冲突且无未决议：单事务生成 status=merged 候选修订 + resolved 审计行；
    - 有冲突：创建/更新 open MergeAttempt（幂等返回同一冲突视图），不生成候选。
    """
    from .revision_service import (revision_dto, replay_idempotent, replay_by_key,
                                   _idempotency)
    sc = _get_scenario(db, scenario_id)
    _guard_custom(sc)
    a_no, b_no = int(body["a"]), int(body["b"])
    if a_no == b_no:
        raise RevError(422, {"message": "合并需要两个不同的分支"})
    ra = _rev(db, sc, a_no)
    rb = _rev(db, sc, b_no)
    base_no = body.get("base")
    if base_no is None:
        # 共同来源：两分支 created_from_revision_no 相同则采用，否则要求显式 base
        if ra.created_from_revision_no == rb.created_from_revision_no and ra.created_from_revision_no:
            base_no = ra.created_from_revision_no
        else:
            raise RevError(422, {"message": "两分支来源不同，必须显式指定共同 base 修订号"})
    base_no = int(base_no)
    resolutions = body.get("resolutions") or {}

    # 幂等：同键直接回放（候选或 open 冲突视图同一结果）
    if idem_key:
        cached = replay_by_key(db, idem_key)
        if cached is not None:
            return cached

    result = compute_merge(db, sc, base_no, a_no, b_no, resolutions)

    attempt = None
    if body.get("attempt_id"):
        attempt = db.get(models.MergeAttempt, int(body["attempt_id"]))
        if attempt is None or attempt.scenario_id != sc.id or attempt.status != "open":
            raise RevError(404, {"message": "合并尝试不存在或已结束"})
        attempt.resolutions_json = json.dumps(resolutions, ensure_ascii=False)

    conflicts = result["conflicts"]
    if conflicts:
        if attempt is None:
            attempt = models.MergeAttempt(
                scenario_id=sc.id, idempotency_key=idem_key,
                status="open", base_no=base_no, parent_a_no=a_no, parent_b_no=b_no,
                result_json=json.dumps(result, ensure_ascii=False),
                resolutions_json=json.dumps(resolutions, ensure_ascii=False))
            db.add(attempt); db.flush()
        else:
            attempt.result_json = json.dumps(result, ensure_ascii=False)
            attempt.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "conflict", "attempt_id": attempt.id,
                "base_no": base_no, "a_no": a_no, "b_no": b_no,
                "auto": result["auto"], "conflicts": conflicts}

    # 无冲突：发布前跑全量校验（钉住引用、区间、最低掺量和等）
    try:
        validate_payload(db, result["candidate_payload"], exclude_id=sc.id)
    except ValueError as e:
        fields = json.loads(str(e))
        if attempt is None:
            attempt = models.MergeAttempt(
                scenario_id=sc.id, idempotency_key=idem_key, status="open",
                base_no=base_no, parent_a_no=a_no, parent_b_no=b_no,
                result_json=json.dumps(result, ensure_ascii=False))
            db.add(attempt); db.flush()
        db.commit()
        return {"status": "conflict", "attempt_id": attempt.id,
                "validation_errors": fields, "conflicts": conflicts}

    # 单事务创建合并候选；任何失败整体回滚，无半合并版本
    cand = models.ScenarioRevision(
        scenario_id=sc.id, revision_no=_next_no(db, sc), status="merged",
        kind="branch", branch_name=f"merge r{a_no}+r{b_no}",
        payload_json=json.dumps(result["candidate_payload"], ensure_ascii=False),
        lock_version=1, created_from_revision_no=base_no,
        merge_base_no=base_no, merge_parent_a_no=a_no, merge_parent_b_no=b_no,
        merge_decisions_json=json.dumps(result["auto"], ensure_ascii=False))
    db.add(cand); db.flush()
    if attempt is not None:
        attempt.status = "resolved"
        attempt.candidate_revision_id = cand.id
        attempt.updated_at = datetime.utcnow()
    out = {"status": "merged", **revision_dto(db, cand),
           "merge": {"base_no": base_no, "a_no": a_no, "b_no": b_no,
                     "auto": result["auto"], "conflicts": []}}
    fp = fingerprint({"a": a_no, "b": b_no, "base": base_no,
                      "resolutions": resolutions})
    _idempotency(db, idem_key, "merge", fp=fp,
                 revision_id=cand.id, status_code=200, response=out)
    db.commit()
    return {"replay": False, **out}


def list_merge_attempts(db: Session, scenario_id: int) -> list[dict]:
    sc = _get_scenario(db, scenario_id)
    rows = db.scalars(select(models.MergeAttempt)
                      .where(models.MergeAttempt.scenario_id == sc.id)
                      .order_by(models.MergeAttempt.id.desc())).all()
    return [{
        "attempt_id": a.id, "status": a.status, "base_no": a.base_no,
        "a_no": a.parent_a_no, "b_no": a.parent_b_no,
        "candidate_revision_id": a.candidate_revision_id,
        "result": json.loads(a.result_json or "{}"),
        "resolutions": json.loads(a.resolutions_json or "{}"),
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    } for a in rows]
