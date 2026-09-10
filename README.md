# ECC V3.1 生产融合运行时

ECC V3.1 生产融合任务的正式运行时（任务 ID：ECC-V3.1-PRODUCTION-FUSION-01）。
本目录是 portable release 的源码根：可独立部署到任意目录运行。

## 组件

| 路径 | 说明 |
| --- | --- |
| `loopx/` | 长期循环引擎（WP-01）：goal/scheduled/event/hybrid 四模式、六态状态机、五熔断、三文件合同（LOOP-CONTRACT.json / STATE.json / RUN-LOG.jsonl）、schema 迁移、ECC 交接与四态回传 |
| `lease.py` | 租约 / fencing token / 失联接管（WP-02）：跨进程锁、CAS 等价原子更新、单调 token、write_guard、租约损坏 fail closed |
| `runners/` | 真实 Codex CLI Runner（WP-03）：现场探测、凭据零读取、超时/取消/错误输出有界失败、子进程树清理、输出脱敏、generic/fake 回归 |
| `faults/` | 系统化故障注入与恢复（WP-04）：18 类故障的注入器与矩阵 |
| `adapter/reasonix/` | Reasonix 影子适配器（WP-05）：技能包（SKILL.md + 脚本 + 回滚计划），formal 接入内容见 Reasonix-integrated release |
| `scripts/` | WP-06 脚本：`endurance1000.py`（1000 轮确定性循环）、`fresh_resume.py`（冷恢复测试）、`endurance60.py`（60 分钟耐力）、`build_release.py`（打包）、`validate_run_evidence.py`（严格证据验证器） |
| `tests/` | 全部单元/集成测试（unittest，131 项） |

## 运行要求

- Python 3.10+（本机验证环境 3.13）
- 不依赖第三方包（标准库实现）；`runners/codex_runner.py` 的真实调用依赖本机 codex CLI 且默认禁用（live=False）

## 测试

```text
python -m unittest discover -s tests -q        # 131 项，全绿
python scripts/endurance1000.py --selftest     # 1000 轮确定性循环自测
python scripts/fresh_resume.py --selftest      # 冷恢复自测
python scripts/endurance60.py --minutes 0.15   # 耐力短时自测（正式 60 分钟由调度方执行）
python scripts/validate_run_evidence.py --selftest   # 证据验证器正反例自测
```

## 关键不变量

- 每轮只处理一个可验证工作包；幂等键集合长度等于轮次数（0 重复 side effect）。
- STATE.json 原子更新（临时文件 + fsync + 读回校验 + 替换）；RUN-LOG.jsonl 只追加且逐行 fsync。
- 状态机六态转换必须命中合法转换表；ECC 回传只接受四态。
- 熔断五类：max_rounds / max_repairs / max_runtime_seconds / stale_limit / same_error_limit。
- release 内禁止携带：`__pycache__`/`*.pyc`、运行数据（STATE/RUN-LOG/checkpoint/lease）、密钥、硬编码绝对路径。

## 版本基线

- 上游基线：V3.0（commit bd71aa2，分支 ecc-v3-loopx）与 V2.1（commit 2a9d006，tag ecc-v2.1.0-stable），字节级冻结，本目录不修改基线。
- V3.1 分支：ecc-v3.1-production。
