---
name: nightly-regression
description: "v1.1.1. 编排 Entropaxis nightly 回归：判定哪些验证移到夜间、登记作业、生成 launchd/CI 配置、装配只报不阻断的报告与失败通知；不替代合入门禁。触发词：nightly、夜间回归、每晚跑测试、定时跑 E2E、launchd 定时、nightly 状态。"
metadata:
  scope: control-plane
  version: "1.1.1"
---

# Nightly 回归编排

本 Skill 是《软件工程》「测试与验证」延迟层契约的执行体。规则侧只保留分层判据与触发意图，
哪些验证该进 nightly、怎么配定时、报告落哪里，在本文件维护。

规则真源：
- [`../../rules/软件工程.md`](../../rules/软件工程.md)「测试与验证」（门禁层/延迟层判据）
- [`../../rules/工具设计.md`](../../rules/工具设计.md)（实例配置声明外置与契约）
- [`../../rules/文件交付.md`](../../rules/文件交付.md) 第 4 节（报告打开闭环）

## 0. 适用边界

**该进 nightly 的验证**（命中任一即归延迟层）：
- 需要运行中的完整栈（数据库/缓存/对象存储/后台 worker）；
- 需要外部账号、真实模型调用或有计费波动；
- 有副作用：改数据、发外部请求、占用独占资源；
- 单轮耗时超过 10 分钟；
- 结果只作趋势信号，不作本次改动放行依据。

**不得进 nightly**（必须留在合入/发布门禁）：
- 单元测试、静态检查、Schema 校验、依赖锁一致性；
- 编译/构建/镜像可复现性；
- 分钟级集成测试。

判据一句话：**nightly 回答「最近有没有坏」，门禁回答「这次能不能合」。** 把 nightly 失败当成阻断本次合入的理由，是层次错位。

---

## 1. 分层盘点

先盘点再动手，不要凭空列命令。

1. 读项目 `AGENTS.md` 的「测试与验证」相关节，列出该已有的测试层与各自命令。
2. 逐层对照第 0 节判据，标成 `gate` 或 `nightly`。
3. 已有人工触发的端到端脚本（如 Playwright 冒烟）优先迁入 nightly——它们本就带「需运行栈/有副作用/调真实模型」属性。
4. 输出一张分层表给用户确认：

```text
层次     命令/入口                          触发时机        失败后果
gate     <项目全量检查命令>                 提交/合并前      阻断合入
gate     <静态检查/Schema 校验>             每次改动        阻断提交
nightly  <端到端/长时基准命令>              夜间无人值守    只报告不阻断
```

用户确认后才进入第 2 节。

---

## 1.5 作业注册与管理（实例数据在 `data/`）

**作业是工作区本地实例，不进 Skill 分发包。** 注册表单点真源是
`data/templates/nightly.yaml`（契约 `schemas/nightly.schema.json`），
读写统一经 `tools/nightly_registry.py`，不手改 YAML。

| 要看/要做什么 | 命令 |
|---|---|
| 有哪些项目配了 nightly、何时跑 | `python3 .entropaxis/tools/nightly_registry.py list` |
| 注册表 × 本机 launchd × 报告三方对齐 | `... nightly_registry.py status` |
| 登记新作业 | `... nightly_registry.py add --id <id> --project <项目id> --label <label> --hour 2 --minute 0 --commands "<命令>" [...]` |
| 停用 / 启用 / 移除 | `... nightly_registry.py disable <id>` / `enable <id>` / `remove <id>` |

`status` 回答「配了但没生效」：`plist=无` 或 `loaded=否` 即登记了没注册；
`enabled=off` 是主动停用；`最近报告` 为空说明还没跑过或报告目录不符。

**登记纪律**：
- `--project` 必须是 `registry.yaml` 里已存在的项目 id；`repo` 缺省取该项目 `code[0]`；
- `--label` 全局唯一，建议 `com.<组织>.<项目>.nightly`；
- `--hour` 默认避开工作时间（如 02:00），由用户确认；
- **凭据不登记**：注册表只存命令原文，`.env`/环境变量由项目自己管；缺凭据的条目在 wrapper 里记 `SKIP` 不记 `FAIL`；
- 一个项目多条 nightly 命令时，用多个 `--id`（如 `<proj>-e2e`、`<proj>-bench`），不要挤进一条 `commands` 里混报告。

---

## 2. 两种承载形态

按用户环境与已有基础设施选，**不重复造轮子**。

### 形态 A：CI 定时任务（优先，已有 CI 时）

适用于项目已有 CI/CD 平台（Jenkins/DSO、GitLab CI、GitHub Actions）。

1. 在项目的 CI 配置中加一个定时触发的 job（cron 表达式由用户定，默认每日一次）。
2. job 步骤：拉目标分支 → 起栈（或连已有测试环境）→ 灌入测试夹具 → 逐条跑 nightly 命令 → 汇总。
3. 夹具要求：**不碰真实业务数据**。测试账号、测试稿件、测试租户走独立前缀，跑完可弃。
4. 失败通知走平台既有通道，不新建第二套。
5. 报告写到 `docs/05_reports/` 并随 job 归档。

### 形态 B：本机 launchd（过渡，CI 未就绪时）

适用于 CI 定时任务尚未打通、需要立刻把验证移出工作时间的场景。

1. 生成 `~/Library/LaunchAgents/<label>.plist`，`label` 取 `com.<用户或组织>.<项目名>.nightly`。
2. `StartCalendarInterval` 由用户指定小时分钟；默认**避开工作时间**（如 02:00）。
3. `ProgramArguments` 指向一个 wrapper 脚本，脚本内容只做三件事：
   - `cd` 到项目根；
   - 顺序执行 nightly 命令，每条结果写进当日日志；
   - 全部跑完后生成汇总报告。
4. wrapper 脚本放项目 `03_工程研发/<app>/scripts/`，**不放进控制面**（它是项目局部执行体）。
5. 凭据不入 plist 也不入脚本：从项目既有 `.env` 或环境变量读，缺凭据的条目记 `SKIP` 不记 `FAIL`。
6. **先登记后注册**：`nightly_registry.py add` 把作业写进 `data/templates/nightly.yaml`，再 `launchctl bootstrap gui/$(id -u) <plist>`。二者顺序不可倒——先注册后登记会出现「在跑但查不到」。
7. 注册：`launchctl bootstrap gui/$(id -u) <plist>`；卸载：`launchctl bootout gui/$(id -u) <label>`。
8. 交付时把「注册/卸载/查看日志」三条命令原文给用户，不代为静默安装。
9. 交付后用 `nightly_registry.py status` 复核：该作业须 `plist=有`、`loaded=是`、`enabled=on`，三者缺一即没生效。

**A 就绪后 B 的退出**：`nightly_registry.py disable <id>` 停登记侧，删 plist 并 `bootout`，删 wrapper 脚本，在项目 `AGENTS.md` 标注 nightly 已由 CI 承载，防止两处同时跑。确认 CI 稳定后再 `nightly_registry.py remove <id>` 清登记。

---

## 3. 报告与通知契约

1. **只报不阻断**：nightly 失败不得回写任何门禁状态、不得改 `Tasks.md` 的完成态、不得阻断分支合入。
2. 报告落 `docs/05_reports/YYYYMMDD_nightly_<项目名>.md`，含四段：
   - 跑了哪几条、各自退出码；
   - 失败条目的可复现命令；
   - 与上一轮的差异（新增失败/转绿）；
   - 未覆盖范围（因缺凭据、缺夹具而 SKIP 的条目）。
3. 连续失败策略：同一条目连续 2 晚失败，在报告里升为「需人工介入」并列出，不自动重试更多次。
4. 不自动修复、不自动回滚、不自动推送。nightly 是信号源，不是执行器。

---

## 4. 收尾

1. 按《文件交付》第 4 节处理报告（`.md` 不自动打开，回复中给路径）。
2. 在项目 `AGENTS.md` 的测试节补一句 nightly 承载形态（A 或 B）与报告位置，保持单一真源。
3. 交付时写明：哪些层留门禁、哪些进 nightly、定时表达式、注册/卸载命令、报告路径、未覆盖范围。
