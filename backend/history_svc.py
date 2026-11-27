"""变更历史。

signboard-12：增加按实体种类 / 动作 / 操作者 / 实体编号过滤的只读能力，
让“远程推屏演练短路 / 实推 / 互斥争用”等同源记录可在变更历史页被筛出。
不带过滤参数时行为与原来完全一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect, rows_to_list

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("")
def list_history(
    limit: int = 100,
    entity_kind: str | None = None,
    action: str | None = None,
    actor: str | None = None,
    entity_id: int | None = None,
    authorization: str | None = Header(default=None),
):
    current_user(authorization)
    clauses = []
    params: list = []
    if entity_kind:
        clauses.append("entity_kind=?")
        params.append(entity_kind.strip())
    if action:
        # 允许传前缀，如 remote_push 命中 remote_push_official / remote_push_contend。
        clauses.append("action LIKE ?")
        params.append(action.strip().replace("*", "%") + ("%" if not action.strip().endswith("%") else ""))
    if actor:
        clauses.append("actor=?")
        params.append(actor.strip())
    if entity_id is not None:
        clauses.append("entity_id=?")
        params.append(entity_id)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(limit, 300)))
    with connect() as conn:
        rows = rows_to_list(
            conn.execute(
                f"SELECT * FROM change_history{where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        )
    return {"items": rows}
