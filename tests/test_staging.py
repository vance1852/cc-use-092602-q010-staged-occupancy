from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from rural_allocation.api import JsonApplication
from rural_allocation.clock import FrozenClock
from rural_allocation.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from rural_allocation.service import SupplyService
from rural_allocation.staging import build_projection, projected_capacity
from rural_allocation.staging_service import StagingService


def household(profile_id: str, *, village="东村", members=4, children=1, elderly=0, commuters=2) -> dict:
    return {
        "profile_id": profile_id,
        "village": village,
        "members": members,
        "school_age_children": children,
        "elderly": elderly,
        "commuters": commuters,
    }


RESOURCES_V1 = [
    {"resource_id": "h1", "kind": "housing", "site": "安居苑", "base_capacity": 3, "daily_turnover": 1, "available_from": "2026-09-01"},
    {"resource_id": "h2", "kind": "housing", "site": "乐业苑", "base_capacity": 0, "daily_turnover": 1, "available_from": "2026-09-01"},
    {"resource_id": "s1", "kind": "school", "site": "新区一小", "base_capacity": 5, "daily_turnover": 1, "available_from": "2026-09-01"},
    {"resource_id": "c1", "kind": "clinic", "site": "社区卫生中心", "base_capacity": 20, "daily_turnover": 2, "available_from": "2026-09-01"},
    {"resource_id": "t1", "kind": "transit", "site": "接驳枢纽", "base_capacity": 10, "daily_turnover": 1, "available_from": "2026-09-01"},
]

CURVE = [
    {"phase": "prepare", "by_date": "2026-09-10", "intake_households": 2},
    {"phase": "trial", "by_date": "2026-09-20", "intake_households": 3},
    {"phase": "expansion", "by_date": "2026-09-30", "intake_households": 3},
    {"phase": "convergence", "by_date": "2026-10-10", "intake_households": 2},
]


class StagingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = SupplyService(self.connection, self.clock)
        self.staging = StagingService(self.connection, self.clock)
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.version = self.staging.publish_resource_version("plan", {"version_id": "v1", "resources": RESOURCES_V1})
        self.staging.submit_household_batch(
            "plan",
            {"batch_id": "b1", "households": [household(f"hh-{i:02d}") for i in range(10)]},
        )

    def tearDown(self) -> None:
        self.connection.close()

    def create_plan(self, plan_id: str = "p1", *, margin: int = 0, rollback=None) -> dict:
        return self.staging.create_staging_plan("plan", {
            "plan_id": plan_id,
            "batch_id": "b1",
            "resource_version_id": "v1",
            "curve": CURVE,
            "turnover_margin_percent": margin,
            "rollback_policy": rollback or {"max_depth": 1, "retain_evidence": True},
        })

    def test_projection_grows_capacity_by_stage_and_is_feasible(self) -> None:
        plan = self.create_plan()
        projection = plan["projection"]
        self.assertEqual(projection["total_households"], 10)
        self.assertEqual([p["phase"] for p in projection["phases"]],
                         ["prepare", "trial", "expansion", "convergence"])
        prepare = projection["phases"][0]
        self.assertEqual(prepare["cumulative_demand"],
                         {"housing": 2, "school": 2, "clinic": 8, "transit": 4})
        # 9 天周转：h1 = 3+9，h2 = 0+9
        housing_sources = {s["resource_id"]: s for s in prepare["sources"] if s["kind"] == "housing"}
        self.assertEqual(housing_sources["h1"]["projected_capacity"], 12)
        self.assertEqual(housing_sources["h2"]["projected_capacity"], 9)
        self.assertEqual(housing_sources["h1"]["assigned_increment"], 2)
        self.assertEqual(projection["blockers"], [])

    def test_project_capacity_formula(self) -> None:
        self.assertEqual(projected_capacity({"base_capacity": 5, "daily_turnover": 2, "available_from": "2026-09-01"}, "2026-09-11"), 25)
        self.assertEqual(projected_capacity({"base_capacity": 5, "daily_turnover": 2, "available_from": "2026-09-01"}, "2026-08-31"), 5)

    def test_blocked_projection_cannot_confirm_or_reserve(self) -> None:
        tight = [dict(item, base_capacity=0, daily_turnover=0) for item in RESOURCES_V1 if item["kind"] == "housing"]
        self.staging.publish_resource_version("plan", {"version_id": "v-tight", "resources": tight + [
            item for item in RESOURCES_V1 if item["kind"] != "housing"
        ]})
        plan = self.staging.create_staging_plan("plan", {
            "plan_id": "p-tight", "batch_id": "b1", "resource_version_id": "v-tight",
            "curve": CURVE, "rollback_policy": {"max_depth": 1, "retain_evidence": True},
        })
        self.assertTrue(any(b["kind"] == "housing" for b in plan["projection"]["blockers"]))
        with self.assertRaises(InvalidState):
            self.staging.confirm_staging_plan("plan", "p-tight")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) c FROM plan_phase_reservations").fetchone()["c"], 0
        )

    def test_turnover_margin_blocks_confirmation(self) -> None:
        # 基层医疗容量恒定 32：扩容阶段累计需求 32 恰好满足，但 50% 周转余量必然阻断。
        resources = [
            dict(item, base_capacity=32, daily_turnover=0) if item["kind"] == "clinic" else dict(item)
            for item in RESOURCES_V1
        ]
        self.staging.publish_resource_version("plan", {"version_id": "v-margin", "resources": resources})
        plan = self.staging.create_staging_plan("plan", {
            "plan_id": "p-margin", "batch_id": "b1", "resource_version_id": "v-margin",
            "curve": CURVE, "turnover_margin_percent": 50,
            "rollback_policy": {"max_depth": 1, "retain_evidence": True},
        })
        codes = {(b["phase"], b["code"]) for b in plan["projection"]["blockers"]}
        self.assertIn(("expansion", "turnover_margin"), codes)
        with self.assertRaises(InvalidState):
            self.staging.confirm_staging_plan("plan", "p-margin")

    def test_confirm_and_reservation_share_one_transaction(self) -> None:
        self.create_plan()
        confirmed = self.staging.confirm_staging_plan("plan", "p1")
        self.assertEqual(confirmed["state"], "confirmed")
        reservations = self.connection.execute(
            "SELECT phase_seq,SUM(reserved_qty) q FROM plan_phase_reservations GROUP BY phase_seq ORDER BY phase_seq"
        ).fetchall()
        # 准备阶段：2 户 * (住房1/学位1/医疗4/接驳2)
        first = {row["phase_seq"]: row["q"] for row in reservations}[0]
        self.assertEqual(first, 2 + 2 + 8 + 4)
        # 重复确认被拒绝
        with self.assertRaises(InvalidState):
            self.staging.confirm_staging_plan("plan", "p1")
        # 同一冻结版本不能有两个占用容量的计划
        self.create_plan("p-dup")
        with self.assertRaises(Conflict):
            self.staging.confirm_staging_plan("plan", "p-dup")

    def test_version_change_invalidates_old_plan(self) -> None:
        self.create_plan()
        self.staging.confirm_staging_plan("plan", "p1")
        self.staging.publish_resource_version("plan", {"version_id": "v2", "resources": [
            dict(item, base_capacity=item["base_capacity"] + 10) for item in RESOURCES_V1
        ]})
        detail = self.staging.staging_plan("audit", "p1")
        self.assertEqual(detail["state"], "invalidated")
        self.assertEqual(detail["invalidated_by_version"], "v2")
        with self.assertRaises(InvalidState):
            self.staging.advance_staging_plan("risk", "p1", "2026-09-10")
        # 旧版本不能再建新计划
        with self.assertRaises(InvalidState):
            self.create_plan("p-old")

    def _prepare_metrics(self, through_seq: int) -> None:
        cumulative = [2, 5, 8, 10]
        kinds = {"housing": 1, "school": 1, "clinic": 4, "transit": 2}
        for kind, per in kinds.items():
            self.staging.record_metric_receipt("dispatch", {
                "receipt_id": f"m-{through_seq}-{kind}", "plan_id": "p1",
                "phase_seq": through_seq, "kind": kind,
                "served_delta": cumulative[through_seq] * per,
                "idempotency_key": f"metric-{through_seq}-{kind}",
            })

    def _intake(self, seq: int, ids: list[str]) -> None:
        for profile_id in ids:
            self.staging.record_intake_receipt("dispatch", {
                "receipt_id": f"r-{seq}-{profile_id}", "plan_id": "p1",
                "phase_seq": seq, "household_profile_id": profile_id,
                "idempotency_key": f"intake-{seq}-{profile_id}",
            })

    def test_gated_advance_requires_metrics_and_turnover(self) -> None:
        self.create_plan()
        self.staging.confirm_staging_plan("plan", "p1")
        # 试入住准入：准备阶段指标未达标时阻断
        entered = self.staging.advance_staging_plan("risk", "p1", "2026-09-10")
        self.assertEqual(entered["phase"], "prepare")
        self._intake(0, ["hh-00", "hh-01"])
        with self.assertRaises(InvalidState):
            self.staging.advance_staging_plan("risk", "p1", "2026-09-20")
        blockers = self.staging.plan_blockers("audit", "p1")["gate_evaluations"]
        self.assertFalse(blockers[-1]["passed"])
        self.assertTrue(any(b["code"] == "metric_below_target" for b in blockers[-1]["blockers"]))
        self._prepare_metrics(0)
        advanced = self.staging.advance_staging_plan("risk", "p1", "2026-09-20")
        self.assertEqual(advanced["phase"], "trial")
        # 推进日太早，周转尚未到位同样阻断
        with self.assertRaises(InvalidState):
            self.staging.advance_staging_plan("risk", "p1", "2026-09-21")

    def test_intake_receipt_is_idempotent_and_deducts_once(self) -> None:
        self.create_plan()
        self.staging.confirm_staging_plan("plan", "p1")
        self.staging.advance_staging_plan("risk", "p1", "2026-09-10")
        payload = {
            "receipt_id": "r-1", "plan_id": "p1", "phase_seq": 0,
            "household_profile_id": "hh-00", "idempotency_key": "key-r1",
        }
        first = self.staging.record_intake_receipt("dispatch", payload)
        second = self.staging.record_intake_receipt("dispatch", payload)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["consumed"], second["consumed"])
        total = self.connection.execute(
            "SELECT SUM(consumed_qty) q FROM plan_phase_reservations WHERE plan_id='p1' AND phase_seq=0"
        ).fetchone()["q"]
        # 一户：1+1+4+2 = 8，重复回执不重复扣减
        self.assertEqual(total, 8)
        changed = dict(payload, receipt_id="r-1b")
        with self.assertRaises(Conflict):
            self.staging.record_intake_receipt("dispatch", changed)
        # 同一家庭不能借新回执再次入住
        other_key = dict(payload, idempotency_key="key-r1-again", receipt_id="r-1c")
        with self.assertRaises(Conflict):
            self.staging.record_intake_receipt("dispatch", other_key)

    def test_rollback_releases_capacity_but_retains_evidence(self) -> None:
        self.create_plan()
        self.staging.confirm_staging_plan("plan", "p1")
        self.staging.advance_staging_plan("risk", "p1", "2026-09-10")
        self._intake(0, ["hh-00", "hh-01"])
        self._prepare_metrics(0)
        self.staging.advance_staging_plan("risk", "p1", "2026-09-20")
        self._intake(1, ["hh-02", "hh-03", "hh-04"])
        window = self.staging.rollback_window("audit", "p1")
        self.assertTrue(window["can_rollback"])
        self.assertEqual([t["phase_seq"] for t in window["targets"]], [0])
        result = self.staging.rollback_staging_plan("risk", "p1", 0, "学校建设延期")
        self.assertEqual(result["active_phase_seq"], 0)
        self.assertEqual(result["retained_intake_receipts"], 3)
        # 试入住阶段预留已释放，但入住证据（3 户）仍然保留
        released = self.connection.execute(
            "SELECT COUNT(*) c FROM plan_phase_reservations WHERE plan_id='p1' AND phase_seq=1 AND state='released'"
        ).fetchone()["c"]
        self.assertGreater(released, 0)
        evidence = self.connection.execute(
            "SELECT COUNT(*) c FROM intake_receipts WHERE plan_id='p1'"
        ).fetchone()["c"]
        self.assertEqual(evidence, 5)
        # 再次回退（已在准备阶段）被拒绝
        with self.assertRaises(ValidationFailed):
            self.staging.rollback_staging_plan("risk", "p1", 0, "再次回退")

    def test_full_lifecycle_completes_and_query_views_work(self) -> None:
        self.create_plan()
        self.staging.confirm_staging_plan("plan", "p1")
        batches = [(0, ["hh-00", "hh-01"]), (1, ["hh-02", "hh-03", "hh-04"]),
                   (2, ["hh-05", "hh-06", "hh-07"]), (3, ["hh-08", "hh-09"])]
        dates = ["2026-09-10", "2026-09-20", "2026-09-30", "2026-10-10"]
        for seq, ids in batches:
            self.staging.advance_staging_plan("risk", "p1", dates[seq])
            self._intake(seq, ids)
            self._prepare_metrics(seq)
        completed = self.staging.complete_staging_plan("risk", "p1", "2026-10-11")
        self.assertEqual(completed["state"], "completed")
        sources = self.staging.plan_capacity_sources("audit", "p1")
        self.assertEqual(len(sources["phases"]), 4)
        convergence = sources["phases"][-1]["sources"]
        self.assertTrue(any(s["consumed_qty"] for s in convergence))
        detail = self.staging.staging_plan("audit", "p1")
        self.assertEqual(detail["intake_evidence"], {"0": 2, "1": 3, "2": 3, "3": 2})
        self.assertTrue(self.service.audit_chain("audit")["valid"])

    def test_permissions_and_api_routing(self) -> None:
        with self.assertRaises(Forbidden):
            self.staging.advance_staging_plan("plan", "p1", "2026-09-10")
        with self.assertRaises(Forbidden):
            self.staging.record_intake_receipt("risk", {"receipt_id": "x", "plan_id": "p1", "phase_seq": 0,
                                                        "household_profile_id": "hh-00", "idempotency_key": "k"})
        app = JsonApplication(self.service)
        response = app.handle("POST", "/resource-versions", {"X-Actor-Id": "dispatch"},
                              b'{"version_id":"vX","resources":[]}')
        self.assertEqual(response.status, 403)
        response = app.handle("GET", "/staging-plans/missing", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
