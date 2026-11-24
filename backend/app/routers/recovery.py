"""检查点恢复路由：分区缺口视图、补读安排、迟到确认、断线重试和台账回查。

固定路径必须先于通用资源路由 /api/{module} 注册，否则会被动态模块路径抢先匹配。
偏移一律携带分区参数，服务端按 (分区, 偏移) 解释，禁止跨分区复用偏移。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/stream_checkpoint", tags=["检查点恢复"])


class RangePayload(BaseModel):
    start: int
    end: int


class ConfirmPayload(BaseModel):
    partition: str
    offsets: list[int] = Field(default_factory=list)


class RecoveryPayload(BaseModel):
    partition: str
    ranges: list[RangePayload]
    checkpoint_id: str = "检查点样例1"
    target_table: str = "dwd.orders_lake"
    idempotency_key: str | None = None
    simulate_disconnect: bool = False


def _service():
    service = service_for("stream_checkpoint")
    if service is None:
        raise RuntimeError("stream_checkpoint 服务未注册")
    return service


@router.get("/gap-view")
def gap_view() -> dict[str, Any]:
    """分区缺口视图：列出每个分区还没确认的区间和仍在写入的分区。"""
    return _service().domain_gap_view()


@router.post("/confirm-offset")
def confirm_offset(payload: ConfirmPayload) -> dict[str, Any]:
    """登记迟到确认：数据已实际入湖，刷新视图后对应缺口被剔除。"""
    return _service().domain_confirm_offset(payload.partition, payload.offsets)


@router.post("/recovery")
def submit_recovery(payload: RecoveryPayload) -> dict[str, Any]:
    """从缺口视图选中区间提交补读，补读结果直接回写恢复台账。"""
    return _service().domain_submit_recovery(
        payload.partition,
        [rng.model_dump() for rng in payload.ranges],
        checkpoint_id=payload.checkpoint_id,
        target_table=payload.target_table,
        idempotency_key=payload.idempotency_key,
        simulate_disconnect=payload.simulate_disconnect,
    )


@router.get("/recovery-ledger")
def recovery_ledger(partition: str | None = None) -> dict[str, Any]:
    """恢复台账：回查选中区间、实际读取范围和写入确认明细。"""
    return _service().domain_recovery_ledger(partition)


@router.post("/recovery/{recovery_id}/retry")
def retry_recovery(recovery_id: int) -> dict[str, Any]:
    """断线重试：只补尚未确认的区间，其他分区照常读取。"""
    return _service().domain_retry_recovery(recovery_id)


@router.get("/detail/{checkpoint_id}")
def checkpoint_detail(checkpoint_id: str) -> dict[str, Any]:
    """检查点详情：显示本次补读是否已经完成，并附带最新缺口视图。"""
    return _service().domain_checkpoint_detail(checkpoint_id)
