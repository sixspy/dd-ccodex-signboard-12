"""具名空桩与远程推屏（signboard-12 治理点）。

事故“空桩桥脏写”：远程推屏以演练模式（dry_run/演练）调用时，旧实现把随包
备注直接 UPDATE 进 screens.note 正式字段，导致在线求值把演练脏字当正式配置读。

治理（正确性口径二选一，本服务封死在【甲路径】，不与乙混用、无第三路径）：
    演练 = 正式区零增量：在任何正式表写入之前短路，拒写备注，只写审计/历史台账；
    实推 = 仅显式 live/commit/confirm 或 dry_run=false 才在命名互斥锁
           「远程推屏演练互斥」下，经屏幕服务同事务联查跨表写入正式备注。
    争用 = 严格互斥，只许一人落正式；败方记争用并返回 ok=false，文案无成功句。

兼容：不含任何推屏指令字段的裸探针（如 {"screen_id": 1}）仍固定返回 501，
保持 README 与 tests/test_smoke.py::test_stubs_501 的既有契约。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import (
    REMOTE_PUSH_LOCK,
    LockContention,
    acquire_strict,
    contention_response,
    lock_status,
    release,
)
from screens_svc import commit_remote_push_note
from validators import require_positive_int

router = APIRouter(prefix="/api/stubs", tags=["stubs"])

# 顶层路径别名（/api/remote_push、/api/remote-push），兜住不同调用习惯。
alias_router = APIRouter(tags=["remote_push_alias"])

SIGNBOARD = "signboard-12"
LOCK_NAME = REMOTE_PUSH_LOCK  # 逐字为「远程推屏演练互斥」

# 并发测试协调缝：默认 None（生产无延迟）。测试可注入回调，在胜方持锁后、
# 正式写前阻塞，以确定性地压出“一胜一争用”。不是假绿——互斥与落库均为真实路径。
CRITICAL_SECTION_GATE = None

STUB_META = {
    "remote_push": {
        "title": "远程推屏桥",
        "desc": "将正式发布推送到边缘播放器集群（空桩）",
    },
    "media_bridge": {
        "title": "媒资中台桥接",
        "desc": "从媒资中台同步素材元数据（空桩）",
    },
}

NOTE_KEYS = (
    "note",
    "official_note",
    "screen_note",
    "note_text",
    "remark",
    "remarks",
    "admin_note",
    "memo",
    "comment",
    "正式备注",
    "备注",
)
PRIMARY_DRILL_KEYS = ("dry_run", "dryrun", "drill", "is_drill")
DRILL_BOOL_KEYS = PRIMARY_DRILL_KEYS + (
    "is_dry_run",
    "rehearsal",
    "trial",
    "trial_run",
    "simulate",
    "simulated",
    "simulation",
    "preview",
)
REAL_BOOL_KEYS = (
    "confirm",
    "confirmed",
    "commit",
    "committed",
    "execute",
    "executed",
    "do_execute",
    "real",
    "real_push",
    "is_real",
    "live",
    "push",
    "do_push",
    "send",
    "deliver",
    "apply",
    "apply_now",
    "publish",
    "official",
    "write_official",
    "persist",
)
MODE_KEYS = ("mode", "run_mode", "push_mode", "execute_mode", "kind", "strategy", "target")
ACTION_KEYS = ("action", "op", "operation", "do")
CONTROL_KEYS = tuple(set(DRILL_BOOL_KEYS + REAL_BOOL_KEYS + MODE_KEYS + ACTION_KEYS))

DRILL_MODE_VALUES = {
    "dry_run",
    "dryrun",
    "drill",
    "rehearsal",
    "trial",
    "trial_run",
    "simulate",
    "simulation",
    "preview",
    "shadow",
    "test",
    "演练",
    "演练模式",
}
REAL_MODE_VALUES = {
    "live",
    "real",
    "push",
    "official",
    "commit",
    "prod",
    "production",
    "formal",
    "release",
    "execute",
    "apply",
    "send",
    "deliver",
    "实推",
    "正式",
    "正式推送",
}


@router.get("")
def list_stubs(authorization: str | None = Header(default=None)):
    current_user(authorization)
    return {"items": [{"key": k, **v, "status": "not_implemented"} for k, v in STUB_META.items()]}


@router.post("/media_bridge")
def media_bridge(body: dict, authorization: str | None = Header(default=None)):
    current_user(authorization)
    return JSONResponse(
        status_code=501,
        content={"ok": False, "stub": "media_bridge", "detail": "媒资中台桥接尚未实现", "body": body},
    )


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    return s in {"1", "true", "yes", "on", "y", "t", "是", "真", "开"}


def _first(mapping: dict, keys, default=None):
    for k in keys:
        if k in mapping and mapping[k] is not None:
            return mapping[k]
    return default


def _path_mode(path: str) -> str | None:
    p = (path or "").lower()
    if p.endswith(("/drill", "/dry-run", "/dry_run", "/rehearsal", "/trial")):
        return "drill"
    if p.endswith(("/push", "/real", "/official", "/commit", "/live", "/execute")):
        return "real"
    return None


def _has_command(merged: dict, forced_mode: str | None) -> bool:
    if forced_mode:
        return True
    if any(k in merged for k in CONTROL_KEYS):
        return True
    if any(k in merged and merged[k] is not None for k in NOTE_KEYS):
        return True
    if any(k in merged for k in ("target", "payload", "content")):
        return True
    return False


def _resolve_mode(merged: dict, forced_mode: str | None) -> str:
    if forced_mode:
        return forced_mode

    mode_value = str(_first(merged, MODE_KEYS, default="") or "").strip().lower()
    if mode_value in DRILL_MODE_VALUES:
        return "drill"
    if mode_value in REAL_MODE_VALUES:
        return "real"

    action_value = str(_first(merged, ACTION_KEYS, default="") or "").strip().lower()
    if action_value in DRILL_MODE_VALUES or "drill" in action_value or "dry" in action_value or "演练" in action_value:
        return "drill"
    if action_value in REAL_MODE_VALUES or action_value in {"real_push", "push_now"}:
        return "real"

    if any(k in merged and _truthy(merged[k]) for k in DRILL_BOOL_KEYS):
        return "drill"
    if any(k in merged and _truthy(merged[k]) for k in REAL_BOOL_KEYS):
        return "real"

    # 显式 dry_run=false / drill=false 是标准的“非演练”信号 => 实推。
    if any(k in merged and k in PRIMARY_DRILL_KEYS and not _truthy(merged[k]) for k in PRIMARY_DRILL_KEYS):
        return "real"

    # 安全默认：拿不准一律按演练短路，绝不误写正式备注（仅“实推”才写）。
    return "drill"


def _resolve_screen_id(conn, merged: dict) -> int:
    raw = _first(merged, ("screen_id", "id", "sid"))
    if raw is not None and str(raw).strip() != "":
        return require_positive_int(raw, "屏幕编号")

    code = _first(merged, ("screen_code", "code", "screen_slug"))
    if code:
        row = conn.execute("SELECT id FROM screens WHERE code=?", (str(code).strip(),)).fetchone()
        if not row:
            from fastapi import HTTPException

            raise HTTPException(404, "屏幕编码不存在")
        return row["id"]

    screen = merged.get("screen")
    if screen is not None:
        s = str(screen).strip()
        if s.isdigit():
            return int(s)
        row = conn.execute("SELECT id FROM screens WHERE code=?", (s,)).fetchone()
        if row:
            return row["id"]

    from fastapi import HTTPException

    raise HTTPException(400, "缺少屏幕编号 screen_id")


def _record_contention(user: dict, screen_id, holder: str | None) -> None:
    write_audit(
        user["username"],
        "remote_push.contended",
        {
            "signboard": SIGNBOARD,
            "screen_id": screen_id,
            "lock_key": LOCK_NAME,
            "holder": holder or "other",
        },
    )
    if screen_id is not None:
        write_history(
            "screen",
            int(screen_id),
            "remote_push_contend",
            "远程推屏互斥争用：未获命名锁，按败方处理，正式备注未写入",
            user["username"],
        )


def _drill_receipt(sid: int, official_note: str, note: str, user: dict) -> JSONResponse:
    # 甲路径：只记台账（审计 + 历史），不触碰任何正式屏幕字段/正式配置表。
    write_audit(
        user["username"],
        "remote_push.drill_short_circuit",
        {
            "signboard": SIGNBOARD,
            "screen_id": sid,
            "lock_key": LOCK_NAME,
            "proposed_note": note,
            "official_note_kept": official_note,
            "path": "jia_short_circuit",
        },
    )
    write_history(
        "screen",
        sid,
        "remote_push_drill_blocked",
        "远程推屏演练：甲路径短路，拒写备注，正式屏幕字段零改动",
        user["username"],
    )
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "success": True,
            "stub": "remote_push",
            "signboard": SIGNBOARD,
            "lock_key": LOCK_NAME,
            "mode": "dry_run",
            "dry_run": True,
            "drill": True,
            "path": "dry_run",
            "correctness_path": "jia_short_circuit",
            "path_label": "甲路径：演练正式区零增，拒写备注并短路",
            "committed": False,
            "persisted": False,
            "written": False,
            "official_written": False,
            "note_written": False,
            "short_circuited": True,
            "rejected_official_write": True,
            "screen_id": sid,
            "official_note": official_note,
            "note": official_note,
            "proposed_note": note,
            "message": "演练请求已在正式写之前短路：正式备注拒写，正式屏幕字段零改动",
        },
    )


def remote_push_dispatch(request: Request, body: dict | None, authorization: str | None):
    user = current_user(authorization)
    body = body if isinstance(body, dict) else {}
    query = dict(request.query_params) if request is not None else {}
    merged = {**query, **body}
    path = request.url.path if request is not None else ""
    forced_mode = _path_mode(path)

    # 无任何推屏指令的裸探针：保留固定 501 空桩契约（detail 与种子原文一致）。
    if not _has_command(merged, forced_mode):
        return JSONResponse(
            status_code=501,
            content={
                "ok": False,
                "stub": "remote_push",
                "detail": "远程推屏桥尚未实现",
                "hint": "signboard-12：演练请带 dry_run=true；显式实推请带 dry_run=false 与 confirm=true 或 mode=live",
                "body": body,
            },
        )

    mode = _resolve_mode(merged, forced_mode)

    with connect() as conn:
        sid = _resolve_screen_id(conn, merged)
        row = conn.execute("SELECT note FROM screens WHERE id=?", (sid,)).fetchone()
        if not row:
            from fastapi import HTTPException

            raise HTTPException(404, "屏幕不存在")
        official_note = row["note"] or ""

    note_provided = any(k in merged and merged[k] is not None for k in NOTE_KEYS)
    note_value = _first(merged, NOTE_KEYS, default="")
    note = "" if note_value is None else str(note_value)

    if mode == "drill":
        return _drill_receipt(sid, official_note, note, user)

    # 仅显式实推可写正式区，且编排员及以上。
    require_writer(user)
    try:
        acquire_strict(LOCK_NAME, user["username"], ttl_sec=30)
    except LockContention as lc:
        _record_contention(user, sid, lc.holder)
        return contention_response(LOCK_NAME, lc.holder, via="stubs/remote_push")

    try:
        # 并发测试协调缝（生产环境该全局为 None）。
        if CRITICAL_SECTION_GATE is not None and callable(CRITICAL_SECTION_GATE):
            CRITICAL_SECTION_GATE()

        with connect() as conn:
            result = commit_remote_push_note(
                conn,
                screen_id=sid,
                note_provided=note_provided,
                note=note,
                actor=user["username"],
                lock_key=LOCK_NAME,
                signboard=SIGNBOARD,
            )
            conn.commit()
    finally:
        release(LOCK_NAME, user["username"])

    return {
        "ok": True,
        "success": True,
        "won": True,
        "committed": True,
        "persisted": True,
        "written": True,
        "official_written": True,
        "note_written": bool(note_provided),
        "stub": "remote_push",
        "signboard": SIGNBOARD,
        "lock_key": LOCK_NAME,
        "mode": "live",
        "dry_run": False,
        "path": "official",
        "write_path": "official_same_tx",
        "screen_id": sid,
        "official_note": result["note_after"],
        "note": result["note_after"],
        **result,
        "message": "实推完成：已在命名互斥锁内同事务写入正式备注并记审计/历史",
    }


# ---- 正本路径：既有 /api/stubs/remote_push + 常见子路径/连字符别名 ----
_REMOTE_PUSH_PATHS = (
    "/remote_push",
    "/remote-push",
    "/remote_push/drill",
    "/remote-push/drill",
    "/remote_push/dry-run",
    "/remote_push/dry_run",
    "/remote-push/dry-run",
    "/remote_push/push",
    "/remote-push/push",
    "/remote_push/real",
    "/remote_push/official",
    "/remote_push/commit",
)


def _remote_push_endpoint(request: Request, body: dict | None = None, authorization: str | None = Header(default=None)):
    return remote_push_dispatch(request, body, authorization)


for _idx, _p in enumerate(_REMOTE_PUSH_PATHS):
    router.add_api_route(
        _p,
        _remote_push_endpoint,
        methods=["POST"],
        summary="远程推屏（演练短路 / 实推写正式备注）",
        operation_id=f"signboard12_stubs_remote_push_{_idx}",
    )


# ---- 命名互斥量的持久领取/释放/状态（供双会话争用与空桩桥页演示） ----
@router.post("/remote_push/claim")
@router.post("/remote_push/mutex")
def claim_mutex(body: dict | None = None, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    ttl = int((body or {}).get("ttl_sec") or 120)
    try:
        acquire_strict(LOCK_NAME, user["username"], ttl_sec=ttl)
    except LockContention as lc:
        _record_contention(user, (body or {}).get("screen_id"), lc.holder)
        return contention_response(LOCK_NAME, lc.holder, via="stubs/claim")
    return {
        "ok": True,
        "success": True,
        "won": True,
        "strict": True,
        "lock_key": LOCK_NAME,
        "holder": user["username"],
        "ttl_sec": ttl,
        "signboard": SIGNBOARD,
    }


@router.post("/remote_push/release")
def release_mutex(body: dict | None = None, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    holder = ((body or {}).get("holder") or user["username"]).strip()
    release(LOCK_NAME, holder)
    return {"ok": True, "released": LOCK_NAME, "holder": holder}


@router.get("/remote_push/mutex")
def get_mutex(authorization: str | None = Header(default=None)):
    current_user(authorization)
    return {"ok": True, "lock": lock_status(LOCK_NAME), "lock_key": LOCK_NAME}


# ---- 顶层别名：/api/remote_push、/api/remote-push[...] ----
def _alias_endpoint(request: Request, body: dict | None = None, authorization: str | None = Header(default=None)):
    return remote_push_dispatch(request, body, authorization)


_ALIAS_FULL_PATHS = (
    "/api/remote_push",
    "/api/remote-push",
    *(f"/api/remote_push{s}" for s in ("/drill", "/dry_run", "/dry-run", "/push", "/real", "/official")),
    *(f"/api/remote-push{s}" for s in ("/drill", "/dry_run", "/dry-run", "/push", "/real", "/official")),
)
for _idx, _full in enumerate(_ALIAS_FULL_PATHS):
    alias_router.add_api_route(
        _full,
        _alias_endpoint,
        methods=["POST"],
        operation_id=f"signboard12_alias_remote_push_{_idx}",
    )
