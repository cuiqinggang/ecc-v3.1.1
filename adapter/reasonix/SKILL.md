---
name: ecc-v3.1-production
description: "ECC V3.1 生产融合运行时最小任务入口：验证 v3.1 全套测试并回传 ECC 四态（loopx 长期循环、租约 fencing 与接管、Codex Runner、18 类故障注入）。"
runAs: inline
color: teal
---

# ECC V3.1 生产融合运行时（影子适配器）

本技能是 ECC V3.1 生产融合的 Reasonix 影子适配器。目录整体复制到
`<workspace>\.reasonix\skills\ecc-v3.1-production` 后即被自动发现，无需注册或改索引。
权威副本长期保留在 v3.1\adapter\reasonix，修改先在此落地再单向同步到技能根。

## 运行时组成

- **loopx 长期循环引擎**：四种模式（goal / scheduled / event / hybrid）、
  六态机（CLOSED / WAITING / REPAIR / ECC_REQUIRED / HUMAN_REQUIRED / BLOCKED）、
  五种预算熔断（max_rounds / max_repairs / max_runtime_seconds / stale_limit /
  same_error_limit）。
- **租约系统**：acquire / renew / release / expire / takeover，fencing 令牌保证
  双 worker 跨进程唯一 owner，重复接管幂等。
- **Codex Runner**：真实 Codex CLI 调用封装（live 冒烟已通过，marker=ECCV31LIVE-OK）。
- **故障注入**：18 类故障案例 + 18 个对照案例（FaultInjector 隔离副本执行）。

## 最小任务协议

1. 运行 `scripts\ecc31_smoke.py`（Python）或 `scripts\ecc31_smoke.ps1`（PowerShell 5.1/7）。
2. 脚本在子进程运行 `python -m unittest discover -s tests -q`（超时 300 秒）。
3. 解析结果并输出单行四态 JSON：

   ```json
   {"status": "ECC_ACCEPTED", "tests_run": 119, "failures": 0, "skipped": 1, "duration": 34.2}
   ```

4. 四态判定：
   - 全绿 → `ECC_ACCEPTED`（exit 0）
   - 部分失败 → `ECC_PARTIAL`（exit 1）
   - 全部失败 → `ECC_BLOCKED`（exit 2）
   - 运行器崩溃 / 测试目录缺失 / 超时 → `ECC_REJECTED`（exit 3）
5. 错误契约与 loopx 侧 ReasonixAdapterPlaceholder 对齐：
   `AdapterStartupError`（启动失败，携带退出码与 stderr 摘要）、
   `AdapterLoadError`（SKILL.md 加载失败），ECC 侧一律 fail closed。

详细合同见 contract.json；正式接入与回滚见 references/rollback-plan.md。
