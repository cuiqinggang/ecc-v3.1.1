# -*- coding: utf-8 -*-
"""WP-06 任务 B：fresh 冷恢复测试。

场景 1（进程崩溃恢复）：进程 A（全新 subprocess）在 runtime-A 跑 50 轮后
sys.exit(99) 强制退出；进程 B（另一个全新 subprocess）在全新工作副本
runtime-B 上从 STATE.json 恢复，继续跑完剩余工作。
断言：completed 不丢、不重复（A 的 50 个幂等键全保留，最终恰 100 个无重复）。

场景 2（fresh 目录恢复）：只给 LOOP-CONTRACT.json / STATE.json / RUN-LOG.jsonl
三个文件 + 恢复命令（本脚本 --worker --resume），从零目录继续跑完。
工作清单由恢复命令内置的确定性清单提供，不依赖额外文件。

用法：
    python scripts/fresh_resume.py --base-dir <DIR> --out <fresh-resume.txt>
    python scripts/fresh_resume.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from loopx import LoopXEngine  # noqa: E402
from loopx import contract as ctr  # noqa: E402

TOTAL_PACKAGES = 100
CRASH_AFTER_ROUNDS = 50


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def mk_pkg(index: int) -> dict:
    payload = {"i": index, "seed": f"fresh-{index:04d}"}
    return {
        "object_id": f"fobj-{index:04d}",
        "content_hash": sha16(json.dumps(payload, sort_keys=True)),
        "idempotency_key": f"fkey-{index:04d}",
        "payload": payload,
        "complex": False,
    }


def seq_provider(total: int):
    """按 STATE.cursor 取确定性清单中的包：恢复后从断点继续，天然不重复。"""

    def provider(state):
        idx = int(state.get("cursor", 0))
        if idx < total:
            return mk_pkg(idx)
        return None

    return provider


def verifier_worker(errors: list):
    def worker(package, state):
        payload = package.get("payload", {})
        got = sha16(json.dumps(payload, sort_keys=True))
        if got != package["content_hash"]:
            errors.append(f"payload 哈希不符: {package['object_id']}")
            return {"status": "failed", "message": "payload hash mismatch"}
        return {"status": "ok", "message": f"verified {package['object_id']}"}

    return worker


def make_contract(runtime_dir: str) -> None:
    contract = ctr.default_contract("fresh-resume-loop", "goal")
    contract["budget"] = {
        "max_rounds": TOTAL_PACKAGES + 50,
        "max_repairs": 5,
        "max_runtime_seconds": 86400.0,
        "stale_limit": 50,
        "same_error_limit": 5,
    }
    ctr.atomic_write_json(os.path.join(runtime_dir, ctr.CONTRACT_NAME), contract)


def run_worker(runtime_dir: str, resume: bool, crash_after: int = 0) -> int:
    """子进程入口：加载引擎并跑轮次；crash_after>0 时跑满即 sys.exit(99)。"""
    errors: list = []
    engine = LoopXEngine(
        runtime_dir, work_provider=seq_provider(TOTAL_PACKAGES),
        worker=verifier_worker(errors))
    engine.open()
    rounds = 0
    while True:
        result = engine.step()
        if result["outcome"] == "BLOCKED":
            print(f"WORKER: 熔断 {result}", file=sys.stderr)
            return 3
        if result["outcome"] == "CLOSED":
            break
        if result["outcome"] == "PROCESSED":
            rounds += 1
        elif result["outcome"] == "SKIPPED":
            rounds += 1
            print(f"WORKER: 恢复后幂等跳过 {result['object_id']}", file=sys.stderr)
        elif result["outcome"] == "CLEAN":
            print(f"WORKER: 意外 CLEAN {result}", file=sys.stderr)
            return 4
        else:
            print(f"WORKER: 意外 outcome {result}", file=sys.stderr)
            return 5
        if crash_after and rounds >= crash_after:
            # 模拟崩溃：不 close、不清理，直接退出（STATE 已逐轮原子落盘）
            sys.stderr.write(f"WORKER: 模拟崩溃（已跑 {rounds} 轮）\n")
            sys.stderr.flush()
            sys.exit(99)
    if errors:
        for e in errors:
            print(f"WORKER ERROR: {e}", file=sys.stderr)
        return 6
    state = engine.status()
    print(f"WORKER: 完成 rounds={state['rounds']} cursor={state['cursor']} "
          f"phase={state['phase']}")
    return 0


def assert_final(runtime_dir: str, label: str, errors: list) -> dict:
    state, migrations = ctr.load_state_with_migrations(
        os.path.join(runtime_dir, ctr.STATE_NAME))
    if migrations:
        errors.append(f"{label}: 意外状态迁移 {migrations}")
    rounds = state["rounds"]
    completed = state["completed_objects"]
    keys = state["completed_keys"]
    if len(completed) != TOTAL_PACKAGES:
        errors.append(f"{label}: completed_objects {len(completed)} "
                      f"!= {TOTAL_PACKAGES}")
    if len(set(completed)) != len(completed):
        errors.append(f"{label}: completed_objects 存在重复")
    if len(keys) != TOTAL_PACKAGES:
        errors.append(f"{label}: completed_keys {len(keys)} != {TOTAL_PACKAGES}")
    if len(set(keys)) != len(keys):
        errors.append(f"{label}: completed_keys 存在重复（side effect 重复执行）")
    if state["phase"] != "CLOSED":
        errors.append(f"{label}: phase={state['phase']} != CLOSED")
    # RUN-LOG：round_start 轮次必须 1..TOTAL 连续且无重复
    seen: list = []
    for entry in ctr.iter_run_log(os.path.join(runtime_dir, ctr.LOG_NAME)):
        if entry.get("event") == "round_start":
            seen.append(int(entry["detail"]["round"]))
    if seen != list(range(1, TOTAL_PACKAGES + 1)):
        dupes = sorted({n for n in seen if seen.count(n) > 1})
        missing = sorted(set(range(1, TOTAL_PACKAGES + 1)) - set(seen))
        errors.append(
            f"{label}: RUN-LOG round_start 不连续（重复={dupes[:5]} "
            f"缺失={missing[:5]}）")
    return {"rounds": rounds, "cursor": state["cursor"],
            "completed": len(completed), "keys": len(keys),
            "phase": state["phase"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="fresh 冷恢复测试")
    parser.add_argument("--base-dir", default=None, help="测试基目录")
    parser.add_argument("--out", default=None, help="结果输出文件")
    parser.add_argument("--worker", action="store_true",
                        help="子进程模式：运行 worker")
    parser.add_argument("--runtime-dir", default=None,
                        help="worker 运行目录（配合 --worker）")
    parser.add_argument("--resume", action="store_true",
                        help="worker 恢复模式（不崩溃，跑完）")
    parser.add_argument("--crash-after", type=int, default=0,
                        help="worker 跑 N 轮后模拟崩溃退出")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.worker:
        if not args.runtime_dir:
            print("--worker 需要 --runtime-dir", file=sys.stderr)
            return 2
        return run_worker(args.runtime_dir, args.resume, args.crash_after)

    if args.selftest:
        return selftest()

    base = args.base_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fresh_resume_tmp")
    shutil.rmtree(base, ignore_errors=True)
    runtime_a = os.path.join(base, "runtime-A")
    runtime_b = os.path.join(base, "runtime-B")
    runtime_fresh = os.path.join(base, "runtime-fresh")
    os.makedirs(runtime_a, exist_ok=True)
    make_contract(runtime_a)

    script = os.path.abspath(__file__)
    python = sys.executable
    errors: list = []
    lines: list = []

    # ---- 场景 1a：进程 A 跑 50 轮后崩溃 ----
    proc_a = subprocess.run(
        [python, script, "--worker", "--runtime-dir", runtime_a,
         "--crash-after", str(CRASH_AFTER_ROUNDS)],
        capture_output=True, text=True, timeout=300)
    state_a = ctr.load_state_with_migrations(
        os.path.join(runtime_a, ctr.STATE_NAME))[0]
    lines.append(f"[A] 进程 A 退出码: {proc_a.returncode}（预期 99）")
    lines.append(f"[A] 崩溃时 rounds={state_a['rounds']} "
                 f"cursor={state_a['cursor']} phase={state_a['phase']}")
    if proc_a.returncode != 99:
        errors.append(f"进程 A 退出码 {proc_a.returncode} != 99")
    if state_a["rounds"] != CRASH_AFTER_ROUNDS:
        errors.append(f"A 轮次 {state_a['rounds']} != {CRASH_AFTER_ROUNDS}")

    # ---- 场景 1b：全新工作副本 B 恢复继续跑完 ----
    shutil.copytree(runtime_a, runtime_b)
    proc_b = subprocess.run(
        [python, script, "--worker", "--resume", "--runtime-dir", runtime_b],
        capture_output=True, text=True, timeout=300)
    if proc_b.returncode != 0:
        errors.append(f"进程 B 退出码 {proc_b.returncode}（stderr: "
                      f"{proc_b.stderr[-500:]}）")
    res_b = assert_final(runtime_b, "B（副本恢复）", errors)
    lines.append(f"[B] 副本恢复: rounds={res_b['rounds']} "
                 f"completed={res_b['completed']} keys={res_b['keys']} "
                 f"phase={res_b['phase']}（completed 不丢且不重复）")

    # ---- 场景 2：fresh 目录恢复（只给三文件 + 恢复命令） ----
    os.makedirs(runtime_fresh, exist_ok=True)
    for name in (ctr.CONTRACT_NAME, ctr.STATE_NAME, ctr.LOG_NAME):
        shutil.copy2(os.path.join(runtime_a, name),
                     os.path.join(runtime_fresh, name))
    only = sorted(os.listdir(runtime_fresh))
    lines.append(f"[F] fresh 目录初始只含: {only}")
    proc_f = subprocess.run(
        [python, script, "--worker", "--resume",
         "--runtime-dir", runtime_fresh],
        capture_output=True, text=True, timeout=300)
    if proc_f.returncode != 0:
        errors.append(f"fresh 进程退出码 {proc_f.returncode}（stderr: "
                      f"{proc_f.stderr[-500:]}）")
    res_f = assert_final(runtime_fresh, "F（fresh 目录恢复）", errors)
    lines.append(f"[F] fresh 恢复: rounds={res_f['rounds']} "
                 f"completed={res_f['completed']} keys={res_f['keys']} "
                 f"phase={res_f['phase']}（从零目录恢复完成）")

    if res_b["keys"] != res_f["keys"] or res_b["completed"] != res_f["completed"]:
        errors.append("B 与 fresh 恢复结果不一致（不丢不重复断言失败）")

    lines.append("RESULT: " + ("PASS" if not errors else "FAIL"))
    for e in errors:
        lines.append(f"ERROR: {e}")
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
    return 0 if not errors else 1


def selftest() -> int:
    """缩微自测：20 包、10 轮崩溃 + 副本恢复 + fresh 恢复。"""
    base = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_fresh_resume_selftest")
    shutil.rmtree(base, ignore_errors=True)
    runtime_a = os.path.join(base, "runtime-A")
    runtime_b = os.path.join(base, "runtime-B")
    runtime_f = os.path.join(base, "runtime-fresh")
    os.makedirs(runtime_a, exist_ok=True)
    make_contract(runtime_a)
    script = os.path.abspath(__file__)
    errors: list = []
    pa = subprocess.run(
        [sys.executable, script, "--worker", "--runtime-dir", runtime_a,
         "--crash-after", "10"],
        capture_output=True, text=True, timeout=120)
    shutil.copytree(runtime_a, runtime_b)
    pb = subprocess.run(
        [sys.executable, script, "--worker", "--resume",
         "--runtime-dir", runtime_b],
        capture_output=True, text=True, timeout=120)
    os.makedirs(runtime_f, exist_ok=True)
    for name in (ctr.CONTRACT_NAME, ctr.STATE_NAME, ctr.LOG_NAME):
        shutil.copy2(os.path.join(runtime_a, name),
                     os.path.join(runtime_f, name))
    pf = subprocess.run(
        [sys.executable, script, "--worker", "--resume",
         "--runtime-dir", runtime_f],
        capture_output=True, text=True, timeout=120)
    if pa.returncode != 99:
        errors.append(f"A 退出码 {pa.returncode}")
    state_b = assert_final(runtime_b, "selftest-B", errors)
    state_f = assert_final(runtime_f, "selftest-F", errors)
    if state_b["keys"] != TOTAL_PACKAGES or state_f["keys"] != TOTAL_PACKAGES:
        errors.append("selftest 键数不符")
    ok = not errors and pb.returncode == 0 and pf.returncode == 0
    print("SELFTEST:", "PASS" if ok else "FAIL")
    for e in errors:
        print("ERROR:", e)
    shutil.rmtree(base, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
