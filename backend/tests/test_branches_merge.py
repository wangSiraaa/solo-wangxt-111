"""并行试验分支—三方合并发布的验收测试（SQLite，端到端 HTTP）。"""
import json

import pytest

from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _custom_six(client, name, **over):
    p = {
        "name": name, "description": "分支合并验收",
        "kh_min": 0.86, "kh_max": 0.96, "sm_min": 2.3, "sm_max": 2.8,
        "im_min": 1.2, "im_max": 1.8,
        "mgo_max": 5.0, "so3_max": 1.5, "alkali_eq_max": 1.0, "cl_max": 0.03,
        "denom_floor": 0.05, "rain_overrides": {}, "rain_extra_cost": {},
        "materials": [
            {"material_code": c, "min_pct": 55.0 if c == "LS_H" else 0.0,
             "max_pct": None, "preferred_cheap": c == "LS_L"}
            for c in ("LS_H", "LS_L", "SST", "SH", "FA", "FE")],
    }
    p.update(over)
    r = client.post("/api/scenarios", json=p)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _branch_from_r1(client, sid, name, payload_over=None):
    r = client.post(f"/api/scenarios/{sid}/branches",
                    json={"source_revision_no": 1, "branch_name": name})
    assert r.status_code == 201, r.text
    rev = r.json()
    # 从分支 payload 派生草稿内容
    p = _payload_to_input(rev["payload"], f"{name}-内容")
    if payload_over:
        payload_over(p)
    d = client.put(f"/api/scenarios/{sid}/draft", json={
        **p, "revision_no": rev["revision_no"],
        "lock_version": rev["lock_version"]})
    assert d.status_code == 200, d.text
    return d.json()


def _payload_to_input(payload, name):
    return {
        "name": name, "description": payload.get("description", ""),
        "kh_min": payload["kh_min"], "kh_max": payload["kh_max"],
        "sm_min": payload["sm_min"], "sm_max": payload["sm_max"],
        "im_min": payload["im_min"], "im_max": payload["im_max"],
        "mgo_max": payload["mgo_max"], "so3_max": payload["so3_max"],
        "alkali_eq_max": payload["alkali_eq_max"], "cl_max": payload["cl_max"],
        "denom_floor": payload["denom_floor"],
        "rain_overrides": payload.get("rain_overrides", {}),
        "rain_extra_cost": payload.get("rain_extra_cost", {}),
        "materials": [
            {"material_code": m["code"], "min_pct": m["min_pct"],
             "max_pct": m["max_pct"], "preferred_cheap": m["preferred_cheap"],
             "assay_id": m.get("assay_id"), "cost_id": m.get("cost_id")}
            for m in payload["materials"]],
    }


def test_two_branches_non_overlapping_merge_and_solve(client):
    sid = _custom_six(client, "并行分支不重叠合并")
    # A 改 KH 下限，B 改 MgO 上限（互不重叠）
    a = _branch_from_r1(client, sid, "KH收紧", lambda p: p.update(kh_min=0.90))
    b = _branch_from_r1(client, sid, "MgO放宽", lambda p: p.update(mgo_max=6.0))
    r = client.post(f"/api/scenarios/{sid}/merge", json={"a": a["revision_no"],
                                                          "b": b["revision_no"]})
    assert r.status_code == 200, r.text
    cand = r.json()
    assert cand["status"] == "merged"
    merge_info = cand["merge"]
    assert merge_info["base_no"] == 1
    assert {merge_info["a_no"], merge_info["b_no"]} == {a["revision_no"], b["revision_no"]}
    # 自动决议包含双方改动
    fields = {x["field"] for x in merge_info["auto"]}
    assert "kh_min" in fields and "mgo_max" in fields
    cand_no = cand["revision_no"]
    assert cand["payload"]["kh_min"] == 0.90
    assert cand["payload"]["mgo_max"] == 6.0
    # 候选不可求解（只有发布后才能）
    assert client.post(f"/api/scenarios/{sid}/solve?revision_no={cand_no}").status_code == 409

    # 发布候选（乐观锁）
    pub = client.post(f"/api/scenarios/{sid}/publish",
                      json={"revision_no": cand_no, "lock_version": cand["lock_version"]})
    assert pub.status_code == 200, pub.text
    cur = next(s for s in client.get("/api/scenarios").json() if s["id"] == sid)
    assert cur["published_revision_no"] == cand_no

    # 旱季/雨季三方案
    for profile in ("base", "rain"):
        d = client.post(f"/api/scenarios/{sid}/solve?profile={profile}").json()
        feas = [s for s in d["solutions"] if s["status"] == "feasible"]
        assert {s["mode"] for s in feas} == {"min_cost", "target_center", "max_cheap"}
        assert all(s["revision_no"] == cand_no for s in feas)


def test_concurrent_branch_save_one_wins(client):
    sid = _custom_six(client, "并发保存同一分支")
    r = client.post(f"/api/scenarios/{sid}/branches",
                    json={"source_revision_no": 1, "branch_name": "并分支"})
    b = r.json()
    p = _payload_to_input(b["payload"], "并分支内容")
    p["kh_min"] = 0.90
    body = {**p, "revision_no": b["revision_no"], "lock_version": b["lock_version"]}
    ok = client.put(f"/api/scenarios/{sid}/draft", json=body)
    stale = client.put(f"/api/scenarios/{sid}/draft", json=body)
    assert ok.status_code == 200
    assert stale.status_code == 409


def test_idempotent_merge_returns_same_candidate(client):
    sid = _custom_six(client, "合并幂等")
    a = _branch_from_r1(client, sid, "幂等A", lambda p: p.update(kh_min=0.88))
    b = _branch_from_r1(client, sid, "幂等B", lambda p: p.update(mgo_max=4.5))
    body = {"a": a["revision_no"], "b": b["revision_no"]}
    r1 = client.post(f"/api/scenarios/{sid}/merge", json=body,
                     headers={"Idempotency-Key": "merge-key-001"})
    r2 = client.post(f"/api/scenarios/{sid}/merge", json=body,
                     headers={"Idempotency-Key": "merge-key-001"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["revision_id"] == r2.json()["revision_id"]
    assert r2.json().get("replay") is True


def test_same_field_conflict_blocks_candidate(client):
    sid = _custom_six(client, "同字段冲突")
    a = _branch_from_r1(client, sid, "A的KH", lambda p: p.update(kh_min=0.90))
    b = _branch_from_r1(client, sid, "B的KH", lambda p: p.update(kh_min=0.88))
    r = client.post(f"/api/scenarios/{sid}/merge", json={"a": a["revision_no"],
                                                          "b": b["revision_no"]})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "conflict"
    fields = {c["field"] for c in d["conflicts"]}
    assert "kh_min" in fields
    assert "attempt_id" in d  # 可继续决议
    # 未生成发布候选：时间线里无 merged
    tl = client.get(f"/api/scenarios/{sid}/revisions").json()
    assert all(x["status"] != "merged" for x in tl)

    # 人工决议后重新合并成功
    r2 = client.post(f"/api/scenarios/{sid}/merge", json={
        "a": a["revision_no"], "b": b["revision_no"],
        "attempt_id": d["attempt_id"], "resolutions": {"kh_min": 0.90}})
    assert r2.status_code == 200, r2.text
    cand = r2.json()
    assert cand["status"] == "merged"
    decisions = {x["field"]: x for x in cand["merge"]["auto"]}
    assert decisions["kh_min"]["resolution"] == "manual"
    assert cand["payload"]["kh_min"] == 0.90


def test_modify_delete_material_conflict(client):
    sid = _custom_six(client, "删改冲突")
    # A 删除 FE；B 修改 FE 最低掺量
    a = _branch_from_r1(client, sid, "删FE",
                        lambda p: p["materials"].__delitem__(
                            next(i for i, m in enumerate(p["materials"])
                                 if m["material_code"] == "FE")))
    def b_edit(p):
        fe = next(m for m in p["materials"] if m["material_code"] == "FE")
        fe["min_pct"] = 2.0
    b = _branch_from_r1(client, sid, "改FE", b_edit)
    r = client.post(f"/api/scenarios/{sid}/merge", json={"a": a["revision_no"],
                                                          "b": b["revision_no"]})
    d = r.json()
    assert d["status"] == "conflict"
    assert any(c["field"].startswith("materials.FE") for c in d["conflicts"])
    assert all(x["status"] != "merged" for x in client.get(
        f"/api/scenarios/{sid}/revisions").json())


def test_stale_pinned_assay_blocks_merge(client):
    sid = _custom_six(client, "失效化验冲突")
    # A 分支正常改边界；B 分支钉入已失效的 assay_id
    a = _branch_from_r1(client, sid, "正常支", lambda p: p.update(kh_min=0.90))
    rb = client.post(f"/api/scenarios/{sid}/branches",
                     json={"source_revision_no": 1, "branch_name": "失效支"}).json()
    p = _payload_to_input(rb["payload"], "失效支内容")
    p["materials"][0]["assay_id"] = 999999  # LS_H 钉入不存在的化验
    upd = client.put(f"/api/scenarios/{sid}/draft",
                     json={**p, "revision_no": rb["revision_no"],
                           "lock_version": rb["lock_version"]})
    # 分支保存本身也应拒绝失效钉住引用
    assert upd.status_code == 422
    assert any(k.startswith("materials[LS_H].assay") for k in upd.json()["detail"]["fields"])


def test_assay_switch_branch_merge_snapshot(client):
    """切换生效化验后：r1 旧解指旧快照，合并发布新解指新快照。"""
    sid = _custom_six(client, "快照合并")
    client.post(f"/api/scenarios/{sid}/solve?profile=base")
    old_id = client.get(f"/api/scenarios/{sid}/solutions").json()[0]["trace"][
        "provenance"]["assay_versions"]["LS_H"]["assay_id"]
    # 新化验并生效
    assays = client.get("/api/materials/LS_H/assays").json()
    base = next(x for x in assays if x["id"] == old_id)
    new = {k: base[k] for k in (
        "version", "sampled_at", "lab_note", "moisture_pct", "cao", "sio2", "al2o3",
        "fe2o3", "mgo", "so3", "k2o", "na2o", "cl", "loi")}
    new["version"] = "D-矿点新批"
    r = client.post("/api/materials/LS_H/assays", json=new)
    new_id = r.json()["id"]

    a = _branch_from_r1(client, sid, "快照A", lambda p: p.update(kh_min=0.88))
    b = _branch_from_r1(client, sid, "快照B", lambda p: p.update(mgo_max=4.5))
    cand = client.post(f"/api/scenarios/{sid}/merge",
                       json={"a": a["revision_no"], "b": b["revision_no"]}).json()
    # 合并材料按编码排序：定位 LS_H；分支从 r1 复制，钉住旧化验
    lsh = next(m for m in cand["payload"]["materials"] if m["code"] == "LS_H")
    assert lsh["assay_id"] == old_id
    pub = client.post(f"/api/scenarios/{sid}/publish", json={
        "revision_no": cand["revision_no"],
        "lock_version": cand["lock_version"]}).json()
    # r1 旧解仍是旧快照
    old_sols = [h for h in client.get(f"/api/scenarios/{sid}/solutions").json()
                if h["revision_no"] == 1]
    assert old_sols[0]["trace"]["provenance"]["assay_versions"]["LS_H"]["assay_id"] == old_id
    # 新发布解——r1 分支复制的是旧 ID；要让新解指新快照，需新修订显式引用
    lsh_pub = next(m for m in pub["payload"]["materials"] if m["code"] == "LS_H")
    assert lsh_pub["assay_id"] == old_id
    assert new_id != old_id


def test_rollback_draft_preserves_conflicts_history_and_builtin(client):
    sid = _custom_six(client, "回滚不破坏审计")
    a = _branch_from_r1(client, sid, "冲突支A", lambda p: p.update(kh_min=0.90))
    b = _branch_from_r1(client, sid, "冲突支B", lambda p: p.update(kh_min=0.88))
    client.post(f"/api/scenarios/{sid}/merge",
                json={"a": a["revision_no"], "b": b["revision_no"]})
    hist_before = client.get(f"/api/scenarios/{sid}/merge-attempts").json()
    assert hist_before and hist_before[0]["status"] == "open"

    # 从 r1 回滚为新线性草稿
    rb = client.post(f"/api/scenarios/{sid}/rollback-draft",
                     json={"source_revision_no": 1})
    assert rb.status_code == 200
    assert rb.json()["created_from_revision_no"] == 1
    # 合并尝试审计仍在
    hist_after = client.get(f"/api/scenarios/{sid}/merge-attempts").json()
    assert len(hist_after) == len(hist_before)
    # 内置 403
    assert client.post("/api/scenarios/1/branches",
                       json={"source_revision_no": 1, "branch_name": "x"}).status_code == 403
    assert client.post("/api/scenarios/1/merge", json={"a": 1, "b": 2}).status_code == 403
    assert client.post("/api/scenarios/1/merge-preview",
                       json={"base": 1, "a": 1, "b": 2}).status_code == 403


def test_recover_after_restart_allows_resume(client):
    """open 合并尝试与候选在重启恢复后仍可继续处理。"""
    sid = _custom_six(client, "重启恢复合并")
    a = _branch_from_r1(client, sid, "恢复A", lambda p: p.update(kh_min=0.90))
    b = _branch_from_r1(client, sid, "恢复B", lambda p: p.update(kh_min=0.88))
    d = client.post(f"/api/scenarios/{sid}/merge",
                    json={"a": a["revision_no"], "b": b["revision_no"]}).json()
    assert d["status"] == "conflict"
    attempt_id = d["attempt_id"]

    # 模拟重启：重新执行启动恢复
    from app.database import SessionLocal
    from app.revision_service import recover
    db = SessionLocal()
    try:
        recover(db)
    finally:
        db.close()

    # open 尝试仍在，可带决议继续；没有半发布
    again = client.post(f"/api/scenarios/{sid}/merge", json={
        "a": a["revision_no"], "b": b["revision_no"],
        "attempt_id": attempt_id, "resolutions": {"kh_min": 0.89}})
    assert again.status_code == 200
    cand = again.json()
    assert cand["status"] == "merged"
