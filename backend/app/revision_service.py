"""场景修订版：草稿—发布冻结—重放审计。

不变量：
1. 只有 published 修订可求解；草稿/历史已发布修订永久可读；
2. 每场景至多一个 draft（DB 部分唯一索引 + 服务层检查）；
3. 草稿保存/发布携带 lock_version 乐观并发，UPDATE 带版本条件，两个浏览器只有一个成功；
4. Idempotency-Key 重复提交返回同一修订结果；
5. 发布 = 单事务（更新修订状态 + 同步场景快照 + 写幂等日志），失败/重启不留半发布；
6. payload 钉住 assay_id/cost_id，切换生效化验不影响旧修订与旧解。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import models

SCALAR_FIELDS = (
    "kh_min", "kh_max", "sm_min", "sm_max", "im_min", "im_max",
    "mgo_max", "so3_max", "alkali_eq_max", "cl_max", "denom_floor",
)


class RevError(Exception):
    def __init__(self, status_code: int, body: dict):
        super().__init__(body.get("message", "revision error"))
        self.status_code = status_code
        self.body = body


# ---------------- payload ----------------

def _materials_snapshot(db: Session, sc: models.Scenario) -> list[dict]:
    rows = []
    for it in sc.items:
        m = it.material
        rows.append({
            "code": m.code,
            "assay_id": m.active_assay_id,
            "cost_id": m.active_cost_id,
            "min_pct": it.min_pct,
            "max_pct": it.max_pct,
            "preferred_cheap": it.preferred_cheap,
        })
    return rows


def payload_from_scenario(db: Session, sc: models.Scenario) -> dict:
    return {
        "name": sc.name, "description": sc.description,
        **{f: getattr(sc, f) for f in SCALAR_FIELDS},
        "rain_overrides": json.loads(sc.rain_overrides or "{}"),
        "rain_extra_cost": json.loads(sc.rain_extra_cost_json or "{}"),
        "materials": _materials_snapshot(db, sc),
    }


def payload_from_input(db: Session, body) -> dict:
    """从建单入参生成 payload，并钉住每个原料当前生效化验/成本 ID。"""
    materials = []
    for it in body.materials:
        m = db.scalar(select(models.Material).where(models.Material.code == it.material_code))
        materials.append({
            "code": it.material_code,
            "assay_id": m.active_assay_id if m else None,
            "cost_id": m.active_cost_id if m else None,
            "min_pct": it.min_pct,
            "max_pct": it.max_pct,
            "preferred_cheap": it.preferred_cheap,
        })
    return {
        "name": body.name.strip(), "description": body.description,
        **{f: getattr(body, f) for f in SCALAR_FIELDS},
        "rain_overrides": body.rain_overrides or {},
        "rain_extra_cost": body.rain_extra_cost or {},
        "materials": materials,
    }


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# ---------------- DTO / timeline / diff ----------------

def revision_dto(db: Session, rev: models.ScenarioRevision) -> dict:
    p = json.loads(rev.payload_json)
    sol_count = db.scalar(
        select(func.count()).select_from(models.Solution)
        .where(models.Solution.scenario_revision_id == rev.id)) or 0
    return {
        "revision_id": rev.id,
        "scenario_id": rev.scenario_id,
        "revision_no": rev.revision_no,
        "status": rev.status,
        "lock_version": rev.lock_version,
        "created_from_revision_no": rev.created_from_revision_no,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
        "published_at": rev.published_at.isoformat() if rev.published_at else None,
        "solutions": sol_count,
        "payload": p,
    }


def list_timeline(db: Session, sc: models.Scenario) -> list[dict]:
    revs = db.scalars(
        select(models.ScenarioRevision)
        .where(models.ScenarioRevision.scenario_id == sc.id)
        .order_by(models.ScenarioRevision.revision_no.desc())
    ).all()
    dtos = [revision_dto(db, r) for r in revs]
    for i in range(len(dtos) - 1):
        dtos[i]["diff_from_published"] = None  # 由路由按需补充
    return dtos


def diff_payloads(old: dict, new: dict) -> dict:
    """结构差异摘要：标量边界、雨季配置、原料增删与掺量变化。"""
    changes: list[dict] = []
    labels = {
        "kh_min": "KH 下限", "kh_max": "KH 上限", "sm_min": "SM 下限", "sm_max": "SM 上限",
        "im_min": "IM 下限", "im_max": "IM 上限", "mgo_max": "MgO 上限",
        "so3_max": "SO₃ 上限", "alkali_eq_max": "碱当量上限", "cl_max": "Cl⁻ 上限",
        "denom_floor": "分母地板", "description": "说明",
    }
    for f, label in labels.items():
        if old.get(f) != new.get(f):
            changes.append({"field": f, "label": label, "old": old.get(f), "new": new.get(f)})
    for f, label in (("rain_overrides", "雨季含水率覆盖"), ("rain_extra_cost", "雨季附加成本")):
        o, n = old.get(f, {}) or {}, new.get(f, {}) or {}
        for code in sorted(set(o) | set(n)):
            if o.get(code) != n.get(code):
                changes.append({"field": f"{f}.{code}", "label": f"{label} {code}",
                                "old": o.get(code), "new": n.get(code)})
    om = {r["code"]: r for r in old.get("materials", [])}
    nm = {r["code"]: r for r in new.get("materials", [])}
    for code in sorted(set(om) | set(nm)):
        if code not in om:
            changes.append({"field": f"materials.{code}", "label": f"新增原料 {code}",
                            "old": None, "new": f"最低 {nm[code]['min_pct']}%"})
        elif code not in nm:
            changes.append({"field": f"materials.{code}", "label": f"移除原料 {code}",
                            "old": f"最低 {om[code]['min_pct']}%", "new": None})
        else:
            for f, label in (("min_pct", "最低掺量"), ("max_pct", "场景上限"),
                             ("preferred_cheap", "廉价标记"),
                             ("assay_id", "化验版本"), ("cost_id", "成本版本")):
                if om[code].get(f) != nm[code].get(f):
                    changes.append({"field": f"materials.{code}.{f}",
                                    "label": f"{code} {label}",
                                    "old": om[code].get(f), "new": nm[code].get(f)})
    return {"changes": changes, "change_count": len(changes)}


# ---------------- 内部工具 ----------------

def _get_scenario(db: Session, scenario_id: int) -> models.Scenario:
    sc = db.get(models.Scenario, scenario_id)
    if sc is None:
        raise RevError(404, {"message": "场景不存在"})
    return sc


def _guard_custom(sc: models.Scenario) -> None:
    if sc.built_in:
        raise RevError(403, {"message": "内置场景不可进入修订流程"})


def _draft(db: Session, sc: models.Scenario) -> models.ScenarioRevision | None:
    return db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == sc.id,
        models.ScenarioRevision.status == "draft"))


def _next_no(db: Session, sc: models.Scenario) -> int:
    n = db.scalar(select(func.coalesce(func.max(models.ScenarioRevision.revision_no), 0))
                  .where(models.ScenarioRevision.scenario_id == sc.id))
    return int(n) + 1


def _idempotency(db: Session, key: str | None, scope: str, fp: str,
                 revision_id: int | None, status_code: int, response: dict) -> None:
    """登记一次成功的幂等请求（与业务改动同一事务提交）。"""
    if not key:
        return
    db.add(models.RevisionRequest(
        idempotency_key=key, scope=scope, revision_id=revision_id, fingerprint=fp,
        status_code=status_code, response_json=json.dumps(response, ensure_ascii=False)))


def replay_idempotent(db: Session, key: str | None, fp: str) -> dict | None:
    """命中幂等键：同指纹返回原结果；不同指纹报 409。未命中返回 None。"""
    if not key:
        return None
    log = db.scalar(select(models.RevisionRequest)
                    .where(models.RevisionRequest.idempotency_key == key))
    if log is None:
        return None
    if log.fingerprint != fp:
        raise RevError(409, {"message": "幂等键已用于不同的请求内容，拒绝重复提交",
                             "revision_id": log.revision_id})
    return {"replay": True, **json.loads(log.response_json)}


def replay_by_key(db: Session, key: str | None) -> dict | None:
    """只按键回放（用于原资源状态已被首个成功请求改变，无法重算指纹的场景，
    如发布后草稿已消失）。"""
    if not key:
        return None
    log = db.scalar(select(models.RevisionRequest)
                    .where(models.RevisionRequest.idempotency_key == key))
    if log is None:
        return None
    return {"replay": True, **json.loads(log.response_json)}


# ---------------- 场景 + rev1（建单即发布） ----------------

def create_published(db: Session, payload: dict, idem_key: str | None) -> dict:
    fp = fingerprint(payload)
    if (cached := replay_idempotent(db, idem_key, fp)) is not None:
        return cached
    sc = models.Scenario(built_in=False, active=True)
    db.add(sc); db.flush()
    rev = models.ScenarioRevision(
        scenario_id=sc.id, revision_no=1, status="published",
        payload_json=json.dumps(payload, ensure_ascii=False), lock_version=1,
        published_at=datetime.utcnow(), created_from_revision_no=None)
    db.add(rev); db.flush()
    _apply_to_scenario(db, sc, rev, payload)
    dto = revision_dto(db, rev)
    dto["scenario"] = scenario_brief(db, sc)
    _idempotency(db, idem_key, "create", fp, rev.id, 201, dto)
    db.commit(); db.refresh(rev)
    return {"replay": False, **revision_dto(db, rev),
            "scenario": scenario_brief(db, sc)}


def _apply_to_scenario(db: Session, sc: models.Scenario,
                       rev: models.ScenarioRevision, p: dict) -> None:
    """把已发布 payload 同步为场景当前快照（求解与列表使用它）。"""
    sc.name = p["name"]
    sc.description = p.get("description", "")
    for f in SCALAR_FIELDS:
        setattr(sc, f, p[f])
    sc.rain_overrides = json.dumps(p.get("rain_overrides", {}), ensure_ascii=False)
    sc.rain_extra_cost_json = json.dumps(p.get("rain_extra_cost", {}), ensure_ascii=False)
    sc.published_revision_id = rev.id
    db.query(models.ScenarioMaterial).filter_by(scenario_id=sc.id).delete()
    db.flush()
    for row in p["materials"]:
        m = db.scalar(select(models.Material).where(models.Material.code == row["code"]))
        db.add(models.ScenarioMaterial(
            scenario_id=sc.id, material_id=m.id,
            min_pct=row["min_pct"], max_pct=row["max_pct"],
            preferred_cheap=row["preferred_cheap"]))


def scenario_brief(db: Session, sc: models.Scenario) -> dict:
    pub = sc.published_revision_id and db.get(models.ScenarioRevision,
                                              sc.published_revision_id)
    d = db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == sc.id,
        models.ScenarioRevision.status == "draft"))
    return {
        "id": sc.id, "name": sc.name, "built_in": sc.built_in,
        "published_revision_no": pub.revision_no if pub else None,
        "published_revision_id": sc.published_revision_id,
        "draft_revision_no": d.revision_no if d else None,
        "draft_revision_id": d.id if d else None,
        "draft_lock_version": d.lock_version if d else None,
    }


# ---------------- 草稿 ----------------

def save_draft(db: Session, scenario_id: int, payload: dict, expected_lock: int | None,
               source_revision_no: int | None, idem_key: str | None) -> dict:
    sc = _get_scenario(db, scenario_id)
    _guard_custom(sc)
    fp = fingerprint(payload)
    if (cached := replay_idempotent(db, idem_key, fp)) is not None:
        return cached

    draft = _draft(db, sc)
    if draft is not None:
        # 更新既有草稿：乐观并发，两个浏览器只有一个成功
        if expected_lock is None:
            raise RevError(428, {"message": "必须携带草稿的 lock_version"})
        result = db.execute(
            update(models.ScenarioRevision)
            .where(models.ScenarioRevision.id == draft.id,
                   models.ScenarioRevision.lock_version == expected_lock,
                   models.ScenarioRevision.status == "draft")
            .values(payload_json=json.dumps(payload, ensure_ascii=False),
                    lock_version=models.ScenarioRevision.lock_version + 1))
        if result.rowcount != 1:
            db.rollback()
            raise RevError(409, {"message": "草稿已被其他会话修改，请刷新后基于最新版本编辑",
                                 "current_lock_version": draft.lock_version})
        db.refresh(draft)
        rev = draft
        created = False
    else:
        if expected_lock is not None:
            raise RevError(409, {"message": "草稿不存在或已被发布，乐观版本失效"})
        src_no = source_revision_no
        if src_no is not None:
            src = db.scalar(select(models.ScenarioRevision).where(
                models.ScenarioRevision.scenario_id == sc.id,
                models.ScenarioRevision.revision_no == src_no))
            if src is None:
                raise RevError(404, {"message": f"源修订版 r{src_no} 不存在"})
        rev = models.ScenarioRevision(
            scenario_id=sc.id, revision_no=_next_no(db, sc), status="draft",
            payload_json=json.dumps(payload, ensure_ascii=False), lock_version=1,
            created_from_revision_no=src_no)
        db.add(rev); db.flush()
        created = True

    dto = revision_dto(db, rev)
    _idempotency(db, idem_key, "save_draft", fp, rev.id, 200, dto)
    db.commit()
    return {"replay": False, "created": created, **revision_dto(db, rev)}


def publish_draft(db: Session, scenario_id: int, expected_lock: int,
                  idem_key: str | None) -> dict:
    sc = _get_scenario(db, scenario_id)
    _guard_custom(sc)
    draft = _draft(db, sc)
    if draft is None:
        # 首请求已成功发布并消除草稿：按幂等键直接回放同一修订结果
        cached = replay_by_key(db, idem_key)
        if cached is not None:
            return cached
        raise RevError(404, {"message": "该场景没有待发布的草稿"})
    payload = json.loads(draft.payload_json)
    # 幂等键按草稿内容绑定（不含 lock_version：成功发布后 lock 会自增，
    # 重试仍应视为同一发布请求）；同键不同草稿内容才判为冲突
    fp = fingerprint({**payload, "_action": "publish"})
    if (cached := replay_idempotent(db, idem_key, fp)) is not None:
        return cached

    # 发布前再次执行与建单完全相同的全量校验（钉住的引用必须仍有效）
    if payload is None:
        raise RevError(404, {"message": "该场景没有待发布的草稿"})
    from .scenario_validation import validate_payload
    try:
        validate_payload(db, payload, exclude_id=sc.id)
    except ValueError as e:
        raise RevError(422, {"message": "发布校验失败", "fields": json.loads(str(e))})

    # 单事务原子发布：状态翻转 + 场景快照同步，任一失败整体回滚
    result = db.execute(
        update(models.ScenarioRevision)
        .where(models.ScenarioRevision.id == draft.id,
               models.ScenarioRevision.lock_version == expected_lock,
               models.ScenarioRevision.status == "draft")
        .values(status="published", lock_version=models.ScenarioRevision.lock_version + 1,
                published_at=datetime.utcnow()))
    if result.rowcount != 1:
        db.rollback()
        raise RevError(409, {"message": "发布冲突：草稿已被其他会话修改或发布",
                             "current_lock_version": draft.lock_version})
    db.refresh(draft)
    _apply_to_scenario(db, sc, draft, payload)
    dto = revision_dto(db, draft)
    _idempotency(db, idem_key, "publish", fp, draft.id, 200, dto)
    db.commit()
    return {"replay": False, **revision_dto(db, draft)}


def rollback_as_draft(db: Session, scenario_id: int, source_revision_no: int,
                      idem_key: str | None) -> dict:
    """把指定旧发布版本（默认当前发布版）复制为新草稿，审计链完整保留。"""
    sc = _get_scenario(db, scenario_id)
    _guard_custom(sc)
    if _draft(db, sc) is not None:
        raise RevError(409, {"message": "已有未发布草稿，请先发布或放弃"})
    src = db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == sc.id,
        models.ScenarioRevision.revision_no == source_revision_no))
    if src is None or src.status != "published":
        raise RevError(404, {"message": f"已发布修订版 r{source_revision_no} 不存在"})
    payload = json.loads(src.payload_json)
    fp = fingerprint({**payload, "_action": "rollback", "_src": source_revision_no})
    if (cached := replay_idempotent(db, idem_key, fp)) is not None:
        return cached
    rev = models.ScenarioRevision(
        scenario_id=sc.id, revision_no=_next_no(db, sc), status="draft",
        payload_json=src.payload_json, lock_version=1,
        created_from_revision_no=source_revision_no)
    db.add(rev); db.flush()
    dto = revision_dto(db, rev)
    _idempotency(db, idem_key, "rollback", fp, rev.id, 200, dto)
    db.commit()
    return {"replay": False, **dto}


def discard_draft(db: Session, scenario_id: int) -> None:
    sc = _get_scenario(db, scenario_id)
    _guard_custom(sc)
    draft = _draft(db, sc)
    if draft is None:
        raise RevError(404, {"message": "该场景没有草稿"})
    db.delete(draft)
    db.commit()


# ---------------- 启动恢复 ----------------

def recover(db: Session) -> list[str]:
    """修复任何半发布/孤儿指针；为升级前的旧场景回填 rev1。幂等。"""
    notes = []
    for sc in db.scalars(select(models.Scenario)).all():
        revs = db.scalars(select(models.ScenarioRevision)
                          .where(models.ScenarioRevision.scenario_id == sc.id)
                          .order_by(models.ScenarioRevision.revision_no)).all()
        if not revs:
            # 升级前创建的场景：用当前场景快照补一个已发布 rev1
            payload = payload_from_scenario(db, sc)
            rev = models.ScenarioRevision(
                scenario_id=sc.id, revision_no=1, status="published",
                payload_json=json.dumps(payload, ensure_ascii=False), lock_version=1,
                published_at=sc.created_at or datetime.utcnow())
            db.add(rev); db.flush()
            sc.published_revision_id = rev.id
            notes.append(f"场景 {sc.id} 无修订记录，已回填发布版 r1")
            continue
        # 不存在“发布中”中间态（发布为单事务）；这里只修复指针与内容不一致
        latest_pub = [r for r in revs if r.status == "published"]
        if sc.published_revision_id is not None:
            ptr = next((r for r in revs if r.id == sc.published_revision_id), None)
            if ptr is None or ptr.status != "published":
                good = latest_pub[-1]
                sc.published_revision_id = good.id
                _apply_to_scenario(db, sc, good, json.loads(good.payload_json))
                notes.append(f"场景 {sc.id} 发布指针损坏，已恢复到 r{good.revision_no}")
        elif latest_pub:
            good = latest_pub[-1]
            sc.published_revision_id = good.id
            _apply_to_scenario(db, sc, good, json.loads(good.payload_json))
            notes.append(f"场景 {sc.id} 缺发布指针，已补指 r{good.revision_no}")
    db.commit()
    return notes
