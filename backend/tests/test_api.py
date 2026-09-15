"""HTTP 链路集成测试：以 SQLite 覆盖数据库，验证四个场景的端到端行为。"""
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["BATCH_DATABASE_URL"] = f"sqlite:///{_tmp.name}"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_and_seed(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "虚构" in r.json()["notice"]
    scenarios = client.get("/api/scenarios").json()
    assert len(scenarios) == 4
    materials = client.get("/api/materials").json()
    assert len(materials) == 9


def test_s1_base_and_rain_comparison(client):
    base = client.post("/api/scenarios/1/solve?profile=base").json()
    rain = client.post("/api/scenarios/1/solve?profile=rain").json()
    assert base["status"] == "feasible"
    assert rain["status"] == "feasible"
    fb = [s for s in base["solutions"] if s["status"] == "feasible"]
    fr = [s for s in rain["solutions"] if s["status"] == "feasible"]
    assert fb and fr
    # 雨季更贵
    assert min(s["cost_dry_t"] for s in fr) > min(s["cost_dry_t"] for s in fb)
    # trace 可追溯：化验版本 + 干湿基换算 + 质量守恒 + 线性约束
    tr = fb[0]["trace"]
    assert tr["provenance"]["assay_versions"]["LS_H"]["assay_version"].startswith("A-")
    assert tr["trace"]["mass_balance"]["wet_input_kg"] >= 1000.0
    assert len(tr["provenance"]["linear_constraints"]) >= 10
    row = tr["trace"]["materials"][0]
    assert row["dry_factor_t_per_t"] >= 1.0


def test_s2_infeasible_conflict(client):
    r = client.post("/api/scenarios/2/solve").json()
    assert r["status"] == "infeasible"
    assert r["conflicts"]
    diag = [s for s in r["solutions"] if s["mode"] == "diagnosis"][0]
    assert diag["conflicts"]


def test_s3_missing_analyte_422(client):
    r = client.post("/api/scenarios/3/solve")
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "MISSING_ANALYTES"
    # 缺测可能来自 blend 级（按原料×项目列出）或优化器前置检查（按原料列出）
    details = body["details"]
    flat = str(details)
    assert "fe2o3" in flat and "SST_NEW" in flat


def test_s4_floor_conflict(client):
    r = client.post("/api/scenarios/4/solve").json()
    assert r["status"] == "infeasible"
    import json
    blob = json.dumps(r["conflicts"], ensure_ascii=False)
    assert ("分母" in blob or "Fe" in blob)


def test_manual_blend_zero_denominator_explicit_error(client):
    # 手工给出 92% 石英砂 + 8% 高钙石灰石 → 合成 Fe2O3 极低
    r = client.post("/api/manual-blend", json={"items": [
        {"code": "QZ", "pct": 92}, {"code": "LS_H", "pct": 8}]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "ZERO_DENOMINATOR"


def test_manual_blend_missing_analyte_error(client):
    r = client.post("/api/manual-blend", json={"items": [
        {"code": "SST_NEW", "pct": 50}, {"code": "LS_H", "pct": 50}]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "MISSING_ANALYTES"


def test_manual_blend_moisture_override(client):
    r = client.post("/api/manual-blend", json={"items": [
        {"code": "LS_H", "pct": 55}, {"code": "LS_L", "pct": 10},
        {"code": "SST", "pct": 15}, {"code": "SH", "pct": 10},
        {"code": "FA", "pct": 7}, {"code": "FE", "pct": 3}],
        "denom_floor": 0.05})
    assert r.status_code == 200
    t = r.json()["trace"]
    assert abs(sum(x["fraction_dry_pct"] for x in t["materials"]) - 100.0) < 1e-6
