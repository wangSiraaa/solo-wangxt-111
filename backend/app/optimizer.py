"""SciPy 配比优化器（纯计算层）。

流程严格按业务要求：
1) 质量守恒：x_i 为各原料占干生料质量分数，sum x = 1，成分 = Σ x_i·c_i；
2) 线性化率值约束（KH/SM/IM 均为线性分式，分母由地板约束保证严格为正）；
3) SLSQP 在三种偏好下求可行解（最低成本 / 指标居中 / 廉价原料最大化）；
4) 不可行时用 HiGHS linprog 做逐项删除诊断，指出冲突项与可达范围。
"""
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog, minimize

from .chemistry import ChemistryError, blend

FLOOR_TOL = 1e-9


@dataclass
class OptMaterial:
    code: str
    name: str
    composition: dict   # 干基百分数；None = 缺测（绝不静默按 0 处理）
    moisture_pct: float
    price_wet_t: float
    min_frac: float = 0.0
    max_frac: float = 1.0
    cheap: bool = False

    def assert_complete(self):
        from .chemistry import REQUIRED_OXIDES
        missing = [a for a in REQUIRED_OXIDES if self.composition.get(a) is None]
        if missing:
            raise ChemistryError(
                f"原料 {self.code} 存在缺测项 {missing}，拒绝默认按零含量参与优化",
                "MISSING_ANALYTES",
                {"material": self.code, "missing": missing},
            )

    @property
    def price_dry_t(self) -> float:
        return self.price_wet_t / (1.0 - self.moisture_pct / 100.0)


@dataclass
class OptTargets:
    kh_min: float; kh_max: float
    sm_min: float; sm_max: float
    im_min: float; im_max: float
    mgo_max: float = 5.0
    so3_max: float = 1.5
    alkali_eq_max: float = 1.0
    cl_max: float = 0.03
    denom_floor: float = 0.05


@dataclass
class LinearConstraint:
    name: str
    coef: np.ndarray
    rhs: float
    sense: str          # "ge" 或 "le"
    group: str          # indicator / hazard / floor / bound

    def ub_row(self):
        """统一成 linprog 形式 coef·x <= rhs。"""
        if self.sense == "le":
            return self.coef, self.rhs
        return -self.coef, -self.rhs

    def residual(self, x):
        v = float(self.coef @ x)
        return v - self.rhs if self.sense == "ge" else self.rhs - v


def _vec(ms: list[OptMaterial], key) -> np.ndarray:
    return np.array([(m.composition.get(key) or 0.0) for m in ms], dtype=float)


def build_constraints(ms: list[OptMaterial], t: OptTargets) -> list[LinearConstraint]:
    n = len(ms)
    cao, sio2 = _vec(ms, "cao"), _vec(ms, "sio2")
    al, fe = _vec(ms, "al2o3"), _vec(ms, "fe2o3")
    cons: list[LinearConstraint] = []

    # 率值（线性分式 → 线性齐次约束）
    kh_n = cao - 1.65 * al - 0.35 * fe
    cons += [
        LinearConstraint(f"KH ≥ {t.kh_min:.3f}", kh_n - 2.8 * t.kh_min * sio2, 0.0, "ge", "indicator"),
        LinearConstraint(f"KH ≤ {t.kh_max:.3f}", kh_n - 2.8 * t.kh_max * sio2, 0.0, "le", "indicator"),
        LinearConstraint(f"SM ≥ {t.sm_min:.2f}", sio2 - t.sm_min * (al + fe), 0.0, "ge", "indicator"),
        LinearConstraint(f"SM ≤ {t.sm_max:.2f}", sio2 - t.sm_max * (al + fe), 0.0, "le", "indicator"),
        LinearConstraint(f"IM ≥ {t.im_min:.2f}", al - t.im_min * fe, 0.0, "ge", "indicator"),
        LinearConstraint(f"IM ≤ {t.im_max:.2f}", al - t.im_max * fe, 0.0, "le", "indicator"),
    ]
    # 有害组分（碱当量 Na2O + 0.658 K2O）
    cons += [
        LinearConstraint(f"MgO ≤ {t.mgo_max}%", _vec(ms, "mgo"), t.mgo_max, "le", "hazard"),
        LinearConstraint(f"SO₃ ≤ {t.so3_max}%", _vec(ms, "so3"), t.so3_max, "le", "hazard"),
        LinearConstraint("碱当量 Na₂O+0.658K₂O ≤ "
                         f"{t.alkali_eq_max}%", _vec(ms, "na2o") + 0.658 * _vec(ms, "k2o"),
                         t.alkali_eq_max, "le", "hazard"),
        LinearConstraint(f"Cl⁻ ≤ {t.cl_max}%", _vec(ms, "cl"), t.cl_max, "le", "hazard"),
    ]
    # 分母保护：杜绝分母为零，且不允许用零含量原料凑解
    cons += [
        LinearConstraint(f"SM 分母 Al₂O₃+Fe₂O₃ ≥ {t.denom_floor}%", al + fe, t.denom_floor, "ge", "floor"),
        LinearConstraint(f"IM 分母 Fe₂O₃ ≥ {t.denom_floor}%", fe, t.denom_floor, "ge", "floor"),
        LinearConstraint(f"KH 分母 2.8·SiO₂ ≥ {t.denom_floor}%", 2.8 * sio2, t.denom_floor, "ge", "floor"),
    ]
    return cons


def _bounds(ms):
    return [(m.min_frac, m.max_frac) for m in ms]


def lp_feasible(ms, cons, drop: set[int] | None = None):
    """HiGHS 线性可行性判定。drop 指定删除的约束下标集合。"""
    n = len(ms)
    drop = drop or set()
    A, b = [], []
    for i, c in enumerate(cons):
        if i in drop:
            continue
        row, rhs = c.ub_row()
        A.append(row); b.append(rhs)
    res = linprog(
        c=np.zeros(n),
        A_ub=np.array(A) if A else None, b_ub=np.array(b) if b else None,
        A_eq=np.ones((1, n)), b_eq=np.array([1.0]),
        bounds=_bounds(ms), method="highs",
    )
    return res.success, (res.x if res.success else None)


def _indicator_value(x, ms, key):
    if key == "kh":
        num = _vec(ms, "cao") - 1.65 * _vec(ms, "al2o3") - 0.35 * _vec(ms, "fe2o3")
        return float((num @ x) / (2.8 * (_vec(ms, "sio2") @ x)))
    if key == "sm":
        d = _vec(ms, "al2o3") + _vec(ms, "fe2o3")
        return float((_vec(ms, "sio2") @ x) / (d @ x))
    return float((_vec(ms, "al2o3") @ x) / (_vec(ms, "fe2o3") @ x))


def _achievable_range(ms, cons, key, structural_groups=("floor",)):
    """二分搜索率值在「结构约束（配比界、分母地板）」下的可达区间。"""
    probe = LinearConstraint("probe", np.zeros(len(ms)), 0.0, "ge", "probe")

    def feasible_at(level):
        if key == "kh":
            num = _vec(ms, "cao") - 1.65 * _vec(ms, "al2o3") - 0.35 * _vec(ms, "fe2o3")
            probe.coef = num - 2.8 * level * _vec(ms, "sio2")
        elif key == "sm":
            probe.coef = _vec(ms, "sio2") - level * (_vec(ms, "al2o3") + _vec(ms, "fe2o3"))
        else:
            probe.coef = _vec(ms, "al2o3") - level * _vec(ms, "fe2o3")
        saved = cons
        full = saved + [probe]
        ok, _ = lp_feasible(ms, full, drop={i for i, c in enumerate(saved)
                                            if c.group == "indicator"})
        return ok

    lo, hi = -10.0, 50.0
    if not feasible_at(lo) or not feasible_at(hi):
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if feasible_at(mid):
            lo = mid
        else:
            hi = mid
    upper = lo
    lo, hi = -10.0, 50.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if feasible_at(mid):
            hi = mid
        else:
            lo = mid
    lower = hi
    return round(lower, 4), round(upper, 4)


def _elastic_minimum_violation(ms, cons):
    """弹性规划：min Σ slack_i（s_i≥0），在所有约束可被松弛下求最小总违约。

    返回 (x, 各约束 slack, 各约束在 x 处的原值)。始终有界可行，
    slack=0 表示该约束在「最小违约点」已满足；>0 的约束即为冲突承担项。
    """
    n, k = len(ms), len(cons)
    # 变量 [x_0..x_{n-1}, s_0..s_{k-1}]
    c_obj = np.concatenate([np.zeros(n), np.ones(k)])
    A = np.zeros((k, n + k))
    b = np.zeros(k)
    for i, con in enumerate(cons):
        row, rhs = con.ub_row()
        A[i, :n] = row
        A[i, n + i] = -1.0   # row·x - s_i <= rhs
        b[i] = rhs
    bounds = [(m.min_frac, m.max_frac) for m in ms] + [(0.0, None)] * k
    res = linprog(c_obj, A_ub=A, b_ub=b,
                  A_eq=np.concatenate([np.ones((1, n)), np.zeros((1, k))], axis=1),
                  b_eq=np.array([1.0]), bounds=bounds, method="highs")
    if not res.success:
        return None, None, None
    x = res.x[:n]
    slacks = res.x[n:]
    values = np.array([float(con.coef @ x) for con in cons])
    return x, slacks, values


def _violation_text(con: LinearConstraint, actual: float, indicator_value: float | None = None,
                    bound: float | None = None) -> str:
    if indicator_value is not None and bound is not None:
        if con.sense == "le":
            return f"实际 {indicator_value:.3f}，上限 {bound:.3f}，超 {indicator_value - bound:.3f}"
        return f"实际 {indicator_value:.3f}，下限 {bound:.3f}，差 {bound - indicator_value:.3f}"
    if con.sense == "le":
        return f"实际 {actual:.4f}，上限 {con.rhs:.4f}，超 {actual - con.rhs:.4f}"
    return f"实际 {actual:.4f}，下限 {con.rhs:.4f}，差 {con.rhs - actual:.4f}"


def _indicator_at(x, ms, name: str):
    """根据约束名解析出实际率值与目标边界；非指标约束返回 (None, None)。"""
    import re
    m = re.match(r"(KH|SM|IM)\s*[≥≤]\s*([0-9.]+)", name)
    if not m:
        return None, None
    key, bound = m.group(1).lower(), float(m.group(2))
    cao, sio2 = _vec(ms, "cao"), _vec(ms, "sio2")
    al, fe = _vec(ms, "al2o3"), _vec(ms, "fe2o3")
    if key == "kh":
        val = float(((cao - 1.65 * al - 0.35 * fe) @ x) / (2.8 * (sio2 @ x)))
    elif key == "sm":
        val = float((sio2 @ x) / ((al + fe) @ x))
    else:
        val = float((al @ x) / (fe @ x))
    return val, bound


def diagnose_conflicts(ms, cons) -> list[dict]:
    """定位导致不可行的冲突项：
    1) 单项可放松恢复可行的约束（必要约束）+ 率值可达区间；
    2) 成对冲突；
    3) 弹性规划最小违约点上仍被违反的具体约束（多约束联合冲突时兜底，
       保证一定能点名「分母为零地板」等结构项，而不是泛泛而谈）。
    """
    ncons = len(cons)
    necessary: list[int] = []
    for i in range(ncons):
        ok, _ = lp_feasible(ms, cons, drop={i})
        if ok:
            necessary.append(i)

    conflicts = []
    if necessary:
        ranges = {}
        for key, zh in (("kh", "KH"), ("sm", "硅率 SM"), ("im", "铝率 IM")):
            rng = _achievable_range(ms, cons, key)
            if rng:
                ranges[key] = {"label": zh, "achievable": rng}
        for i in necessary:
            c = cons[i]
            item = {"kind": c.group, "constraint": c.name,
                    "detail": f"放宽约束「{c.name}」后可恢复可行"}
            if c.name.startswith("KH"):
                item["achievable"] = ranges.get("kh")
            elif c.name.startswith("SM"):
                item["achievable"] = ranges.get("sm")
            elif c.name.startswith("IM"):
                item["achievable"] = ranges.get("im")
            conflicts.append(item)
        return conflicts

    # 单项删除无效：尝试两两删除，找最小冲突组合
    found_pairs = []
    for i in range(ncons):
        for j in range(i + 1, ncons):
            ok, _ = lp_feasible(ms, cons, drop={i, j})
            if ok:
                found_pairs.append((i, j))
    for i, j in found_pairs[:6]:
        conflicts.append({
            "kind": "conflict_pair",
            "constraints": [cons[i].name, cons[j].name],
            "detail": f"约束「{cons[i].name}」与「{cons[j].name}」无法同时满足，"
                      f"同时放宽后系统可行",
        })

    # 弹性规划兜底：列出最小违约点上仍违反的具体约束
    x, slacks, values = _elastic_minimum_violation(ms, cons)
    if x is not None:
        active = [i for i in range(ncons) if slacks[i] > 1e-6]
        if active:
            violated = []
            for i in active:
                ind_val, ind_bound = _indicator_at(x, ms, cons[i].name)
                violated.append({
                    "constraint": cons[i].name, "group": cons[i].group,
                    "actual": round(float(ind_val if ind_val is not None else values[i]), 6),
                    "rhs": ind_bound if ind_bound is not None else cons[i].rhs,
                    "sense": cons[i].sense,
                    "shortfall_or_excess": round(float(slacks[i]), 6),
                    "text": _violation_text(cons[i], values[i], ind_val, ind_bound),
                })
            conflicts.append({
                "kind": "joint_conflict",
                "detail": "单项放宽均无法恢复可行；在最小总违约的配比下，"
                          "下列约束仍被同时违反，说明原料体系无法同时覆盖这些边界：",
                "violated_constraints": violated,
                "least_bad_mix_pct": {m.code: round(100.0 * float(v), 3)
                                      for m, v in zip(ms, x)},
            })
    if not conflicts:
        conflicts.append({
            "kind": "structural",
            "detail": "约束联合冲突（最低掺量之和可能超过 100%，或原料成分无法覆盖目标），"
                      "请放宽最低掺量 / 可用量",
        })
    return conflicts


def _slsqp_solve(ms, cons, mode: str, targets: OptTargets, seed: int):
    n = len(ms)
    cost = np.array([m.price_dry_t for m in ms])
    cheap = np.array([1.0 if m.cheap else 0.0 for m in ms])

    def obj(x):
        if mode == "min_cost":
            return float(cost @ x)
        if mode == "max_cheap":
            return -float(cheap @ x) + 1e-4 * float(cost @ x)
        # target_center：率值相对半宽偏差平方 + 成本微扰保证唯一
        comp = {k: _vec(ms, k) @ x for k in ("cao", "sio2", "al2o3", "fe2o3")}
        kh = (comp["cao"] - 1.65 * comp["al2o3"] - 0.35 * comp["fe2o3"]) / (2.8 * comp["sio2"])
        sm = comp["sio2"] / (comp["al2o3"] + comp["fe2o3"])
        im = comp["al2o3"] / comp["fe2o3"]
        kh0, kh1 = targets.kh_min, targets.kh_max
        sm0, sm1 = targets.sm_min, targets.sm_max
        im0, im1 = targets.im_min, targets.im_max
        dk = ((kh - 0.5 * (kh0 + kh1)) / max(1e-9, 0.5 * (kh1 - kh0))) ** 2
        ds = ((sm - 0.5 * (sm0 + sm1)) / max(1e-9, 0.5 * (sm1 - sm0))) ** 2
        di = ((im - 0.5 * (im0 + im1)) / max(1e-9, 0.5 * (im1 - im0))) ** 2
        return dk + ds + di + 1e-6 * float(cost @ x) / 100.0

    ineqs = [{"type": "ineq", "fun": (lambda x, c=c: c.residual(x))} for c in cons]
    eqs = [{"type": "eq", "fun": lambda x: float(np.sum(x) - 1.0)}]

    best = None
    rng = np.random.default_rng(seed)
    starts = []
    lo = np.array([m.min_frac for m in ms]); hi = np.array([m.max_frac for m in ms])
    base = lo + (1.0 - lo.sum()) * (hi - lo) / max(1e-12, (hi - lo).sum())
    starts.append(np.clip(base, lo, hi))
    for _ in range(12):
        y = -np.log(rng.random(n))
        y *= (hi - lo) * rng.random(n) + lo
        y = lo + y / y.sum() * (1.0 - lo.sum())
        if y.sum() <= 1.0 + 1e-6 and np.all(y <= hi + 1e-8):
            starts.append(np.clip(y, lo, hi))

    for x0 in starts:
        try:
            r = minimize(obj, x0, method="SLSQP", bounds=list(zip(lo, hi)),
                         constraints=ineqs + eqs,
                         options={"maxiter": 300, "ftol": 1e-10})
        except Exception:
            continue
        x = np.clip(r.x, lo, hi)
        x = x / x.sum()
        if not all(c.residual(x) >= -2e-3 for c in cons):
            continue
        val = obj(x)
        if best is None or val < best[0]:
            best = (val, x)
    return best[1] if best else None


MODES = [
    ("min_cost", "最低成本方案"),
    ("target_center", "指标居中方案"),
    ("max_cheap", "廉价原料最大化方案"),
]


def solve(ms: list[OptMaterial], targets: OptTargets, moisture_overrides=None, extra_cost=None):
    """主入口。返回 {status, modes:[...], conflicts:[...], constraint_log}。

    求解后的每个可行方案都会调用 chemistry.blend() 重新按质量守恒合成、
    计算率值，保证「先合成、后算指标」且 trace 完整。
    """
    # 0) 缺测硬性前置检查（任何必需氧化物为 None 都直接报错）
    for m in ms:
        m.assert_complete()
    cons = build_constraints(ms, targets)
    log = [{"name": c.name, "sense": c.sense, "rhs": c.rhs,
            "coef": {m.code: round(float(v), 6) for m, v in zip(ms, c.coef)}} for c in cons]

    ok, _ = lp_feasible(ms, cons)
    if not ok:
        return {
            "status": "infeasible",
            "modes": [],
            "conflicts": diagnose_conflicts(ms, cons),
            "constraint_log": log,
        }

    from .chemistry import MaterialAnalyses
    results = []
    seen = []
    for k, (mode, label) in enumerate(MODES):
        x = _slsqp_solve(ms, cons, mode, targets, seed=42 + k)
        if x is None:
            results.append({"mode": mode, "label": label, "status": "failed",
                            "reason": "SLSQP 未能收敛到可行解"})
            continue
        if any(float(np.max(np.abs(x - s))) < 0.03 for s in seen):
            # 与已有方案过于相似：仍保留但标记为重复，前端可选择折叠
            duplicate = True
        else:
            duplicate = False
            seen.append(x)
        frac = [float(v) for v in x]
        try:
            ams = [MaterialAnalyses(
                code=m.code, name=m.name, composition=m.composition,
                moisture_pct=m.moisture_pct,
                price_wet_t=m.price_wet_t + (extra_cost or {}).get(m.code, 0.0),
            ) for m in ms]
            trace = blend(ams, frac, denom_floor=targets.denom_floor,
                          moisture_overrides=moisture_overrides)
        except ChemistryError as e:
            results.append({"mode": mode, "label": label, "status": "failed",
                            "reason": str(e), "error_code": e.code, "details": e.details})
            continue
        results.append({
            "mode": mode, "label": label, "status": "feasible",
            "fractions_dry": frac, "duplicate": duplicate, "trace": trace,
        })
    return {"status": "feasible" if any(r["status"] == "feasible" for r in results) else "failed",
            "modes": results, "conflicts": [], "constraint_log": log}
