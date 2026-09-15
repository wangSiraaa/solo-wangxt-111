"""SQLAlchemy 模型：检测成分（化验版本）、成本、可用量、场景与求解结果。

所有氧化物成分均以「干基质量百分数」存储；湿基（收到基）只与含水率相关，
通过 moisture_pct 动态换算，换算过程在 optimizer.trace 中留痕。
"""
from datetime import datetime

from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, ForeignKey, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Material(Base):
    __tablename__ = "material"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), default="原料")  # 钙质/硅铝质/铁质/校正料
    # 当前生效的化验版本号
    active_assay_id: Mapped[int | None] = mapped_column(ForeignKey("assay.id", use_alter=True), nullable=True)
    active_cost_id: Mapped[int | None] = mapped_column(ForeignKey("cost.id", use_alter=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assays: Mapped[list["Assay"]] = relationship(foreign_keys="Assay.material_id", back_populates="material")
    costs: Mapped[list["Cost"]] = relationship(foreign_keys="Cost.material_id", back_populates="material")
    availability: Mapped["Availability | None"] = relationship(back_populates="material", uselist=False)


class Assay(Base):
    """一个化验版本。氧化物列：缺失（NULL）与显式 0.0 语义不同——
    NULL 表示缺测，参与配比合成时必须报缺测错误；0.0 表示未检出。"""
    __tablename__ = "assay"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    version: Mapped[str] = mapped_column(String(32))
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lab_note: Mapped[str] = mapped_column(Text, default="")

    # 含水率（湿基）：水占收到基总质量百分数
    moisture_pct: Mapped[float] = mapped_column(Float, default=0.0)

    # 干基化学成分（%，烧失前生料基口径）；允许 None = 缺测
    cao: Mapped[float | None] = mapped_column(Float, nullable=True)
    sio2: Mapped[float | None] = mapped_column(Float, nullable=True)
    al2o3: Mapped[float | None] = mapped_column(Float, nullable=True)
    fe2o3: Mapped[float | None] = mapped_column(Float, nullable=True)
    mgo: Mapped[float | None] = mapped_column(Float, nullable=True)
    so3: Mapped[float | None] = mapped_column(Float, nullable=True)
    k2o: Mapped[float | None] = mapped_column(Float, nullable=True)
    na2o: Mapped[float | None] = mapped_column(Float, nullable=True)
    cl: Mapped[float | None] = mapped_column(Float, nullable=True)
    loi: Mapped[float | None] = mapped_column(Float, nullable=True)  # 烧失量（干基）

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    material: Mapped[Material] = relationship(foreign_keys=[material_id], back_populates="assays")
    __table_args__ = (UniqueConstraint("material_id", "version", name="uq_assay_material_version"),)


class Cost(Base):
    """到厂价，按收到基（湿基）每吨人民币计。干基成本由含水率换算。"""
    __tablename__ = "cost"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    price_wet_t: Mapped[float] = mapped_column(Float)  # 元/吨收到基
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    note: Mapped[str] = mapped_column(Text, default="")

    material: Mapped[Material] = relationship(foreign_keys=[material_id], back_populates="costs")


class Availability(Base):
    """工厂可用量上限（干基占生料质量百分数口径的简化约束，见 optimizer）。
    max_fraction_pct 是该原料在干生料中允许的最高配比；-1 表示不限制。"""
    __tablename__ = "availability"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"), unique=True)
    max_fraction_pct: Mapped[float] = mapped_column(Float, default=-1.0)
    supply_note: Mapped[str] = mapped_column(Text, default="")

    material: Mapped[Material] = relationship(back_populates="availability")


class Scenario(Base):
    """配比试算场景：指标目标、有害上限、参与的原料及最低掺量。"""
    __tablename__ = "scenario"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # True=内置 S1-S4（虚构演示边界），不可修改/删除；False=研发自建场景
    built_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 当前发布修订（草稿不参与求解）；内置场景固定指向 rev1
    published_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenario_revision.id", use_alter=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # 率值目标区间（由已发布修订同步；创建中的瞬时窗口允许为空）
    kh_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    kh_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    sm_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    sm_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    im_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    im_max: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 有害组分上限（干基生料百分数）；碱当量按 Na2O + 0.658*K2O
    mgo_max: Mapped[float] = mapped_column(Float, default=5.0)
    so3_max: Mapped[float] = mapped_column(Float, default=1.5)
    alkali_eq_max: Mapped[float] = mapped_column(Float, default=1.0)
    cl_max: Mapped[float] = mapped_column(Float, default=0.03)

    # 分母保护阈值：小于该值视为分母缺失（不允许静默按零含量算）
    denom_floor: Mapped[float] = mapped_column(Float, default=0.05)

    # 雨季含水率覆盖（JSON: {material_code: moisture_pct}），用于含水率差异演示
    rain_overrides: Mapped[str] = mapped_column(Text, default="{}")
    # 雨季额外成本（元/湿吨，可选，默认 0）
    rain_extra_cost_json: Mapped[str] = mapped_column(Text, default="{}")

    items: Mapped[list["ScenarioMaterial"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )
    revisions: Mapped[list["ScenarioRevision"]] = relationship(
        foreign_keys="ScenarioRevision.scenario_id", back_populates="scenario")


class ScenarioRevision(Base):
    """场景修订版：草稿—发布冻结的不可变快照。

    - 同一自定义场景最多一个 draft；只有 published 修订可求解。
    - payload_json 钉住每个原料的 assay_id/cost_id，切换生效化验不影响旧修订。
    - lock_version 为乐观并发版本；发布与草稿保存都必须携带期望值。
    """
    __tablename__ = "scenario_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft / published
    payload_json: Mapped[str] = mapped_column(Text)
    lock_version: Mapped[int] = mapped_column(Integer, default=1)
    created_from_revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_by: Mapped[str] = mapped_column(String(64), default="研发")

    scenario: Mapped[Scenario] = relationship(
        foreign_keys=[scenario_id], back_populates="revisions")
    __table_args__ = (UniqueConstraint("scenario_id", "revision_no", name="uq_revision_no"),)


class RevisionRequest(Base):
    """幂等请求日志：同一 Idempotency-Key 重复提交返回同一修订结果。"""
    __tablename__ = "revision_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    scope: Mapped[str] = mapped_column(String(32))       # create / save_draft / publish
    revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer)
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ScenarioMaterial(Base):
    __tablename__ = "scenario_material"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("material.id"))
    min_pct: Mapped[float] = mapped_column(Float, default=0.0)   # 最低掺量（干基百分数）
    max_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # 场景级上限，空则用可用量
    preferred_cheap: Mapped[bool] = mapped_column(Boolean, default=False)  # 标记「廉价原料」用于方案三

    scenario: Mapped[Scenario] = relationship(back_populates="items")
    material: Mapped[Material] = relationship()
    __table_args__ = (UniqueConstraint("scenario_id", "material_id", name="uq_sm"),)


class Solution(Base):
    """一次求解结果。trace_json 保存完整可追溯链路：化验版本、干湿基换算、
    质量守恒合成、率值计算、求解状态与冲突诊断。"""
    __tablename__ = "solution"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(ForeignKey("scenario.id"))
    # 绑定求解时使用的已发布修订版；旧解永不随后续编辑/发布改变
    scenario_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("scenario_revision.id"), nullable=True)
    revision_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile: Mapped[str] = mapped_column(String(32), default="base")  # base / rain
    mode: Mapped[str] = mapped_column(String(32))     # min_cost / target_center / max_cheap
    status: Mapped[str] = mapped_column(String(32))   # feasible / infeasible / failed
    cost_dry_t: Mapped[float | None] = mapped_column(Float, nullable=True)  # 元/吨干生料
    cost_wet_t: Mapped[float | None] = mapped_column(Float, nullable=True)
    kh: Mapped[float | None] = mapped_column(Float, nullable=True)
    sm: Mapped[float | None] = mapped_column(Float, nullable=True)
    im: Mapped[float | None] = mapped_column(Float, nullable=True)
    mix_json: Mapped[str] = mapped_column(Text, default="{}")
    trace_json: Mapped[str] = mapped_column(Text, default="{}")
    conflict_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
