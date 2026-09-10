#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ECC V3.1 Reasonix 影子适配器 · 最小任务入口（ecc31_smoke）。

职责：
- 基于 __file__ 相对路径定位 v3.1 根目录（零硬编码绝对路径）。
- 子进程运行 `python -m unittest discover -s <tests> -q`（超时默认 300 秒）。
- 解析 unittest 输出（tests run / failures+errors / skipped）。
- 输出单行四态 JSON 到 stdout：{status, tests_run, failures, skipped, duration}。

四态与退出码（详见 contract.json）：
  ECC_ACCEPTED = 0 : tests_run > 0 且 failures == 0（全绿）
  ECC_PARTIAL  = 1 : 0 < failures < tests_run（部分失败）
  ECC_BLOCKED  = 2 : tests_run > 0 且 failures >= tests_run（全部失败）
  ECC_REJECTED = 3 : 运行器无法完成（测试目录缺失 / 解释器不可用 / 超时 /
                     解析不到 "Ran N tests" 结果行）

错误契约与 loopx 侧 ReasonixAdapterPlaceholder 对齐：启动类失败在 stderr 报
AdapterStartupError 诊断（携带退出码或原因），ECC 侧 fail closed。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

DEFAULT_TIMEOUT_SECONDS = 300
EXIT_CODES = {
    "ECC_ACCEPTED": 0,
    "ECC_PARTIAL": 1,
    "ECC_BLOCKED": 2,
    "ECC_REJECTED": 3,
}

_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?\s+in\s+[\d.]+s")
_SKIPPED_RE = re.compile(r"\bskipped=(\d+)")
_FAILURES_RE = re.compile(r"\bfailures=(\d+)")
_ERRORS_RE = re.compile(r"\berrors=(\d+)")


class _StartupFailure(Exception):
    """运行器启动/执行失败（映射 AdapterStartupError 语义，fail closed）。"""


def _force_utf8_streams() -> None:
    """尽量把 stdout/stderr 切到 UTF-8，避免旧控制台代码页损坏输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _emit(status: str, tests_run: int = 0, failures: int = 0,
          skipped: int = 0, duration: float = 0.0) -> None:
    payload = {
        "status": status,
        "tests_run": int(tests_run),
        "failures": int(failures),
        "skipped": int(skipped),
        "duration": round(float(duration), 3),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def locate_v31_root() -> str:
    """v3.1 根目录 = 本文件向上三级（scripts -> reasonix -> adapter -> v3.1）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(os.path.dirname(here)))


def classify(tests_run: int, failures: int) -> str:
    """按四态定义归类：全绿 ACCEPTED / 全挂 BLOCKED / 部分挂 PARTIAL /
    无有效运行（含 tests_run==0）REJECTED。"""
    if tests_run <= 0:
        return "ECC_REJECTED"
    if failures == 0:
        return "ECC_ACCEPTED"
    if failures >= tests_run:
        return "ECC_BLOCKED"
    return "ECC_PARTIAL"


def _int_or_zero(match) -> int:
    return int(match.group(1)) if match else 0


def run_suite(test_root: str, timeout_seconds: float, cwd: str) -> dict:
    """在子进程运行 unittest discover 并解析结果；启动/执行失败抛 _StartupFailure。"""
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", test_root, "-q"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=float(timeout_seconds), cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        raise _StartupFailure(
            f"测试套件超时（>{float(timeout_seconds):.0f} 秒，exit 语义 TimeoutExpired）"
        ) from exc
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    matched = _RAN_RE.search(output)
    if not matched:
        snippet = "\n".join(
            line for line in output.strip().splitlines() if line.strip()
        )[:400]
        raise _StartupFailure(
            f"运行器无有效结果（exit={proc.returncode}）"
            f"{'：' + snippet if snippet else ''}"
        )
    tests_run = int(matched.group(1))
    skipped = _int_or_zero(_SKIPPED_RE.search(output))
    failures = (_int_or_zero(_FAILURES_RE.search(output))
                + _int_or_zero(_ERRORS_RE.search(output)))
    return {
        "status": classify(tests_run, failures),
        "tests_run": tests_run,
        "failures": failures,
        "skipped": skipped,
    }


def main(argv=None) -> int:
    _force_utf8_streams()
    parser = argparse.ArgumentParser(
        prog="ecc31_smoke",
        description="ECC V3.1 最小任务入口：跑全套测试并输出 ECC 四态 JSON。")
    parser.add_argument(
        "--test-root", default=None,
        help="测试目录（默认 <v3.1>/tests；用于隔离副本验证）")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"测试套件超时秒数（默认 {DEFAULT_TIMEOUT_SECONDS}）")
    args = parser.parse_args(argv)

    root = locate_v31_root()
    test_root = os.path.abspath(args.test_root) if args.test_root \
        else os.path.join(root, "tests")
    if not os.path.isdir(test_root):
        sys.stderr.write(
            f"[ECC31] AdapterStartupError: 测试目录不存在（fail closed）：{test_root}\n")
        _emit("ECC_REJECTED")
        return EXIT_CODES["ECC_REJECTED"]

    start = time.time()
    try:
        result = run_suite(test_root, args.timeout, cwd=root)
    except _StartupFailure as exc:
        sys.stderr.write(f"[ECC31] AdapterStartupError: {exc}（fail closed）\n")
        _emit("ECC_REJECTED", duration=time.time() - start)
        return EXIT_CODES["ECC_REJECTED"]
    except Exception as exc:  # 解释器不可用等意外 → fail closed
        sys.stderr.write(
            f"[ECC31] AdapterStartupError: 运行器异常 {type(exc).__name__}: "
            f"{exc}（fail closed）\n")
        _emit("ECC_REJECTED", duration=time.time() - start)
        return EXIT_CODES["ECC_REJECTED"]

    _emit(result["status"], tests_run=result["tests_run"],
          failures=result["failures"], skipped=result["skipped"],
          duration=time.time() - start)
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
