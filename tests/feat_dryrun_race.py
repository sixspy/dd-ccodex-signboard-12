"""signboard-12 后件（红测丙）：双会话抢「远程推屏演练互斥」不得无锁双成。

事故形状：写锁用 INSERT ... ON CONFLICT DO UPDATE，且同持有者可重入；登录令牌
又是按用户名确定性签发，“双会话”常常是同一持有者，于是两个并发请求都成功
（无锁双成），可能各写一次正式备注。

正确口径：命名互斥量「远程推屏演练互斥」严格排他——一对争用中只许一人落
正式，另一人记争用（audit/history）并返回失败口径，败方响应禁止出现成功句。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

LOCK_NAME = "远程推屏演练互斥"
NOTE_A = "RACE_NOTE_A_甲方落正式"
NOTE_B = "RACE_NOTE_B_乙方落正式"

# 并发临界区协调缝的名字（业务码里默认 None，测试用它确定性地压住胜方）。
GATE_ATTR = "CRITICAL_SECTION_GATE"


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


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _first_screen_id(client, headers):
    items = client.get("/api/screens", headers=headers).json()["items"]
    return items[0]["id"]


def _real_body(sid, note):
    return {"screen_id": sid, "dry_run": False, "confirm": True, "note": note}


def test_同名双会话抢命名互斥_只能一个200一个409(client):
    # 两次登录同一编排员：种子的确定性 token + 同持有者 upsert 会无锁双成。
    h1 = _login(client, "writer", "writer123")
    h2 = _login(client, "writer", "writer123")

    r1 = client.post("/api/locks/acquire", headers=h1, json={"lock_key": LOCK_NAME, "ttl_sec": 60})
    r2 = client.post("/api/locks/acquire", headers=h2, json={"lock_key": LOCK_NAME, "ttl_sec": 60})

    statuses = sorted([r1.status_code, r2.status_code])
    # 红：种子为 [200, 200]（无锁双成）；绿：[200, 409]。
    assert statuses == [200, 409], f"双会话无锁双成: {[(r1.status_code, r1.text), (r2.status_code, r2.text)]}"

    loser = r2 if r2.status_code == 409 else r1
    body = loser.json()
    assert body.get("ok") is False
    # 败方禁成功句：整条响应不得出现“成功”，也不得带 ok=true。
    assert "成功" not in loser.text

    # 清理本用例持有的锁。
    win = r1 if r1.status_code == 200 else r2
    holder = win.json().get("holder") or "writer"
    client.post("/api/locks/release", headers=h1, json={"lock_key": LOCK_NAME, "holder": holder})


def test_异账号双会话抢命名互斥_同样一胜一争用(client):
    ha = _login(client, "admin", "admin123")
    hw = _login(client, "writer", "writer123")
    r1 = client.post("/api/locks/acquire", headers=ha, json={"lock_key": LOCK_NAME})
    r2 = client.post("/api/locks/acquire", headers=hw, json={"lock_key": LOCK_NAME})
    assert {r1.status_code, r2.status_code} == {200, 409}
    loser = r2 if r2.status_code == 409 else r1
    assert loser.json().get("ok") is False
    assert "成功" not in loser.text


def test_acquire_strict原语_八线程仅一个胜者(tmp_path, monkeypatch):
    db = tmp_path / "app.db"
    monkeypatch.setattr("db.DB_PATH", db)
    monkeypatch.setattr("db.DATA", tmp_path)
    init_db()

    from locks import acquire_strict  # 红：种子无此原语 -> ImportError（红）

    barrier = threading.Barrier(8)
    wins, loses = [], []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        try:
            acquire_strict("remote_push:strict:primitive", f"holder-{i}", ttl_sec=30)
            with lock:
                wins.append(i)
        except Exception:
            with lock:
                loses.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)

    assert len(wins) == 1, f"严格互斥被击穿，胜者={wins}"
    assert len(loses) == 7


def test_双请求实推争用_只一人落正式_败方记争用且无成功句(client, monkeypatch):
    import stubs_svc

    admin = _login(client, "admin", "admin123")
    writer = _login(client, "writer", "writer123")
    sid = _first_screen_id(client, admin)

    winner_in = threading.Event()
    release_winner = threading.Event()

    def gate():
        # 仅锁的胜者会在临界区内调用；压住它直到我们观察到败方已记争用。
        winner_in.set()
        release_winner.wait(timeout=10)

    monkeypatch.setattr(stubs_svc, GATE_ATTR, gate, raising=False)

    outcomes = {}

    with TestClient(app) as cA, TestClient(app) as cB:

        def push(name, c, who, note):
            r = c.post("/api/stubs/remote_push", headers=who, json=_real_body(sid, note))
            try:
                payload = r.json()
            except Exception:
                payload = {"raw": r.text}
            outcomes[name] = {"status": r.status_code, "body": payload, "text": r.text}

        ta = threading.Thread(target=push, args=("A", cA, admin, NOTE_A))
        tb = threading.Thread(target=push, args=("B", cB, writer, NOTE_B))
        ta.start()
        tb.start()

        entered = winner_in.wait(timeout=3)
        try:
            # 红：种子 remote_push 直接 501，没有任何请求进入正式写临界区。
            assert entered, "没有请求进入持锁临界区：空桩 501 或缺少互斥/正式写实现"

            contentions = 0
            for _ in range(100):
                with connect() as conn:
                    contentions = conn.execute(
                        "SELECT COUNT(*) c FROM audit_log WHERE action LIKE 'remote_push.contend%'"
                    ).fetchone()["c"]
                if contentions >= 1:
                    break
                release_winner.wait(0.03)
            assert contentions >= 1, "败方未记录争用审计"
        finally:
            release_winner.set()
            ta.join(10)
            tb.join(10)

    assert set(outcomes) == {"A", "B"}
    statuses = {k: v["status"] for k, v in outcomes.items()}
    assert sorted(statuses.values()) == [200, 409], statuses

    winner_name = next(k for k, v in outcomes.items() if v["status"] == 200)
    loser_name = next(k for k, v in outcomes.items() if v["status"] == 409)

    assert outcomes[winner_name]["body"].get("ok") is True
    loser_body = outcomes[loser_name]["body"]
    assert loser_body.get("ok") is False
    assert loser_body.get("contended") is True or "锁定" in outcomes[loser_name]["text"] or "争用" in outcomes[loser_name]["text"]
    # 败方整句禁止“成功”。
    assert "成功" not in outcomes[loser_name]["text"]

    expected_note = NOTE_A if winner_name == "A" else NOTE_B
    with connect() as conn:
        final_note = conn.execute("SELECT note FROM screens WHERE id=?", (sid,)).fetchone()["note"]
        official_audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_log WHERE action='remote_push.official'"
        ).fetchone()["c"]

    assert final_note == expected_note
    assert official_audits == 1
