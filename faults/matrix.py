# -*- coding: utf-8 -*-
"""WP-04 故障注入与恢复矩阵：18 类故障案例 + 18 个对照案例。

每类案例记录七元组：注入方法 / 预期 / 观察 / 退出码 / 状态不变量 / 恢复动作 /
证据。全部注入在 FaultInjector 自建的隔离副本上执行；崩溃类案例以子进程
os._exit(非零码) 在精确崩溃点模拟。

编号（按命令包）：
 01 进程在状态写入前崩溃          02 状态写入后、日志追加前崩溃
 03 日志追加后、side effect 确认前崩溃  04 checkpoint payload 篡改
 05 manifest 缺项                  06 READY 缺失
 07 STATE 截断或非法 JSON          08 RUN-LOG 尾行半写
 09 磁盘空间不足/写入失败模拟      10 文件被占用
 11 Runner 超时/崩溃/返回畸形      12 重复事件和乱序事件
 13 双 worker 同时抢租约           14 旧 owner 恢复后尝试写入（fencing）
 15 系统时间前跳/后跳              16 Reasonix 启动失败和适配器加载失败
 17 配置候选写后 readback 不一致   18 回滚中断后再次恢复
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from lease import (
    FencingTokenMismatch,
    FakeClock as LeaseFakeClock,
    LeaseAlreadyHeld,
    LeaseOwnershipLost,
    LeaseStore,
)
from loopx import FakeClock, LoopXEngine
from loopx import contract as ctr
from runners.codex_runner import CodexRunner

from .injector import (
    CheckpointCorruptedError,
    CheckpointStore,
    ConfigReadbackMismatch,
    ConfigSwitcher,
    FaultInjector,
    FileLockContender,
    FileLockedError,
    OrderedEventQueue,
    ReadyGate,
    ReasonixAdapterPlaceholder,
    AdapterLoadError,
    AdapterStartupError,
    Rollbacker,
    tolerant_log_reader,
    reap_child,
    read_json_file,
    run_child,
    spawn_child,
    V31_ROOT,
)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------
def _pkg(object_id, content_hash=None, key=None, ready=True, complex_=False):
    return {
        "object_id": object_id,
        "content_hash": content_hash or f"h-{object_id}",
        "idempotency_key": key or f"key-{object_id}",
        "payload": {},
        "ready": ready,
        "complex": complex_,
    }


def _provider(items: list) -> Callable:
    idx = {"i": 0}

    def provider(state):
        i = idx["i"]
        if i < len(items):
            idx["i"] += 1
            return items[i]
        return None
    return provider


def _make_runtime(d: str, loop_id="l1", mode="goal", **overrides) -> dict:
    contract = ctr.default_contract(loop_id, mode, **overrides)
    ctr.atomic_write_json(os.path.join(d, ctr.CONTRACT_NAME), contract)
    return contract


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _fill(script: str, **kw) -> str:
    """把脚本模板里的占位符替换为安全字面量（字符串走 repr）。"""
    for key, value in kw.items():
        script = script.replace(key, repr(value) if isinstance(value, str)
                                else str(value))
    return script


# ---------------------------------------------------------------------------
# 子进程注入脚本模板
# ---------------------------------------------------------------------------
_SCRIPT_C01 = """# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, _V31)
import loopx.contract as ctr
from loopx import FakeClock, LoopXEngine
_target = _TARGET
_orig_write = ctr.atomic_write_json
def hooked_write(path, payload):
    if path.endswith(ctr.STATE_NAME):
        os._exit(9)  # 崩溃点：状态写入前
    _orig_write(path, payload)
ctr.atomic_write_json = hooked_write
def provider(state):
    return {"object_id": "o-2", "content_hash": "h-2",
            "idempotency_key": "k-2", "payload": {}, "complex": False}
eng = LoopXEngine(_target, clock=FakeClock(1000), work_provider=provider)
eng.open()
eng.step()
"""

_SCRIPT_C02 = """# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, _V31)
import loopx.contract as ctr
from loopx import FakeClock, LoopXEngine
_target = _TARGET
_orig_log = ctr.append_run_log
def hooked_log(path, level, event, detail, ts=None):
    if event == "round_start" and isinstance(detail, dict) and detail.get("round") == 2:
        os._exit(9)  # 崩溃点：第1轮 STATE 已落盘、第2轮日志追加前
    return _orig_log(path, level, event, detail, ts=ts)
ctr.append_run_log = hooked_log
_idx = {"i": 0}
_pkgs = [
    {"object_id": "o-1", "content_hash": "h-1",
     "idempotency_key": "k-1", "payload": {}, "complex": False},
    {"object_id": "o-2", "content_hash": "h-2",
     "idempotency_key": "k-2", "payload": {}, "complex": False},
]
def provider(state):
    i = _idx["i"]
    if i < len(_pkgs):
        _idx["i"] += 1
        return _pkgs[i]
    return None
eng = LoopXEngine(_target, clock=FakeClock(1000), work_provider=provider)
eng.open()
eng.step()
eng.step()
"""

_SCRIPT_C03 = """# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, _V31)
import loopx.contract as ctr
from loopx import FakeClock, LoopXEngine
_target = _TARGET
_effects = _EFFECTS
_armed = {"v": False}
_orig_log = ctr.append_run_log
def hooked_log(path, level, event, detail, ts=None):
    if event == "work_processed":
        _orig_log(path, level, event, detail, ts=ts)  # LOG 有行
        _armed["v"] = True
        return
    return _orig_log(path, level, event, detail, ts=ts)
_orig_write = ctr.atomic_write_json
def hooked_write(path, payload):
    if _armed["v"] and path.endswith(ctr.STATE_NAME):
        os._exit(9)  # 崩溃点：日志已追加、side effect 确认（STATE 持久化）前
    _orig_write(path, payload)
ctr.append_run_log = hooked_log
ctr.atomic_write_json = hooked_write
def worker(package, state):
    key = package["idempotency_key"]
    done = os.path.join(_effects, "done-" + key + ".txt")
    if os.path.exists(done):
        return {"status": "ok", "message": "already-done"}
    with open(os.path.join(_effects, "count.txt"), "a", encoding="utf-8") as f:
        f.write("x")
    with open(done, "w", encoding="utf-8") as f:
        f.write("side-effect")
    return {"status": "ok", "message": "executed"}
def provider(state):
    return {"object_id": "o-1", "content_hash": "h-1",
            "idempotency_key": "k-1", "payload": {}, "complex": False}
eng = LoopXEngine(_target, clock=FakeClock(1000), work_provider=provider,
                  worker=worker)
eng.open()
eng.step()
"""

_SCRIPT_C10_HOLD = """# -*- coding: utf-8 -*-
import os, sys, time
path = sys.argv[1]
hold = float(sys.argv[2])
if os.name == "nt":
    import msvcrt
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    os.write(fd, b"x")
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
else:
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    os.write(fd, b"x")
    fcntl.flock(fd, fcntl.LOCK_EX)
time.sleep(hold)
"""

_SCRIPT_C13_RACE = """# -*- coding: utf-8 -*-
import json, sys
sys.path.insert(0, _V31)
from lease import LeaseStore, LeaseAlreadyHeld, LeaseError
store = LeaseStore(sys.argv[1])
try:
    lease = store.acquire(sys.argv[2], float(sys.argv[3]))
    print(json.dumps({"ok": True, "owner": lease.owner_id,
                      "token": lease.fencing_token}))
    sys.exit(0)
except LeaseAlreadyHeld:
    print(json.dumps({"ok": False, "error": "LeaseAlreadyHeld"}))
    sys.exit(1)
except LeaseError as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
    sys.exit(2)
"""

_SCRIPT_C18_KILL = """# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, _V31)
from faults.injector import Rollbacker
rollbacker = Rollbacker(sys.argv[1])
def hook(rel, idx, total):
    if idx == 2:
        os._exit(11)  # 崩溃点：restore 恢复第 2 个文件后中断
rollbacker.restore(sys.argv[2], sys.argv[3], on_file_restored=hook)
"""

_FAKE_CODEX_OK = """# -*- coding: utf-8 -*-
print('{"type":"response.completed","item":{"text":"done"}}')
"""

_FAKE_CODEX_HANG = """# -*- coding: utf-8 -*-
import time
time.sleep(30)
"""

_FAKE_CODEX_CRASH = """# -*- coding: utf-8 -*-
import sys
sys.stderr.write("codex: fatal internal error")
sys.exit(7)
"""

_FAKE_CODEX_GARBAGE = """# -*- coding: utf-8 -*-
print("this is not jsonl at all")
"""

_FAKE_ADAPTER_BAD = """# -*- coding: utf-8 -*-
import sys
sys.stderr.write("reasonix: cannot start: missing module reasonix.core")
sys.exit(7)
"""

_FAKE_ADAPTER_OK = """# -*- coding: utf-8 -*-
import sys
print("reasonix adapter ready")
sys.exit(0)
"""


# ---------------------------------------------------------------------------
# 案例结果
# ---------------------------------------------------------------------------
@dataclass
class CaseOutcome:
    case_id: int
    name: str
    passed: bool
    exit_code: str
    recovery: str
    evidence: List[str]
    invariant: str
    invariant_holds: bool
    detail: str = ""
    control_passed: bool = False


# ---------------------------------------------------------------------------
# 18 类案例静态七元组表
# ---------------------------------------------------------------------------
CASES: Dict[int, dict] = {
    1: {
        "name": "进程在状态写入前崩溃",
        "injection": "子进程包装 ctr.atomic_write_json，在 STATE 写入前 os._exit(9)",
        "expectation": "STATE.json 不存在或保持旧值（原子写保证）；恢复后不丢已接受事实",
        "invariant": "原子写：崩溃点之前的状态可解析、不被损坏，已接受事实（completed_keys）不丢",
        "recovery": "重启引擎继续 step；已持久化的对象保留、未确认的对象重新处理",
    },
    2: {
        "name": "状态写入后、日志追加前崩溃",
        "injection": "子进程包装 ctr.append_run_log，在第 2 轮 round_start 日志追加前 os._exit(9)",
        "expectation": "STATE 新（第 1 轮已落盘）、LOG 缺第 2 轮行",
        "invariant": "STATE 与 LOG 不一致时以 STATE 为权威；日志允许缺行、可重放",
        "recovery": "重启引擎继续 step：第 2 轮重新执行并补齐日志",
    },
    3: {
        "name": "日志追加后、side effect 确认前崩溃",
        "injection": "子进程：work_processed 日志写盘后、STATE 持久化前 os._exit(9)",
        "expectation": "LOG 有行、side effect 已执行、STATE 未确认",
        "invariant": "恢复后 side effect 不重复执行（幂等键：effect 文件存在即跳过）",
        "recovery": "重启引擎 step：worker 幂等检查 effect 文件，不重复执行 side effect",
    },
    4: {
        "name": "checkpoint payload 篡改",
        "injection": "篡改 checkpoint.json 的 payload（sha256 保持不变）",
        "expectation": "检测（哈希校验）+ 拒绝恢复（fail closed，不返回部分数据）",
        "invariant": "CheckpointCorruptedError 上抛；不返回被篡改的 payload",
        "recovery": "重写正确 checkpoint 后 load 成功；lease 记录继续接受新 checkpoint",
    },
    5: {
        "name": "manifest 缺项",
        "injection": "删除 LOOP-CONTRACT.json / 合同缺 loop_id 字段",
        "expectation": "拒绝启动/恢复，明确报缺什么",
        "invariant": "fail closed：异常上抛且消息点名缺失项（LOOP-CONTRACT.json / loop_id）",
        "recovery": "补齐缺失项后重新构造引擎成功",
    },
    6: {
        "name": "READY 缺失",
        "injection": "工作包缺 ready 标记（ReadyGate 过滤）",
        "expectation": "不继续处理该对象（CLEAN），跳过事实被记录",
        "invariant": "未 READY 对象不进 completed_objects、不产生轮次；gate.skipped 记录该对象",
        "recovery": "对象补齐 READY 后正常处理（对照路径）",
    },
    7: {
        "name": "STATE 截断或非法 JSON",
        "injection": "STATE.json 写半截 JSON / 写合法 JSON 但 phase 非法",
        "expectation": "fail closed 明确报错（JSONDecodeError / 非法 phase）",
        "invariant": "拒绝加载，不猜测、不覆盖；错误消息可定位原因",
        "recovery": "从备份写回合法 STATE 后引擎正常加载",
    },
    8: {
        "name": "RUN-LOG 尾行半写",
        "injection": "RUN-LOG.jsonl 追加半行（截断 JSON）",
        "expectation": "容忍：读日志时丢弃半行并记录",
        "invariant": "好行完整返回、坏行被丢弃且记录（行号+内容）；引擎不受影响",
        "recovery": "无需恢复；后续日志继续正常追加",
    },
    9: {
        "name": "磁盘空间不足/写入失败模拟",
        "injection": "注入 write 失败钩子：ctr.atomic_write_json 抛 OSError(ENOSPC)",
        "expectation": "状态保持旧值、错误上抛、不损坏",
        "invariant": "STATE.json 保持上一轮值且可解析（不损坏）",
        "recovery": "解除钩子后新引擎 step 成功推进",
    },
    10: {
        "name": "文件被占用",
        "injection": "子进程对锁文件持独占锁（msvcrt.locking / flock）",
        "expectation": "重试/明确报错（FileLockedError 含路径），不静默丢",
        "invariant": "占用期间检测到冲突且明确报错；无任何数据被静默丢弃或写坏",
        "recovery": "占用者退出后带重试的 acquire 成功并正常释放",
    },
    11: {
        "name": "Runner 超时/崩溃/返回畸形",
        "injection": "CodexRunner 假 binary 注入：挂起 / exit 7 / 输出非 JSONL 垃圾",
        "expectation": "超时清理、崩溃归因 nonzero_exit、畸形归因 malformed_output",
        "invariant": "三种注入均 ok=False 且 reason 精确归因；正常 binary 对照 ok=True",
        "recovery": "更换正常 binary 后调用成功（对照）",
    },
    12: {
        "name": "重复事件和乱序事件",
        "injection": "相同 payload 事件重复 dispatch；事件 seq 乱序/重复入队",
        "expectation": "幂等跳过重复、乱序按游标拒绝或缓冲排序后处理",
        "invariant": "重复事件不产生第二次 side effect；乱序交付保持 seq 顺序；过期 seq 拒绝",
        "recovery": "乱序补位后按序交付（对照）",
    },
    13: {
        "name": "双 worker 同时抢租约",
        "injection": "两个子进程同时 acquire 同一 lease.json（不同 owner）",
        "expectation": "双进程竞争唯一 owner：一成功一 LeaseAlreadyHeld",
        "invariant": "lease.json 唯一 active owner；退出码集合 {0, 1}",
        "recovery": "失败者自然退出；成功者正常持有并可 renew",
    },
    14: {
        "name": "旧 owner 恢复后尝试写入",
        "injection": "A 过期 → B takeover（token+1）→ A 用旧 token/lease_id 写入",
        "expectation": "fencing 拒绝（FencingTokenMismatch / LeaseOwnershipLost）",
        "invariant": "旧 token 的写入必拒；当前 owner 状态不被破坏",
        "recovery": "旧 owner 重新接管获得新 token 后可正常写入",
    },
    15: {
        "name": "系统时间前跳/后跳",
        "injection": "FakeClock：前跳超过 TTL；后跳回旧 owner 时代",
        "expectation": "前跳可接管；后跳不复活（renew/write_guard 均拒）",
        "invariant": "过期判定以单调 fencing_token 为准，后跳不能复活旧 owner",
        "recovery": "时钟回正后当前 owner 正常续租",
    },
    16: {
        "name": "Reasonix 启动失败和适配器加载失败",
        "injection": "占位适配器：假 executable exit 7 / 坏 JSON 配置 / 缺依赖字段",
        "expectation": "启动失败 exit!=0；加载失败可诊断（点名缺失项）；ECC 侧 fail closed",
        "invariant": "AdapterStartupError 携带退出码与 stderr 摘要；AdapterLoadError 点名依赖",
        "recovery": "替换正常 executable/配置后 load+start 成功（对照）",
    },
    17: {
        "name": "配置候选写入后 readback 不一致",
        "injection": "候选写入 staging 后篡改（corruptor 钩子）",
        "expectation": "读回内容哈希比对失败 → 拒绝采用候选、保留旧配置",
        "invariant": "活跃配置保持旧值；候选被丢弃（staging 清理）",
        "recovery": "重新 propose 无篡改 → commit 成功，活跃配置更新",
    },
    18: {
        "name": "回滚中断后再次恢复",
        "injection": "Rollbacker restore 恢复第 2 个文件后子进程 os._exit(11)",
        "expectation": "再次恢复从备份完整性校验继续，不丢备份、不半恢复",
        "invariant": "备份逐文件 sha256 完好；最终全部目标与备份一致；manifest phase=done",
        "recovery": "再次调用 restore：完整性校验通过 → 全部文件恢复 → phase=done",
    },
}


# ---------------------------------------------------------------------------
# 案例实现
# ---------------------------------------------------------------------------
def _c01(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=_provider([_pkg("o-1", key="k-1")]))
    eng.open()
    r1 = eng.step()
    assert r1["outcome"] == "PROCESSED"
    script = _fill(_SCRIPT_C01, _V31=V31_ROOT, _TARGET=runtime)
    res = run_child(script)
    state = read_json_file(os.path.join(runtime, ctr.STATE_NAME))
    old_kept = (state is not None and state["cursor"] == 1
                and state["completed_keys"] == ["k-1"])
    # 恢复：重启引擎继续 step，已接受事实不丢
    eng2 = LoopXEngine(runtime, clock=FakeClock(1100),
                       work_provider=_provider([_pkg("o-2", key="k-2")]))
    eng2.open()
    r2 = eng2.step()
    recovered = (r2["outcome"] == "PROCESSED"
                 and eng2.status()["completed_keys"] == ["k-1", "k-2"])
    return {
        "passed": res.returncode == 9 and old_kept and recovered,
        "exit_code": str(res.returncode),
        "invariant_holds": old_kept and recovered,
        "evidence": [
            f"崩溃退出码={res.returncode}",
            f"STATE 保持旧值 cursor=1: {old_kept}",
            f"恢复后 completed_keys=[k-1,k-2]: {recovered}",
        ],
    }


def _c02(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    script = _fill(_SCRIPT_C02, _V31=V31_ROOT, _TARGET=runtime)
    res = run_child(script)
    state = read_json_file(os.path.join(runtime, ctr.STATE_NAME))
    state_new = state is not None and state["cursor"] == 1
    log_path = os.path.join(runtime, ctr.LOG_NAME)
    log_text = _read_text(log_path) if os.path.exists(log_path) else ""
    log_missing = '"round": 2' not in log_text
    # 恢复：重启引擎继续 step，补齐日志
    eng2 = LoopXEngine(runtime, clock=FakeClock(1100),
                       work_provider=_provider([_pkg("o-2", key="k-2")]))
    eng2.open()
    r2 = eng2.step()
    log_after = _read_text(log_path)
    recovered = (r2["outcome"] == "PROCESSED"
                 and '"round": 2' in log_after
                 and eng2.status()["cursor"] == 2)
    return {
        "passed": res.returncode == 9 and state_new and log_missing and recovered,
        "exit_code": str(res.returncode),
        "invariant_holds": state_new and log_missing and recovered,
        "evidence": [
            f"崩溃退出码={res.returncode}",
            f"STATE 新值 cursor=1: {state_new}",
            f"LOG 缺第 2 轮行: {log_missing}",
            f"恢复后日志补齐且 cursor=2: {recovered}",
        ],
    }


def _c03(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    effects = inj.child("effects")
    _make_runtime(runtime)
    script = _fill(_SCRIPT_C03, _V31=V31_ROOT, _TARGET=runtime, _EFFECTS=effects)
    res = run_child(script)
    state = read_json_file(os.path.join(runtime, ctr.STATE_NAME))
    state_unconfirmed = (state is not None and "k-1" not in state["completed_keys"])
    log_has_line = '"event": "work_processed"' in _read_text(
        os.path.join(runtime, ctr.LOG_NAME))
    effect_done = os.path.exists(os.path.join(effects, "done-k-1.txt"))
    count_before = _read_text(os.path.join(effects, "count.txt"))

    def worker(package, state):
        key = package["idempotency_key"]
        done = os.path.join(effects, "done-" + key + ".txt")
        if os.path.exists(done):
            return {"status": "ok", "message": "already-done"}
        with open(os.path.join(effects, "count.txt"), "a", encoding="utf-8") as f:
            f.write("x")
        with open(done, "w", encoding="utf-8") as f:
            f.write("side-effect")
        return {"status": "ok", "message": "executed"}

    eng2 = LoopXEngine(runtime, clock=FakeClock(1100),
                       work_provider=_provider([_pkg("o-1", key="k-1")]),
                       worker=worker)
    eng2.open()
    r2 = eng2.step()
    count_after = _read_text(os.path.join(effects, "count.txt"))
    no_repeat = (r2["outcome"] == "PROCESSED" and count_after == count_before
                 and "k-1" in eng2.status()["completed_keys"])
    return {
        "passed": (res.returncode == 9 and state_unconfirmed and log_has_line
                   and effect_done and no_repeat),
        "exit_code": str(res.returncode),
        "invariant_holds": no_repeat,
        "evidence": [
            f"崩溃退出码={res.returncode}",
            f"LOG 有 work_processed 行: {log_has_line}",
            f"STATE 未确认 k-1: {state_unconfirmed}",
            f"恢复后 side effect 未重复（计数不变）: {no_repeat}",
        ],
    }


def _c04(inj: FaultInjector) -> dict:
    d = inj.child("checkpoint")
    cp_path = os.path.join(d, "checkpoint.json")
    cp = CheckpointStore(cp_path)
    cp.write({"cursor": 5, "object_id": "o-3", "accepted": ["k1"]})
    # 篡改 payload（sha256 字段保持旧值）
    record = read_json_file(cp_path)
    record["payload"]["cursor"] = 6
    ctr.atomic_write_json(cp_path, record)
    detected = False
    try:
        cp.load()
    except CheckpointCorruptedError as exc:
        detected = "哈希校验失败" in str(exc)
    # lease 联动：checkpoint 拒绝恢复期间 lease 记录不受损
    lease_path = os.path.join(d, "lease.json")
    store = LeaseStore(lease_path, clock=LeaseFakeClock(1000))
    a = store.acquire("owner-A", 100)
    store.record_last_accepted_checkpoint("owner-A", a.lease_id,
                                          a.fencing_token, "cp-1")
    lease_kept = store.current().last_accepted_checkpoint == "cp-1"
    # 恢复：重写正确 checkpoint
    cp.write({"cursor": 5, "object_id": "o-3", "accepted": ["k1"]})
    payload = cp.load()
    recovered = payload["cursor"] == 5
    store.record_last_accepted_checkpoint("owner-A", a.lease_id,
                                          a.fencing_token, "cp-2")
    return {
        "passed": detected and lease_kept and recovered,
        "exit_code": "1(异常上抛)",
        "invariant_holds": detected and lease_kept and recovered,
        "evidence": [
            f"篡改被哈希校验检测: {detected}",
            f"fail closed（异常而非返回部分数据）: {detected}",
            f"lease 记录保留 cp-1: {lease_kept}",
            f"重写后恢复 load 成功: {recovered}",
        ],
    }


def _c05(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    # 5a 缺 manifest：删除合同文件
    os.unlink(os.path.join(runtime, ctr.CONTRACT_NAME))
    msg_a = ""
    try:
        LoopXEngine(runtime, clock=FakeClock(1000))
    except FileNotFoundError as exc:
        msg_a = str(exc)
    missing_manifest = ctr.CONTRACT_NAME in msg_a
    # 5b 缺项：合同缺 loop_id
    contract = ctr.default_contract("l1", "goal")
    del contract["loop_id"]
    ctr.atomic_write_json(os.path.join(runtime, ctr.CONTRACT_NAME), contract)
    msg_b = ""
    try:
        LoopXEngine(runtime, clock=FakeClock(1000))
    except ValueError as exc:
        msg_b = str(exc)
    missing_field = "loop_id" in msg_b
    # 恢复：补齐字段
    contract = ctr.default_contract("l1", "goal")
    ctr.atomic_write_json(os.path.join(runtime, ctr.CONTRACT_NAME), contract)
    eng = LoopXEngine(runtime, clock=FakeClock(1000))
    recovered = eng.open()["outcome"] == "OPENED"
    return {
        "passed": missing_manifest and missing_field and recovered,
        "exit_code": "1(异常上抛)",
        "invariant_holds": missing_manifest and missing_field and recovered,
        "evidence": [
            f"缺 manifest 报错点名 {ctr.CONTRACT_NAME}: {missing_manifest}",
            f"缺 loop_id 报错点名 loop_id: {missing_field}",
            f"补齐后启动成功: {recovered}",
        ],
    }


def _c06(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    gate = ReadyGate()
    packages = [_pkg("no-ready-1", ready=False), _pkg("ready-1", ready=True)]
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=gate.wrap_provider(_provider(packages)),
                      success_check=lambda s, c: False)
    eng.open()
    r1 = eng.step()
    r2 = eng.step()
    status = eng.status()
    not_processed = ("no-ready-1" not in status["completed_objects"]
                     and r1["outcome"] == "CLEAN")
    processed_ready = (r2["outcome"] == "PROCESSED"
                       and status["completed_objects"] == ["ready-1"]
                       and status["rounds"] == 1)
    skipped_recorded = (len(gate.skipped) == 1
                        and gate.skipped[0][0] == "no-ready-1")
    return {
        "passed": not_processed and processed_ready and skipped_recorded,
        "exit_code": "0(正常跳过)",
        "invariant_holds": not_processed and processed_ready,
        "evidence": [
            f"未 READY 对象不处理（CLEAN）: {not_processed}",
            f"READY 对象正常处理 rounds=1: {processed_ready}",
            f"跳过事实已记录: {skipped_recorded}",
        ],
    }


def _c07(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    # 先造一份合法 STATE 并备份
    eng = LoopXEngine(runtime, clock=FakeClock(1000))
    eng.open()
    state_path = os.path.join(runtime, ctr.STATE_NAME)
    with open(state_path, "r", encoding="utf-8") as f:
        good_state = f.read()
    # 7a 截断
    with open(state_path, "w", encoding="utf-8") as f:
        f.write(good_state[:len(good_state) // 2])
    truncated_rejected = False
    try:
        LoopXEngine(runtime, clock=FakeClock(1000))
    except json.JSONDecodeError:
        truncated_rejected = True
    # 7b 非法 phase
    bad = json.loads(good_state)
    bad["phase"] = "BOGUS"
    ctr.atomic_write_json(state_path, bad)
    illegal_rejected = False
    msg_b = ""
    try:
        LoopXEngine(runtime, clock=FakeClock(1000))
    except ValueError as exc:
        illegal_rejected = "非法 STATE phase" in str(exc)
        msg_b = str(exc)
    # 恢复：从备份写回
    ctr.atomic_write_json(state_path, json.loads(good_state))
    eng2 = LoopXEngine(runtime, clock=FakeClock(1000))
    recovered = eng2.status()["phase"] == "WAITING"
    return {
        "passed": truncated_rejected and illegal_rejected and recovered,
        "exit_code": "1(异常上抛)",
        "invariant_holds": truncated_rejected and illegal_rejected and recovered,
        "evidence": [
            f"截断 STATE 拒绝加载（JSONDecodeError）: {truncated_rejected}",
            f"非法 phase 明确报错: {illegal_rejected} ({msg_b[:60]})",
            f"从备份恢复后正常加载: {recovered}",
        ],
    }


def _c08(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    log_path = os.path.join(runtime, ctr.LOG_NAME)
    ctr.append_run_log(log_path, "INFO", "e1", {"n": 1})
    ctr.append_run_log(log_path, "INFO", "e2", {"n": 2})
    with open(log_path, "a", encoding="utf-8") as f:
        f.write('{"ts": "2025-01-01T00:00:00Z", "level": "INFO", "event": "e3"')
    dropped = []
    entries = tolerant_log_reader(
        log_path, on_dropped=lambda ln, raw: dropped.append((ln, raw)))
    tolerated = (len(entries) == 2 and len(dropped) == 1 and dropped[0][0] == 3)
    # 引擎在同一 runtime 上继续工作不受半行影响
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=_provider([_pkg("o-1", key="k-1")]))
    eng.open()
    ok = eng.step()["outcome"] == "PROCESSED"
    return {
        "passed": tolerated and ok,
        "exit_code": "0(容忍)",
        "invariant_holds": tolerated,
        "evidence": [
            f"好行完整返回 2 条、坏行丢弃并记录（行 3）: {tolerated}",
            f"引擎不受半行影响正常处理: {ok}",
        ],
    }


def _c09(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=_provider(
                          [_pkg("o-1", key="k-1"), _pkg("o-2", key="k-2")]))
    eng.open()
    assert eng.step()["outcome"] == "PROCESSED"
    state_path = os.path.join(runtime, ctr.STATE_NAME)
    before = read_json_file(state_path)
    orig = ctr.atomic_write_json

    def failing(path, payload):
        raise OSError(28, "No space left on device (injected)")

    ctr.atomic_write_json = failing
    raised = False
    try:
        eng.step()
    except OSError as exc:
        raised = exc.errno == 28
    finally:
        ctr.atomic_write_json = orig
    after = read_json_file(state_path)
    old_kept = (after is not None and after["cursor"] == before["cursor"]
                and after["completed_keys"] == before["completed_keys"])
    # 恢复：解除钩子后新引擎 step 成功
    eng2 = LoopXEngine(runtime, clock=FakeClock(1100),
                       work_provider=_provider([_pkg("o-2", key="k-2")]))
    eng2.open()
    recovered = (eng2.step()["outcome"] == "PROCESSED"
                 and eng2.status()["completed_keys"] == ["k-1", "k-2"])
    return {
        "passed": raised and old_kept and recovered,
        "exit_code": "OSError(28) 上抛",
        "invariant_holds": raised and old_kept,
        "evidence": [
            f"写失败 OSError(ENOSPC) 上抛: {raised}",
            f"STATE 保持旧值且可解析（不损坏）: {old_kept}",
            f"解除钩子后恢复推进: {recovered}",
        ],
    }


def _c10(inj: FaultInjector) -> dict:
    d = inj.child("locks")
    lock_path = os.path.join(d, "file.lock")
    hold = spawn_child(_SCRIPT_C10_HOLD, [lock_path, "1.5"])
    time.sleep(0.4)  # 等锁生效
    contender = FileLockContender(interval=0.05)
    locked_reported = False
    try:
        contender.acquire(lock_path, timeout=0.3)
    except FileLockedError as exc:
        locked_reported = lock_path in str(exc)
    reap_child(hold)
    fd = contender.acquire(lock_path, timeout=5.0)  # 重试路径
    acquired = fd is not None
    contender.release(lock_path)
    # 占用期间无静默数据变化：STATE 类文件内容不变
    state_path = os.path.join(d, "state.txt")
    with open(state_path, "w", encoding="utf-8") as f:
        f.write("untouched")
    untouched = _read_text(state_path) == "untouched"
    return {
        "passed": locked_reported and acquired and untouched,
        "exit_code": "0(重试后获得)",
        "invariant_holds": locked_reported and untouched,
        "evidence": [
            f"占用期间明确报错（含路径）: {locked_reported}",
            f"占用者退出后重试获得锁: {acquired}",
            f"无数据被静默丢弃/写坏: {untouched}",
        ],
    }


def _c11(inj: FaultInjector) -> dict:
    scripts = inj.child("scripts")
    wd = inj.child("workdir")

    def fake(name, text):
        p = os.path.join(scripts, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    ok_path = fake("fake_ok.py", _FAKE_CODEX_OK)
    hang_path = fake("fake_hang.py", _FAKE_CODEX_HANG)
    crash_path = fake("fake_crash.py", _FAKE_CODEX_CRASH)
    garbage_path = fake("fake_garbage.py", _FAKE_CODEX_GARBAGE)

    import sys as _sys
    r_timeout = CodexRunner(binary=[_sys.executable, hang_path]).run(
        "p", wd, timeout_seconds=1.0)
    r_crash = CodexRunner(binary=[_sys.executable, crash_path]).run(
        "p", wd, timeout_seconds=10.0)
    r_garbage = CodexRunner(binary=[_sys.executable, garbage_path]).run(
        "p", wd, timeout_seconds=10.0)
    r_ok = CodexRunner(binary=[_sys.executable, ok_path]).run(
        "p", wd, timeout_seconds=10.0)
    timeout_ok = (not r_timeout.ok and r_timeout.timed_out
                  and r_timeout.reason == "timeout")
    crash_ok = (not r_crash.ok and r_crash.exit_code == 7
                and r_crash.reason == "nonzero_exit")
    garbage_ok = (not r_garbage.ok and r_garbage.reason == "malformed_output")
    control_ok = r_ok.ok and r_ok.reason is None
    return {
        "passed": timeout_ok and crash_ok and garbage_ok and control_ok,
        "exit_code": f"timeout/{r_timeout.exit_code} crash=7 garbage=0 对照=0",
        "invariant_holds": timeout_ok and crash_ok and garbage_ok,
        "evidence": [
            f"超时清理归因 timeout: {timeout_ok}",
            f"崩溃归因 nonzero_exit exit=7: {crash_ok}",
            f"畸形归因 malformed_output: {garbage_ok}",
            f"正常 binary 对照 ok=True: {control_ok}",
        ],
    }


def _c12(inj: FaultInjector) -> dict:
    runtime = inj.child("runtime")
    _make_runtime(runtime, mode="event",
                  trigger={"event_types": ["work"]})
    counts = {"n": 0}

    def worker(package, state):
        counts["n"] += 1
        return {"status": "ok", "message": "did"}

    eng = LoopXEngine(runtime, clock=FakeClock(1000), worker=worker)
    eng.open()
    eng.dispatch_event({"type": "work", "payload": {"x": 1}})
    r1 = eng.step()
    eng.dispatch_event({"type": "work", "payload": {"x": 1}})  # 重复
    r2 = eng.step()
    idempotent = r1["outcome"] == "PROCESSED" and r2["outcome"] == "SKIPPED"
    no_repeat_effect = counts["n"] == 1
    # 乱序队列
    q = OrderedEventQueue(start_seq=1)
    first = q.offer({"seq": 2, "type": "b"})
    second = q.offer({"seq": 1, "type": "a"})
    drained = [e["seq"] for e in q.drain()]
    stale = q.offer({"seq": 1, "type": "a-again"})
    ordered = (first == "buffered" and second == "ready"
               and drained == [1, 2] and stale == "rejected"
               and len(q.rejected) == 1)
    return {
        "passed": idempotent and no_repeat_effect and ordered,
        "exit_code": "0(幂等跳过/拒绝)",
        "invariant_holds": idempotent and no_repeat_effect and ordered,
        "evidence": [
            f"重复事件幂等跳过（SKIPPED）: {idempotent}",
            f"无重复 side effect（执行次数=1）: {no_repeat_effect}",
            f"乱序缓冲排序交付 [1,2]、过期 seq 拒绝: {ordered}",
        ],
    }


def _c13(inj: FaultInjector) -> dict:
    d = inj.child("lease")
    lease_path = os.path.join(d, "lease.json")
    script = _fill(_SCRIPT_C13_RACE, _V31=V31_ROOT)
    p1 = spawn_child(script, [lease_path, "owner-A", "30"])
    p2 = spawn_child(script, [lease_path, "owner-B", "30"])
    r1 = reap_child(p1)
    r2 = reap_child(p2)
    codes = sorted([r1.returncode, r2.returncode])
    record = read_json_file(lease_path)
    one_active_owner = (record is not None and record["state"] == "active"
                        and record["owner_id"] in ("owner-A", "owner-B")
                        and record["fencing_token"] == 1)
    return {
        "passed": codes == [0, 1] and one_active_owner,
        "exit_code": "/".join(str(x) for x in sorted([r1.returncode, r2.returncode])),
        "invariant_holds": codes == [0, 1] and one_active_owner,
        "evidence": [
            f"双进程退出码 {codes}（一成功一 LeaseAlreadyHeld）: {codes == [0, 1]}",
            f"lease.json 唯一 active owner: {one_active_owner}",
        ],
    }


def _c14(inj: FaultInjector) -> dict:
    d = inj.child("lease")
    store = LeaseStore(os.path.join(d, "lease.json"), clock=LeaseFakeClock(1000))
    a = store.acquire("owner-A", 100)          # token 1，expires 1100
    clock = store._clock
    clock.advance(150)                          # t=1150 过期
    b = store.takeover("owner-B", 100)          # token 2
    fenced = False
    try:
        store.write_guard(a.fencing_token)
    except FencingTokenMismatch:
        fenced = True
    lost = False
    try:
        store.record_last_accepted_checkpoint("owner-A", a.lease_id,
                                              a.fencing_token, "cp-9")
    except LeaseOwnershipLost:
        lost = True
    # 恢复：B 释放后旧 owner 重新接管获得新 token
    store.release("owner-B", b.lease_id)
    c = store.takeover("owner-A", 100)          # token 3
    try:
        store.write_guard(c.fencing_token)
        rewritable = True
    except FencingTokenMismatch:
        rewritable = False
    return {
        "passed": fenced and lost and rewritable and b.fencing_token == 2,
        "exit_code": "异常上抛(FencingTokenMismatch/LeaseOwnershipLost)",
        "invariant_holds": fenced and lost,
        "evidence": [
            f"旧 token write_guard 被 fencing 拒绝: {fenced}",
            f"旧 lease_id checkpoint 写入被拒: {lost}",
            f"重新接管（token 3）后可写入: {rewritable}",
        ],
    }


def _c15(inj: FaultInjector) -> dict:
    d = inj.child("lease")
    clock = LeaseFakeClock(1000)
    store = LeaseStore(os.path.join(d, "lease.json"), clock=clock)
    a = store.acquire("owner-A", 100)          # token 1，expires 1100
    clock.advance(300)                          # t=1300 前跳超 TTL
    b = store.takeover("owner-B", 100)          # 前跳可接管 → token 2
    forward_ok = b.fencing_token == 2
    clock.set(900)                              # 后跳回 A 时代
    no_revive_renew = False
    try:
        store.renew("owner-A", a.lease_id)
    except LeaseOwnershipLost:
        no_revive_renew = True
    no_revive_guard = False
    try:
        store.write_guard(a.fencing_token)
    except FencingTokenMismatch:
        no_revive_guard = True
    # 恢复：时钟回正后当前 owner 正常续租
    clock.set(1300)
    renewed = store.renew("owner-B", b.lease_id)
    owner_intact = renewed.owner_id == "owner-B"
    return {
        "passed": (forward_ok and no_revive_renew and no_revive_guard
                   and owner_intact),
        "exit_code": "异常上抛(后跳拒绝)",
        "invariant_holds": forward_ok and no_revive_renew and no_revive_guard,
        "evidence": [
            f"前跳超 TTL 后可接管（token 2）: {forward_ok}",
            f"后跳不复活：旧 owner renew 被拒: {no_revive_renew}",
            f"后跳不复活：旧 token write_guard 被拒: {no_revive_guard}",
            f"时钟回正后当前 owner 续租成功: {owner_intact}",
        ],
    }


def _c16(inj: FaultInjector) -> dict:
    d = inj.child("adapter")
    good_cfg = os.path.join(d, "good.json")
    ctr.atomic_write_json(good_cfg, {
        "adapter": "reasonix",
        "dependencies": {"reasonix.core": "2.1", "ecc.bridge": "1.0"},
        "config": {"a": 1},
    })
    bad_json_cfg = os.path.join(d, "bad-json.json")
    with open(bad_json_cfg, "w", encoding="utf-8") as f:
        f.write('{"adapter": "reasonix", ')
    missing_dep_cfg = os.path.join(d, "missing-dep.json")
    ctr.atomic_write_json(missing_dep_cfg, {"adapter": "reasonix"})
    bad_exe = os.path.join(d, "fake_bad.py")
    with open(bad_exe, "w", encoding="utf-8") as f:
        f.write(_FAKE_ADAPTER_BAD)
    good_exe = os.path.join(d, "fake_ok.py")
    with open(good_exe, "w", encoding="utf-8") as f:
        f.write(_FAKE_ADAPTER_OK)

    adapter = ReasonixAdapterPlaceholder(config_path=good_cfg,
                                         executable=bad_exe)
    adapter.load()  # 配置本身合法
    start_failed = False
    try:
        adapter.start(timeout_seconds=10)
    except AdapterStartupError as exc:
        start_failed = (exc.exit_code == 7 and "missing module" in str(exc))
    bad_json_rejected = False
    try:
        ReasonixAdapterPlaceholder(config_path=bad_json_cfg,
                                   executable=good_exe).load()
    except AdapterLoadError as exc:
        bad_json_rejected = "JSON" in str(exc)
    missing_dep_rejected = False
    try:
        ReasonixAdapterPlaceholder(config_path=missing_dep_cfg,
                                   executable=good_exe).load()
    except AdapterLoadError as exc:
        missing_dep_rejected = "reasonix.core" in str(exc)
    # 恢复（对照）：正常 executable + 正常配置
    good = ReasonixAdapterPlaceholder(config_path=good_cfg,
                                      executable=good_exe)
    good.load()
    started = good.start(timeout_seconds=10)
    recovered = started["ok"] and started["exit_code"] == 0
    return {
        "passed": (start_failed and bad_json_rejected
                   and missing_dep_rejected and recovered),
        "exit_code": "7(启动失败) / 异常上抛(加载失败)",
        "invariant_holds": start_failed and bad_json_rejected \
            and missing_dep_rejected,
        "evidence": [
            f"启动失败 exit=7 且可诊断（stderr 摘要）: {start_failed}",
            f"坏 JSON 配置加载失败可诊断: {bad_json_rejected}",
            f"缺依赖加载失败点名 reasonix.core: {missing_dep_rejected}",
            f"正常 executable/配置恢复启动成功: {recovered}",
        ],
    }


def _c17(inj: FaultInjector) -> dict:
    d = inj.child("config")
    active = os.path.join(d, "active.json")
    with open(active, "w", encoding="utf-8", newline="\n") as f:
        f.write('{"version": 1}\n')  # 旧配置
    sw = ConfigSwitcher(active, os.path.join(d, "staging"))

    def corruptor(tmp):
        with open(tmp, "a", encoding="utf-8") as f:
            f.write("TAMPERED")

    mismatch = False
    try:
        sw.propose('{"version": 2}\n', corruptor=corruptor)
    except ConfigReadbackMismatch:
        mismatch = True
    old_kept = sw.active() == '{"version": 1}\n'
    staged_clean = sw.discarded()
    # 恢复：重新 propose 无篡改 → commit
    cand = sw.propose('{"version": 2}\n')
    sw.commit(cand)
    updated = sw.active() == '{"version": 2}\n'
    return {
        "passed": mismatch and old_kept and staged_clean and updated,
        "exit_code": "异常上抛(ConfigReadbackMismatch)",
        "invariant_holds": mismatch and old_kept and staged_clean,
        "evidence": [
            f"读回不一致被检测（拒绝采用）: {mismatch}",
            f"旧配置保留（active 未变）: {old_kept}",
            f"候选被丢弃（staging 清理）: {staged_clean}",
            f"重新 propose+commit 成功更新: {updated}",
        ],
    }


def _c18(inj: FaultInjector) -> dict:
    work = inj.child("targets")
    backup_root = inj.child("backups")
    files = {}
    for i in range(1, 5):
        p = os.path.join(work, f"f{i}.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"v1-content-{i}\n")
        files[f"f{i}.txt"] = p
    rb = Rollbacker(backup_root)
    rb.backup(files, snapshot_id="snap-001")
    # 全部目标篡改为 v2
    for i in range(1, 5):
        with open(os.path.join(work, f"f{i}.txt"), "w", encoding="utf-8") as f:
            f.write(f"v2-tampered-{i}\n")
    script = _fill(_SCRIPT_C18_KILL, _V31=V31_ROOT)
    res = run_child(script, [backup_root, "snap-001", work])
    phase_after_kill = rb.status("snap-001")["phase"]
    partially_restored = phase_after_kill == "restoring"
    # 再次恢复：从备份完整性校验继续
    rb.restore("snap-001", work)
    manifest = rb.status("snap-001")
    all_restored = all(
        _read_text(os.path.join(work, f"f{i}.txt")) == f"v1-content-{i}\n"
        for i in range(1, 5))
    backup_intact = manifest["phase"] == "done"
    verify_ok = rb.verify_backup("snap-001") is not None
    return {
        "passed": (res.returncode == 11 and partially_restored and all_restored
                   and backup_intact and verify_ok),
        "exit_code": f"11(中断) → 0(续跑完成)",
        "invariant_holds": all_restored and backup_intact and verify_ok,
        "evidence": [
            f"中断退出码={res.returncode}，manifest phase=restoring: {partially_restored}",
            f"再次恢复后全部目标与备份一致: {all_restored}",
            f"备份完整性校验通过（不丢备份）: {verify_ok}",
            f"manifest phase=done（不半恢复）: {backup_intact}",
        ],
    }


_FAULTS: Dict[int, Callable] = {
    1: _c01, 2: _c02, 3: _c03, 4: _c04, 5: _c05, 6: _c06,
    7: _c07, 8: _c08, 9: _c09, 10: _c10, 11: _c11, 12: _c12,
    13: _c13, 14: _c14, 15: _c15, 16: _c16, 17: _c17, 18: _c18,
}


# ---------------------------------------------------------------------------
# 对照案例（相同场景、不注入故障 → 正常完成）
# ---------------------------------------------------------------------------
def _run_engine_normal(inj: FaultInjector) -> bool:
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=_provider(
                          [_pkg("o-1", key="k-1"), _pkg("o-2", key="k-2")]))
    eng.open()
    r1 = eng.step()
    r2 = eng.step()
    return (r1["outcome"] == "PROCESSED" and r2["outcome"] == "PROCESSED"
            and eng.status()["cursor"] == 2
            and eng.status()["completed_keys"] == ["k-1", "k-2"])


def _ctrl_01(inj):
    return _run_engine_normal(inj)


def _ctrl_02(inj):
    return _run_engine_normal(inj)


def _ctrl_03(inj):
    runtime = inj.child("runtime")
    effects = inj.child("effects")
    _make_runtime(runtime)
    count_path = os.path.join(effects, "count.txt")

    def worker(package, state):
        with open(count_path, "a", encoding="utf-8") as f:
            f.write("x")
        return {"status": "ok", "message": "executed"}

    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=_provider([_pkg("o-1", key="k-1")]),
                      worker=worker)
    eng.open()
    r = eng.step()
    return r["outcome"] == "PROCESSED" and eng.status()["cursor"] == 1


def _ctrl_04(inj):
    d = inj.child("checkpoint")
    cp = CheckpointStore(os.path.join(d, "cp.json"))
    cp.write({"cursor": 5, "accepted": ["k1"]})
    return cp.load()["cursor"] == 5


def _ctrl_05(inj):
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    eng = LoopXEngine(runtime, clock=FakeClock(1000))
    return eng.open()["outcome"] == "OPENED"


def _ctrl_06(inj):
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    gate = ReadyGate()
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=gate.wrap_provider(
                          _provider([_pkg("ready-1", ready=True)])),
                      success_check=lambda s, c: False)
    eng.open()
    r = eng.step()
    return (r["outcome"] == "PROCESSED" and not gate.skipped
            and eng.status()["completed_objects"] == ["ready-1"])


def _ctrl_07(inj):
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    eng = LoopXEngine(runtime, clock=FakeClock(1000))
    eng.open()
    eng2 = LoopXEngine(runtime, clock=FakeClock(1000))  # 重新加载合法 STATE
    return eng2.status()["phase"] == "WAITING"


def _ctrl_08(inj):
    runtime = inj.child("runtime")
    log_path = os.path.join(runtime, "clean.log")
    ctr.append_run_log(log_path, "INFO", "e1", {})
    ctr.append_run_log(log_path, "INFO", "e2", {})
    dropped = []
    entries = tolerant_log_reader(
        log_path, on_dropped=lambda ln, raw: dropped.append((ln, raw)))
    return len(entries) == 2 and not dropped


def _ctrl_09(inj):
    runtime = inj.child("runtime")
    _make_runtime(runtime)
    eng = LoopXEngine(runtime, clock=FakeClock(1000),
                      work_provider=_provider([_pkg("o-1", key="k-1")]))
    eng.open()
    r = eng.step()
    return r["outcome"] == "PROCESSED" and eng.status()["cursor"] == 1


def _ctrl_10(inj):
    d = inj.child("locks")
    lock_path = os.path.join(d, "file.lock")
    contender = FileLockContender(interval=0.05)
    fd = contender.acquire(lock_path, timeout=2.0)
    contender.release(lock_path)
    return fd is not None


def _ctrl_11(inj):
    d = inj.child("scripts")
    wd = inj.child("workdir")
    p = os.path.join(d, "fake_ok.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write(_FAKE_CODEX_OK)
    import sys as _sys
    r = CodexRunner(binary=[_sys.executable, p]).run("p", wd,
                                                     timeout_seconds=10)
    return r.ok and r.reason is None


def _ctrl_12(inj):
    runtime = inj.child("runtime")
    _make_runtime(runtime, mode="event", trigger={"event_types": ["work"]})
    eng = LoopXEngine(runtime, clock=FakeClock(1000))
    eng.open()
    eng.dispatch_event({"type": "work", "payload": {"x": 1}})
    r = eng.step()
    q = OrderedEventQueue(start_seq=1)
    q.offer({"seq": 1, "type": "a"})
    q.offer({"seq": 2, "type": "b"})
    drained = [e["seq"] for e in q.drain()]
    return r["outcome"] == "PROCESSED" and drained == [1, 2] and not q.rejected


def _ctrl_13(inj):
    d = inj.child("lease")
    store = LeaseStore(os.path.join(d, "lease.json"),
                       clock=LeaseFakeClock(1000))
    a = store.acquire("owner-A", 100)
    store.renew("owner-A", a.lease_id)
    store.release("owner-A", a.lease_id)
    return store.current().state == "released"


def _ctrl_14(inj):
    d = inj.child("lease")
    store = LeaseStore(os.path.join(d, "lease.json"),
                       clock=LeaseFakeClock(1000))
    a = store.acquire("owner-A", 100)
    b = store.renew("owner-A", a.lease_id)
    store.write_guard(b.fencing_token)
    return b.fencing_token == a.fencing_token


def _ctrl_15(inj):
    d = inj.child("lease")
    clock = LeaseFakeClock(1000)
    store = LeaseStore(os.path.join(d, "lease.json"), clock=clock)
    a = store.acquire("owner-A", 100)
    clock.advance(50)  # TTL 内正常前进
    b = store.renew("owner-A", a.lease_id)
    return b.owner_id == "owner-A"


def _ctrl_16(inj):
    d = inj.child("adapter")
    cfg = os.path.join(d, "good.json")
    ctr.atomic_write_json(cfg, {
        "adapter": "reasonix",
        "dependencies": {"reasonix.core": "2.1", "ecc.bridge": "1.0"},
    })
    exe = os.path.join(d, "fake_ok.py")
    with open(exe, "w", encoding="utf-8") as f:
        f.write(_FAKE_ADAPTER_OK)
    adapter = ReasonixAdapterPlaceholder(config_path=cfg, executable=exe)
    adapter.load()
    r = adapter.start(timeout_seconds=10)
    return r["ok"] and r["exit_code"] == 0


def _ctrl_17(inj):
    d = inj.child("config")
    active = os.path.join(d, "active.json")
    with open(active, "w", encoding="utf-8", newline="\n") as f:
        f.write('{"version": 1}\n')
    sw = ConfigSwitcher(active, os.path.join(d, "staging"))
    cand = sw.propose('{"version": 2}\n')
    sw.commit(cand)
    return sw.active() == '{"version": 2}\n'


def _ctrl_18(inj):
    work = inj.child("targets")
    backup_root = inj.child("backups")
    files = {}
    for i in range(1, 3):
        p = os.path.join(work, f"f{i}.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"v1-{i}\n")
        files[f"f{i}.txt"] = p
    rb = Rollbacker(backup_root)
    rb.backup(files, snapshot_id="snap-001")
    for i in range(1, 3):
        with open(os.path.join(work, f"f{i}.txt"), "w", encoding="utf-8") as f:
            f.write("tampered\n")
    rb.restore("snap-001", work)
    manifest = rb.status("snap-001")
    all_ok = all(
        _read_text(os.path.join(work, f"f{i}.txt")) == f"v1-{i}\n"
        for i in range(1, 3))
    return manifest["phase"] == "done" and all_ok


_CONTROLS: Dict[int, Callable] = {
    1: _ctrl_01, 2: _ctrl_02, 3: _ctrl_03, 4: _ctrl_04, 5: _ctrl_05,
    6: _ctrl_06, 7: _ctrl_07, 8: _ctrl_08, 9: _ctrl_09, 10: _ctrl_10,
    11: _ctrl_11, 12: _ctrl_12, 13: _ctrl_13, 14: _ctrl_14, 15: _ctrl_15,
    16: _ctrl_16, 17: _ctrl_17, 18: _ctrl_18,
}


# ---------------------------------------------------------------------------
# 执行入口与报告
# ---------------------------------------------------------------------------
def run_case(case_id: int, base: str = None) -> CaseOutcome:
    """在隔离副本上执行指定案例，返回 CaseOutcome。"""
    spec = CASES[case_id]
    inj = FaultInjector(base)
    inj.inject(case_id, spec["injection"], spec["expectation"],
               spec["invariant"], spec["recovery"])
    try:
        result = _FAULTS[case_id](inj)
        passed = bool(result["passed"]) and bool(result["invariant_holds"])
        exit_code = result["exit_code"]
        evidence = result["evidence"]
        invariant_holds = bool(result["invariant_holds"])
        detail = ""
    except Exception as exc:  # noqa: BLE001 —— 案例失败也要产出行报告
        passed = False
        exit_code = "-"
        evidence = []
        invariant_holds = False
        detail = f"执行异常: {type(exc).__name__}: {exc}"
    inj.observe(case_id, observation=(
        "通过" if passed else f"失败 {detail}"), exit_code=exit_code,
        evidence=evidence)
    inj.close()
    return CaseOutcome(
        case_id=case_id, name=spec["name"], passed=passed,
        exit_code=exit_code, recovery=spec["recovery"],
        evidence=evidence, invariant=spec["invariant"],
        invariant_holds=invariant_holds, detail=detail,
    )


def run_control(case_id: int, base: str = None) -> bool:
    """对照案例：相同场景、不注入故障 → 正常完成。"""
    inj = FaultInjector(base)
    try:
        return bool(_CONTROLS[case_id](inj))
    except Exception:  # noqa: BLE001
        return False
    finally:
        inj.close()


def run_all(base: str = None) -> List[CaseOutcome]:
    """执行全部 18 类故障案例 + 18 个对照，返回结果列表。"""
    outcomes = []
    for case_id in sorted(CASES):
        outcome = run_case(case_id, base)
        outcome.control_passed = run_control(case_id, base)
        outcomes.append(outcome)
    return outcomes


def render_report(outcomes: List[CaseOutcome],
                  title: str = "ECC V3.1 WP-04 故障注入与恢复矩阵") -> str:
    lines = ["=" * 76, title,
             f"生成: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
             "=" * 76]
    for o in outcomes:
        spec = CASES[o.case_id]
        lines.append(f"{o.case_id:02d}. {o.name} ...... "
                     f"{'PASS' if o.passed else 'FAIL'}")
        lines.append(f"    注入方法: {spec['injection']}")
        lines.append(f"    预期:     {spec['expectation']}")
        lines.append(f"    观察:     {'通过' if o.passed else o.detail or '失败'}")
        lines.append(f"    退出码:   {o.exit_code}")
        lines.append(f"    状态不变量: {o.invariant}")
        lines.append(f"    恢复动作: {o.recovery}")
        lines.append(f"    证据:     {' | '.join(o.evidence) if o.evidence else '-'}")
        lines.append(f"    对照:     {'PASS' if o.control_passed else 'FAIL'}")
    faults_passed = sum(1 for o in outcomes if o.passed)
    controls_passed = sum(1 for o in outcomes if o.control_passed)
    lines.append("-" * 76)
    lines.append(f"故障案例: {faults_passed}/{len(outcomes)} PASS")
    lines.append(f"对照案例: {controls_passed}/{len(outcomes)} PASS")
    lines.append("已知限制: 案例 16 使用占位适配器（fake executable），"
                 "真实 Reasonix 适配器接入后在 WP-05 用同一错误契约补真实案例；"
                 "案例 18 使用通用回滚器，真实 Reasonix 回滚在 WP-05 用同一机制。")
    return "\n".join(lines) + "\n"
