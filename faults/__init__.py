# -*- coding: utf-8 -*-
"""ECC V3.1 faults 包（WP-04：系统化故障注入与恢复，18 类）。

公开 API：
- injector：统一 FaultInjector + 故障工具（崩溃点子进程、CheckpointStore、
  Rollbacker、ConfigSwitcher、ReadyGate、OrderedEventQueue、FileLockContender、
  ReasonixAdapterPlaceholder、tolerant_log_reader）。
- matrix：18 类故障案例矩阵（每类七元组记录：注入方法 / 预期 / 观察 /
  退出码 / 状态不变量 / 恢复动作 / 证据）+ 对照案例 + 报告渲染。

安全约定：一切注入只发生在隔离副本（tempfile.TemporaryDirectory）上，
绝不触碰正式 runtime / 用户数据 / 证据目录。
"""
from __future__ import annotations

from . import injector, matrix

__all__ = ["injector", "matrix"]
