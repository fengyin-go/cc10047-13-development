"""检查点恢复队列的内存台账。

围绕「分区缺口视图 → 选中区间补读 → 回写恢复台账 → 检查点详情回查」这条链路
保存运行态数据。所有偏移都以 (分区, 偏移) 二元组作为身份：偏移 101 在 p1 和 p2
是两条互不相关的数据，确认、补读、去重都不会跨分区解释。

并发约束：
- 同一分区同一区间重复提交补读，只会有一份真正写入（幂等令牌去重 + 区间二次裁剪）；
- 断线重试只继续补尚未确认的区间，已经确认的偏移不会被重复写入；
- 一个分区的补读与重试不影响其他分区，其他分区照常读取。
"""
from __future__ import annotations

import threading
from typing import Any

from app.compute import merge_offset_ranges, offset_gap_view, plan_recovery_reads


# 场景初始状态：p1 缺 101~104，p2 缺 32~39，两个分区仍在持续写入。
DEFAULT_BASE_OFFSETS = {"p1": 101, "p2": 32}
DEFAULT_WATERMARKS = {"p1": 104, "p2": 39}


class OffsetRecoveryState:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.base_offsets: dict[str, int] = {**DEFAULT_BASE_OFFSETS}
        self.watermarks: dict[str, int] = {**DEFAULT_WATERMARKS}
        # 已确认（实际入湖）的偏移集合，按分区隔离。
        self.confirmed: dict[str, set[int]] = {
            partition: set(range(0, base)) for partition, base in self.base_offsets.items()
        }
        self.inflight: dict[str, bool] = {partition: True for partition in self.watermarks}
        self.ledger: list[dict[str, Any]] = []
        self._idempotency: dict[str, int] = {}
        self._next_id = 1
        self._next_attempt = 1

    def reset(self) -> None:
        """恢复到场景初始状态，仅供测试和演示重置使用。"""
        with self._lock:
            self.base_offsets = {**DEFAULT_BASE_OFFSETS}
            self.watermarks = {**DEFAULT_WATERMARKS}
            self.confirmed = {
                partition: set(range(0, base))
                for partition, base in self.base_offsets.items()
            }
            self.inflight = {partition: True for partition in self.watermarks}
            self.ledger = []
            self._idempotency = {}
            self._next_id = 1
            self._next_attempt = 1

    # ------------------------------------------------------------------
    # 校验与视图
    # ------------------------------------------------------------------
    def _require_partition(self, partition: str) -> None:
        # 同一偏移不能跨分区解释：未登记的分区直接拒绝，避免偏移被错误复用。
        if partition not in self.watermarks:
            raise KeyError(f"分区 {partition} 未登记，偏移不能跨分区解释")

    def partitions(self) -> list[str]:
        with self._lock:
            return sorted(self.watermarks)

    def gap_view(self) -> list[dict[str, Any]]:
        with self._lock:
            return offset_gap_view(
                self.watermarks,
                {partition: sorted(offsets) for partition, offsets in self.confirmed.items()},
                base_offsets=self.base_offsets,
                inflight=self.inflight,
            )

    def _gap_row(self, partition: str) -> dict[str, Any]:
        for row in self.gap_view():
            if row["partition"] == partition:
                return row
        raise KeyError(f"分区 {partition} 未登记，偏移不能跨分区解释")

    # ------------------------------------------------------------------
    # 迟到确认：数据已经实际入湖，只是确认通知晚到
    # ------------------------------------------------------------------
    def confirm_offset(self, partition: str, offsets: list[int]) -> dict[str, Any]:
        with self._lock:
            self._require_partition(partition)
            base = self.base_offsets[partition]
            accepted: list[int] = []
            already: list[int] = []
            for raw in offsets:
                offset = int(raw)
                if offset < base:
                    continue
                if offset in self.confirmed[partition]:
                    already.append(offset)
                else:
                    self.confirmed[partition].add(offset)
                    accepted.append(offset)
            return {
                "partition": partition,
                "newly_confirmed": sorted(accepted),
                "already_confirmed": sorted(already),
            }

    # ------------------------------------------------------------------
    # 提交补读：检查点从分区缺口视图安排实际补读
    # ------------------------------------------------------------------
    def submit_recovery(
        self,
        partition: str,
        requested_ranges: list[dict[str, int]],
        *,
        checkpoint_id: str = "检查点样例1",
        target_table: str = "dwd.orders_lake",
        idempotency_key: str | None = None,
        simulate_disconnect: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_partition(partition)
            normalized = self._normalize_ranges(requested_ranges)
            if not normalized:
                raise ValueError("补读区间不能为空")

            # 并发提交相同补读：相同幂等键只确认一份数据。
            if idempotency_key and idempotency_key in self._idempotency:
                existing_id = self._idempotency[idempotency_key]
                return {
                    "duplicate": True,
                    "message": "相同补读已提交，仅保留首次写入确认",
                    "recovery": self.get_recovery(existing_id),
                }

            # 用提交时刻的最新缺口裁剪选中区间，迟到确认会在这里被剔除。
            gap_row = self._gap_row(partition)
            read_ranges, skipped_late = plan_recovery_reads(normalized, gap_row)

            record: dict[str, Any] = {
                "id": self._next_id,
                "recovery_id": f"REC-{self._next_id:04d}",
                "partition": partition,
                "checkpoint_id": checkpoint_id,
                "target_table": target_table,
                "selected_ranges": normalized,
                "read_ranges": [],
                "skipped_late_offsets": skipped_late,
                "written_offsets": [],
                "pending_ranges": read_ranges,
                "write_confirmations": [],
                "attempts": 0,
                "status": "待补读",
                "completed": False,
                "disconnect": bool(simulate_disconnect),
                "idempotency_key": idempotency_key,
            }
            self._next_id += 1
            if idempotency_key:
                self._idempotency[idempotency_key] = record["id"]
            self.ledger.append(record)
            self._execute(record, simulate_disconnect=simulate_disconnect)
            return {"duplicate": False, "message": "补读已安排", "recovery": dict(record)}

    # ------------------------------------------------------------------
    # 断线重试：只补尚未确认的区间，其他分区不受影响
    # ------------------------------------------------------------------
    def retry_recovery(self, recovery_id: int) -> dict[str, Any]:
        with self._lock:
            record = self._find(recovery_id)
            if record["completed"]:
                return {"recovery": dict(record), "message": "补读已完成，无需重试"}
            # 重试前按最新确认情况再次裁剪，已经迟到确认的偏移不会重复写入。
            gap_row = self._gap_row(record["partition"])
            remaining, skipped_late = plan_recovery_reads(
                record["pending_ranges"], gap_row,
            )
            record["pending_ranges"] = remaining
            record["skipped_late_offsets"] = sorted(
                set(record["skipped_late_offsets"]) | set(skipped_late)
            )
            self._execute(record, simulate_disconnect=False, is_retry=True)
            return {"recovery": dict(record), "message": "断线重试已完成剩余区间补读"}

    def _execute(
        self,
        record: dict[str, Any],
        *,
        simulate_disconnect: bool,
        is_retry: bool = False,
    ) -> None:
        record["attempts"] += 1
        attempt_no = self._next_attempt
        self._next_attempt += 1
        plan = list(record["pending_ranges"])
        attempt: dict[str, Any] = {
            "attempt": attempt_no,
            "kind": "retry" if is_retry else "initial",
            "planned_ranges": plan,
            "read_offsets": [],
            "written_offsets": [],
            "confirmed_offsets": [],
            "disconnected": False,
        }
        partition = record["partition"]
        # 模拟断线：首个区间写入首个偏移后即中断，剩余区间保持未确认，等待重试。
        disconnect_after: int | None = None
        if simulate_disconnect and plan:
            disconnect_after = plan[0]["start"]

        stopped = False
        for rng in plan:
            if stopped:
                break
            for offset in range(rng["start"], rng["end"] + 1):
                if disconnect_after is not None and offset > disconnect_after:
                    attempt["disconnected"] = True
                    stopped = True
                    break
                # 二次确认裁剪：并发/迟到确认过的偏移只保留一份数据。
                if offset in self.confirmed[partition]:
                    record["skipped_late_offsets"] = sorted(
                        set(record["skipped_late_offsets"]) | {offset}
                    )
                    continue
                attempt["read_offsets"].append(offset)
                attempt["written_offsets"].append(offset)
                self.confirmed[partition].add(offset)
                attempt["confirmed_offsets"].append(offset)

        record["written_offsets"] = sorted(
            set(record["written_offsets"]) | set(attempt["written_offsets"])
        )
        record["read_ranges"] = merge_offset_ranges(record["written_offsets"])
        record["write_confirmations"].append(attempt)
        remaining, _ = plan_recovery_reads(record["pending_ranges"], self._gap_row(partition))
        record["pending_ranges"] = remaining
        if remaining:
            record["status"] = "待重试"
            record["completed"] = False
        else:
            record["status"] = "已完成"
            record["completed"] = True

    # ------------------------------------------------------------------
    # 台账与检查点详情回查
    # ------------------------------------------------------------------
    def list_ledger(self, partition: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self.ledger]
            if partition:
                rows = [row for row in rows if row["partition"] == partition]
            return rows

    def get_recovery(self, recovery_id: int) -> dict[str, Any]:
        with self._lock:
            return dict(self._find(recovery_id))

    def checkpoint_detail(self, checkpoint_id: str) -> dict[str, Any]:
        with self._lock:
            related = [dict(row) for row in self.ledger if row["checkpoint_id"] == checkpoint_id]
            pending = [row for row in related if not row["completed"]]
            return {
                "checkpoint_id": checkpoint_id,
                "recovery_total": len(related),
                "recovery_completed": sum(1 for row in related if row["completed"]),
                "recovery_pending": len(pending),
                "all_recovery_done": len(related) > 0 and not pending,
                "gap_view": self.gap_view(),
                "recoveries": related,
            }

    # ------------------------------------------------------------------
    def _find(self, recovery_id: int) -> dict[str, Any]:
        for row in self.ledger:
            if row["id"] == int(recovery_id):
                return row
        raise KeyError(f"补读记录 {recovery_id} 不存在")

    @staticmethod
    def _normalize_ranges(ranges: list[dict[str, int]]) -> list[dict[str, int]]:
        points: list[int] = []
        for rng in ranges:
            start = int(rng["start"])
            end = int(rng["end"])
            if start > end:
                start, end = end, start
            points.extend(range(start, end + 1))
        return merge_offset_ranges(points)


recovery_state = OffsetRecoveryState()
