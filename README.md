# Entropaxis

> Turns entropy into taxis: a human-AI workspace that routes disorder into structured, executable action.

---

**人机协作控制面** · 规则抽象 · 技能生态 · 自动化治理

---

## 🧑 人类协作者须知

### 核心理念与上手

1. **解放双手的下一代人机协作系统**  
   这是一套**彻底解放人操作鼠标动作的人机协作系统**。将繁琐的 UI 点选、表单录入、跨平台状态同步与重复性事务处理，全量转为“自然语言意图输入 + 受控 Agent 自动化生产与治理”的确定性闭环。

2. **零门槛极简上手**  
   无需记忆复杂的命令行参数，只需**使用本地 Agent（如 Claude Code / OMP / Cursor / Codex）直接打开工作区根目录**，即可通过自然语言对话完成待办流转、方案审修（审计/修正）、多模态协作（自定义角色）、看板管理、纪要生成、发票报销与知识沉淀。  
   *（新电脑或非技术同学亦可直接双击一键脚本，或对 Agent 说“初始化”一键就绪：macOS / Linux 用 `.system/tools/🚀_一键配置工作区.command`，Windows 用 `.system/tools/一键配置工作区.bat`）*

3. **健康度自检**：对 Agent 说“系统自检”即可；异常时说“初始化工作区”一键自愈。

### 小白快速上手 10 个场景

不懂技术也没关系，下面按常见职务角色举例，照着说这句话就行：

1. **行政/财务岗**，想把这几天攒的发票统一算清楚生成报销单，就说一句“报销归总”。
2. **新入职同事**，第一次用这台电脑/这个工作区，什么都不会配，就说一句“初始化”。
3. **项目经理**，接了个新项目要开始张罗，得先有个地方装文档和进度，就说一句“起个事务 XX项目”。
4. **行政秘书**，老板要的那份合同/纪要突然找不到在哪个文件夹了，就说一句“找一下XX合同”。
5. **产品/运营专员**，做完一件事发现有个经验以后还会用到，想先记下来别忘了，就说一句“记录”（或“沉淀”）。
6. **团队负责人**，一个月过去了，想看看团队这段时间踩了什么坑、有什么没收尾的，就说一句“复盘”。
7. **客服/运营专员**，开会时定下来的几件跟进事项，要同步进公司用的任务看板里，就说一句“同步任务”。
8. **项目助理**，不确定自己本地记的项目状态跟公司系统里显示的是不是一致，就说一句“同步”。
9. **任何一位同事**，领导口头交代了一件事，怕自己忘，想正式登记成一条待办，就说一句“帮我建个任务：XX”。
10. **个人助理/HR**，刚才那次判断被人纠正了，觉得这个教训值得记住、以后别再犯，就说一句“这条记下来”（或“记为案例”）。

### 系统目录架构

| 路径 | 职责定位 |
|---|---|
| `entrypoints/` | 工作区根入口物理源文件（`AGENTS.md`、`CLAUDE.md`） |
| `rules/` | 跨项目纯抽象规则真源（零外部系统硬编码，纯架构约束） |
| `config/` | 由规则正文派生、供工具消费的机器可读控制面数据 |
| `schemas/` | 机器配置与文档元数据的结构化契约 |
| `skills/` | 工作区核心能力扩展库（100% 自包含 Agent Skill） |
| `templates/` | 项目入口、文档结构和机器配置模板 |
| `tools/` | 工作区自愈（`bootstrap.py`）、项目脚手架与健康度检查工具（`lint_workspace.py`） |
| `tests/` | 规则与工具自动化单测 |

> 📌 **数据隔离说明**：工作区实例数据（业务项目注册表、内部战略、凭据）独立存放于 `.data/`（与 `.system/` 同级），严禁提交至公共系统仓库。

> ⚠️ **分发信道（唯一合法路径）**：把本系统交付给他人时，只允许 `git clone`、`git archive`、GitHub 版本库 ZIP 下载，或 `build_windows_installer.py` 产出的 Windows 安装包（其载荷即 `git archive` 跟踪集）。**严禁直接拷贝、压缩 `.system/` 目录或经网盘同步**——私有 Skill 靠自带 `.gitignore` 排除出版本库，它们在文件系统上依然物理存在，拷目录会连同内部端点与业务口径一并带走。

### Windows 安装

#### 推荐：双击 exe 安装包

向维护者要一份 **`Entropaxis安装程序.exe`**，双击即可：弹框选安装目录 → 展开控制面 → 自动初始化。控制面整份打在 exe 里，**安装全程不联网、不拉远端、不需要 GitHub 账号，也不需要先装 Python**。

装完日常使用仍需本机装有 Python 3（工作区的工具都是 `.py`）：到 <https://www.python.org/downloads/> 安装，记得勾选 **Add python.exe to PATH**。

装到已有工作区上即为升级：只覆盖 `.system/`，不动 `.data/`（你的实例数据与凭据），也不删你自己放进 `.system/skills/` 的私有能力。

> **维护者侧**：exe 由 GitHub Actions 的 `build-windows-installer` 工作流在 `windows-latest` 上构建（`main` 分支相关路径变更时自动触发，也可手动 `workflow_dispatch`），载荷强制取自 `git archive` 跟踪集，因此天然不含私有 Skill。
> 每次成功构建都会滚动更新 Releases 侧边栏的 `latest` 发布（侧边栏点开即最新版，不需要手动打 tag）；Actions 页面的 `entropaxis-windows-installer` 制品仅作 90 天内的构建过程留痕。

#### 备选：手动下载代码包

拿不到安装包时走版本库。这是一个私有仓库，只有受邀成员才能访问；如果 AI 助手提示"下载失败"或"无法访问"，**不用理会报错信息**，按以下步骤手动下载即可：

1. 用浏览器打开 `https://github.com/DandiWong/Entropaxis`（确保已登录 GitHub）
2. 点击绿色的 **Code** 按钮 → **Download ZIP**
3. 把下载好的 ZIP 交给 AI 助手，让它帮你解压到工作区
4. 解压到位后进入 `.system\tools\`，双击 **`一键配置工作区.bat`**（不是 `.command`，那个是 Mac 用的）

#### 让 AI 助手帮你操作网页

如果你希望 AI 助手直接在浏览器里帮你操作（比如帮你点按钮、填表单），需要先按以下步骤开启一个特殊模式，让 AI 助手能"看到"你的浏览器：

1. 完全关闭 Chrome（任务栏右键 → 退出）
2. 重新打开 Chrome，然后告诉 AI 助手"Chrome 已重启，请继续"

> 如果这一步太麻烦，直接告诉 AI 助手"我自己操作浏览器，你告诉我点哪里"就好，不必强行让 AI 接管。

#### 其他已知问题（执行决策参考）

| 症状 | 处理方式 |
|------|---------|
| PowerShell 返回空输出或报错 | 改用 `cmd /c <命令>` 执行，或用 `Out-File` 将输出写入临时文件再读取 |
| `.system` / `.data` 在文件管理器中可见 | 执行 `attrib +h +s <路径>\.system` 和 `attrib +h +s <路径>\.data` |

---

## 🤖 AI Agent 须知

### 引导与初始化契约

当 AI Agent 接入或初始化本工作区时，请严格遵守以下操作协议：

1. **仓库挂载与初始化**  
   将本仓库作为 `.system/` 目录置于工作区根目录下，并首先执行入口配置同步工具：
   ```bash
   python3 .system/tools/bootstrap.py
   ```
   该工具会自动将 `.system/entrypoints/` 下的 `AGENTS.md` 与 `CLAUDE.md` 物理同步至工作区根目录，建立全局统一的规则控制面（纯文件复制，杜绝云同步网盘跨平台软链冲突）。
2. **规则与上下文纪律**  
   - 严格遵循根 `AGENTS.md` 常驻层控制面与 `.system/rules/` 架构规范；
   - 坚持**单一真源（Single Source of Truth）**、**上下文瘦身**与 **YAGNI 原则**，禁止跨层级复制规则或创建冗余文件；
   - 涉及多端任务与项目识别时，以 `.data/templates/registry.md` 实例注册表为唯一映射基准。

### 常用开发与治理入口

- **根入口配置同步/自愈**：`python3 .system/tools/bootstrap.py`
- **角色模态与外部 Agent 交互配置**：`python3 .system/tools/setup_agents.py`（支持 `--scan` 探测 CLI、`--verify` 校验角色、`--apply-preset` 应用预设）
- **指令路由与静态上下文审计**：`python3 .system/tools/audit_routing.py --strict`（`--json` 输出完整 Hook、去重后的必需读取区间、未知项。静态估算不代表模型 usage 或费用）
- **工作区规则与健康度体检**：`python3 .system/tools/lint_workspace.py`（阻断项与建议项以工具输出为准；治理卫生类为建议项，全新工作区未初始化状态不红屏）
- **分发就绪核验（收件方视角）**：`python3 .system/tools/check_distribution.py`
- **构建 Windows 安装包**：`.github/workflows/build-windows-installer.yml` 在 `windows-latest` 上跑 `python3 .system/tools/build_windows_installer.py`（`--out` 产物路径、`--ref` 打包提交）导出版本库跟踪集载荷，再用 PyInstaller 把它与 `tools/install_windows.py` 一起冻结成单文件 exe；收件方侧由 `install_windows.py` 承接选目录、落盘与初始化
- **方案审计门禁校验**：`python3 .system/tools/check_audit_gate.py <审计报告路径>`
- **按配置打开文件**：`python3 .system/tools/open_file.py <path> [<path> ...]`
- **通用项目脚手架初始化**：`python3 .system/tools/init_project.py <项目名>`
- **软件应用脚手架初始化**：`python3 .system/tools/init_app.py <app-name> --target-dir <项目路径>/02_开发`
- **路由读取契约**：`config/route_map.json` 使用 `reads: [{file, anchor}]`，`anchor: null` 表示全文；每个必需依赖显式列出，条件依赖按动作追加。Hook 和审计共用 `tools/route_context.py`，保留适用范围、合并重叠区间；锚点异常回退会留痕并由体检检出。
- **语料维护**：`tests/fixtures/instruction_cases.json` 是已登记回归集；`instruction_holdout.json` 是冻结的构造样本诊断，不据它调词或刷分。用留出集参与调参后必须保留其身份并另设新组，不能继续宣称未见数据。

### 规则真源导航

总路由见根 `AGENTS.md`；本目录职责划分见 [`AGENTS.md`](AGENTS.md)；修改系统规则前必读 [`rules/00_元规则.md`](rules/00_元规则.md) 与 [`rules/01_根系统治理.md`](rules/01_根系统治理.md)（自迭代 SOP 与双重体检门禁）。业务项目实现细节沉淀于项目自身 README、Spec 或产物中，不反向污染系统抽象层。
