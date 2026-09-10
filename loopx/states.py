# -*- coding: utf-8 -*-
"""LoopX 循环状态机：CLOSED / WAITING / REPAIR / ECC_REQUIRED / HUMAN_REQUIRED / BLOCKED。

- CLOSED：循环关闭（终态；完成或手动关闭）。
- WAITING：等待下一轮工作。
- REPAIR：上一轮失败，进入返修。
- ECC_REQUIRED：已向 ECC 交接复杂任务，等待四态回传。
- HUMAN_REQUIRED：等待人工介入。
- BLOCKED：预算熔断或 ECC_BLOCKED。

规则：状态转换必须命中合法转换表，否则抛 IllegalStateTransition。
ECC 回传四态只允许 ECC_ACCEPTED / ECC_PARTIAL / ECC_BLOCKED / ECC_REJECTED。
"""
from __future__ import annotations

LOOP_STATES = (
    "CLOSED", "WAITING", "REPAIR", "ECC_REQUIRED", "HUMAN_REQUIRED", "BLOCKED",
)
ECC_RESULTS = ("ECC_ACCEPTED", "ECC_PARTIAL", "ECC_BLOCKED", "ECC_REJECTED")

LEGAL_TRANSITIONS = {
    "CLOSED": {"WAITING"},
    "WAITING": {"REPAIR", "ECC_REQUIRED", "HUMAN_REQUIRED", "BLOCKED", "CLOSED"},
    "REPAIR": {"WAITING", "ECC_REQUIRED", "HUMAN_REQUIRED", "BLOCKED", "CLOSED"},
    "ECC_REQUIRED": {"WAITING", "REPAIR", "HUMAN_REQUIRED", "BLOCKED", "CLOSED"},
    "HUMAN_REQUIRED": {"WAITING", "BLOCKED", "CLOSED"},
    "BLOCKED": {"WAITING", "CLOSED"},
}


class IllegalStateTransition(ValueError):
    """非法状态转换。"""

    def __init__(self, current: str, target: str):
        self.current = current
        self.target = target
        super().__init__(
            f"IllegalStateTransition: '{current}' -> '{target}' 不在合法转换表中"
        )


class InvalidLoopState(ValueError):
    """非法循环状态。"""


def assert_valid_loop_state(state: str) -> str:
    if state not in LOOP_STATES:
        raise InvalidLoopState(f"InvalidLoopState: {state!r} 不在 {LOOP_STATES}")
    return state


def assert_valid_ecc_result(state: str) -> str:
    if state not in ECC_RESULTS:
        raise ValueError(f"InvalidEccResult: {state!r} 不在 {ECC_RESULTS}")
    return state


def transition(current: str, target: str) -> str:
    """执行一次状态转换；非法转换抛 IllegalStateTransition。"""
    assert_valid_loop_state(current)
    assert_valid_loop_state(target)
    allowed = LEGAL_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise IllegalStateTransition(current, target)
    return target
