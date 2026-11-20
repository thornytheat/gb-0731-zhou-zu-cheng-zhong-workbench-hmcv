"""整车争配解析（M4 整车证据取舍）专项测试。"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.evidence import SourceRow
from app.modules.m2_calibration.crossing import Crossing
from app.modules.m2_calibration.service import Wheel
from app.modules.m1_intake.parse import Trigger
from app.modules.m4_axles.resolver import resolve
from app.modules.m4_axles.service import reconstruct

T0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def source(line):
    return SourceRow("syn.csv", line, line, ())


def trigger(record_id, side, offset_ms, kg):
    line = 100 + offset_ms + (50 if side == "R" else 0)
    return Trigger(
        record_id, "X1", side, T0 + timedelta(milliseconds=offset_ms),
        Decimal(kg), source(line), False,
    )


def wheel(record_id, side, offset_ms, kg):
    return Wheel(trigger(record_id, side, offset_ms, kg), Decimal(kg), "C1", "cert.csv:1")


def crossing():
    return Crossing("X1", "粤X·00001", T0, T0 + timedelta(seconds=3), Decimal("10"), source(1))


def test_more_axles_beats_nearest_greedy_pairing():
    # 全部轮载在 35ms 争配窗内：交叉配对会产生反向时序（轴距违规），
    # 顺序两轴方案在轴距证据上严格占优，贪心挑最近会选错。
    wheels = [
        wheel("X1-1L", "L", 0, "2400"),
        wheel("X1-2L", "L", 20, "2600"),
        wheel("X1-1R", "R", 10, "2450"),
        wheel("X1-2R", "R", 30, "2500"),
    ]
    best, status, ranked, _ = resolve(wheels, crossing(), 35)
    assert status == "resolved"
    assert best.axle_count == 2
    assert [pairing.key for pairing in best.pairings] == [
        ("X1-1L", "X1-1R"), ("X1-2L", "X1-2R"),
    ]
    crossed = next(
        matching for matching in ranked
        if [p.key for p in matching.pairings] == [("X1-1L", "X1-2R"), ("X1-1R", "X1-2L")]
    )
    assert crossed is not best


def test_tied_whole_vehicle_evidence_blocks_resolution():
    # 两条左轮分别与同一右轮配对：成轴数、轴距、偏载、磅单差额完全一致，
    # 仅记录号不同 → 证据并列，不得自行择一。
    wheels = [
        wheel("X1-1L", "L", 0, "2000"),
        wheel("X1-2L", "L", 8, "2000"),
        wheel("X1-1R", "R", 4, "2000"),
    ]
    best, status, ranked, _ = resolve(wheels, crossing(), 35, ticket_kg=Decimal("4000.00"))
    assert status == "blocked"
    one_axle = [matching for matching in ranked if matching.axle_count == 1]
    assert len(one_axle) == 2


def test_blocked_pass_marks_axes_tentative_and_quarantines_contenders():
    wheels = [
        wheel("X1-1L", "L", 0, "2000"),
        wheel("X1-2L", "L", 8, "2000"),
        wheel("X1-1R", "R", 4, "2000"),
    ]
    result = reconstruct({"X1": wheels}, {"X1": crossing()}, {"X1": Decimal("4000.00")})
    row = next(item for item in result["contention"] if item["pass_id"] == "X1")
    assert row["status"] == "blocked"
    assert result["axes"], "挂起轴仍需展示以便复核"
    assert all(axis["contended"] and axis["status"] == "tentative" for axis in result["axes"])
    assert {issue.code for issue in result["issues"]} == {"PAIR_CONTENDED"}


def test_blocked_balance_is_ungraded_and_reconcile_never_matches():
    from app.modules.m5_balance.service import balance

    wheels = [
        wheel("X1-1L", "L", 0, "2000"),
        wheel("X1-2L", "L", 8, "2000"),
        wheel("X1-1R", "R", 4, "2000"),
    ]
    rebuilt = reconstruct({"X1": wheels}, {"X1": crossing()}, {"X1": Decimal("4000.00")})
    grades = balance(rebuilt["axes"])
    assert grades["counts"]["blocked"] == len(rebuilt["axes"])
    assert all(row["level"] == "blocked" and row["percent"] is None for row in grades["rows"])


def test_rerun_with_same_input_is_stable_and_keeps_exclusions():
    wheels = [
        wheel("X1-1L", "L", 0, "2400"),
        wheel("X1-2L", "L", 20, "2600"),
        wheel("X1-1R", "R", 10, "2450"),
        wheel("X1-2R", "R", 30, "2500"),
    ]
    first = reconstruct({"X1": wheels}, {"X1": crossing()})
    second = reconstruct({"X1": wheels}, {"X1": crossing()})
    assert first["contention"] == second["contention"]
    assert [axis["id"] for axis in first["axes"]] == [axis["id"] for axis in second["axes"]]
    # 被排除候选在重跑结果中保持同一处置，不会换一套配对再计一次。
    assert first["contention"][0]["excluded_refs"] == second["contention"][0]["excluded_refs"]


def test_missing_ticket_is_neutral_not_a_winning_evidence():
    wheels = [
        wheel("X1-1L", "L", 0, "2400"),
        wheel("X1-2L", "L", 20, "2600"),
        wheel("X1-1R", "R", 10, "2450"),
        wheel("X1-2R", "R", 30, "2500"),
    ]
    best_none, status_none, _, _ = resolve(wheels, crossing(), 35, ticket_kg=None)
    best_ticket, status_ticket, _, _ = resolve(wheels, crossing(), 35, ticket_kg=Decimal("9999.99"))
    # 缺磅单不改变按成轴数/轴距/偏载得到的最优配对。
    assert [p.key for p in best_none.pairings] == [p.key for p in best_ticket.pairings]
    assert status_none == status_ticket == "resolved"


def test_wheel_record_change_invalidates_and_recomputes_conclusion():
    # 同一轮载载荷变化即新输入：争配结论必须重算，而不是沿用旧配对。
    base = [
        wheel("X1-1L", "L", 0, "2000"),
        wheel("X1-2L", "L", 8, "2000"),
        wheel("X1-1R", "R", 4, "2000"),
    ]
    first = reconstruct({"X1": base}, {"X1": crossing()})
    # 上游修正其中一条左轮载荷，打破对称，裁决可能由 blocked 变为 resolved。
    updated = [
        wheel("X1-1L", "L", 0, "2800"),
        wheel("X1-2L", "L", 8, "2000"),
        wheel("X1-1R", "R", 4, "2000"),
    ]
    second = reconstruct({"X1": updated}, {"X1": crossing()})
    assert first["contention"][0]["status"] == "blocked"
    # 上游修正打破证据并列，整车重算后得到唯一最优，旧的挂起结论不再沿用。
    assert second["contention"][0]["status"] == "resolved"
    assert all(not axis["contended"] for axis in second["axes"])
