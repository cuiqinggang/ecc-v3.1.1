# -*- coding: utf-8 -*-
"""WP-05-2 合同测试：Reasonix 影子适配器（v3.1/adapter/reasonix）。

覆盖（mock 集成，全部真跑子进程，不写 AppData）：
1. SKILL.md 解析：frontmatter 字段齐全、name 合法、runAs 合法、正文覆盖运行时主题。
2. ecc31_smoke.py 在隔离测试目录真跑（--test-root）：
   全绿 -> ECC_ACCEPTED；注入 1 个失败 -> ECC_PARTIAL；全部失败 -> ECC_BLOCKED；
   test-root 不存在 -> ECC_REJECTED + AdapterStartupError 诊断。
3. SKILL.md 缺 frontmatter / 坏 YAML -> AdapterLoadError（模拟加载器解析）。
4. ecc31_smoke.ps1 用 powershell -NoProfile -File 真跑（5.1），
   exit code 语义与 python 一致（三个场景）。
5. 绝对路径扫描：SKILL.md / contract.json / scripts / references 中不允许出现
   带盘符路径字面量（代码注释行除外）。
6. contract.json schema：输入/输出字段、四态 enum、退出码、错误契约。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml  # 环境已有 pyyaml（v3.1 依赖）

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # v3.1/
_ADAPTER = os.path.join(_ROOT, "adapter", "reasonix")
_SKILL_MD = os.path.join(_ADAPTER, "SKILL.md")
_SMOKE_PY = os.path.join(_ADAPTER, "scripts", "ecc31_smoke.py")
_SMOKE_PS1 = os.path.join(_ADAPTER, "scripts", "ecc31_smoke.ps1")
_CONTRACT = os.path.join(_ADAPTER, "contract.json")
_ROLLBACK = os.path.join(_ADAPTER, "references", "rollback-plan.md")

ECC_STATES = ("ECC_ACCEPTED", "ECC_PARTIAL", "ECC_BLOCKED", "ECC_REJECTED")


# ---------------------------------------------------------------------------
# 模拟加载器（与 Reasonix 技能加载机制对齐的最小解析；真实 loader 由正式接入时
# 的 Reasonix 侧承担，本测试只验证 SKILL.md 文件本身的合法性）
# ---------------------------------------------------------------------------
class AdapterError(Exception):
    """适配器错误基类（ECC 侧 fail closed）。"""


class AdapterLoadError(AdapterError):
    """技能文件加载失败：缺 frontmatter / 坏 YAML / 字段缺失（消息可诊断）。"""


def load_skill(path: str) -> dict:
    """解析 SKILL.md 的 frontmatter，失败抛 AdapterLoadError。"""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        raise AdapterLoadError(f"SKILL.md 不可读（fail closed）：{path}") from exc
    if not text.startswith("---"):
        raise AdapterLoadError("SKILL.md 缺 frontmatter（fail closed）")
    matched = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not matched:
        raise AdapterLoadError("SKILL.md frontmatter 无闭合分隔符（fail closed）")
    try:
        fm = yaml.safe_load(matched.group(1))
    except yaml.YAMLError as exc:
        raise AdapterLoadError(
            f"SKILL.md frontmatter YAML 非法（fail closed）：{exc}") from exc
    if not isinstance(fm, dict):
        raise AdapterLoadError("SKILL.md frontmatter 必须是映射（fail closed）")
    for field in ("name", "description", "runAs"):
        if not fm.get(field):
            raise AdapterLoadError(
                f"SKILL.md 缺 frontmatter 字段（fail closed）：{field}")
    if fm["runAs"] not in ("inline", "subagent"):
        raise AdapterLoadError(
            f"SKILL.md runAs 非法（fail closed）：{fm['runAs']!r}")
    return fm


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


_GREEN_TESTS = '''# -*- coding: utf-8 -*-
import unittest


class GreenOne(unittest.TestCase):
    def test_add(self):
        self.assertEqual(1 + 1, 2)

    def test_in(self):
        self.assertIn("b", "abc")


class GreenTwo(unittest.TestCase):
    def test_true(self):
        self.assertTrue(True)
'''  # 3 个全绿测试

_RED_TEST = '''
import unittest


class RedOne(unittest.TestCase):
    def test_boom(self):
        self.assertEqual(1, 2)
'''  # 1 个失败测试

_RED_TESTS = '''
import unittest


class RedOne(unittest.TestCase):
    def test_boom_a(self):
        self.assertEqual(1, 2)


class RedTwo(unittest.TestCase):
    def test_boom_b(self):
        raise RuntimeError("boom")
'''  # 2 个全部失败测试（1 failure + 1 error）


def _make_isolated_tests(specs) -> str:
    """在临时目录创建 tests/ 并写入给定 {文件名: 内容}，返回 tests 目录路径。

    调用方须用 addCleanup(shutil.rmtree, os.path.dirname(tests_dir)) 回收。
    """
    tmp = tempfile.mkdtemp(prefix="ecc-adapter-iso-")
    tests_dir = os.path.join(tmp, "tests")
    os.makedirs(tests_dir)
    for name, text in specs.items():
        _write(os.path.join(tests_dir, name), text)
    return tests_dir


def _claim_isolated(testcase, tests_dir) -> None:
    """把隔离目录挂到测试用例清理链上。"""
    testcase.addCleanup(shutil.rmtree, os.path.dirname(tests_dir),
                        ignore_errors=True)


def _run_smoke(test_root=None, timeout=90):
    argv = [sys.executable, _SMOKE_PY]
    if test_root:
        argv += ["--test-root", test_root]
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)


def _parse_smoke_json(proc):
    last = proc.stdout.strip().splitlines()[-1]
    return json.loads(last)


class ReasonixAdapterSkillTests(unittest.TestCase):
    """SKILL.md 解析与加载失败场景。"""

    def test_skill_frontmatter_complete_and_legal(self):
        fm = load_skill(_SKILL_MD)
        self.assertEqual(fm["name"], "ecc-v3.1-production")
        self.assertRegex(fm["name"], r"^[a-z0-9][a-z0-9._-]*$")
        self.assertEqual(fm["runAs"], "inline")
        desc = fm["description"]
        self.assertTrue(0 < len(desc) <= 120,
                        f"description 长度 {len(desc)} 超出 120 字")
        with open(_SKILL_MD, encoding="utf-8") as f:
            body = f.read()
        for keyword in ("loopx", "goal", "hybrid", "租约", "熔断", "故障",
                        "ECC_ACCEPTED", "ECC_PARTIAL", "ECC_BLOCKED",
                        "ECC_REJECTED", "tests_run", "AdapterStartupError",
                        "AdapterLoadError"):
            self.assertIn(keyword, body)

    def test_skill_missing_frontmatter_raises_adapter_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "SKILL.md")
            _write(bad, "# 没有 frontmatter\n\n正文\n")
            with self.assertRaises(AdapterLoadError):
                load_skill(bad)

    def test_skill_bad_yaml_raises_adapter_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "SKILL.md")
            _write(bad, "---\nname: [未闭合\nrunAs: inline\n---\n正文\n")
            with self.assertRaises(AdapterLoadError):
                load_skill(bad)

    def test_skill_missing_required_field_raises_adapter_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "SKILL.md")
            _write(bad, "---\nname: x\nrunAs: inline\n---\n正文\n")
            with self.assertRaises(AdapterLoadError):
                load_skill(bad)


class ReasonixAdapterSmokeTests(unittest.TestCase):
    """ecc31_smoke.py 四态真跑（隔离测试目录，--test-root）。"""

    def test_smoke_all_green_accepted(self):
        tests_dir = _make_isolated_tests({"test_green.py": _GREEN_TESTS})
        _claim_isolated(self, tests_dir)
        proc = _run_smoke(tests_dir)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        payload = _parse_smoke_json(proc)
        self.assertEqual(payload["status"], "ECC_ACCEPTED")
        self.assertEqual(payload["tests_run"], 3)
        self.assertEqual(payload["failures"], 0)
        self.assertGreaterEqual(payload["duration"], 0.0)

    def test_smoke_one_failure_partial(self):
        tests_dir = _make_isolated_tests({
            "test_green.py": _GREEN_TESTS,
            "test_red.py": _RED_TEST,
        })
        _claim_isolated(self, tests_dir)
        proc = _run_smoke(tests_dir)
        self.assertEqual(proc.returncode, 1, proc.stderr[-400:])
        payload = _parse_smoke_json(proc)
        self.assertEqual(payload["status"], "ECC_PARTIAL")
        self.assertEqual(payload["tests_run"], 4)
        self.assertEqual(payload["failures"], 1)

    def test_smoke_all_failures_blocked(self):
        tests_dir = _make_isolated_tests({"test_red.py": _RED_TESTS})
        _claim_isolated(self, tests_dir)
        proc = _run_smoke(tests_dir)
        self.assertEqual(proc.returncode, 2, proc.stderr[-400:])
        payload = _parse_smoke_json(proc)
        self.assertEqual(payload["status"], "ECC_BLOCKED")
        self.assertEqual(payload["tests_run"], 2)
        self.assertEqual(payload["failures"], 2)

    def test_smoke_missing_test_root_rejected(self):
        proc = _run_smoke(os.path.join(tempfile.gettempdir(),
                                       "ecc-no-such-test-root"))
        self.assertEqual(proc.returncode, 3, proc.stderr[-400:])
        payload = _parse_smoke_json(proc)
        self.assertEqual(payload["status"], "ECC_REJECTED")
        self.assertEqual(payload["tests_run"], 0)
        self.assertIn("AdapterStartupError", proc.stderr)

    def test_smoke_default_root_is_v31_tests(self):
        # 只验证定位逻辑与默认目录存在，不跑 34 秒全量（由外部验证步骤真跑）。
        sys.path.insert(0, os.path.dirname(_SMOKE_PY))
        import ecc31_smoke as smoke_mod  # noqa: E402
        self.assertEqual(
            os.path.realpath(smoke_mod.locate_v31_root()),
            os.path.realpath(_ROOT))
        self.assertTrue(os.path.isdir(
            os.path.join(smoke_mod.locate_v31_root(), "tests")))
        self.assertEqual(smoke_mod.classify(5, 0), "ECC_ACCEPTED")
        self.assertEqual(smoke_mod.classify(4, 2), "ECC_PARTIAL")
        self.assertEqual(smoke_mod.classify(3, 3), "ECC_BLOCKED")
        self.assertEqual(smoke_mod.classify(3, 5), "ECC_BLOCKED")
        self.assertEqual(smoke_mod.classify(0, 0), "ECC_REJECTED")
        self.assertEqual(smoke_mod.classify(0, 3), "ECC_REJECTED")


class ReasonixAdapterPowerShellTests(unittest.TestCase):
    """ecc31_smoke.ps1 用 powershell（5.1）-NoProfile -File 真跑。"""

    def _run_ps1(self, args=(), timeout=120):
        if not shutil.which("powershell"):
            self.skipTest("powershell (5.1) 不可用")
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", _SMOKE_PS1] + list(args)
        return subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)

    def test_powershell_exit_codes_match_python_semantics(self):
        tests_dir = _make_isolated_tests({"test_green.py": _GREEN_TESTS})
        _claim_isolated(self, tests_dir)
        # 场景 1：全绿 -> exit 0 / ECC_ACCEPTED
        proc = self._run_ps1(["-TestRoot", tests_dir])
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        payload = _parse_smoke_json(proc)
        self.assertEqual(payload["status"], "ECC_ACCEPTED")
        self.assertEqual(payload["tests_run"], 3)
        # 场景 2：注入 1 失败 -> exit 1 / ECC_PARTIAL
        _write(os.path.join(tests_dir, "test_red.py"), _RED_TEST)
        proc = self._run_ps1(["-TestRoot", tests_dir])
        self.assertEqual(proc.returncode, 1, proc.stderr[-400:])
        self.assertEqual(_parse_smoke_json(proc)["status"], "ECC_PARTIAL")
        # 场景 3：test-root 不存在 -> exit 3 / ECC_REJECTED
        proc = self._run_ps1(
            ["-TestRoot", os.path.join(tests_dir, "no-such-dir")])
        self.assertEqual(proc.returncode, 3, proc.stderr[-400:])
        self.assertEqual(_parse_smoke_json(proc)["status"], "ECC_REJECTED")


class ReasonixAdapterPathAndContractTests(unittest.TestCase):
    """绝对路径扫描与 contract.json schema。"""

    def test_no_drive_letter_path_literals(self):
        pattern = re.compile(r"(?i)[a-z]:[\\/]")
        scanned = (_SKILL_MD, _CONTRACT, _SMOKE_PY, _SMOKE_PS1, _ROLLBACK)
        for path in scanned:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if pattern.search(line) and not line.strip().startswith("#"):
                        self.fail(
                            f"{os.path.relpath(path, _ROOT)}:{lineno} 出现带盘符"
                            f"绝对路径字面量（注释行除外）：{line.strip()!r}")

    def test_contract_schema(self):
        with open(_CONTRACT, encoding="utf-8") as f:
            contract = json.load(f)
        self.assertEqual(contract["skill"]["name"], "ecc-v3.1-production")
        self.assertEqual(contract["skill"]["runAs"], "inline")
        self.assertEqual(contract["adapter"], "ecc-v3.1-production")
        out = contract["output"]
        for key in ("status", "tests_run", "failures", "skipped", "duration"):
            self.assertIn(key, out["properties"])
            self.assertIn(key, out["required"])
        self.assertEqual(out["properties"]["status"]["enum"], list(ECC_STATES))
        self.assertEqual(contract["exit_codes"], {
            "ECC_ACCEPTED": 0, "ECC_PARTIAL": 1,
            "ECC_BLOCKED": 2, "ECC_REJECTED": 3})
        for key in ("AdapterStartupError", "AdapterLoadError"):
            self.assertIn(key, contract["errors"])
        self.assertEqual(contract["timeouts"]["test_suite_seconds"], 300)
        for entry in ("python", "powershell"):
            path = os.path.join(_ROOT, contract["entrypoints"][entry])
            self.assertTrue(os.path.isfile(path), path)


if __name__ == "__main__":
    unittest.main()
