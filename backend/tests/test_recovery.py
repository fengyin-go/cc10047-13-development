"""检查点恢复端到端测试：分区缺口视图、迟到确认、补读幂等、断线重试和台账回查。"""
from __future__ import annotations

import sys
sys.path.insert(0, '.')

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.recovery_state import recovery_state

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_recovery_state():
    recovery_state.reset()
    yield
    recovery_state.reset()


class TestPartitionGapView:
    def test_seed_gap_is_p1_101_104_and_p2_32_39(self):
        resp = client.get("/api/stream_checkpoint/gap-view")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        rows = {row["partition"]: row for row in data["partitions"]}
        assert rows["p1"]["gap_ranges"] == [{"start": 101, "end": 104, "count": 4}]
        assert rows["p2"]["gap_ranges"] == [{"start": 32, "end": 39, "count": 8}]
        # 两个分区仍在写入。
        assert set(data["writing_partitions"]) == {"p1", "p2"}
        assert rows["p1"]["next_write_offset"] == 105

    def test_offset_never_crosses_partition(self):
        # p1 确认 101/102 不影响 p2 的 32~39，也不影响 p2 自己的偏移解释。
        resp = client.post(
            "/api/stream_checkpoint/confirm-offset",
            json={"partition": "p1", "offsets": [101, 102]},
        )
        assert resp.json()["ok"] is True
        view = {
            row["partition"]: row
            for row in client.get("/api/stream_checkpoint/gap-view").json()["partitions"]
        }
        assert view["p1"]["gap_ranges"] == [{"start": 103, "end": 104, "count": 2}]
        assert view["p2"]["gap_ranges"] == [{"start": 32, "end": 39, "count": 8}]

    def test_unknown_partition_rejected(self):
        resp = client.post(
            "/api/stream_checkpoint/confirm-offset",
            json={"partition": "p9", "offsets": [101]},
        )
        assert resp.json()["ok"] is False
        assert "不能跨分区" in resp.json()["message"]


class TestLateConfirmationRecovery:
    def test_full_operator_flow(self):
        # 1. 同一缺口出现迟到确认：p1 的 101、102 已实际入湖。
        client.post(
            "/api/stream_checkpoint/confirm-offset",
            json={"partition": "p1", "offsets": [101, 102]},
        )

        # 2. 操作员仍按视图原缺口提交 p1 的 101~104 补读。
        resp = client.post(
            "/api/stream_checkpoint/recovery",
            json={
                "partition": "p1",
                "ranges": [{"start": 101, "end": 104}],
                "idempotency_key": "p1-101-104",
            },
        )
        data = resp.json()
        assert data["ok"] is True
        recovery = data["recovery"]
        # 实际只取 103、104；101、102 因迟到确认被跳过。
        assert recovery["read_ranges"] == [{"start": 103, "end": 104, "count": 2}]
        assert recovery["skipped_late_offsets"] == [101, 102]
        assert recovery["completed"] is True

        # 3. 刷新恢复台账，核对选中区间、读取范围和写入确认。
        ledger = client.get("/api/stream_checkpoint/recovery-ledger").json()
        item = ledger["items"][0]
        assert item["selected_ranges"] == [{"start": 101, "end": 104, "count": 4}]
        assert item["read_ranges"] == [{"start": 103, "end": 104, "count": 2}]
        assert item["write_confirmations"][0]["confirmed_offsets"] == [103, 104]
        assert item["target_table"] == "dwd.orders_lake"

        # 4. 切回视图，p1 缺口清空；p2 缺口原样保留。
        view = {
            row["partition"]: row
            for row in client.get("/api/stream_checkpoint/gap-view").json()["partitions"]
        }
        assert view["p1"]["gap_ranges"] == []
        assert view["p2"]["gap_ranges"] == [{"start": 32, "end": 39, "count": 8}]

        # 5. 检查点详情显示本次补读已经完成。
        detail = client.get("/api/stream_checkpoint/detail/检查点样例1").json()
        assert detail["ok"] is True
        assert detail["all_recovery_done"] is True
        assert detail["recovery_completed"] == 1


class TestConcurrentDuplicateRecovery:
    def test_duplicate_submission_confirms_once(self):
        payload = {
            "partition": "p2",
            "ranges": [{"start": 32, "end": 39}],
            "idempotency_key": "p2-dup",
        }
        first = client.post("/api/stream_checkpoint/recovery", json=payload).json()
        second = client.post("/api/stream_checkpoint/recovery", json=payload).json()
        assert first["duplicate"] is False
        assert second["duplicate"] is True
        assert second["recovery"]["id"] == first["recovery"]["id"]

        ledger = client.get("/api/stream_checkpoint/recovery-ledger").json()
        # 只有一份补读记录，32~39 只确认一份数据。
        assert ledger["total"] == 1
        assert ledger["items"][0]["written_offsets"] == list(range(32, 40))

    def test_duplicate_without_key_still_dedups_by_confirmed_offsets(self):
        payload = {"partition": "p2", "ranges": [{"start": 32, "end": 35}]}
        first = client.post("/api/stream_checkpoint/recovery", json=payload).json()
        second = client.post("/api/stream_checkpoint/recovery", json=payload).json()
        assert first["recovery"]["written_offsets"] == [32, 33, 34, 35]
        # 第二次没有可补的缺口，全部作为迟到/已确认跳过，不会重复写入。
        assert second["recovery"]["written_offsets"] == []
        assert second["recovery"]["skipped_late_offsets"] == [32, 33, 34, 35]


class TestDisconnectRetry:
    def test_retry_keeps_unconfirmed_and_other_partition_reads(self):
        # 带断线的补读：101 写入后断线，102~104 保留为未确认。
        resp = client.post(
            "/api/stream_checkpoint/recovery",
            json={
                "partition": "p1",
                "ranges": [{"start": 101, "end": 104}],
                "simulate_disconnect": True,
            },
        )
        recovery = resp.json()["recovery"]
        assert recovery["status"] == "待重试"
        assert recovery["completed"] is False
        assert recovery["written_offsets"] == [101]
        assert recovery["pending_ranges"] == [{"start": 102, "end": 104, "count": 3}]
        assert recovery["write_confirmations"][-1]["disconnected"] is True

        # 其他分区照常读取，不受 p1 断线影响。
        p2 = client.post(
            "/api/stream_checkpoint/recovery",
            json={"partition": "p2", "ranges": [{"start": 32, "end": 39}]},
        ).json()
        assert p2["recovery"]["completed"] is True

        # 重试只补尚未确认的 102~104，101 不会被重复写入。
        retry = client.post(
            f"/api/stream_checkpoint/recovery/{recovery['id']}/retry",
        ).json()
        retried = retry["recovery"]
        assert retried["status"] == "已完成"
        assert retried["written_offsets"] == [101, 102, 103, 104]
        confirmations = retried["write_confirmations"]
        assert confirmations[-1]["kind"] == "retry"
        assert confirmations[-1]["confirmed_offsets"] == [102, 103, 104]
        # 重试没有重复确认 101。
        assert 101 not in confirmations[-1]["confirmed_offsets"]

        detail = client.get("/api/stream_checkpoint/detail/检查点样例1").json()
        assert detail["all_recovery_done"] is True

    def test_retry_after_late_confirmation(self):
        recovery = client.post(
            "/api/stream_checkpoint/recovery",
            json={
                "partition": "p1",
                "ranges": [{"start": 101, "end": 104}],
                "simulate_disconnect": True,
            },
        ).json()["recovery"]
        # 断线期间 102 由正常链路迟到确认入湖。
        client.post(
            "/api/stream_checkpoint/confirm-offset",
            json={"partition": "p1", "offsets": [102]},
        )
        retry = client.post(
            f"/api/stream_checkpoint/recovery/{recovery['id']}/retry",
        ).json()["recovery"]
        assert retry["status"] == "已完成"
        assert 102 in retry["skipped_late_offsets"]
        assert retry["written_offsets"] == [101, 103, 104]
