"""供应服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS supply_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','dispatcher','risk','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_index_quotes (
    quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_index TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    close_cny TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    supersedes_quote_id INTEGER REFERENCES market_index_quotes(quote_id),
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(market_index, trade_date, source_revision)
);

CREATE INDEX IF NOT EXISTS idx_quotes_series
ON market_index_quotes(market_index, trade_date, quote_id);

CREATE TABLE IF NOT EXISTS facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    timezone TEXT NOT NULL,
    capacity_mu TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    origin_id TEXT NOT NULL REFERENCES facilities(facility_id),
    destination_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    daily_capacity TEXT NOT NULL,
    loss_basis_points INTEGER NOT NULL,
    transit_hours INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','retired')),
    created_at TEXT NOT NULL,
    CHECK(origin_id <> destination_id)
);

CREATE TABLE IF NOT EXISTS route_outages (
    outage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    capacity_percent TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'announced' CHECK(state IN ('announced','active','closed','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outages_route_time
ON route_outages(route_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS inventory_lots (
    lot_id TEXT PRIMARY KEY,
    facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    product TEXT NOT NULL,
    grade TEXT NOT NULL,
    quantity_mu TEXT NOT NULL,
    available_mu TEXT NOT NULL,
    unit_cost_cny TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inventory_available
ON inventory_lots(facility_id, product, received_at);

CREATE TABLE IF NOT EXISTS inventory_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    delta_mu TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nominations (
    nomination_id TEXT PRIMARY KEY,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    shipper_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    requested_mu TEXT NOT NULL,
    allocated_mu TEXT NOT NULL DEFAULT '0',
    delivered_mu TEXT NOT NULL DEFAULT '0',
    priority INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','allocated','in_transit','delivered','cancelled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nominations_schedule
ON nominations(route_id, service_date, priority, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_runs (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    service_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    available_capacity TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(route_id, service_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS transfers (
    transfer_id TEXT PRIMARY KEY,
    nomination_id TEXT NOT NULL UNIQUE REFERENCES nominations(nomination_id),
    inventory_lot_id TEXT NOT NULL REFERENCES inventory_lots(lot_id),
    surveyed_mu TEXT NOT NULL,
    expected_delivered_mu TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at TEXT,
    state TEXT NOT NULL DEFAULT 'in_transit' CHECK(state IN ('in_transit','delivered','disputed')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_scenarios (
    scenario_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','approved','retired')),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scenario_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL REFERENCES supply_scenarios(scenario_id),
    as_of_date TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(scenario_id, as_of_date, input_sha256)
);

CREATE TABLE IF NOT EXISTS supply_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS supply_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_supply_audit_entity
ON supply_audit_events(entity_type, entity_id, event_id);

CREATE TABLE IF NOT EXISTS resource_versions (
    version_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','superseded')),
    published_by TEXT NOT NULL REFERENCES supply_users(user_id),
    published_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS catalog_resources (
    version_id TEXT NOT NULL REFERENCES resource_versions(version_id),
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('housing','school','clinic','transit')),
    site TEXT NOT NULL,
    base_capacity INTEGER NOT NULL CHECK(base_capacity >= 0),
    daily_turnover INTEGER NOT NULL CHECK(daily_turnover >= 0),
    available_from TEXT NOT NULL,
    PRIMARY KEY(version_id, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_catalog_resources_kind
ON catalog_resources(version_id, kind, resource_id);

CREATE TABLE IF NOT EXISTS household_batches (
    batch_id TEXT PRIMARY KEY,
    profiles_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    household_count INTEGER NOT NULL CHECK(household_count > 0),
    submitted_by TEXT NOT NULL REFERENCES supply_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staging_plans (
    plan_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES household_batches(batch_id),
    resource_version_id TEXT NOT NULL REFERENCES resource_versions(version_id),
    curve_json TEXT NOT NULL,
    rollback_json TEXT NOT NULL,
    turnover_margin_percent INTEGER NOT NULL DEFAULT 0 CHECK(turnover_margin_percent BETWEEN 0 AND 100),
    content_sha256 TEXT NOT NULL,
    projection_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK(state IN ('draft','confirmed','active','completed','rolled_back','invalidated')),
    active_phase_seq INTEGER,
    invalidated_by_version TEXT,
    invalidated_at TEXT,
    created_by TEXT NOT NULL REFERENCES supply_users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES supply_users(user_id),
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_staging_plans_version
ON staging_plans(resource_version_id, state);

CREATE TABLE IF NOT EXISTS plan_phase_states (
    plan_id TEXT NOT NULL REFERENCES staging_plans(plan_id),
    phase_seq INTEGER NOT NULL CHECK(phase_seq BETWEEN 0 AND 3),
    phase TEXT NOT NULL,
    by_date TEXT NOT NULL,
    intake_households INTEGER NOT NULL,
    cumulative_demand_json TEXT NOT NULL,
    gates_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','entered','promoted','rolled_back')),
    entered_at TEXT,
    promoted_at TEXT,
    rolled_back_at TEXT,
    PRIMARY KEY(plan_id, phase_seq)
);

CREATE TABLE IF NOT EXISTS plan_phase_reservations (
    plan_id TEXT NOT NULL REFERENCES staging_plans(plan_id),
    phase_seq INTEGER NOT NULL CHECK(phase_seq BETWEEN 0 AND 3),
    resource_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('housing','school','clinic','transit')),
    reserved_qty INTEGER NOT NULL CHECK(reserved_qty >= 0),
    consumed_qty INTEGER NOT NULL DEFAULT 0 CHECK(consumed_qty >= 0),
    released_qty INTEGER NOT NULL DEFAULT 0 CHECK(released_qty >= 0),
    state TEXT NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','released')),
    PRIMARY KEY(plan_id, phase_seq, resource_id)
);

CREATE INDEX IF NOT EXISTS idx_reservations_deduct
ON plan_phase_reservations(plan_id, phase_seq, kind, state, resource_id);

CREATE TABLE IF NOT EXISTS intake_receipts (
    receipt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES staging_plans(plan_id),
    phase_seq INTEGER NOT NULL,
    household_profile_id TEXT NOT NULL,
    quantities_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(plan_id, household_profile_id)
);

CREATE TABLE IF NOT EXISTS metric_receipts (
    receipt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES staging_plans(plan_id),
    phase_seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('housing','school','clinic','transit')),
    served_delta INTEGER NOT NULL CHECK(served_delta >= 0),
    idempotency_key TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL REFERENCES supply_users(user_id),
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metric_receipts_plan
ON metric_receipts(plan_id, phase_seq, kind);

CREATE TABLE IF NOT EXISTS plan_gate_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL REFERENCES staging_plans(plan_id),
    at_phase_seq INTEGER NOT NULL,
    gate TEXT NOT NULL CHECK(gate IN ('entry','advance','complete','rollback')),
    as_of_date TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK(passed IN (0,1)),
    blockers_json TEXT NOT NULL,
    evaluated_by TEXT NOT NULL REFERENCES supply_users(user_id),
    evaluated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_evaluations_plan
ON plan_gate_evaluations(plan_id, evaluation_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
