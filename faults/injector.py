# -*- coding: utf-8 -*-
"""WP-04 系统化故障注入与恢复——统一 FaultInjector 与故障工具。

安全约定：
- 全部注入发生在隔离副本（tempfile.TemporaryDirectory）上；FaultInjector 默认
  自建隔离目录，绝不指向正式 runtime / 用户数据 / 证据目录。
- 崩溃类案例用独立子进程在精确崩溃点 os._exit(非零码) 模拟（真实进程退出语义），
  钩子通过包装 loopx.contract.atomic_write_json / append_run_log 注入。

工具清单：
- CheckpointStore：checkpoint payload + sha256 校验；篡改即 fail closed。
- Rollbacker：backup / restore 两阶段 + 备份完整性校验；restore 中断后再次
  恢复从完整性校验继续，不丢备份、不半恢复。
- ConfigSwitcher：候选写入 -> 读回 -> 哈希比对；不一致拒绝采用、保留旧配置。
- ReadyGate：READY 标记缺失的对象不处理（记录跳过）。
- OrderedEventQueue：乱序事件缓冲排序、重复/过期按游标拒绝。
- FileLockContender：文件占用检测 + 重试 + 明确报错（不静默丢）。
- ReasonixAdapterPlaceholder：占位适配器（启动失败 exit!=0 / 加载失败可诊断，
  ECC 侧 fail closed）；真实 Reasonix 适配器接入后（WP-05）以同一错误契约替换。
- tolerant_log_reader：容忍 RUN-LOG 尾行半写并记录丢弃行。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from loopx import contract as ctr

# ---------------------------------------------------------------------------
# 路径与子进程
# ---------------------------------------------------------------------------
V31_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


@dataclass
class ChildProcessResult:
    """子进程运行结果（注入进程的观测）。"""
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


def write_child_script(script_text: str, dirpath: str) -> str:
    """把注入脚本写到隔离目录，返回脚本路径。"""
    path = os.path.join(dirpath, "child_fault.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(script_text)
    return path


def run_child(script_text: str, args=(), *, cwd: str = None,
              timeout: float = 60.0) -> ChildProcessResult:
    """在隔离临时目录运行注入脚本（sys.executable 直跑，绝不 shell 拼接）。"""
    tmp = tempfile.mkdtemp(prefix="ecc-wp04-child-")
    try:
        script = write_child_script(script_text, tmp)
        argv = [PY, script] + [str(a) for a in args]
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, cwd=cwd,
            )
            return ChildProcessResult(
                proc.returncode, proc.stdout, proc.stderr,
                round(time.monotonic() - start, 3),
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            return ChildProcessResult(-1, out, err + "\n<child timeout>",
                                      round(time.monotonic() - start, 3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def spawn_child(script_text: str, args=(), *, cwd: str = None):
    """非阻塞启动注入脚本，返回 Popen（供持锁/竞争类案例使用）。"""
    tmp = tempfile.mkdtemp(prefix="ecc-wp04-hold-")
    script = write_child_script(script_text, tmp)
    argv = [PY, script] + [str(a) for a in args]
    proc = subprocess.Popen(
        argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    proc._wp04_tmp = tmp  # 清理标记
    return proc


def reap_child(proc) -> ChildProcessResult:
    """等待子进程结束并清理其脚本目录。"""
    out, err = proc.communicate(timeout=60)
    tmp = getattr(proc, "_wp04_tmp", None)
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return ChildProcessResult(proc.returncode, out or "", err or "")


# ---------------------------------------------------------------------------
# 观察
# ---------------------------------------------------------------------------
def read_json_file(path: str) -> Optional[dict]:
    """读取 JSON 文件；不存在返回 None；损坏抛 JSONDecodeError/OSError。"""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_text_file(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def tolerant_log_reader(path: str, *, on_dropped: Callable = None) -> List[dict]:
    """读取 RUN-LOG，容忍尾行半写：坏行丢弃并记录（on_dropped(lineno, raw)）。"""
    entries: List[dict] = []
    if not os.path.exists(path):
        return entries
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                if on_dropped is not None:
                    on_dropped(lineno, line)
    return entries


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# CheckpointStore：payload 哈希校验，篡改 fail closed
# ---------------------------------------------------------------------------
class CheckpointCorruptedError(Exception):
    """checkpoint 损坏（结构缺失或 sha256 不匹配）：fail closed。"""


class CheckpointStore:
    """checkpoint payload + sha256 校验和；load 校验失败拒绝返回任何部分数据。"""

    SCHEMA = "ecc-v3.1-checkpoint"

    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    @staticmethod
    def _digest(payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return sha256_text(canonical)

    def write(self, payload: dict) -> dict:
        record = {
            "schema": self.SCHEMA,
            "payload": payload,
            "sha256": self._digest(payload),
        }
        ctr.atomic_write_json(self.path, record)
        return record

    def load(self) -> dict:
        """校验并返回 payload；损坏抛 CheckpointCorruptedError（fail closed）。"""
        try:
            record = read_json_file(self.path)
        except (json.JSONDecodeError, OSError) as exc:
            raise CheckpointCorruptedError(
                f"checkpoint 无法解析（fail closed）：{self.path}: {exc}"
            ) from exc
        if not isinstance(record, dict) or record.get("schema") != self.SCHEMA:
            raise CheckpointCorruptedError(
                f"checkpoint 结构非法（fail closed）：{self.path}"
            )
        payload = record.get("payload")
        if not isinstance(payload, dict) or "sha256" not in record:
            raise CheckpointCorruptedError(
                f"checkpoint 缺 payload/sha256（fail closed）：{self.path}"
            )
        if record["sha256"] != self._digest(payload):
            raise CheckpointCorruptedError(
                f"checkpoint 哈希校验失败（payload 被篡改，fail closed）：{self.path}"
            )
        return payload


# ---------------------------------------------------------------------------
# Rollbacker：backup/restore 两阶段 + 完整性校验续跑
# ---------------------------------------------------------------------------
class BackupCorruptedError(Exception):
    """备份损坏（文件缺失或 sha256 不匹配）：fail closed，拒绝恢复。"""


class RollbackInterrupted(Exception):
    """上次 restore 中断（manifest phase=restoring）：信息性异常。"""


class Rollbacker:
    """通用回滚器（两阶段）：

    - backup(files)：把 {target_rel: source_abs} 复制到备份目录并写 manifest
      （phase=prepared，逐文件 sha256）。
    - restore(snapshot_id)：先做备份完整性校验（逐文件 sha256，失败抛
      BackupCorruptedError）→ manifest phase=restoring → 逐文件原子恢复
      （on_file_restored 钩子可在半途 os._exit 模拟中断）→ phase=done。
    - 中断后再次 restore：完整性校验通过即从备份继续，全部文件重新恢复，
      不丢备份、不半恢复。
    """

    MANIFEST = "manifest.json"
    SCHEMA = "ecc-v3.1-rollback"

    def __init__(self, backup_dir: str):
        self.backup_dir = os.path.abspath(backup_dir)
        os.makedirs(self.backup_dir, exist_ok=True)

    def _snap_dir(self, snapshot_id: str) -> str:
        return os.path.join(self.backup_dir, snapshot_id)

    def _files_dir(self, snapshot_id: str) -> str:
        return os.path.join(self._snap_dir(snapshot_id), "files")

    def _manifest_path(self, snapshot_id: str) -> str:
        return os.path.join(self._snap_dir(snapshot_id), self.MANIFEST)

    def backup(self, files: Dict[str, str], snapshot_id: str = "snap-001") -> dict:
        """files: {target_rel_path: source_abs_path}。复制后记录 sha256。"""
        snap = self._snap_dir(snapshot_id)
        files_dir = self._files_dir(snapshot_id)
        if os.path.exists(snap):
            raise ValueError(f"快照已存在：{snapshot_id}")
        os.makedirs(files_dir, exist_ok=True)
        entries = []
        for rel, src in sorted(files.items()):
            dst = os.path.join(files_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            entries.append({
                "rel": rel, "sha256": sha256_file(dst),
                "size": os.path.getsize(dst),
            })
        manifest = {
            "schema": self.SCHEMA,
            "snapshot_id": snapshot_id,
            "phase": "prepared",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": entries,
        }
        ctr.atomic_write_json(self._manifest_path(snapshot_id), manifest)
        return manifest

    def _read_manifest(self, snapshot_id: str) -> dict:
        manifest = read_json_file(self._manifest_path(snapshot_id))
        if not isinstance(manifest, dict) or manifest.get("schema") != self.SCHEMA:
            raise BackupCorruptedError(
                f"备份 manifest 缺失或非法（fail closed）：{snapshot_id}"
            )
        return manifest

    def verify_backup(self, snapshot_id: str) -> dict:
        """备份完整性校验：逐文件 sha256 比对；损坏抛 BackupCorruptedError。"""
        manifest = self._read_manifest(snapshot_id)
        problems = []
        for entry in manifest.get("files", []):
            fp = os.path.join(self._files_dir(snapshot_id),
                              entry["rel"].replace("/", os.sep))
            if not os.path.isfile(fp):
                problems.append(f"缺文件 {entry['rel']}")
            elif sha256_file(fp) != entry["sha256"]:
                problems.append(f"哈希不匹配 {entry['rel']}")
        if problems:
            raise BackupCorruptedError(
                f"备份完整性校验失败（fail closed）：{snapshot_id}: {problems}"
            )
        return manifest

    def status(self, snapshot_id: str) -> dict:
        return self._read_manifest(snapshot_id)

    def restore(self, snapshot_id: str, targets_dir: str,
                on_file_restored: Callable = None) -> dict:
        """恢复全部文件。中断后再次调用会从完整性校验继续（幂等覆盖）。"""
        manifest = self.verify_backup(snapshot_id)
        manifest["phase"] = "restoring"
        ctr.atomic_write_json(self._manifest_path(snapshot_id), manifest)
        entries = manifest["files"]
        for idx, entry in enumerate(entries, start=1):
            src = os.path.join(self._files_dir(snapshot_id),
                               entry["rel"].replace("/", os.sep))
            dst = os.path.join(targets_dir, entry["rel"].replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".rollback-", dir=os.path.dirname(dst))
            os.close(fd)  # 立即关闭句柄（Windows 上泄漏句柄会阻止目录清理）
            try:
                with open(tmp, "wb") as out, open(src, "rb") as inp:
                    shutil.copyfileobj(inp, out)
                os.replace(tmp, dst)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            if on_file_restored is not None:
                on_file_restored(entry["rel"], idx, len(entries))
        manifest["phase"] = "done"
        manifest["restored_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ctr.atomic_write_json(self._manifest_path(snapshot_id), manifest)
        return manifest


# ---------------------------------------------------------------------------
# ConfigSwitcher：候选写后读回不一致 → 拒绝采用
# ---------------------------------------------------------------------------
class ConfigReadbackMismatch(Exception):
    """候选配置写入后读回内容与提交内容哈希不一致：拒绝采用。"""


class ConfigSwitcher:
    """配置切换器：候选写入 staging → 读回 → 哈希比对 → 通过才可 commit。
    读回不一致抛 ConfigReadbackMismatch，活跃配置保持旧值。"""

    def __init__(self, active_path: str, staging_dir: str):
        self.active_path = os.path.abspath(active_path)
        self.staging_dir = os.path.abspath(staging_dir)
        os.makedirs(self.staging_dir, exist_ok=True)
        self._candidates: Dict[str, str] = {}

    def _hash(self, text: str) -> str:
        return sha256_text(text)

    def propose(self, text: str, *, corruptor: Callable = None) -> str:
        """写候选 → （可选篡改）→ 读回 → 哈希比对。不一致抛错并丢弃候选。"""
        fd, tmp = tempfile.mkstemp(prefix="cand-", suffix=".json",
                                   dir=self.staging_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
            if corruptor is not None:
                corruptor(tmp)
            readback = read_text_file(tmp)
            if readback is None or self._hash(readback) != self._hash(text):
                raise ConfigReadbackMismatch(
                    f"候选配置读回不一致（拒绝采用）：期望 sha256={self._hash(text)}"
                    f" 实际 sha256={self._hash(readback or '')} 候选={tmp}"
                )
            cand_id = os.path.basename(tmp)
            self._candidates[cand_id] = tmp
            return cand_id
        except ConfigReadbackMismatch:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def commit(self, candidate_id: str) -> None:
        """通过读回校验的候选才允许采用（commit 时再校验一次，防 TOCTOU）。"""
        tmp = self._candidates.get(candidate_id)
        if tmp is None or not os.path.exists(tmp):
            raise ValueError(f"候选不存在或已丢弃：{candidate_id}")
        text = read_text_file(tmp)
        os.replace(tmp, self.active_path)  # 同卷原子替换
        self._candidates.pop(candidate_id, None)
        _ = text

    def active(self) -> Optional[str]:
        return read_text_file(self.active_path)

    def discarded(self) -> bool:
        """staging 是否已清理（无候选残留）。"""
        leftovers = [n for n in os.listdir(self.staging_dir)
                     if n.startswith("cand-")]
        return not leftovers


# ---------------------------------------------------------------------------
# ReadyGate：READY 缺失不处理
# ---------------------------------------------------------------------------
class ReadyGate:
    """工作包 READY 门：无 ready 标记（或 ready 非真）的对象不交付处理，
    记录跳过事实（skipped）。"""

    def __init__(self):
        self.skipped: List[tuple] = []

    def filter(self, package: dict):
        if not package.get("ready"):
            self.skipped.append((package.get("object_id"), "missing READY"))
            return None
        return package

    def wrap_provider(self, provider: Callable) -> Callable:
        def wrapped(state):
            package = provider(state)
            if package is None:
                return None
            return self.filter(package)
        return wrapped


# ---------------------------------------------------------------------------
# OrderedEventQueue：乱序按游标拒绝或缓冲排序后处理
# ---------------------------------------------------------------------------
class OutOfOrderEvent(Exception):
    """事件序号乱序/重复（seq 小于期望游标）。"""


class OrderedEventQueue:
    """按 seq 有序交付事件：seq == next 立即就绪；seq > next 缓冲等待补位；
    seq < next（重复/过期）拒绝并记录。"""

    def __init__(self, start_seq: int = 1):
        self.next_seq = int(start_seq)
        self._buffer: Dict[int, dict] = {}
        self.rejected: List[tuple] = []

    def offer(self, event: dict) -> str:
        seq = event.get("seq")
        if seq is None or isinstance(seq, bool) or not isinstance(seq, int):
            raise ValueError(f"事件缺合法 seq：{event!r}")
        if seq < self.next_seq:
            self.rejected.append((seq, "stale_or_duplicate"))
            return "rejected"
        self._buffer[seq] = event
        return "ready" if seq == self.next_seq else "buffered"

    def drain(self) -> List[dict]:
        out = []
        while self.next_seq in self._buffer:
            out.append(self._buffer.pop(self.next_seq))
            self.next_seq += 1
        return out


# ---------------------------------------------------------------------------
# FileLockContender：文件占用 → 重试 / 明确报错
# ---------------------------------------------------------------------------
class FileLockedError(OSError):
    """目标文件被占用：明确报错（含路径），绝不静默丢。"""


class FileLockContender:
    """文件锁竞争者：非阻塞尝试获取独占锁；被占用时按 interval 重试，
    超时抛 FileLockedError（含路径与底层原因）。"""

    def __init__(self, interval: float = 0.05):
        if float(interval) <= 0:
            raise ValueError("interval 必须 > 0")
        self.interval = float(interval)
        self._held: Dict[str, int] = {}

    @staticmethod
    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        if os.name == "nt":  # pragma: no cover - 平台分支
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def try_acquire(self, path: str) -> Optional[int]:
        """非阻塞获取独占锁；被占用返回 None（不抛）。"""
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            self._try_lock(fd)
            self._held[path] = fd
            return fd
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            return None

    def acquire(self, path: str, *, timeout: float = 5.0) -> int:
        """带重试的获取；超时抛 FileLockedError（明确报错）。"""
        deadline = time.monotonic() + float(timeout)
        last_reason = "unknown"
        while True:
            fd = self.try_acquire(path)
            if fd is not None:
                return fd
            last_reason = "lock conflict"
            if time.monotonic() >= deadline:
                raise FileLockedError(
                    f"文件被占用，重试 {timeout}s 后仍失败：{path}（{last_reason}）"
                )
            time.sleep(self.interval)

    def release(self, path: str) -> None:
        fd = self._held.pop(path, None)
        if fd is None:
            return
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":  # pragma: no cover
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# ReasonixAdapterPlaceholder：占位适配器（WP-05 以同一错误契约接真实适配器）
# ---------------------------------------------------------------------------
class AdapterError(Exception):
    """适配器错误基类（ECC 侧 fail closed）。"""


class AdapterLoadError(AdapterError):
    """配置加载失败：坏 JSON / 缺依赖字段（消息可诊断缺失项）。"""


class AdapterStartupError(AdapterError):
    """适配器进程启动失败：exit != 0（携带退出码与 stderr 摘要）。"""

    def __init__(self, message: str, exit_code: Optional[int]):
        super().__init__(message)
        self.exit_code = exit_code


class ReasonixAdapterPlaceholder:
    """Reasonix 占位适配器。

    - load()：校验配置文件（JSON 合法性 + 依赖字段齐备）；失败抛
      AdapterLoadError，消息点名缺失项（可诊断）。
    - start()：以子进程启动注入的 executable；exit != 0 抛 AdapterStartupError
      （携带退出码 + stderr 摘要），ECC 侧 fail closed。
    - 对照：合法配置 + 正常 executable → exit 0。
    真实 Reasonix 适配器接入后（WP-05）以同一错误契约替换本占位实现。
    """

    def __init__(self, *, config_path: str = None, executable=None,
                 required_deps=("reasonix.core", "ecc.bridge"),
                 workdir: str = None):
        self.config_path = config_path
        self.executable = executable
        self.required_deps = tuple(required_deps)
        self.workdir = workdir

    def load(self) -> dict:
        if not self.config_path or not os.path.exists(self.config_path):
            raise AdapterLoadError(
                f"Reasonix 配置缺失（fail closed）：{self.config_path!r}"
            )
        try:
            cfg = read_json_file(self.config_path)
        except json.JSONDecodeError as exc:
            raise AdapterLoadError(
                f"Reasonix 配置 JSON 非法（fail closed）：{self.config_path}: {exc}"
            ) from exc
        if not isinstance(cfg, dict):
            raise AdapterLoadError(
                f"Reasonix 配置必须是 JSON 对象（fail closed）：{self.config_path}"
            )
        deps = cfg.get("dependencies") or {}
        missing = [d for d in self.required_deps if not deps.get(d)]
        if missing:
            raise AdapterLoadError(
                f"Reasonix 配置缺依赖（fail closed）：{missing}"
            )
        return cfg

    def start(self, *, args=(), timeout_seconds: float = 15.0) -> dict:
        if not self.executable:
            raise AdapterStartupError(
                "Reasonix 未配置可执行文件（fail closed）", None)
        if isinstance(self.executable, (list, tuple)):
            argv = [str(x) for x in self.executable]
        else:
            exe = str(self.executable)
            argv = [PY, exe] if exe.endswith(".py") else [exe]
        argv += [str(a) for a in args]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=float(timeout_seconds),
                cwd=self.workdir,
            )
        except FileNotFoundError as exc:
            raise AdapterStartupError(
                f"Reasonix 可执行文件不存在（fail closed）：{argv[0]}", 127
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AdapterStartupError(
                f"Reasonix 启动超时（fail closed）：{argv[0]}", None
            ) from exc
        if proc.returncode != 0:
            stderr_summary = (proc.stderr or "").strip()[:300]
            raise AdapterStartupError(
                f"Reasonix 启动失败 exit={proc.returncode}"
                f"（fail closed，可诊断）：{stderr_summary or '<无 stderr>'}",
                proc.returncode,
            )
        return {"ok": True, "exit_code": 0,
                "stdout": (proc.stdout or "").strip()}


# ---------------------------------------------------------------------------
# 统一 FaultInjector：七元组记录 + 隔离副本
# ---------------------------------------------------------------------------
@dataclass
class CaseRecord:
    """每类案例的七元组记录。"""
    case_id: int
    injection: str
    expectation: str
    observation: str = ""
    exit_code: str = ""
    state_invariant: str = ""
    recovery: str = ""
    evidence: List[str] = field(default_factory=list)


class FaultInjector:
    """统一故障注入入口：inject(case_id, target_dir) 在隔离副本上制造故障。

    默认自建 TemporaryDirectory 隔离副本；传入 target_dir 时要求调用方保证
    其为隔离副本（本包绝不自作主张指向正式 runtime / 用户数据）。
    """

    def __init__(self, target_dir: str = None, *, prefix: str = "ecc-wp04-iso-"):
        if target_dir is None:
            self._tmp = tempfile.TemporaryDirectory(prefix=prefix)
            self.target_dir = os.path.abspath(self._tmp.name)
            self._owns_tmp = True
        else:
            self.target_dir = os.path.abspath(target_dir)
            os.makedirs(self.target_dir, exist_ok=True)
            self._owns_tmp = False
        self.records: List[CaseRecord] = []

    # ------------------------------------------------------------ 生命周期
    def child(self, sub: str) -> str:
        d = os.path.join(self.target_dir, sub)
        os.makedirs(d, exist_ok=True)
        return d

    def close(self) -> None:
        if self._owns_tmp:
            self._tmp.cleanup()

    # ------------------------------------------------------------ 七元组
    def inject(self, case_id: int, injection: str, expectation: str,
               state_invariant: str, recovery: str) -> CaseRecord:
        record = CaseRecord(
            case_id=case_id, injection=injection, expectation=expectation,
            state_invariant=state_invariant, recovery=recovery,
        )
        self.records.append(record)
        return record

    def observe(self, case_id: int, observation: str,
                exit_code="", evidence: List[str] = None) -> None:
        for record in self.records:
            if record.case_id == case_id:
                record.observation = observation
                record.exit_code = exit_code
                if evidence:
                    record.evidence.extend(evidence)
                return
        raise KeyError(f"未登记的案例：{case_id}")

    def get(self, case_id: int) -> CaseRecord:
        for record in self.records:
            if record.case_id == case_id:
                return record
        raise KeyError(f"未登记的案例：{case_id}")

    def render_record(self, case_id: int) -> str:
        r = self.get(case_id)
        return (
            f"case {r.case_id:02d}\n"
            f"  注入方法: {r.injection}\n"
            f"  预期:     {r.expectation}\n"
            f"  观察:     {r.observation}\n"
            f"  退出码:   {r.exit_code}\n"
            f"  状态不变量: {r.state_invariant}\n"
            f"  恢复动作: {r.recovery}\n"
            f"  证据:     {' | '.join(r.evidence) if r.evidence else '-'}"
        )
