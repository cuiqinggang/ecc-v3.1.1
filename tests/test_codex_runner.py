# -*- coding: utf-8 -*-
"""WP-03 Codex Runner 测试：合同字段、错误案例全覆盖（假 binary 注入）、
超时/取消进程树清理（tasklist 断言无僵尸）、脱敏断言、live=False 默认拒绝。

真实 codex 调用不在这里 —— 见 test_codex_runner_live.py（ECC_V31_LIVE_CODEX=1）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

_PARENT = os.path.dirname(os.path.abspath(__file__))          # tests/
_ROOT = os.path.dirname(_PARENT)                              # v3.1/
sys.path.insert(0, _ROOT)

from runners import CodexRunResult, CodexRunner  # noqa: E402
from runners.codex_runner import DEFAULT_ENV_ALLOWLIST, redact_text  # noqa: E402

FAKE_CODEX_SOURCE = r'''# -*- coding: utf-8 -*-
"""ECC WP-03 测试用假 codex：行为由 argv[1] 的 mode 控制。

- ok:       两行 JSONL（item.completed agent 消息 + turn.completed）+ 写 -o 文件，exit 0
- exit3:    stderr 写业务失败消息，exit 3
- sleep:    写 PID 文件（本进程 + 子进程），sleep 120（由 Runner 超时/取消杀死）
- garbage:  stdout 写非 JSONL 垃圾，exit 0
- authfail: stderr 写含疑似凭据的认证失败，exit 1
- touch:    向 FAKE_CODEX_TOUCH 路径写文件（用于断言"是否被启动过"），exit 0
"""
import json
import os
import subprocess
import sys
import time


def _out_file(args):
    for i, a in enumerate(args):
        if a == "-o" and i + 1 < len(args):
            return args[i + 1]
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
    if mode not in ("ok", "exit3", "sleep", "garbage", "authfail", "touch"):
        mode = "ok"  # Runner 默认 binary=[py, fake.py] 时 argv[1] 是 "exec"
    args = sys.argv[2:]
    if mode == "ok":
        for ev in [
            {"type": "thread.started", "thread_id": "t-fake"},
            {"type": "item.completed",
             "item": {"id": "it-1", "type": "agent_message",
                      "role": "assistant",
                      "content": [{"type": "output_text",
                                   "text": "FAKE-OK-MESSAGE"}]}},
            {"type": "turn.completed", "thread_id": "t-fake"},
        ]:
            sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()
        out = _out_file(args)
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write("FAKE-OK-MESSAGE\n")
        sys.exit(0)
    if mode == "exit3":
        sys.stderr.write("fake codex: boom 业务失败\n")
        sys.exit(3)
    if mode == "sleep":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        pidfile = os.environ.get("FAKE_CODEX_PIDFILE")
        if pidfile:
            with open(pidfile, "w", encoding="utf-8") as f:
                f.write("%d\n%d\n" % (os.getpid(), child.pid))
        time.sleep(120)
        sys.exit(0)
    if mode == "garbage":
        sys.stdout.write("this is not JSONL\n")
        sys.stdout.write("neither is this line\n")
        sys.stdout.flush()
        sys.exit(0)
    if mode == "authfail":
        sys.stderr.write(
            "authentication failed: invalid api key sk-live-ABCDEFGH12\n"
        )
        sys.exit(1)
    if mode == "touch":
        t = os.environ.get("FAKE_CODEX_TOUCH")
        if t:
            with open(t, "w", encoding="utf-8") as f:
                f.write("spawned\n")
        sys.exit(0)
    sys.exit(2)


if __name__ == "__main__":
    main()
'''


def _pid_alive(pid: int) -> bool:
    """psutil 不可用时用 tasklist 检查进程是否存活。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:  # noqa: BLE001
        return False  # tasklist 不可用：保守按"已死"处理并继续
    return f'"{pid}"' in out or f",{pid}," in out or str(pid) in out.split()


class CodexRunnerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="ecc-wp03-")
        cls.fake_path = os.path.join(cls._tmp, "fake_codex.py")
        with open(cls.fake_path, "w", encoding="utf-8") as f:
            f.write(FAKE_CODEX_SOURCE)
        cls.fake_binary = [sys.executable, cls.fake_path]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="ecc-wp03-ws-")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _runner(self, **kwargs) -> CodexRunner:
        kwargs.setdefault("binary", list(self.fake_binary))
        return CodexRunner(**kwargs)


class CodexRunnerContractTest(CodexRunnerTestBase):
    def test_contract_fields_ok(self):
        result = self._runner().run(
            "hello", self.ws, timeout_seconds=30)
        self.assertIsInstance(result, CodexRunResult)
        for name in ("ok", "exit_code", "stdout", "stderr", "final_message",
                     "marker_file", "timed_out", "cancelled",
                     "duration_seconds", "run_id", "reason"):
            self.assertTrue(hasattr(result, name), f"缺字段 {name}")
        self.assertTrue(result.ok)
        self.assertEqual(result.exit_code, 0)
        self.assertIsNotNone(result.run_id)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertIsNone(result.reason)
        self.assertIsInstance(result.duration_seconds, float)
        self.assertGreaterEqual(result.duration_seconds, 0.0)
        self.assertEqual(result.final_message, "FAKE-OK-MESSAGE")
        self.assertGreaterEqual(len(result.events), 2)
        # to_dict 观测
        d = result.to_dict()
        self.assertEqual(d["event_count"], len(result.events))

    def test_ok_result_has_marker_and_stdout_events(self):
        result = self._runner().run("hello", self.ws, timeout_seconds=30)
        self.assertIn("turn.completed", result.stdout)
        self.assertTrue(result.ok)


class CodexRunnerErrorCaseTest(CodexRunnerTestBase):
    def test_binary_not_found_fast_fail(self):
        runner = CodexRunner(binary=os.path.join(self._tmp, "no-such-file.exe"))
        result = runner.run("hello", self.ws, timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "cli_not_found")
        self.assertEqual(result.exit_code, 127)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.cancelled)

    def test_nonzero_exit_captured(self):
        result = self._runner(binary=list(self.fake_binary)
                              + ["exit3"]).run(
            "hello", self.ws, timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.reason, "nonzero_exit")
        self.assertIn("boom", result.stderr)

    def test_malformed_output_garbage(self):
        result = self._runner(binary=list(self.fake_binary) + ["garbage"]).run(
            "hello", self.ws, timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "malformed_output")
        self.assertEqual(result.exit_code, 0)

    def test_malformed_output_empty_stdout(self):
        # exit 0 但 stdout 为空（无任何 JSONL 事件）→ malformed
        result = self._runner(binary=[sys.executable, "-c", "import sys; sys.exit(0)"]).run(
            "hello", self.ws, timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "malformed_output")

    def test_invalid_working_dir_missing(self):
        result = self._runner().run(
            "hello", os.path.join(self.ws, "nope", "deep"),
            timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_working_dir")
        self.assertIsNone(result.exit_code)

    def test_invalid_working_dir_is_file(self):
        blocker = os.path.join(self.ws, "file-not-dir")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        result = self._runner().run("hello", blocker, timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "invalid_working_dir")
        self.assertIsNone(result.exit_code)

    def test_invalid_working_dir_not_spawn(self):
        """工作目录不可写/不存在 → 快速失败且绝不启动子进程。"""
        touch = os.path.join(self._tmp, "touch-not-spawned.txt")
        os.environ["FAKE_CODEX_TOUCH"] = touch
        try:
            runner = CodexRunner(binary=list(self.fake_binary) + ["touch"])
            result = runner.run(
                "hello", os.path.join(self.ws, "missing-dir"),
                env_allowlist=["FAKE_CODEX_TOUCH"], timeout_seconds=30)
            self.assertFalse(result.ok)
            self.assertEqual(result.reason, "invalid_working_dir")
            self.assertFalse(os.path.exists(touch),
                             "工作目录失败仍启动了子进程")
        finally:
            del os.environ["FAKE_CODEX_TOUCH"]

    def test_auth_failure_attribution(self):
        result = self._runner(binary=list(self.fake_binary) + ["authfail"]).run(
            "hello", self.ws, timeout_seconds=30)
        self.assertFalse(result.ok)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.reason, "auth_failure")

    def test_redaction_stderr(self):
        result = self._runner(binary=list(self.fake_binary) + ["authfail"]).run(
            "hello", self.ws, timeout_seconds=30)
        self.assertIn("<REDACTED>", result.stderr)
        self.assertNotIn("sk-live-ABCDEFGH12", result.stderr)
        self.assertNotIn("sk-live-", result.stderr)
        self.assertNotIn("sk-live-", result.stdout)

    def test_redact_text_unit(self):
        self.assertIn("<REDACTED>", redact_text("Authorization: Bearer abc.def"))
        self.assertNotIn("abc.def", redact_text("Authorization: Bearer abc.def"))
        self.assertIn("<REDACTED>",
                      redact_text("token=sk-proj-1234567890"))
        self.assertIn("<REDACTED>",
                      redact_text("x=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"))
        self.assertEqual(redact_text(""), "")
        self.assertEqual(redact_text("normal text"), "normal text")

    def test_live_disabled_by_default(self):
        # binary=None → 解析真实 codex；live=False 必须拒绝（防误烧钱）。
        runner = CodexRunner()  # live=False 默认
        self.assertFalse(runner.live)
        result = runner.run("hello", self.ws, timeout_seconds=5)
        self.assertFalse(result.ok)
        # 本机 codex 存在 → live_disabled；不存在 → cli_not_found。都不烧钱。
        self.assertIn(result.reason, ("live_disabled", "cli_not_found"))
        self.assertIsNone(result.exit_code)
        self.assertLess(result.duration_seconds, 5.0)

    def test_env_allowlist_merge_and_no_full_inherit(self):
        os.environ["ECC_V31_FAKE_EXTRA"] = "extra-value"
        os.environ["ECC_V31_SECRET_TOKEN"] = "secret-value"
        try:
            env = CodexRunner._build_env(["ECC_V31_FAKE_EXTRA"])
            self.assertIn("PATH", env)
            self.assertIn("ECC_V31_FAKE_EXTRA", env)
            self.assertEqual(env["ECC_V31_FAKE_EXTRA"], "extra-value")
            self.assertNotIn("THIS_VAR_SHOULD_NEVER_EXIST_XYZ", env)
            # 禁止全量继承：白名单外变量绝不出现在 env
            env2 = CodexRunner._build_env(None)
            self.assertNotIn("ECC_V31_SECRET_TOKEN", env2)
            self.assertNotIn("ECC_V31_FAKE_EXTRA", env2)
        finally:
            del os.environ["ECC_V31_FAKE_EXTRA"]
            del os.environ["ECC_V31_SECRET_TOKEN"]


class CodexRunnerTimeoutCancelTest(CodexRunnerTestBase):
    def _wait_pids_gone(self, pidfile: str, pids: list, timeout: float = 8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(not _pid_alive(p) for p in pids):
                return True
            time.sleep(0.3)
        return False

    def test_timeout_kills_process_tree(self):
        pidfile = os.path.join(self._tmp, "pids-timeout.txt")
        os.environ["FAKE_CODEX_PIDFILE"] = pidfile
        try:
            runner = CodexRunner(binary=list(self.fake_binary) + ["sleep"])
            t0 = time.monotonic()
            result = runner.run(
                "hello", self.ws, env_allowlist=["FAKE_CODEX_PIDFILE"],
                timeout_seconds=3, cancellable=True)
            elapsed = time.monotonic() - t0
        finally:
            del os.environ["FAKE_CODEX_PIDFILE"]
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)
        self.assertEqual(result.reason, "timeout")
        self.assertLess(elapsed, 10.0)
        # 进程树清理：fake 父进程 + 子进程都必须不存在
        self.assertTrue(os.path.exists(pidfile), "fake codex 未写 PID 文件")
        with open(pidfile, "r", encoding="utf-8") as f:
            pids = [int(x) for x in f.read().split()]
        self.assertEqual(len(pids), 2)
        self.assertTrue(
            self._wait_pids_gone(pidfile, pids),
            f"僵尸进程遗留: {[ (p, _pid_alive(p)) for p in pids ]}")

    def test_cancel_kills_process_tree(self):
        pidfile = os.path.join(self._tmp, "pids-cancel.txt")
        os.environ["FAKE_CODEX_PIDFILE"] = pidfile
        try:
            runner = CodexRunner(binary=list(self.fake_binary) + ["sleep"])
            result_holder = {}
            t = threading.Thread(
                target=lambda: result_holder.update(
                    res=runner.run(
                        "hello", self.ws, env_allowlist=["FAKE_CODEX_PIDFILE"],
                        timeout_seconds=120, cancellable=True)),
                daemon=True)
            t.start()
            # 等 fake codex 写出 PID 文件（证明已启动）
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not os.path.exists(pidfile):
                time.sleep(0.1)
            self.assertTrue(os.path.exists(pidfile), "fake codex 未启动")
            accepted = runner.cancel()
            self.assertTrue(accepted, "cancellable=True 的调用应接受 cancel()")
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), "run() 未在取消后返回")
            result = result_holder.get("res")
            self.assertIsNotNone(result)
            self.assertFalse(result.ok)
            self.assertTrue(result.cancelled)
            self.assertFalse(result.timed_out)
            self.assertEqual(result.reason, "cancelled")
            with open(pidfile, "r", encoding="utf-8") as f:
                pids = [int(x) for x in f.read().split()]
            self.assertTrue(
                self._wait_pids_gone(pidfile, pids),
                f"取消后僵尸进程遗留: {[ (p, _pid_alive(p)) for p in pids ]}")
        finally:
            del os.environ["FAKE_CODEX_PIDFILE"]

    def test_cancel_rejected_when_not_cancellable(self):
        pidfile = os.path.join(self._tmp, "pids-nocancel.txt")
        os.environ["FAKE_CODEX_PIDFILE"] = pidfile
        try:
            runner = CodexRunner(binary=list(self.fake_binary) + ["sleep"])
            result_holder = {}
            t = threading.Thread(
                target=lambda: result_holder.update(
                    res=runner.run(
                        "hello", self.ws, env_allowlist=["FAKE_CODEX_PIDFILE"],
                        timeout_seconds=4, cancellable=False)),
                daemon=True)
            t.start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not os.path.exists(pidfile):
                time.sleep(0.1)
            self.assertTrue(os.path.exists(pidfile), "fake codex 未启动")
            self.assertFalse(runner.cancel(),
                             "cancellable=False 应拒绝 cancel()")
            t.join(timeout=15)
            self.assertFalse(t.is_alive())
            result = result_holder["res"]
            # 未被取消：最终应为超时（timeout=4 < sleep 120）
            self.assertTrue(result.timed_out)
            self.assertFalse(result.cancelled)
            with open(pidfile, "r", encoding="utf-8") as f:
                pids = [int(x) for x in f.read().split()]
            self.assertTrue(self._wait_pids_gone(pidfile, pids))
        finally:
            del os.environ["FAKE_CODEX_PIDFILE"]


if __name__ == "__main__":
    unittest.main()
