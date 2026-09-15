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
    fields = r.json()["detail"]["fields"]
    # 双缺失：化验与成本两类原因分别保留、可定位，互不覆盖
    assert "materials[NOASSAY].assay" in fields
    assert "materials[NOASSAY].cost" in fields
    assert "化验" in fields["materials[NOASSAY].assay"]
    assert "成本" in fields["materials[NOASSAY].cost"]
    after = client.get("/api/scenarios").json()
    assert len(after) == before  # 不落半成品
    assert all(s["name"] != "缺化验原料" for s in after)


def test_material_without_cost_only_reports_cost(client):
    # 有生效化验但无成本：只报 cost，不应误伤 assay
    client.post("/api/materials", json={"code": "NOCOST", "name": "无成本料", "category": "测试"})
    assay = {
        "version": "A1", "moisture_pct": 3.0, "lab_note": "t",
        "cao": 50.0, "sio2": 5.0, "al2o3": 1.5, "fe2o3": 0.6,
        "mgo": 1.0, "so3": 0.1, "k2o": 0.2, "na2o": 0.05, "cl": 0.005, "loi": 40.0,
    }
    assert client.post("/api/materials/NOCOST/assays", json=assay).status_code == 200
    p = base_payload(name="缺成本原料")
    p["materials"][2] = {"material_code": "NOCOST", "min_pct": 0.0,
                         "max_pct": None, "preferred_cheap": False}
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422
    fields = r.json()["detail"]["fields"]
    assert "materials[NOCOST].cost" in fields
    assert "materials[NOCOST].assay" not in fields


def test_unknown_rain_override_code_rejected_and_not_persisted(client):
    before = len(client.get("/api/scenarios").json())
    p = base_payload(name="雨季未知含水率编码")
    p["rain_overrides"] = {"FA": 26.0, "GHOST_R2": 11.0}
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422
    fields = r.json()["detail"]["fields"]
    assert "rain_overrides.GHOST_R2" in fields
    assert "不在本次参与原料列表中" in fields["rain_overrides.GHOST_R2"]
    assert len(client.get("/api/scenarios").json()) == before  # 场景数不变


def test_unknown_rain_extra_cost_code_rejected_and_not_persisted(client):
    before = len(client.get("/api/scenarios").json())
    p = base_payload(name="雨季未知附加成本编码")
    p["rain_extra_cost"] = {"GHOST_R2": 5.0}
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 422
    fields = r.json()["detail"]["fields"]
    assert "rain_extra_cost.GHOST_R2" in fields
    assert len(client.get("/api/scenarios").json()) == before


def test_unknown_rain_code_rejected_on_draft(client):
    sid = test_create_custom_scenario_and_persist(client)
    p = base_payload(name=f"草稿未知雨季码-{next(_seq)}")
    p["rain_overrides"] = {"GHOST_R2": 9.0}
    r = client.put(f"/api/scenarios/{sid}/draft", json=p)
    assert r.status_code == 422
    assert "rain_overrides.GHOST_R2" in r.json()["detail"]["fields"]
    # 当前发布版未被破坏，仍可求解
    assert client.post(f"/api/scenarios/{sid}/solve").json()["status"] == "feasible"


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
    # 直接改写已发布场景：405（已发布修订不可改写，必须走草稿—发布）
    assert client.put("/api/scenarios/1", json=base_payload(name="尝试改内置")).status_code == 405
    assert client.delete("/api/scenarios/1").status_code == 403
    # 内置场景不可进入任何修订流程
    p = base_payload(name="内置草稿尝试")
    assert client.put("/api/scenarios/1/draft", json=p).status_code == 403
    assert client.post("/api/scenarios/1/publish", json={"lock_version": 1}).status_code == 403
    assert client.post("/api/scenarios/1/rollback-draft", json={}).status_code == 403


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
    p["rain_overrides"] = {}
    p["rain_extra_cost"] = {}
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

    # 新修订草稿钉住新化验版本；旧发布修订不被改变
    p = base_payload(name=f"换化验后的修订-{next(_seq)}")
    draft = client.put(f"/api/scenarios/{sid}/draft", json=p)
    assert draft.status_code == 200, draft.text
    lock = draft.json()["lock_version"]
    rev2_no = draft.json()["revision_no"]
    pub = client.post(f"/api/scenarios/{sid}/publish", json={"lock_version": lock})
    assert pub.status_code == 200, pub.text

    # 新发布修订求解：引用新化验
    client.post(f"/api/scenarios/{sid}/solve?profile=base")
    hist = client.get(f"/api/scenarios/{sid}/solutions").json()
    new_sols = [h for h in hist if h["revision_no"] == rev2_no]
    old_sols = [h for h in hist if h["revision_no"] == 1]
    assert new_sols and old_sols
    assert new_sols[0]["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_id"] == new_assay_id
    # 旧修订的历史解仍引用旧化验版本
    assert old_sols[0]["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_id"] == old_assay_id
    # 旧修订重放求解仍引用旧化验（旧版本永久可读、可重放）
    replay = client.post(f"/api/scenarios/{sid}/solve?revision_no=1")
    assert replay.status_code == 200
    feas = [s for s in replay.json()["solutions"] if s["status"] == "feasible"][0]
    assert feas["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_id"] == old_assay_id
    # 草稿不可求解
    client.put(f"/api/scenarios/{sid}/draft",
               json=base_payload(name=f"临时草稿-{next(_seq)}"))
    draft_rev = client.get(f"/api/scenarios/{sid}/revisions").json()[0]
    assert draft_rev["status"] == "draft"
    assert client.post(f"/api/scenarios/{sid}/solve?revision_no={draft_rev['revision_no']}").status_code == 409
    assert old_assay_id != new_assay_id


def test_optimistic_concurrency_two_editors(client):
    """两个浏览器基于同一草稿保存：只有一个成功（409 给后者）。"""
    sid = test_create_custom_scenario_and_persist(client)
    p = base_payload(name=f"并发草稿-{next(_seq)}")
    d1 = client.put(f"/api/scenarios/{sid}/draft", json=p).json()
    lock = d1["lock_version"]
    # 浏览器 A 先保存成功
    p2 = base_payload(name=f"并发草稿A-{next(_seq)}")
    r_ok = client.put(f"/api/scenarios/{sid}/draft",
                      json={**p2, "lock_version": lock})
    assert r_ok.status_code == 200
    assert r_ok.json()["lock_version"] == lock + 1
    # 浏览器 B 用过期 lock 保存
    p3 = base_payload(name=f"并发草稿B-{next(_seq)}")
    r_stale = client.put(f"/api/scenarios/{sid}/draft",
                         json={**p3, "lock_version": lock})
    assert r_stale.status_code == 409
    # 发布同样有乐观锁：旧 lock 失败
    r_pub_stale = client.post(f"/api/scenarios/{sid}/publish",
                              json={"lock_version": lock})
    assert r_pub_stale.status_code == 409
    # 最新 lock 发布成功
    latest = [r for r in client.get(f"/api/scenarios/{sid}/revisions").json()
              if r["status"] == "draft"][0]
    assert client.post(f"/api/scenarios/{sid}/publish",
                       json={"lock_version": latest["lock_version"]}).status_code == 200


def test_idempotent_publish_and_create(client):
    sid = test_create_custom_scenario_and_persist(client)
    p = base_payload(name=f"幂等草稿-{next(_seq)}")
    draft = client.put(f"/api/scenarios/{sid}/draft", json=p).json()
    key = f"idem-pub-{sid}-{next(_seq)}"
    body = {"lock_version": draft["lock_version"]}
    r1 = client.post(f"/api/scenarios/{sid}/publish", json=body,
                     headers={"Idempotency-Key": key})
    r2 = client.post(f"/api/scenarios/{sid}/publish", json=body,
                     headers={"Idempotency-Key": key})
    assert r1.status_code == 200 and r2.status_code == 200
    # 重复提交返回同一修订结果（revision_no / revision_id 相同）
    assert r1.json()["revision_id"] == r2.json()["revision_id"]
    assert r2.json().get("replay") is True
    # 草稿保存幂等：发布后再开新草稿，同键重复提交返回同一修订
    name_a = f"幂等草稿A-{next(_seq)}"
    p2 = base_payload(name=name_a)
    # 第一次（无键）建立草稿
    pre = client.put(f"/api/scenarios/{sid}/draft", json=p2)
    assert pre.status_code == 200
    k1 = f"idem-draft-{sid}-{pre.json()['revision_id']}"
    a = client.put(f"/api/scenarios/{sid}/draft",
                   json={**p2, "lock_version": pre.json()["lock_version"]},
                   headers={"Idempotency-Key": k1})
    b = client.put(f"/api/scenarios/{sid}/draft",
                   json={**p2, "lock_version": pre.json()["lock_version"]},
                   headers={"Idempotency-Key": k1})
    assert a.status_code == b.status_code == 200
    assert a.json()["revision_id"] == b.json()["revision_id"]
    assert b.json().get("replay") is True
    # 同键不同内容 → 409
    other = base_payload(name=f"幂等冲突-{next(_seq)}", denom_floor=0.1)
    r_diff = client.put(f"/api/scenarios/{sid}/draft",
                        json={**other, "lock_version": 1},
                        headers={"Idempotency-Key": k1})
    assert r_diff.status_code == 409
    client.delete(f"/api/scenarios/{sid}/draft")


def test_revision_timeline_diff_and_published_immutability(client):
    sid = test_create_custom_scenario_and_persist(client)
    # 新建草稿修改 KH 下限
    p = base_payload(name=f"差异摘要-{next(_seq)}", kh_min=0.90)
    client.put(f"/api/scenarios/{sid}/draft", json=p)
    tl = client.get(f"/api/scenarios/{sid}/revisions").json()
    assert {r["revision_no"] for r in tl} == {1, 2}
    draft_dto = next(r for r in tl if r["status"] == "draft")
    fields = {c["field"] for c in draft_dto["diff_from_published"]["changes"]}
    assert "kh_min" in fields
    # 发布历史版本 r1 仍可单独读取（永久可读）
    r1 = client.get(f"/api/scenarios/{sid}/revisions/1").json()
    assert r1["status"] == "published" and r1["payload"]["kh_min"] == 0.86


def test_rollback_published_as_new_draft(client):
    sid = test_create_custom_scenario_and_persist(client)
    # r2: KH 下限改为 0.90 并发布
    p = base_payload(name=f"回滚研究-{next(_seq)}", kh_min=0.90)
    d = client.put(f"/api/scenarios/{sid}/draft", json=p).json()
    client.post(f"/api/scenarios/{sid}/publish", json={"lock_version": d["lock_version"]})
    # 求解 r2（可行）产生历史
    assert client.post(f"/api/scenarios/{sid}/solve").json()["status"] == "feasible"
    # 从当前发布 r2 回滚为新草稿（内容等同 r2，审计源记为 2）
    rb = client.post(f"/api/scenarios/{sid}/rollback-draft", json={})
    assert rb.status_code == 200
    assert rb.json()["created_from_revision_no"] == 2
    tl_nos = [r["revision_no"] for r in client.get(f"/api/scenarios/{sid}/revisions").json()]
    assert 3 in tl_nos
    # 已有草稿时再回滚被拒绝
    assert client.post(f"/api/scenarios/{sid}/rollback-draft", json={}).status_code == 409


def test_recover_no_half_published_state(client):
    """模拟半发布指针损坏：启动恢复函数把指针修回最近有效发布版。"""
    sid = test_create_custom_scenario_and_persist(client)
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        # 人为制造孤儿指针
        db.query(models.Scenario).filter_by(id=sid).update(
            {models.Scenario.published_revision_id.name: 999999},
            synchronize_session=False)
        db.commit()
    finally:
        db.close()
    from app.revision_service import recover
    db = SessionLocal()
    try:
        notes = recover(db)
        assert any(f"场景 {sid}" in n for n in notes)
    finally:
        db.close()
    # 恢复后求解正常
    assert client.post(f"/api/scenarios/{sid}/solve").json()["status"] == "feasible"


def test_draft_publish_and_delete_custom(client):
    sid = test_create_custom_scenario_and_persist(client)
    p = base_payload(name=f"草稿改45-{next(_seq)}")
    p["materials"][0]["min_pct"] = 45.0
    d = client.put(f"/api/scenarios/{sid}/draft", json=p)
    assert d.status_code == 200
    lock = d.json()["lock_version"]
    pub = client.post(f"/api/scenarios/{sid}/publish", json={"lock_version": lock})
    assert pub.status_code == 200
    assert pub.json()["revision_no"] == 2
    # 发布后场景快照更新且可求解
    r = client.get("/api/scenarios").json()
    cur = next(s for s in r if s["id"] == sid)
    assert cur["published_revision_no"] == 2 and cur["draft_revision_no"] is None
    assert client.post(f"/api/scenarios/{sid}/solve").json()["status"] == "feasible"
    # 发布失败不留半发布：构造内容非法的草稿（绕过保存校验）后发布必须 422
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        good = client.put(f"/api/scenarios/{sid}/draft",
                          json=base_payload(name=f"发布失败验证-{next(_seq)}", cl_max=0.01))
        rev_id = good.json()["revision_id"]
        # 人为钉入一个引用失效成本的草稿（模拟发布前引用被删/失效）
        import json as _json
        rev = db.get(models.ScenarioRevision, rev_id)
        payload = _json.loads(rev.payload_json)
        payload["materials"][0]["cost_id"] = 999999
        rev.payload_json = _json.dumps(payload, ensure_ascii=False)
        db.commit()
        bd_lock = rev.lock_version
    finally:
        db.close()
    rbad = client.post(f"/api/scenarios/{sid}/publish",
                       json={"lock_version": bd_lock})
    assert rbad.status_code == 422
    tl = client.get(f"/api/scenarios/{sid}/revisions").json()
    bd2 = next(x for x in tl if x["revision_id"] == rev_id)
    assert bd2["status"] == "draft"  # 没有变成半发布
    # 当前发布版仍是 r2，求解正常
    assert client.post(f"/api/scenarios/{sid}/solve").json()["status"] == "feasible"
    # 放弃草稿
    assert client.delete(f"/api/scenarios/{sid}/draft").status_code == 200
    # 删除场景（连同全部修订与解）
    assert client.delete(f"/api/scenarios/{sid}").status_code == 200
    assert client.get(f"/api/scenarios/{sid}/solutions").status_code == 404
    assert client.delete(f"/api/scenarios/{sid}").status_code == 404

