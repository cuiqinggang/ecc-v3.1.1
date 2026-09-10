# -*- coding: utf-8 -*-
"""LoopX 长期循环引擎（WP-01）。

四种循环模式：
- goal：目标驱动。每轮从 work_provider 取一个可验证工作包处理，直到目标达成。
- scheduled：定时。到 next_run_at 才执行一轮，之后按 interval_seconds 顺延。
- event：事件驱动。dispatch_event 入队，每轮消费一个事件作为工作包。
- hybrid：混合。事件触发 goal 执行（事件是唤醒信号，工作包仍来自 work_provider）。

职责分离三文件：LOOP-CONTRACT.json（合同）/ STATE.json（原子更新权威状态）/
RUN-LOG.jsonl（只追加日志）。

关键规则：
- 确定性预处理：无工作返回 CLEAN，不唤醒智能体、不产生轮次。
- 每轮只处理一个可验证目标或工作包。
- 完整状态机 CLOSED/WAITING/REPAIR/ECC_REQUIRED/HUMAN_REQUIRED/BLOCKED。
- 复杂工作包交接 ECC：ECC_REQUIRED + 交接包；ECC 只回传四态（record_ecc_result）。
- 五种预算熔断：max_rounds / max_repairs / max_runtime_seconds / stale_limit /
  same_error_limit（按内容哈希聚错）。
- contract_version + schema 迁移 + 向后兼容（旧合同可读取，缺字段用默认值并记录
  迁移事件）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

from . import contract as ctr
from . import handoff
from . import states

__all__ = [
    "LoopXEngine",
    "LoopXError",
    "LoopClosedError",
    "LoopBlockedError",
    "LoopNotRunnable",
    "EccResultError",
    "EccRunIdMismatch",
    "EccResultAlreadyRecorded",
    "EccResultOutOfBand",
    "InvalidEvent",
    "SystemClock",
    "FakeClock",
]


class LoopXError(ValueError):
    """LoopX 域错误基类。"""


class LoopClosedError(LoopXError):
    """循环已 CLOSED，不能再 step。"""


class LoopBlockedError(LoopXError):
    """循环已 BLOCKED（熔断），须 unblock 后继续。"""


class LoopNotRunnable(LoopXError):
    """ECC_REQUIRED / HUMAN_REQUIRED 状态等待外部动作，不能 step。"""


class EccResultError(LoopXError):
    """ECC 回传错误基类。"""


class EccRunIdMismatch(EccResultError):
    """回传 run_id 与待回传交接不一致。"""


class EccResultAlreadyRecorded(EccResultError):
    """同一 run_id 已回传过（重复回传拒绝）。"""


class EccResultOutOfBand(EccResultError):
    """非 ECC_REQUIRED 状态下回传。"""


class InvalidEvent(LoopXError):
    """非法事件（模式不接受事件 / 事件类型不在白名单）。"""


class FakeClock:
    """可注入时钟。"""

    def __init__(self, start: float = 1000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += float(seconds)

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)

    def set(self, t: float) -> None:
        self._t = float(t)


class SystemClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _default_worker(package: dict, state: dict) -> dict:
    """默认智能体替代品：只回显成功（测试注入真实 worker）。"""
    return {"status": "ok", "output_hash": package["content_hash"], "message": "echo"}


class LoopXEngine:
    """长期循环引擎。

    worker / work_provider / success_check 均可注入，保证确定性与可测试性：
    - work_provider(state) -> 工作包 dict | None；None 表示当前无工作。
    - worker(package, state) -> {"status": "ok"|"failed", "message", ...}。
    - success_check(state, contract) -> bool。
    工作包字段：object_id / content_hash / idempotency_key / payload / complex。
    """

    def __init__(self, runtime_dir: str, *, clock=None, worker=None,
                 work_provider=None, success_check=None):
        self.runtime_dir = os.path.abspath(runtime_dir)
        self._clock = clock if clock is not None else SystemClock()
        self._worker = worker if worker is not None else _default_worker
        self._provider = work_provider
        self._success_check = success_check  # None 表示用默认 provider 语义

        self._contract_path = os.path.join(self.runtime_dir, ctr.CONTRACT_NAME)
        self._state_path = os.path.join(self.runtime_dir, ctr.STATE_NAME)
        self._log_path = os.path.join(self.runtime_dir, ctr.LOG_NAME)

        if not os.path.exists(self._contract_path):
            raise FileNotFoundError(
                f"缺少 LOOP-CONTRACT.json：{self._contract_path}"
            )
        self._contract, contract_migrations = ctr.load_contract_with_migrations(
            self._contract_path
        )
        if os.path.exists(self._state_path):
            self._state, state_migrations = ctr.load_state_with_migrations(
                self._state_path
            )
        else:
            self._state = ctr.fresh_state(self._contract["loop_id"])
            state_migrations = []

        if contract_migrations:
            # 迁移后的合同写回（向后兼容：旧版本合同升级后仍只维护一份权威文件）
            ctr.atomic_write_json(self._contract_path, self._contract)
            self._log("INFO", "contract_migration", {"events": contract_migrations})
        if state_migrations:
            self._save()
            self._log("INFO", "state_migration", {"events": state_migrations})

    # ------------------------------------------------------------ 基础设施
    def _is_goal_met(self) -> bool:
        """目标达成判定：显式 success_check 优先；默认语义 = work_provider 无
        更多工作（返回 None）即达成。"""
        if self._success_check is not None:
            return self._success_check(self._state, self._contract)
        return True

    def _log(self, level: str, event: str, detail) -> None:
        ctr.append_run_log(
            self._log_path, level, event, detail, ts=_iso(self._clock.now())
        )

    def _save(self) -> None:
        self._state["updated_at"] = self._clock.now()
        ctr.atomic_write_json(self._state_path, self._state)

    def _budget(self) -> dict:
        return self._contract["budget"]

    # ------------------------------------------------------------ 公开操作
    def open(self) -> dict:
        """CLOSED -> WAITING：开启循环。幂等：已开启则原样返回。"""
        s = self._state
        if s["phase"] != "CLOSED":
            return {"outcome": "ALREADY_OPEN", "phase": s["phase"]}
        s["phase"] = states.transition("CLOSED", "WAITING")
        s["started_at"] = self._clock.now()
        s["cursor"] = int(self._contract.get("initial_cursor", 0))
        if self._contract["mode"] == "scheduled":
            trigger = self._contract.get("trigger", {})
            s["next_run_at"] = (
                self._clock.now() + float(trigger.get("initial_delay_seconds", 0))
            )
        self._log("INFO", "loop_open", {
            "loop_id": self._contract["loop_id"],
            "mode": self._contract["mode"],
            "goal": self._contract["goal"],
        })
        self._save()
        return {"outcome": "OPENED", "phase": "WAITING"}

    def step(self) -> dict:
        """执行一轮。返回 outcome：
        CLEAN / PROCESSED / SKIPPED / REPAIR / ECC_REQUIRED / BLOCKED / CLOSED。
        CLEAN 不产生轮次、不唤醒智能体。
        """
        c, s = self._contract, self._state
        phase = s["phase"]
        if phase == "CLOSED":
            raise LoopClosedError(s.get("close_reason"))
        if phase == "BLOCKED":
            raise LoopBlockedError(s.get("block_reason"))
        if phase == "ECC_REQUIRED":
            raise LoopNotRunnable("ECC_REQUIRED：等待 ECC 回传 record_ecc_result")
        if phase == "HUMAN_REQUIRED":
            raise LoopNotRunnable("HUMAN_REQUIRED：等待 resolve_human 人工介入")

        now = self._clock.now()
        budget = self._budget()

        # 1. 前置熔断检查（不产生轮次）
        trip = self._pre_trip_check(now)
        if trip:
            return self._trip(trip[0], trip[1])

        mode = c["mode"]

        # 2. 触发判定
        event = None
        if mode == "scheduled":
            if now < s["next_run_at"]:
                return self._clean("未到定时触发点")
            s["next_run_at"] = now + float(c["trigger"]["interval_seconds"])
        elif mode in ("event", "hybrid"):
            if not s["pending_events"]:
                return self._clean("无待处理事件")
            event = s["pending_events"].pop(0)

        # 3. 确定性预处理：取一个工作包
        if mode == "event":
            package = self._event_to_package(event)
        else:
            package = self._provider(s) if self._provider is not None else None

        if package is None:
            if mode in ("event", "hybrid"):
                self._log("INFO", "step_clean", "事件已触发一次检查，无工作")
                self._save()
                return {"outcome": "CLEAN", "round": None, "reason": "事件触发但无工作"}
            if self._is_goal_met():
                return self.close("goal_met")
            return self._clean("无工作且目标未达成")

        # 4. 产生一个轮次
        s["rounds"] += 1
        round_no = s["rounds"]
        key = package["idempotency_key"]
        s["idempotency_key"] = key
        s["object_id"] = package["object_id"]
        s["content_hash"] = package["content_hash"]
        self._log("INFO", "round_start", {
            "round": round_no,
            "object_id": package["object_id"],
            "mode": mode,
        })

        # 5. 幂等：已执行过的键跳过（游标仍前进，视为进展）
        if key in s["completed_keys"]:
            s["cursor"] += 1
            s["last_progress_round"] = round_no
            s["no_progress_streak"] = 0
            self._log("INFO", "work_skipped", {
                "round": round_no, "idempotency_key": key,
            })
            self._save()
            return {
                "outcome": "SKIPPED", "round": round_no,
                "object_id": package["object_id"], "cursor": s["cursor"],
            }

        # 6. 复杂工作包 -> 交接 ECC
        if package.get("complex"):
            return self._request_ecc(package, round_no)

        # 7. 执行智能体（一个工作包 / 一个可验证目标）
        result = self._worker(package, s)
        if result.get("status") == "ok":
            return self._on_success(package, round_no, result)
        return self._on_failure(package, round_no, result)

    # ------------------------------------------------------------ 结果处理
    def _on_success(self, package: dict, round_no: int, result: dict) -> dict:
        c, s = self._contract, self._state
        s["cursor"] += 1
        s["completed_objects"].append(package["object_id"])
        s["completed_keys"].append(package["idempotency_key"])
        s["last_progress_round"] = round_no
        s["no_progress_streak"] = 0
        if s["phase"] == "REPAIR":
            s["phase"] = states.transition("REPAIR", "WAITING")
        self._log("INFO", "work_processed", {
            "round": round_no,
            "object_id": package["object_id"],
            "cursor": s["cursor"],
            "message": result.get("message"),
        })
        if (c["mode"] in ("goal", "scheduled", "hybrid")
                and self._success_check is not None and self._is_goal_met()):
            self._save()
            return self.close("goal_met")
        self._save()
        return {
            "outcome": "PROCESSED", "round": round_no,
            "object_id": package["object_id"], "cursor": s["cursor"],
        }

    def _on_failure(self, package: dict, round_no: int, result: dict) -> dict:
        c, s = self._contract, self._state
        budget = self._budget()
        content_hash = package["content_hash"]
        ledger = s["error_ledger"].setdefault(content_hash, {
            "count": 0, "first_round": round_no, "last_message": None,
        })
        ledger["count"] += 1
        ledger["last_message"] = result.get("message")
        s["repairs"] += 1
        s["repair_history"].append({
            "round": round_no,
            "object_id": package["object_id"],
            "content_hash": content_hash,
            "message": result.get("message"),
            "strategy_change": result.get("strategy_change"),
        })
        s["no_progress_streak"] += 1
        if s["phase"] != "REPAIR":
            s["phase"] = states.transition(s["phase"], "REPAIR")
        self._log("ERROR", "work_failed", {
            "round": round_no,
            "object_id": package["object_id"],
            "content_hash": content_hash,
            "message": result.get("message"),
        })
        # 即时熔断
        if s["repairs"] > budget["max_repairs"]:
            return self._trip("max_repairs", {
                "limit": budget["max_repairs"], "repairs": s["repairs"],
            })
        if ledger["count"] > budget["same_error_limit"]:
            return self._trip("same_error_limit", {
                "content_hash": content_hash,
                "count": ledger["count"],
                "limit": budget["same_error_limit"],
            })
        if s["no_progress_streak"] >= budget["stale_limit"]:
            return self._trip("stale_limit", {
                "streak": s["no_progress_streak"],
                "limit": budget["stale_limit"],
            })
        self._save()
        return {
            "outcome": "REPAIR", "round": round_no, "phase": "REPAIR",
            "repairs": s["repairs"],
        }

    # ------------------------------------------------------------ 熔断
    def _pre_trip_check(self, now: float):
        c, s = self._contract, self._state
        budget = self._budget()
        if s["rounds"] >= budget["max_rounds"]:
            return ("max_rounds", {
                "limit": budget["max_rounds"], "rounds": s["rounds"],
            })
        if (s["started_at"] is not None
                and (now - s["started_at"]) > budget["max_runtime_seconds"]):
            return ("max_runtime_seconds", {
                "limit": budget["max_runtime_seconds"],
                "elapsed": now - s["started_at"],
            })
        if s["no_progress_streak"] >= budget["stale_limit"]:
            return ("stale_limit", {
                "limit": budget["stale_limit"],
                "streak": s["no_progress_streak"],
            })
        return None

    def _trip(self, fuse: str, detail: dict) -> dict:
        s = self._state
        s["block_reason"] = {
            "fuse": fuse, "detail": detail, "at": self._clock.now(),
        }
        if s["phase"] != "BLOCKED":
            s["phase"] = states.transition(s["phase"], "BLOCKED")
        self._log("ERROR", "budget_tripped", {"fuse": fuse, **detail})
        self._save()
        return {"outcome": "BLOCKED", "fuse": fuse, "detail": detail}

    def _clean(self, reason: str) -> dict:
        self._log("INFO", "step_clean", {"reason": reason})
        self._save()
        return {"outcome": "CLEAN", "round": None, "reason": reason}

    # ------------------------------------------------------------ ECC 交接
    def _request_ecc(self, package: dict, round_no: int) -> dict:
        c, s = self._contract, self._state
        h = handoff.build_handoff(c, s, [package], now=self._clock.now())
        s["ecc"] = {
            "run_id": h["run_id"],
            "handoff": h,
            "result": None,
            "requested_at": self._clock.now(),
            "requested_round": round_no,
            "result_received_at": None,
        }
        s["phase"] = states.transition(s["phase"], "ECC_REQUIRED")
        self._log("WARN", "ecc_handoff", {
            "round": round_no, "run_id": h["run_id"],
            "work_packages": [p["object_id"] for p in h["work_packages"]],
        })
        self._save()
        return {
            "outcome": "ECC_REQUIRED", "round": round_no,
            "run_id": h["run_id"], "handoff": h,
        }

    def record_ecc_result(self, payload: dict) -> dict:
        """ECC -> Loop 四态回传。校验：状态必须是 ECC_REQUIRED、run_id 匹配、
        四态合法、必需回传字段在场；同一 run_id 只接受一次。"""
        c, s = self._contract, self._state
        if s["ecc"] is not None and s["ecc"].get("result") is not None:
            raise EccResultAlreadyRecorded(
                f"run_id={s['ecc']['run_id']} 已回传过（结果 {s['ecc']['result']}）"
            )
        if s["phase"] != "ECC_REQUIRED":
            raise EccResultOutOfBand(
                f"当前 phase={s['phase']}，无待回传的 ECC 交接"
            )
        handoff.validate_ecc_result(
            payload,
            required_fields=tuple(c.get("required_return_fields") or ()),
        )
        if payload.get("run_id") != s["ecc"]["run_id"]:
            raise EccRunIdMismatch(
                f"回传 run_id={payload.get('run_id')!r} 与待回传 "
                f"run_id={s['ecc']['run_id']!r} 不一致"
            )
        result = payload["result"]
        s["ecc"]["result"] = result
        s["ecc"]["result_received_at"] = self._clock.now()

        if result == "ECC_ACCEPTED":
            new_cursor = payload.get("new_cursor")
            if new_cursor is not None:
                if new_cursor < s["cursor"]:
                    raise EccResultError(
                        f"ECC 回传光标回退：new_cursor={new_cursor} < cursor={s['cursor']}"
                    )
                s["cursor"] = new_cursor
            for wp in s["ecc"]["handoff"]["work_packages"]:
                if wp["idempotency_key"] not in s["completed_keys"]:
                    s["completed_keys"].append(wp["idempotency_key"])
                if wp["object_id"] not in s["completed_objects"]:
                    s["completed_objects"].append(wp["object_id"])
            s["last_progress_round"] = s["rounds"]
            s["no_progress_streak"] = 0
            s["phase"] = states.transition("ECC_REQUIRED", "WAITING")
        elif result == "ECC_PARTIAL":
            s["repairs"] += 1
            s["repair_history"].append({
                "round": s["rounds"],
                "source": "ecc_partial",
                "message": payload.get("evidence_path"),
            })
            s["phase"] = states.transition("ECC_REQUIRED", "REPAIR")
        elif result == "ECC_BLOCKED":
            s["block_reason"] = {
                "fuse": "ecc_blocked",
                "detail": payload.get("evidence_path"),
                "at": self._clock.now(),
            }
            s["phase"] = states.transition("ECC_REQUIRED", "BLOCKED")
        else:  # ECC_REJECTED
            s["human_reason"] = payload.get("next_action") or "ECC 拒绝交接"
            s["phase"] = states.transition("ECC_REQUIRED", "HUMAN_REQUIRED")

        self._log("INFO", "ecc_result", {
            "run_id": s["ecc"]["run_id"], "result": result,
            "phase": s["phase"],
        })
        self._save()
        return {"outcome": result, "phase": s["phase"], "run_id": s["ecc"]["run_id"]}

    # ------------------------------------------------------------ 事件
    def dispatch_event(self, event: dict) -> dict:
        c, s = self._contract, self._state
        if s["phase"] == "CLOSED":
            raise LoopClosedError(s.get("close_reason"))
        if c["mode"] not in ("event", "hybrid"):
            raise InvalidEvent(f"mode={c['mode']!r} 不接受事件")
        event_type = event.get("type")
        allowed = c.get("trigger", {}).get("event_types")
        if not event_type or (allowed and event_type not in allowed):
            raise InvalidEvent(
                f"非法事件类型 {event_type!r}（允许 {allowed}）"
            )
        s["pending_events"].append(event)
        self._log("INFO", "event_dispatched", {"type": event_type})
        self._save()
        return {
            "outcome": "EVENT_QUEUED", "type": event_type,
            "pending_events": len(s["pending_events"]),
        }

    def _event_to_package(self, event: dict) -> dict:
        payload_text = json.dumps(
            event.get("payload", {}), sort_keys=True, ensure_ascii=False
        )
        content_hash = hashlib.sha256(
            payload_text.encode("utf-8")
        ).hexdigest()[:16]
        return {
            "object_id": f"event:{event['type']}",
            "content_hash": content_hash,
            "idempotency_key": f"event:{event['type']}:{content_hash}",
            "payload": event.get("payload", {}),
            "complex": bool(event.get("complex", False)),
        }

    # ------------------------------------------------------------ 收尾
    def close(self, reason: str = "closed_by_user") -> dict:
        s = self._state
        if s["phase"] == "CLOSED":
            return {"outcome": "CLOSED", "note": "already_closed"}
        s["phase"] = states.transition(s["phase"], "CLOSED")
        s["close_reason"] = reason
        s["closed_at"] = self._clock.now()
        self._log("INFO", "loop_closed", {
            "reason": reason, "rounds": s["rounds"], "cursor": s["cursor"],
        })
        self._save()
        return {
            "outcome": "CLOSED", "reason": reason,
            "rounds": s["rounds"], "cursor": s["cursor"],
        }

    def resolve_human(self, note: str = "") -> dict:
        """人工介入完成：HUMAN_REQUIRED -> WAITING。"""
        s = self._state
        if s["phase"] != "HUMAN_REQUIRED":
            raise LoopXError(f"当前 phase={s['phase']}，无待处理的人工介入")
        s["phase"] = states.transition("HUMAN_REQUIRED", "WAITING")
        s["human_reason"] = None
        self._log("INFO", "human_resolved", {"note": note})
        self._save()
        return {"outcome": "RESOLVED", "phase": "WAITING"}

    def unblock(self, note: str = "") -> dict:
        """人工解除熔断：BLOCKED -> WAITING（重置无进展计数）。"""
        s = self._state
        if s["phase"] != "BLOCKED":
            raise LoopXError(f"当前 phase={s['phase']}，无熔断可解除")
        s["phase"] = states.transition("BLOCKED", "WAITING")
        s["no_progress_streak"] = 0
        s["block_reason"] = None
        self._log("WARN", "unblocked", {"note": note})
        self._save()
        return {"outcome": "UNBLOCKED", "phase": "WAITING"}

    def status(self) -> dict:
        return dict(self._state)
