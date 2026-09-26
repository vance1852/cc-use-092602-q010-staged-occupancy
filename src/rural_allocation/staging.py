"""入住容量预演与分阶段切换的确定性领域规则。

专班冻结家庭画像、目标曲线和回退方案后，本模块只依赖入参计算：
每个阶段（准备、试入住、扩容、收敛）的需求、容量来源、周转余量与阻断原因。
计算结果不访问数据库，便于离线预演和重复验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed
from .models import date_text, identifier, required_text

PHASES = ("prepare", "trial", "expansion", "convergence")
PHASE_INDEX = {key: index for index, key in enumerate(PHASES)}
PHASE_LABELS = {
    "prepare": "准备",
    "trial": "试入住",
    "expansion": "扩容",
    "convergence": "收敛",
}
SERVICE_KINDS = ("housing", "school", "clinic", "transit")
KIND_LABELS = {
    "housing": "住房",
    "school": "学校学位",
    "clinic": "基层医疗",
    "transit": "交通接驳",
}


def non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field} 必须是非负整数")
    return value


def positive_integer(value: object, field: str) -> int:
    result = non_negative_integer(value, field)
    if result == 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return result


def demand_for_profile(*, members: int, school_age_children: int, commuters: int) -> dict[str, int]:
    """家庭画像对四类公共服务的需求派生：一户一套住房、学位按学龄人口、
    基层医疗按签约人口、交通接驳按每日通勤人次。"""
    return {
        "housing": 1,
        "school": school_age_children,
        "clinic": members,
        "transit": commuters,
    }


@dataclass(frozen=True, slots=True)
class StagingResource:
    resource_id: str
    kind: str
    site: str
    base_capacity: int
    daily_turnover: int
    available_from: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "StagingResource":
        kind = required_text(raw.get("kind"), "kind", 16)
        if kind not in SERVICE_KINDS:
            raise ValidationFailed("kind 必须是 housing、school、clinic 或 transit")
        return cls(
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            kind=kind,
            site=required_text(raw.get("site"), "site"),
            base_capacity=non_negative_integer(raw.get("base_capacity"), "base_capacity"),
            daily_turnover=non_negative_integer(raw.get("daily_turnover"), "daily_turnover"),
            available_from=date_text(raw.get("available_from"), "available_from"),
        )


@dataclass(frozen=True, slots=True)
class HouseholdProfile:
    profile_id: str
    village: str
    members: int
    school_age_children: int
    elderly: int
    commuters: int

    @property
    def demand(self) -> dict[str, int]:
        return demand_for_profile(
            members=self.members,
            school_age_children=self.school_age_children,
            commuters=self.commuters,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "HouseholdProfile":
        members = positive_integer(raw.get("members"), "members")
        school_age = non_negative_integer(raw.get("school_age_children", 0), "school_age_children")
        elderly = non_negative_integer(raw.get("elderly", 0), "elderly")
        commuters = non_negative_integer(raw.get("commuters", 0), "commuters")
        if school_age + elderly > members:
            raise ValidationFailed("学龄人口与老人数之和不能超过家庭人口")
        if commuters > members:
            raise ValidationFailed("通勤人数不能超过家庭人口")
        return cls(
            profile_id=identifier(raw.get("profile_id"), "profile_id"),
            village=required_text(raw.get("village"), "village", 64),
            members=members,
            school_age_children=school_age,
            elderly=elderly,
            commuters=commuters,
        )


@dataclass(frozen=True, slots=True)
class CurvePoint:
    phase: str
    by_date: str
    intake_households: int
    gates: Mapping[str, int]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CurvePoint":
        phase = required_text(raw.get("phase"), "phase", 16)
        if phase not in PHASE_INDEX:
            raise ValidationFailed("phase 必须是 prepare、trial、expansion 或 convergence")
        raw_gates = raw.get("gates", {})
        if not isinstance(raw_gates, Mapping):
            raise ValidationFailed("gates 必须是对象")
        gates: dict[str, int] = {}
        for kind, threshold in raw_gates.items():
            if kind not in SERVICE_KINDS:
                raise ValidationFailed(f"gates.{kind} 不是受支持的服务类型")
            if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold <= 100:
                raise ValidationFailed(f"gates.{kind} 必须是 0 到 100 的整数")
            gates[kind] = threshold
        return cls(
            phase=phase,
            by_date=date_text(raw.get("by_date"), "by_date"),
            intake_households=non_negative_integer(raw.get("intake_households"), "intake_households"),
            gates=gates,
        )


@dataclass(frozen=True, slots=True)
class RollbackPolicy:
    max_depth: int
    retain_evidence: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "RollbackPolicy":
        raw = raw or {}
        max_depth = non_negative_integer(raw.get("max_depth", len(PHASES) - 1), "rollback_policy.max_depth")
        if not 1 <= max_depth <= len(PHASES) - 1:
            raise ValidationFailed("rollback_policy.max_depth 必须在 1 到 3 之间")
        retain_evidence = raw.get("retain_evidence", True)
        if not isinstance(retain_evidence, bool):
            raise ValidationFailed("rollback_policy.retain_evidence 必须是布尔值")
        return cls(max_depth=max_depth, retain_evidence=retain_evidence)


def projected_capacity(resource: StagingResource | Mapping[str, Any], on_date: str) -> int:
    """资源在指定日期的容量：交付存量 + 自转存日起每日周转增量。"""
    available_from = resource["available_from"] if isinstance(resource, Mapping) else resource.available_from
    base = int(resource["base_capacity"]) if isinstance(resource, Mapping) else resource.base_capacity
    daily = int(resource["daily_turnover"]) if isinstance(resource, Mapping) else resource.daily_turnover
    days = max(0, (date.fromisoformat(on_date) - date.fromisoformat(available_from)).days)
    return base + daily * days


def _ceil_headroom(demand: int, margin_percent: Decimal) -> int:
    if demand <= 0 or margin_percent <= 0:
        return 0
    required = Decimal(demand) * margin_percent / Decimal(100)
    return int(required.quantize(Decimal("1"), rounding=ROUND_CEILING))


def _blocker(phase: str, kind: str, code: str, message: str, **numbers: int) -> dict[str, Any]:
    return {
        "phase": phase,
        "phase_label": PHASE_LABELS[phase],
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "code": code,
        "message": message,
        **numbers,
    }


def evaluate_turnover(
    *,
    resources: Sequence[StagingResource | Mapping[str, Any]],
    on_date: str,
    cumulative_demand: Mapping[str, int],
    margin_percent: Decimal,
    phase: str,
) -> list[dict[str, Any]]:
    """推进日周转余量闸门：容量（存量+周转）必须覆盖累计需求并保留周转余量。"""
    blockers: list[dict[str, Any]] = []
    for kind in SERVICE_KINDS:
        demand = int(cumulative_demand.get(kind, 0))
        capacity = sum(
            projected_capacity(item, on_date)
            for item in resources
            if (item["kind"] if isinstance(item, Mapping) else item.kind) == kind
        )
        headroom = capacity - demand
        required_headroom = _ceil_headroom(demand, margin_percent)
        if capacity < demand:
            blockers.append(_blocker(
                phase, kind, "capacity_short",
                f"{KIND_LABELS[kind]}容量不足：{on_date} 仅有 {capacity}，累计需求 {demand}",
                capacity=capacity, demand=demand, headroom=headroom,
                required_headroom=required_headroom,
            ))
        elif headroom < required_headroom:
            blockers.append(_blocker(
                phase, kind, "turnover_margin",
                f"{KIND_LABELS[kind]}周转余量不足：余量 {headroom}，至少需要 {required_headroom}",
                capacity=capacity, demand=demand, headroom=headroom,
                required_headroom=required_headroom,
            ))
    return blockers


def evaluate_metrics(
    *,
    phase: str,
    cumulative_demand: Mapping[str, int],
    served: Mapping[str, int],
    gates: Mapping[str, int],
) -> list[dict[str, Any]]:
    """服务指标闸门：住房入住、学位、医疗签约、接驳人次的累计达标率。"""
    blockers: list[dict[str, Any]] = []
    for kind in SERVICE_KINDS:
        demand = int(cumulative_demand.get(kind, 0))
        actual = int(served.get(kind, 0))
        required_percent = int(gates.get(kind, 100))
        if demand == 0:
            continue
        actual_percent = actual * 100 // demand
        if actual < demand and actual_percent < required_percent:
            blockers.append(_blocker(
                phase, kind, "metric_below_target",
                f"{KIND_LABELS[kind]}指标未达标：已服务 {actual}/{demand}（{actual_percent}%），门槛 {required_percent}%",
                served=actual, demand=demand, actual_percent=actual_percent,
                required_percent=required_percent,
            ))
    return blockers


def build_projection(
    *,
    profiles: Sequence[HouseholdProfile],
    resources: Sequence[StagingResource],
    curve: Sequence[CurvePoint],
    margin_percent: Decimal,
) -> dict[str, Any]:
    """按冻结版本生成四阶段预演：需求、容量来源、逐阶段预留量和阻断原因。

    家庭按编号顺序进入曲线；每个阶段的新增需求按资源编号贪心分配到
    当时已经周转到位的容量上，因此容量来源可逐阶段追溯。
    """
    if len(curve) != len(PHASES):
        raise ValidationFailed("目标曲线必须恰好包含准备、试入住、扩容、收敛四个阶段")
    ordered_curve = sorted(curve, key=lambda item: PHASE_INDEX[item.phase])
    if [point.phase for point in ordered_curve] != list(PHASES):
        raise ValidationFailed("目标曲线必须包含全部四个阶段且不重复")
    previous_date: str | None = None
    for point in ordered_curve:
        if previous_date is not None and date.fromisoformat(point.by_date) <= date.fromisoformat(previous_date):
            raise ValidationFailed("目标曲线日期必须严格递增")
        previous_date = point.by_date

    ordered_profiles = sorted(profiles, key=lambda item: item.profile_id)
    cumulative_households = 0
    cumulative_demand = {kind: 0 for kind in SERVICE_KINDS}
    assigned_cumulative = {resource.resource_id: 0 for resource in resources}
    phases_out: list[dict[str, Any]] = []
    reservations_out: list[dict[str, Any]] = []
    all_blockers: list[dict[str, Any]] = []

    for seq, point in enumerate(ordered_curve):
        cumulative_households += point.intake_households
        if cumulative_households > len(ordered_profiles):
            raise ValidationFailed(f"{PHASE_LABELS[point.phase]}阶段累计入户数超过画像总数 {len(ordered_profiles)}")
        start = cumulative_households - point.intake_households
        entering = ordered_profiles[start:cumulative_households]
        increment_demand = {kind: 0 for kind in SERVICE_KINDS}
        for profile in entering:
            for kind, qty in profile.demand.items():
                increment_demand[kind] += qty
                cumulative_demand[kind] += qty

        sources: list[dict[str, Any]] = []
        for resource in sorted(resources, key=lambda item: item.resource_id):
            capacity = projected_capacity(resource, point.by_date)
            sources.append({
                "resource_id": resource.resource_id,
                "kind": resource.kind,
                "kind_label": KIND_LABELS[resource.kind],
                "site": resource.site,
                "base_capacity": resource.base_capacity,
                "daily_turnover": resource.daily_turnover,
                "available_from": resource.available_from,
                "projected_capacity": capacity,
                "assigned_cumulative": assigned_cumulative[resource.resource_id],
                "assigned_increment": 0,
                "reservation_state": "pending_confirm",
            })

        phase_blockers: list[dict[str, Any]] = []
        source_by_id = {row["resource_id"]: row for row in sources}
        for kind in SERVICE_KINDS:
            need = increment_demand[kind]
            kind_resources = sorted(
                (row for row in sources if row["kind"] == kind),
                key=lambda row: row["resource_id"],
            )
            for row in kind_resources:
                if need == 0:
                    break
                headroom = row["projected_capacity"] - row["assigned_cumulative"]
                take = min(need, max(0, headroom))
                row["assigned_increment"] = take
                row["assigned_cumulative"] += take
                assigned_cumulative[row["resource_id"]] = row["assigned_cumulative"]
                need -= take
            # 容量是否足够的最终判定交给 evaluate_turnover，它同时检查累计需求与周转余量；
            # 此处未分配完的缺口只影响 assigned_* 数字。

        phase_blockers.extend(evaluate_turnover(
            resources=resources,
            on_date=point.by_date,
            cumulative_demand=cumulative_demand,
            margin_percent=margin_percent,
            phase=point.phase,
        ))

        for row in sources:
            if row["assigned_increment"] > 0:
                reservations_out.append({
                    "phase_seq": seq,
                    "resource_id": row["resource_id"],
                    "kind": row["kind"],
                    "reserved_qty": row["assigned_increment"],
                })
        phases_out.append({
            "seq": seq,
            "phase": point.phase,
            "phase_label": PHASE_LABELS[point.phase],
            "by_date": point.by_date,
            "intake_households": point.intake_households,
            "cumulative_households": cumulative_households,
            "increment_demand": dict(increment_demand),
            "cumulative_demand": dict(cumulative_demand),
            "gates": {kind: int(point.gates.get(kind, 100)) for kind in SERVICE_KINDS},
            "sources": sources,
            "blockers": phase_blockers,
        })
        all_blockers.extend(phase_blockers)

    return {
        "phases": phases_out,
        "reservations": reservations_out,
        "blockers": all_blockers,
        "total_households": cumulative_households,
    }
