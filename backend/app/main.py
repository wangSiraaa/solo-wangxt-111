"""FastAPI 入口：原料/化验/成本/场景 CRUD + 试算求解 + 手工试算 + 结果追溯。"""
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload, Session

from . import models, schemas
from .chemistry import ChemistryError, MaterialAnalyses, blend
from .config import settings
from .database import Base, engine, get_db
from .seed import seed
from .service import ANALYTES, run_scenario


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        seed(db)
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

def _scenario_dto(s: models.Scenario) -> dict:
    return {
        "id": s.id, "name": s.name, "description": s.description,
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
             "preferred_cheap": it.preferred_cheap}
            for it in s.items],
    }


@app.get("/api/scenarios")
def list_scenarios(db: Session = Depends(get_db)):
    rows = db.scalars(
        select(models.Scenario).options(
            selectinload(models.Scenario.items).selectinload(models.ScenarioMaterial.material))
        .order_by(models.Scenario.id)
    ).all()
    return [_scenario_dto(s) for s in rows]


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
                   db: Session = Depends(get_db)):
    s = _load_scenario(db, scenario_id)
    try:
        result, saved = run_scenario(db, s, profile)
    except ChemistryError as e:
        raise HTTPException(422, {"message": str(e), "code": e.code, "details": e.details})
    return {"status": result["status"], "profile": profile,
            "conflicts": result["conflicts"],
            "solutions": [_solution_dto(sol) for sol in saved]}


def _solution_dto(sol: models.Solution) -> dict:
    return {
        "solution_id": sol.id, "mode": sol.mode, "status": sol.status,
        "cost_dry_t": sol.cost_dry_t, "cost_wet_t": sol.cost_wet_t,
        "kh": sol.kh, "sm": sol.sm, "im": sol.im,
        "mix": json.loads(sol.mix_json or "{}"),
        "conflicts": json.loads(sol.conflict_json or "[]"),
        "trace": json.loads(sol.trace_json or "{}") if sol.trace_json else {},
    }


@app.get("/api/scenarios/{scenario_id}/solutions")
def list_solutions(scenario_id: int, db: Session = Depends(get_db)):
    s = db.get(models.Scenario, scenario_id)
    if s is None:
        raise HTTPException(404, "场景不存在")
    rows = db.scalars(
        select(models.Solution).where(models.Solution.scenario_id == scenario_id)
        .order_by(models.Solution.id.desc())
    ).all()
    return [_solution_dto(r) for r in rows]


@app.get("/api/solutions/{solution_id}")
def get_solution(solution_id: int, db: Session = Depends(get_db)):
    sol = db.get(models.Solution, solution_id)
    if not sol:
        raise HTTPException(404, "结果不存在")
    dto = _solution_dto(sol)
    dto["scenario_id"] = sol.scenario_id
    dto["profile"] = sol.profile
    dto["created_at"] = sol.created_at.isoformat()
    return dto


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
