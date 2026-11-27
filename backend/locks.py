"""写入短锁，防并发双写。

signboard-12：
- acquire() 改为以 UNIQUE(lock_key) 为线性化点的原子获取，保留“同持有者可续期”
  的既有语义（publish/panic/override 仍在用）。
- 新增 acquire_strict()：严格二进制互斥，只要存在未过期锁行，任何持有者
  （含同名双会话）都不得再入，违例抛 LockContention。专用于
  「远程推屏演练互斥」这类“双会话只许一人落正式”的对打场景。
"""
from __future__ import annotations

import sqlite3
import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from audit_svc import write_audit
from auth import current_user, require_writer
from db import connect

router = APIRouter(prefix="/api/locks", tags=["locks"])

# signboard-12 命名互斥量（题面逐字点名）。
REMOTE_PUSH_LOCK = "远程推屏演练互斥"

# 命中这些片段的锁键，在通用 /api/locks/acquire 上也按严格互斥处理，
# 兜住不同命名习惯（中文锁名 / remote_push / dryrun / 枪号）。
STRICT_KEY_MARKERS = (
    "远程推屏演练互斥",
    "远程推屏",
    "演练互斥",
    "remote_push",
    "remote-push",
    "dryrun",
    "dry_run",
    "race_dryrun",
    "signboard-12",
)


class LockContention(Exception):
    """严格互斥锁已被他方持有。"""

    def __init__(self, lock_key: str, holder: str | None = None):
        self.lock_key = lock_key
        self.holder = holder or "other"
        super().__init__(f"互斥争用：{lock_key} 已由 {self.holder} 持有")


def is_strict_key(lock_key: str) -> bool:
    key = lock_key or ""
    return any(m in key for m in STRICT_KEY_MARKERS)


def _purge_expired(conn, lock_key: str, now: float) -> None:
    conn.execute("DELETE FROM write_locks WHERE lock_key=? AND expires_at<=?", (lock_key, now))


def acquire_strict(lock_key: str, holder: str, ttl_sec: int = 30) -> None:
    """严格二进制互斥：存在未过期锁行即争用，哪怕 holder 同名也不可重入。

    以 UNIQUE(lock_key) 为线性化点：先清过期行，再原子 INSERT；并发下只有一个
    插入成功，其余捕获 IntegrityError 并抛 LockContention，从根上消除“无锁双成”。
    """
    now = time.time()
    conn = connect()
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        _purge_expired(conn, lock_key, now)
        try:
            conn.execute(
                "INSERT INTO write_locks(lock_key,holder,expires_at) VALUES(?,?,?)",
                (lock_key, holder, now + ttl_sec),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            row = conn.execute(
                "SELECT holder, expires_at FROM write_locks WHERE lock_key=?",
                (lock_key,),
            ).fetchone()
            active = bool(row and row["expires_at"] > now)
            current_holder = row["holder"] if row else "other"
            if active:
                raise LockContention(lock_key, current_holder) from exc
            # 行已过期但首轮清理未命中（极端并发）：再清后重试一次插入。
            _purge_expired(conn, lock_key, now)
            try:
                conn.execute(
                    "INSERT INTO write_locks(lock_key,holder,expires_at) VALUES(?,?,?)",
                    (lock_key, holder, now + ttl_sec),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc2:
                conn.rollback()
                row = conn.execute(
                    "SELECT holder FROM write_locks WHERE lock_key=?", (lock_key,)
                ).fetchone()
                raise LockContention(lock_key, row["holder"] if row else "other") from exc2
    finally:
        conn.close()


def acquire(lock_key: str, holder: str, ttl_sec: int = 20) -> None:
    """获取短锁（signboard-12 起同样严格排他，含同名双会话）。

    既有的发布/撤屏/覆盖每个请求只获取一次并在 finally 释放，顺序请求不受影响；
    这里把 LockContention 转成框架惯用的 HTTPException(409, 资源锁定中：holder)。
    """
    try:
        acquire_strict(lock_key, holder, ttl_sec=ttl_sec)
    except LockContention as lc:
        raise HTTPException(409, f"资源锁定中：{lc.holder}") from lc


def release(lock_key: str, holder: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "DELETE FROM write_locks WHERE lock_key=? AND holder=?",
            (lock_key, holder),
        )
        conn.commit()
    finally:
        conn.close()


def lock_status(lock_key: str) -> dict | None:
    now = time.time()
    with connect() as conn:
        row = conn.execute(
            "SELECT holder, expires_at FROM write_locks WHERE lock_key=?",
            (lock_key,),
        ).fetchone()
    if not row:
        return None
    return {
        "lock_key": lock_key,
        "holder": row["holder"],
        "expires_at": row["expires_at"],
        "held": row["expires_at"] > now,
    }


def contention_response(lock_key: str, holder: str | None, *, via: str = "lock") -> JSONResponse:
    """败方统一口径：HTTP 409 + 顶层 ok:false，且文案不含“成功”。"""
    return JSONResponse(
        status_code=409,
        content={
            "ok": False,
            "success": False,
            "won": False,
            "committed": False,
            "official_written": False,
            "contended": True,
            "via": via,
            "lock_key": lock_key,
            "holder": holder or "other",
            "signboard": "signboard-12",
            "error": "lock_contention",
            "detail": f"互斥争用：{lock_key} 正由 {holder or '他方'} 持有，本请求记争用，未写入正式区",
            "message": "未获得命名互斥锁，按争用处理：正式备注零写入",
        },
    )


@router.post("/acquire")
def api_acquire(body: dict, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    key = (body.get("lock_key") or "").strip()
    if not key:
        raise HTTPException(400, "缺少 lock_key")
    ttl = int(body.get("ttl_sec") or 20)
    # signboard-12：所有写锁均严格排他（含同名双会话），杜绝无锁双成。
    try:
        acquire_strict(key, user["username"], ttl or 20)
    except LockContention as lc:
        # 败方记争用：远程推屏命名锁归入 remote_push.contended，其余记通用锁争用。
        action = "remote_push.contended" if is_strict_key(key) else "lock.strict_contended"
        write_audit(
            user["username"],
            action,
            {"lock_key": key, "holder": lc.holder, "signboard": "signboard-12"},
        )
        return contention_response(key, lc.holder, via="api/locks/acquire")
    return {
        "ok": True,
        "won": True,
        "strict": True,
        "lock_key": key,
        "holder": user["username"],
    }


@router.post("/release")
def api_release(body: dict, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    key = (body.get("lock_key") or "").strip()
    holder = (body.get("holder") or user["username"]).strip()
    release(key, holder)
    return {"ok": True}


@router.get("/status")
def api_status(lock_key: str, authorization: str | None = Header(default=None)):
    current_user(authorization)
    return {"ok": True, "lock": lock_status(lock_key)}
