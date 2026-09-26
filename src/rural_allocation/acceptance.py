"""贯通补偿单价、地块资源池、土地库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .onboarding_service import OnboardingService
from .service import SupplyService


def _run_onboarding(service: SupplyService) -> dict[str, object]:
    onboarding = OnboardingService(service.connection, service.clock)
    service.create_user("task", "task", "taskforce")
    onboarding.freeze_resource_version("task", {
        "version_id": "rv-2026-09",
        "items": [
            {"resource_id": "home-a", "kind": "housing_unit", "provider_name": "一号安置楼", "capacity": 6, "source_revision": "blueprint-1"},
            {"resource_id": "home-b", "kind": "housing_unit", "provider_name": "二号安置楼", "capacity": 6, "source_revision": "blueprint-1"},
            {"resource_id": "school-a", "kind": "school_seat", "provider_name": "新区一小", "capacity": 12, "source_revision": "edu-plan-1"},
            {"resource_id": "clinic-a", "kind": "primary_care", "provider_name": "社区卫生服务中心", "capacity": 34, "source_revision": "health-plan-1"},
            {"resource_id": "shuttle-a", "kind": "transit", "provider_name": "镇村接驳一线", "capacity": 24, "source_revision": "route-map-1"},
        ],
    })
    households = [
        {
            "household_id": f"hh-{number:02d}",
            "village_id": "north-village",
            "members": 3,
            "school_age_children": 1,
            "daily_transit_trips": 2,
            "housing_tier": "standard",
        }
        for number in range(1, 11)
    ]
    rules = {
        "target_curve": {"prepare": 0, "pilot": 2, "expand": 6, "converge": 10},
        "metric_thresholds": {
            "occupancy_rate": 90,
            "school_placement_rate": 90,
            "clinic_service_rate": 90,
            "transit_on_time_rate": 90,
        },
        "turnover_margin_percent": 10,
        "rollback_policy": {"pilot": 1, "expand": 1, "converge": 2},
    }
    confirmed = onboarding.confirm_plan("task", {
        "plan_id": "move-2026-09",
        "taskforce_id": "new-town-taskforce",
        "version_id": "rv-2026-09",
        "households": households,
        "rules": rules,
        "idempotency_key": "confirm-move-2026-09",
    })
    onboarding.advance_stage("task", "move-2026-09")  # prepare -> pilot

    def checkin(number: int) -> None:
        payload = {
            "receipt_id": f"rc-{number:02d}",
            "plan_id": "move-2026-09",
            "household_id": f"hh-{number:02d}",
            "idempotency_key": f"receipt-key-{number:02d}",
        }
        onboarding.record_checkin("task", payload)
        onboarding.record_checkin("task", dict(payload))  # 重复回执必须幂等

    def metrics() -> None:
        onboarding.record_metrics("task", {
            "plan_id": "move-2026-09",
            "metrics": {
                "occupancy_rate": 96,
                "school_placement_rate": 95,
                "clinic_service_rate": 97,
                "transit_on_time_rate": 94,
            },
        })

    for number in (1, 2):
        checkin(number)
    metrics()
    onboarding.advance_stage("task", "move-2026-09")  # pilot -> expand
    for number in range(3, 7):
        checkin(number)
    metrics()
    onboarding.advance_stage("task", "move-2026-09")  # expand -> converge
    for number in range(7, 11):
        checkin(number)
    metrics()
    converged = onboarding.complete_convergence("task", "move-2026-09")
    status = onboarding.plan_status("audit", "move-2026-09")
    audit = service.audit_chain("audit")
    return {
        "plan_id": confirmed["plan_id"],
        "stages": [stage["stage"] for stage in confirmed["stages"]],
        "final_state": converged["state"],
        "checked_in_total": status["checked_in_total"],
        "reservation": {
            kind: next(
                stage["capacity_sources"][kind]["demand"]
                for stage in status["stages"] if stage["stage"] == "converge"
            )
            for kind in ("housing_unit", "school_seat", "primary_care", "transit")
        },
        "rollback_policy": status["rollback"]["policy"],
        "audit_valid": audit["valid"],
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_quote("plan", {"market_index": "PEAK_VALLEY", "trade_date": f"2026-09-{index}", "close_cny": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"facility_id": "village-a", "name": "北部示范村", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_mu": "500000"})
    service.create_facility("plan", {"facility_id": "settlement-b", "name": "东部安置片区", "kind": "settlement", "timezone": "Asia/Shanghai", "capacity_mu": "800000"})
    service.create_route("plan", {"route_id": "pool-a-b", "origin_id": "village-a", "destination_id": "settlement-b", "product": "cultivated-land", "daily_capacity": "100000", "loss_basis_points": 25, "transit_hours": 36})
    service.add_inventory_lot("dispatch", {"lot_id": "lot-001", "facility_id": "village-a", "product": "cultivated-land", "grade": "PEAK_VALLEY", "quantity_mu": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_nomination("dispatch", {"nomination_id": "nom-001", "route_id": "pool-a-b", "shipper_id": "household-east", "service_date": "2026-09-25", "requested_mu": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "pool-a-b", "2026-09-25")
    transfer = service.dispatch_transfer("dispatch", "transfer-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "relocation-recovery", "name": "关键机组检修恢复与需求回落", "market_index_drop_percent": "9", "route_capacity_changes": {"pool-a-b": "20"}, "demand_changes": {"village-a:cultivated-land": "-5"}})
    service.approve_scenario("risk", "relocation-recovery", 1)
    scenario = service.run_scenario("plan", "relocation-recovery", "2026-09-23")
    onboarding_summary = _run_onboarding(service)
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"], "onboarding": onboarding_summary, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行乡镇片区调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
