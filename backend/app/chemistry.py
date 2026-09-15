"""生料化学核心计算（纯函数，不依赖数据库，便于离线试算与单测）。

口径约定（虚构工艺边界，仅用于研究，不构成生产指令）：
- 所有化验成分均为干基质量百分数；含水率单独存储。
- 配比变量为各原料占「干生料」的质量分数，之和 = 1。
- 率值按波特兰水泥常用近似式：
    硅率 SM = SiO2 / (Al2O3 + Fe2O3)
    铝率 IM = Al2O3 / Fe2O3
    石灰饱和系数 KH = (CaO - 1.65*Al2O3 - 0.35*Fe2O3) / (2.8*SiO2)
- 缺测（化验值为 None）绝不按零含量处理；分母低于保护阈值明确报错。
"""
from dataclasses import dataclass, field
from typing import Mapping, Sequence

# 质量守恒合成时必须参与的氧化物（缺失即报错；不允许默认零含量）
REQUIRED_OXIDES = ("cao", "sio2", "al2o3", "fe2o3", "loi")
# 有害组分：缺失时按 0.0 处理是合理的（未检出），但为可追溯仍在 trace 中记录
HAZARD_OXIDES = ("mgo", "so3", "k2o", "na2o", "cl")
ALL_ANALYTES = REQUIRED_OXIDES + HAZARD_OXIDES

OXIDE_LABELS = {
    "cao": "CaO", "sio2": "SiO₂", "al2o3": "Al₂O₃", "fe2o3": "Fe₂O₃",
    "mgo": "MgO", "so3": "SO₃", "k2o": "K₂O", "na2o": "Na₂O",
    "cl": "Cl⁻", "loi": "LOI",
}


class ChemistryError(Exception):
    """缺测 / 分母为零 / 含水率非法等硬性计算错误。"""

    def __init__(self, message: str, code: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


@dataclass
class MaterialAnalyses:
    code: str
    name: str
    composition: Mapping[str, float | None]  # 干基百分数；None = 缺测
    moisture_pct: float
    price_wet_t: float  # 元/吨收到基（湿吨）

    def dry_factor(self) -> float:
        """1 吨干料对应的收到基吨数。"""
        if not 0.0 <= self.moisture_pct < 100.0:
            raise ChemistryError(
                f"原料 {self.code} 含水率 {self.moisture_pct}% 非法（应在 [0,100) 区间）",
                "BAD_MOISTURE",
                {"material": self.code, "moisture_pct": self.moisture_pct},
            )
        return 1.0 / (1.0 - self.moisture_pct / 100.0)

    def price_dry_t(self) -> float:
        """干基吨成本 = 湿吨价 / 干料占比。"""
        return self.price_wet_t * self.dry_factor()


def check_required(
    materials: Sequence[MaterialAnalyses], moisture_overrides: Mapping[str, float] | None = None
) -> list[dict]:
    """返回缺测项列表 [{material, analyte, label}]，并校验覆盖含水率。"""
    missing: list[dict] = []
    overrides = moisture_overrides or {}
    for m in materials:
        moisture = overrides.get(m.code, m.moisture_pct)
        if not 0.0 <= moisture < 100.0:
            raise ChemistryError(
                f"原料 {m.code} 含水率覆盖值 {moisture}% 非法（应在 [0,100) 区间）",
                "BAD_MOISTURE",
                {"material": m.code, "moisture_pct": moisture},
            )
        for a in REQUIRED_OXIDES:
            if m.composition.get(a) is None:
                missing.append({"material": m.code, "name": m.name, "analyte": a, "label": OXIDE_LABELS[a]})
    return missing


@dataclass
class BlendResult:
    composition: dict[str, float]          # 合成干基成分 %
    composition_ignited: dict[str, float]  # 灼烧基成分 %
    kh: float
    sm: float
    im: float
    alkali_eq: float
    kh_warning: str | None
    denom_checks: list[dict] = field(default_factory=list)


def indicators(comp: Mapping[str, float], denom_floor: float = 0.05) -> dict:
    """计算率值，分母（按百分数绝对量）低于 denom_floor 时明确报错。"""
    sio2, al2o3, fe2o3, cao = comp["sio2"], comp["al2o3"], comp["fe2o3"], comp["cao"]
    checks = [
        {"name": "SM 分母 SiO₂+Al₂O₃+Fe₂O₃ 口径中 Al₂O₃+Fe₂O₃",
         "value": al2o3 + fe2o3, "floor": denom_floor,
         "ok": (al2o3 + fe2o3) >= denom_floor},
        {"name": "IM 分母 Fe₂O₃", "value": fe2o3, "floor": denom_floor,
         "ok": fe2o3 >= denom_floor},
        {"name": "KH 分母 2.8·SiO₂", "value": 2.8 * sio2, "floor": denom_floor,
         "ok": 2.8 * sio2 >= denom_floor},
    ]
    failed = [c for c in checks if not c["ok"]]
    if failed:
        names = "；".join(f"{c['name']}={c['value']:.4f}%" for c in failed)
        raise ChemistryError(
            f"率值分母低于保护阈值 {denom_floor}%，拒绝按零含量静默计算：{names}",
            "ZERO_DENOMINATOR",
            {"checks": checks},
        )
    sm = sio2 / (al2o3 + fe2o3)
    im = al2o3 / fe2o3
    kh = (cao - 1.65 * al2o3 - 0.35 * fe2o3) / (2.8 * sio2)
    return {"kh": kh, "sm": sm, "im": im, "checks": checks}


def blend(
    materials: Sequence[MaterialAnalyses],
    fractions_dry: Sequence[float],
    denom_floor: float = 0.05,
    moisture_overrides: Mapping[str, float] | None = None,
    extra_cost: Mapping[str, float] | None = None,
) -> dict:
    """按质量守恒合成生料并计算全部指标与成本。

    fractions_dry: 各原料在干生料中的质量分数（和应为 1）。
    返回包含合成成分、率值、干湿基换算与成本的完整字典（即 trace 主体）。
    """
    if len(materials) != len(fractions_dry):
        raise ChemistryError("原料数与配比数不一致", "BAD_INPUT")
    missing = check_required(materials, moisture_overrides)
    if missing:
        raise ChemistryError(
            f"存在 {len(missing)} 项缺测，拒绝按零含量参与合成", "MISSING_ANALYTES", {"missing": missing}
        )

    total_w = sum(fractions_dry)
    if abs(total_w - 1.0) > 1e-6:
        raise ChemistryError(
            f"干基配比之和必须等于 1（当前 {total_w:.6f}）", "BAD_FRACTION_SUM",
            {"sum": total_w},
        )

    overrides = moisture_overrides or {}
    extras = extra_cost or {}
    comp = {a: 0.0 for a in ALL_ANALYTES}
    material_rows = []
    cost_dry = 0.0
    wet_mass_total = 0.0  # 生产 1 吨干生料所需各原料收到基吨数之和

    for m, w in zip(materials, fractions_dry):
        if w < -1e-9:
            raise ChemistryError(f"原料 {m.code} 配比为负（{w}）", "NEGATIVE_FRACTION")
        moisture = overrides.get(m.code, m.moisture_pct)
        dry_factor = 1.0 / (1.0 - moisture / 100.0)
        price_wet = m.price_wet_t + extras.get(m.code, 0.0)
        price_dry = price_wet * dry_factor
        wet_mass = w * dry_factor
        contribution = {}
        for a in ALL_ANALYTES:
            v = m.composition.get(a)
            v = 0.0 if v is None else float(v)  # 有害组分缺测按未检出 0，已在 check_required 之外
            comp[a] += w * v
            contribution[a] = w * v
        cost_dry += w * price_dry
        wet_mass_total += wet_mass
        material_rows.append({
            "material": m.code, "name": m.name,
            "fraction_dry_pct": w * 100.0,
            "moisture_pct": moisture,
            "base_moisture_pct": m.moisture_pct,
            "moisture_overridden": m.code in overrides,
            "dry_factor_t_per_t": dry_factor,        # 每吨干料所需收到基吨数
            "wet_mass_t_per_t_dry_blend": wet_mass,
            "fraction_wet_pct": None,                # 汇总后回填
            "price_wet_t": price_wet,
            "extra_cost_wet_t": extras.get(m.code, 0.0),
            "price_dry_t": price_dry,
            "cost_contribution": w * price_dry,      # 元/吨干生料
            "oxide_contribution_pct": contribution,
        })
    for row in material_rows:  # 收到基（湿基）配比口径
        row["fraction_wet_pct"] = 100.0 * row["wet_mass_t_per_t_dry_blend"] / wet_mass_total

    # 灼烧基：扣除 LOI 后等比放大（率值不变，但给出灼烧基口径便于追溯）
    loi = comp["loi"]
    if not 0.0 <= loi < 100.0:
        raise ChemistryError(f"合成烧失量 {loi}% 非法", "BAD_LOI", {"loi": loi})
    scale = 100.0 / (100.0 - loi)
    comp_ignited = {a: comp[a] * scale for a in ALL_ANALYTES if a != "loi"}

    ind = indicators(comp, denom_floor)
    alkali_eq = comp["na2o"] + 0.658 * comp["k2o"]
    kh_warning = None
    if ind["kh"] > 1.0:
        kh_warning = f"KH={ind['kh']:.3f} > 1.0，CaO 超过理论饱和上限，数据或配比需复核"

    return {
        "composition_dry_pct": {a: round(comp[a], 6) for a in ALL_ANALYTES},
        "composition_ignited_pct": {a: round(v, 6) for a, v in comp_ignited.items()},
        "loi_scaling_factor": scale,
        "indicators": {"kh": ind["kh"], "sm": ind["sm"], "im": ind["im"]},
        "alkali_eq_pct": alkali_eq,
        "kh_warning": kh_warning,
        "denominator_checks": ind["checks"],
        "materials": material_rows,
        "cost_dry_t": cost_dry,                 # 元 / 吨干生料
        "cost_wet_equiv_t": cost_dry / wet_mass_total,  # 折合元 / 吨收到基混合料
        "total_wet_mass_t_per_t_dry": wet_mass_total,
        "mass_balance": {
            "basis": "干生料 1000 kg",
            "dry_input_kg": 1000.0,
            "wet_input_kg": 1000.0 * wet_mass_total,
            "water_kg": 1000.0 * (wet_mass_total - 1.0),
            "ignited_kg": 1000.0 * (1.0 - loi / 100.0),
        },
    }
