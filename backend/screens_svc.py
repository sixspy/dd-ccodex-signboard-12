"""屏幕台账。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from audit_svc import insert_audit, insert_history, write_audit, write_history
from auth import current_user, require_writer
from db import connect, rows_to_list
from models import SCREEN_STATUS_LABEL, ZONE_TYPE_LABEL
from validators import require_code, require_screen_status, require_title

router = APIRouter(prefix="/api/screens", tags=["screens"])


def _enrich(row: dict, zone: dict | None = None) -> dict:
    row["status_label"] = SCREEN_STATUS_LABEL.get(row["status"], row["status"])
    if zone:
        row["zone_code"] = zone["code"]
        row["zone_title"] = zone["title"]
        row["zone_type_label"] = ZONE_TYPE_LABEL.get(zone["zone_type"], zone["zone_type"])
        row["risk_level"] = zone["risk_level"]
    return row


def commit_remote_push_note(
    conn,
    *,
    screen_id: int,
    note_provided: bool,
    note: str,
    actor: str,
    lock_key: str,
    signboard: str = "signboard-12",
) -> dict:
    """signboard-12 甲路径下“仅实推”可调用的正式备注写入口。

    必须在调用方已获取命名互斥锁后、于同一事务连接内调用：
    - 联查 screens JOIN zones，确认屏幕与分区账实一致；
    - 核对该屏正式发布数量（跨表账实）；
    - 仅当请求确实携带备注时才 UPDATE screens.note；
    - 变更历史与审计随正式写在同一事务落库，调用方统一 commit。
    演练分支严禁调用本函数。
    """
    screen = conn.execute(
        """
        SELECT s.*, z.code AS zone_code, z.title AS zone_title
        FROM screens s JOIN zones z ON z.id = s.zone_id
        WHERE s.id=?
        """,
        (screen_id,),
    ).fetchone()
    if not screen:
        raise HTTPException(404, "屏幕不存在或未绑定有效分区")

    before = screen["note"] or ""
    after = before
    changed = False
    if note_provided:
        after = note or ""
        changed = after != before
        conn.execute("UPDATE screens SET note=? WHERE id=?", (after, screen_id))

    official_pubs = conn.execute(
        "SELECT COUNT(*) c FROM publications WHERE screen_id=? AND official=1",
        (screen_id,),
    ).fetchone()["c"]

    insert_history(
        conn,
        "screen",
        screen_id,
        "remote_push_official",
        f"远程推屏实推落正式备注（变化={'是' if changed else '否'}，正式发布 {official_pubs} 条）",
        actor,
    )
    insert_audit(
        conn,
        actor,
        "remote_push.official",
        {
            "signboard": signboard,
            "screen_id": screen_id,
            "lock_key": lock_key,
            "note_before": before,
            "note_after": after,
            "changed": changed,
            "official_publications": official_pubs,
        },
    )
    return {
        "screen_id": screen_id,
        "note_before": before,
        "note_after": after,
        "changed": changed,
        "official_publications": official_pubs,
    }


@router.get("")
def list_screens(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, z.code zone_code, z.title zone_title, z.zone_type, z.risk_level
            FROM screens s JOIN zones z ON z.id=s.zone_id
            ORDER BY s.id
            """
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["status_label"] = SCREEN_STATUS_LABEL.get(d["status"], d["status"])
        d["zone_type_label"] = ZONE_TYPE_LABEL.get(d["zone_type"], d["zone_type"])
        items.append(d)
    return {"items": items}


@router.post("")
def create_screen(body: dict, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    code = require_code(body.get("code"), "屏幕编码")
    title = require_title(body.get("title"), "屏幕标题")
    status = require_screen_status(body.get("status") or "offline")
    resolution = (body.get("resolution") or "1920x1080").strip()
    orientation = body.get("orientation") or "landscape"
    zone_code = (body.get("zone_code") or "").strip()
    with connect() as conn:
        zone = conn.execute("SELECT * FROM zones WHERE code=?", (zone_code,)).fetchone()
        if not zone:
            raise HTTPException(404, "分区不存在")
        try:
            cur = conn.execute(
                "INSERT INTO screens(code,title,zone_id,resolution,status,orientation,note) VALUES(?,?,?,?,?,?,?)",
                (code, title, zone["id"], resolution, status, orientation, body.get("note") or ""),
            )
            sid = cur.lastrowid
            conn.commit()
        except Exception as exc:
            raise HTTPException(409, f"屏幕编码冲突: {exc}") from exc
    write_history("screen", sid, "create", f"登记屏幕 {code}", user["username"])
    write_audit(user["username"], "screen.create", {"id": sid, "code": code})
    return {"id": sid, "code": code}


@router.patch("/{screen_id}")
def patch_screen(screen_id: int, body: dict, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    require_writer(user)
    with connect() as conn:
        row = conn.execute("SELECT * FROM screens WHERE id=?", (screen_id,)).fetchone()
        if not row:
            raise HTTPException(404, "屏幕不存在")
        status = require_screen_status(body.get("status", row["status"]))
        title = require_title(body.get("title", row["title"]), "屏幕标题")
        note = body.get("note", row["note"])
        conn.execute(
            "UPDATE screens SET title=?, status=?, note=? WHERE id=?",
            (title, status, note, screen_id),
        )
        conn.commit()
    write_history("screen", screen_id, "patch", f"屏幕状态={status}", user["username"])
    return {"ok": True}
