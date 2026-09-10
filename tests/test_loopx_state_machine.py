# -*- coding: utf-8 -*-
"""WP-01 状态机 / CLEAN / 五熔断 / 交接包 / ECC 四态回传 / schema 迁移测试。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loopx import (  # noqa: E402
    EccResultAlreadyRecorded,
    EccResultOutOfBand,
    EccRunIdMismatch,
    FakeClock,
    LoopBlockedError,
    LoopClosedError,
    LoopNotRunnable,
    LoopXEngine,
    handoff,
    states,
)
from loopx import contract as ctr  # noqa: E402


def mk_pkg(object_id, content_hash=None, key=None, complex_=False):
    return {
        "object_id": object_id,
        "content_hash": content_hash or f"h-{object_id}",
        "idempotency_key": key or f"key-{object_id}",
        "payload": {},
        "complex": complex_,
    }


def seq_provider(items):
    idx = {"i": 0}

    def provider(state):
        i = idx["i"]
        if i < len(items):
            idx["i"] += 1
            return items[i]
        return None

    return provider


def fail_worker(message="boom"):
    def worker(package, state):
        return {"status": "failed", "message": message}
    return worker


class LoopXEngineTestCase(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix="loopx-sm-")

    def tearDown(self):
        shutil.rmtree(self.runtime, ignore_errors=True)

    def write_contract(self, loop_id="l1", mode="goal", **overrides):
        contract = ctr.default_contract(loop_id, mode, **overrides)
        ctr.atomic_write_json(
            os.path.join(self.runtime, ctr.CONTRACT_NAME), contract
        )
        return contract

    def make_engine(self, clock=None, **kwargs):
        return LoopXEngine(self.runtime, clock=clock or FakeClock(), **kwargs)

    def engine_in_ecc_required(self, engine):
        engine.open()
        result = engine.step()
        assert result["outcome"] == "ECC_REQUIRED", result
        return result


class StateMachineTest(LoopXEngineTestCase):
    def test_all_legal_transitions(self):
        legal = [
            ("CLOSED", "WAITING"),
            ("WAITING", "REPAIR"),
            ("WAITING", "ECC_REQUIRED"),
            ("WAITING", "HUMAN_REQUIRED"),
            ("WAITING", "BLOCKED"),
            ("WAITING", "CLOSED"),
            ("REPAIR", "WAITING"),
            ("REPAIR", "ECC_REQUIRED"),
            ("REPAIR", "BLOCKED"),
            ("ECC_REQUIRED", "WAITING"),
            ("ECC_REQUIRED", "REPAIR"),
            ("ECC_REQUIRED", "HUMAN_REQUIRED"),
            ("ECC_REQUIRED", "BLOCKED"),
            ("ECC_REQUIRED", "CLOSED"),
            ("HUMAN_REQUIRED", "WAITING"),
            ("HUMAN_REQUIRED", "BLOCKED"),
            ("HUMAN_REQUIRED", "CLOSED"),
            ("BLOCKED", "WAITING"),
            ("BLOCKED", "CLOSED"),
        ]
        for cur, nxt in legal:
            self.assertEqual(states.transition(cur, nxt), nxt, f"{cur}->{nxt}")

    def test_illegal_transitions_rejected(self):
        illegal = [
            ("CLOSED", "REPAIR"),
            ("CLOSED", "BLOCKED"),
            ("CLOSED", "CLOSED"),
            ("WAITING", "WAITING"),
            ("REPAIR", "REPAIR"),
            ("HUMAN_REQUIRED", "ECC_REQUIRED"),
            ("HUMAN_REQUIRED", "REPAIR"),
            ("BLOCKED", "REPAIR"),
            ("BLOCKED", "ECC_REQUIRED"),
        ]
        for cur, nxt in illegal:
            with self.assertRaises(states.IllegalStateTransition, msg=f"{cur}->{nxt}"):
                states.transition(cur, nxt)

    def test_full_lifecycle_through_engine(self):
        """全迁移：CLOSED->WAITING->REPAIR->WAITING->ECC_REQUIRED->WAITING->CLOSED。"""
        self.write_contract()
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-bad"),
                mk_pkg("obj-ok"),
                mk_pkg("obj-complex", complex_=True),
                mk_pkg("obj-last"),
            ]),
            worker=lambda pkg, state: (
                {"status": "failed", "message": "x"}
                if pkg["object_id"] == "obj-bad"
                else {"status": "ok", "message": "ok"}
            ),
        )
        self.assertEqual(engine.status()["phase"], "CLOSED")
        engine.open()
        self.assertEqual(engine.status()["phase"], "WAITING")
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        self.assertEqual(engine.status()["phase"], "REPAIR")
        self.assertEqual(engine.step()["outcome"], "PROCESSED")
        self.assertEqual(engine.status()["phase"], "WAITING")
        ecc = engine.step()
        self.assertEqual(ecc["outcome"], "ECC_REQUIRED")
        self.assertEqual(engine.status()["phase"], "ECC_REQUIRED")
        engine.record_ecc_result({
            "run_id": ecc["run_id"],
            "result": "ECC_ACCEPTED",
            "new_cursor": 3,
            "evidence_path": None,
            "next_action": None,
        })
        self.assertEqual(engine.status()["phase"], "WAITING")
        self.assertEqual(engine.status()["cursor"], 3)
        self.assertEqual(engine.step()["outcome"], "PROCESSED")
        self.assertEqual(engine.step()["outcome"], "CLOSED")
        self.assertEqual(engine.status()["phase"], "CLOSED")

    def test_step_refused_when_not_runnable(self):
        """CLOSED / ECC_REQUIRED / HUMAN_REQUIRED / BLOCKED 时 step 拒绝。"""
        self.write_contract(budget=dict(ctr.DEFAULT_BUDGET, max_rounds=1))
        engine = self.make_engine(
            work_provider=seq_provider([mk_pkg("obj-1")]),
        )
        with self.assertRaises(LoopClosedError):
            engine.step()
        engine.open()
        engine.step()  # rounds=1
        second = engine.step()  # max_rounds 熔断触发轮
        self.assertEqual(second["outcome"], "BLOCKED")
        self.assertEqual(second["fuse"], "max_rounds")
        with self.assertRaises(LoopBlockedError):
            engine.step()  # 已 BLOCKED 后 step 拒绝
        engine.unblock()
        self.assertEqual(engine.status()["phase"], "WAITING")
        # 转 ECC_REQUIRED 后拒绝 step（独立 runtime 避免状态串扰）
        runtime2 = tempfile.mkdtemp(prefix="loopx-sm2-")
        self.addCleanup(shutil.rmtree, runtime2, ignore_errors=True)
        ctr.atomic_write_json(
            os.path.join(runtime2, ctr.CONTRACT_NAME),
            ctr.default_contract("l2", "goal"),
        )
        engine2 = LoopXEngine(
            runtime2, clock=FakeClock(),
            work_provider=seq_provider([mk_pkg("obj-c", complex_=True)]),
        )
        engine2.open()
        engine2.step()
        with self.assertRaises(LoopNotRunnable):
            engine2.step()

    def test_human_required_and_unblock_flow(self):
        self.write_contract()
        engine = self.make_engine(
            work_provider=seq_provider([mk_pkg("obj-c", complex_=True)])
        )
        engine.open()
        ecc = engine.step()
        engine.record_ecc_result({
            "run_id": ecc["run_id"], "result": "ECC_REJECTED",
            "new_cursor": None, "evidence_path": "审计拒绝",
            "next_action": "人工复核合同",
        })
        self.assertEqual(engine.status()["phase"], "HUMAN_REQUIRED")
        with self.assertRaises(LoopNotRunnable):
            engine.step()
        engine.resolve_human("已复核")
        self.assertEqual(engine.status()["phase"], "WAITING")


class CleanReturnTest(LoopXEngineTestCase):
    def test_clean_no_work_no_round_no_worker(self):
        """确定性预处理：无工作 CLEAN，不产生轮次、不唤醒智能体。"""
        self.write_contract()
        calls = []
        engine = self.make_engine(
            work_provider=lambda state: None,
            success_check=lambda state, contract: False,  # 目标未达成
            worker=lambda pkg, state: calls.append(pkg) or {"status": "ok"},
        )
        engine.open()
        for _ in range(3):
            result = engine.step()
            self.assertEqual(result["outcome"], "CLEAN")
        self.assertEqual(engine.status()["rounds"], 0)
        self.assertEqual(calls, [])
        events = [
            e["event"] for e in ctr.iter_run_log(
                os.path.join(self.runtime, ctr.LOG_NAME)
            )
        ]
        self.assertNotIn("round_start", events)  # 无轮次事件
        self.assertIn("step_clean", events)


class BudgetFuseTest(LoopXEngineTestCase):
    def _infinite_packages(self, factory):
        def provider(state):
            return factory(state)
        return provider

    def test_fuse_max_rounds(self):
        self.write_contract(budget=dict(ctr.DEFAULT_BUDGET, max_rounds=2))
        engine = self.make_engine(
            work_provider=self._infinite_packages(
                lambda s: mk_pkg(f"obj-{s['rounds']}")
            ),
        )
        engine.open()
        self.assertEqual(engine.step()["outcome"], "PROCESSED")
        self.assertEqual(engine.step()["outcome"], "PROCESSED")
        result = engine.step()
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["fuse"], "max_rounds")
        self.assertEqual(engine.status()["phase"], "BLOCKED")

    def test_fuse_max_repairs(self):
        self.write_contract(budget=dict(
            ctr.DEFAULT_BUDGET, max_repairs=2, stale_limit=100, same_error_limit=100,
        ))
        engine = self.make_engine(
            work_provider=self._infinite_packages(
                lambda s: mk_pkg(f"obj-{s['rounds']}", key=f"key-{s['rounds']}")
            ),
            worker=fail_worker(),
        )
        engine.open()
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        result = engine.step()
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["fuse"], "max_repairs")

    def test_fuse_max_runtime_seconds(self):
        clock = FakeClock(1000.0)
        self.write_contract(budget=dict(
            ctr.DEFAULT_BUDGET, max_runtime_seconds=100.0,
        ))
        engine = self.make_engine(
            clock=clock,
            work_provider=self._infinite_packages(
                lambda s: mk_pkg(f"obj-{s['rounds']}")
            ),
        )
        engine.open()
        clock.advance(150.0)
        result = engine.step()
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["fuse"], "max_runtime_seconds")
        self.assertEqual(engine.status()["rounds"], 0)  # 熔断不产生轮次

    def test_fuse_stale_limit(self):
        self.write_contract(budget=dict(
            ctr.DEFAULT_BUDGET, stale_limit=3, max_repairs=100, same_error_limit=100,
        ))
        engine = self.make_engine(
            work_provider=self._infinite_packages(
                lambda s: mk_pkg(
                    f"obj-{s['rounds']}",
                    content_hash=f"h-{s['rounds']}",  # 每次不同哈希（排除同错熔断）
                )
            ),
            worker=fail_worker(),
        )
        engine.open()
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        result = engine.step()
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["fuse"], "stale_limit")

    def test_fuse_same_error_limit(self):
        self.write_contract(budget=dict(
            ctr.DEFAULT_BUDGET, same_error_limit=2, max_repairs=100, stale_limit=100,
        ))
        engine = self.make_engine(
            work_provider=self._infinite_packages(
                lambda s: mk_pkg(
                    "obj-same",
                    content_hash="h-same",  # 同一内容哈希聚错
                    key=f"key-{s['rounds']}",
                )
            ),
            worker=fail_worker("同错"),
        )
        engine.open()
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        self.assertEqual(engine.step()["outcome"], "REPAIR")
        result = engine.step()
        self.assertEqual(result["outcome"], "BLOCKED")
        self.assertEqual(result["fuse"], "same_error_limit")
        # 错误按内容哈希聚合
        self.assertEqual(engine.status()["error_ledger"]["h-same"]["count"], 3)


class EccHandoffTest(LoopXEngineTestCase):
    def test_handoff_has_all_required_fields(self):
        """交接包字段完整性。"""
        self.write_contract(
            allowed_scope=["src/"], forbidden_scope=["secrets/"],
            rollback_scope=["work/"], stop_conditions=[{"fuse": "max_rounds"}],
        )
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-bad"),
                mk_pkg("obj-complex", complex_=True),
            ]),
            worker=fail_worker("前置失败"),
        )
        engine.open()
        engine.step()  # 产生 failure_evidence
        ecc = engine.step()
        h = ecc["handoff"]
        problems = handoff.validate_handoff(h)
        self.assertEqual(problems, [], f"交接包缺失字段: {problems}")
        for field in handoff.REQUIRED_HANDOFF_FIELDS:
            self.assertIn(field, h, f"交接包缺 {field}")
        self.assertEqual(h["loop_id"], "l1")
        self.assertTrue(h["run_id"].startswith("l1-run-"))
        self.assertEqual(h["cursor"], 0)
        self.assertEqual(h["processed_objects"], [])
        self.assertEqual(h["allowed_scope"], ["src/"])
        self.assertEqual(h["forbidden_scope"], ["secrets/"])
        self.assertEqual(h["rollback_scope"], ["work/"])
        self.assertEqual(h["work_packages"][0]["object_id"], "obj-complex")
        self.assertEqual(len(h["failure_evidence"]), 1)  # 已有失败证据
        self.assertEqual(len(h["repair_history"]), 1)  # 返修历史
        self.assertIn("max_rounds", h["budget"])
        self.assertEqual(len(h["stop_conditions"]), 1)
        self.assertEqual(
            h["required_return_fields"],
            ["result", "new_cursor", "evidence_path", "next_action"],
        )

    def test_handoff_carry_over_cursor_and_processed(self):
        """交接包含当前游标与已处理对象。"""
        self.write_contract()
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-1"),
                mk_pkg("obj-c", complex_=True),
            ]),
        )
        engine.open()
        engine.step()
        ecc = engine.step()
        self.assertEqual(ecc["handoff"]["cursor"], 1)
        self.assertEqual(ecc["handoff"]["processed_objects"], ["obj-1"])


class EccResultTest(LoopXEngineTestCase):
    def _ecc_payload(self, run_id, result, **extra):
        payload = {
            "run_id": run_id,
            "result": result,
            "new_cursor": None,
            "evidence_path": None,
            "next_action": None,
        }
        payload.update(extra)
        return payload

    def _engine_with_complex(self):
        self.write_contract()
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-c", complex_=True),
                mk_pkg("obj-last"),
            ]),
        )
        engine.open()
        return engine, engine.step()

    def test_ecc_accepted_back_to_waiting(self):
        engine, ecc = self._engine_with_complex()
        out = engine.record_ecc_result(
            self._ecc_payload(ecc["run_id"], "ECC_ACCEPTED", new_cursor=1)
        )
        self.assertEqual(out["outcome"], "ECC_ACCEPTED")
        self.assertEqual(engine.status()["phase"], "WAITING")
        self.assertEqual(engine.status()["cursor"], 1)
        # 委托的工作包幂等键已标记完成，可继续下一轮
        self.assertEqual(engine.step()["outcome"], "PROCESSED")

    def test_ecc_partial_enters_repair(self):
        engine, ecc = self._engine_with_complex()
        out = engine.record_ecc_result(
            self._ecc_payload(ecc["run_id"], "ECC_PARTIAL",
                              evidence_path="审计部分拒绝")
        )
        self.assertEqual(out["outcome"], "ECC_PARTIAL")
        self.assertEqual(engine.status()["phase"], "REPAIR")
        self.assertEqual(engine.status()["repairs"], 1)

    def test_ecc_blocked_trips_blocked(self):
        engine, ecc = self._engine_with_complex()
        out = engine.record_ecc_result(
            self._ecc_payload(ecc["run_id"], "ECC_BLOCKED",
                              evidence_path="外部依赖不可用")
        )
        self.assertEqual(out["outcome"], "ECC_BLOCKED")
        self.assertEqual(engine.status()["phase"], "BLOCKED")
        engine.unblock()
        self.assertEqual(engine.status()["phase"], "WAITING")

    def test_ecc_rejected_requires_human(self):
        engine, ecc = self._engine_with_complex()
        out = engine.record_ecc_result(
            self._ecc_payload(ecc["run_id"], "ECC_REJECTED",
                              next_action="人工复核")
        )
        self.assertEqual(out["outcome"], "ECC_REJECTED")
        self.assertEqual(engine.status()["phase"], "HUMAN_REQUIRED")

    def test_invalid_ecc_result_rejected(self):
        engine, ecc = self._engine_with_complex()
        with self.assertRaises(ValueError):
            engine.record_ecc_result(
                self._ecc_payload(ecc["run_id"], "ECC_MAYBE")
            )

    def test_ecc_run_id_mismatch_rejected(self):
        engine, _ecc = self._engine_with_complex()
        with self.assertRaises(EccRunIdMismatch):
            engine.record_ecc_result(
                self._ecc_payload("other-run-id", "ECC_ACCEPTED")
            )

    def test_ecc_duplicate_result_rejected(self):
        engine, ecc = self._engine_with_complex()
        engine.record_ecc_result(
            self._ecc_payload(ecc["run_id"], "ECC_ACCEPTED", new_cursor=1)
        )
        with self.assertRaises(EccResultAlreadyRecorded):
            engine.record_ecc_result(
                self._ecc_payload(ecc["run_id"], "ECC_ACCEPTED")
            )

    def test_ecc_result_out_of_band_rejected(self):
        self.write_contract()
        engine = self.make_engine(
            work_provider=seq_provider([mk_pkg("obj-1")])
        )
        engine.open()
        engine.step()
        with self.assertRaises(EccResultOutOfBand):
            engine.record_ecc_result(
                self._ecc_payload("x", "ECC_ACCEPTED")
            )

    def test_ecc_result_missing_return_field_rejected(self):
        engine, ecc = self._engine_with_complex()
        payload = {"run_id": ecc["run_id"], "result": "ECC_ACCEPTED"}
        with self.assertRaises(ValueError):
            engine.record_ecc_result(payload)

    def test_ecc_cursor_rollback_rejected(self):
        """ECC 回传光标回退拒绝。"""
        self.write_contract()
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-1"),
                mk_pkg("obj-c", complex_=True),
            ]),
        )
        engine.open()
        engine.step()  # cursor=1
        ecc = engine.step()
        with self.assertRaises(ValueError):
            engine.record_ecc_result(
                self._ecc_payload(ecc["run_id"], "ECC_ACCEPTED", new_cursor=0)
            )


class SchemaMigrationTest(LoopXEngineTestCase):
    def test_v1_contract_migrates_with_defaults(self):
        """向后兼容：v1 合同缺字段用默认值补齐并记录迁移事件。"""
        v1_contract = {
            "contract_version": 1,
            "loop_id": "legacy-loop",
            "mode": "goal",
            "goal": "处理遗留数据",
            "success_criteria": "全部处理完",
            "budget": {"max_rounds": 10, "max_repairs": 2},
            "allowed_scope": [],
            "forbidden_scope": [],
        }
        ctr.atomic_write_json(
            os.path.join(self.runtime, ctr.CONTRACT_NAME), v1_contract
        )
        engine = LoopXEngine(self.runtime, clock=FakeClock())
        migrated = ctr.load_json(os.path.join(self.runtime, ctr.CONTRACT_NAME))
        self.assertEqual(migrated["contract_version"], ctr.CONTRACT_VERSION)
        # 缺失字段默认值补齐
        self.assertEqual(migrated["budget"]["stale_limit"],
                         ctr.DEFAULT_BUDGET["stale_limit"])
        self.assertEqual(migrated["budget"]["same_error_limit"],
                         ctr.DEFAULT_BUDGET["same_error_limit"])
        self.assertEqual(migrated["rollback_scope"], [])
        self.assertEqual(migrated["initial_cursor"], 0)
        self.assertEqual(
            migrated["required_return_fields"],
            ["result", "new_cursor", "evidence_path", "next_action"],
        )
        # 迁移事件记录在 schema_migrations 表
        self.assertEqual(len(migrated["schema_migrations"]), 1)
        event = migrated["schema_migrations"][0]
        self.assertEqual(event["from_version"], 1)
        self.assertEqual(event["to_version"], 2)
        self.assertTrue(event["changes"])
        # RUN-LOG 记录迁移事件
        events = [
            e["event"] for e in ctr.iter_run_log(
                os.path.join(self.runtime, ctr.LOG_NAME)
            )
        ]
        self.assertIn("contract_migration", events)
        # 迁移后的旧合同可直接运行
        engine.open()
        self.assertEqual(engine.status()["phase"], "WAITING")

    def test_legacy_state_migrates(self):
        """向后兼容：旧 STATE（无 state_version）补齐字段。"""
        self.write_contract()
        legacy_state = {
            "phase": "WAITING", "cursor": 7, "rounds": 3,
        }
        ctr.atomic_write_json(
            os.path.join(self.runtime, ctr.STATE_NAME), legacy_state
        )
        engine = LoopXEngine(self.runtime, clock=FakeClock())
        state = engine.status()
        self.assertEqual(state["state_version"], ctr.STATE_VERSION)
        self.assertEqual(state["cursor"], 7)
        self.assertEqual(state["rounds"], 3)
        self.assertIn("error_ledger", state)
        self.assertIn("completed_keys", state)
        self.assertIn("state_migration", [
            e["event"] for e in ctr.iter_run_log(
                os.path.join(self.runtime, ctr.LOG_NAME)
            )
        ])

    def test_unsupported_contract_version_rejected(self):
        self.write_contract(contract_version=99)
        with self.assertRaises(ValueError):
            LoopXEngine(self.runtime, clock=FakeClock())


if __name__ == "__main__":
    unittest.main()
