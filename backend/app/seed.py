"""虚构演示数据：7 个原料 + 多个化验版本 + 3 个试算场景。

注意：所有成分与价格均为虚构，仅用于离线工艺研究，不对应任何真实矿山/工厂。
"""
import json

from sqlalchemy.orm import Session

from . import models

# (code, name, category, moisture, price_wet, availability_max_pct(-1=不限), comps...)
# comps 顺序: cao sio2 al2o3 fe2o3 mgo so3 k2o na2o cl loi
MATERIALS = [
    ("LS_H", "高钙石灰石", "钙质", 2.0, 38.0, -1,
     dict(cao=50.5, sio2=4.2, al2o3=1.3, fe2o3=0.6, mgo=1.2, so3=0.20,
          k2o=0.25, na2o=0.05, cl=0.005, loi=41.2)),
    ("LS_L", "低钙石灰石(廉价)", "钙质", 3.0, 22.0, 45.0,
     dict(cao=45.5, sio2=9.5, al2o3=2.4, fe2o3=1.0, mgo=2.0, so3=0.30,
          k2o=0.45, na2o=0.08, cl=0.008, loi=38.0)),
    ("SST", "砂岩", "硅铝质", 5.0, 72.0, -1,
     dict(cao=1.2, sio2=82.5, al2o3=7.8, fe2o3=3.0, mgo=0.8, so3=0.10,
          k2o=1.10, na2o=0.25, cl=0.010, loi=2.8)),
    ("SH", "页岩", "硅铝质", 8.0, 45.0, -1,
     dict(cao=6.5, sio2=58.0, al2o3=17.0, fe2o3=7.5, mgo=2.0, so3=0.40,
          k2o=2.60, na2o=0.70, cl=0.015, loi=4.5)),
    ("FA", "粉煤灰", "校正料", 18.0, 55.0, 12.0,
     dict(cao=4.5, sio2=52.0, al2o3=28.0, fe2o3=7.5, mgo=1.4, so3=0.80,
          k2o=1.50, na2o=0.60, cl=0.010, loi=4.0)),
    ("FE", "铁尾矿", "铁质", 12.0, 210.0, 5.0,
     dict(cao=5.0, sio2=22.0, al2o3=9.0, fe2o3=52.0, mgo=2.5, so3=0.50,
          k2o=0.30, na2o=0.10, cl=0.020, loi=1.5)),
    ("CG", "煤矸石(廉价高碱)", "硅铝质", 6.0, 15.0, 30.0,
     dict(cao=3.0, sio2=55.0, al2o3=20.0, fe2o3=6.0, mgo=1.5, so3=1.20,
          k2o=3.20, na2o=0.80, cl=0.040, loi=8.0)),
    ("QZ", "石英砂(未检出铁)", "硅铝质", 4.0, 68.0, -1,
     dict(cao=0.5, sio2=95.0, al2o3=3.2, fe2o3=0.0, mgo=0.2, so3=0.05,
          k2o=0.20, na2o=0.05, cl=0.002, loi=0.8)),
    ("SST_NEW", "新矿点砂岩(缺测)", "硅铝质", 6.0, 60.0, -1,
     dict(cao=1.0, sio2=80.0, al2o3=8.5, fe2o3=None, mgo=0.9, so3=0.10,
          k2o=1.00, na2o=0.20, cl=0.010, loi=3.0)),
]

# 旱季/雨季两套含水率化验版本（仅给含水率差异大的几个原料做雨季版本）
RAIN_ASSAY = {"LS_L": 7.0, "SST": 9.0, "SH": 14.0, "FA": 26.0, "FE": 19.0, "CG": 11.0}


def seed(db: Session) -> None:
    if db.query(models.Material).count() > 0:
        return

    created = {}
    for code, name, cat, moisture, price, avail_max, comp in MATERIALS:
        m = models.Material(code=code, name=name, category=cat)
        db.add(m); db.flush()
        a = models.Assay(material_id=m.id, version="A-旱季基线", moisture_pct=moisture,
                         lab_note="虚构化验室 2026-07 批次（干基成分）", **comp)
        db.add(a); db.flush()
        rain_moist = RAIN_ASSAY.get(code)
        if rain_moist is not None:
            ar = models.Assay(material_id=m.id, version="B-雨季复测", moisture_pct=rain_moist,
                              lab_note="虚构化验室 2026-09 雨季复测（成分不变，仅含水率变化）", **comp)
            db.add(ar); db.flush()
        c = models.Cost(material_id=m.id, price_wet_t=price, note="到厂价(收到基)，虚构")
        db.add(c); db.flush()
        if avail_max >= 0:
            db.add(models.Availability(material_id=m.id, max_fraction_pct=avail_max,
                                       supply_note="虚构月度可供量折算的配比上限"))
        m.active_assay_id = a.id
        m.active_cost_id = c.id
        created[code] = m
    db.flush()

    def add_scenario(name, desc, target, items, rain=None, extra=None, hazards=None):
        hazards = hazards or {}
        sc = models.Scenario(
            name=name, description=desc, built_in=True,
            kh_min=target["kh"][0], kh_max=target["kh"][1],
            sm_min=target["sm"][0], sm_max=target["sm"][1],
            im_min=target["im"][0], im_max=target["im"][1],
            mgo_max=hazards.get("mgo_max", 5.0),
            so3_max=hazards.get("so3_max", 1.5),
            alkali_eq_max=hazards.get("alkali_eq_max", 1.0),
            cl_max=hazards.get("cl_max", 0.03),
            rain_overrides=json.dumps(rain or {}, ensure_ascii=False),
            rain_extra_cost_json=json.dumps(extra or {}, ensure_ascii=False),
        )
        db.add(sc); db.flush()
        for code, mn, mx, cheap in items:
            db.add(models.ScenarioMaterial(
                scenario_id=sc.id, material_id=created[code].id,
                min_pct=mn, max_pct=mx, preferred_cheap=cheap))
        return sc

    base_target = dict(kh=(0.86, 0.96), sm=(2.3, 2.8), im=(1.2, 1.8))
    add_scenario(
        "S1 六原料常规配比（含雨季对比）",
        "虚构 5000t/d 研究边界：高/低钙石灰石 + 砂岩/页岩 + 粉煤灰/铁尾矿。"
        "低钙石灰石标记为廉价原料；粉煤灰、铁尾矿雨季含水率显著上升。",
        base_target,
        [("LS_H", 55.0, None, False), ("LS_L", 0.0, None, True),
         ("SST", 0.0, None, False), ("SH", 0.0, None, False),
         ("FA", 0.0, None, False), ("FE", 0.0, None, False)],
        rain={"LS_L": 7.0, "SST": 9.0, "SH": 14.0, "FA": 26.0, "FE": 19.0},
        extra={"LS_L": 3.0},
    )

    add_scenario(
        "S2 廉价原料强配（预期不可行）",
        "研究问题：低钙石灰石最低 35% 且强制掺入 15% 煤矸石时，石灰饱和与碱当量上限"
        "是否还能同时守住？（预期：KH 下限与碱当量上限构成冲突对）",
        dict(kh=(0.90, 0.96), sm=(2.5, 2.9), im=(1.2, 1.7)),
        [("LS_L", 35.0, None, True), ("CG", 15.0, None, True),
         ("SST", 0.0, None, False), ("FE", 0.0, None, False)],
        hazards=dict(alkali_eq_max=0.6, cl_max=0.015),
    )

    add_scenario(
        "S3 缺测原料（预期缺测报错）",
        "新矿点砂岩 Fe₂O₃ 尚无化验结果（NULL，不是 0），验证系统拒绝把缺测当零含量。",
        base_target,
        [("LS_H", 55.0, None, False), ("SST_NEW", 0.0, None, False),
         ("SH", 0.0, None, False), ("FE", 0.0, None, False)],
    )

    add_scenario(
        "S4 未检出铁原料（预期分母报错）",
        "石英砂 Fe₂O₃ 为显式 0.00%（未检出，非缺测）。强制 96% 石英砂后，合成 Fe₂O₃ "
        "低于分母保护阈值，优化器在可行性阶段即按分母地板约束拒绝并指出冲突；"
        "在「手工试算」页给出同类配比还会直接返回 ZERO_DENOMINATOR 错误，绝不默认零含量。",
        dict(kh=(0.80, 1.00), sm=(2.0, 4.0), im=(0.5, 3.0)),
        [("LS_H", 0.0, 4.0, False), ("QZ", 96.0, None, False)],
    )
    db.commit()
