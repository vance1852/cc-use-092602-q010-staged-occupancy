"""入住容量预演与分阶段切换用例。

与 SupplyService 共用同一 SQLite 库中的账号与审计哈希链：
- 资源版本一经发布即冻结，新版本发布使引用旧版本的未终结计划失效；
- 家庭画像批次不可变，目标曲线与回退方案在创建计划时冻结；
- 确认计划与逐阶段容量预留处在同一事务；
- 推进同时检查服务指标达标率与推进日周转余量；
- 入住/指标回执幂等，重复回执回放原结果，不重复扣减；
- 回退释放未被入住证据占用的预留，证据本身永久保留。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .planning import canonical_json
from .staging import (
    KIND_LABELS,
    PHASES,
    PHASE_LABELS,
    CurvePoint,
    HouseholdProfile,
    RollbackPolicy,
    StagingResource,
    build_projection,
    evaluate_metrics,
    evaluate_turnover,
)
from .storage import initialize, transaction

STAGING_PERMISSIONS = {
    "planner": {"version.write", "staging.plan", "staging.confirm", "staging.read"},
    "dispatcher": {"receipt.write", "staging.read"},
    "risk": {"staging.advance", "staging.rollback", "staging.read"},
    "auditor": {"staging.read", "audit.read"},
}

TERMINAL_PLAN_STATES = ("completed", "rolled_back", "invalidated")


class StagingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM supply_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in STAGING_PERMISSIONS.get(user["role"], set()):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM supply_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO supply_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    # ---- 冻结资源版本 -------------------------------------------------

    def publish_resource_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "version.write")
        version_id = raw.get("version_id")
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValidationFailed("version_id 不能为空")
        resources = [StagingResource.from_dict(item) for item in raw.get("resources", [])]
        if not resources:
            raise ValidationFailed("resources 至少包含一项资源")
        ids = [item.resource_id for item in resources]
        if len(set(ids)) != len(ids):
            raise ValidationFailed("资源编号在版本内不能重复")
        definition = [
            {
                "resource_id": item.resource_id,
                "kind": item.kind,
                "site": item.site,
                "base_capacity": item.base_capacity,
                "daily_turnover": item.daily_turnover,
                "available_from": item.available_from,
            }
            for item in sorted(resources, key=lambda item: item.resource_id)
        ]
        content_sha256 = hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()
        previous = self.connection.execute(
            "SELECT version_id FROM resource_versions WHERE state='active' ORDER BY published_at,version_id"
        ).fetchone()
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO resource_versions(version_id,content_sha256,published_by,published_at) "
                    "VALUES(?,?,?,?)",
                    (version_id.strip(), content_sha256, actor_id, self._now()),
                )
                for item in definition:
                    self.connection.execute(
                        "INSERT INTO catalog_resources(version_id,resource_id,kind,site,base_capacity,"
                        "daily_turnover,available_from) VALUES(?,?,?,?,?,?,?)",
                        (
                            version_id.strip(),
                            item["resource_id"],
                            item["kind"],
                            item["site"],
                            item["base_capacity"],
                            item["daily_turnover"],
                            item["available_from"],
                        ),
                    )
                if previous is not None:
                    self.connection.execute(
                        "UPDATE resource_versions SET state='superseded' WHERE version_id=?",
                        (previous["version_id"],),
                    )
                    stale_plans = self.connection.execute(
                        "SELECT plan_id FROM staging_plans WHERE resource_version_id=? "
                        "AND state IN ('draft','confirmed','active')",
                        (previous["version_id"],),
                    ).fetchall()
                    for plan in stale_plans:
                        self.connection.execute(
                            "UPDATE staging_plans SET state='invalidated',invalidated_by_version=?,"
                            "invalidated_at=? WHERE plan_id=? AND state IN ('draft','confirmed','active')",
                            (version_id.strip(), self._now(), plan["plan_id"]),
                        )
                        self._audit(
                            "staging_plan", plan["plan_id"], "staging_plan.invalidated", actor_id,
                            {"resource_version_id": previous["version_id"], "superseded_by": version_id.strip()},
                        )
                self._audit(
                    "resource_version", version_id.strip(), "resource_version.published", actor_id,
                    {"resources": len(definition), "sha256": content_sha256,
                     "supersedes": None if previous is None else previous["version_id"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源版本编号或内容已经存在") from exc
        return {
            "version_id": version_id.strip(),
            "state": "active",
            "resources": len(definition),
            "supersedes": None if previous is None else previous["version_id"],
            "invalidated_plans": 0 if previous is None else len(stale_plans),
            "sha256": content_sha256,
        }

    def resource_version(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM resource_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资源版本不存在")
        resources = self.connection.execute(
            "SELECT resource_id,kind,site,base_capacity,daily_turnover,available_from "
            "FROM catalog_resources WHERE version_id=? ORDER BY resource_id",
            (version_id,),
        ).fetchall()
        return {
            "version_id": row["version_id"],
            "state": row["state"],
            "content_sha256": row["content_sha256"],
            "published_at": row["published_at"],
            "resources": [dict(item) for item in resources],
        }

    # ---- 不可变家庭画像批次 -------------------------------------------

    def submit_household_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "staging.plan")
        batch_id = raw.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValidationFailed("batch_id 不能为空")
        households_raw = raw.get("households", [])
        if not isinstance(households_raw, list) or not households_raw:
            raise ValidationFailed("households 至少包含一个家庭画像")
        profiles = [HouseholdProfile.from_dict(item) for item in households_raw]
        ids = [item.profile_id for item in profiles]
        if len(set(ids)) != len(ids):
            raise ValidationFailed("家庭画像编号在批次内不能重复")
        definition = [
            {
                "profile_id": item.profile_id,
                "village": item.village,
                "members": item.members,
                "school_age_children": item.school_age_children,
                "elderly": item.elderly,
                "commuters": item.commuters,
            }
            for item in sorted(profiles, key=lambda item: item.profile_id)
        ]
        content_sha256 = hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO household_batches(batch_id,profiles_json,content_sha256,household_count,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?)",
                    (batch_id.strip(), canonical_json(definition), content_sha256, len(definition), actor_id, self._now()),
                )
                self._audit(
                    "household_batch", batch_id.strip(), "household_batch.submitted", actor_id,
                    {"households": len(definition), "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("画像批次编号或内容已经存在；家庭画像一经提交不可修改") from exc
        return {"batch_id": batch_id.strip(), "state": "immutable", "households": len(definition), "sha256": content_sha256}

    def _load_profiles(self, batch_id: str) -> list[HouseholdProfile]:
        row = self.connection.execute(
            "SELECT profiles_json FROM household_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("家庭画像批次不存在")
        return [HouseholdProfile.from_dict(item) for item in json.loads(row["profiles_json"])]

    def _load_resources(self, version_id: str) -> list[StagingResource]:
        rows = self.connection.execute(
            "SELECT resource_id,kind,site,base_capacity,daily_turnover,available_from "
            "FROM catalog_resources WHERE version_id=? ORDER BY resource_id",
            (version_id,),
        ).fetchall()
        if not rows:
            raise NotFound("资源版本不存在或没有资源")
        return [
            StagingResource(
                resource_id=item["resource_id"],
                kind=item["kind"],
                site=item["site"],
                base_capacity=int(item["base_capacity"]),
                daily_turnover=int(item["daily_turnover"]),
                available_from=item["available_from"],
            )
            for item in rows
        ]

    # ---- 计划草稿与预演 -----------------------------------------------

    def create_staging_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "staging.plan")
        plan_id = raw.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValidationFailed("plan_id 不能为空")
        batch_id = raw.get("batch_id")
        version_id = raw.get("resource_version_id")
        if not isinstance(batch_id, str) or not isinstance(version_id, str):
            raise ValidationFailed("batch_id 与 resource_version_id 不能为空")
        version = self.connection.execute(
            "SELECT state FROM resource_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if version is None:
            raise NotFound("资源版本不存在")
        if version["state"] != "active":
            raise InvalidState("资源版本已被新版本取代，不能据此创建计划")
        curve = [CurvePoint.from_dict(item) for item in raw.get("curve", [])]
        policy = RollbackPolicy.from_dict(raw.get("rollback_policy"))
        margin = raw.get("turnover_margin_percent", 0)
        if isinstance(margin, bool) or not isinstance(margin, int) or not 0 <= margin <= 100:
            raise ValidationFailed("turnover_margin_percent 必须是 0 到 100 的整数")
        profiles = self._load_profiles(batch_id)
        resources = self._load_resources(version_id)
        projection = build_projection(
            profiles=profiles,
            resources=resources,
            curve=curve,
            margin_percent=Decimal(margin),
        )
        curve_definition = [
            {
                "phase": point.phase,
                "by_date": point.by_date,
                "intake_households": point.intake_households,
                "gates": dict(point.gates),
            }
            for point in sorted(curve, key=lambda item: item.phase)
        ]
        rollback_definition = {"max_depth": policy.max_depth, "retain_evidence": policy.retain_evidence}
        content = {
            "batch_id": batch_id,
            "resource_version_id": version_id,
            "curve": curve_definition,
            "rollback_policy": rollback_definition,
            "turnover_margin_percent": margin,
        }
        content_sha256 = hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO staging_plans(plan_id,batch_id,resource_version_id,curve_json,rollback_json,"
                    "turnover_margin_percent,content_sha256,projection_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id.strip(), batch_id, version_id,
                        canonical_json(curve_definition), canonical_json(rollback_definition),
                        margin, content_sha256, canonical_json(projection), actor_id, self._now(),
                    ),
                )
                self._audit(
                    "staging_plan", plan_id.strip(), "staging_plan.created", actor_id,
                    {"batch_id": batch_id, "resource_version_id": version_id, "sha256": content_sha256},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("计划编号或计划内容已经存在") from exc
        return {"plan_id": plan_id.strip(), "state": "draft", "sha256": content_sha256, "projection": projection}

    def _plan_row(self, plan_id: str, *, require_actor: str | None = None) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM staging_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("入住计划不存在")
        return row

    # ---- 确认与同事务预留 ---------------------------------------------

    def confirm_staging_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "staging.confirm")
        with transaction(self.connection, immediate=True):
            plan = self._plan_row(plan_id)
            if plan["state"] != "draft":
                raise InvalidState(f"计划当前状态为 {plan['state']}，不能确认")
            version = self.connection.execute(
                "SELECT state FROM resource_versions WHERE version_id=?",
                (plan["resource_version_id"],),
            ).fetchone()
            if version is None or version["state"] != "active":
                raise InvalidState("资源版本已变化，请按新版本重新编制计划")
            projection = json.loads(plan["projection_json"])
            blockers = projection["blockers"]
            if blockers:
                raise InvalidState("预演存在容量或周转余量阻断，不能确认预留")
            occupying = self.connection.execute(
                "SELECT plan_id FROM staging_plans WHERE resource_version_id=? "
                "AND state IN ('confirmed','active','completed') AND plan_id<>?",
                (plan["resource_version_id"], plan_id),
            ).fetchall()
            if occupying:
                raise Conflict("该冻结版本已有其他计划占用容量预留")
            for phase in projection["phases"]:
                self.connection.execute(
                    "INSERT INTO plan_phase_states(plan_id,phase_seq,phase,by_date,intake_households,"
                    "cumulative_demand_json,gates_json) VALUES(?,?,?,?,?,?,?)",
                    (
                        plan_id, phase["seq"], phase["phase"], phase["by_date"],
                        phase["intake_households"], canonical_json(phase["cumulative_demand"]),
                        canonical_json(phase["gates"]),
                    ),
                )
                for reservation in projection["reservations"]:
                    if reservation["phase_seq"] != phase["seq"]:
                        continue
                    self.connection.execute(
                        "INSERT INTO plan_phase_reservations(plan_id,phase_seq,resource_id,kind,reserved_qty) "
                        "VALUES(?,?,?,?,?)",
                        (
                            plan_id, reservation["phase_seq"], reservation["resource_id"],
                            reservation["kind"], reservation["reserved_qty"],
                        ),
                    )
            self.connection.execute(
                "UPDATE staging_plans SET state='confirmed',confirmed_by=?,confirmed_at=? WHERE plan_id=?",
                (actor_id, self._now(), plan_id),
            )
            self._audit(
                "staging_plan", plan_id, "staging_plan.confirmed", actor_id,
                {"resource_version_id": plan["resource_version_id"],
                 "reservations": len(projection["reservations"])},
            )
        return {"plan_id": plan_id, "state": "confirmed", "active_phase_seq": None}

    # ---- 闸门评估 -----------------------------------------------------

    def _record_evaluation(
        self, plan_id: str, at_phase_seq: int, gate: str, as_of_date: str,
        blockers: Sequence[Mapping[str, Any]], actor_id: str,
    ) -> None:
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO plan_gate_evaluations(plan_id,at_phase_seq,gate,as_of_date,passed,"
                "blockers_json,evaluated_by,evaluated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    plan_id, at_phase_seq, gate, as_of_date, 1 if not blockers else 0,
                    canonical_json(list(blockers)), actor_id, self._now(),
                ),
            )

    def _cumulative_served(self, plan_id: str, through_seq: int) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT kind,SUM(served_delta) total FROM metric_receipts "
            "WHERE plan_id=? AND phase_seq<=? GROUP BY kind",
            (plan_id, through_seq),
        ).fetchall()
        served = {kind: 0 for kind in ("housing", "school", "clinic", "transit")}
        for row in rows:
            served[row["kind"]] = int(row["total"])
        return served

    def advance_staging_plan(self, actor_id: str, plan_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "staging.advance")
        if not isinstance(as_of_date, str) or not as_of_date.strip():
            raise ValidationFailed("as_of_date 不能为空")
        plan = self._plan_row(plan_id)
        if plan["state"] in ("invalidated",):
            raise InvalidState("计划已因资源版本变化失效，不能推进")
        if plan["state"] not in ("confirmed", "active"):
            raise InvalidState(f"计划当前状态为 {plan['state']}，不能推进")
        current_seq = plan["active_phase_seq"]
        entering_seq = 0 if current_seq is None else current_seq + 1
        if entering_seq >= len(PHASES):
            raise InvalidState("收敛阶段已完成，没有下一阶段")
        resources = self._load_resources(plan["resource_version_id"])
        entering = self.connection.execute(
            "SELECT * FROM plan_phase_states WHERE plan_id=? AND phase_seq=?",
            (plan_id, entering_seq),
        ).fetchone()
        if entering is None:
            raise NotFound("阶段状态不存在")

        blockers: list[dict[str, Any]] = []
        gate = "entry" if current_seq is None else "advance"
        if current_seq is not None:
            current = self.connection.execute(
                "SELECT * FROM plan_phase_states WHERE plan_id=? AND phase_seq=?",
                (plan_id, current_seq),
            ).fetchone()
            blockers.extend(evaluate_metrics(
                phase=current["phase"],
                cumulative_demand=json.loads(current["cumulative_demand_json"]),
                served=self._cumulative_served(plan_id, current_seq),
                gates=json.loads(current["gates_json"]),
            ))
        if blockers:
            self._record_evaluation(plan_id, current_seq if current_seq is not None else 0, "advance", as_of_date, blockers, actor_id)
            raise InvalidState("；".join(item["message"] for item in blockers))

        blockers.extend(evaluate_turnover(
            resources=resources,
            on_date=as_of_date,
            cumulative_demand=json.loads(entering["cumulative_demand_json"]),
            margin_percent=Decimal(plan["turnover_margin_percent"]),
            phase=entering["phase"],
        ))
        if blockers:
            self._record_evaluation(plan_id, entering_seq, gate, as_of_date, blockers, actor_id)
            raise InvalidState("；".join(item["message"] for item in blockers))

        completing = False
        with transaction(self.connection, immediate=True):
            # 重新进入曾经回退的阶段：恢复此前释放的预留，已保留的入住证据继续占用。
            rolled_back_rows = self.connection.execute(
                "SELECT resource_id,reserved_qty,consumed_qty FROM plan_phase_reservations "
                "WHERE plan_id=? AND phase_seq=? AND state='released'",
                (plan_id, entering_seq),
            ).fetchall()
            for row in rolled_back_rows:
                self.connection.execute(
                    "UPDATE plan_phase_reservations SET state='reserved',released_qty=0 "
                    "WHERE plan_id=? AND phase_seq=? AND resource_id=?",
                    (plan_id, entering_seq, row["resource_id"]),
                )
            if current_seq is not None:
                self.connection.execute(
                    "UPDATE plan_phase_states SET status='promoted',promoted_at=? "
                    "WHERE plan_id=? AND phase_seq=?",
                    (self._now(), plan_id, current_seq),
                )
            self.connection.execute(
                "UPDATE plan_phase_states SET status='entered',entered_at=? "
                "WHERE plan_id=? AND phase_seq=?",
                (self._now(), plan_id, entering_seq),
            )
            self.connection.execute(
                "UPDATE staging_plans SET state='active',active_phase_seq=? WHERE plan_id=?",
                (entering_seq, plan_id),
            )
            self.connection.execute(
                "INSERT INTO plan_gate_evaluations(plan_id,at_phase_seq,gate,as_of_date,passed,"
                "blockers_json,evaluated_by,evaluated_at) VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, entering_seq, gate, as_of_date, 1, "[]", actor_id, self._now()),
            )
            self._audit(
                "staging_plan", plan_id, "staging_plan.advanced",
                actor_id, {"phase_seq": entering_seq, "phase": entering["phase"], "as_of_date": as_of_date},
            )
        return {
            "plan_id": plan_id,
            "state": "active",
            "active_phase_seq": entering_seq,
            "phase": entering["phase"],
            "phase_label": PHASE_LABELS[entering["phase"]],
        }

    def complete_staging_plan(self, actor_id: str, plan_id: str, as_of_date: str) -> dict[str, Any]:
        """收敛收尾：收敛阶段服务指标全部达标后计划才进入 completed。"""
        self._require(actor_id, "staging.advance")
        plan = self._plan_row(plan_id)
        if plan["state"] != "active" or plan["active_phase_seq"] != len(PHASES) - 1:
            raise InvalidState("只有进入收敛阶段的计划可以收尾")
        seq = len(PHASES) - 1
        phase = self.connection.execute(
            "SELECT * FROM plan_phase_states WHERE plan_id=? AND phase_seq=?",
            (plan_id, seq),
        ).fetchone()
        blockers = evaluate_metrics(
            phase=phase["phase"],
            cumulative_demand=json.loads(phase["cumulative_demand_json"]),
            served=self._cumulative_served(plan_id, seq),
            gates=json.loads(phase["gates_json"]),
        )
        if blockers:
            self._record_evaluation(plan_id, seq, "complete", as_of_date, blockers, actor_id)
            raise InvalidState("；".join(item["message"] for item in blockers))
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE plan_phase_states SET status='promoted',promoted_at=? "
                "WHERE plan_id=? AND phase_seq=?",
                (self._now(), plan_id, seq),
            )
            self.connection.execute(
                "UPDATE staging_plans SET state='completed' WHERE plan_id=?",
                (plan_id,),
            )
            self.connection.execute(
                "INSERT INTO plan_gate_evaluations(plan_id,at_phase_seq,gate,as_of_date,passed,"
                "blockers_json,evaluated_by,evaluated_at) VALUES(?,?,?,?,?,?,?,?)",
                (plan_id, seq, "complete", as_of_date, 1, "[]", actor_id, self._now()),
            )
            self._audit(
                "staging_plan", plan_id, "staging_plan.completed", actor_id,
                {"phase_seq": seq, "as_of_date": as_of_date},
            )
        return {"plan_id": plan_id, "state": "completed", "active_phase_seq": seq}

    # ---- 幂等回执 -----------------------------------------------------

    def _idempotent(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        table = "intake_receipts" if scope == "intake" else "metric_receipts"
        stored = self.connection.execute(
            f"SELECT request_sha256,response_json FROM {table} WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同回执内容")
        return {**json.loads(stored["response_json"]), "replayed": True}

    def _entering_profile_ids(self, plan: sqlite3.Row, phase_seq: int) -> set[str]:
        projection = json.loads(plan["projection_json"])
        profiles = sorted(
            (item.profile_id for item in self._load_profiles(plan["batch_id"]))
        )
        phase = projection["phases"][phase_seq]
        start = phase["cumulative_households"] - phase["intake_households"]
        return set(profiles[start:phase["cumulative_households"]])

    def record_intake_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        receipt_id = raw.get("receipt_id")
        plan_id = raw.get("plan_id")
        phase_seq = raw.get("phase_seq")
        profile_id = raw.get("household_profile_id")
        key = raw.get("idempotency_key")
        for name, value in (("receipt_id", receipt_id), ("plan_id", plan_id),
                            ("household_profile_id", profile_id), ("idempotency_key", key)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailed(f"{name} 不能为空")
        if isinstance(phase_seq, bool) or not isinstance(phase_seq, int) or not 0 <= phase_seq < len(PHASES):
            raise ValidationFailed("phase_seq 必须是 0 到 3 的整数")
        request_digest = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        replayed = self._idempotent("intake", key, request_digest)
        if replayed is not None:
            return replayed
        existing_household = self.connection.execute(
            "SELECT receipt_id FROM intake_receipts WHERE plan_id=? AND household_profile_id=?",
            (plan_id, profile_id),
        ).fetchone()
        if existing_household is not None:
            raise Conflict("该家庭已有入住回执，不能重复入住")
        plan = self._plan_row(plan_id)
        if plan["state"] != "active" or plan["active_phase_seq"] != phase_seq:
            raise InvalidState("只有当前进行中的阶段可以登记入住回执")
        phase = self.connection.execute(
            "SELECT status FROM plan_phase_states WHERE plan_id=? AND phase_seq=?",
            (plan_id, phase_seq),
        ).fetchone()
        if phase is None or phase["status"] != "entered":
            raise InvalidState("该阶段当前不接收入住")
        if profile_id not in self._entering_profile_ids(plan, phase_seq):
            raise ValidationFailed("家庭不属于该阶段的目标曲线入户范围")
        profile = next(
            (item for item in self._load_profiles(plan["batch_id"]) if item.profile_id == profile_id),
            None,
        )
        quantities = profile.demand
        with transaction(self.connection, immediate=True):
            consumed: dict[str, dict[str, int]] = {}
            for kind, need_total in quantities.items():
                need = need_total
                rows = self.connection.execute(
                    "SELECT resource_id,reserved_qty,consumed_qty,released_qty FROM plan_phase_reservations "
                    "WHERE plan_id=? AND phase_seq=? AND kind=? AND state='reserved' ORDER BY resource_id",
                    (plan_id, phase_seq, kind),
                ).fetchall()
                for row in rows:
                    available = row["reserved_qty"] - row["consumed_qty"] - row["released_qty"]
                    take = min(need, max(0, available))
                    if take:
                        self.connection.execute(
                            "UPDATE plan_phase_reservations SET consumed_qty=consumed_qty+? "
                            "WHERE plan_id=? AND phase_seq=? AND resource_id=?",
                            (take, plan_id, phase_seq, row["resource_id"]),
                        )
                        consumed.setdefault(row["resource_id"], {})[kind] = take
                        need -= take
                    if need == 0:
                        break
                if need > 0:
                    raise InvalidState(
                        f"{KIND_LABELS[kind]}预留容量不足，缺少 {need}；请补足周转或回退后重试"
                    )
            response = {
                "receipt_id": receipt_id,
                "plan_id": plan_id,
                "phase_seq": phase_seq,
                "household_profile_id": profile_id,
                "quantities": quantities,
                "consumed": consumed,
                "state": "recorded",
                "replayed": False,
            }
            self.connection.execute(
                "INSERT INTO intake_receipts(receipt_id,plan_id,phase_seq,household_profile_id,"
                "quantities_json,idempotency_key,request_sha256,response_json,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id, plan_id, phase_seq, profile_id, canonical_json(quantities),
                    key, request_digest, canonical_json(response), actor_id, self._now(),
                ),
            )
            self._audit(
                "intake_receipt", receipt_id, "intake.recorded", actor_id,
                {"plan_id": plan_id, "phase_seq": phase_seq, "household_profile_id": profile_id},
            )
        return response

    def record_metric_receipt(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        receipt_id = raw.get("receipt_id")
        plan_id = raw.get("plan_id")
        phase_seq = raw.get("phase_seq")
        kind = raw.get("kind")
        delta = raw.get("served_delta")
        key = raw.get("idempotency_key")
        for name, value in (("receipt_id", receipt_id), ("plan_id", plan_id), ("idempotency_key", key)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailed(f"{name} 不能为空")
        if isinstance(phase_seq, bool) or not isinstance(phase_seq, int) or not 0 <= phase_seq < len(PHASES):
            raise ValidationFailed("phase_seq 必须是 0 到 3 的整数")
        if kind not in ("housing", "school", "clinic", "transit"):
            raise ValidationFailed("kind 必须是 housing、school、clinic 或 transit")
        if isinstance(delta, bool) or not isinstance(delta, int) or delta < 0:
            raise ValidationFailed("served_delta 必须是非负整数")
        request_digest = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        replayed = self._idempotent("metric", key, request_digest)
        if replayed is not None:
            return replayed
        plan = self._plan_row(plan_id)
        if plan["state"] not in ("active", "completed"):
            raise InvalidState("计划未在执行中，不能登记指标回执")
        phase = self.connection.execute(
            "SELECT status FROM plan_phase_states WHERE plan_id=? AND phase_seq=?",
            (plan_id, phase_seq),
        ).fetchone()
        if phase is None or phase["status"] not in ("entered", "promoted"):
            raise InvalidState("该阶段尚未进入，不能登记指标回执")
        response = {
            "receipt_id": receipt_id,
            "plan_id": plan_id,
            "phase_seq": phase_seq,
            "kind": kind,
            "served_delta": delta,
            "state": "recorded",
            "replayed": False,
        }
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO metric_receipts(receipt_id,plan_id,phase_seq,kind,served_delta,"
                "idempotency_key,request_sha256,response_json,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id, plan_id, phase_seq, kind, delta,
                    key, request_digest, canonical_json(response), actor_id, self._now(),
                ),
            )
            self._audit(
                "metric_receipt", receipt_id, "metric.recorded", actor_id,
                {"plan_id": plan_id, "phase_seq": phase_seq, "kind": kind, "served_delta": delta},
            )
        return response

    # ---- 异常回退（保留证据）------------------------------------------

    def rollback_staging_plan(self, actor_id: str, plan_id: str, to_phase_seq: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "staging.rollback")
        if isinstance(to_phase_seq, bool) or not isinstance(to_phase_seq, int) or not 0 <= to_phase_seq < len(PHASES):
            raise ValidationFailed("to_phase_seq 必须是 0 到 3 的整数")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("回退原因不能为空")
        plan = self._plan_row(plan_id)
        if plan["state"] != "active" or plan["active_phase_seq"] is None:
            raise InvalidState("只有执行中的计划可以回退")
        active_seq = int(plan["active_phase_seq"])
        if to_phase_seq >= active_seq:
            raise ValidationFailed("回退目标阶段必须早于当前阶段")
        policy = json.loads(plan["rollback_json"])
        depth = active_seq - to_phase_seq
        if depth > int(policy["max_depth"]):
            raise InvalidState(
                f"回退跨度 {depth} 个阶段，超过回退方案允许的 {policy['max_depth']} 个阶段"
            )
        released_summary: list[dict[str, Any]] = []
        retained_households = 0
        with transaction(self.connection, immediate=True):
            for seq in range(active_seq, to_phase_seq, -1):
                rows = self.connection.execute(
                    "SELECT resource_id,kind,reserved_qty,consumed_qty,released_qty "
                    "FROM plan_phase_reservations WHERE plan_id=? AND phase_seq=? ORDER BY resource_id",
                    (plan_id, seq),
                ).fetchall()
                for row in rows:
                    releasable = row["reserved_qty"] - row["consumed_qty"] - row["released_qty"]
                    if releasable < 0:
                        releasable = 0
                    self.connection.execute(
                        "UPDATE plan_phase_reservations SET released_qty=released_qty+?,state='released' "
                        "WHERE plan_id=? AND phase_seq=? AND resource_id=?",
                        (releasable, plan_id, seq, row["resource_id"]),
                    )
                    if releasable:
                        released_summary.append({
                            "phase_seq": seq, "resource_id": row["resource_id"],
                            "kind": row["kind"], "released_qty": releasable,
                            "retained_consumed_qty": row["consumed_qty"],
                        })
                retained_households += int(self.connection.execute(
                    "SELECT COUNT(*) c FROM intake_receipts WHERE plan_id=? AND phase_seq=?",
                    (plan_id, seq),
                ).fetchone()["c"])
                self.connection.execute(
                    "UPDATE plan_phase_states SET status='rolled_back',rolled_back_at=? "
                    "WHERE plan_id=? AND phase_seq=?",
                    (self._now(), plan_id, seq),
                )
            self.connection.execute(
                "UPDATE plan_phase_states SET status='entered' WHERE plan_id=? AND phase_seq=?",
                (plan_id, to_phase_seq),
            )
            self.connection.execute(
                "UPDATE staging_plans SET active_phase_seq=? WHERE plan_id=?",
                (to_phase_seq, plan_id),
            )
            self._audit(
                "staging_plan", plan_id, "staging_plan.rolled_back", actor_id,
                {"from_phase_seq": active_seq, "to_phase_seq": to_phase_seq, "reason": reason.strip(),
                 "released": released_summary, "retained_intake_receipts": retained_households,
                 "retain_evidence": bool(policy["retain_evidence"])},
            )
        return {
            "plan_id": plan_id,
            "state": "active",
            "active_phase_seq": to_phase_seq,
            "phase": PHASES[to_phase_seq],
            "released": released_summary,
            "retained_intake_receipts": retained_households,
        }

    def rollback_window(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "staging.read")
        plan = self._plan_row(plan_id)
        policy = json.loads(plan["rollback_json"])
        active_seq = plan["active_phase_seq"]
        if plan["state"] != "active" or active_seq is None:
            return {
                "plan_id": plan_id,
                "state": plan["state"],
                "can_rollback": False,
                "targets": [],
                "retain_evidence": bool(policy["retain_evidence"]),
            }
        active_seq = int(active_seq)
        lowest = max(0, active_seq - int(policy["max_depth"]))
        targets: list[dict[str, Any]] = []
        for seq in range(lowest, active_seq):
            evidence = self.connection.execute(
                "SELECT COUNT(*) c FROM intake_receipts WHERE plan_id=? AND phase_seq>?",
                (plan_id, seq),
            ).fetchone()["c"]
            targets.append({
                "phase_seq": seq,
                "phase": PHASES[seq],
                "phase_label": PHASE_LABELS[PHASES[seq]],
                "retained_intake_receipts": int(evidence),
            })
        return {
            "plan_id": plan_id,
            "state": plan["state"],
            "can_rollback": bool(targets),
            "max_depth": int(policy["max_depth"]),
            "retain_evidence": bool(policy["retain_evidence"]),
            "targets": targets,
        }

    # ---- 查询：来源、阻断、全貌 ----------------------------------------

    def plan_capacity_sources(self, actor_id: str, plan_id: str, phase_seq: int | None = None) -> dict[str, Any]:
        self._require(actor_id, "staging.read")
        plan = self._plan_row(plan_id)
        projection = json.loads(plan["projection_json"])
        result_phases = []
        for phase in projection["phases"]:
            if phase_seq is not None and phase["seq"] != phase_seq:
                continue
            rows = self.connection.execute(
                "SELECT resource_id,kind,reserved_qty,consumed_qty,released_qty,state "
                "FROM plan_phase_reservations WHERE plan_id=? AND phase_seq=? ORDER BY resource_id",
                (plan_id, phase["seq"]),
            ).fetchall()
            actual = {row["resource_id"]: dict(row) for row in rows}
            sources = []
            for source in phase["sources"]:
                used = actual.get(source["resource_id"])
                sources.append({
                    **source,
                    "reserved_qty": None if used is None else used["reserved_qty"],
                    "consumed_qty": None if used is None else used["consumed_qty"],
                    "released_qty": None if used is None else used["released_qty"],
                    "reservation_state": None if used is None else used["state"],
                })
            result_phases.append({
                "seq": phase["seq"],
                "phase": phase["phase"],
                "phase_label": phase["phase_label"],
                "by_date": phase["by_date"],
                "cumulative_demand": phase["cumulative_demand"],
                "sources": sources,
            })
        return {
            "plan_id": plan_id,
            "resource_version_id": plan["resource_version_id"],
            "phases": result_phases,
        }

    def plan_blockers(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "staging.read")
        plan = self._plan_row(plan_id)
        projection = json.loads(plan["projection_json"])
        evaluations = self.connection.execute(
            "SELECT e.* FROM plan_gate_evaluations e "
            "JOIN (SELECT gate,at_phase_seq,MAX(evaluation_id) max_id FROM plan_gate_evaluations "
            "WHERE plan_id=? GROUP BY gate,at_phase_seq) latest "
            "ON latest.max_id=e.evaluation_id ORDER BY e.evaluation_id",
            (plan_id,),
        ).fetchall()
        return {
            "plan_id": plan_id,
            "state": plan["state"],
            "projection_blockers": projection["blockers"],
            "gate_evaluations": [
                {
                    "at_phase_seq": row["at_phase_seq"],
                    "gate": row["gate"],
                    "as_of_date": row["as_of_date"],
                    "passed": bool(row["passed"]),
                    "blockers": json.loads(row["blockers_json"]),
                    "evaluated_at": row["evaluated_at"],
                }
                for row in evaluations
            ],
        }

    def staging_plan(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "staging.read")
        plan = self._plan_row(plan_id)
        phase_states = self.connection.execute(
            "SELECT * FROM plan_phase_states WHERE plan_id=? ORDER BY phase_seq",
            (plan_id,),
        ).fetchall()
        state_by_seq = {row["phase_seq"]: row for row in phase_states}
        projection = json.loads(plan["projection_json"])
        phases = []
        for phase in projection["phases"]:
            state = state_by_seq.get(phase["seq"])
            reservations = self.connection.execute(
                "SELECT resource_id,kind,reserved_qty,consumed_qty,released_qty,state "
                "FROM plan_phase_reservations WHERE plan_id=? AND phase_seq=? ORDER BY resource_id",
                (plan_id, phase["seq"]),
            ).fetchall()
            phases.append({
                **phase,
                "status": None if state is None else state["status"],
                "entered_at": None if state is None else state["entered_at"],
                "promoted_at": None if state is None else state["promoted_at"],
                "rolled_back_at": None if state is None else state["rolled_back_at"],
                "reservations": [dict(row) for row in reservations],
            })
        intake = self.connection.execute(
            "SELECT phase_seq,COUNT(*) c FROM intake_receipts WHERE plan_id=? GROUP BY phase_seq",
            (plan_id,),
        ).fetchall()
        return {
            "plan_id": plan["plan_id"],
            "state": plan["state"],
            "batch_id": plan["batch_id"],
            "resource_version_id": plan["resource_version_id"],
            "turnover_margin_percent": plan["turnover_margin_percent"],
            "active_phase_seq": plan["active_phase_seq"],
            "invalidated_by_version": plan["invalidated_by_version"],
            "created_at": plan["created_at"],
            "confirmed_at": plan["confirmed_at"],
            "curve": json.loads(plan["curve_json"]),
            "rollback_policy": json.loads(plan["rollback_json"]),
            "phases": phases,
            "intake_evidence": {str(row["phase_seq"]): row["c"] for row in intake},
        }
