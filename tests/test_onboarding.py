from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from rural_allocation.onboarding import (
    PlanRules,
    evaluate_stage_gate,
    rollback_reachable,
)
from rural_allocation.onboarding_service import OnboardingService
from rural_allocation.service import SupplyService


def household(household_id: str) -> dict[str, object]:
    return {
        "household_id": household_id,
        "village_id": "village-north",
        "members": 3,
        "school_age_children": 1,
        "daily_transit_trips": 2,
        "housing_tier": "standard",
    }


VERSION_ITEMS = [
    {"resource_id": "home-a", "kind": "housing_unit", "provider_name": "一号安置楼", "capacity": 6, "source_revision": "rev-1"},
    {"resource_id": "home-b", "kind": "housing_unit", "provider_name": "二号安置楼", "capacity": 6, "source_revision": "rev-1"},
    {"resource_id": "school-a", "kind": "school_seat", "provider_name": "新区一小", "capacity": 12, "source_revision": "rev-1"},
    {"resource_id": "clinic-a", "kind": "primary_care", "provider_name": "社区卫生中心", "capacity": 34, "source_revision": "rev-1"},
    {"resource_id": "shuttle-a", "kind": "transit", "provider_name": "接驳一线", "capacity": 22, "source_revision": "rev-1"},
]

RULES = {
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


class GateRuleTests(unittest.TestCase):
    def _rules(self, margin: int = 10) -> PlanRules:
        return PlanRules.from_dict(dict(RULES, turnover_margin_percent=margin), 10)

    def test_target_curve_must_end_at_total_households(self) -> None:
        with self.assertRaises(ValidationFailed):
            PlanRules.from_dict(
                dict(RULES, target_curve={"prepare": 0, "pilot": 2, "expand": 6, "converge": 9}),
                10,
            )

    def test_turnover_margin_blocks_when_buffer_too_thin(self) -> None:
        rules = self._rules(margin=30)
        demands = [
            {"housing_unit": 0, "school_seat": 0, "primary_care": 0, "transit": 0},
            {"housing_unit": 2, "school_seat": 2, "primary_care": 6, "transit": 4},
            {"housing_unit": 6, "school_seat": 6, "primary_care": 18, "transit": 12},
            {"housing_unit": 10, "school_seat": 10, "primary_care": 30, "transit": 20},
        ]
        blockers = evaluate_stage_gate(
            stage_index=2,
            checked_in_households=6,
            metrics={key: 95 for key in (
                "occupancy_rate", "school_placement_rate", "clinic_service_rate", "transit_on_time_rate")},
            rules=rules,
            demands_by_stage=demands,
            version_capacity={"housing_unit": 12, "school_seat": 12, "primary_care": 34, "transit": 22},
            other_reserved={},
        )
        codes = {item["code"] for item in blockers}
        self.assertIn("turnover_margin_insufficient", codes)

    def test_rollback_reachable_respects_policy(self) -> None:
        rules = self._rules()
        self.assertEqual(rollback_reachable(rules, 3), [2, 1])
        self.assertEqual(rollback_reachable(rules, 2), [1])
        self.assertEqual(rollback_reachable(rules, 0), [])


class OnboardingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        self.onboarding = OnboardingService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"),
            ("audit", "auditor"), ("task", "taskforce"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.onboarding.freeze_resource_version("task", {"version_id": "v1", "items": VERSION_ITEMS})

    def tearDown(self) -> None:
        self.connection.close()

    def _households(self, count: int = 10) -> list[dict[str, object]]:
        return [household(f"h{number:02d}") for number in range(1, count + 1)]

    def _confirm(self, plan_id: str = "plan-1", rules: dict | None = None) -> dict[str, object]:
        return self.onboarding.confirm_plan("task", {
            "plan_id": plan_id,
            "taskforce_id": "tf-new-town",
            "version_id": "v1",
            "households": self._households(),
            "rules": rules or RULES,
            "idempotency_key": f"confirm-{plan_id}",
        })

    def _checkin(self, number: int, stage: str) -> dict[str, object]:
        return self.onboarding.record_checkin("task", {
            "receipt_id": f"rc-{number:02d}",
            "plan_id": "plan-1",
            "household_id": f"h{number:02d}",
            "idempotency_key": f"ck-{number:02d}",
        })

    def _metrics(self, values: tuple[int, int, int, int] = (95, 95, 95, 95)) -> dict[str, object]:
        occupancy, school, clinic, transit = values
        return self.onboarding.record_metrics("task", {
            "plan_id": "plan-1",
            "metrics": {
                "occupancy_rate": occupancy,
                "school_placement_rate": school,
                "clinic_service_rate": clinic,
                "transit_on_time_rate": transit,
            },
        })

    def test_confirm_generates_four_stages_and_reserves_same_transaction(self) -> None:
        result = self._confirm()
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual([stage["stage"] for stage in result["stages"]],
                         ["prepare", "pilot", "expand", "converge"])
        converge_demand = result["stages"][-1]["demand"]
        self.assertEqual(converge_demand,
                         {"housing_unit": 10, "school_seat": 10, "primary_care": 30, "transit": 20})
        reservations = self.connection.execute(
            "SELECT kind,reserved_units FROM onboarding_reservations WHERE plan_id='plan-1' ORDER BY kind"
        ).fetchall()
        self.assertEqual({row["kind"]: row["reserved_units"] for row in reservations},
                         {"housing_unit": 10, "primary_care": 30, "school_seat": 10, "transit": 20})
        # 画像与规则哈希存证，之后不可修改。
        row = self.connection.execute(
            "SELECT length(profiles_sha256) AS l,length(rules_sha256) AS r FROM onboarding_plans"
        ).fetchone()
        self.assertEqual((row["l"], row["r"]), (64, 64))

    def test_insufficient_capacity_rolls_back_plan_and_reservation(self) -> None:
        self.onboarding.freeze_resource_version("task", {
            "version_id": "v-small",
            "items": [dict(item, capacity=1) for item in VERSION_ITEMS],
        })
        with self.assertRaises(InvalidState):
            self.onboarding.confirm_plan("task", {
                "plan_id": "plan-bad",
                "taskforce_id": "tf-new-town",
                "version_id": "v-small",
                "households": self._households(),
                "rules": RULES,
                "idempotency_key": "confirm-bad",
            })
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM onboarding_plans WHERE plan_id='plan-bad'").fetchone()["c"],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM onboarding_reservations WHERE plan_id='plan-bad'").fetchone()["c"],
            0,
        )

    def test_second_plan_reservation_counts_against_same_version(self) -> None:
        self._confirm()
        with self.assertRaises(InvalidState):
            self._confirm("plan-2")

    def test_checkin_before_stage_opens_is_rejected(self) -> None:
        self._confirm()
        with self.assertRaises(InvalidState):
            self._checkin(1, "prepare")

    def test_full_stage_progression_with_gates(self) -> None:
        self._confirm()
        # prepare -> pilot：准备阶段无入住目标，周转余量足够即可开放试入住。
        advanced = self.onboarding.advance_stage("task", "plan-1")
        self.assertEqual(advanced["current_stage"], "pilot")
        self._checkin(1, "pilot")
        # 入住未到位时推进被阻断，阻断原因可查。
        with self.assertRaises(InvalidState):
            self.onboarding.advance_stage("task", "plan-1")
        status = self.onboarding.plan_status("audit", "plan-1")
        self.assertTrue(any(item["code"] == "admission_incomplete" for item in status["gate_blockers"]))
        self._checkin(2, "pilot")
        # 指标不达标同样阻断。
        self._metrics((80, 95, 95, 95))
        with self.assertRaises(InvalidState):
            self.onboarding.advance_stage("task", "plan-1")
        self._metrics((95, 95, 95, 95))
        self.assertEqual(self.onboarding.advance_stage("task", "plan-1")["current_stage"], "expand")
        for number in range(3, 7):
            self._checkin(number, "expand")
        self._metrics()
        self.assertEqual(self.onboarding.advance_stage("task", "plan-1")["current_stage"], "converge")
        for number in range(7, 11):
            self._checkin(number, "converge")
        self._metrics()
        converged = self.onboarding.complete_convergence("task", "plan-1")
        self.assertEqual(converged["state"], "converged")

    def test_duplicate_receipt_replay_does_not_double_deduct(self) -> None:
        self._confirm()
        self.onboarding.advance_stage("task", "plan-1")
        payload = {
            "receipt_id": "rc-01",
            "plan_id": "plan-1",
            "household_id": "h01",
            "idempotency_key": "ck-01",
        }
        first = self.onboarding.record_checkin("task", payload)
        second = self.onboarding.record_checkin("task", dict(payload))
        self.assertEqual(first, second)
        self.assertEqual(second["checked_in_total"], 1)
        # 同一家庭换回执再来也不能重复占容量。
        with self.assertRaises(Conflict):
            self.onboarding.record_checkin("task", {
                "receipt_id": "rc-01-again",
                "plan_id": "plan-1",
                "household_id": "h01",
                "idempotency_key": "ck-01-again",
            })

    def test_rollback_preserves_checkin_evidence_and_respects_scope(self) -> None:
        self._confirm()
        self.onboarding.advance_stage("task", "plan-1")
        self._checkin(1, "pilot")
        self._checkin(2, "pilot")
        self._metrics()
        self.onboarding.advance_stage("task", "plan-1")  # -> expand
        for number in range(3, 7):
            self._checkin(number, "expand")
        status = self.onboarding.plan_status("task", "plan-1")
        self.assertEqual(status["rollback"]["reachable_stages"], ["pilot"])
        result = self.onboarding.rollback_stage("task", "plan-1")
        self.assertEqual(result["current_stage"], "pilot")
        # 六户入住证据全部保留，容量未被重复扣减或抹除。
        self.assertEqual(result["preserved_checkins"], 6)
        status = self.onboarding.plan_status("task", "plan-1")
        self.assertEqual(status["checked_in_total"], 6)
        self.assertEqual(len(status["checkin_evidence"]), 6)
        stage_states = {row["stage"]: row["state"] for row in status["stages"]}
        self.assertEqual(stage_states["expand"], "rolled_back")
        # 不能把回退目标指向尚未到达的阶段。
        with self.assertRaises(InvalidState):
            self.onboarding.rollback_stage("task", "plan-1", "converge")
        # pilot 按方案允许再退一步到 prepare，证据继续保留。
        again = self.onboarding.rollback_stage("task", "plan-1", "prepare")
        self.assertEqual(again["current_stage"], "prepare")
        self.assertEqual(again["preserved_checkins"], 6)

    def test_new_resource_revision_invalidates_old_plan(self) -> None:
        self._confirm()
        self.onboarding.advance_stage("task", "plan-1")
        self._checkin(1, "pilot")
        new_items = [
            dict(item, source_revision="rev-2") if item["resource_id"] == "home-a" else dict(item)
            for item in VERSION_ITEMS
        ]
        self.onboarding.freeze_resource_version("task", {"version_id": "v2", "items": new_items})
        self.assertEqual(
            self.connection.execute("SELECT state FROM resource_versions WHERE version_id='v1'").fetchone()["state"],
            "superseded",
        )
        plan_row = self.connection.execute(
            "SELECT state,invalidated_reason FROM onboarding_plans WHERE plan_id='plan-1'"
        ).fetchone()
        self.assertEqual(plan_row["state"], "invalidated")
        self.assertIn("home-a", plan_row["invalidated_reason"])
        # 失效计划不能再推进或登记回执，证据仍可查。
        with self.assertRaises(InvalidState):
            self.onboarding.advance_stage("task", "plan-1")
        with self.assertRaises(InvalidState):
            self._checkin(2, "pilot")
        status = self.onboarding.plan_status("audit", "plan-1")
        self.assertEqual(len(status["checkin_evidence"]), 1)
        # 旧版本上不能再确认新计划。
        with self.assertRaises(InvalidState):
            self.onboarding.confirm_plan("task", {
                "plan_id": "plan-old",
                "taskforce_id": "tf-new-town",
                "version_id": "v1",
                "households": self._households(),
                "rules": RULES,
                "idempotency_key": "confirm-old",
            })

    def test_status_exposes_capacity_sources_blockers_and_rollback_scope(self) -> None:
        self._confirm()
        status = self.onboarding.plan_status("audit", "plan-1")
        converge = next(stage for stage in status["stages"] if stage["stage"] == "converge")
        housing = converge["capacity_sources"]["housing_unit"]
        self.assertEqual(housing["demand"], 10)
        self.assertEqual(housing["version_capacity"], 12)
        self.assertEqual(
            [(item["resource_id"], item["contributed"]) for item in housing["items"]],
            [("home-a", 6), ("home-b", 4)],
        )
        self.assertEqual(status["rollback"]["policy"], {"pilot": 1, "expand": 1, "converge": 2})
        self.assertEqual(status["rollback"]["reachable_stages"], [])
        self.assertEqual(status["profiles_sha256"] != status["rules_sha256"], True)

    def test_role_permissions(self) -> None:
        with self.assertRaises(Forbidden):
            self.onboarding.freeze_resource_version("plan", {"version_id": "vx", "items": VERSION_ITEMS})
        with self.assertRaises(Forbidden):
            self.onboarding.confirm_plan("dispatch", {
                "plan_id": "p", "taskforce_id": "t", "version_id": "v1",
                "households": self._households(), "rules": RULES, "idempotency_key": "k",
            })
        # 审计员只读，不能推进阶段。
        self._confirm()
        with self.assertRaises(Forbidden):
            self.onboarding.advance_stage("audit", "plan-1")

    def test_api_routes_for_onboarding(self) -> None:
        app = JsonApplication(self.service)
        response = app.handle("GET", "/onboarding/plans", {"X-Actor-Id": "task"})
        self.assertEqual(response.status, 200)
        response = app.handle("POST", "/onboarding/plans", {"X-Actor-Id": "task"}, __import__("json").dumps({
            "plan_id": "plan-api",
            "taskforce_id": "tf-new-town",
            "version_id": "v1",
            "households": self._households(),
            "rules": RULES,
            "idempotency_key": "confirm-api",
        }).encode())
        self.assertEqual(response.status, 201)
        response = app.handle("GET", "/onboarding/plans/plan-api", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["current_stage"], "prepare")


if __name__ == "__main__":
    unittest.main()
