"""分区缺口视图与补读规划的纯算法测试。"""
from __future__ import annotations

import sys
sys.path.insert(0, '.')

from app.compute import (
    intersect_ranges,
    merge_offset_ranges,
    offset_gap_view,
    plan_recovery_reads,
)


class TestMergeOffsetRanges:
    def test_merges_continuous_points(self):
        assert merge_offset_ranges([103, 101, 102, 104]) == [
            {"start": 101, "end": 104, "count": 4},
        ]

    def test_keeps_gaps_separate(self):
        assert merge_offset_ranges([101, 104]) == [
            {"start": 101, "end": 101, "count": 1},
            {"start": 104, "end": 104, "count": 1},
        ]

    def test_dedup_points(self):
        assert merge_offset_ranges([101, 101, 102]) == [
            {"start": 101, "end": 102, "count": 2},
        ]


class TestIntersectRanges:
    def test_overlap(self):
        result = intersect_ranges(
            [{"start": 101, "end": 104, "count": 4}],
            [{"start": 103, "end": 105, "count": 3}],
        )
        assert result == [{"start": 103, "end": 104, "count": 2}]

    def test_no_overlap(self):
        assert intersect_ranges(
            [{"start": 1, "end": 2, "count": 2}],
            [{"start": 5, "end": 6, "count": 2}],
        ) == []


class TestOffsetGapView:
    def test_gap_ranges_and_inflight(self):
        view = offset_gap_view(
            watermarks={"p1": 104},
            confirmed={"p1": [101, 102]},
            base_offsets={"p1": 101},
            inflight={"p1": True},
        )
        row = view[0]
        assert row["partition"] == "p1"
        assert row["gap_ranges"] == [{"start": 103, "end": 104, "count": 2}]
        assert row["unconfirmed_count"] == 2
        assert row["inflight"] is True
        assert row["next_write_offset"] == 105

    def test_partitions_are_isolated(self):
        # 同一个偏移数字在两个分区各自独立解释，互不影响。
        view = offset_gap_view(
            watermarks={"p1": 104, "p2": 104},
            confirmed={"p1": [101, 102, 103, 104], "p2": [101]},
            base_offsets={"p1": 101, "p2": 101},
        )
        by_partition = {row["partition"]: row for row in view}
        assert by_partition["p1"]["unconfirmed_count"] == 0
        assert by_partition["p2"]["unconfirmed_count"] == 3

    def test_no_gap_when_all_confirmed(self):
        view = offset_gap_view(
            watermarks={"p2": 39},
            confirmed={"p2": list(range(32, 40))},
            base_offsets={"p2": 32},
        )
        assert view[0]["gap_ranges"] == []
        assert view[0]["confirmed_count"] == 8


class TestPlanRecoveryReads:
    def test_late_confirmation_trimmed(self):
        gap_row = {"gap_ranges": [{"start": 103, "end": 104, "count": 2}]}
        read_ranges, skipped = plan_recovery_reads(
            [{"start": 101, "end": 104, "count": 4}], gap_row,
        )
        assert read_ranges == [{"start": 103, "end": 104, "count": 2}]
        assert skipped == [101, 102]

    def test_full_gap_request(self):
        gap_row = {"gap_ranges": [{"start": 32, "end": 39, "count": 8}]}
        read_ranges, skipped = plan_recovery_reads(
            [{"start": 32, "end": 39, "count": 8}], gap_row,
        )
        assert read_ranges == [{"start": 32, "end": 39, "count": 8}]
        assert skipped == []
