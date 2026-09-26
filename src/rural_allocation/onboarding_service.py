"""入住容量预演与分阶段切换的事务用例。

关键不变量：
- 家庭画像、目标曲线、回退方案在确认时固化（哈希存证），之后不可修改；
- 确认计划与容量预留处于同一个 SQLite 事务，要么同时生效要么都不生效；
- 冻结资源版本一旦变化（同一容量来源出现新 source_revision），引用旧版本
  的计划立即失效；
- 入住回执走幂等表，重复回执不会再次扣减容量；
- 异常回退只改阶段状态，入住回执等证据一律保留。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import FrozenResourceVersion
from .onboarding import (
    METRIC_KEYS,
    RESOURCE_KINDS,
    STAGE_CODES,
    HouseholdProfile,
    PlanRules,
    ResourceItem,
    allocate_sources,
    capacity_shortfalls,
    cumulative_demands,
    evaluate_stage_gate,
    ordered_households,
    rollback_reachable,
)
from .planning import canonical_json, digest
from .storage import transaction


class OnboardingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()

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
        from .service import ROLE_PERMISSIONS

        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
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

    # ------------------------------------------------------------------
    # 冻结的住房及公共服务容量版本
    # ------------------------------------------------------------------

    def freeze_resource_version(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "resource_version.write")
        version = FrozenResourceVersion.from_dict(raw)
        definition = canonical_json(raw)
        content_sha256 = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        now = self._now()
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO resource_versions(version_id,definition_json,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (version.version_id, definition, content_sha256, actor_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("容量版本编号已经存在") from exc
            self.connection.executemany(
                "INSERT INTO resource_version_items(version_id,resource_id,kind,provider_name,capacity,"
                "source_revision) VALUES(?,?,?,?,?,?)",
                [
                    (
                        version.version_id,
                        item.resource_id,
                        item.kind,
                        item.provider_name,
                        item.capacity,
                        item.source_revision,
                    )
                    for item in version.items
                ],
            )
            # 同一容量来源出现新的来源修订，即视为版本变化，旧计划失效。
            for item in version.items:
                old = self.connection.execute(
                    "SELECT v.version_id,v.state,i.source_revision FROM resource_version_items i "
                    "JOIN resource_versions v ON v.version_id=i.version_id "
                    "WHERE i.resource_id=? AND v.version_id<>?",
                    (item.resource_id, version.version_id),
                ).fetchall()
                for row in old:
                    if row["source_revision"] == item.source_revision:
                        continue
                    if row["state"] == "frozen":
                        self.connection.execute(
                            "UPDATE resource_versions SET state='superseded' WHERE version_id=?",
                            (row["version_id"],),
                        )
                    cursor = self.connection.execute(
                        "UPDATE onboarding_plans SET state='invalidated',invalidated_reason=? "
                        "WHERE version_id=? AND state='confirmed'",
                        (
                            f"容量来源 {item.resource_id} 修订由 {row['source_revision']} 变为 {item.source_revision}",
                            row["version_id"],
                        ),
                    )
                    if cursor.rowcount:
                        self._audit(
                            "onboarding_plan",
                            row["version_id"],
                            "plan.invalidated",
                            actor_id,
                            {
                                "resource_id": item.resource_id,
                                "old_revision": row["source_revision"],
                                "new_revision": item.source_revision,
                                "new_version_id": version.version_id,
                            },
                        )
            self._audit(
                "resource_version",
                version.version_id,
                "resource_version.frozen",
                actor_id,
                {"sha256": content_sha256, "items": len(version.items)},
            )
        return {
            "version_id": version.version_id,
            "state": "frozen",
            "sha256": content_sha256,
            "items": len(version.items),
        }

    def resource_version(self, version_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM resource_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("容量版本不存在")
        items = self.connection.execute(
            "SELECT resource_id,kind,provider_name,capacity,source_revision "
            "FROM resource_version_items WHERE version_id=? ORDER BY kind,resource_id",
            (version_id,),
        ).fetchall()
        return {
            "version_id": version_id,
            "state": row["state"],
            "revision": row["revision"],
            "sha256": row["content_sha256"],
            "items": [dict(item) for item in items],
        }

    # ------------------------------------------------------------------
    # 计划确认（与资源预留同一事务）
    # ------------------------------------------------------------------

    def confirm_plan(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "plan.write")
        plan_id = raw.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValidationFailed("plan_id 不能为空")
        plan_id = plan_id.strip()
        version_id = raw.get("version_id")
        if not isinstance(version_id, str) or not version_id.strip():
            raise ValidationFailed("version_id 不能为空")
        version_id = version_id.strip()
        taskforce_id = raw.get("taskforce_id")
        if not isinstance(taskforce_id, str) or not taskforce_id.strip():
            raise ValidationFailed("taskforce_id 不能为空")
        idempotency_key = raw.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValidationFailed("idempotency_key 不能为空")
        idempotency_key = idempotency_key.strip()

        households_raw = raw.get("households")
        if not isinstance(households_raw, list) or not households_raw:
            raise ValidationFailed("households 必须是非空数组")
        profiles = [HouseholdProfile.from_dict(item) for item in households_raw]
        if len({profile.household_id for profile in profiles}) != len(profiles):
            raise ValidationFailed("家庭画像编号不能重复")
        rules_raw = raw.get("rules")
        if not isinstance(rules_raw, Mapping):
            raise ValidationFailed("rules 必须是对象")
        rules = PlanRules.from_dict(rules_raw, len(profiles))

        request_sha256 = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='onboarding_plan' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha256:
                raise Conflict("幂等键对应不同入住计划内容")
            return json.loads(stored["response_json"])

        demands = cumulative_demands(profiles, rules.target_curve)
        profiles_json = canonical_json(households_raw)
        rules_json = canonical_json(rules_raw)
        profiles_sha256 = hashlib.sha256(profiles_json.encode("utf-8")).hexdigest()
        rules_sha256 = hashlib.sha256(rules_json.encode("utf-8")).hexdigest()
        now = self._now()
        response: dict[str, Any] = {
            "plan_id": plan_id,
            "version_id": version_id,
            "state": "confirmed",
            "current_stage": "prepare",
            "revision": 1,
            "stages": [
                {"stage": code, "target_households": target, "demand": demand}
                for code, target, demand in zip(STAGE_CODES, rules.target_curve, demands)
            ],
        }
        with transaction(self.connection, immediate=True):
            # 拿写锁后复核版本状态与其它计划预留，消除并发确认窗口。
            version = self.connection.execute(
                "SELECT state FROM resource_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise NotFound("容量版本不存在")
            if version["state"] != "frozen":
                raise InvalidState("容量版本已被新版本取代，不能确认计划")
            items = self.connection.execute(
                "SELECT resource_id,kind,provider_name,capacity FROM resource_version_items WHERE version_id=?",
                (version_id,),
            ).fetchall()
            resources = [
                ResourceItem(row["resource_id"], row["kind"], row["provider_name"], int(row["capacity"]))
                for row in items
            ]
            resources_by_kind = {kind: [] for kind in RESOURCE_KINDS}
            for resource in resources:
                resources_by_kind[resource.kind].append(resource)
            blockers = capacity_shortfalls(
                resources_by_kind, demands, self._reserved_by_kind(version_id, exclude_plan=None)
            )
            if blockers:
                raise InvalidState("容量不足以确认计划：" + "; ".join(item["message"] for item in blockers))
            try:
                self.connection.execute(
                    "INSERT INTO onboarding_plans(plan_id,taskforce_id,version_id,profiles_json,rules_json,"
                    "profiles_sha256,rules_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        plan_id,
                        taskforce_id.strip(),
                        version_id,
                        profiles_json,
                        rules_json,
                        profiles_sha256,
                        rules_sha256,
                        actor_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("入住计划编号冲突或容量版本不存在") from exc
            self.connection.executemany(
                "INSERT INTO onboarding_plan_stages(plan_id,stage_index,stage_code,target_households,"
                "demand_json,state,entered_at) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        plan_id,
                        index,
                        code,
                        rules.target_curve[index],
                        canonical_json(demands[index]),
                        "active" if index == 0 else "pending",
                        now if index == 0 else None,
                    )
                    for index, code in enumerate(STAGE_CODES)
                ],
            )
            # 预留量以收敛阶段（全部家庭入住）的累计需求为准，与计划同事务落库。
            self.connection.executemany(
                "INSERT INTO onboarding_reservations(plan_id,kind,reserved_units) VALUES(?,?,?)",
                [(plan_id, kind, demands[-1][kind]) for kind in RESOURCE_KINDS],
            )
            self.connection.execute(
                "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('onboarding_plan',?,?,?,?)",
                (idempotency_key, request_sha256, canonical_json(response), now),
            )
            self.connection.execute(
                "INSERT INTO onboarding_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, 0, "plan.confirmed", canonical_json({"version_id": version_id}), actor_id, now),
            )
            self._audit(
                "onboarding_plan",
                plan_id,
                "plan.confirmed",
                actor_id,
                {
                    "version_id": version_id,
                    "profiles_sha256": profiles_sha256,
                    "rules_sha256": rules_sha256,
                    "households": len(profiles),
                    "reservations": demands[-1],
                },
            )
        return response

    def _reserved_by_kind(self, version_id: str, exclude_plan: str | None) -> dict[str, int]:
        # 已确认计划整段预留容量；已收敛计划的家庭已经入住，容量同样被占用，
        # 两者都要计入版本剩余量。
        query = (
            "SELECT kind,SUM(reserved_units) AS total FROM onboarding_reservations r "
            "JOIN onboarding_plans p ON p.plan_id=r.plan_id "
            "WHERE p.version_id=? AND p.state IN ('confirmed','converged')"
        )
        params: list[Any] = [version_id]
        if exclude_plan is not None:
            query += " AND r.plan_id<>?"
            params.append(exclude_plan)
        query += " GROUP BY kind"
        totals = {kind: 0 for kind in RESOURCE_KINDS}
        for row in self.connection.execute(query, params):
            totals[row["kind"]] = int(row["total"] or 0)
        return totals

    def _load_plan(self, plan_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM onboarding_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound("入住计划不存在")
        return row

    def _plan_context(self, plan_row: sqlite3.Row) -> dict[str, Any]:
        profiles = [HouseholdProfile.from_dict(item) for item in json.loads(plan_row["profiles_json"])]
        rules = PlanRules.from_dict(json.loads(plan_row["rules_json"]), len(profiles))
        demands = cumulative_demands(profiles, rules.target_curve)
        items = self.connection.execute(
            "SELECT resource_id,kind,provider_name,capacity,source_revision "
            "FROM resource_version_items WHERE version_id=? ORDER BY kind,resource_id",
            (plan_row["version_id"],),
        ).fetchall()
        resources = [
            ResourceItem(row["resource_id"], row["kind"], row["provider_name"], int(row["capacity"]))
            for row in items
        ]
        resources_by_kind = {kind: [] for kind in RESOURCE_KINDS}
        for resource in resources:
            resources_by_kind[resource.kind].append(resource)
        version_capacity = {
            kind: sum(item.capacity for item in resources_by_kind[kind]) for kind in RESOURCE_KINDS
        }
        return {
            "profiles": profiles,
            "rules": rules,
            "demands": demands,
            "resources": resources,
            "resources_by_kind": resources_by_kind,
            "version_capacity": version_capacity,
            "items": [dict(row) for row in items],
        }

    # ------------------------------------------------------------------
    # 入住回执（幂等，重复回执不重复扣减容量）
    # ------------------------------------------------------------------

    def record_checkin(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "checkin.write")
        receipt_id = raw.get("receipt_id")
        plan_id = raw.get("plan_id")
        household_id = raw.get("household_id")
        idempotency_key = raw.get("idempotency_key")
        for field, value in (("receipt_id", receipt_id), ("plan_id", plan_id),
                             ("household_id", household_id), ("idempotency_key", idempotency_key)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailed(f"{field} 不能为空")
        receipt_id, plan_id, household_id, idempotency_key = (
            receipt_id.strip(), plan_id.strip(), household_id.strip(), idempotency_key.strip(),
        )
        resource_id = raw.get("resource_id")
        kind = raw.get("kind")
        units = 0
        if resource_id is not None or kind is not None:
            if not isinstance(resource_id, str) or not resource_id.strip():
                raise ValidationFailed("resource_id 必须是字符串")
            if not isinstance(kind, str) or kind not in RESOURCE_KINDS:
                raise ValidationFailed("kind 必须是 housing_unit、school_seat、primary_care 或 transit")
            resource_id = resource_id.strip()
            units_value = raw.get("units", 1)
            if isinstance(units_value, bool) or not isinstance(units_value, int) or units_value <= 0:
                raise ValidationFailed("units 必须是正整数")
            units = units_value

        request_sha256 = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM supply_idempotency "
            "WHERE scope='onboarding_checkin' AND idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_sha256:
                raise Conflict("幂等键对应不同入住回执内容")
            return json.loads(stored["response_json"])

        plan_row = self._load_plan(plan_id)
        context = self._plan_context(plan_row)
        profile_ids = {profile.household_id for profile in context["profiles"]}
        if household_id not in profile_ids:
            raise ValidationFailed("家庭不在计划冻结的画像中")
        if resource_id is not None:
            version_ids = {row["resource_id"] for row in context["items"]}
            if resource_id not in version_ids:
                raise ValidationFailed("容量来源不在计划冻结的版本中")
            version_row = next(row for row in context["items"] if row["resource_id"] == resource_id)
            if version_row["kind"] != kind:
                raise ValidationFailed("容量来源类型与 kind 不一致")

        now = self._now()
        with transaction(self.connection, immediate=True):
            # 拿写锁后复核计划状态与当前阶段开放范围，避免与推进/回退并发。
            locked = self._load_plan(plan_id)
            if locked["state"] != "confirmed":
                raise InvalidState(f"入住计划处于 {locked['state']} 状态，不能登记回执")
            active_stage = int(locked["current_stage"])
            rules = PlanRules.from_dict(json.loads(locked["rules_json"]), len(context["profiles"]))
            open_households = {
                profile.household_id
                for profile in ordered_households(context["profiles"])[: rules.target_curve[active_stage]]
            }
            if household_id not in open_households:
                raise InvalidState("该家庭不在当前阶段的目标曲线范围内，不能提前入住")
            try:
                self.connection.execute(
                    "INSERT INTO onboarding_checkins(receipt_id,plan_id,stage_index,household_id,resource_id,"
                    "kind,units,idempotency_key,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt_id, plan_id, active_stage, household_id,
                        resource_id, kind, units, idempotency_key, actor_id, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("回执编号冲突、幂等键冲突或该家庭已有入住回执") from exc
            total = int(self.connection.execute(
                "SELECT COUNT(*) AS c FROM onboarding_checkins WHERE plan_id=? AND state='recorded'",
                (plan_id,),
            ).fetchone()["c"])
            response = {
                "receipt_id": receipt_id,
                "plan_id": plan_id,
                "household_id": household_id,
                "stage": STAGE_CODES[active_stage],
                "state": "recorded",
                "checked_in_total": total,
                "replayed": False,
            }
            self.connection.execute(
                "INSERT INTO supply_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('onboarding_checkin',?,?,?,?)",
                (idempotency_key, request_sha256, canonical_json(response), now),
            )
            self._audit(
                "onboarding_checkin",
                receipt_id,
                "checkin.recorded",
                actor_id,
                {"plan_id": plan_id, "household_id": household_id, "stage": STAGE_CODES[active_stage]},
            )
        return response

    # ------------------------------------------------------------------
    # 阶段指标与门禁
    # ------------------------------------------------------------------

    def record_metrics(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "metric.write")
        plan_id = raw.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValidationFailed("plan_id 不能为空")
        metrics_raw = raw.get("metrics")
        if not isinstance(metrics_raw, Mapping):
            raise ValidationFailed("metrics 必须是对象")
        metrics: dict[str, int] = {}
        for key in METRIC_KEYS:
            value = metrics_raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise ValidationFailed(f"metrics.{key} 必须是 0 到 100 的整数百分比")
            metrics[key] = value
        plan_row = self._load_plan(plan_id.strip())
        if plan_row["state"] != "confirmed":
            raise InvalidState(f"入住计划处于 {plan_row['state']} 状态，不能上报指标")
        stage_index = int(plan_row["current_stage"])
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO onboarding_metric_reports(plan_id,stage_index,metrics_json,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (plan_id.strip(), stage_index, canonical_json(metrics), actor_id, now),
            )
            report_id = int(cursor.lastrowid)
            self._audit(
                "onboarding_plan", plan_id.strip(), "metrics.recorded", actor_id,
                {"report_id": report_id, "stage": STAGE_CODES[stage_index], **metrics},
            )
        return {"report_id": report_id, "plan_id": plan_id.strip(), "stage": STAGE_CODES[stage_index], "metrics": metrics}

    def _latest_metrics(self, plan_id: str, stage_index: int) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT metrics_json FROM onboarding_metric_reports WHERE plan_id=? AND stage_index=? "
            "ORDER BY report_id DESC LIMIT 1",
            (plan_id, stage_index),
        ).fetchone()
        if row is None:
            return {}
        return {key: int(value) for key, value in json.loads(row["metrics_json"]).items()}

    def _checked_in_count(self, plan_id: str) -> int:
        return int(self.connection.execute(
            "SELECT COUNT(*) AS c FROM onboarding_checkins WHERE plan_id=? AND state='recorded'",
            (plan_id,),
        ).fetchone()["c"])

    def _live_blockers(self, plan_row: sqlite3.Row, context: Mapping[str, Any]) -> list[dict[str, object]]:
        current_index = int(plan_row["current_stage"])
        rules: PlanRules = context["rules"]
        metrics = self._latest_metrics(plan_row["plan_id"], current_index) if current_index >= 1 else {
            key: 100 for key in METRIC_KEYS
        }
        other_reserved = self._reserved_by_kind(plan_row["version_id"], exclude_plan=plan_row["plan_id"])
        return evaluate_stage_gate(
            stage_index=current_index,
            checked_in_households=self._checked_in_count(plan_row["plan_id"]),
            metrics=metrics,
            rules=rules,
            demands_by_stage=context["demands"],
            version_capacity=context["version_capacity"],
            other_reserved=other_reserved,
        )

    def advance_stage(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "stage.advance")
        plan_row = self._load_plan(plan_id)
        if plan_row["state"] != "confirmed":
            raise InvalidState(f"入住计划处于 {plan_row['state']} 状态，不能推进阶段")
        current_index = int(plan_row["current_stage"])
        if current_index >= len(STAGE_CODES) - 1:
            raise InvalidState("收敛阶段之后没有可推进的阶段")
        context = self._plan_context(plan_row)

        next_index = current_index + 1
        now = self._now()
        with transaction(self.connection, immediate=True):
            # 拿写锁后复核计划状态、门禁与周转余量，避免并发回执/回退绕过校验。
            locked = self._load_plan(plan_id)
            if locked["state"] != "confirmed" or int(locked["current_stage"]) != current_index:
                raise InvalidState("计划状态已变化，请刷新后重试")
            blockers = self._live_blockers(locked, context)
            if blockers:
                # 阻断原因本身留痕（与本次尝试同一事务），便于查询“为什么没开放下一批”。
                self.connection.execute(
                    "INSERT INTO onboarding_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, current_index, "stage.blocked", canonical_json(blockers), actor_id, now),
                )
                self._audit(
                    "onboarding_plan", plan_id, "stage.blocked", actor_id,
                    {"stage": STAGE_CODES[current_index], "reasons": [item["code"] for item in blockers]},
                )
                raise InvalidState("阶段门禁未通过：" + "; ".join(str(item["message"]) for item in blockers))
            self.connection.execute(
                "UPDATE onboarding_plan_stages SET state='passed',passed_at=? "
                "WHERE plan_id=? AND stage_index=? AND state='active'",
                (now, plan_id, current_index),
            )
            self.connection.execute(
                "UPDATE onboarding_plan_stages SET state='active',entered_at=? "
                "WHERE plan_id=? AND stage_index=? AND state IN ('pending','rolled_back')",
                (now, plan_id, next_index),
            )
            self.connection.execute(
                "UPDATE onboarding_plans SET current_stage=?,revision=revision+1 WHERE plan_id=?",
                (next_index, plan_id),
            )
            self.connection.execute(
                "INSERT INTO onboarding_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, next_index, "stage.advanced",
                 canonical_json({"from": STAGE_CODES[current_index], "to": STAGE_CODES[next_index]}),
                 actor_id, now),
            )
            self._audit(
                "onboarding_plan", plan_id, "stage.advanced", actor_id,
                {"from": STAGE_CODES[current_index], "to": STAGE_CODES[next_index]},
            )
        return {
            "plan_id": plan_id,
            "state": "confirmed",
            "current_stage": STAGE_CODES[next_index],
            "revision": int(locked["revision"]) + 1,
        }

    def complete_convergence(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        """收敛阶段指标与入住全部到位后收口计划。"""
        self._require(actor_id, "stage.advance")
        plan_row = self._load_plan(plan_id)
        if plan_row["state"] != "confirmed":
            raise InvalidState(f"入住计划处于 {plan_row['state']} 状态，不能收口")
        if int(plan_row["current_stage"]) != len(STAGE_CODES) - 1:
            raise InvalidState("只有收敛阶段可以收口")
        context = self._plan_context(plan_row)
        now = self._now()
        with transaction(self.connection, immediate=True):
            locked = self._load_plan(plan_id)
            if locked["state"] != "confirmed" or int(locked["current_stage"]) != len(STAGE_CODES) - 1:
                raise InvalidState("计划状态已变化，请刷新后重试")
            blockers = self._live_blockers(locked, context)
            if blockers:
                self.connection.execute(
                    "INSERT INTO onboarding_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (plan_id, len(STAGE_CODES) - 1, "stage.blocked", canonical_json(blockers), actor_id, now),
                )
                raise InvalidState("收敛门禁未通过：" + "; ".join(str(item["message"]) for item in blockers))
            self.connection.execute(
                "UPDATE onboarding_plan_stages SET state='passed',passed_at=? "
                "WHERE plan_id=? AND stage_index=? AND state='active'",
                (now, plan_id, len(STAGE_CODES) - 1),
            )
            self.connection.execute(
                "UPDATE onboarding_plans SET state='converged',revision=revision+1 WHERE plan_id=?",
                (plan_id,),
            )
            self.connection.execute(
                "INSERT INTO onboarding_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, len(STAGE_CODES) - 1, "plan.converged", "{}", actor_id, now),
            )
            self._audit("onboarding_plan", plan_id, "plan.converged", actor_id, {})
        return {"plan_id": plan_id, "state": "converged", "current_stage": "converge",
                "revision": int(locked["revision"]) + 1}

    # ------------------------------------------------------------------
    # 异常回退（保留入住证据）
    # ------------------------------------------------------------------

    def rollback_stage(self, actor_id: str, plan_id: str, to_stage: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "stage.rollback")
        plan_row = self._load_plan(plan_id)
        if plan_row["state"] != "confirmed":
            raise InvalidState(f"入住计划处于 {plan_row['state']} 状态，不能回退")
        current_index = int(plan_row["current_stage"])
        context = self._plan_context(plan_row)
        rules: PlanRules = context["rules"]
        reachable = rollback_reachable(rules, current_index)
        if not reachable:
            raise InvalidState("当前阶段的回退方案不允许回退")
        if to_stage is None:
            target_index = reachable[-1]
        else:
            if to_stage not in STAGE_CODES:
                raise ValidationFailed("to_stage 不是受支持的阶段")
            target_index = STAGE_CODES.index(to_stage)
            if target_index not in reachable:
                raise InvalidState(
                    f"回退方案最多允许回退到 {STAGE_CODES[reachable[-1]]}，不能回退到 {to_stage}"
                )
        if target_index == current_index:
            raise InvalidState("目标阶段必须早于当前阶段")
        now = self._now()
        with transaction(self.connection, immediate=True):
            locked = self._load_plan(plan_id)
            if locked["state"] != "confirmed" or int(locked["current_stage"]) != current_index:
                raise InvalidState("计划状态已变化，请刷新后重试")
            for index in range(target_index + 1, current_index + 1):
                self.connection.execute(
                    "UPDATE onboarding_plan_stages SET state='rolled_back' "
                    "WHERE plan_id=? AND stage_index=? AND state IN ('active','passed')",
                    (plan_id, index),
                )
            self.connection.execute(
                "UPDATE onboarding_plan_stages SET state='active' "
                "WHERE plan_id=? AND stage_index=?",
                (plan_id, target_index),
            )
            self.connection.execute(
                "UPDATE onboarding_plans SET current_stage=?,revision=revision+1 WHERE plan_id=?",
                (target_index, plan_id),
            )
            # 注意：onboarding_checkins 一行都不删除、不反转，入住证据原样保留。
            evidence = int(self.connection.execute(
                "SELECT COUNT(*) AS c FROM onboarding_checkins WHERE plan_id=? AND state='recorded'",
                (plan_id,),
            ).fetchone()["c"])
            self.connection.execute(
                "INSERT INTO onboarding_stage_events(plan_id,stage_index,event_type,detail_json,actor_id,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (plan_id, target_index, "stage.rolled_back",
                 canonical_json({
                     "from": STAGE_CODES[current_index],
                     "to": STAGE_CODES[target_index],
                     "preserved_checkins": evidence,
                 }),
                 actor_id, now),
            )
            self._audit(
                "onboarding_plan", plan_id, "stage.rolled_back", actor_id,
                {"from": STAGE_CODES[current_index], "to": STAGE_CODES[target_index],
                 "preserved_checkins": evidence},
            )
        return {
            "plan_id": plan_id,
            "state": "confirmed",
            "current_stage": STAGE_CODES[target_index],
            "preserved_checkins": evidence,
            "revision": int(plan_row["revision"]) + 1,
        }

    # ------------------------------------------------------------------
    # 查询：容量来源、阻断原因、可回退范围
    # ------------------------------------------------------------------

    def plan_status(self, actor_id: str, plan_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        plan_row = self._load_plan(plan_id)
        context = self._plan_context(plan_row)
        rules: PlanRules = context["rules"]
        demands = context["demands"]
        resources_by_kind: Mapping[str, Sequence[ResourceItem]] = context["resources_by_kind"]
        other_reserved = self._reserved_by_kind(plan_row["version_id"], exclude_plan=plan_id)
        current_index = int(plan_row["current_stage"])

        stage_rows = self.connection.execute(
            "SELECT * FROM onboarding_plan_stages WHERE plan_id=? ORDER BY stage_index",
            (plan_id,),
        ).fetchall()
        stages: list[dict[str, Any]] = []
        for row in stage_rows:
            index = int(row["stage_index"])
            demand = json.loads(row["demand_json"])
            sources = {}
            for kind in RESOURCE_KINDS:
                rows = allocate_sources(resources_by_kind[kind], demand[kind])
                sources[kind] = {
                    "demand": demand[kind],
                    "version_capacity": sum(item.capacity for item in resources_by_kind[kind]),
                    "other_plans_reserved": other_reserved[kind],
                    "items": rows,
                }
            stages.append({
                "stage": row["stage_code"],
                "state": row["state"],
                "target_households": row["target_households"],
                "entered_at": row["entered_at"],
                "passed_at": row["passed_at"],
                "demand": demand,
                "capacity_sources": sources,
            })

        reachable = rollback_reachable(rules, current_index) if plan_row["state"] == "confirmed" else []
        last_blocked = self.connection.execute(
            "SELECT detail_json,created_at FROM onboarding_stage_events "
            "WHERE plan_id=? AND event_type='stage.blocked' ORDER BY event_id DESC LIMIT 1",
            (plan_id,),
        ).fetchone()
        gate_blockers = self._live_blockers(plan_row, context) if plan_row["state"] == "confirmed" else []
        receipts = self.connection.execute(
            "SELECT receipt_id,household_id,stage_index,resource_id,kind,units,recorded_at "
            "FROM onboarding_checkins WHERE plan_id=? AND state='recorded' ORDER BY recorded_at,receipt_id",
            (plan_id,),
        ).fetchall()

        return {
            "plan_id": plan_id,
            "taskforce_id": plan_row["taskforce_id"],
            "version_id": plan_row["version_id"],
            "state": plan_row["state"],
            "invalidated_reason": plan_row["invalidated_reason"],
            "current_stage": STAGE_CODES[current_index],
            "revision": plan_row["revision"],
            "profiles_sha256": plan_row["profiles_sha256"],
            "rules_sha256": plan_row["rules_sha256"],
            "metric_thresholds": dict(rules.metric_thresholds),
            "turnover_margin_percent": rules.turnover_margin_percent,
            "checked_in_total": self._checked_in_count(plan_id),
            "latest_metrics": self._latest_metrics(plan_id, current_index),
            "stages": stages,
            "gate_blockers": gate_blockers,
            "last_blocked_event": None if last_blocked is None else {
                "created_at": last_blocked["created_at"],
                "blockers": json.loads(last_blocked["detail_json"]),
            },
            "rollback": {
                "policy": dict(rules.rollback_steps),
                "reachable_stages": [STAGE_CODES[index] for index in reachable],
            },
            "checkin_evidence": [dict(row) for row in receipts],
        }

    def list_plans(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "plan.read")
        rows = self.connection.execute(
            "SELECT plan_id,taskforce_id,version_id,state,current_stage,invalidated_reason,revision,created_at "
            "FROM onboarding_plans ORDER BY plan_id"
        ).fetchall()
        return {"plans": [
            {
                "plan_id": row["plan_id"],
                "taskforce_id": row["taskforce_id"],
                "version_id": row["version_id"],
                "state": row["state"],
                "current_stage": STAGE_CODES[int(row["current_stage"])],
                "invalidated_reason": row["invalidated_reason"],
                "revision": row["revision"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]}
