from collections import defaultdict

from app.config import PAIR_MS
from app.evidence import Issue
from app.modules.m4_axles.resolver import Pairing, group_plan, resolve
from app.modules.m4_axles.spacing import classify_spacing


def _issue(code, wheel, crossing, refs):
    return Issue(code, wheel.trigger.source, crossing.id, tuple(dict.fromkeys(refs)))


def resolve_pass(wheels, crossing, ticket_kg):
    """对单个车次做整车争配裁决，返回轴、问题与争配台账行。"""
    best, status, ranked, edges = resolve(wheels, crossing, PAIR_MS, ticket_kg)

    # 卷入争配（候选不唯一或对侧也有多个候选）的轮载证据。
    ambiguous_refs = set()
    for wheel in wheels:
        peers = edges[wheel.trigger.id]
        if not peers:
            continue
        if len(peers) > 1 or any(len(edges[peer.trigger.id]) > 1 for peer in peers):
            ambiguous_refs.add(wheel.trigger.source.ref)
            for peer in peers:
                ambiguous_refs.add(peer.trigger.source.ref)

    issues = []
    if status == "blocked":
        # 争配未解决：所有卷入候选关系的轮载挂起，等待现场复秤，不做轴距/偏载判级。
        for wheel in sorted(wheels, key=lambda item: (item.trigger.time, item.trigger.source.line)):
            if wheel.trigger.source.ref in ambiguous_refs:
                issues.append(_issue("PAIR_CONTENDED", wheel, crossing, sorted(ambiguous_refs)))
    else:
        # 已解决（含无争配）：候选缺失或方案未采用的轮载按缺证隔离，去重后逐轮一条。
        missing_wheels = {wheel.trigger.id: wheel for wheel in wheels if not edges[wheel.trigger.id]}
        for wheel in best.unmatched:
            missing_wheels.setdefault(wheel.trigger.id, wheel)
        for wheel in sorted(missing_wheels.values(),
                            key=lambda item: (item.trigger.time, item.trigger.source.line)):
            issues.append(_issue("PAIR_MISSING", wheel, crossing, [wheel.trigger.source.ref]))

    plan = group_plan(best, crossing)
    axes = []
    for item in plan:
        pairing = item["pairing"]
        axes.append({
            "id": f"{crossing.id}/A{item['serial']:02}",
            "pass_id": crossing.id,
            "group": f"{crossing.id}/G{item['group_no']:02}",
            "time": pairing.time.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "skew_ms": pairing.skew_ms,
            "gap_m": item["gap_m"],
            "left": pairing.left,
            "right": pairing.right,
            "total_kg": pairing.total_kg,
            "status": "tentative" if status == "blocked" else "confirmed",
            "contended": status == "blocked",
        })

    if status != "blocked":
        # 轴距窗只对已确认方案执行；被轴距判废的轴整体隔离并从轴列表剔除。
        legacy_pairs = tuple(
            Pairing(pairing.left, pairing.right) for pairing in best.pairings
        )
        _, spacing_issues = classify_spacing(
            legacy_pairs,
            sorted(wheel.trigger.time for wheel in best.unmatched),
            crossing,
        )
        issues.extend(spacing_issues)
        rejected_refs = {ref for issue in spacing_issues for ref in issue.related}
        axes = [
            axis for axis in axes
            if axis["left"].trigger.source.ref not in rejected_refs
            and axis["right"].trigger.source.ref not in rejected_refs
        ]

    chosen_refs = {
        item.trigger.source.ref
        for pairing in best.pairings for item in (pairing.left, pairing.right)
    }
    ledger_row = {
        "pass_id": crossing.id,
        "status": status,
        "candidate_plans": len(ranked),
        "chosen_pairs": [list(pairing.key) for pairing in best.pairings],
        "excluded_refs": sorted({
            wheel.trigger.source.ref
            for matching in ranked
            for wheel in matching.unmatched
            if wheel.trigger.source.ref not in chosen_refs
        }),
        "alternatives": [
            {
                "pairs": [list(pairing.key) for pairing in matching.pairings],
                "axle_count": matching.axle_count,
            }
            for matching in ranked[1:]
        ],
    }
    return axes, issues, ledger_row


def reconstruct(kept, passes, ticket_weights=None):
    by_pass = defaultdict(list)
    if isinstance(kept, dict):
        by_pass.update(kept)
    else:
        for wheel in kept:
            by_pass[wheel.trigger.pass_id].append(wheel)
    axes, issues, contention = [], [], []
    ticket_weights = ticket_weights or {}
    for pass_id, crossing in passes.items():
        pass_axes, pass_issues, row = resolve_pass(
            by_pass[pass_id], crossing, ticket_weights.get(pass_id),
        )
        axes.extend(pass_axes)
        issues.extend(pass_issues)
        contention.append(row)
    return {
        "axes": axes,
        "issues": issues,
        "groups": len({axis["group"] for axis in axes}),
        "contention": contention,
    }
