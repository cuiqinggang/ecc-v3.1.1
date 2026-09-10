# -*- coding: utf-8 -*-
"""WP-02 租约（lease）/ fencing token / 失联接管。

设计要点：
- 单一权威租约文件 lease.json（路径由调用方指定）；原子更新 = 临时文件 + fsync +
  读回校验 + os.replace。
- 跨进程互斥 = 独立锁文件 <lease>.lock：Windows 用 msvcrt.locking，POSIX 用
  fcntl.flock；进程崩溃退出时 OS 自动释放锁（"锁随进程释放"）。
- 所有读-改-写都在持锁临界区内完成：加锁 -> 重读租约 -> 比较（CAS 等价）-> 修改
  -> 原子替换 -> 释放锁。跨进程下同一时刻只有一个操作成功生效。
- fencing_token 单调递增：每次建立新租约（acquire / takeover）时 +1。旧 owner 的
  side effect 提交前调用 write_guard(token)，token 与当前租约不一致即抛
  FencingTokenMismatch，保证旧 owner 失效后写入必拒。
- 租约文件损坏（无法解析 / 缺字段 / 字段类型错）时 fail closed：所有操作抛
  LeaseCorruptedError，绝不猜测 owner。
- FakeClock 可注入，测试确定性；heartbeat_interval 默认 30.0 秒（节制），
  心跳循环用 threading.Event.wait 阻塞等待，不做高频 CPU 轮询。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Lease",
    "LeaseStore",
    "LeaseError",
    "LeaseAlreadyHeld",
    "LeaseOwnershipLost",
    "LeaseStillActive",
    "FencingTokenMismatch",
    "LeaseCorruptedError",
    "FakeClock",
    "HeartbeatLoop",
]


class LeaseError(Exception):
    """租约域错误基类。"""


class LeaseAlreadyHeld(LeaseError):
    """租约有效且未过期（无论 owner 是谁），无法 acquire。"""


class LeaseOwnershipLost(LeaseError):
    """renew/release/checkpoint 时 owner_id 或 lease_id 已不匹配（已被接管或释放）。"""


class LeaseStillActive(LeaseError):
    """takeover 目标租约尚未过期。"""


class FencingTokenMismatch(LeaseError):
    """write_guard：调用者持有的 fencing token 已失效。"""


class LeaseCorruptedError(LeaseError):
    """租约文件损坏：fail closed，不猜测 owner。"""


def _utc_iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


@dataclass
class Lease:
    lease_id: str
    owner_id: str
    acquired_at: float
    expires_at: float
    renewed_at: float
    fencing_token: int
    ttl_seconds: float
    state: str  # "active" | "released"
    last_accepted_checkpoint: Optional[str] = None
    takeover: Optional[dict] = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at <= now

    def to_dict(self) -> dict:
        return {
            "schema": "ecc-v3.1-lease",
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "renewed_at": self.renewed_at,
            "fencing_token": self.fencing_token,
            "ttl_seconds": self.ttl_seconds,
            "state": self.state,
            "last_accepted_checkpoint": self.last_accepted_checkpoint,
            "takeover": self.takeover,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lease":
        _REQUIRED = (
            "lease_id", "owner_id", "acquired_at", "expires_at", "renewed_at",
            "fencing_token", "ttl_seconds", "state",
        )
        if not isinstance(data, dict):
            raise ValueError("租约记录必须是 JSON 对象")
        missing = [k for k in _REQUIRED if k not in data]
        if missing:
            raise ValueError(f"租约记录缺字段: {missing}")
        if data["state"] not in ("active", "released"):
            raise ValueError(f"非法 state: {data['state']!r}")
        token = data["fencing_token"]
        if isinstance(token, bool) or not isinstance(token, int) or token < 1:
            raise ValueError(f"非法 fencing_token: {token!r}")
        for k in ("acquired_at", "expires_at", "renewed_at", "ttl_seconds"):
            v = data[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"非法数值字段 {k}: {v!r}")
        for k in ("lease_id", "owner_id"):
            v = data[k]
            if not isinstance(v, str) or not v:
                raise ValueError(f"{k} 必须是非空字符串")
        takeover = data.get("takeover")
        if takeover is not None and not isinstance(takeover, dict):
            raise ValueError("takeover 字段必须是对象或 null")
        return cls(
            lease_id=data["lease_id"],
            owner_id=data["owner_id"],
            acquired_at=data["acquired_at"],
            expires_at=data["expires_at"],
            renewed_at=data["renewed_at"],
            fencing_token=token,
            ttl_seconds=data["ttl_seconds"],
            state=data["state"],
            last_accepted_checkpoint=data.get("last_accepted_checkpoint"),
            takeover=takeover,
        )


class FakeClock:
    """可注入时钟：now() / sleep() / advance() / set()。测试确定性。"""

    def __init__(self, start: float = 1000.0):
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += float(seconds)

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)

    def set(self, t: float) -> None:
        self._t = float(t)


class SystemClock:
    """真实时钟适配器。"""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _make_lease_id(token: int, now: float) -> str:
    """lease_id 由 token（全局单调递增）与时间戳派生，唯一且确定。"""
    return f"{token:06d}-{int(now * 1e6)}"


def _validate_owner(owner_id: str) -> None:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id 必须是非空字符串")


def _validate_ttl(ttl_seconds: float) -> float:
    ttl = float(ttl_seconds)
    if ttl <= 0:
        raise ValueError("ttl_seconds 必须 > 0")
    return ttl


class LeaseStore:
    """跨进程安全的租约存储。"""

    def __init__(self, lease_path: str, *, clock=None, heartbeat_interval: float = 30.0):
        self.lease_path = os.path.abspath(lease_path)
        self.lock_path = self.lease_path + ".lock"
        self._clock = clock if clock is not None else SystemClock()
        if float(heartbeat_interval) <= 0:
            raise ValueError("heartbeat_interval 必须 > 0")
        self.heartbeat_interval = float(heartbeat_interval)
        self._heartbeat_failures = 0

    # ------------------------------------------------------------------ 锁
    @contextmanager
    def _locked(self):
        """临界区：锁文件互斥；进程退出时 OS 自动释放锁。"""
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if os.name == "nt":  # pragma: no cover - 分支由平台决定
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if os.name == "nt":  # pragma: no cover
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -------------------------------------------------------------- 读写
    def _read_record(self) -> Optional[Lease]:
        if not os.path.exists(self.lease_path):
            return None
        try:
            with open(self.lease_path, "r", encoding="utf-8") as f:
                raw = f.read()
            data = json.loads(raw)
            return Lease.from_dict(data)
        except (LeaseError, ValueError, KeyError, TypeError, json.JSONDecodeError,
                OSError) as exc:
            raise LeaseCorruptedError(
                f"租约文件损坏（fail closed）：{self.lease_path}：{exc}"
            ) from exc

    def _write_record(self, lease: Lease) -> None:
        directory = os.path.dirname(self.lease_path)
        os.makedirs(directory, exist_ok=True)
        text = json.dumps(lease.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".lease-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            with open(tmp, "r", encoding="utf-8") as f:
                json.load(f)  # 读回校验
            os.replace(tmp, self.lease_path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # -------------------------------------------------------------- 操作
    def acquire(self, owner_id: str, ttl_seconds: float) -> Lease:
        """建立新租约。存在有效未过期租约时抛 LeaseAlreadyHeld；已过期/已释放则
        视为自然接管：token +1，继承 last_accepted_checkpoint。"""
        _validate_owner(owner_id)
        ttl = _validate_ttl(ttl_seconds)
        with self._locked():
            record = self._read_record()
            now = self._clock.now()
            if record is not None and record.state == "active" and not record.is_expired(now):
                raise LeaseAlreadyHeld(
                    f"租约已被 {record.owner_id!r} 持有（lease_id={record.lease_id}，"
                    f"expires_at={_utc_iso(record.expires_at)}）"
                )
            token = record.fencing_token + 1 if record is not None else 1
            lease = Lease(
                lease_id=_make_lease_id(token, now),
                owner_id=owner_id,
                acquired_at=now,
                expires_at=now + ttl,
                renewed_at=now,
                fencing_token=token,
                ttl_seconds=ttl,
                state="active",
                last_accepted_checkpoint=(
                    record.last_accepted_checkpoint if record is not None else None
                ),
                takeover=(
                    {
                        "from_lease_id": record.lease_id,
                        "from_owner": record.owner_id,
                        "key": None,
                        "at": now,
                    }
                    if record is not None else None
                ),
            )
            self._write_record(lease)
            return lease

    def renew(self, owner_id: str, lease_id: str) -> Lease:
        """心跳续租：owner_id + lease_id 必须与当前记录一致。"""
        with self._locked():
            record = self._read_record()
            now = self._clock.now()
            if (record is None or record.state != "active"
                    or record.owner_id != owner_id or record.lease_id != lease_id):
                raise LeaseOwnershipLost(
                    f"续租失败：owner={owner_id!r} lease={lease_id!r} 已不持有租约"
                )
            if record.is_expired(now):
                raise LeaseOwnershipLost("租约已过期，须 takeover 后继续")
            record.renewed_at = now
            record.expires_at = now + record.ttl_seconds
            self._write_record(record)
            return record

    def release(self, owner_id: str, lease_id: str) -> None:
        """主动释放：标记 released（保留记录供审计），token 不再变化。"""
        with self._locked():
            record = self._read_record()
            if (record is None or record.owner_id != owner_id
                    or record.lease_id != lease_id or record.state != "active"):
                raise LeaseOwnershipLost(
                    f"释放失败：owner={owner_id!r} lease={lease_id!r} 已不持有租约"
                )
            record.state = "released"
            record.expires_at = self._clock.now()
            self._write_record(record)

    def expire(self, lease_id: str) -> None:
        """主动过期（管理/测试用）：expires_at 立即置为当前时间。"""
        with self._locked():
            record = self._read_record()
            if record is None or record.lease_id != lease_id:
                raise LeaseOwnershipLost(f"expire 失败：lease_id={lease_id!r} 不存在")
            record.expires_at = self._clock.now()
            self._write_record(record)

    def takeover(self, owner_id: str, ttl_seconds: float, lease_id: str = None,
                 takeover_key: str = None) -> Lease:
        """失联接管：
        - 无租约 / 已释放：直接建立新租约；
        - 租约有效且 owner 是调用者：幂等返回当前租约（同一 takeover 只生效一次，
          token 不再递增；若 key 与已记录接管不同则视为续租）；
        - 租约有效且 owner 不同：抛 LeaseStillActive；
        - 租约过期：接管，token +1，继承 last_accepted_checkpoint。
        """
        _validate_owner(owner_id)
        ttl = _validate_ttl(ttl_seconds)
        with self._locked():
            record = self._read_record()
            now = self._clock.now()
            if record is None or record.state == "released":
                token = record.fencing_token + 1 if record is not None else 1
                lease = Lease(
                    lease_id=_make_lease_id(token, now),
                    owner_id=owner_id,
                    acquired_at=now,
                    expires_at=now + ttl,
                    renewed_at=now,
                    fencing_token=token,
                    ttl_seconds=ttl,
                    state="active",
                    last_accepted_checkpoint=(
                        record.last_accepted_checkpoint if record is not None else None
                    ),
                    takeover=None,
                )
                self._write_record(lease)
                return lease
            if not record.is_expired(now):
                if record.owner_id == owner_id:
                    tk = record.takeover or {}
                    if (tk.get("from_lease_id") == lease_id
                            and tk.get("key") == takeover_key
                            and record.owner_id == owner_id):
                        return record  # 幂等：同一 takeover 只生效一次
                    # 自己持有：等价续租，token 不变
                    record.renewed_at = now
                    record.expires_at = now + record.ttl_seconds
                    self._write_record(record)
                    return record
                raise LeaseStillActive(
                    f"接管失败：租约仍有效，owner={record.owner_id!r}，"
                    f"expires_at={_utc_iso(record.expires_at)}"
                )
            # 过期接管
            token = record.fencing_token + 1
            lease = Lease(
                lease_id=_make_lease_id(token, now),
                owner_id=owner_id,
                acquired_at=now,
                expires_at=now + ttl,
                renewed_at=now,
                fencing_token=token,
                ttl_seconds=ttl,
                state="active",
                last_accepted_checkpoint=record.last_accepted_checkpoint,
                takeover={
                    "from_lease_id": record.lease_id,
                    "from_owner": record.owner_id,
                    "key": takeover_key,
                    "at": now,
                },
            )
            self._write_record(lease)
            return lease

    def write_guard(self, fencing_token: int) -> None:
        """side effect 提交前的 fencing 校验：当前租约 token 与调用者 token
        不一致时抛 FencingTokenMismatch（旧 owner 失效后写入必拒）。"""
        with self._locked():
            record = self._read_record()
            if (record is None or record.state != "active"
                    or record.fencing_token != fencing_token):
                raise FencingTokenMismatch(
                    f"fencing 拒绝：调用者 token={fencing_token!r}，"
                    f"当前 token={record.fencing_token if record else None!r}"
                )

    def record_last_accepted_checkpoint(self, owner_id: str, lease_id: str,
                                        fencing_token: int, checkpoint_id: str) -> None:
        """记录最后已接受 checkpoint：先 fencing 校验再写入，供 takeover 后继续。"""
        with self._locked():
            record = self._read_record()
            if (record is None or record.owner_id != owner_id
                    or record.lease_id != lease_id or record.state != "active"):
                raise LeaseOwnershipLost(
                    f"checkpoint 记录失败：owner={owner_id!r} lease={lease_id!r} 已不持有租约"
                )
            if record.fencing_token != fencing_token:
                raise FencingTokenMismatch(
                    f"fencing 拒绝：调用者 token={fencing_token!r}，"
                    f"当前 token={record.fencing_token!r}"
                )
            record.last_accepted_checkpoint = checkpoint_id
            self._write_record(record)

    def current(self) -> Optional[Lease]:
        with self._locked():
            return self._read_record()

    def heartbeat(self, owner_id: str, lease_id: str) -> Lease:
        """心跳 = 续租；失败计数保留用于观测。"""
        try:
            return self.renew(owner_id, lease_id)
        except LeaseError:
            self._heartbeat_failures += 1
            raise

    @property
    def heartbeat_failures(self) -> int:
        return self._heartbeat_failures


class HeartbeatLoop:
    """后台心跳线程：Event.wait 阻塞等待（非忙轮询），间隔默认取 store 的
    heartbeat_interval（默认 30.0 秒）。"""

    def __init__(self, store: LeaseStore, owner_id: str, lease_id: str,
                 interval: float = None):
        self._store = store
        self._owner = owner_id
        self._lease_id = lease_id
        self._interval = float(
            interval if interval is not None else store.heartbeat_interval
        )
        if self._interval <= 0:
            raise ValueError("interval 必须 > 0")
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._beats = 0
        self._failures = 0

    @property
    def beats(self) -> int:
        return self._beats

    @property
    def failures(self) -> int:
        return self._failures

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="lease-heartbeat"
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._store.renew(self._owner, self._lease_id)
                self._beats += 1
            except LeaseError:
                self._failures += 1

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def run_once(self) -> Lease:
        """手动心跳一次（测试用，确定性）。"""
        try:
            lease = self._store.renew(self._owner, self._lease_id)
            self._beats += 1
            return lease
        except LeaseError:
            self._failures += 1
            raise
