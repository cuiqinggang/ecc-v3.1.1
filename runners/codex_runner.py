# -*- coding: utf-8 -*-
"""WP-03 真实 Codex CLI Runner。

以子进程方式调用本机 `codex exec`（参数数组，绝不经过 shell 拼接）：
- `--json` 收集 JSONL 事件流，`-o <marker>` 落盘最后消息；
- 超时与取消共用同一进程树清理路径（Windows：CREATE_NEW_PROCESS_GROUP +
  taskkill /T /F + proc.kill 兜底），保证无僵尸进程遗留；
- 结构化结果 `CodexRunResult`；JSONL 畸形（无法解析 / 缺少结束事件）→
  ok=False + reason=malformed_output；
- stderr/stdout 一律脱敏：疑似 key/token/secret/JWT 的值替换为 <REDACTED>；
- 环境白名单：只继承 DEFAULT_ENV_ALLOWLIST + 调用方显式追加，禁止全量继承；
- 真实调用成本有界：`live=True` 才允许解析并调用真实 codex CLI；库默认
  live=False 直接拒绝真实调用（防误烧钱）。显式注入 binary（测试假 codex）
  不受 live 开关限制，因为注入的本地脚本不产生真实调用成本。

合同输入（每次调用）：
    prompt / working_dir / env_allowlist / timeout_seconds / cancellable
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Union

__all__ = [
    "CodexRunner",
    "CodexRunResult",
    "DEFAULT_ENV_ALLOWLIST",
]

# ---------------------------------------------------------------------------
# 环境白名单：默认只继承安全变量；调用方可通过 env_allowlist 显式追加。
# 绝不全量继承 os.environ（防止凭据类变量泄漏给子进程）。
# ---------------------------------------------------------------------------
DEFAULT_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR",
    "TEMP", "TMP",
    "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "HOME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "ProgramFiles",
    "USERNAME", "OS", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "LANG", "LC_ALL", "CODEX_HOME",
)

# ---------------------------------------------------------------------------
# 脱敏：疑似凭据的值替换为 <REDACTED>
# ---------------------------------------------------------------------------
_REDACT = "<REDACTED>"

# 带「关键字 + 分隔符 + 值」结构的模式：保留关键字，值替换为 <REDACTED>。
_KEY_VALUE_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?\S+"),
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|secret|token|password|passwd|credential"
        r"|auth[_-]?token)\b\s*[:=]\s*\S+"
    ),
]
# 整串替换的模式（值本身就是可识别凭据形状）。
_WHOLE_VALUE_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}\b"),                 # OpenAI key
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}\b"),          # Slack token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),            # GitHub token
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),               # Google API key
]


def redact_text(text: str) -> str:
    """脱敏：疑似凭据的值替换为 <REDACTED>。无凭据时原样返回。"""
    if not text:
        return text
    out = text
    for pat in _KEY_VALUE_PATTERNS:
        out = pat.sub(lambda m: m.group(1) + _REDACT, out)
    for pat in _WHOLE_VALUE_PATTERNS:
        out = pat.sub(_REDACT, out)
    return out


# ---------------------------------------------------------------------------
# 认证失败归因：stderr 出现这些字样 → reason=auth_failure
# ---------------------------------------------------------------------------
_AUTH_PATTERN = re.compile(
    r"(?i)(not\s+authenticated|authentication\s+(failed|error|required)|"
    r"login\s+(failed|required|error)|unauthorized|please\s+log\s+in|"
    r"需要登录|登录失败|认证失败|未认证)"
)


def is_auth_failure(stderr: str) -> bool:
    return bool(_AUTH_PATTERN.search(stderr or ""))


# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
@dataclass
class CodexRunResult:
    ok: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    final_message: Optional[str]
    marker_file: Optional[str]
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    run_id: str
    reason: Optional[str] = None
    events: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "final_message": self.final_message,
            "marker_file": self.marker_file,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "duration_seconds": self.duration_seconds,
            "run_id": self.run_id,
            "reason": self.reason,
            "event_count": len(self.events),
        }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
BinarySpec = Union[str, List[str]]


class CodexRunner:
    """真实 Codex CLI Runner（WP-03）。

    参数：
    - binary：显式注入的可执行（str 或 argv 前缀 list，测试用假 codex）。
      为 None 时解析本机真实 codex（node + @openai/codex/bin/codex.js）。
    - live：True 才允许解析并调用真实 codex；默认 False（防误烧钱）。
      显式注入的 binary 不受 live 限制。
    - sandbox：传给 `codex exec -s` 的值（默认 workspace-write）。
    """

    name = "codex-runner-v3.1"

    def __init__(self, binary: Optional[BinarySpec] = None, *,
                 live: bool = False, sandbox: str = "workspace-write"):
        if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            raise ValueError(f"非法 sandbox: {sandbox!r}")
        self._binary = binary
        self.live = bool(live)
        self.sandbox = sandbox
        self._cancel_event: Optional[threading.Event] = None
        self._active_proc: Optional[subprocess.Popen] = None
        self._active_cancellable: bool = False
        self._active_lock = threading.Lock()

    # ------------------------------------------------------------ binary
    def _resolve_real_binary(self) -> Optional[List[str]]:
        """解析本机真实 codex：npm shim 的 codex.cmd 无法被 subprocess 直接
        执行（Windows .cmd 需 shell），因此定位真正的 node 入口脚本。"""
        codex_cmd = shutil.which("codex")
        if codex_cmd is None:
            return None
        npm_dir = os.path.dirname(codex_cmd)
        js = os.path.join(npm_dir, "node_modules", "@openai", "codex",
                          "bin", "codex.js")
        if not os.path.isfile(js):
            return None
        node = shutil.which("node")
        if node is None:
            return None
        return [node, js]

    def _resolve_argv(self) -> Optional[List[str]]:
        if self._binary is not None:
            if isinstance(self._binary, str):
                return [self._binary]
            return [str(x) for x in self._binary]
        if not self.live:
            return None  # live=False：拒绝真实 codex（防误烧钱）
        return self._resolve_real_binary()

    # ------------------------------------------------------------ 进程清理
    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """Windows：taskkill /T /F 树杀（连同子进程），proc.kill 兜底。
        超时与取消共用本路径。"""
        if proc.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:  # noqa: BLE001 —— 清理路径必须吞异常继续兜底
                pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 环境
    @staticmethod
    def _build_env(env_allowlist: Optional[List[str]]) -> dict:
        """白名单环境：默认安全变量 + 显式追加。禁止全量继承。"""
        names = list(DEFAULT_ENV_ALLOWLIST)
        for name in (env_allowlist or []):
            if name and name not in names:
                names.append(name)
        return {k: os.environ[k] for k in names if k in os.environ}

    # ------------------------------------------------------------ JSONL
    @staticmethod
    def _parse_events(stdout: str) -> List[dict]:
        events = []
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # 容忍个别非 JSONL 行（版本横幅等）
            if isinstance(obj, dict):
                events.append(obj)
        return events

    @staticmethod
    def _extract_texts(value) -> List[str]:
        """从事件对象里递归收集所有 text 字段。"""
        texts: List[str] = []
        if isinstance(value, dict):
            for key in ("text", "message"):
                if isinstance(value.get(key), str) and value[key].strip():
                    texts.append(value[key])
            for sub in value.values():
                texts.extend(CodexRunner._extract_texts(sub))
        elif isinstance(value, list):
            for sub in value:
                texts.extend(CodexRunner._extract_texts(sub))
        return texts

    @classmethod
    def _final_message_from_events(cls, events: List[dict]) -> Optional[str]:
        """最后一条 assistant/agent 消息文本；宽泛匹配 codex-cli 各版本字段。"""
        candidates: List[str] = []
        for ev in events:
            texts = cls._extract_texts(ev.get("item") if "item" in ev else ev)
            for t in texts:
                if t.strip() and t not in candidates:
                    candidates.append(t)
        return candidates[-1].strip() if candidates else None

    @staticmethod
    def _saw_terminal_event(events: List[dict]) -> bool:
        for ev in events:
            etype = str(ev.get("type", ""))
            if etype.endswith(".completed") or "end" in etype.lower() or \
                    etype.endswith("_done"):
                return True
        return False

    # ------------------------------------------------------------ run
    def run(self, prompt: str, working_dir: str, *,
            env_allowlist: Optional[List[str]] = None,
            timeout_seconds: float = 600.0,
            cancellable: bool = False) -> CodexRunResult:
        """执行一次 codex exec 调用。阻塞直到结束/超时/取消。"""
        start = time.monotonic()
        run_id = uuid.uuid4().hex[:16]
        timeout = float(timeout_seconds)
        if timeout <= 0:
            raise ValueError("timeout_seconds 必须 > 0")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt 必须是非空字符串")

        def fast_fail(reason: str, stderr: str, exit_code=None) -> CodexRunResult:
            return CodexRunResult(
                ok=False, exit_code=exit_code, stdout="", stderr=stderr,
                final_message=None, marker_file=None, timed_out=False,
                cancelled=False,
                duration_seconds=round(time.monotonic() - start, 3),
                run_id=run_id, reason=reason,
            )

        # 0. 工作目录校验：不存在/不可写 → 快速失败，绝不启动子进程。
        wd = os.path.abspath(working_dir)
        if not os.path.isdir(wd):
            return fast_fail("invalid_working_dir",
                             f"codex-runner: 工作目录不存在: {wd}\n")
        probe = None
        try:
            fd, probe = tempfile.mkstemp(prefix=".ecc-w-", dir=wd)
            os.close(fd)
        except OSError as exc:
            return fast_fail("invalid_working_dir",
                             f"codex-runner: 工作目录不可写: {wd} ({exc})\n")
        finally:
            if probe is not None:
                try:
                    os.unlink(probe)
                except OSError:
                    pass

        # 1. binary 解析（live 开关在这里把关）。
        argv_prefix = self._resolve_argv()
        if argv_prefix is None:
            if self._binary is None and not self.live:
                return fast_fail(
                    "live_disabled",
                    "codex-runner: live=False，拒绝调用真实 codex CLI"
                    "（防误烧钱；显式注入 binary 或 live=True 放行）\n")
            return fast_fail("cli_not_found",
                             "codex-runner: 无法解析 codex CLI（binary=None "
                             "且 PATH 中无 codex / 入口脚本缺失）\n")

        # 2. marker 文件：`-o` 落盘最后消息（放系统临时目录，不依赖 wd）。
        marker_fd, marker_file = tempfile.mkstemp(
            prefix="ecc-codex-marker-", suffix=".txt")
        os.close(marker_fd)

        # 3. 参数数组（绝不 shell 拼接）。
        argv = list(argv_prefix) + [
            "exec", "-C", wd, "-s", self.sandbox,
            "--skip-git-repo-check", "--json",
            "-o", marker_file, prompt,
        ]
        env = self._build_env(env_allowlist)

        popen_kwargs = dict(
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        if os.name == "nt":  # pragma: no cover - 平台分支
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        cancel_event = threading.Event()
        with self._active_lock:
            self._cancel_event = cancel_event
            self._active_cancellable = bool(cancellable)
            try:
                proc = subprocess.Popen(argv, **popen_kwargs)
            except FileNotFoundError as exc:
                self._cancel_event = None
                try:
                    os.unlink(marker_file)
                except OSError:
                    pass
                return fast_fail("cli_not_found",
                                 f"codex-runner: binary 不存在: {argv_prefix[0]} "
                                 f"({exc})\n", exit_code=127)
            except OSError as exc:
                self._cancel_event = None
                try:
                    os.unlink(marker_file)
                except OSError:
                    pass
                return fast_fail("spawn_failed",
                                 f"codex-runner: 启动失败: {exc}\n")
            self._active_proc = proc

        # 4. 事件驱动收集：communicate 切片循环，检查超时与取消。
        deadline = start + timeout
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        timed_out = False
        cancelled = False
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    self._kill_tree(proc)
                    break
                if cancel_event.is_set():
                    cancelled = True
                    self._kill_tree(proc)
                    break
                try:
                    out, err = proc.communicate(timeout=min(0.2, remaining))
                    stdout_buf.extend(out or b"")
                    stderr_buf.extend(err or b"")
                    break
                except subprocess.TimeoutExpired as exc:
                    stdout_buf.extend(exc.stdout or b"")
                    stderr_buf.extend(exc.stderr or b"")
        finally:
            with self._active_lock:
                self._active_proc = None
                self._cancel_event = None
                self._active_cancellable = False

        stdout_raw = bytes(stdout_buf).decode("utf-8", errors="replace")
        stderr_raw = bytes(stderr_buf).decode("utf-8", errors="replace")

        # 5. marker 文件读取（最后消息的落盘来源）。
        final_message = None
        try:
            with open(marker_file, "r", encoding="utf-8", errors="replace") as f:
                marker_text = f.read().strip()
            if marker_text:
                final_message = marker_text
        except OSError:
            pass
        try:
            os.unlink(marker_file)
        except OSError:
            pass

        events = self._parse_events(stdout_raw)
        from_events = self._final_message_from_events(events)
        if from_events:
            final_message = from_events
        final_message = redact_text(final_message) if final_message else None

        exit_code = proc.poll()
        duration = round(time.monotonic() - start, 3)

        # 6. 归因。
        if timed_out:
            return CodexRunResult(
                ok=False, exit_code=exit_code, stdout=redact_text(stdout_raw),
                stderr=redact_text(stderr_raw), final_message=final_message,
                marker_file=marker_file, timed_out=True, cancelled=False,
                duration_seconds=duration, run_id=run_id, reason="timeout",
                events=events)
        if cancelled:
            return CodexRunResult(
                ok=False, exit_code=exit_code, stdout=redact_text(stdout_raw),
                stderr=redact_text(stderr_raw), final_message=final_message,
                marker_file=marker_file, timed_out=False, cancelled=True,
                duration_seconds=duration, run_id=run_id, reason="cancelled",
                events=events)

        if exit_code != 0:
            if is_auth_failure(stderr_raw):
                reason = "auth_failure"
            else:
                reason = "nonzero_exit"
            return CodexRunResult(
                ok=False, exit_code=exit_code, stdout=redact_text(stdout_raw),
                stderr=redact_text(stderr_raw), final_message=final_message,
                marker_file=marker_file, timed_out=False, cancelled=False,
                duration_seconds=duration, run_id=run_id, reason=reason,
                events=events)

        # 7. 成功路径的畸形检测：JSONL 无法解析 / 缺少结束事件 → malformed。
        if not events:
            return CodexRunResult(
                ok=False, exit_code=exit_code, stdout=redact_text(stdout_raw),
                stderr=redact_text(stderr_raw), final_message=final_message,
                marker_file=marker_file, timed_out=False, cancelled=False,
                duration_seconds=duration, run_id=run_id,
                reason="malformed_output", events=events)
        if not self._saw_terminal_event(events) and not final_message:
            return CodexRunResult(
                ok=False, exit_code=exit_code, stdout=redact_text(stdout_raw),
                stderr=redact_text(stderr_raw), final_message=None,
                marker_file=marker_file, timed_out=False, cancelled=False,
                duration_seconds=duration, run_id=run_id,
                reason="malformed_output", events=events)

        return CodexRunResult(
            ok=True, exit_code=exit_code, stdout=redact_text(stdout_raw),
            stderr=redact_text(stderr_raw), final_message=final_message,
            marker_file=marker_file, timed_out=False, cancelled=False,
            duration_seconds=duration, run_id=run_id, reason=None,
            events=events)

    # ------------------------------------------------------------ cancel
    def cancel(self) -> bool:
        """取消当前活动调用（事件驱动，与超时共用清理路径）。
        活动调用标记 cancellable=False 时拒绝；无活动调用时返回 False。"""
        with self._active_lock:
            ev = self._cancel_event
            proc = self._active_proc
            if ev is None or proc is None or proc.poll() is not None:
                return False
            if not self._active_cancellable:
                return False
        ev.set()
        if proc.poll() is None:
            self._kill_tree(proc)
        return True
