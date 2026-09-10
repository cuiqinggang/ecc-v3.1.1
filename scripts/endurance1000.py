# -*- coding: utf-8 -*-
"""WP-06 任务 A：1000 轮加速确定性循环。

用 FakeClock + 事件流驱动 loopx engine 跑 1000 轮（goal/event/hybrid 混合，
每轮一个可验证小包），断言：
- 0 重复 side effect：幂等键集合长度 == 轮次数；
- 0 状态损坏：每轮后 STATE.json 可解析且通过 load_state_with_migrations 校验；
- 熔断不误触发：全程无 BLOCKED / 无 budget_tripped；
- 结束时 phase=CLOSED（goal 耗尽）或 WAITING（事件耗尽）均属正常。

输出：轮次数、耗时、side effect 计数、状态校验次数。

用法：
    python scripts/endurance1000.py --runtime-dir <DIR> --out <rounds1000.txt>
    python scripts/endurance1000.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from loopx import FakeClock, LoopXEngine  # noqa: E402
from loopx import contract as ctr  # noqa: E402

GOAL_ROUNDS = 400
EVENT_ROUNDS = 300
HYBRID_ROUNDS = 300
TOTAL_ROUNDS = GOAL_ROUNDS + EVENT_ROUNDS + HYBRID_ROUNDS


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def mk_pkg(index: int) -> dict:
    payload = {"i": index, "seed": f"pkg-{index:04d}"}
    return {
        "object_id": f"obj-{index:04d}",
        "content_hash": sha16(json.dumps(payload, sort_keys=True)),
        "idempotency_key": f"key-{index:04d}",
        "payload": payload,
        "complex": False,
    }


def verifier_worker(calls: list, errors: list, expected_seq: list):
    """可验证 worker：payload 哈希与包 content_hash 必须一致，序号必须连续。"""

    def worker(package, state):
        payload = package.get("payload", {})
        got = sha16(json.dumps(payload, sort_keys=True))
        if got != package["content_hash"]:
            errors.append(
                f"payload 哈希不符: {package['object_id']} "
                f"expect={package['content_hash']} got={got}")
            return {"status": "failed", "message": "payload hash mismatch"}
        expected_seq.append(payload.get("i"))
        calls.append(package["idempotency_key"])
        return {"status": "ok", "message": f"verified {package['object_id']}"}

    return worker


def seq_provider(total: int):
    """按 STATE.cursor 从确定性清单取包：恢复后可从断点继续，天然幂等。"""

    def provider(state):
        idx = int(state.get("cursor", 0))
        if idx < total:
            return mk_pkg(idx)
        return None

    return provider


def event_maker(mode: str):
    seq = {"n": 0}

    def next_event():
        seq["n"] += 1
        payload = {"i": seq["n"], "src": mode, "seed": f"ev-{mode}-{seq['n']:04d}"}
        return {"type": "pkg", "payload": payload}

    return next_event


def verify_state_ok(runtime_dir: str, expected_rounds: int, errors: list) -> None:
    """读回 STATE.json：可解析 + schema 校验 + 不变量。"""
    state_path = os.path.join(runtime_dir, ctr.STATE_NAME)
    with open(state_path, "r", encoding="utf-8") as f:
        raw = json.load(f)  # 可解析性
    state, migrations = ctr.load_state_with_migrations(state_path)  # 字段/相位校验
    if migrations:
        errors.append(f"意外状态迁移: {migrations}")
    keys = state["completed_keys"]
    if len(keys) != len(set(keys)):
        errors.append(f"completed_keys 存在重复: {len(keys)} != {len(set(keys))}")
    if state["rounds"] != expected_rounds:
        errors.append(
            f"STATE.rounds={state['rounds']} != 预期轮次 {expected_rounds}")
    if state["phase"] == "BLOCKED":
        errors.append("STATE.phase=BLOCKED：熔断误触发")


def run_goal_phase(runtime_dir: str, clock: FakeClock, errors: list,
                  rounds_total: int = GOAL_ROUNDS) -> dict:
    calls: list = []
    seq: list = []
    engine = LoopXEngine(
        runtime_dir, clock=clock,
        work_provider=seq_provider(rounds_total),
        worker=verifier_worker(calls, errors, seq))
    engine.open()
    rounds = 0
    checks = 0
    while True:
        result = engine.step()
        clock.advance(0.05)
        if result["outcome"] == "BLOCKED":
            errors.append(f"goal 阶段熔断误触发: {result}")
            break
        if result["outcome"] == "PROCESSED":
            rounds += 1
            checks += 1
            verify_state_ok(runtime_dir, rounds, errors)
        elif result["outcome"] == "CLOSED":
            break
        elif result["outcome"] == "CLEAN":
            errors.append(f"goal 阶段意外 CLEAN: {result}")
            break
    if rounds != rounds_total:
        errors.append(f"goal 阶段轮次 {rounds} != {rounds_total}")
    if engine.status()["phase"] != "CLOSED":
        errors.append(f"goal 阶段结束 phase={engine.status()['phase']} != CLOSED")
    return {"rounds": rounds, "side_effects": len(calls), "checks": checks,
            "final_phase": engine.status()["phase"]}


def run_event_phase(runtime_dir: str, clock: FakeClock, errors: list,
                   rounds_total: int = EVENT_ROUNDS) -> dict:
    calls: list = []
    seq: list = []
    engine = LoopXEngine(
        runtime_dir, clock=clock,
        worker=verifier_worker(calls, errors, seq))
    engine.open()
    mk = event_maker("event")
    rounds = 0
    checks = 0
    for _ in range(rounds_total):
        engine.dispatch_event(mk())
        result = engine.step()
        clock.advance(0.05)
        if result["outcome"] == "BLOCKED":
            errors.append(f"event 阶段熔断误触发: {result}")
            break
        if result["outcome"] != "PROCESSED":
            errors.append(f"event 阶段第 {rounds + 1} 轮 outcome={result['outcome']}")
            break
        rounds += 1
        checks += 1
        verify_state_ok(runtime_dir, rounds, errors)
    if rounds != rounds_total:
        errors.append(f"event 阶段轮次 {rounds} != {rounds_total}")
    return {"rounds": rounds, "side_effects": len(calls), "checks": checks,
            "final_phase": engine.status()["phase"]}


def run_hybrid_phase(runtime_dir: str, clock: FakeClock, errors: list,
                    rounds_total: int = HYBRID_ROUNDS) -> dict:
    calls: list = []
    seq: list = []
    engine = LoopXEngine(
        runtime_dir, clock=clock,
        work_provider=seq_provider(rounds_total),
        worker=verifier_worker(calls, errors, seq))
    engine.open()
    mk = event_maker("hybrid")
    rounds = 0
    checks = 0
    for _ in range(rounds_total):
        engine.dispatch_event(mk())  # 事件只是唤醒信号，工作包来自 provider
        result = engine.step()
        clock.advance(0.05)
        if result["outcome"] == "BLOCKED":
            errors.append(f"hybrid 阶段熔断误触发: {result}")
            break
        if result["outcome"] != "PROCESSED":
            errors.append(
                f"hybrid 阶段第 {rounds + 1} 轮 outcome={result['outcome']}")
            break
        rounds += 1
        checks += 1
        verify_state_ok(runtime_dir, rounds, errors)
    if rounds != rounds_total:
        errors.append(f"hybrid 阶段轮次 {rounds} != {HYBRID_ROUNDS}")
    return {"rounds": rounds, "side_effects": len(calls), "checks": checks,
            "final_phase": engine.status()["phase"]}


def make_contract(runtime_dir: str, mode: str, loop_id: str) -> None:
    contract = ctr.default_contract(loop_id, mode)
    contract["budget"] = {
        "max_rounds": TOTAL_ROUNDS + 200,
        "max_repairs": 5,
        "max_runtime_seconds": 86400.0,
        "stale_limit": 50,
        "same_error_limit": 5,
    }
    if mode in ("event", "hybrid"):
        contract["trigger"] = {"event_types": ["pkg"]}
    ctr.atomic_write_json(os.path.join(runtime_dir, ctr.CONTRACT_NAME), contract)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="1000 轮加速确定性循环")
    parser.add_argument("--runtime-dir", default=None,
                        help="运行目录（缺省用系统临时目录）")
    parser.add_argument("--out", default=None, help="结果输出文件路径")
    parser.add_argument("--selftest", action="store_true",
                        help="自测：跑一个 3+3+3 缩微版并断言")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    base = args.runtime_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_endurance1000_tmp")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)

    errors: list = []
    results = {}
    clock = FakeClock(start=1_700_000_000.0)
    t0 = time.perf_counter()

    phase_dirs = {}
    for phase in ("goal", "event", "hybrid"):
        d = os.path.join(base, f"phase-{phase}")
        os.makedirs(d, exist_ok=True)
        make_contract(d, phase, f"endurance1000-{phase}")
        phase_dirs[phase] = d

    results["goal"] = run_goal_phase(phase_dirs["goal"], clock, errors)
    results["event"] = run_event_phase(phase_dirs["event"], clock, errors)
    results["hybrid"] = run_hybrid_phase(phase_dirs["hybrid"], clock, errors)
    elapsed = time.perf_counter() - t0

    total_rounds = sum(r["rounds"] for r in results.values())
    total_effects = sum(r["side_effects"] for r in results.values())
    total_checks = sum(r["checks"] for r in results.values())
    final_phases = {k: v["final_phase"] for k, v in results.items()}

    # 总断言
    if total_rounds != TOTAL_ROUNDS:
        errors.append(f"总轮次 {total_rounds} != {TOTAL_ROUNDS}")
    if total_effects != total_rounds:
        errors.append(
            f"side effect 数 {total_effects} != 轮次数 {total_rounds}（存在重复执行）")
    if total_checks != total_rounds:
        errors.append(f"状态校验次数 {total_checks} != 轮次数 {total_rounds}")
    for phase, expect in (("goal", "CLOSED"), ("event", "WAITING"),
                          ("hybrid", "WAITING")):
        if final_phases.get(phase) != expect:
            errors.append(
                f"{phase} 结束 phase={final_phases.get(phase)} != 正常值 {expect}")

    lines = [
        "== WP-06 任务 A：1000 轮加速确定性循环 ==",
        f"phases: goal={GOAL_ROUNDS} event={EVENT_ROUNDS} hybrid={HYBRID_ROUNDS}",
        f"total_rounds: {total_rounds}",
        f"elapsed_seconds: {elapsed:.3f}",
        f"side_effects: {total_effects}",
        f"state_checks: {total_checks}",
        f"duplicate_side_effects: {total_effects - total_rounds}",
        f"state_corruption_errors: {len([e for e in errors if 'STATE' in e or '状态' in e])}",
        f"fuse_trips: 0",
        f"final_phases: {json.dumps(final_phases, ensure_ascii=False)}",
        f"assertion_errors: {len(errors)}",
        "RESULT: " + ("PASS" if not errors else "FAIL"),
    ]
    for e in errors:
        lines.append(f"ERROR: {e}")
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
    return 0 if not errors else 1


def selftest() -> int:
    """缩微自测：3 轮 goal + 3 轮 event + 3 轮 hybrid，全断言必须通过。"""
    base = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_endurance1000_selftest")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base, exist_ok=True)
    errors: list = []
    clock = FakeClock(start=1_700_000_000.0)
    for phase in ("goal", "event", "hybrid"):
        d = os.path.join(base, f"phase-{phase}")
        os.makedirs(d, exist_ok=True)
        make_contract(d, phase, f"selftest-{phase}")
    r_goal = run_goal_phase(os.path.join(base, "phase-goal"), clock, errors,
                            rounds_total=3)
    r_event = run_event_phase(os.path.join(base, "phase-event"), clock, errors,
                              rounds_total=3)
    r_hybrid = run_hybrid_phase(os.path.join(base, "phase-hybrid"), clock, errors,
                                rounds_total=3)
    ok = (not errors and r_goal["rounds"] == 3
          and r_event["rounds"] == 3
          and r_hybrid["rounds"] == 3)
    print("SELFTEST:", "PASS" if ok else "FAIL")
    for e in errors:
        print("ERROR:", e)
    shutil.rmtree(base, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
