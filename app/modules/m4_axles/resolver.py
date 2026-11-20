"""整车争配解析。

同车同侧多条轮载争同一对侧轮载时，不在局部挑最近的一组，而是在整车（车次）
范围内枚举所有满足配对时窗的合法方案，按整车证据字典序取唯一最优：

1. 成轴数（缺轴的方案先淘汰，整车少轴无法核平）；
2. 轴距违规数（MIN_SPACING..MAX_SPACING，并轴组 TANDEM_SPACING）；
3. 偏载轴数（先严重 20%、再预警 10%）；
4. 磅单差额绝对值（无有效磅单时为中性，不参与裁决）；
5. 确定性回退（总偏时、记录号序列），仅用于挂起时稳定展示，不视为已解决。

证据并列时不存在唯一最优，整车次争配保持未解决，交由现场复秤。
"""
from bisect import bisect_right
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.config import (
    MAX_SPACING,
    MIN_SPACING,
    NOTICE_RATIO,
    SEVERE_RATIO,
    TANDEM_SPACING,
)
from app.values import delta_ms, rounded


@dataclass(frozen=True)
class Pairing:
    left: object
    right: object

    @property
    def time(self):
        return self.left.trigger.time + timedelta(microseconds=self.skew_ms * 500)

    @property
    def skew_ms(self):
        return delta_ms(self.right.trigger.time, self.left.trigger.time)

    @property
    def total_kg(self):
        return self.left.kg + self.right.kg

    @property
    def key(self):
        return tuple(sorted((self.left.trigger.id, self.right.trigger.id)))


@dataclass(frozen=True)
class Matching:
    pairings: tuple[Pairing, ...]
    unmatched: tuple[object, ...]

    @property
    def axle_count(self):
        return len(self.pairings)


def candidate_edges(wheels, crossing, pair_ms):
    """返回每条轮载在对侧轮道时窗内的候选（按时刻、源行稳定排序）。"""
    lanes = {side: sorted(
        (wheel for wheel in wheels if wheel.trigger.side == side),
        key=lambda wheel: (wheel.trigger.time, wheel.trigger.source.line),
    ) for side in ("L", "R")}
    edges = {}
    for side, other in (("L", "R"), ("R", "L")):
        for wheel in lanes[side]:
            neighbors = [
                peer for peer in lanes[other]
                if abs(delta_ms(peer.trigger.time, wheel.trigger.time)) <= pair_ms
            ]
            edges[wheel.trigger.id] = neighbors
    return lanes, edges


def enumerate_matchings(wheels, crossing, pair_ms):
    """枚举整车所有一对一合法配对（对侧时窗内），未配对轮载一并返回。"""
    lanes, edges = candidate_edges(wheels, crossing, pair_ms)
    solutions = []

    def backtrack(lefts, chosen, used_right, skipped_left):
        if not lefts:
            unmatched_right = tuple(
                wheel for wheel in lanes["R"] if wheel.trigger.id not in used_right
            )
            unmatched = tuple(sorted(
                skipped_left + list(unmatched_right),
                key=lambda wheel: (wheel.trigger.time, wheel.trigger.source.line),
            ))
            ordered = tuple(sorted(
                chosen, key=lambda pairing: (pairing.time, pairing.key),
            ))
            solutions.append(Matching(ordered, unmatched))
            return
        left = lefts[0]
        rest = lefts[1:]
        # 方案一：该左轮暂不配对（留给缺证隔离），保证缺轴方案也被枚举。
        backtrack(rest, chosen, used_right, skipped_left + [left])
        for right in edges[left.trigger.id]:
            if right.trigger.id in used_right:
                continue
            used_right.add(right.trigger.id)
            backtrack(rest, chosen + [Pairing(left, right)], used_right, skipped_left)
            used_right.remove(right.trigger.id)

    backtrack(lanes["L"], [], set(), [])
    # 去重并按确定性顺序排列，保证同一输入重跑结果完全一致。
    unique = {}
    for matching in solutions:
        signature = (
            tuple(pairing.key for pairing in matching.pairings),
            tuple(wheel.trigger.id for wheel in matching.unmatched),
        )
        unique.setdefault(signature, matching)
    return sorted(unique.values(), key=lambda matching: (
        tuple(pairing.key for pairing in matching.pairings),
        tuple(wheel.trigger.id for wheel in matching.unmatched),
    )), edges


def spacing_breaches(matching, crossing):
    """复用轴距窗，返回违规相邻轴数与各轴轴距（无中间缺证时）。"""
    ordered = matching.pairings
    gaps = []
    breaches = 0
    unresolved_times = [wheel.trigger.time for wheel in matching.unmatched]
    for previous, current in zip(ordered, ordered[1:]):
        between = bisect_right(unresolved_times, previous.time)
        if between < len(unresolved_times) and unresolved_times[between] < current.time:
            gaps.append(None)  # 缺证区间不穿推断距
            continue
        elapsed = (current.time - previous.time) // timedelta(microseconds=1)
        distance = crossing.speed_mps * Decimal(elapsed) / 1_000_000
        gaps.append(distance)
        if not MIN_SPACING <= distance <= MAX_SPACING:
            breaches += 1
    return breaches, gaps


def balance_counts(matching):
    severe = warn = 0
    for pairing in matching.pairings:
        total = pairing.total_kg
        difference = abs(pairing.left.kg - pairing.right.kg)
        if difference >= total * SEVERE_RATIO:
            severe += 1
        elif difference >= total * NOTICE_RATIO:
            warn += 1
    return severe, warn


def ticket_gap(matching, ticket_kg):
    if ticket_kg is None:
        return None
    wheel_kg = sum((pairing.total_kg for pairing in matching.pairings), Decimal("0.00"))
    return abs(rounded(ticket_kg - wheel_kg))


def evidence_score(matching, crossing, ticket_kg):
    """整车证据字典序（不含确定性回退）。元组越靠前越优；磅单缺证记中性。"""
    breaches, _ = spacing_breaches(matching, crossing)
    severe, warn = balance_counts(matching)
    gap = ticket_gap(matching, ticket_kg)
    skew = sum(abs(pairing.skew_ms) for pairing in matching.pairings)
    # None（无磅单）排在零差额之后、正差额之前，避免缺证被当作最优证据。
    ticket_rank = (1, Decimal(0)) if gap is None else (0, gap)
    return (
        -matching.axle_count,
        breaches,
        severe,
        warn,
        ticket_rank,
        skew,
    )


def score(matching, crossing, ticket_kg):
    """证据分末尾追加确定性回退（记录号序列），仅用于稳定排序。"""
    signature = tuple(pairing.key for pairing in matching.pairings)
    return evidence_score(matching, crossing, ticket_kg) + (signature,)


def resolve(wheels, crossing, pair_ms, ticket_kg=None):
    """枚举整车方案并按整车证据裁决。

    返回 (best, status, ranked, edges)：
    - status 为 "resolved" 时 best 是唯一最优方案；
    - 为 "blocked" 时证据并列，best 仅为确定性挂起代表，不得判级或核平。
    """
    matchings, edges = enumerate_matchings(wheels, crossing, pair_ms)
    ranked = sorted(matchings, key=lambda matching: score(matching, crossing, ticket_kg))
    best = ranked[0]
    best_evidence = evidence_score(best, crossing, ticket_kg)
    if all(
        evidence_score(matching, crossing, ticket_kg) != best_evidence
        for matching in ranked[1:]
    ):
        status = "resolved"
    else:
        status = "blocked"
    return best, status, ranked, edges


def group_plan(matching, crossing):
    """按轴距给最优方案分轴组，返回轴载荷字典列表（含轴号、轴组、轴距与配对）。"""
    _, gaps = spacing_breaches(matching, crossing)
    groups, serial, group_no = [], 0, 0
    for index, pairing in enumerate(matching.pairings):
        gap = gaps[index - 1] if index > 0 else None
        if index == 0 or gap is None or gap > TANDEM_SPACING:
            group_no += 1
        serial += 1
        groups.append({
            "serial": serial,
            "group_no": group_no,
            "gap_m": gap,
            "pairing": pairing,
        })
    return groups
