"""自定义场景 CRUD/校验/求解/历史/化验版本快照测试（SQLite，端到端 HTTP）。"""
import itertools

import pytest

from fastapi.testclient import TestClient

from app.main import app

_seq = itertools.count(1)


def base_payload(**over):
    p = {
        "name": f"自定义研究场景 {next(_seq)}",
        "description": "研发自建虚构边界",
        "kh_min": 0.86, "kh_max": 0.96,
        "sm_min": 2.3, "sm_max": 2.8,
        "im_min": 1.2, "im_max": 1.8,
        "mgo_max": 5.0, "so3_max": 1.5, "alkali_eq_max": 1.0, "cl_max": 0.03,
        "denom_floor": 0.05,
        "rain_overrides": {"FA": 26.0, "FE": 19.0},
        "rain_extra_cost": {"LS_L": 3.0},
        "materials": [
            {"material_code": "LS_H", "min_pct": 55.0, "max_pct": None, "preferred_cheap": False},
            {"material_code": "LS_L", "min_pct": 0.0, "max_pct": None, "preferred_cheap": True},
            {"material_code": "SST", "min_pct": 0.0, "max_pct": None, "preferred_cheap": False},
            {"material_code": "SH", "min_pct": 0.0, "max_pct": None, "preferred_cheap": False},
            {"material_code": "FA", "min_pct": 0.0, "max_pct": None, "preferred_cheap": False},
            {"material_code": "FE", "min_pct": 0.0, "max_pct": None, "preferred_cheap": False},
        ],
    }
    p.update(over)
    return p


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_builtin_marked_and_listed(client):
    ss = client.get("/api/scenarios").json()
    built = {s["id"]: s for s in ss if s["built_in"]}
    assert set(built) == {1, 2, 3, 4}


def test_create_custom_scenario_and_persist(client):
    r = client.post("/api/scenarios", json=base_payload())
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["built_in"] is False
    assert len(d["materials"]) == 6
    assert client.get("/api/scenarios").json()  # 出现在列表中
    return d["id"]


def test_duplicate_name_rejected(client):
    p1 = base_payload()
    assert client.post("/api/scenarios", json=p1).status_code == 201
    r = client.post("/api/scenarios", json=base_payload(name=p1["name"]))
    assert r.status_code == 422
    assert "name" in r.json()["detail"]["fields"]


def test_blank_name_rejected(client):
    r = client.post("/api/scenarios", json=base_payload(name="   "))
    assert r.status_code == 422 and "name" in r.json()["detail"]["fields"]


def test_less_than_two_materials(client):
    p = base_payload(name="单原料非法")
    p["materials"] = [p["materials"][0]]
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422 and "materials" in r.json()["detail"]["fields"]


def test_bad_indicator_bounds(client):
    p = base_payload(name="区间倒置")
    p["kh_min"] = 0.99; p["kh_max"] = 0.80
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422 and "kh_min" in r.json()["detail"]["fields"]


def test_bad_percentages_and_moisture(client):
    p = base_payload(name="非法百分数")
    p["cl_max"] = -0.01
    p["denom_floor"] = 0.0
    p["rain_overrides"] = {"FA": 120.0}
    r = client.post("/api/scenarios", json=p)
    fields = r.json()["detail"]["fields"]
    assert {"cl_max", "denom_floor", "rain_overrides.FA"} <= set(fields)


def test_min_sum_over_100(client):
    p = base_payload(name="最低掺量超限")
    for m in p["materials"]:
        m["min_pct"] = 30.0
    r = client.post("/api/scenarios", json=p)
    fields = r.json()["detail"]["fields"]
    assert "materials.min_sum" in fields


def test_min_above_availability(client):
    p = base_payload(name="最低超可用量")
    p["materials"] = [
        {"material_code": "LS_H", "min_pct": 50.0, "max_pct": None, "preferred_cheap": False},
        {"material_code": "FA", "min_pct": 50.0, "max_pct": None, "preferred_cheap": False},
    ]
    r = client.post("/api/scenarios", json=p)
    # FA 可用量上限 12%
    assert r.status_code == 422
    assert "materials[FA].min_pct" in r.json()["detail"]["fields"]


def test_material_without_assay_rejected_and_no_half_writes(client):
    before = len(client.get("/api/scenarios").json())
    m = client.post("/api/materials", json={"code": "NOASSAY", "name": "无化验料", "category": "测试"}).json()
    assert m["active_assay_id"] is None
    p = base_payload(name="缺化验原料")
    p["materials"][3] = {"material_code": "NOASSAY", "min_pct": 0.0,
                         "max_pct": None, "preferred_cheap": False}
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422
    assert "materials[NOASSAY]" in r.json()["detail"]["fields"]
    after = client.get("/api/scenarios").json()
    assert len(after) == before  # 不落半成品
    assert all(s["name"] != "缺化验原料" for s in after)


def test_unknown_code_and_duplicate_material(client):
    p = base_payload(name="编码错误")
    p["materials"][1] = {"material_code": "GHOST", "min_pct": 0.0,
                         "max_pct": None, "preferred_cheap": False}
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422 and "materials[GHOST]" in r.json()["detail"]["fields"]

    p2 = base_payload(name="重复原料")
    p2["materials"][1] = dict(p2["materials"][0])
    r2 = client.post("/api/scenarios", json=p2)
    assert r2.status_code == 422 and "materials[LS_H]" in r2.json()["detail"]["fields"]


def test_builtin_cannot_modify_or_delete(client):
    assert client.put("/api/scenarios/1", json=base_payload(name="尝试改内置")).status_code == 403
    assert client.delete("/api/scenarios/1").status_code == 403


def test_custom_scenario_solve_base_and_rain_with_history(client):
    sid = test_create_custom_scenario_and_persist(client)
    for profile in ("base", "rain"):
        r = client.post(f"/api/scenarios/{sid}/solve?profile={profile}")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "feasible"
        feas = [s for s in d["solutions"] if s["status"] == "feasible"]
        assert {s["mode"] for s in feas} == {"min_cost", "target_center", "max_cheap"}

    # 历史追溯
    hist = client.get(f"/api/scenarios/{sid}/solutions").json()
    profiles = {h["profile"] for h in hist}
    assert {"base", "rain"} <= profiles
    for h in hist:
        if h["status"] == "feasible":
            assert h["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_version"]
    return sid


def test_custom_conflict_scenario_shows_specific_conflicts(client):
    p = base_payload(
        name="自定义强配冲突",
        kh_min=0.90, kh_max=0.96, sm_min=2.5, sm_max=2.9, im_min=1.2, im_max=1.7,
        alkali_eq_max=0.6, cl_max=0.015)
    p["materials"] = [
        {"material_code": "LS_L", "min_pct": 35.0, "max_pct": None, "preferred_cheap": True},
        {"material_code": "CG", "min_pct": 15.0, "max_pct": None, "preferred_cheap": True},
        {"material_code": "SST", "min_pct": 0.0, "max_pct": None, "preferred_cheap": False},
        {"material_code": "FE", "min_pct": 0.0, "max_pct": 5.0, "preferred_cheap": False},
    ]
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 201
    sid = r.json()["id"]
    d = client.post(f"/api/scenarios/{sid}/solve").json()
    assert d["status"] == "infeasible"
    blob = __import__("json").dumps(d["conflicts"], ensure_ascii=False)
    assert "KH ≥ 0.900" in blob and "碱当量" in blob
    # 不可行结果也持久化诊断行
    diag = [s for s in client.get(f"/api/scenarios/{sid}/solutions").json()
            if s["mode"] == "diagnosis"]
    assert diag and diag[0]["conflicts"]


def test_assay_version_snapshot_old_vs_new(client):
    """切换生效化验版本后：旧解仍引用旧版本，新解引用新版本。"""
    sid = test_create_custom_scenario_and_persist(client)
    client.post(f"/api/scenarios/{sid}/solve?profile=base")
    old_hist = client.get(f"/api/scenarios/{sid}/solutions").json()
    old_assay_id = old_hist[0]["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_id"]

    assays = client.get("/api/materials/LS_H/assays").json()
    base = next(a for a in assays if a["id"] == old_assay_id)
    new = {k: base[k] for k in (
        "version", "sampled_at", "lab_note", "moisture_pct", "cao", "sio2", "al2o3",
        "fe2o3", "mgo", "so3", "k2o", "na2o", "cl", "loi")}
    new["version"] = "C-研发复测"
    new["moisture_pct"] = 4.5
    new["lab_note"] = "研发自建化验版本（虚构）"
    r = client.post("/api/materials/LS_H/assays", json=new)
    assert r.status_code == 200
    new_assay_id = r.json()["id"]

    client.post(f"/api/scenarios/{sid}/solve?profile=base")
    hist = client.get(f"/api/scenarios/{sid}/solutions").json()
    new_assay_seen = hist[0]["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_id"]
    assert new_assay_seen == new_assay_id
    # 旧解（在历史靠后位置）仍保留旧版本引用
    older = [h for h in hist
             if h["trace"].get("provenance", {}).get("assay_versions", {})
                .get("LS_H", {}).get("assay_id") == old_assay_id]
    assert older
    assert old_assay_id != new_assay_id


def test_update_and_delete_custom(client):
    sid = test_create_custom_scenario_and_persist(client)
    p = base_payload(name=f"自定义研究场景改-{next(_seq)}")
    # 更新：改名称并把 LS_H 最低掺量从 55% 降到 45%（保持可行体系）
    p["materials"][0]["min_pct"] = 45.0
    r = client.put(f"/api/scenarios/{sid}", json=p)
    assert r.status_code == 200 and len(r.json()["materials"]) == 6
    assert r.json()["materials"][0]["min_pct"] == 45.0
    # 更新后仍可求解
    assert client.post(f"/api/scenarios/{sid}/solve").json()["status"] == "feasible"
    assert client.delete(f"/api/scenarios/{sid}").status_code == 200
    assert client.get(f"/api/scenarios/{sid}/solutions").status_code == 404
    assert client.delete(f"/api/scenarios/{sid}").status_code == 404
