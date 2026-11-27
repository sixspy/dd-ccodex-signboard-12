"""signboard-12 前件（红测甲）：演练模式仍改写正式备注。

夹具名逐字为「演练模式仍改写正式备注」，复现“空桩远程推屏在演练模式下把
脏备注 UPDATE 进 screens.note 正式字段”的事故形状。

正确性口径（甲路径，班内封死，禁止第三路径）：
    演练 = 正式区零增量：拒写备注并在任何正式写之前短路；
    只有显式实推才允许写 screens.note。

本文件在业务码修复前必须为红：种子里 /api/stubs/remote_push 固定 501，
且没有“演练短路 / 正式备注零改”的契约。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

DIRTY_NOTE = "演练模式仍改写正式备注"
REAL_NOTE = "实推才落正式备注-signboard12"
LOCK_NAME = "远程推屏演练互斥"


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


def _login(client: TestClient, username: str = "writer", password: str = "writer123") -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def 演练模式仍改写正式备注(client):
    """复现夹具：选定一块屏，记录正式备注原值，并演示旧脏写的可检测形状。"""
    headers = _login(client)
    screens = client.get("/api/screens", headers=headers).json()["items"]
    screen = next(s for s in screens if s["code"] == "scr_lobby_side")
    sid = screen["id"]

    with connect() as conn:
        original = conn.execute("SELECT note FROM screens WHERE id=?", (sid,)).fetchone()["note"]

    # 事故复现（不落业务码）：旧实现会把演练脏字直接写进正式列。
    # 这里仅在夹具内手动模拟一次，证明断言“正式备注被改写”有牙齿，随后还原。
    with connect() as conn:
        conn.execute("UPDATE screens SET note=? WHERE id=?", (DIRTY_NOTE, sid))
        conn.commit()
    with connect() as conn:
        planted = conn.execute("SELECT note FROM screens WHERE id=?", (sid,)).fetchone()["note"]
    assert DIRTY_NOTE in planted  # 检测口径本身有效：脏字进正式列即可被抓到

    with connect() as conn:
        conn.execute("UPDATE screens SET note=? WHERE id=?", (original, sid))
        conn.commit()

    before = client.get("/api/screens", headers=headers).json()["items"]
    target = next(s for s in before if s["id"] == sid)
    assert (target.get("note") or "") == (original or "")

    ctx = SimpleNamespace(
        client=client,
        headers=headers,
        screen=target,
        sid=sid,
        original_note=original or "",
        dirty=DIRTY_NOTE,
        lock_name=LOCK_NAME,
    )
    yield ctx

    # 还原，避免污染（每个用例本就使用临时库，这里只是卫生）。
    with connect() as conn:
        conn.execute("UPDATE screens SET note=? WHERE id=?", (original or "", sid))
        conn.commit()


def _drill(client, headers, sid, note, **extra):
    body = {"screen_id": sid, "dry_run": True, "note": note}
    body.update(extra)
    return client.post("/api/stubs/remote_push", headers=headers, json=body)


def _real_push(client, headers, sid, note, **extra):
    body = {"screen_id": sid, "dry_run": False, "confirm": True, "note": note}
    body.update(extra)
    return client.post("/api/stubs/remote_push", headers=headers, json=body)


def test_演练模式仍改写正式备注_演练必须零改写并短路(演练模式仍改写正式备注):
    d = 演练模式仍改写正式备注
    r = _drill(d.client, d.headers, d.sid, d.dirty)

    # 红：种子返回 501；绿：甲路径给出 200 演练回执。
    assert r.status_code == 200, r.text
    data = r.json()

    # 明确的“未提交 / 正式未写 / 已短路”口径，禁止只改页角标式假绿。
    assert data.get("dry_run") is True or data.get("drill") is True
    assert data.get("official_written") is False
    assert data.get("committed") is False or data.get("short_circuited") is True

    # API 视角：正式备注原样，脏字不得进正式字段。
    items = d.client.get("/api/screens", headers=d.headers).json()["items"]
    now_screen = next(s for s in items if s["id"] == d.sid)
    assert (now_screen.get("note") or "") == d.original_note
    assert d.dirty not in (now_screen.get("note") or "")

    # 库视角：账实一致，正式列零改。
    with connect() as conn:
        db_note = conn.execute("SELECT note FROM screens WHERE id=?", (d.sid,)).fetchone()["note"]
    assert db_note == d.original_note
    assert d.dirty not in (db_note or "")


def test_仅实推才允许改写正式备注(演练模式仍改写正式备注):
    d = 演练模式仍改写正式备注

    # 先演练一次：不得写。
    rd = _drill(d.client, d.headers, d.sid, d.dirty)
    assert rd.status_code == 200, rd.text
    with connect() as conn:
        note_after_drill = conn.execute("SELECT note FROM screens WHERE id=?", (d.sid,)).fetchone()["note"]
    assert note_after_drill == d.original_note

    # 显式实推：必须写，且写的是正式备注。
    rr = _real_push(d.client, d.headers, d.sid, REAL_NOTE)
    assert rr.status_code == 200, rr.text
    body = rr.json()
    assert body.get("ok") is True
    assert body.get("official_written") is True
    assert body.get("committed") is True

    with connect() as conn:
        note_after_real = conn.execute("SELECT note FROM screens WHERE id=?", (d.sid,)).fetchone()["note"]
    assert note_after_real == REAL_NOTE

    # 实推之后再演练另一段脏字：正式备注保持实推值，不被演练覆盖。
    rd2 = _drill(d.client, d.headers, d.sid, "SECOND_DIRTY_演练脏字")
    assert rd2.status_code == 200, rd2.text
    with connect() as conn:
        note_final = conn.execute("SELECT note FROM screens WHERE id=?", (d.sid,)).fetchone()["note"]
    assert note_final == REAL_NOTE


def test_演练正式区零增量_不新增屏幕或正式发布(演练模式仍改写正式备注):
    d = 演练模式仍改写正式备注
    with connect() as conn:
        screens_before = conn.execute("SELECT COUNT(*) c FROM screens").fetchone()["c"]
        pubs_before = conn.execute("SELECT COUNT(*) c FROM publications WHERE official=1").fetchone()["c"]

    r = _drill(d.client, d.headers, d.sid, d.dirty, playlist_id=1, window_id=1)
    assert r.status_code == 200, r.text

    with connect() as conn:
        screens_after = conn.execute("SELECT COUNT(*) c FROM screens").fetchone()["c"]
        pubs_after = conn.execute("SELECT COUNT(*) c FROM publications WHERE official=1").fetchone()["c"]
        note = conn.execute("SELECT note FROM screens WHERE id=?", (d.sid,)).fetchone()["note"]

    assert screens_after == screens_before
    assert pubs_after == pubs_before
    assert note == d.original_note


def test_裸空桩探针仍保持501_不破坏既有契约(client):
    # 无任何推屏指令字段的裸探针，维持 README/冒烟里的固定 501。
    h = _login(client, "admin", "admin123")
    r = client.post("/api/stubs/remote_push", headers=h, json={"screen_id": 1})
    assert r.status_code == 501
