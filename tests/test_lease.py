# -*- coding: utf-8 -*-
"""WP-02 租约测试：acquire/renew/release/expire/takeover、fencing、损坏 fail
closed、fake clock 确定性、双 worker 跨进程唯一 owner、重复接管幂等。"""
import json
import multiprocessing
import os
import shutil
import sys
import tempfile
import time
import unittest

_PARENT = os.path.dirname(os.path.abspath(__file__))          # tests/
_ROOT = os.path.dirname(_PARENT)                              # v3.1/
sys.path.insert(0, _ROOT)

# spawn 子进程重新导入本模块时需要 tests/ 与 v3.1/ 在 PYTHONPATH 中
_existing = os.environ.get("PYTHONPATH", "")
if _ROOT not in _existing.split(os.pathsep) or _PARENT not in _existing.split(os.pathsep):
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in (_ROOT, _PARENT, _existing) if p
    )

from lease import (  # noqa: E402
    FakeClock,
    FencingTokenMismatch,
    HeartbeatLoop,
    LeaseAlreadyHeld,
    LeaseCorruptedError,
    LeaseOwnershipLost,
    LeaseStillActive,
    LeaseStore,
)


def _proc_acquire(lease_path, owner, ttl, queue):
    """子进程：尝试 acquire，结果回传（ok/err）。"""
    from lease import LeaseStore
    store = LeaseStore(lease_path)
    try:
        lease = store.acquire(owner, ttl)
        queue.put(("ok", owner, lease.lease_id, lease.fencing_token))
    except Exception as exc:  # noqa: BLE001 - 跨进程回传异常类型
        queue.put(("err", owner, type(exc).__name__))


def _proc_acquire_release_guard(lease_path, owner, guard_token, queue):
    """子进程：acquire -> release -> write_guard(旧 token)。"""
    from lease import LeaseStore
    store = LeaseStore(lease_path)
    try:
        lease = store.acquire(owner, 5.0)
        queue.put(("acquired", owner, lease.lease_id, lease.fencing_token))
        token = lease.fencing_token if guard_token is None else guard_token
        store.release(owner, lease.lease_id)
        queue.put(("released", owner))
        try:
            store.write_guard(token)
            queue.put(("guard", "ok"))
        except FencingTokenMismatch:
            queue.put(("guard", "rejected"))
    except Exception as exc:  # noqa: BLE001
        queue.put(("err", owner, type(exc).__name__))


class LeaseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lease-test-")
        self.lease_path = os.path.join(self.tmp, "lease.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_store(self, clock=None, **kwargs):
        return LeaseStore(self.lease_path, clock=clock or FakeClock(), **kwargs)


class LeaseBasicTest(LeaseTestBase):
    def test_acquire_creates_lease_with_fields(self):
        """acquire 后字段完整：owner/lease_id/acquired_at/expires_at/renewed_at/
        fencing_token。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 30.0)
        self.assertEqual(lease.owner_id, "worker-A")
        self.assertEqual(lease.fencing_token, 1)
        self.assertEqual(lease.acquired_at, 1000.0)
        self.assertEqual(lease.expires_at, 1030.0)
        self.assertEqual(lease.renewed_at, 1000.0)
        self.assertEqual(lease.state, "active")
        self.assertEqual(lease.last_accepted_checkpoint, None)

    def test_acquire_second_owner_rejected(self):
        """有效租约下第二个 acquire -> LeaseAlreadyHeld。"""
        store = self.make_store()
        store.acquire("worker-A", 30.0)
        with self.assertRaises(LeaseAlreadyHeld):
            store.acquire("worker-B", 30.0)

    def test_renew_updates_renewed_at_and_expires(self):
        """renew 延长 expires_at 并推进 renewed_at，token 不变。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 30.0)
        clock.advance(10.0)
        renewed = store.renew("worker-A", lease.lease_id)
        self.assertEqual(renewed.renewed_at, 1010.0)
        self.assertEqual(renewed.expires_at, 1040.0)
        self.assertEqual(renewed.fencing_token, 1)  # 续租不改 token

    def test_renew_wrong_owner_or_lease_rejected(self):
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        with self.assertRaises(LeaseOwnershipLost):
            store.renew("worker-B", lease.lease_id)
        with self.assertRaises(LeaseOwnershipLost):
            store.renew("worker-A", "other-lease")

    def test_renew_expired_rejected(self):
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        with self.assertRaises(LeaseOwnershipLost):
            store.renew("worker-A", lease.lease_id)

    def test_release_marks_released_and_frees(self):
        """release 后 state=released，他人可 acquire（token +1）。"""
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        store.release("worker-A", lease.lease_id)
        self.assertEqual(store.current().state, "released")
        next_lease = store.acquire("worker-B", 30.0)
        self.assertEqual(next_lease.fencing_token, 2)

    def test_release_wrong_owner_rejected(self):
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        with self.assertRaises(LeaseOwnershipLost):
            store.release("worker-B", lease.lease_id)

    def test_expire_then_takeover(self):
        """心跳丢失：expire 后 takeover 成功，token +1。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 30.0)
        store.expire(lease.lease_id)
        taken = store.takeover(
            "worker-B", 30.0, lease_id=lease.lease_id, takeover_key="k1"
        )
        self.assertEqual(taken.owner_id, "worker-B")
        self.assertEqual(taken.fencing_token, 2)
        self.assertNotEqual(taken.lease_id, lease.lease_id)
        self.assertEqual(
            taken.takeover,
            {
                "from_lease_id": lease.lease_id,
                "from_owner": "worker-A",
                "key": "k1",
                "at": 1000.0,
            },
        )

    def test_takeover_still_active_rejected(self):
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        with self.assertRaises(LeaseStillActive):
            store.takeover("worker-B", 30.0, lease_id=lease.lease_id)

    def test_acquire_after_expiry_takes_over(self):
        """租约自然过期后 acquire 视为接管（token +1，继承 checkpoint）。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 10.0)
        store.record_last_accepted_checkpoint(
            "worker-A", lease.lease_id, lease.fencing_token, "cp-7"
        )
        clock.advance(11.0)
        next_lease = store.acquire("worker-B", 30.0)
        self.assertEqual(next_lease.fencing_token, 2)
        self.assertEqual(next_lease.last_accepted_checkpoint, "cp-7")


class FencingTest(LeaseTestBase):
    def test_write_guard_accepts_current_token(self):
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        store.write_guard(lease.fencing_token)  # 不抛

    def test_fencing_rejects_old_owner_after_release(self):
        """旧 owner 失效后写入必拒。"""
        store = self.make_store()
        lease_a = store.acquire("worker-A", 30.0)
        store.release("worker-A", lease_a.lease_id)
        lease_b = store.acquire("worker-B", 30.0)
        self.assertEqual(lease_b.fencing_token, 2)
        with self.assertRaises(FencingTokenMismatch):
            store.write_guard(lease_a.fencing_token)
        store.write_guard(lease_b.fencing_token)  # 新 owner 正常

    def test_fencing_rejects_old_owner_after_takeover(self):
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        store.takeover("worker-B", 30.0, lease_id=lease_a.lease_id,
                       takeover_key="k1")
        with self.assertRaises(FencingTokenMismatch):
            store.write_guard(lease_a.fencing_token)

    def test_fencing_rejects_when_no_lease(self):
        store = self.make_store()
        with self.assertRaises(FencingTokenMismatch):
            store.write_guard(1)

    def test_checkpoint_record_guards_fencing(self):
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        with self.assertRaises(FencingTokenMismatch):
            store.record_last_accepted_checkpoint(
                "worker-A", lease.lease_id, 999, "cp-x"
            )
        store.record_last_accepted_checkpoint(
            "worker-A", lease.lease_id, lease.fencing_token, "cp-1"
        )
        self.assertEqual(store.current().last_accepted_checkpoint, "cp-1")


class CorruptedLeaseTest(LeaseTestBase):
    def _write_raw(self, text):
        with open(self.lease_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def test_corrupted_json_fail_closed(self):
        """损坏 JSON：所有操作抛 LeaseCorruptedError，不猜测 owner。"""
        self._write_raw("{ not json !!!")
        store = self.make_store()
        ops = (
            lambda: store.acquire("A", 10.0),
            lambda: store.renew("A", "x"),
            lambda: store.release("A", "x"),
            lambda: store.expire("x"),
            lambda: store.takeover("A", 10.0),
            lambda: store.write_guard(1),
            lambda: store.record_last_accepted_checkpoint("A", "x", 1, "cp"),
            lambda: store.current(),
        )
        for op in ops:
            with self.assertRaises(LeaseCorruptedError):
                op()

    def test_missing_field_fail_closed(self):
        self._write_raw(json.dumps({"owner_id": "A", "state": "active"}))
        store = self.make_store()
        with self.assertRaises(LeaseCorruptedError):
            store.current()
        with self.assertRaises(LeaseCorruptedError):
            store.acquire("B", 10.0)

    def test_invalid_token_fail_closed(self):
        self._write_raw(json.dumps({
            "schema": "ecc-v3.1-lease", "lease_id": "l", "owner_id": "A",
            "acquired_at": 1.0, "expires_at": 2.0, "renewed_at": 1.0,
            "fencing_token": "one", "ttl_seconds": 10.0, "state": "active",
            "last_accepted_checkpoint": None, "takeover": None,
        }))
        store = self.make_store()
        with self.assertRaises(LeaseCorruptedError):
            store.current()

    def test_empty_file_fail_closed(self):
        self._write_raw("")
        store = self.make_store()
        with self.assertRaises(LeaseCorruptedError):
            store.current()


class DeterminismTest(LeaseTestBase):
    def test_fake_clock_deterministic_sequence(self):
        """相同操作序列 + 相同 fake clock -> 完全相同的租约状态。"""
        runs = []
        for _ in range(2):
            tmp = tempfile.mkdtemp(prefix="lease-det-")
            self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
            clock = FakeClock(1000.0)
            store = LeaseStore(os.path.join(tmp, "lease.json"), clock=clock)
            lease = store.acquire("w", 30.0)
            clock.advance(10.0)
            store.renew("w", lease.lease_id)
            clock.advance(40.0)
            taken = store.takeover("w2", 30.0, lease_id=lease.lease_id,
                                   takeover_key="k1")
            runs.append((
                lease.lease_id, lease.fencing_token, lease.expires_at,
                taken.lease_id, taken.fencing_token, taken.acquired_at,
                taken.expires_at, taken.last_accepted_checkpoint,
            ))
        self.assertEqual(runs[0], runs[1])

    def test_clock_jump_backward_does_not_revive_old_lease(self):
        """时钟前跳导致接管后，再后跳也不复活旧租约（lease_id 已变）。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        clock.advance(20.0)
        store.takeover("worker-B", 30.0, lease_id=lease_a.lease_id,
                       takeover_key="k1")
        clock.set(1005.0)  # 后跳
        with self.assertRaises(LeaseOwnershipLost):
            store.renew("worker-A", lease_a.lease_id)

    def test_clock_jump_backward_before_takeover_still_active(self):
        """时钟后跳后未过期租约仍拒绝 takeover（绝对时间语义）。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 10.0)
        clock.advance(5.0)
        with self.assertRaises(LeaseStillActive):
            store.takeover("worker-B", 30.0, lease_id=lease.lease_id)
        clock.set(1002.0)  # 后跳，仍有效
        with self.assertRaises(LeaseStillActive):
            store.takeover("worker-B", 30.0, lease_id=lease.lease_id)


class IdempotentTakeoverTest(LeaseTestBase):
    def test_duplicate_takeover_idempotent(self):
        """重复接管幂等：同一 takeover 只生效一次（token 不再递增）。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        taken = store.takeover("worker-B", 30.0, lease_id=lease_a.lease_id,
                               takeover_key="k1")
        again = store.takeover("worker-B", 30.0, lease_id=lease_a.lease_id,
                               takeover_key="k1")
        self.assertEqual(again.lease_id, taken.lease_id)
        self.assertEqual(again.fencing_token, taken.fencing_token)
        self.assertEqual(store.current().fencing_token, 2)  # 只 +1 一次

    def test_takeover_same_owner_other_key_renews(self):
        """owner 相同的重复接管（不同 key）等价续租：token 不变。"""
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease_a = store.acquire("worker-A", 10.0)
        clock.advance(11.0)
        taken = store.takeover("worker-B", 30.0, lease_id=lease_a.lease_id,
                               takeover_key="k1")
        clock.advance(5.0)
        renewed = store.takeover("worker-B", 30.0, lease_id=lease_a.lease_id,
                                 takeover_key="k2")
        self.assertEqual(renewed.lease_id, taken.lease_id)
        self.assertEqual(renewed.fencing_token, 2)
        self.assertGreater(renewed.expires_at, taken.expires_at)


class HeartbeatTest(LeaseTestBase):
    def test_default_heartbeat_interval_is_moderate(self):
        """默认 heartbeat_interval=30.0（节制），非法值拒绝。"""
        store = self.make_store()
        self.assertEqual(store.heartbeat_interval, 30.0)
        with self.assertRaises(ValueError):
            LeaseStore(self.lease_path, clock=FakeClock(), heartbeat_interval=0)

    def test_heartbeat_equals_renew(self):
        clock = FakeClock(1000.0)
        store = self.make_store(clock=clock)
        lease = store.acquire("worker-A", 30.0)
        clock.advance(5.0)
        renewed = store.heartbeat("worker-A", lease.lease_id)
        self.assertEqual(renewed.renewed_at, 1005.0)

    def test_heartbeat_failure_counted(self):
        store = self.make_store()
        lease = store.acquire("worker-A", 30.0)
        store.release("worker-A", lease.lease_id)
        with self.assertRaises(LeaseOwnershipLost):
            store.heartbeat("worker-A", lease.lease_id)
        self.assertEqual(store.heartbeat_failures, 1)

    def test_heartbeat_real_clock_smoke(self):
        """真实时钟烟雾测试（<5 秒）：后台心跳推进 renewed_at。"""
        store = LeaseStore(self.lease_path, heartbeat_interval=0.05)
        lease = store.acquire("smoke", ttl_seconds=10.0)
        loop = HeartbeatLoop(store, "smoke", lease.lease_id, interval=0.05)
        start = time.monotonic()
        loop.start()
        try:
            while time.monotonic() - start < 0.5 and loop.beats < 2:
                time.sleep(0.02)
        finally:
            loop.stop()
        self.assertLess(time.monotonic() - start, 5.0)
        self.assertGreaterEqual(loop.beats, 1)
        current = store.current()
        self.assertGreater(current.renewed_at, lease.renewed_at)
        store.release("smoke", lease.lease_id)


class MultiProcessTest(LeaseTestBase):
    def test_two_processes_race_single_owner(self):
        """双 worker（multiprocessing 两进程）抢同一租约文件：唯一 owner。"""
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        p1 = ctx.Process(target=_proc_acquire,
                         args=(self.lease_path, "worker-A", 5.0, queue))
        p2 = ctx.Process(target=_proc_acquire,
                         args=(self.lease_path, "worker-B", 5.0, queue))
        p1.start()
        p2.start()
        results = [queue.get(timeout=30), queue.get(timeout=30)]
        p1.join(15)
        p2.join(15)
        oks = [r for r in results if r[0] == "ok"]
        errs = [r for r in results if r[0] == "err"]
        self.assertEqual(len(oks), 1, f"必须唯一 owner: {results}")
        self.assertEqual(len(errs), 1, f"另一进程必须被拒: {results}")
        self.assertEqual(errs[0][2], "LeaseAlreadyHeld")

    def test_fencing_across_processes(self):
        """跨进程 fencing：A 释放后 B 获得新 token，A 的旧 token 写入必拒。"""
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        proc = ctx.Process(
            target=_proc_acquire_release_guard,
            args=(self.lease_path, "worker-A", None, queue),
        )
        proc.start()
        # 主进程在 A release 后立刻 acquire 新租约（token 2）
        first = queue.get(timeout=30)
        self.assertEqual(first[0], "acquired")
        token_a = first[3]
        released = queue.get(timeout=30)
        self.assertEqual(released[0], "released")
        store = self.make_store()
        lease_b = store.acquire("worker-B", 5.0)
        self.assertEqual(lease_b.fencing_token, 2)
        guard = queue.get(timeout=30)
        self.assertEqual(guard, ("guard", "rejected"))  # 旧 token 被拒
        proc.join(15)
        store.write_guard(lease_b.fencing_token)  # 新 token 正常


if __name__ == "__main__":
    unittest.main()
