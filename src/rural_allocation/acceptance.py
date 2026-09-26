"""贯通补偿单价、地块资源池、土地库存、提名和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import SupplyService
from .staging_service import StagingService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = SupplyService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    staging = StagingService(connection, service.clock)
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

    # 入住容量预演与分阶段切换：冻结住房与公共服务版本，提交不可变家庭画像。
    staging.publish_resource_version("plan", {"version_id": "newtown-v1", "resources": [
        {"resource_id": "home-a", "kind": "housing", "site": "安居苑", "base_capacity": 6, "daily_turnover": 0, "available_from": "2026-09-01"},
        {"resource_id": "school-a", "kind": "school", "site": "新区一小", "base_capacity": 6, "daily_turnover": 0, "available_from": "2026-09-01"},
        {"resource_id": "clinic-a", "kind": "clinic", "site": "社区卫生服务中心", "base_capacity": 24, "daily_turnover": 0, "available_from": "2026-09-01"},
        {"resource_id": "shuttle-a", "kind": "transit", "site": "接驳枢纽", "base_capacity": 12, "daily_turnover": 0, "available_from": "2026-09-01"},
    ]})
    staging.submit_household_batch("plan", {"batch_id": "families-001", "households": [
        {"profile_id": f"fam-{index:02d}", "village": "东岭村", "members": 4,
         "school_age_children": 1, "elderly": 1, "commuters": 2}
        for index in range(6)
    ]})
    plan = staging.create_staging_plan("plan", {
        "plan_id": "intake-001",
        "batch_id": "families-001",
        "resource_version_id": "newtown-v1",
        "turnover_margin_percent": 0,
        "curve": [
            {"phase": "prepare", "by_date": "2026-09-10", "intake_households": 1, "gates": {"housing": 100}},
            {"phase": "trial", "by_date": "2026-09-20", "intake_households": 2, "gates": {"housing": 90, "school": 90, "clinic": 90, "transit": 90}},
            {"phase": "expansion", "by_date": "2026-09-30", "intake_households": 2, "gates": {"housing": 90, "school": 90, "clinic": 90, "transit": 90}},
            {"phase": "convergence", "by_date": "2026-10-10", "intake_households": 1, "gates": {"housing": 100, "school": 100, "clinic": 100, "transit": 100}},
        ],
        "rollback_policy": {"max_depth": 1, "retain_evidence": True},
    })
    staging.confirm_staging_plan("plan", "intake-001")
    phase_dates = ("2026-09-10", "2026-09-20", "2026-09-30", "2026-10-10")
    phase_families = (["fam-00"], ["fam-01", "fam-02"], ["fam-03", "fam-04"], ["fam-05"])
    cumulative_households = (1, 3, 5, 6)
    for seq, families in enumerate(phase_families):
        staging.advance_staging_plan("risk", "intake-001", phase_dates[seq])
        for family in families:
            payload = {
                "receipt_id": f"intake-{seq}-{family}", "plan_id": "intake-001",
                "phase_seq": seq, "household_profile_id": family,
                "idempotency_key": f"key-intake-{seq}-{family}",
            }
            staging.record_intake_receipt("dispatch", payload)
            staging.record_intake_receipt("dispatch", payload)  # 重复回执回放，不重复扣减
        for kind, per_household in (("housing", 1), ("school", 1), ("clinic", 4), ("transit", 2)):
            staging.record_metric_receipt("dispatch", {
                "receipt_id": f"metric-{seq}-{kind}", "plan_id": "intake-001",
                "phase_seq": seq, "kind": kind,
                "served_delta": cumulative_households[seq] * per_household,
                "idempotency_key": f"key-metric-{seq}-{kind}",
            })
    completed = staging.complete_staging_plan("risk", "intake-001", "2026-10-11")
    sources = staging.plan_capacity_sources("audit", "intake-001")
    blockers = staging.plan_blockers("audit", "intake-001")
    rollback_window = staging.rollback_window("audit", "intake-001")
    result = {"status": "ok", "price": service.price_summary("PEAK_VALLEY"), "allocation_id": allocation["allocation_id"], "transfer": transfer, "scenario_run_id": scenario["run_id"],
              "staging": {"plan": plan["plan_id"], "state": completed["state"], "phases": len(sources["phases"]),
                          "projection_blockers": len(blockers["projection_blockers"]),
                          "passed_gate_evaluations": sum(1 for item in blockers["gate_evaluations"] if item["passed"]),
                          "rollback_window_after_completion": rollback_window["can_rollback"]},
              "audit": service.audit_chain("audit"), "workspace": workspace.name}
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
