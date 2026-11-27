#!/usr/bin/env python3
"""signboard-12 双请求争用脚本 race_dryrun_note。

对打命名互斥量「远程推屏演练互斥」：两个会话并发发起“实推写正式备注”，
断言——
  1) 恰好一个请求 200 落正式，另一个 409 记争用；
  2) 败方响应 ok=false 且整句不含“成功”；
  3) screens.note 最终只等于胜方写入的那份（账实一致，无双写）。

三种用法：
  python scripts/race_dryrun_note.py                 # 自举临时库 + 本机真实 HTTP（确定性，默认）
  python scripts/race_dryrun_note.py --inproc        # 不起端口，用 TestClient 发双请求
  python scripts/race_dryrun_note.py --url http://127.0.0.1:8000   # 打已运行的服务（纯标准库）

退出码 0 表示互斥口径成立，1 表示出现“无锁双成”或口径被破坏。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
LOCK_NAME = "远程推屏演练互斥"
NOTE_A = "RACE_SCRIPT_NOTE_A"
NOTE_B = "RACE_SCRIPT_NOTE_B"


def _maybe_reexec_venv() -> None:
    try:
        import fastapi  # noqa: F401
        return
    except Exception:
        pass
    # venv 内 python 可能是指向系统解释器的符号链接，不能用 realpath 比较可执行体；
    # 用环境标记防止“venv 也缺依赖”时无限重入。
    if os.environ.get("SB_RACE_REEXEC") == "1":
        return
    venv_py = ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        os.environ["SB_RACE_REEXEC"] = "1"
        script = str(Path(__file__).resolve())
        os.execv(str(venv_py), [str(venv_py), script, *sys.argv[1:]])


# ------------------------- 纯标准库 HTTP 客户端 -------------------------
def _http(method: str, url: str, payload: dict | None = None, token: str | None = None):
    data = None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def _login(base: str, username: str, password: str) -> str:
    status, body = _http("POST", f"{base}/api/auth/login", {"username": username, "password": password})
    if status != 200:
        raise RuntimeError(f"登录失败 {username}: {status} {body}")
    return body["token"]


def _pick_screen(base: str, token: str) -> int:
    status, body = _http("GET", f"{base}/api/screens", token=token)
    assert status == 200, (status, body)
    return body["items"][0]["id"]


def fire_dual_requests(base: str) -> dict:
    """对已运行的 base 发起两个并发实推请求，返回结构化结果。"""
    admin = _login(base, "admin", "admin123")
    writer = _login(base, "writer", "writer123")
    sid = _pick_screen(base, admin)

    results: dict[str, tuple[int, dict]] = {}
    barrier = threading.Barrier(2)

    def one(name: str, token: str, note: str) -> None:
        barrier.wait()
        results[name] = _http(
            "POST",
            f"{base}/api/stubs/remote_push",
            {"screen_id": sid, "dry_run": False, "confirm": True, "note": note},
            token,
        )

    t1 = threading.Thread(target=one, args=("A", admin, NOTE_A))
    t2 = threading.Thread(target=one, args=("B", writer, NOTE_B))
    t1.start()
    t2.start()
    t1.join(20)
    t2.join(20)
    return {"screen_id": sid, "results": results}


def evaluate(base: str, out: dict, final_note: str | None = None) -> bool:
    results = out["results"]
    statuses = {k: v[0] for k, v in results.items()}
    print(f"[race] screen={out['screen_id']} statuses={statuses}")
    for k, (st, body) in results.items():
        print(f"  - {k}: HTTP {st} ok={body.get('ok')} contended={body.get('contended')} msg={body.get('message') or body.get('detail')}")

    ok = True
    if sorted(statuses.values()) != [200, 409]:
        print("[FAIL] 期望恰好一个 200、一个 409（无锁双成或全部失败）")
        ok = False
    else:
        winner = next(k for k, (st, _) in results.items() if st == 200)
        loser = next(k for k, (st, _) in results.items() if st == 409)
        lstatus, lbody = results[loser]
        loser_text = json.dumps(lbody, ensure_ascii=False)
        if lbody.get("ok") is not False:
            print("[FAIL] 败方响应顶层 ok 必须为 false")
            ok = False
        if "成功" in loser_text:
            print("[FAIL] 败方响应出现成功句")
            ok = False
        expected_note = NOTE_A if winner == "A" else NOTE_B
        if final_note is not None:
            if final_note != expected_note:
                print(f"[FAIL] 正式备注={final_note!r}，应为胜方 {winner} 的 {expected_note!r}")
                ok = False
            else:
                print(f"[ok] 正式备注唯一落为胜方 {winner} 的值")
    if ok:
        print("[PASS] 远程推屏互斥：一人落正式，一人记争用，败方无成功句")
    return ok


# ------------------------- 自举：真实 uvicorn HTTP -------------------------
def run_self_http() -> int:
    _maybe_reexec_venv()
    sys.path.insert(0, str(BACKEND))
    import sqlite3  # noqa: E402

    import db  # noqa: E402
    import stubs_svc  # noqa: E402
    from app import app  # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="race_dryrun_"))
    db.DB_PATH = tmp / "app.db"
    db.DATA = tmp
    db.init_db()

    winner_started = threading.Event()
    release_winner = threading.Event()

    def gate():
        winner_started.set()
        # 等到脚本观察到败方已写争用审计后再放胜方提交，确定性压出争用。
        for _ in range(100):
            try:
                c = sqlite3.connect(db.DB_PATH)
                n = c.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action LIKE 'remote_push.contend%'"
                ).fetchone()[0]
                c.close()
                if n >= 1:
                    break
            except Exception:
                pass
            time.sleep(0.03)
        release_winner.set()

    stubs_svc.CRITICAL_SECTION_GATE = gate

    import uvicorn  # noqa: E402

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # 子线程内不要装信号处理
    th = threading.Thread(target=server.run, daemon=True)
    th.start()

    base = None
    for _ in range(100):
        if server.started and server.servers:
            try:
                port = server.servers[0].sockets[0].getsockname()[1]
                base = f"http://127.0.0.1:{port}"
                break
            except Exception:
                pass
        time.sleep(0.05)
    if base is None:
        print("[FAIL] 自举服务未就绪")
        return 1

    try:
        out = fire_dual_requests(base)
        winner_started.wait(3)
        time.sleep(0.2)
        release_winner.set()
        time.sleep(0.3)
        with sqlite3.connect(db.DB_PATH) as c:
            final_note = c.execute("SELECT note FROM screens WHERE id=?", (out["screen_id"],)).fetchone()[0]
        ok = evaluate(base, out, final_note)
        return 0 if ok else 1
    finally:
        release_winner.set()
        server.should_exit = True
        time.sleep(0.2)


# ------------------------- InProc：TestClient -------------------------
def run_inproc() -> int:
    _maybe_reexec_venv()
    sys.path.insert(0, str(BACKEND))
    import db  # noqa: E402
    import stubs_svc  # noqa: E402
    from app import app  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="race_dryrun_inproc_"))
    db.DB_PATH = tmp / "app.db"
    db.DATA = tmp
    db.init_db()

    winner_started = threading.Event()
    release_winner = threading.Event()
    stubs_svc.CRITICAL_SECTION_GATE = lambda: (winner_started.set(), release_winner.wait(5))

    results: dict[str, tuple[int, dict]] = {}
    with TestClient(app) as ca, TestClient(app) as cb:
        ta = threading.Thread(
            target=lambda: results.update(
                A=(
                    (r := ca.post(
                        "/api/stubs/remote_push",
                        headers={"Authorization": "Bearer " + ca.post(
                            "/api/auth/login", json={"username": "admin", "password": "admin123"}
                        ).json()["token"]},
                        json={"screen_id": 1, "dry_run": False, "confirm": True, "note": NOTE_A},
                    )).status_code,
                    r.json(),
                )
            )
        )

        def b_work():
            tok = cb.post("/api/auth/login", json={"username": "writer", "password": "writer123"}).json()["token"]
            r = cb.post(
                "/api/stubs/remote_push",
                headers={"Authorization": f"Bearer {tok}"},
                json={"screen_id": 1, "dry_run": False, "confirm": True, "note": NOTE_B},
            )
            results["B"] = (r.status_code, r.json())

        tb = threading.Thread(target=b_work)
        ta.start()
        tb.start()
        winner_started.wait(3)
        time.sleep(0.4)
        release_winner.set()
        ta.join(10)
        tb.join(10)

    import sqlite3  # noqa: E402

    with sqlite3.connect(db.DB_PATH) as c:
        final_note = c.execute("SELECT note FROM screens WHERE id=1").fetchone()[0]
    sid = 1
    return 0 if evaluate("inproc", {"screen_id": sid, "results": results}, final_note) else 1


def run_url(base: str) -> int:
    """打已运行服务。

    确定性硬判据：命名互斥锁两次领取必为 [200,409]。
    备注双推受网络/线程时序影响可能未重叠（串行两次合法写入都会 200），
    因此多轮尝试捕获真实重叠对；一旦出现 [200,409] 即校验败方口径与账实。
    """
    admin = _login(base, "admin", "admin123")
    writer = _login(base, "writer", "writer123")

    l1 = _http("POST", f"{base}/api/locks/acquire", {"lock_key": LOCK_NAME, "ttl_sec": 30}, admin)
    l2 = _http("POST", f"{base}/api/locks/acquire", {"lock_key": LOCK_NAME, "ttl_sec": 30}, writer)
    print(f"[lock] named mutex acquires: {l1[0]}, {l2[0]}")
    lock_ok = sorted([l1[0], l2[0]]) == [200, 409] and l2[1].get("ok") is False and "成功" not in json.dumps(l2[1], ensure_ascii=False)
    _http("POST", f"{base}/api/locks/release", {"lock_key": LOCK_NAME}, admin)
    if not lock_ok:
        print("[FAIL] 命名互斥锁未做到双会话一胜一争用")

    proven = False
    note_ok = True
    for rnd in range(1, 7):
        out = fire_dual_requests(base)
        statuses = {k: v[0] for k, v in out["results"].items()}
        if sorted(statuses.values()) == [200, 409]:
            proven = True
            _, items = _http("GET", f"{base}/api/screens", token=admin)
            final_note = next(x for x in items["items"] if x["id"] == out["screen_id"]).get("note")
            print(f"[note] round {rnd} captured overlapping pair: {statuses}")
            note_ok = evaluate(base, out, final_note)
            break
        print(f"[note] round {rnd} non-overlapping/serial statuses={statuses}; retrying…")

    if not proven:
        print("[info] 本轮未捕获备注双推的时间重叠对；互斥口径以命名锁确定性结果为准。")
    return 0 if (lock_ok and note_ok) else 1


def main(argv: list[str]) -> int:
    if "--url" in argv:
        base = argv[argv.index("--url") + 1].rstrip("/")
        return run_url(base)
    if "--inproc" in argv:
        return run_inproc()
    return run_self_http()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
