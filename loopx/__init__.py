# -*- coding: utf-8 -*-
"""LoopX 长期循环（WP-01）：四模式循环引擎 + 三文件合同 + ECC 交接。

公开 API：
- LoopXEngine：循环引擎（goal/scheduled/event/hybrid、熔断、幂等、ECC 交接）。
- states：六态状态机与转换表。
- contract：三文件读写、schema 迁移、默认预算。
- handoff：Loop -> ECC 交接包与四态回传校验。
"""
from __future__ import annotations

from . import contract, handoff, states
from .engine import (
    EccResultAlreadyRecorded,
    EccResultError,
    EccResultOutOfBand,
    EccRunIdMismatch,
    FakeClock,
    InvalidEvent,
    LoopBlockedError,
    LoopClosedError,
    LoopNotRunnable,
    LoopXEngine,
    LoopXError,
    SystemClock,
)

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
    "FakeClock",
    "SystemClock",
    "states",
    "contract",
    "handoff",
]
