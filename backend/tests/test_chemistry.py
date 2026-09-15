"""核心化学计算单测：质量守恒、干湿基换算、缺测/零分母硬性报错。"""
import math

import pytest

from app.chemistry import (
    ChemistryError, MaterialAnalyses, blend, indicators, check_required,
)


def make_material(code, moisture=0.0, price=50.0, **comps):
    base = dict(cao=0, sio2=0, al2o3=0, fe2o3=0, mgo=0, so3=0,
                k2o=0, na2o=0, cl=0, loi=0)
    base.update(comps)
    return MaterialAnalyses(code=code, name=code, composition=base,
                            moisture_pct=moisture, price_wet_t=price)


def test_mass_balance_and_indicators():
    # 两原料配比 80/20，手算合成
    a = make_material("A", cao=50.0, sio2=10.0, al2o3=2.0, fe2o3=1.0, loi=36.0)
    b = make_material("B", cao=10.0, sio2=60.0, al2o3=20.0, fe2o3=10.0, loi=0.0)
    r = blend([a, b], [0.8, 0.2])
    assert r["composition_dry_pct"]["cao"] == pytest.approx(42.0)
    assert r["composition_dry_pct"]["sio2"] == pytest.approx(20.0)
    assert r["composition_dry_pct"]["al2o3"] == pytest.approx(5.6)
    assert r["composition_dry_pct"]["fe2o3"] == pytest.approx(2.8)
    assert r["indicators"]["sm"] == pytest.approx(20.0 / 8.4)
    assert r["indicators"]["im"] == pytest.approx(5.6 / 2.8)
    assert r["indicators"]["kh"] == pytest.approx(
        (42.0 - 1.65 * 5.6 - 0.35 * 2.8) / (2.8 * 20.0))
    # 灼烧基扣除 LOI 后等比放大
    assert r["composition_ignited_pct"]["cao"] == pytest.approx(42.0 / (1 - 0.288))


def test_wet_dry_conversion():
    # 含水率 20%：1 吨干料需要 1.25 吨收到基，干基成本=湿价×1.25
    a = make_material("A", moisture=20.0, price=100.0)
    assert a.dry_factor() == pytest.approx(1.25)
    assert a.price_dry_t() == pytest.approx(125.0)
    # 20% 含水的单一原料：每吨干生料需要 1.25t 湿料
    wet = make_material("W", moisture=20.0, price=40.0, cao=1, sio2=1, al2o3=1, fe2o3=1, loi=0)
    rw = blend([wet], [1.0])
    assert rw["mass_balance"]["wet_input_kg"] == pytest.approx(1250.0)
    assert rw["mass_balance"]["water_kg"] == pytest.approx(250.0)
    assert rw["cost_dry_t"] == pytest.approx(50.0)
    row = rw["materials"][0]
    assert row["fraction_wet_pct"] == pytest.approx(100.0)
    assert row["fraction_dry_pct"] == pytest.approx(100.0)


def test_moisture_override_changes_cost_not_composition():
    a = make_material("A", moisture=5.0, price=100.0, cao=50, sio2=10, al2o3=2,
                      fe2o3=1, loi=36)
    base = blend([a], [1.0])
    wet = blend([a], [1.0], moisture_overrides={"A": 25.0}, extra_cost={"A": 10.0})
    # 干基成分不变
    assert wet["composition_dry_pct"] == base["composition_dry_pct"]
    # 干基成本上升：110 / 0.75 vs 100 / 0.95
    assert wet["cost_dry_t"] == pytest.approx(110.0 / 0.75)
    assert base["cost_dry_t"] == pytest.approx(100.0 / 0.95)
    assert wet["materials"][0]["moisture_overridden"] is True


def test_missing_required_analyte_rejected():
    a = make_material("A", cao=50, sio2=10, al2o3=2, fe2o3=1, loi=36)
    b = make_material("B", cao=10, sio2=60, al2o3=20, fe2o3=10, loi=0)
    b.composition = {**b.composition, "fe2o3": None}
    with pytest.raises(ChemistryError) as ei:
        blend([a, b], [0.5, 0.5])
    assert ei.value.code == "MISSING_ANALYTES"
    assert ei.value.details["missing"][0]["analyte"] == "fe2o3"


def test_zero_denominator_rejected_not_infinity():
    # 显式零含量（未检出）≠ 缺测；此时应抛分母错误而不是 inf
    a = make_material("A", cao=50, sio2=10, al2o3=2, fe2o3=0.0, loi=36)
    with pytest.raises(ChemistryError) as ei:
        indicators(a.composition)
    assert ei.value.code == "ZERO_DENOMINATOR"
    assert not math.isinf(0)  # 确保没有静默产生 inf


def test_zero_silica_denominator():
    a = make_material("A", cao=50, sio2=0.0, al2o3=2, fe2o3=1, loi=36)
    with pytest.raises(ChemistryError) as ei:
        blend([a], [1.0])
    assert ei.value.code == "ZERO_DENOMINATOR"


def test_bad_moisture():
    a = make_material("A", moisture=100.0)
    with pytest.raises(ChemistryError) as ei:
        check_required([a])
    assert ei.value.code == "BAD_MOISTURE"


def test_fraction_sum_must_be_one():
    a = make_material("A", cao=1, sio2=1, al2o3=1, fe2o3=1, loi=0)
    with pytest.raises(ChemistryError) as ei:
        blend([a], [0.9])
    assert ei.value.code == "BAD_FRACTION_SUM"


def test_kh_above_one_warns():
    a = make_material("A", cao=70, sio2=2, al2o3=0.5, fe2o3=0.5, loi=25)
    r = blend([a], [1.0])
    assert r["kh_warning"] is not None
