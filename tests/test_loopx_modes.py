# -*- coding: utf-8 -*-
"""WP-01 四模式测试：goal / scheduled / event / hybrid 的正常与错误案例。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loopx import FakeClock, InvalidEvent, LoopXEngine  # noqa: E402
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
    """按游标顺序每次吐一个工作包，耗尽返回 None。"""
    idx = {"i": 0}

    def provider(state):
        i = idx["i"]
        if i < len(items):
            idx["i"] += 1
            return items[i]
        return None

    return provider


def ok_worker(calls=None):
    def worker(package, state):
        if calls is not None:
            calls.append(package["object_id"])
        return {"status": "ok", "message": "done"}
    return worker


class LoopXModeTestBase(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.mkdtemp(prefix="loopx-modes-")

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


class GoalModeTest(LoopXModeTestBase):
    def test_goal_mode_processes_one_package_per_round(self):
        """goal 模式：每轮只处理一个工作包，耗尽后目标达成 CLOSED。"""
        self.write_contract()
        calls = []
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-1"), mk_pkg("obj-2"), mk_pkg("obj-3"),
            ]),
            worker=ok_worker(calls),
        )
        engine.open()
        r1 = engine.step()
        r2 = engine.step()
        r3 = engine.step()
        self.assertEqual([r["outcome"] for r in (r1, r2, r3)],
                         ["PROCESSED", "PROCESSED", "PROCESSED"])
        self.assertEqual(calls, ["obj-1", "obj-2", "obj-3"])
        state = engine.status()
        self.assertEqual(state["cursor"], 3)
        self.assertEqual(state["rounds"], 3)
        r4 = engine.step()  # 无工作 -> 达成 -> CLOSED
        self.assertEqual(r4["outcome"], "CLOSED")
        self.assertEqual(r4["reason"], "goal_met")
        self.assertEqual(engine.status()["phase"], "CLOSED")
        self.assertEqual(engine.status()["rounds"], 3)  # 达成不产生额外轮次

    def test_goal_mode_worker_failure_enters_repair(self):
        """goal 模式：工作包失败 -> REPAIR。"""
        self.write_contract(budget=dict(ctr.DEFAULT_BUDGET))
        engine = self.make_engine(
            work_provider=seq_provider([mk_pkg("obj-1")]),
            worker=lambda pkg, state: {"status": "failed", "message": "boom"},
        )
        engine.open()
        result = engine.step()
        self.assertEqual(result["outcome"], "REPAIR")
        self.assertEqual(engine.status()["phase"], "REPAIR")
        self.assertEqual(engine.status()["repairs"], 1)

    def test_goal_mode_missing_contract_refused(self):
        """合同缺失 -> FileNotFoundError。"""
        with self.assertRaises(FileNotFoundError):
            LoopXEngine(self.runtime, clock=FakeClock())

    def test_goal_mode_invalid_mode_rejected(self):
        """非法模式 -> ValueError。"""
        self.write_contract(mode="weekly")
        with self.assertRaises(ValueError):
            LoopXEngine(self.runtime, clock=FakeClock())

    def test_goal_mode_accepts_event_rejected(self):
        """goal 模式不接受事件 -> InvalidEvent。"""
        self.write_contract()
        engine = self.make_engine()
        engine.open()
        with self.assertRaises(InvalidEvent):
            engine.dispatch_event({"type": "msg"})

    def test_goal_mode_idempotent_skip_advances_cursor(self):
        """重复幂等键：第二个同键包 SKIPPED，游标仍前进。"""
        self.write_contract()
        calls = []
        engine = self.make_engine(
            work_provider=seq_provider([
                mk_pkg("obj-1", key="dup"),
                mk_pkg("obj-2", key="dup"),
                mk_pkg("obj-3", key="k3"),
            ]),
            worker=ok_worker(calls),
        )
        engine.open()
        results = [engine.step()["outcome"] for _ in range(3)]
        self.assertEqual(results, ["PROCESSED", "SKIPPED", "PROCESSED"])
        self.assertEqual(calls, ["obj-1", "obj-3"])  # dup 未重复执行
        self.assertEqual(engine.status()["cursor"], 3)


class ScheduledModeTest(LoopXModeTestBase):
    def _make_scheduled(self, interval=60.0):
        return self.write_contract(
            mode="scheduled",
            trigger={"interval_seconds": interval},
        )

    def test_scheduled_mode_clean_before_due(self):
        """scheduled：未到点返回 CLEAN，不产生轮次、不唤醒智能体。"""
        self.write_contract(
            mode="scheduled",
            trigger={"interval_seconds": 60.0, "initial_delay_seconds": 30.0},
        )
        calls = []
        engine = self.make_engine(
            work_provider=seq_provider([mk_pkg("obj-1")]),
            worker=ok_worker(calls),
        )
        engine.open()
        result = engine.step()
        self.assertEqual(result["outcome"], "CLEAN")
        self.assertEqual(engine.status()["rounds"], 0)
        self.assertEqual(calls, [])

    def test_scheduled_mode_runs_when_due(self):
        """scheduled：时钟推进到触发点后处理一个工作包，且触发点顺延。"""
        clock = FakeClock(1000.0)
        self._make_scheduled(interval=60.0)
        engine = self.make_engine(
            clock=clock, work_provider=seq_provider([mk_pkg("obj-1")])
        )
        engine.open()
        clock.advance(60.0)
        result = engine.step()
        self.assertEqual(result["outcome"], "PROCESSED")
        # 顺延后未到下一触发点 -> CLEAN
        self.assertEqual(engine.step()["outcome"], "CLEAN")
        clock.advance(60.0)
        self.assertEqual(engine.step()["outcome"], "CLOSED")  # 无工作 -> 达成

    def test_scheduled_mode_missing_interval_rejected(self):
        """scheduled 缺 interval_seconds -> ValueError。"""
        self.write_contract(mode="scheduled", trigger={})
        with self.assertRaises(ValueError):
            LoopXEngine(self.runtime, clock=FakeClock())


class EventModeTest(LoopXModeTestBase):
    def _make_event(self, types=("msg",)):
        return self.write_contract(
            mode="event", trigger={"event_types": list(types)}
        )

    def test_event_mode_dispatches_and_processes(self):
        """event：合法事件入队，每轮消费一个事件作为工作包。"""
        self._make_event()
        engine = self.make_engine(worker=ok_worker())
        engine.open()
        engine.dispatch_event({"type": "msg", "payload": {"n": 1}})
        result = engine.step()
        self.assertEqual(result["outcome"], "PROCESSED")
        state = engine.status()
        self.assertEqual(state["object_id"], "event:msg")
        self.assertEqual(state["cursor"], 1)

    def test_event_mode_no_event_clean_no_round(self):
        """event：无事件 CLEAN，不产生轮次。"""
        self._make_event()
        engine = self.make_engine()
        engine.open()
        result = engine.step()
        self.assertEqual(result["outcome"], "CLEAN")
        self.assertEqual(engine.status()["rounds"], 0)

    def test_event_mode_illegal_event_type_rejected(self):
        """event：类型不在白名单 -> InvalidEvent。"""
        self._make_event(types=("msg",))
        engine = self.make_engine()
        engine.open()
        with self.assertRaises(InvalidEvent):
            engine.dispatch_event({"type": "hack"})

    def test_event_mode_duplicate_event_skipped(self):
        """event：相同事件（同类型同内容）幂等跳过。"""
        self._make_event()
        engine = self.make_engine(worker=ok_worker())
        engine.open()
        engine.dispatch_event({"type": "msg", "payload": {"n": 1}})
        engine.dispatch_event({"type": "msg", "payload": {"n": 1}})
        self.assertEqual(engine.step()["outcome"], "PROCESSED")
        self.assertEqual(engine.step()["outcome"], "SKIPPED")
        self.assertEqual(engine.status()["cursor"], 2)


class HybridModeTest(LoopXModeTestBase):
    def _make_hybrid(self, types=("wake",)):
        return self.write_contract(
            mode="hybrid", trigger={"event_types": list(types)}
        )

    def test_hybrid_mode_event_triggers_goal(self):
        """hybrid：事件触发 goal 执行（工作包来自 provider）。"""
        self._make_hybrid()
        engine = self.make_engine(
            work_provider=seq_provider([mk_pkg("obj-1"), mk_pkg("obj-2")]),
        )
        engine.open()
        # 无事件 -> CLEAN
        self.assertEqual(engine.step()["outcome"], "CLEAN")
        engine.dispatch_event({"type": "wake"})
        result = engine.step()
        self.assertEqual(result["outcome"], "PROCESSED")
        self.assertEqual(engine.status()["cursor"], 1)

    def test_hybrid_mode_event_without_work_clean(self):
        """hybrid：事件触发检查但 provider 无工作 -> CLEAN（事件已消费）。"""
        self._make_hybrid()
        engine = self.make_engine(work_provider=lambda state: None)
        engine.open()
        engine.dispatch_event({"type": "wake"})
        result = engine.step()
        self.assertEqual(result["outcome"], "CLEAN")
        self.assertEqual(engine.status()["pending_events"], [])

    def test_hybrid_mode_illegal_event_rejected(self):
        """hybrid：非法事件类型 -> InvalidEvent。"""
        self._make_hybrid()
        engine = self.make_engine()
        engine.open()
        with self.assertRaises(InvalidEvent):
            engine.dispatch_event({"type": "hack"})


if __name__ == "__main__":
    unittest.main()
