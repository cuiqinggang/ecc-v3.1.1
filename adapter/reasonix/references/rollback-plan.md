# ecc-v3.1-production 正式接入与回滚计划

> 本目录是 Reasonix 影子适配器（WP-05 第2步产物）。正式接入由 control 角色单独
> 执行；执行与回滚均不修改 AppData 用户目录、不写 global-workspace 的既有内容。
> 本文件不含任何带盘符的绝对路径，`<workspace>` 指目标 Reasonix 工作区根目录。

## 1. 正式接入步骤（control 执行）

1. **接入前快照**（记录目标 skills 目录现状，回滚比对用）：
   - 目录清单：`dir /s /b <workspace>\.reasonix\skills > snapshot-before.txt`
   - 如已存在同名技能目录 `ecc-v3.1-production`，先整体备份为
     `ecc-v3.1-production.bak-<日期>` 并记录差异，再继续。
2. **复制影子适配器到技能根**（PowerShell 示例）：
   `Copy-Item -Recurse adapter\reasonix <workspace>\.reasonix\skills\ecc-v3.1-production`
3. **冲突检查**：技能名 `ecc-v3.1-production` 不得与 Reasonix 内置技能
   （init / explore / test 等）或既有技能同名；如冲突，先在权威副本改名
   （SKILL.md frontmatter 的 name 与 contract.json 的 skill.name 同步修改）
   再重新复制。
4. **验证加载**：Reasonix 会话刷新后技能索引中出现 `ecc-v3.1-production`
   （runAs: inline），无 shadowed 提示。
5. **验证运行**：在技能根副本目录执行
   `python scripts\ecc31_smoke.py`，期望 exit 0 且 `status=ECC_ACCEPTED`；
   再用 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ecc31_smoke.ps1`
   复核 exit code 语义一致。
6. **权威副本保留**：`v3.1\adapter\reasonix` 为唯一权威源。后续修改先在此
   落地（跑 v3.1 合同测试全绿），再单向复制到技能根；禁止反向修改技能根
   副本后再回写。

## 2. 回滚步骤（control 执行）

1. 删除技能根副本：
   `Remove-Item -Recurse -Force <workspace>\.reasonix\skills\ecc-v3.1-production`
2. 与 `snapshot-before.txt` 比对，确认 skills 目录恢复接入前状态。
3. 如接入时备份过旧同名目录，按备份恢复：
   `Move-Item ecc-v3.1-production.bak-<日期> <workspace>\.reasonix\skills\ecc-v3.1-production`
4. 刷新 Reasonix 会话，确认索引中不再出现该技能。
5. 回滚不影响影子副本 `v3.1\adapter\reasonix`（权威源保留，可随时再次接入）。

## 3. 接入前快照清单

- 目标技能根路径：`<workspace>\.reasonix\skills\`
- 快照文件：`snapshot-before.txt`（`dir /s /b` 输出，含文件名与相对层级）
- 同名旧目录备份：`ecc-v3.1-production.bak-<日期>`（仅当接入前已存在同名技能）
- 依赖事实：python 3.x（unittest 为标准库）、PowerShell 5.1（powershell）与
  pwsh 7 均可作为入口；适配器仅读 v3.1 源码与测试，不产生任何 AppData 写入。

## 4. 恢复命令（对照快照）

- 删除本次接入产物：
  `Remove-Item -Recurse -Force <workspace>\.reasonix\skills\ecc-v3.1-production`
- 恢复旧备份（如存在）：
  `Move-Item ecc-v3.1-production.bak-<日期> <workspace>\.reasonix\skills\ecc-v3.1-production`
- 复核：重新生成 `dir /s /b <workspace>\.reasonix\skills` 输出，与
  `snapshot-before.txt` 做 diff；除备份文件外必须为空。
