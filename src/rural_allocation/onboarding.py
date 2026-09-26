"""入住容量预演与分阶段切换的纯领域规则。

专班提交不可变的家庭画像、目标曲线与回退方案后，系统依据冻结的住房及
公共服务版本，确定性地生成准备、试入住、扩容、收敛四个阶段，并负责：

- 按家庭画像累加每阶段的住房、学位、基层医疗与接驳需求；
- 以稳定顺序把需求切分到具体容量来源；
- 评估阶段准入门禁（入住到位、指标达标、周转余量足够）；
- 计算可回退范围。

本模块只做整数人数/容量运算，不接触数据库和时间源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .errors import ValidationFailed
from .models import identifier, positive_integer, required_text

STAGE_CODES: tuple[str, ...] = ("prepare", "pilot", "expand", "converge")
STAGE_INDEX = {code: index for index, code in enumerate(STAGE_CODES)}
RESOURCE_KINDS: tuple[str, ...] = ("housing_unit", "school_seat", "primary_care", "transit")
METRIC_KEYS: tuple[str, ...] = (
    "occupancy_rate",
    "school_placement_rate",
    "clinic_service_rate",
    "transit_on_time_rate",
)
KIND_LABELS = {
    "housing_unit": "住房套数",
    "school_seat": "学校学位",
    "primary_care": "基层医疗名额",
    "transit": "交通接驳班次",
}


def _non_negative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationFailed(f"{field} 必须是非负整数")
    return value


def _percentage(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValidationFailed(f"{field} 必须是 0 到 100 的整数百分比")
    return value


@dataclass(frozen=True, slots=True)
class HouseholdProfile:
    household_id: str
    village_id: str
    members: int
    school_age_children: int
    daily_transit_trips: int
    housing_tier: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "HouseholdProfile":
        household_id = identifier(raw.get("household_id"), "household_id")
        village_id = identifier(raw.get("village_id"), "village_id")
        members = positive_integer(raw.get("members"), "members")
        children = _non_negative_integer(raw.get("school_age_children"), "school_age_children")
        if children > members:
            raise ValidationFailed("school_age_children 不能超过家庭人数")
        trips = _non_negative_integer(raw.get("daily_transit_trips"), "daily_transit_trips")
        tier = required_text(raw.get("housing_tier"), "housing_tier", 32)
        return cls(household_id, village_id, members, children, trips, tier)

    def demands(self) -> dict[str, int]:
        """每个家庭对四类资源的需求量。"""
        return {
            "housing_unit": 1,
            "school_seat": self.school_age_children,
            "primary_care": self.members,
            "transit": self.daily_transit_trips,
        }


@dataclass(frozen=True, slots=True)
class ResourceItem:
    resource_id: str
    kind: str
    provider_name: str
    capacity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> "ResourceItem":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("kind 必须是 housing_unit、school_seat、primary_care 或 transit")
        return cls(
            resource_id=identifier(raw.get("resource_id"), "resource_id"),
            kind=kind,
            provider_name=required_text(raw.get("provider_name"), "provider_name"),
            capacity=_non_negative_integer(raw.get("capacity"), "capacity"),
        )


@dataclass(frozen=True, slots=True)
class PlanRules:
    target_curve: tuple[int, ...]
    metric_thresholds: Mapping[str, int]
    turnover_margin_percent: int
    rollback_steps: Mapping[str, int]

    @classmethod
    def from_dict(cls, raw: Mapping[str, object], household_count: int) -> "PlanRules":
        curve_raw = raw.get("target_curve")
        if not isinstance(curve_raw, Mapping):
            raise ValidationFailed("target_curve 必须是阶段到累计家庭数的对象")
        curve: list[int] = []
        for code in STAGE_CODES:
            value = _non_negative_integer(curve_raw.get(code), f"target_curve.{code}")
            curve.append(value)
        for previous, current, code in zip(curve, curve[1:], STAGE_CODES[1:]):
            if current < previous:
                raise ValidationFailed(f"target_curve.{code} 不能低于前一阶段")
        if curve[0] != 0:
            raise ValidationFailed("target_curve.prepare 必须为 0，准备阶段不安排入住")
        if curve[-1] != household_count:
            raise ValidationFailed("target_curve.converge 必须等于家庭画像总数")
        thresholds_raw = raw.get("metric_thresholds")
        if not isinstance(thresholds_raw, Mapping):
            raise ValidationFailed("metric_thresholds 必须是指标到最低百分比的对象")
        thresholds: dict[str, int] = {}
        for key in METRIC_KEYS:
            thresholds[key] = _percentage(thresholds_raw.get(key), f"metric_thresholds.{key}")
        margin = _percentage(raw.get("turnover_margin_percent", 0), "turnover_margin_percent")
        policy_raw = raw.get("rollback_policy", {})
        if not isinstance(policy_raw, Mapping):
            raise ValidationFailed("rollback_policy 必须是阶段到最大回退步数的对象")
        policy: dict[str, int] = {}
        for key, value in policy_raw.items():
            if key not in STAGE_CODES or key == "prepare":
                raise ValidationFailed("rollback_policy 只能声明 pilot、expand 或 converge")
            steps = _non_negative_integer(value, f"rollback_policy.{key}")
            if steps > len(STAGE_CODES) - 1:
                raise ValidationFailed(f"rollback_policy.{key} 超出可回退阶段数")
            policy[key] = steps
        return cls(tuple(curve), thresholds, margin, policy)


def ordered_households(profiles: Sequence[HouseholdProfile]) -> list[HouseholdProfile]:
    return sorted(profiles, key=lambda item: item.household_id)


def cumulative_demands(
    profiles: Sequence[HouseholdProfile],
    target_curve: Sequence[int],
) -> list[dict[str, int]]:
    """返回每个阶段累计入住家庭对四类资源的需求。

    家庭按编号稳定排序后取目标曲线的前 N 户，保证任何时候重算结果一致。
    """
    ordered = ordered_households(profiles)
    result: list[dict[str, int]] = []
    for cumulative_households in target_curve:
        totals = {kind: 0 for kind in RESOURCE_KINDS}
        for profile in ordered[:cumulative_households]:
            for kind, units in profile.demands().items():
                totals[kind] += units
        result.append(totals)
    return result


def allocate_sources(resources: Sequence[ResourceItem], demand: int) -> list[dict[str, object]]:
    """把累计需求按资源编号顺序贪心切分到容量来源。"""
    remaining = demand
    rows: list[dict[str, object]] = []
    for resource in sorted(resources, key=lambda item: item.resource_id):
        contributed = min(max(remaining, 0), resource.capacity)
        rows.append({
            "resource_id": resource.resource_id,
            "capacity": resource.capacity,
            "contributed": contributed,
        })
        remaining -= contributed
    return rows


def capacity_shortfalls(
    resources_by_kind: Mapping[str, Sequence[ResourceItem]],
    demands_by_stage: Sequence[Mapping[str, int]],
    other_reserved: Mapping[str, int],
) -> list[dict[str, object]]:
    """确认计划前的容量可行性阻断原因。"""
    blockers: list[dict[str, object]] = []
    final_demand = demands_by_stage[-1]
    for kind in RESOURCE_KINDS:
        capacity = sum(item.capacity for item in resources_by_kind.get(kind, ()))
        reserved_elsewhere = other_reserved.get(kind, 0)
        demand = final_demand[kind]
        if capacity < demand + reserved_elsewhere:
            blockers.append({
                "code": "capacity_insufficient",
                "kind": kind,
                "message": f"{KIND_LABELS[kind]}容量不足：版本容量 {capacity}，其它计划已预留 {reserved_elsewhere}，本计划需要 {demand}",
                "version_capacity": capacity,
                "demand": demand,
                "other_reserved": reserved_elsewhere,
                "shortfall": demand + reserved_elsewhere - capacity,
            })
    return blockers


def evaluate_stage_gate(
    *,
    stage_index: int,
    checked_in_households: int,
    metrics: Mapping[str, int],
    rules: PlanRules,
    demands_by_stage: Sequence[Mapping[str, int]],
    version_capacity: Mapping[str, int],
    other_reserved: Mapping[str, int],
) -> list[dict[str, object]]:
    """评估阶段推进门禁，返回阻断原因列表，空列表表示可以推进。"""
    blockers: list[dict[str, object]] = []
    target_households = rules.target_curve[stage_index]
    if checked_in_households < target_households:
        blockers.append({
            "code": "admission_incomplete",
            "message": "本阶段目标家庭尚未全部入住，不能推进",
            "expected": target_households,
            "actual": checked_in_households,
        })
    for key in METRIC_KEYS:
        if key not in metrics:
            blockers.append({
                "code": "metric_missing",
                "metric": key,
                "message": f"缺少指标 {key}",
            })
            continue
        threshold = rules.metric_thresholds[key]
        actual = int(metrics[key])
        if actual < threshold:
            blockers.append({
                "code": "metric_below_threshold",
                "metric": key,
                "message": f"指标 {key} 为 {actual}%，低于门槛 {threshold}%",
                "threshold": threshold,
                "actual": actual,
            })
    next_index = stage_index + 1
    if next_index < len(STAGE_CODES):
        for kind in RESOURCE_KINDS:
            demand = demands_by_stage[next_index][kind]
            free = version_capacity.get(kind, 0) - demand - other_reserved.get(kind, 0)
            # free*100 >= margin%*demand 即周转余量足够
            if free < 0 or free * 100 < rules.turnover_margin_percent * demand:
                blockers.append({
                    "code": "turnover_margin_insufficient",
                    "kind": kind,
                    "message": (
                        f"{KIND_LABELS[kind]}周转余量不足：开放下一阶段后剩余 {max(free, 0)}，"
                        f"要求至少保留 {rules.turnover_margin_percent}% 的缓冲（需求 {demand}）"
                    ),
                    "version_capacity": version_capacity.get(kind, 0),
                    "next_stage_demand": demand,
                    "other_reserved": other_reserved.get(kind, 0),
                    "free_after_open": max(free, 0),
                    "turnover_margin_percent": rules.turnover_margin_percent,
                })
    return blockers


def rollback_reachable(rules: PlanRules, current_index: int) -> list[int]:
    """返回当前阶段按回退方案可以退回的阶段索引（由近及远）。"""
    if current_index <= 0:
        return []
    steps = rules.rollback_steps.get(STAGE_CODES[current_index], 0)
    if steps <= 0:
        return []
    lowest = max(0, current_index - steps)
    return list(range(current_index - 1, lowest - 1, -1))
