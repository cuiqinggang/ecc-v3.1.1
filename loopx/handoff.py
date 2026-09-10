# -*- coding: utf-8 -*-
"""Loop -> ECC 交接合同与 ECC 四态回传校验。

Loop -> ECC 交接包（ECC_REQUIRED 时生成）必须携带：
- loop_id / run_id：身份定位；
- goal / success_criteria：目标与成功标准；
- cursor / processed_objects：当前游标与已处理对象；
- allowed_scope / forbidden_scope / rollback_scope：允许 / 禁止 / 回滚范围；
- work_packages / dependencies：委托工作包与依赖；
- failure_evidence / repair_history：已有失败证据与返修历史；
- budget / stop_conditions：预算与停止条件；
- required_return_fields：ECC 必须回传的状态字段。

ECC -> Loop 只允许回传四态：ECC_ACCEPTED / ECC_PARTIAL / ECC_BLOCKED / ECC_REJECTED，
且必须携带 required_return_fields 声明的字段（值允许为 null）。
"""
from __future__ import annotations

import hashlib

from . import states

REQUIRED_HANDOFF_FIELDS = (
    "loop_id",
    "run_id",
    "goal",
    "success_criteria",
    "cursor",
    "processed_objects",
    "allowed_scope",
    "forbidden_scope",
    "rollback_scope",
    "work_packages",
    "dependencies",
    "failure_evidence",
    "repair_history",
    "budget",
    "stop_conditions",
    "required_return_fields",
)


def make_run_id(loop_id: str, rounds: int, now: float) -> str:
    """run_id：loop_id + 轮次 + 时间指纹（确定性、可追溯）。"""
    digest = hashlib.sha1(
        f"{loop_id}:{rounds}:{now:.3f}".encode("utf-8")
    ).hexdigest()[:8]
    return f"{loop_id}-run-{rounds + 1:04d}-{digest}"


def build_handoff(contract: dict, state: dict, work_packages: list,
                  now: float) -> dict:
    """构造 Loop -> ECC 交接包。work_packages 为本次委托的工作包列表。"""
    budget = dict(contract["budget"])
    stop_conditions = list(contract.get("stop_conditions") or [])
    if not stop_conditions:
        stop_conditions = [
            {"fuse": key, "limit": budget[key]}
            for key in ("max_rounds", "max_repairs", "max_runtime_seconds",
                        "stale_limit", "same_error_limit")
        ]
    handoff = {
        "schema": "ecc-v3.1-loop-ecc-handoff",
        "loop_id": contract["loop_id"],
        "run_id": make_run_id(
            contract["loop_id"], state["rounds"], now
        ),
        "goal": contract["goal"],
        "success_criteria": contract["success_criteria"],
        "cursor": state["cursor"],
        "processed_objects": list(state["completed_objects"]),
        "allowed_scope": list(contract["allowed_scope"]),
        "forbidden_scope": list(contract["forbidden_scope"]),
        "rollback_scope": list(contract["rollback_scope"]),
        "work_packages": list(work_packages),
        "dependencies": list(contract.get("dependencies") or []),
        "failure_evidence": [
            {
                "content_hash": content_hash,
                "count": entry["count"],
                "first_round": entry["first_round"],
                "last_message": entry["last_message"],
            }
            for content_hash, entry in sorted(state["error_ledger"].items())
        ],
        "repair_history": list(state["repair_history"]),
        "budget": budget,
        "stop_conditions": stop_conditions,
        "required_return_fields": list(
            contract.get("required_return_fields")
            or ["result", "new_cursor", "evidence_path", "next_action"]
        ),
        "created_at": now,
    }
    return handoff


def validate_handoff(handoff: dict) -> list:
    """校验交接包字段完整性，返回缺失/非法字段描述列表（空列表 = 完整）。"""
    problems: list = []
    if not isinstance(handoff, dict):
        return ["handoff 必须是 JSON 对象"]
    for field in REQUIRED_HANDOFF_FIELDS:
        if field not in handoff:
            problems.append(f"缺失字段: {field}")
    if "cursor" in handoff and (
        isinstance(handoff["cursor"], bool)
        or not isinstance(handoff["cursor"], (int, float))
    ):
        problems.append("cursor 必须是数值")
    for field in ("processed_objects", "work_packages", "failure_evidence",
                  "repair_history"):
        if field in handoff and not isinstance(handoff[field], list):
            problems.append(f"{field} 必须是列表")
    for field in ("allowed_scope", "forbidden_scope", "rollback_scope",
                  "dependencies", "stop_conditions", "required_return_fields"):
        if field in handoff and not isinstance(handoff[field], list):
            problems.append(f"{field} 必须是列表")
    if "budget" in handoff and not isinstance(handoff["budget"], dict):
        problems.append("budget 必须是对象")
    return problems


def validate_ecc_result(payload: dict, required_fields: tuple = None) -> None:
    """校验 ECC 回传：四态合法 + 必需回传字段在场（值允许为 null）。"""
    if not isinstance(payload, dict):
        raise ValueError("ECC 回传必须是 JSON 对象")
    result = payload.get("result")
    states.assert_valid_ecc_result(result)
    required = tuple(required_fields) if required_fields else (
        "result", "new_cursor", "evidence_path", "next_action",
    )
    missing = [f for f in required if f not in payload]
    if missing:
        raise ValueError(f"ECC 回传缺必需字段: {missing}")
    new_cursor = payload.get("new_cursor")
    if new_cursor is not None and (
        isinstance(new_cursor, bool) or not isinstance(new_cursor, int)
    ):
        raise ValueError(f"new_cursor 必须是整数或 null: {new_cursor!r}")
