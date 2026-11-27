"""审计与辅助查询。

signboard-12：补充“复用调用方连接/事务”的 insert_* 助手，使正式备注写入、
变更历史、审计能在同一个 sqlite 事务内提交（同事务联查、账实同源）。
原 write_audit / write_history 签名保持不变，既有调用方零改动。
"""
from __future__ import annotations

import json
import time
from fastapi import APIRouter, Header

from auth import current_user
from db import connect, rows_to_list

router = APIRouter(prefix="/api/audit", tags=["audit"])


def insert_audit(conn, actor: str, action: str, payload: dict) -> None:
    """在调用方给定的连接/事务内写审计，不单独提交。"""
    conn.execute(
        "INSERT INTO audit_log(actor,action,payload,created_at) VALUES(?,?,?,?)",
        (actor, action, json.dumps(payload, ensure_ascii=False), time.time()),
    )


def write_audit(actor: str, action: str, payload: dict) -> None:
    with connect() as conn:
        insert_audit(conn, actor, action, payload)
        conn.commit()


def insert_history(
    conn, entity_kind: str, entity_id: int, action: str, detail: str, actor: str
) -> None:
    """在调用方给定的连接/事务内写变更历史，不单独提交。"""
    conn.execute(
        "INSERT INTO change_history(entity_kind,entity_id,action,detail,actor,created_at) VALUES(?,?,?,?,?,?)",
        (entity_kind, entity_id, action, detail, actor, time.time()),
    )


def write_history(entity_kind: str, entity_id: int, action: str, detail: str, actor: str) -> None:
    with connect() as conn:
        insert_history(conn, entity_kind, entity_id, action, detail, actor)
        conn.commit()


@router.get("/recent")
def recent(limit: int = 50, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
    return {"items": rows_to_list(rows)}
