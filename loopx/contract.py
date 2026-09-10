# -*- coding: utf-8 -*-
"""LoopX 三文件合同：LOOP-CONTRACT.json / STATE.json / RUN-LOG.jsonl + schema 迁移。

- LOOP-CONTRACT.json：循环合同（loop_id、mode、goal、success_criteria、budget、
  contract_version、schema_migrations 迁移表）。只读权威输入，迁移后写回。
- STATE.json：原子更新的权威状态（游标 cursor、object_id、content_hash、
  idempotency_key、轮次、熔断计数、ECC 交接）。Loop 与 ECC 只维护这一份状态。
- RUN-LOG.jsonl：只追加运行日志，每行一个 JSON 对象，写入后 fsync。
- 状态原子更新 = 写临时文件 + fsync + 读回校验 + os.replace。
- 向后兼容：旧 contract_version 合同可读取，缺失字段用默认值补齐并记录迁移事件。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any

CONTRACT_NAME = "LOOP-CONTRACT.json"
STATE_NAME = "STATE.json"
LOG_NAME = "RUN-LOG.jsonl"

CONTRACT_VERSION = 2
STATE_VERSION = 1

MODES = ("goal", "scheduled", "event", "hybrid")

DEFAULT_BUDGET = {
    "max_rounds": 1000,
    "max_repairs": 3,
    "max_runtime_seconds": 86400.0,
    "stale_limit": 10,
    "same_error_limit": 5,
}

# ECC 必须回传的状态字段（Loop 侧校验）
REQUIRED_RETURN_FIELDS = ("result", "new_cursor", "evidence_path", "next_action")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_write_json(path: str, payload: dict) -> None:
    """候选文件 -> fsync -> 读回校验 -> 原子替换（UTF-8, LF）。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".atomic-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, "r", encoding="utf-8") as f:
            json.load(f)  # 读回校验
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_run_log(path: str, level: str, event: str, detail: Any,
                   ts: str | None = None) -> None:
    """追加一行 RUN-LOG.jsonl；每次写入后 fsync，保证崩溃后日志不丢行。"""
    entry = {
        "ts": ts if ts is not None else _utc_now(),
        "level": level,
        "event": event,
        "detail": detail,
    }
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def iter_run_log(path: str):
    """逐行读取 RUN-LOG，容忍最后一行不完整（崩溃残留）。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue  # 跳过崩溃残留的半行


# ---------------------------------------------------------------- 合同

def default_contract(loop_id: str, mode: str = "goal", **overrides) -> dict:
    """构造一份当前版本（contract_version=CONTRACT_VERSION）的完整合同。"""
    contract = {
        "schema": "ecc-v3.1-loop-contract",
        "contract_version": CONTRACT_VERSION,
        "loop_id": loop_id,
        "mode": mode,
        "goal": "处理循环目标",
        "success_criteria": "所有工作包处理完成",
        "budget": dict(DEFAULT_BUDGET),
        "trigger": {},
        "initial_cursor": 0,
        "allowed_scope": [],
        "forbidden_scope": [],
        "rollback_scope": [],
        "stop_conditions": [],
        "dependencies": [],
        "required_return_fields": list(REQUIRED_RETURN_FIELDS),
        "schema_migrations": [],
    }
    contract.update(overrides)
    return contract


def _check_num(name: str, value: Any, minimum: float = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"合同字段 {name} 必须是数值: {value!r}")
    if value < minimum:
        raise ValueError(f"合同字段 {name} 必须 >= {minimum}: {value!r}")


def validate_contract(contract: dict) -> None:
    """合同合法性校验：非法模式 / 缺 loop_id / 非法预算直接拒绝。"""
    if not isinstance(contract, dict):
        raise ValueError("合同必须是 JSON 对象")
    mode = contract.get("mode")
    if mode not in MODES:
        raise ValueError(f"InvalidMode: {mode!r} 不在 {MODES}")
    loop_id = contract.get("loop_id")
    if not isinstance(loop_id, str) or not loop_id:
        raise ValueError("合同缺 loop_id 或 loop_id 非空字符串")
    if not isinstance(contract.get("contract_version"), int) or contract.get("contract_version") < 1:
        raise ValueError(f"非法 contract_version: {contract.get('contract_version')!r}")
    budget = contract.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("budget 必须是对象")
    for key, minimum in (
        ("max_rounds", 1), ("max_repairs", 0), ("max_runtime_seconds", 0),
        ("stale_limit", 1), ("same_error_limit", 1),
    ):
        _check_num(f"budget.{key}", budget.get(key), minimum)
    if mode == "scheduled":
        interval = contract.get("trigger", {}).get("interval_seconds")
        _check_num("trigger.interval_seconds", interval, minimum=0.001)
    if mode in ("event", "hybrid"):
        types = contract.get("trigger", {}).get("event_types")
        if types is not None and (not isinstance(types, list) or not types):
            raise ValueError("trigger.event_types 必须是非空列表（或省略表示不限）")


def _migrate_contract_v1_to_v2(c: dict) -> list:
    """v1 -> v2：补齐预算、范围与回传字段（用默认值）。"""
    changes: list = []
    budget = c.setdefault("budget", {})
    for key, value in DEFAULT_BUDGET.items():
        if key not in budget:
            budget[key] = value
            changes.append(f"add budget.{key}={value}")
    for key in ("rollback_scope", "stop_conditions", "dependencies", "trigger"):
        if key not in c:
            c[key] = []
            changes.append(f"add {key}=[]")
    if "initial_cursor" not in c:
        c["initial_cursor"] = 0
        changes.append("add initial_cursor=0")
    if "required_return_fields" not in c:
        c["required_return_fields"] = list(REQUIRED_RETURN_FIELDS)
        changes.append(f"add required_return_fields={list(REQUIRED_RETURN_FIELDS)}")
    if "allowed_scope" not in c:
        c["allowed_scope"] = []
        changes.append("add allowed_scope=[]")
    if "forbidden_scope" not in c:
        c["forbidden_scope"] = []
        changes.append("add forbidden_scope=[]")
    if "goal" not in c or not c["goal"]:
        c["goal"] = "循环目标（历史合同未声明）"
        changes.append("add goal=默认")
    if "success_criteria" not in c or not c["success_criteria"]:
        c["success_criteria"] = "所有工作包处理完成"
        changes.append("add success_criteria=默认")
    return changes


_CONTRACT_MIGRATORS = {
    1: _migrate_contract_v1_to_v2,
}


def load_contract_with_migrations(path: str) -> tuple:
    """读取合同并执行 schema 迁移，返回 (contract, migration_events)。

    旧版本合同缺失字段用默认值补齐；每个迁移事件记入 schema_migrations 表。
    """
    raw = load_json(path)
    if "contract_version" not in raw:
        raw["contract_version"] = 1
    if raw["contract_version"] > CONTRACT_VERSION:
        raise ValueError(
            f"contract_version={raw['contract_version']} 高于当前支持的版本 "
            f"{CONTRACT_VERSION}，拒绝读取"
        )
    events: list = []
    while raw["contract_version"] < CONTRACT_VERSION:
        migrator = _CONTRACT_MIGRATORS.get(raw["contract_version"])
        if migrator is None:
            raise ValueError(
                f"不支持的 contract_version {raw['contract_version']}（无迁移路径）"
            )
        changes = migrator(raw)
        event = {
            "from_version": raw["contract_version"],
            "to_version": raw["contract_version"] + 1,
            "applied_at": _utc_now(),
            "changes": changes,
        }
        raw["contract_version"] += 1
        raw.setdefault("schema_migrations", []).append(event)
        events.append(event)
    validate_contract(raw)
    return raw, events


# ---------------------------------------------------------------- 状态

def fresh_state(loop_id: str) -> dict:
    """未运行循环的初始 STATE（phase=CLOSED）。"""
    return {
        "schema": "ecc-v3.1-loop-state",
        "state_version": STATE_VERSION,
        "loop_id": loop_id,
        "phase": "CLOSED",
        "close_reason": None,
        "block_reason": None,
        "human_reason": None,
        "cursor": 0,
        "object_id": None,
        "content_hash": None,
        "idempotency_key": None,
        "rounds": 0,
        "repairs": 0,
        "no_progress_streak": 0,
        "last_progress_round": None,
        "completed_objects": [],
        "completed_keys": [],
        "error_ledger": {},
        "repair_history": [],
        "pending_events": [],
        "next_run_at": None,
        "started_at": None,
        "closed_at": None,
        "updated_at": None,
        "ecc": None,
        "schema_migrations": [],
    }


def _migrate_state_to_v1(s: dict) -> list:
    """无版本状态 -> v1：补齐全部字段（向后兼容旧 STATE）。"""
    changes: list = []
    for key, value in fresh_state(s.get("loop_id", "unknown")).items():
        if key not in s:
            s[key] = value
            changes.append(f"add {key}={value!r}")
    return changes


_STATE_MIGRATORS = {
    0: _migrate_state_to_v1,
}


def load_state_with_migrations(path: str) -> tuple:
    """读取 STATE 并执行 schema 迁移，返回 (state, migration_events)。"""
    raw = load_json(path)
    if "state_version" not in raw:
        raw["state_version"] = 0
    events: list = []
    while raw["state_version"] < STATE_VERSION:
        migrator = _STATE_MIGRATORS.get(raw["state_version"])
        if migrator is None:
            raise ValueError(
                f"不支持的 state_version {raw['state_version']}（无迁移路径）"
            )
        changes = migrator(raw)
        event = {
            "from_version": raw["state_version"],
            "to_version": raw["state_version"] + 1,
            "applied_at": _utc_now(),
            "changes": changes,
        }
        raw["state_version"] += 1
        raw.setdefault("schema_migrations", []).append(event)
        events.append(event)
    if raw.get("phase") not in (
        "CLOSED", "WAITING", "REPAIR", "ECC_REQUIRED", "HUMAN_REQUIRED", "BLOCKED",
    ):
        raise ValueError(f"非法 STATE phase: {raw.get('phase')!r}")
    return raw, events
