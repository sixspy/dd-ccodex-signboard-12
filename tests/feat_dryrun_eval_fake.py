"""signboard-12 中件（红测乙）：求值不得把演练脏备注当成正式配置。

事故链：空桩远程推屏演练把脏字 UPDATE 进 screens.note，在线求值又把该备注
当正式配置读出，于是演练脏字进入“此刻应播什么”的正式求值结果。

修复口径：求值只投影正式区（screens.note 等 official 列），并显式标注
config_scope='official'；演练在甲路径下根本不落任何正式字段，审计/历史台账
里即便留了提案脏字，也绝不参与求值。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import app
from audit_svc import write_audit
from db import connect, init_db

DIRTY_EVAL = "EVAL_DIRTY_求值读到演练脏备注当成正式配置"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    monkeypatch.setattr("db.DB_PATH", db)
    monkeypatch.setattr("db.DATA", tmp_path)
    if db.exists():
        db.unlink()
    init_db()
    with TestClient(app) as c:
        yield c


def _login(client, username="admin", password="admin123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _screen_id(client, headers, code="scr_lobby_main"):
    items = client.get("/api/screens", headers=headers).json()["items"]
    return next(s["id"] for s in items if s["code"] == code)


def test_求值结果显式标注正式区且投影正式备注(client):
    h = _login(client)
    sid = _screen_id(client, h)

    r = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": sid, "now_hm": "10:00", "weekday": "1"},
    )
    assert r.status_code == 200, r.text
    data = r.json()

    # 红：种子求值没有 official_note / config_scope 字段（KeyError 或断言失败）。
    assert data.get("config_scope") == "official"
    with connect() as conn:
        official_note = conn.execute("SELECT note FROM screens WHERE id=?", (sid,)).fetchone()["note"]
    assert data.get("official_note") == (official_note or "")
    assert data.get("screen_note") == (official_note or "")


def test_演练脏备注不得出现在求值结果中(client):
    h = _login(client, "writer", "writer123")
    sid = _screen_id(client, h, "scr_lobby_side")

    # 发起一次带脏备注的演练（甲路径：短路，不写正式列）。
    dr = client.post(
        "/api/stubs/remote_push",
        headers=h,
        json={"screen_id": sid, "dry_run": True, "note": DIRTY_EVAL},
    )
    assert dr.status_code == 200, dr.text

    ev = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": sid, "now_hm": "10:00", "weekday": "1"},
    )
    assert ev.status_code == 200, ev.text
    text = ev.text

    # 整条求值 JSON 都不得携带演练脏字。
    assert DIRTY_EVAL not in text
    body = ev.json()
    assert body.get("config_scope") == "official"
    with connect() as conn:
        note = conn.execute("SELECT note FROM screens WHERE id=?", (sid,)).fetchone()["note"]
    assert body.get("official_note") == (note or "")
    assert DIRTY_EVAL not in (body.get("official_note") or "")


def test_审计台账中的演练提案脏字绝不泄漏进求值(client):
    h = _login(client)
    sid = _screen_id(client, h)

    # 甲路径会把“被短路的提案”记进审计台账；这里直接构造该台账行，
    # 专门验证求值服务不读 audit_log / change_history 作为配置来源。
    write_audit(
        "writer",
        "remote_push.drill_short_circuit",
        {"screen_id": sid, "proposed_note": DIRTY_EVAL},
    )

    ev = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": sid, "now_hm": "10:00", "weekday": "1"},
    )
    assert ev.status_code == 200
    assert DIRTY_EVAL not in ev.text
    assert ev.json().get("config_scope") == "official"


def test_只有正式列真被改写时求值才读到新备注_检测有牙齿(client):
    h = _login(client)
    sid = _screen_id(client, h)
    marker = "OFFICIAL_ONLY_MARKER_正式配置"

    # 直接改正式列（等价于“实推落库”后的状态），求值必须如实投影——
    # 这证明 official_note 确实来自 screens.note，而不是被写死的假绿。
    with connect() as conn:
        conn.execute("UPDATE screens SET note=? WHERE id=?", (marker, sid))
        conn.commit()

    ev = client.post(
        "/api/evaluate/screen",
        headers=h,
        json={"screen_id": sid, "now_hm": "10:00", "weekday": "1"},
    )
    assert ev.status_code == 200
    assert ev.json().get("official_note") == marker
    assert marker in ev.text
