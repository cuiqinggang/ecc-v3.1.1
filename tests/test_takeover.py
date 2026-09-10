# -*- coding: utf-8 -*-
"""WP-02 接管场景测试：从最后 checkpoint 继续、不重复 side effect、时钟前跳/
后跳、旧 owner 恢复不重复执行（幂等键 + fencing 双重防护）。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lease import (  # noqa: E402
    FakeClock,
    FencingTokenMismatch,
    LeaseOwnershipLost,
    LeaseStillActive,
    LeaseStore,
)


class TakeoverTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="takeover-test-")
        self.lease_path = os.path.join(self.tmp, "lease.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, clock=None):
        return LeaseStore(self.lease_path, clock=clock or FakeClock())


class CheckpointContinuityTest(TakeoverTestBase):
    def test_takeover_continues_from_last_accepted_checkpoint(self):
        """接管后从最后已接受 checkpoint 继续。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", ttl_seconds=10.0)
        store.record_last_accepted_checkpoint(
            "worker-A", lease_a.lease_id, lease_a.fencing_token, "cp-42"
        )
        clock.advance(11.0)  # 心跳丢失，租约过期
        lease_b = store.takeover(
            "worker-B", 10.0, lease_id=lease_a.lease_id, takeover_key="t1"
        )
        # 新 owner 从 cp-42 继续
        self.assertEqual(lease_b.last_accepted_checkpoint, "cp-42")
        # B 推进 checkpoint：旧 token 无法写（fencing），新 token 正常
        with self.assertRaises(FencingTokenMismatch):
            store.record_last_accepted_checkpoint(
                "worker-B", lease_b.lease_id, lease_a.fencing_token, "cp-43"
            )
        store.record_last_accepted_checkpoint(
            "worker-B", lease_b.lease_id, lease_b.fencing_token, "cp-43"
        )
        self.assertEqual(store.current().last_accepted_checkpoint, "cp-43")

    def test_checkpoint_chain_through_multiple_takeovers(self):
        """多次接管链条：checkpoint 逐段推进不丢失。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        a = store.acquire("A", 10.0)
        store.record_last_accepted_checkpoint("A", a.lease_id, a.fencing_token, "cp-1")
        clock.advance(11.0)
        b = store.takeover("B", 10.0, lease_id=a.lease_id, takeover_key="t1")
        self.assertEqual(b.last_accepted_checkpoint, "cp-1")
        store.record_last_accepted_checkpoint("B", b.lease_id, b.fencing_token, "cp-2")
        clock.advance(11.0)
        c = store.takeover("C", 10.0, lease_id=b.lease_id, takeover_key="t2")
        self.assertEqual(c.last_accepted_checkpoint, "cp-2")


class SideEffectSafetyTest(TakeoverTestBase):
    def test_no_duplicate_side_effect_after_takeover(self):
        """旧 owner 的 side effect 提交前 fencing 校验拒绝，不重复执行。"""
        executed = []

        def guarded_side_effect(store, lease, label):
            store.write_guard(lease.fencing_token)  # 提交前校验
            executed.append(label)

        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        lease_b = store.takeover(
            "worker-B", 10.0, lease_id=lease_a.lease_id, takeover_key="t1"
        )
        # A 失联期间的写入被拒
        with self.assertRaises(FencingTokenMismatch):
            guarded_side_effect(store, lease_a, "A-write")
        self.assertEqual(executed, [])
        # B 正常提交一次
        guarded_side_effect(store, lease_b, "B-write")
        self.assertEqual(executed, ["B-write"])

    def test_old_owner_recovers_does_not_redo(self):
        """旧 owner 恢复后不重复执行：幂等键 + fencing 双重防护。"""
        done_keys = set()

        def execute(store, lease, key):
            if key in done_keys:
                return "skipped-idempotent"
            store.write_guard(lease.fencing_token)  # fencing 防护
            done_keys.add(key)
            return "executed"

        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        self.assertEqual(execute(store, lease_a, "job-1"), "executed")
        clock.advance(11.0)
        lease_b = store.takeover(
            "worker-B", 10.0, lease_id=lease_a.lease_id, takeover_key="t1"
        )
        # B 重放 job-1：幂等键跳过
        self.assertEqual(execute(store, lease_b, "job-1"), "skipped-idempotent")
        # A 恢复后尝试 job-2：fencing 拒绝
        with self.assertRaises(FencingTokenMismatch):
            execute(store, lease_a, "job-2")
        self.assertNotIn("job-2", done_keys)
        # B 执行 job-2 正常
        self.assertEqual(execute(store, lease_b, "job-2"), "executed")
        self.assertEqual(done_keys, {"job-1", "job-2"})


class ClockDriftTest(TakeoverTestBase):
    def test_clock_jump_forward_enables_takeover(self):
        """时钟前跳：越过 expires_at 后 takeover 成功。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 10.0)
        clock.advance(10.1)
        taken = store.takeover(
            "worker-B", 10.0, lease_id=lease.lease_id, takeover_key="t1"
        )
        self.assertEqual(taken.owner_id, "worker-B")
        self.assertEqual(taken.fencing_token, 2)

    def test_clock_jump_backward_does_not_revive(self):
        """时钟后跳：已接管的旧租约不复活，旧 owner renew 仍失败。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        store.takeover("worker-B", 10.0, lease_id=lease_a.lease_id,
                       takeover_key="t1")
        clock.set(1000.0)  # 回到最初
        with self.assertRaises(LeaseOwnershipLost):
            store.renew("worker-A", lease_a.lease_id)
        with self.assertRaises(FencingTokenMismatch):
            store.write_guard(lease_a.fencing_token)

    def test_clock_backward_before_expiry_blocks_takeover(self):
        """时钟后跳：未过期租约仍拒绝 takeover（绝对时间语义，不误判）。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 10.0)
        clock.advance(9.9)
        with self.assertRaises(LeaseStillActive):
            store.takeover("worker-B", 10.0, lease_id=lease.lease_id)
        clock.set(1000.5)  # 后跳，仍有效
        with self.assertRaises(LeaseStillActive):
            store.takeover("worker-B", 10.0, lease_id=lease.lease_id)


class RecoveryTest(TakeoverTestBase):
    def test_expired_owner_renew_rejected(self):
        """心跳丢失后旧 owner 恢复：renew 被拒（须走 takeover）。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        with self.assertRaises(LeaseOwnershipLost):
            store.renew("worker-A", lease.lease_id)
        # 旧 owner 重新 acquire 视为接管（token +1）
        again = store.acquire("worker-A", 10.0)
        self.assertEqual(again.fencing_token, 2)


if __name__ == "__main__":
    unittest.main()
