"""Pydantic 入参/出参模型。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AssayIn(BaseModel):
    version: str
    sampled_at: Optional[datetime] = None
    lab_note: str = ""
    moisture_pct: float = 0.0
    cao: Optional[float] = None
    sio2: Optional[float] = None
    al2o3: Optional[float] = None
    fe2o3: Optional[float] = None
    mgo: Optional[float] = None
    so3: Optional[float] = None
    k2o: Optional[float] = None
    na2o: Optional[float] = None
    cl: Optional[float] = None
    loi: Optional[float] = None


class AssayOut(AssayIn):
    id: int
    material_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CostIn(BaseModel):
    price_wet_t: float = Field(ge=0)
    note: str = ""


class CostOut(CostIn):
    id: int
    material_id: int
    effective_from: datetime

    class Config:
        from_attributes = True


class AvailabilityIn(BaseModel):
    max_fraction_pct: float = -1.0
    supply_note: str = ""


class MaterialIn(BaseModel):
    code: str
    name: str
    category: str = "原料"


class MaterialOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    active_assay_id: Optional[int] = None
    active_cost_id: Optional[int] = None

    class Config:
        from_attributes = True


class ScenarioMaterialIn(BaseModel):
    material_code: str
    min_pct: float = 0.0
    max_pct: Optional[float] = None
    preferred_cheap: bool = False
    # 修订草稿可显式钉住引用；建单时留空由后端填当前生效版本
    assay_id: Optional[int] = None
    cost_id: Optional[int] = None


class ScenarioIn(BaseModel):
    name: str
    description: str = ""
    kh_min: float = 0.86
    kh_max: float = 0.96
    sm_min: float = 2.3
    sm_max: float = 2.8
    im_min: float = 1.2
    im_max: float = 1.8
    mgo_max: float = 5.0
    so3_max: float = 1.5
    alkali_eq_max: float = 1.0
    cl_max: float = 0.03
    denom_floor: float = 0.05
    rain_overrides: dict[str, float] = {}
    rain_extra_cost: dict[str, float] = {}
    materials: list[ScenarioMaterialIn]


class ScenarioOut(ScenarioIn):
    id: int
    active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class SolutionOut(BaseModel):
    id: int
    scenario_id: int
    profile: str
    mode: str
    status: str
    cost_dry_t: Optional[float] = None
    cost_wet_t: Optional[float] = None
    kh: Optional[float] = None
    sm: Optional[float] = None
    im: Optional[float] = None
    mix_json: str
    trace_json: str
    conflict_json: str
    created_at: datetime

    class Config:
        from_attributes = True
