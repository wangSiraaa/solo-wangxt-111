"""FastAPI 入口：原料/化验/成本/场景 CRUD + 试算求解 + 手工试算 + 结果追溯。"""
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload, Session

from . import models, schemas
from .chemistry import ChemistryError, MaterialAnalyses, blend
from .config import settings
from .database import Base, engine, get_db
from .seed import seed
from .service import ANALYTES, run_revision, run_scenario
from .scenario_validation import ensure_schema, validate_scenario, validate_payload
from . import revision_service as revs


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        ensure_schema(db)
        seed(db)
        revs.recover(db)
    yield


app = FastAPI(title="离线生料配比试算（虚构工艺边界）", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health")
def health():
    return {"status": "ok", "notice": "全部数据为虚构，仅供离线研究，不构成生产指令"}


# ---------------- 原料 / 化验版本 / 成本 ----------------

@app.get("/api/materials", response_model=list[schemas.MaterialOut])
def list_materials(db: Session = Depends(get_db)):
    return db.scalars(select(models.Material).order_by(models.Material.id)).all()


@app.post("/api/materials", response_model=schemas.MaterialOut)
def create_material(body: schemas.MaterialIn, db: Session = Depends(get_db)):
    if db.scalar(select(models.Material).where(models.Material.code == body.code)):
        raise HTTPException(409, f"原料编码 {body.code} 已存在")
    m = models.Material(**body.model_dump())
    db.add(m); db.commit(); db.refresh(m)
    return m


@app.get("/api/materials/{code}/assays", response_model=list[schemas.AssayOut])
def list_assays(code: str, db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    return db.scalars(
        select(models.Assay).where(models.Assay.material_id == m.id).order_by(models.Assay.id)
    ).all()


@app.post("/api/materials/{code}/assays", response_model=schemas.AssayOut)
def add_assay(code: str, body: schemas.AssayIn, activate: bool = Query(True),
              db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    a = models.Assay(material_id=m.id, **body.model_dump())
    db.add(a); db.flush()
    if activate:
        m.active_assay_id = a.id
    db.commit(); db.refresh(a)
    return a


@app.post("/api/materials/{code}/assays/{assay_id}/activate", response_model=schemas.AssayOut)
def activate_assay(code: str, assay_id: int, db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    a = db.get(models.Assay, assay_id)
    if not a or a.material_id != m.id:
        raise HTTPException(404, "化验版本不存在")
    m.active_assay_id = a.id
    db.commit(); db.refresh(a)
    return a


@app.get("/api/materials/{code}/costs", response_model=list[schemas.CostOut])
def list_costs(code: str, db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    return db.scalars(
        select(models.Cost).where(models.Cost.material_id == m.id).order_by(models.Cost.id)
    ).all()


@app.post("/api/materials/{code}/costs", response_model=schemas.CostOut)
def add_cost(code: str, body: schemas.CostIn, activate: bool = Query(True),
             db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    c = models.Cost(material_id=m.id, **body.model_dump())
    db.add(c); db.flush()
    if activate:
        m.active_cost_id = c.id
    db.commit(); db.refresh(c)
    return c


@app.get("/api/materials/{code}/availability")
def get_availability(code: str, db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    av = m.availability
    if av is None:
        return {"material_id": m.id, "max_fraction_pct": -1.0, "supply_note": ""}
    return {"material_id": m.id, "max_fraction_pct": av.max_fraction_pct,
            "supply_note": av.supply_note}


@app.put("/api/materials/{code}/availability")
def set_availability(code: str, body: schemas.AvailabilityIn, db: Session = Depends(get_db)):
    m = db.scalar(select(models.Material).where(models.Material.code == code))
    if not m:
        raise HTTPException(404, "原料不存在")
    av = m.availability
    if av is None:
        av = models.Availability(material_id=m.id)
        db.add(av)
    av.max_fraction_pct = body.max_fraction_pct
    av.supply_note = body.supply_note
    db.commit()
    return {"status": "ok"}


# ---------------- 场景 ----------------

def _scenario_dto(s: models.Scenario, db: Session | None = None) -> dict:
    draft = db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == s.id,
        models.ScenarioRevision.status == "draft")) if db is not None else None
    pub = db.get(models.ScenarioRevision, s.published_revision_id) \
        if db is not None and s.published_revision_id else None
    dto = {
        "id": s.id, "name": s.name, "description": s.description,
        "built_in": s.built_in, "created_at": s.created_at.isoformat(),
        "published_revision_no": pub.revision_no if pub else None,
        "draft_revision_no": draft.revision_no if draft else None,
        "draft_revision_id": draft.id if draft else None,
        "draft_lock_version": draft.lock_version if draft else None,
        "targets": {"kh": [s.kh_min, s.kh_max], "sm": [s.sm_min, s.sm_max],
                    "im": [s.im_min, s.im_max]},
        "hazards": {"mgo_max": s.mgo_max, "so3_max": s.so3_max,
                    "alkali_eq_max": s.alkali_eq_max, "cl_max": s.cl_max},
        "denom_floor": s.denom_floor,
        "rain_overrides": json.loads(s.rain_overrides or "{}"),
        "rain_extra_cost": json.loads(s.rain_extra_cost_json or "{}"),
        "materials": [
            {"code": it.material.code, "name": it.material.name,
             "min_pct": it.min_pct, "max_pct": it.max_pct,
             "preferred_cheap": it.preferred_cheap,
             "assay_id": it.material.active_assay_id,
             "cost_id": it.material.active_cost_id}
            for it in s.items],
    }
    return dto


@app.get("/api/scenarios")
def list_scenarios(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(models.Scenario).options(
            selectinload(models.Scenario.items).selectinload(models.ScenarioMaterial.material))
        .order_by(models.Scenario.built_in.desc(), models.Scenario.id)
    ).all()
    return [_scenario_dto(s, db) for s in rows]


def _rev_error(e: revs.RevError) -> HTTPException:
    return HTTPException(e.status_code, e.body)


@app.post("/api/scenarios", status_code=201)
def create_scenario(body: schemas.ScenarioIn, db: Session = Depends(get_db),
                    idempotency_key: str | None = Header(default=None)):
    """建自定义场景 = 建单并发布 rev1（单事务，非法不落半成品）。"""
    try:
        validate_scenario(db, body)
        payload = revs.payload_from_input(db, body)
        out = revs.create_published(db, payload, idempotency_key)
    except ValueError as e:
        raise HTTPException(422, {"message": "场景校验失败", "fields": json.loads(str(e))})
    except revs.RevError as e:
        raise _rev_error(e)
    db.refresh(db.get(models.Scenario, out["scenario"]["id"]))
    return _scenario_dto(db.get(models.Scenario, out["scenario"]["id"]), db)


# ---------------- 修订版：草稿 / 发布 / 回滚 / 时间线 ----------------

def _payload_from_body(body: schemas.ScenarioIn) -> dict:
    """修订编辑入参的材料行可携带 assay_id/cost_id；缺省由当前生效引用填充。"""
    materials = []
    for it in body.materials:
        materials.append({
            "code": it.material_code,
            "assay_id": getattr(it, "assay_id", None),
            "cost_id": getattr(it, "cost_id", None),
            "min_pct": it.min_pct, "max_pct": it.max_pct,
            "preferred_cheap": it.preferred_cheap,
        })
    return {
        "name": body.name.strip(), "description": body.description,
        "kh_min": body.kh_min, "kh_max": body.kh_max,
        "sm_min": body.sm_min, "sm_max": body.sm_max,
        "im_min": body.im_min, "im_max": body.im_max,
        "mgo_max": body.mgo_max, "so3_max": body.so3_max,
        "alkali_eq_max": body.alkali_eq_max, "cl_max": body.cl_max,
        "denom_floor": body.denom_floor,
        "rain_overrides": body.rain_overrides or {},
        "rain_extra_cost": body.rain_extra_cost or {},
        "materials": materials,
    }


def _resolve_pinned(db: Session, payload: dict) -> dict:
    """行内未钉 assay_id/cost_id 时补当前生效引用。"""
    for row in payload["materials"]:
        m = db.scalar(select(models.Material).where(models.Material.code == row["code"]))
        if m is not None:
            row["assay_id"] = row.get("assay_id") or m.active_assay_id
            row["cost_id"] = row.get("cost_id") or m.active_cost_id
    return payload


@app.get("/api/scenarios/{scenario_id}/revisions")
def scenario_revisions(scenario_id: int, db: Session = Depends(get_db)):
    sc = db.get(models.Scenario, scenario_id)
    if sc is None:
        raise HTTPException(404, "场景不存在")
    dtos = revs.list_timeline(db, sc)
    pub_payload = json.loads(
        db.get(models.ScenarioRevision, sc.published_revision_id).payload_json)         if sc.published_revision_id else None
    for d in dtos:
        if d["status"] == "draft" and pub_payload is not None:
            d["diff_from_published"] = revs.diff_payloads(pub_payload, d["payload"])
    return dtos


@app.get("/api/scenarios/{scenario_id}/revisions/{revision_no}")
def get_revision(scenario_id: int, revision_no: int, db: Session = Depends(get_db)):
    sc = db.get(models.Scenario, scenario_id)
    if sc is None:
        raise HTTPException(404, "场景不存在")
    rev = db.scalar(select(models.ScenarioRevision).where(
        models.ScenarioRevision.scenario_id == scenario_id,
        models.ScenarioRevision.revision_no == revision_no))
    if rev is None:
        raise HTTPException(404, "修订版不存在")
    return revs.revision_dto(db, rev)


class DraftIn(schemas.ScenarioIn):
    lock_version: int | None = None
    source_revision_no: int | None = None


@app.put("/api/scenarios/{scenario_id}/draft")
def save_draft(scenario_id: int, body: DraftIn, db: Session = Depends(get_db),
               idempotency_key: str | None = Header(default=None)):
    try:
        payload = _resolve_pinned(db, _payload_from_body(body))
        validate_payload(db, payload, exclude_id=scenario_id)
        out = revs.save_draft(db, scenario_id, payload, body.lock_version,
                              body.source_revision_no, idempotency_key)
    except ValueError as e:
        raise HTTPException(422, {"message": "草稿校验失败", "fields": json.loads(str(e))})
    except revs.RevError as e:
        raise _rev_error(e)
    return out


@app.post("/api/scenarios/{scenario_id}/publish")
def publish(scenario_id: int, body: dict, db: Session = Depends(get_db),
            idempotency_key: str | None = Header(default=None)):
    try:
        out = revs.publish_draft(db, scenario_id, int(body.get("lock_version")),
                                 idempotency_key)
    except ValueError as e:
        raise HTTPException(422, {"message": "发布校验失败", "fields": json.loads(str(e))})
    except revs.RevError as e:
        raise _rev_error(e)
    return out


@app.post("/api/scenarios/{scenario_id}/rollback-draft")
def rollback_draft(scenario_id: int, body: dict, db: Session = Depends(get_db),
                   idempotency_key: str | None = Header(default=None)):
    try:
        sc = db.get(models.Scenario, scenario_id)
        if sc is None:
            raise HTTPException(404, "场景不存在")
        src_no = body.get("source_revision_no")
        if src_no is None and sc.published_revision_id:
            src_no = db.get(models.ScenarioRevision,
                            sc.published_revision_id).revision_no
        out = revs.rollback_as_draft(db, scenario_id, int(src_no), idempotency_key)
    except revs.RevError as e:
        raise _rev_error(e)
    return out


@app.delete("/api/scenarios/{scenario_id}/draft")
def discard_draft(scenario_id: int, db: Session = Depends(get_db)):
    try:
        revs.discard_draft(db, scenario_id)
    except revs.RevError as e:
        raise _rev_error(e)
    return {"status": "discarded"}


@app.put("/api/scenarios/{scenario_id}")
def update_scenario_legacy(scenario_id: int, body: schemas.ScenarioIn,
                           db: Session = Depends(get_db)):
    raise HTTPException(405, {"message": "已发布修订不可直接改写；请创建草稿并发布"})


@app.delete("/api/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int, db: Session = Depends(get_db)):
    sc = db.get(models.Scenario, scenario_id)
    if not sc:
        raise HTTPException(404, "场景不存在")
    if sc.built_in:
        raise HTTPException(403, "内置场景不可删除")
    db.query(models.RevisionRequest).filter(
        models.RevisionRequest.revision_id.in_(
            select(models.ScenarioRevision.id).where(
                models.ScenarioRevision.scenario_id == scenario_id))).delete(synchronize_session=False)
    db.query(models.Solution).filter_by(scenario_id=scenario_id).delete()
    db.query(models.ScenarioMaterial).filter_by(scenario_id=scenario_id).delete()
    db.query(models.ScenarioRevision).filter_by(scenario_id=scenario_id).delete()
    db.delete(sc)
    db.commit()
    return {"status": "deleted", "id": scenario_id}


# ---------------- 求解 ----------------

def _load_scenario(db: Session, scenario_id: int) -> models.Scenario:
    s = db.scalar(
        select(models.Scenario).where(models.Scenario.id == scenario_id)
        .options(selectinload(models.Scenario.items).selectinload(models.ScenarioMaterial.material)
                 .selectinload(models.Material.availability))
    )
    if not s:
        raise HTTPException(404, "场景不存在")
    return s


@app.post("/api/scenarios/{scenario_id}/solve")
def solve_scenario(scenario_id: int, profile: str = Query("base", pattern="^(base|rain)$"),
                   revision_no: int | None = Query(None),
                   db: Session = Depends(get_db)):
    s = _load_scenario(db, scenario_id)
    if revision_no is not None:
        # 重放审计：只能重放已发布（冻结）修订，草稿不可求解
        rev = db.scalar(select(models.ScenarioRevision).where(
            models.ScenarioRevision.scenario_id == scenario_id,
            models.ScenarioRevision.revision_no == revision_no))
        if rev is None:
            raise HTTPException(404, "修订版不存在")
        if rev.status != "published":
            raise HTTPException(409, {"message": "草稿不可求解，请先发布",
                                      "revision_no": rev.revision_no})
    else:
        rev = db.get(models.ScenarioRevision, s.published_revision_id)
        if rev is None:
            raise HTTPException(409, "场景没有已发布修订版，不能求解")
    try:
        result, saved = run_revision(db, s, rev, profile)
    except ChemistryError as e:
        raise HTTPException(422, {"message": str(e), "code": e.code, "details": e.details})
    return {"status": result["status"], "profile": profile,
            "revision_no": rev.revision_no,
            "conflicts": result["conflicts"],
            "solutions": [_solution_dto(sol) for sol in saved]}


def _solution_dto(sol: models.Solution) -> dict:
    return {
        "solution_id": sol.id, "scenario_id": sol.scenario_id,
        "scenario_revision_id": sol.scenario_revision_id,
        "revision_no": sol.revision_no,
        "profile": sol.profile, "mode": sol.mode, "status": sol.status,
        "cost_dry_t": sol.cost_dry_t, "cost_wet_t": sol.cost_wet_t,
        "kh": sol.kh, "sm": sol.sm, "im": sol.im,
        "created_at": sol.created_at.isoformat() if sol.created_at else None,
        "mix": json.loads(sol.mix_json or "{}"),
        "conflicts": json.loads(sol.conflict_json or "[]"),
        "trace": json.loads(sol.trace_json or "{}") if sol.trace_json else {},
    }


@app.get("/api/scenarios/{scenario_id}/solutions")
def list_solutions(scenario_id: int, revision_no: int | None = None,
                   db: Session = Depends(get_db)):
    s = db.get(models.Scenario, scenario_id)
    if s is None:
        raise HTTPException(404, "场景不存在")
    q = select(models.Solution).where(models.Solution.scenario_id == scenario_id)
    if revision_no is not None:
        q = q.where(models.Solution.revision_no == revision_no)
    rows = db.scalars(q.order_by(models.Solution.id.desc())).all()
    return [_solution_dto(r) for r in rows]


@app.get("/api/solutions/{solution_id}")
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    sol = db.get(models.Solution, solution_id)
    if not sol:
        raise HTTPException(404, "结果不存在")
    return _solution_dto(sol)


# ---------------- 手工配比试算（前端滑块直算，不经过优化器） ----------------

class ManualItem(BaseModel):
    code: str
    pct: float               # 干基百分数
    moisture_override: float | None = None
    extra_cost_wet_t: float = 0.0


class ManualRequest(BaseModel):
    items: list[ManualItem]
    denom_floor: float = 0.05


@app.post("/api/manual-blend")
def manual_blend(body: ManualRequest, db: Session = Depends(get_db)):
    if not body.items:
        raise HTTPException(422, {"message": "未提供任何原料", "code": "EMPTY_INPUT"})
    ams, fracs = [], []
    versions = []
    overrides, extras = {}, {}
    for it in body.items:
        m = db.scalar(select(models.Material).where(models.Material.code == it.code))
        if not m:
            raise HTTPException(404, f"原料 {it.code} 不存在")
        if m.active_assay_id is None:
            raise HTTPException(422, {"message": f"原料 {it.code} 没有生效化验版本",
                                      "code": "NO_ASSAY"})
        assay = db.get(models.Assay, m.active_assay_id)
        if m.active_cost_id is None:
            raise HTTPException(422, {"message": f"原料 {it.code} 没有生效成本", "code": "NO_COST"})
        cost = db.get(models.Cost, m.active_cost_id)
        comp = {a: getattr(assay, a) for a in ANALYTES}
        price = cost.price_wet_t + it.extra_cost_wet_t
        ams.append(MaterialAnalyses(code=m.code, name=m.name, composition=comp,
                                    moisture_pct=assay.moisture_pct, price_wet_t=price))
        fracs.append(it.pct / 100.0)
        if it.moisture_override is not None:
            overrides[it.code] = it.moisture_override
        if it.extra_cost_wet_t:
            extras[it.code] = it.extra_cost_wet_t
        versions.append({"material": m.code, "assay_version": assay.version,
                         "assay_id": assay.id, "lab_note": assay.lab_note,
                         "base_moisture_pct": assay.moisture_pct,
                         "cost_id": cost.id})
    try:
        trace = blend(ams, fracs, denom_floor=body.denom_floor,
                      moisture_overrides=overrides or None, extra_cost=extras or None)
    except ChemistryError as e:
        raise HTTPException(422, {"message": str(e), "code": e.code, "details": e.details})
    return {"trace": trace, "provenance": {"assay_versions": versions}}
