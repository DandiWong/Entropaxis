# 系统工程（.system）

这里维护跨项目共用的规则、模板、工具和 Skill。工作区元规则及总路由以 [根 AGENTS](../AGENTS.md) 为准，本文件只说明系统工程目录的职责。

## 目录

| 路径 | 职责 |
|---|---|
| `root-configs/` | 工作区根入口物理源文件（`AGENTS.md`、`CLAUDE.md`） |
| `rules/` | 跨项目纯抽象规则真源（零系统绑定） |
| `templates/` | 项目入口、文档和配置模板 |
| `tools/` | 初始化、同步、软链恢复与规则校验工具 |
| `skills/` | 工作区维护的 Skill 真源（各 Skill 100% 自包含） |
| `tests/` | 工具级单元测试 |
> 工作区实例数据（项目注册表、内部战略文档、凭据）存放于 `.data/`（与 `.system` 同级），不在本目录。

## 规则层级

```text
工作区根/AGENTS.md
  ├─ .system/rules/*.md
  └─ <项目>/AGENTS.md
       └─ README / Spec / 任务材料
```

根入口负责路由与安全边界；系统规则负责跨项目抽象约束；项目入口只保存项目差异和按需入口。第三方或上游仓库的内部 `AGENTS.md` 仍由其自身维护。

## 常用入口

- 新项目初始化：`python .system/tools/init_project.py <项目路径>`
+- 根入口软链恢复：`python3 .system/tools/bootstrap.py`
- 规则检查：`python .system/tools/lint_workspace.py`
- 开发规则：`rules/开发通用规则.md`
- 任务联动：`rules/开发项目联动规则.md`
- 规则治理：`rules/代码库重构与治理规则.md`

修改系统规则前先读本目录 `AGENTS.md`；项目实现细节应留在项目 README、Spec 或代码中，不回填到系统规则。
