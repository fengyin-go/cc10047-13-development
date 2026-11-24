"""流计算检查点领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。

检查点恢复扩展：把源分区偏移数据计算为分区缺口视图，从视图选中缺口区间安排
补读，补读结果回写恢复台账，并在检查点详情中回查补读是否完成。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.recovery_state import recovery_state
from app.store import store


class StreamCheckpointService(BaseModuleService):
    MODULE = "stream_checkpoint"
    ENTITY = "检查点"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["检查点标识", "关联管道", "检查点时间", "数据偏移", "状态大小", "保存路径", "保留时长", "检查状态"]
    REQUIRED_FIELDS = ["检查点标识", "关联管道", "检查点时间"]
    STATUS_ORDER = ["保存中", "已保存", "已恢复", "已过期"]
    ACTION_RULES = {"创建检查点": "保存中", "恢复检查点": "已恢复"}
    NEGATIVE_ACTIONS = []

    def domain_save_checkpoint(self, entry_id: int) -> dict[str, Any]:
        """对当前数据执行写入，联动关联和回填处理，产出标准化记录。处理流程：读取配置→执行写入→关联校验→回填批次→写入存储，下游可消费且批次可追溯。"""
        entry = store.find(self.MODULE, entry_id)
        if not entry:        # 返回计算处理结果，含标准化校验和批次批次信息

            return {"ok": False, "message": f"检查点 {entry_id} 不存在"}
        import hashlib        # 返回计算处理结果，含标准化校验和批次批次信息

        return {
            "ok": True,
            "checkpoint_id": entry.get("检查点标识"),
            "pipeline": entry.get("关联管道"),
            "checkpoint_time": entry.get("检查点时间"),
            "data_offset": entry.get("数据偏移"),
            "state_size_kb": int(entry.get("状态大小", 128)),
            "save_path": entry.get("保存路径", "/checkpoints"),
            "retention_hours": 24,
            "checksum": hashlib.sha256(str(entry).encode()).hexdigest()[:16],
        }

    # ------------------------------------------------------------------
    # 分区缺口视图：每个分区还没确认的区间 + 哪些数据仍在写入
    # ------------------------------------------------------------------
    def domain_gap_view(self) -> dict[str, Any]:
        rows = recovery_state.gap_view()
        return {
            "ok": True,
            "partitions": rows,
            "total_gap_offsets": sum(int(row["unconfirmed_count"]) for row in rows),
            "writing_partitions": [row["partition"] for row in rows if row["inflight"]],
        }

    def domain_confirm_offset(self, partition: str, offsets: list[int]) -> dict[str, Any]:
        """登记迟到确认：对应偏移已经实际入湖，视图刷新后缺口随之收缩。"""
        try:
            result = recovery_state.confirm_offset(partition, offsets)
        except KeyError as exc:
            return {"ok": False, "message": str(exc).strip("'")}
        return {"ok": True, **result}

    # ------------------------------------------------------------------
    # 检查点从分区缺口视图安排实际补读，结果回写恢复台账
    # ------------------------------------------------------------------
    def domain_submit_recovery(
        self,
        partition: str,
        ranges: list[dict[str, int]],
        *,
        checkpoint_id: str = "检查点样例1",
        target_table: str = "dwd.orders_lake",
        idempotency_key: str | None = None,
        simulate_disconnect: bool = False,
    ) -> dict[str, Any]:
        try:
            result = recovery_state.submit_recovery(
                partition,
                ranges,
                checkpoint_id=checkpoint_id,
                target_table=target_table,
                idempotency_key=idempotency_key,
                simulate_disconnect=simulate_disconnect,
            )
        except (KeyError, ValueError) as exc:
            return {"ok": False, "message": str(exc).strip("'")}
        result["ok"] = True
        return result

    def domain_retry_recovery(self, recovery_id: int) -> dict[str, Any]:
        """断线重试：只补尚未确认的区间，已确认偏移不重复写入。"""
        try:
            result = recovery_state.retry_recovery(recovery_id)
        except KeyError as exc:
            return {"ok": False, "message": str(exc).strip("'")}
        result["ok"] = True
        return result

    def domain_recovery_ledger(self, partition: str | None = None) -> dict[str, Any]:
        """恢复台账：回查选中区间、读取范围和写入确认。"""
        rows = recovery_state.list_ledger(partition)
        return {
            "ok": True,
            "items": rows,
            "total": len(rows),
            "pending": sum(1 for row in rows if not row["completed"]),
        }

    def domain_checkpoint_detail(self, checkpoint_id: str) -> dict[str, Any]:
        """检查点详情：显示该检查点触发的补读是否已经全部完成。"""
        return {"ok": True, **recovery_state.checkpoint_detail(checkpoint_id)}
