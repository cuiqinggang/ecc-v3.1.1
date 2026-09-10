# -*- coding: utf-8 -*-
"""WP-03 真实 codex 调用测试（成本有界：单个测试 = 1 次真实调用）。

默认跳过。真实运行时：
    ECC_V31_LIVE_CODEX=1 python -m unittest tests.test_codex_runner_live -q

- 工作目录与日志路径由环境变量 ECC_V31_LIVE_DIR / ECC_V31_LIVE_LOG 指定，
  未设置时使用系统临时目录（release 内不携带任何绝对路径）。
- prompt 指示 codex 只创建一个 marker.txt（内容恰好一行 ECCV31LIVE-OK）。
- 成功 / 认证失败均把完整脱敏日志写入 codex-live-smoke.log。
- 认证不可用 → 日志标记 live_auth_blocked，测试 skipTest（B 级分支阻塞，
  其余错误案例测试继续）。
"""
import datetime
import os
import shutil
import sys
import tempfile
import unittest

_PARENT = os.path.dirname(os.path.abspath(__file__))          # tests/
_ROOT = os.path.dirname(_PARENT)                              # v3.1/
sys.path.insert(0, _ROOT)

from runners import CodexRunner  # noqa: E402

LIVE_DIR = os.environ.get(
    "ECC_V31_LIVE_DIR",
    os.path.join(tempfile.gettempdir(), "ecc-v31-live-smoke"))
LOG_PATH = os.environ.get(
    "ECC_V31_LIVE_LOG",
    os.path.join(tempfile.gettempdir(), "ecc-v31-codex-live-smoke.log"))
MARKER_PATH = os.path.join(LIVE_DIR, "marker.txt")
MARKER_EXPECTED = "ECCV31LIVE-OK"
PROMPT = (
    "在当前目录创建 marker.txt，内容恰好为一行 ECCV31LIVE-OK"
    "（不要写其他文件，不要解释，直接创建后结束）"
)


def _log(msg: str) -> None:
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {msg}"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


@unittest.skipUnless(
    os.environ.get("ECC_V31_LIVE_CODEX") == "1",
    "真实 codex 调用需要 ECC_V31_LIVE_CODEX=1（成本有界，默认跳过）")
class CodexRunnerLiveTest(unittest.TestCase):
    """真实调用：1 次 codex exec，验证 marker 落盘。"""

    def test_live_marker_creation(self):
        if os.path.exists(MARKER_PATH):
            os.unlink(MARKER_PATH)
        os.makedirs(LIVE_DIR, exist_ok=True)

        _log("== live call start ==")
        _log(f"binary: 本机 codex (live=True)")
        _log(f"sandbox: workspace-write, working_dir: {LIVE_DIR}")
        _log(f"prompt: {PROMPT}")

        timeout = float(os.environ.get("ECC_V31_LIVE_TIMEOUT", "600"))
        _log(f"timeout_seconds: {timeout}")

        runner = CodexRunner(live=True, sandbox="workspace-write")
        result = runner.run(
            PROMPT, LIVE_DIR, timeout_seconds=timeout)

        # 完整脱敏日志落盘（result.stdout/stderr 已在 Runner 内脱敏）
        _log(f"result.to_dict(): {result.to_dict()!r}")
        _log(f"stdout (redacted): {result.stdout[:8000]}")
        _log(f"stderr (redacted): {result.stderr[:4000]}")

        if not result.ok and result.reason == "auth_failure":
            _log("live_auth_blocked: 认证不可用，WP-03 真实调用标记 B 级分支阻塞")
            self.skipTest(
                f"live_auth_blocked（认证不可用，见 {LOG_PATH}）: "
                f"{result.stderr[:200]}")
        if not result.ok and result.reason in ("cli_not_found",
                                               "live_disabled"):
            _log(f"live_blocked: {result.reason} {result.stderr[:200]}")
            self.skipTest(f"live_blocked: {result.reason}")

        self.assertTrue(result.ok,
                        f"真实调用失败: reason={result.reason} "
                        f"exit={result.exit_code} stderr={result.stderr[:300]}")
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(os.path.isfile(MARKER_PATH),
                        "marker.txt 未创建（codex 未按 prompt 执行）")
        with open(MARKER_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertEqual(
            content.strip(), MARKER_EXPECTED,
            f"marker.txt 内容不符: {content!r}")
        _log(f"live_ok: marker.txt == {content.strip()!r}, "
             f"duration={result.duration_seconds}s, "
             f"events={len(result.events)}, "
             f"final_message={result.final_message!r}")
        print(f"\n[LIVE-OK] duration={result.duration_seconds}s "
              f"marker={content.strip()!r} events={len(result.events)}")


if __name__ == "__main__":
    unittest.main()
