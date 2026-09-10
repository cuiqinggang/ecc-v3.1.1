# -*- coding: utf-8 -*-
"""WP-06 任务 C：60 分钟真实耐力脚本（由 control 后台调度执行，本交付不执行）。

- 真实时钟（time.monotonic / lease 用 time.time），总时长参数 --minutes（默认 60）。
- 每轮真实工作：处理事件包（哈希小对象 + 状态推进 + 日志追加），无空等。
- 租约 acquire 后每 --heartbeat-seconds（默认 30）秒 renew（节制心跳）。
- 每 --checkpoint-every（默认 10）轮写 checkpoint；租约记录 last_accepted_checkpoint。
- 每 --audit-minutes（默认 5）分钟记录审计点（轮次/token/checkpoint 数/租约状态/时间戳）。
- 期间 --recovery-count（默认 3）次恢复演练：子进程模拟崩溃 → 从 checkpoint 恢复
  继续，恢复事件记入 JSONL 证据流。
- JSONL 证据流（--events-out）：每行 {ts, event, round, token, ...}。
- 防空等：轮间间隔 >2 秒记 slow_round 事件；连续 5 分钟无新轮次立即中止 STALLED。
- 结束打印总轮次/续期次数/checkpoint 数/恢复次数/有效工作时间占比。

用法（control 后台执行，约 60 分钟）：
    python scripts/endurance60.py --minutes 60 \
        --runtime-dir <runtime 目录> \
        --events-out <evidence jsonl> --summary-out <summary txt>

自测（短时全路径）：
    python scripts/endurance60.py --minutes 0.15 --recovery-count 2 \
        --checkpoint-every 3 --audit-minutes 0.05
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from lease import LeaseStore, LeaseOwnershipLost  # noqa: E402
from loopx import LoopXEngine  # noqa: E402
from loopx import contract as ctr  # noqa: E402

STALL_SECONDS = 300.0
SLOW_ROUND_SECONDS = 2.0

# 恢复演练子进程参数（副本上：goal 模式确定性清单）
CRASH_SIM_TOTAL = 16
CRASH_SIM_CHECKPOINT_EVERY = 4


# ---------------------------------------------------------------- 工具
def utc_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def hash_work(blob: bytes, iterations: int) -> str:
    """真实哈希工作：对 blob 反复迭代 sha256，返回最终摘要。"""
    digest = hashlib.sha256(blob).digest()
    for _ in range(iterations):
        digest = hashlib.sha256(digest + blob).digest()
    return digest.hex()


# ---------------------------------------------------------------- 合同
def make_endurance_contract(runtime_dir: str, mode: str = "event",
                            loop_id: str = "endurance60-main") -> None:
    contract = ctr.default_contract(loop_id, mode)
    contract["goal"] = "60 分钟耐力：持续处理事件包并维持租约/检查点"
    contract["success_criteria"] = "每轮真实工作、无空等、租约持续有效、checkpoint 与恢复闭环"
    contract["budget"] = {
        "max_rounds": 10_000_000,
        "max_repairs": 100,
        "max_runtime_seconds": 86400.0 * 7,
        "stale_limit": 1000,
        "same_error_limit": 100,
    }
    if mode == "event":
        contract["trigger"] = {"event_types": ["work"]}
    ctr.atomic_write_json(os.path.join(runtime_dir, ctr.CONTRACT_NAME), contract)


def make_goal_contract(runtime_dir: str, loop_id: str, total: int) -> None:
    contract = ctr.default_contract(loop_id, "goal")
    contract["budget"] = {
        "max_rounds": total + 20,
        "max_repairs": 5,
        "max_runtime_seconds": 86400.0,
        "stale_limit": 50,
        "same_error_limit": 5,
    }
    ctr.atomic_write_json(os.path.join(runtime_dir, ctr.CONTRACT_NAME), contract)


def mk_goal_pkg(index: int) -> dict:
    payload = {"i": index, "seed": f"rec-{index:04d}"}
    return {
        "object_id": f"rec-obj-{index:04d}",
        "content_hash": sha16(json.dumps(payload, sort_keys=True)),
        "idempotency_key": f"rec-key-{index:04d}",
        "payload": payload,
        "complex": False,
    }


def goal_provider(total: int):
    def provider(state):
        idx = int(state.get("cursor", 0))
        if idx < total:
            return mk_goal_pkg(idx)
        return None

    return provider


def goal_worker(errors: list):
    def worker(package, state):
        payload = package.get("payload", {})
        got = sha16(json.dumps(payload, sort_keys=True))
        if got != package["content_hash"]:
            errors.append(f"payload 哈希不符: {package['object_id']}")
            return {"status": "failed", "message": "payload hash mismatch"}
        return {"status": "ok", "message": f"verified {package['object_id']}"}

    return worker


# ---------------------------------------------------------------- checkpoint
def write_checkpoint(runtime_dir: str, cp_dir: str, round_no: int,
                     now: float) -> dict:
    """把当前 STATE 复制为 checkpoint-NN.json + manifest（SHA/轮次/时间）。"""
    os.makedirs(cp_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, ctr.STATE_NAME)
    with open(state_path, "r", encoding="utf-8") as f:
        state_text = f.read()
    state_obj = json.loads(state_text)
    cp_name = f"checkpoint-{round_no:06d}.json"
    cp_path = os.path.join(cp_dir, cp_name)
    with open(cp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(state_text)
    sha = hashlib.sha256(state_text.encode("utf-8")).hexdigest()
    manifest = {
        "schema": "ecc-v3.1-checkpoint-manifest",
        "checkpoint": cp_name,
        "round": round_no,
        "cursor": state_obj.get("cursor"),
        "sha256": sha,
        "created_at": utc_iso(now),
    }
    with open(cp_path + ".manifest.json", "w", encoding="utf-8",
              newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


def restore_checkpoint(runtime_dir: str, cp_dir: str, cp_name: str) -> None:
    """把 checkpoint-NN.json 恢复为 STATE.json（模拟崩溃后恢复）。"""
    shutil.copy2(os.path.join(cp_dir, cp_name),
                 os.path.join(runtime_dir, ctr.STATE_NAME))


# ---------------------------------------------------------------- 主耐力流
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="60 分钟真实耐力（control 调度）")
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--runtime-dir", default=None)
    parser.add_argument("--events-out", default=None)
    parser.add_argument("--summary-out", default=None)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--audit-minutes", type=float, default=5.0)
    parser.add_argument("--recovery-count", type=int, default=3)
    parser.add_argument("--lease-ttl", type=float, default=60.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=30.0)
    parser.add_argument("--work-blob-bytes", type=int, default=4096)
    parser.add_argument("--work-iterations", type=int, default=16)
    # 恢复演练子命令
    parser.add_argument("--crash-sim", action="store_true",
                        help="子进程：副本上跑 CRASH_SIM_TOTAL 轮（含 checkpoint）后模拟崩溃")
    parser.add_argument("--resume-after-crash", action="store_true",
                        help="子进程：从最新 checkpoint 恢复副本并跑完")
    args = parser.parse_args(argv)

    if args.crash_sim:
        return crash_sim(args)
    if args.resume_after_crash:
        return resume_after_crash(args)

    runtime_dir = args.runtime_dir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_endurance60_tmp")
    shutil.rmtree(runtime_dir, ignore_errors=True)
    os.makedirs(runtime_dir, exist_ok=True)
    make_endurance_contract(runtime_dir)
    events_path = args.events_out or os.path.join(runtime_dir,
                                                  "endurance60-events.jsonl")

    def emit(**fields):
        fields.setdefault("ts", utc_iso(time.time()))
        with open(events_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")

    lease_path = os.path.join(runtime_dir, "lease.json")
    store = LeaseStore(lease_path, heartbeat_interval=args.heartbeat_seconds)
    lease = store.acquire(owner_id="endurance60", ttl_seconds=args.lease_ttl)
    token = lease.fencing_token

    # ---- worker：真实工作 + 哈希验证 ----
    stats = {"work_seconds": 0.0, "errors": 0}

    def worker(package, state):
        payload = package.get("payload", {})
        expect = sha16(json.dumps(payload, sort_keys=True))
        if expect != package["content_hash"]:
            stats["errors"] += 1
            return {"status": "failed", "message": "payload hash mismatch"}
        t0 = time.perf_counter()
        blob_hex = payload.get("blob", "")
        try:
            blob = bytes.fromhex(blob_hex)
        except ValueError:
            stats["errors"] += 1
            return {"status": "failed", "message": "blob hex 非法"}
        digest = hash_work(blob, args.work_iterations)
        stats["work_seconds"] += time.perf_counter() - t0
        if digest == payload.get("digest") and digest:
            return {"status": "ok", "message": "hashed"}
        stats["errors"] += 1
        return {"status": "failed", "message": "digest mismatch"}

    engine = LoopXEngine(runtime_dir, worker=worker)
    engine.open()

    rounds = 0
    checkpoints = 0
    renewals = 0
    recoveries = 0
    slow_rounds = 0
    last_renew = time.monotonic()
    last_audit = time.monotonic()
    last_round_at = time.monotonic()
    start_wall = time.monotonic()
    deadline = start_wall + args.minutes * 60.0
    recovery_times = sorted(
        start_wall + (i + 1) * (args.minutes * 60.0) / (args.recovery_count + 1)
        for i in range(args.recovery_count)
    )
    cp_dir = os.path.join(runtime_dir, "checkpoints")
    stalled = False
    error_msg = ""

    emit(event="start", round=0, token=token,
         minutes=args.minutes, lease_id=lease.lease_id)

    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            # 防空等硬门：5 分钟无新轮次 → STALLED 中止
            if now - last_round_at >= STALL_SECONDS:
                stalled = True
                error_msg = "STALLED：5 分钟无新轮次"
                emit(event="stalled", round=rounds, token=token)
                break
            # 轮间间隔监控
            if rounds > 0 and now - last_round_at > SLOW_ROUND_SECONDS:
                slow_rounds += 1
                emit(event="slow_round", round=rounds, token=token,
                     gap_seconds=round(now - last_round_at, 3),
                     reason="上一轮完成到本轮开始间隔 > 2 秒")

            # 每轮：真实事件包 + 哈希工作 + 状态推进 + 日志追加
            blob = os.urandom(args.work_blob_bytes)
            payload = {
                "i": rounds + 1,
                "blob": blob.hex(),
                "digest": hash_work(blob, args.work_iterations),
            }
            engine.dispatch_event({"type": "work", "payload": payload})
            result = engine.step()
            rounds += 1
            last_round_at = time.monotonic()
            if result["outcome"] not in ("PROCESSED",):
                stats["errors"] += 1
                emit(event="round_error", round=rounds, token=token,
                     outcome=result["outcome"])
            emit(event="round", round=rounds, token=token,
                 cursor=result.get("cursor"),
                 work_ms=round(stats["work_seconds"] * 1000, 1))

            # 租约节制心跳：每 heartbeat_seconds 续期一次
            if time.monotonic() - last_renew >= args.heartbeat_seconds:
                try:
                    lease = store.renew("endurance60", lease.lease_id)
                    renewals += 1
                    last_renew = time.monotonic()
                    emit(event="renew", round=rounds, token=token,
                         renewals=renewals,
                         expires_at=utc_iso(lease.expires_at))
                except LeaseOwnershipLost as exc:
                    stalled = True
                    error_msg = f"租约丢失：{exc}"
                    emit(event="lease_lost", round=rounds, token=token)
                    break

            # 每 checkpoint-every 轮写 checkpoint + 租约记录
            if rounds % args.checkpoint_every == 0:
                manifest = write_checkpoint(runtime_dir, cp_dir, rounds,
                                            time.time())
                checkpoints += 1
                store.record_last_accepted_checkpoint(
                    "endurance60", lease.lease_id, token,
                    f"checkpoint-{rounds:06d}.json")
                emit(event="checkpoint", round=rounds, token=token,
                     checkpoints=checkpoints,
                     checkpoint=manifest["checkpoint"],
                     sha256=manifest["sha256"])

            # 审计点：每 audit_minutes 一次
            if time.monotonic() - last_audit >= args.audit_minutes * 60.0:
                last_audit = time.monotonic()
                current = store.current()
                emit(event="audit", round=rounds, token=token,
                     checkpoints=checkpoints, renewals=renewals,
                     recoveries=recoveries, slow_rounds=slow_rounds,
                     lease_state=current.state if current else None,
                     lease_owner=current.owner_id if current else None,
                     errors=stats["errors"])

            # 恢复演练：到达预定时间点执行（子进程崩溃 → checkpoint 恢复）
            if recovery_times and time.monotonic() >= recovery_times[0]:
                recovery_times.pop(0)
                ok, detail = run_recovery_drill(script=os.path.abspath(__file__),
                                                base_dir=runtime_dir)
                recoveries += 1
                emit(event="recovery", round=rounds, token=token,
                     recovery_no=recoveries, ok=ok, detail=detail)
                last_round_at = time.monotonic()
                if not ok:
                    stats["errors"] += 1

        if not stalled:
            engine.close("endurance_deadline_reached")
            emit(event="finish", round=rounds, token=token,
                 checkpoints=checkpoints, renewals=renewals,
                 recoveries=recoveries, slow_rounds=slow_rounds,
                 phase=engine.status()["phase"])
        store.release("endurance60", lease.lease_id)
    except Exception as exc:  # noqa: BLE001 - 全部落证据后退出
        error_msg = f"{type(exc).__name__}: {exc}"
        emit(event="fatal", round=rounds, token=token, error=error_msg)
        try:
            store.release("endurance60", lease.lease_id)
        except Exception:  # noqa: BLE001
            pass

    total_wall = time.monotonic() - start_wall
    work_ratio = (stats["work_seconds"] / total_wall
                  if total_wall > 0 else 0.0)
    summary = {
        "schema": "ecc-v3.1-endurance60-summary",
        "status": "STALLED" if stalled else ("ERROR" if error_msg else "PASS"),
        "error": error_msg or None,
        "total_rounds": rounds,
        "renewals": renewals,
        "checkpoints": checkpoints,
        "recoveries": recoveries,
        "slow_rounds": slow_rounds,
        "wall_seconds": round(total_wall, 2),
        "work_seconds": round(stats["work_seconds"], 2),
        "effective_work_ratio": round(work_ratio, 4),
        "minutes_requested": args.minutes,
        "errors": stats["errors"],
        "events_path": events_path,
        "finished_at": utc_iso(time.time()),
    }
    lines = [
        "== WP-06 任务 C：60 分钟耐力（control 调度执行）==",
        f"status: {summary['status']}",
        f"total_rounds: {rounds}",
        f"renewals: {renewals}",
        f"checkpoints: {checkpoints}",
        f"recoveries: {recoveries}",
        f"slow_rounds: {slow_rounds}",
        f"wall_seconds: {total_wall:.2f}",
        f"work_seconds: {stats['work_seconds']:.2f}",
        f"effective_work_ratio: {work_ratio:.4f}",
        f"errors: {stats['errors']}",
        f"events: {events_path}",
    ]
    if error_msg:
        lines.append(f"ERROR: {error_msg}")
    report = "\n".join(lines) + "\n"
    print(report)
    if args.summary_out:
        with open(args.summary_out, "w", encoding="utf-8", newline="\n") as f:
            f.write(report)
    return 0 if summary["status"] == "PASS" else 1


# ---------------------------------------------------------------- 恢复演练
def run_recovery_drill(script: str, base_dir: str) -> tuple:
    """子进程模拟崩溃 → 从 checkpoint 恢复继续；返回 (ok, detail)。"""
    drill_dir = os.path.join(base_dir, "recovery-drill")
    shutil.rmtree(drill_dir, ignore_errors=True)
    os.makedirs(drill_dir, exist_ok=True)
    make_goal_contract(drill_dir, "recovery-drill", CRASH_SIM_TOTAL)
    python = sys.executable
    crash = subprocess.run(
        [python, script, "--crash-sim", "--runtime-dir", drill_dir],
        capture_output=True, text=True, timeout=300)
    resume = subprocess.run(
        [python, script, "--resume-after-crash", "--runtime-dir", drill_dir],
        capture_output=True, text=True, timeout=300)
    if crash.returncode == 0 or resume.returncode != 0:
        return (False,
                f"crash_exit={crash.returncode} resume_exit={resume.returncode} "
                f"stderr={resume.stderr[-200:]}")
    state, migrations = ctr.load_state_with_migrations(
        os.path.join(drill_dir, ctr.STATE_NAME))
    if migrations:
        return (False, f"意外状态迁移 {migrations}")
    keys = state["completed_keys"]
    ok = (state["phase"] == "CLOSED"
          and len(keys) == CRASH_SIM_TOTAL
          and len(set(keys)) == len(keys))
    return (ok, f"crash_exit={crash.returncode} "
                f"resumed_rounds={state['rounds']} "
                f"completed={len(keys)} phase={state['phase']}")


def crash_sim(args) -> int:
    """子进程：跑 8 轮（含 cp-4 checkpoint）后 os._exit(1) 模拟崩溃。"""
    runtime_dir = args.runtime_dir
    cp_dir = os.path.join(runtime_dir, "checkpoints")
    engine = LoopXEngine(
        runtime_dir, work_provider=goal_provider(CRASH_SIM_TOTAL),
        worker=goal_worker([]))
    engine.open()
    for n in range(CRASH_SIM_TOTAL // 2):
        result = engine.step()
        if result["outcome"] == "BLOCKED":
            return 2
        if n + 1 == CRASH_SIM_CHECKPOINT_EVERY:
            write_checkpoint(runtime_dir, cp_dir, n + 1, time.time())
    # 模拟崩溃：不 close，直接死掉（STATE 已逐轮原子落盘）
    os._exit(1)


def resume_after_crash(args) -> int:
    """子进程：从最新 checkpoint 恢复 STATE 后继续跑完。"""
    runtime_dir = args.runtime_dir
    cp_dir = os.path.join(runtime_dir, "checkpoints")
    cps = sorted(f for f in os.listdir(cp_dir)
                 if f.startswith("checkpoint-") and f.endswith(".json"))
    if not cps:
        print("resume: 无 checkpoint 可恢复", file=sys.stderr)
        return 3
    latest = cps[-1]
    restore_checkpoint(runtime_dir, cp_dir, latest)
    engine = LoopXEngine(
        runtime_dir, work_provider=goal_provider(CRASH_SIM_TOTAL),
        worker=goal_worker([]))
    engine.open()
    while True:
        result = engine.step()
        if result["outcome"] == "CLOSED":
            break
        if result["outcome"] in ("BLOCKED", "CLEAN", "REPAIR"):
            print(f"resume: 意外 outcome {result['outcome']}", file=sys.stderr)
            return 4
    state = engine.status()
    keys = state["completed_keys"]
    if (len(keys) != CRASH_SIM_TOTAL
            or len(set(keys)) != len(keys)
            or state["phase"] != "CLOSED"):
        print(f"resume: 完成断言失败 keys={len(keys)} "
              f"phase={state['phase']}", file=sys.stderr)
        return 5
    print(f"resume: OK rounds={state['rounds']} from={latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
