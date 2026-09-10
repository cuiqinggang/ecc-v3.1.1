# -*- coding: utf-8 -*-
"""WP-04 系统化故障注入与恢复测试（18 类 + 对照，全隔离）。

- 18 类故障案例每类一个测试（matrix.run_case），加矩阵对照与报告渲染测试，
  合计 20 项。
- 全隔离：每个案例在 FaultInjector 自建的 tempfile.TemporaryDirectory 副本上
  执行，绝不触碰正式 runtime / 用户数据 / 证据目录。
- 确定性：FakeClock + 假 binary / 假 executable 注入；崩溃用子进程
  os._exit(非零码) 精确模拟。
- 每个测试显式断言"状态不变量"（旧值保留 / 拒绝写入 / 唯一 owner /
  无重复 side effect / 备份完好等）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from faults import matrix  # noqa: E402
from faults.injector import FaultInjector  # noqa: E402


class FaultInjectionTestCase(unittest.TestCase):
    """18 类故障案例：每类 1 个测试，断言通过 + 退出码 + 状态不变量。"""

    def _assert_case(self, case_id: int, expect_exit=None):
        outcome = matrix.run_case(case_id)
        self.assertTrue(outcome.passed,
                        f"case {case_id} 失败: {outcome.detail} "
                        f"evidence={outcome.evidence}")
        self.assertTrue(outcome.invariant_holds,
                        f"case {case_id} 状态不变量不成立: "
                        f"{outcome.invariant}")
        if expect_exit is not None:
            self.assertEqual(outcome.exit_code, expect_exit)
        return outcome

    def test_case01_crash_before_state_write(self):
        outcome = self._assert_case(1, expect_exit="9")
        # 状态不变量：旧 STATE 保留、已接受事实不丢
        joined = " ".join(outcome.evidence)
        self.assertIn("cursor=1", joined)

    def test_case02_crash_between_state_and_log(self):
        outcome = self._assert_case(2, expect_exit="9")
        joined = " ".join(outcome.evidence)
        self.assertIn("LOG 缺第 2 轮行", joined)
        self.assertIn("日志补齐", joined)

    def test_case03_crash_before_side_effect_confirm(self):
        outcome = self._assert_case(3, expect_exit="9")
        joined = " ".join(outcome.evidence)
        # 状态不变量：恢复后 side effect 不重复执行（幂等键）
        self.assertIn("未重复", joined)

    def test_case04_checkpoint_tamper(self):
        outcome = self._assert_case(4)
        joined = " ".join(outcome.evidence)
        # 状态不变量：fail closed，不返回部分数据
        self.assertIn("fail closed", joined)
        self.assertIn("哈希校验检测", joined)

    def test_case05_missing_manifest(self):
        outcome = self._assert_case(5)
        joined = " ".join(outcome.evidence)
        # 状态不变量：明确报缺什么
        self.assertIn("LOOP-CONTRACT.json", joined)
        self.assertIn("loop_id", joined)

    def test_case06_missing_ready(self):
        outcome = self._assert_case(6)
        joined = " ".join(outcome.evidence)
        # 状态不变量：未 READY 对象不处理、跳过被记录
        self.assertIn("不处理", joined)
        self.assertIn("已记录", joined)

    def test_case07_truncated_or_illegal_state(self):
        outcome = self._assert_case(7)
        joined = " ".join(outcome.evidence)
        # 状态不变量：fail closed 明确报错、恢复后旧值可加载
        self.assertIn("JSONDecodeError", joined)
        self.assertIn("非法 phase", joined)
        self.assertIn("从备份恢复", joined)

    def test_case08_half_written_log_tail(self):
        outcome = self._assert_case(8)
        joined = " ".join(outcome.evidence)
        # 状态不变量：好行完整、坏行丢弃并记录
        self.assertIn("坏行丢弃并记录", joined)

    def test_case09_disk_full_simulation(self):
        outcome = self._assert_case(9)
        joined = " ".join(outcome.evidence)
        # 状态不变量：STATE 保持旧值且可解析（不损坏）
        self.assertIn("保持旧值", joined)
        self.assertIn("不损坏", joined)

    def test_case10_file_locked(self):
        outcome = self._assert_case(10)
        joined = " ".join(outcome.evidence)
        # 状态不变量：明确报错（含路径）、无静默丢
        self.assertIn("明确报错", joined)
        self.assertIn("静默", joined)

    def test_case11_runner_timeout_crash_malformed(self):
        outcome = self._assert_case(11)
        joined = " ".join(outcome.evidence)
        # 状态不变量：三种注入均精确归因
        self.assertIn("timeout", joined)
        self.assertIn("nonzero_exit", joined)
        self.assertIn("malformed_output", joined)

    def test_case12_duplicate_and_out_of_order_events(self):
        outcome = self._assert_case(12)
        joined = " ".join(outcome.evidence)
        # 状态不变量：幂等跳过重复、无重复 side effect、乱序按序交付
        self.assertIn("幂等跳过", joined)
        self.assertIn("执行次数=1", joined)
        self.assertIn("[1,2]", joined)

    def test_case13_two_workers_race_lease(self):
        outcome = self._assert_case(13)
        # 状态不变量：唯一 owner、一成功一失败
        self.assertEqual(outcome.exit_code, "0/1")
        self.assertIn("唯一", " ".join(outcome.evidence))

    def test_case14_stale_owner_fencing(self):
        outcome = self._assert_case(14)
        joined = " ".join(outcome.evidence)
        # 状态不变量：旧 token 写入必拒、重新接管后可写
        self.assertIn("fencing 拒绝", joined)
        self.assertIn("可写入", joined)

    def test_case15_clock_jump_forward_backward(self):
        outcome = self._assert_case(15)
        joined = " ".join(outcome.evidence)
        # 状态不变量：前跳可接管、后跳不复活
        self.assertIn("可接管", joined)
        self.assertIn("不复活", joined)

    def test_case16_reasonix_startup_and_load_failure(self):
        outcome = self._assert_case(16)
        joined = " ".join(outcome.evidence)
        # 状态不变量：exit!=0、加载失败可诊断（点名缺失依赖）
        self.assertIn("exit=7", joined)
        self.assertIn("reasonix.core", joined)

    def test_case17_config_readback_mismatch(self):
        outcome = self._assert_case(17)
        joined = " ".join(outcome.evidence)
        # 状态不变量：拒绝采用候选、保留旧配置
        self.assertIn("拒绝采用", joined)
        self.assertIn("旧配置保留", joined)

    def test_case18_rollback_interrupted_then_resume(self):
        outcome = self._assert_case(18)
        joined = " ".join(outcome.evidence)
        # 状态不变量：备份完好、最终全部恢复、不半恢复
        self.assertIn("不丢备份", joined)
        self.assertIn("不半恢复", joined)


class FaultMatrixReportTest(unittest.TestCase):
    """矩阵对照与报告渲染（对照 = 相同场景不注入故障 → 正常完成）。"""

    def test_matrix_all_controls_pass(self):
        outcomes = matrix.run_all()
        self.assertEqual(len(outcomes), 18)
        for outcome in outcomes:
            self.assertTrue(outcome.control_passed,
                            f"case {outcome.case_id} 对照失败: {outcome.detail}")
            self.assertTrue(outcome.passed,
                            f"case {outcome.case_id} 故障案例失败: "
                            f"{outcome.detail} {outcome.evidence}")

    def test_matrix_report_renders_seven_tuple(self):
        outcomes = matrix.run_all()
        report = matrix.render_report(outcomes)
        # 七元组全部出现在报告中
        for key in ("注入方法", "预期", "观察", "退出码", "状态不变量",
                    "恢复动作", "证据"):
            self.assertIn(key, report)
        self.assertIn("故障案例: 18/18 PASS", report)
        self.assertIn("对照案例: 18/18 PASS", report)
        # 报告写入隔离临时文件（不触碰正式 runtime）
        with tempfile.TemporaryDirectory(prefix="ecc-wp04-report-") as tmp:
            path = os.path.join(tmp, "fault-matrix.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
            self.assertGreater(os.path.getsize(path), 1000)

    def test_injector_isolated_copy_only(self):
        """FaultInjector 默认在自建隔离副本上工作并正确清理。"""
        inj = FaultInjector()
        target = inj.target_dir
        self.assertTrue(os.path.isdir(target))
        self.assertNotIn("production-runtime", target)
        inj.close()
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
