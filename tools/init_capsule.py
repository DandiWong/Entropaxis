#!/usr/bin/env python3
"""初始化事务胶囊 (Transaction Capsule) 标准目录骨架。

规范真源：`.entropaxis/data/docs/20260910_事务胶囊设计规范/02_方案.md`（`status: active`，
经 6 轮对抗性审计，16 项问题全部 `closed`，见同目录 `05_审计报告.md`）。本工具
经《治理指令》「机制转工具」6 步 SOP 由该规范的原型脚手架纳管而来，核心逻辑
与审计通过版本逐字一致，只补齐 ApX 行动导向错误契约与 `--json` 结构化输出。

设计要点（对应审计 R2-C1/M1/M2/M4/M5/m1~m6、R3-m1、R4-M1、R5-M1/m1）：
  1. 只生成 Schema 合法的阶段文档：文档级 status 一律 `draft`，交付/验收字段一律「待回填」占位；
  2. 不预生成 05_审计报告.md——审计报告由「审计」指令在审计时创建并写入受审文件真实 SHA-256；
  3. 胶囊级生命周期（draft/active/delivered/archived/archived-unmeasured）唯一真源为 capsule.yaml，
     不与文档级 Front Matter 状态（draft/active/revised/completed）混用；
  4. 不预建任何空目录（00_原始素材/、assets/ 按需创建，由 capsule.yaml 的 optional_stages 声明）；
  5. 校验主题与 Task ID 的路径合法性，拒绝分隔符、`..`、控制字符、保留字符与首尾空白；
  6. 原子创建：先在同级临时目录完整生成并通过自检，再一次性改名落位；失败自动清理，无半成品；
  7. 主题以 JSON 字符串语法（YAML 兼容的带引号标量）写入所有 Front Matter 与 capsule.yaml，
     防止 `#`/`:`/`&` 等 YAML 指示字符被解析器当作注释或结构截断主题；自检对写入值做真实
     反解析，核对与原始输入等值。

用法：
  python3 .entropaxis/tools/init_capsule.py <父目录> <中文主题> --id Tech-N --mode full|light|research [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path


class CapsuleInitError(Exception):
    """胶囊无法安全初始化；消息已包含 ❌ 错误原因 与 👉 修复建议（ApX 行动导向错误契约）。"""


def _error(reason: str, fix: str) -> CapsuleInitError:
    return CapsuleInitError(f"❌ {reason}\n👉 {fix}")


# —— 输入契约（单一真源，脚本内只此一份） ————————————————
TOPIC_RESERVED = set('*:?\"<>|/\\')
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
CHINESE_RE = re.compile(r"[一-龥]")
ID_RE = re.compile(r"^(Tech|Task|B|R|SC|M|Bug)-\d+$")


def _yaml_str(value: str) -> str:
    """把自由文本序列化为 YAML 兼容的带引号标量（R5-M1）。

    JSON 字符串语法是 YAML 双引号标量的严格子集：转义规则完全一致，任何
    YAML 1.1/1.2 解析器都能按 JSON 语义读回原值。用它而不是裸插值，
    `#`/`:`/`&`/`[`/`]` 等指示字符不再有机会被解析器当作注释或结构分隔符。
    """
    return json.dumps(value, ensure_ascii=False)


# 文档级 Front Matter 契约：status 枚举与 id 正则必须与
# .entropaxis/schemas/front_matter.schema.json 一致；此处用于生成后自检。
DOC_STATUS_ENUM = ("draft", "active", "revised", "completed")

# 各阶段文档的 Front Matter 契约（type, author, status, 是否带 id）
STAGE_CONTRACT = {
    "01_调研.md": ("Research", "Researcher", "draft"),
    "02_方案.md": ("Proposal", "Designer", "draft"),
    "03_设计.md": ("Proposal", "Designer", "draft"),
    "07_验收报告.md": ("Report", "Maintainer", "draft"),
}

MODE_STAGES = {
    "research": ["01_调研.md"],
    "light": ["01_调研.md", "02_方案.md", "04", "07_验收报告.md"],
    "full": ["01_调研.md", "02_方案.md", "03_设计.md", "04", "07_验收报告.md"],
}

# capsule.yaml 的 instantiated_stages 展示名，单源自 MODE_STAGES（R4-M1：此前
# planned_stages 由一段独立拼接的字面量硬编码，遗漏已生成的 01/02/03，还给
# research 模式声明了它从不生成的 04/07——两份定义各自漂移。展示名只做去
# 后缀/去序号前缀的最小转换，不引入与 MODE_STAGES 不同步的第二份阶段清单）。
_STAGE_DISPLAY = {
    "01_调研.md": "01_调研",
    "02_方案.md": "02_方案",
    "03_设计.md": "03_设计",
    "04": "04_Spec",
    "07_验收报告.md": "07_验收",
}

MANIFEST_TEMPLATE = """\
# 事务胶囊清单 —— 胶囊级生命周期唯一真源（文档级状态仍用 draft/active/revised/completed）
id: {id}
topic: {topic_q}
mode: {mode}
created_at: {date}
# draft | active | delivered | archived | archived-unmeasured
lifecycle: draft
# 工程交付日：代码合入主干且全量测试通过后回填；以下两个日期随之派生
delivered_at: null        # 交付日
review_due_at: null       # delivered_at + 30 天：首次价值回收提醒
closure_deadline: null    # delivered_at + 60 天：正常归档或未度量归档的硬截止
# 本次已实例化的阶段（单源自 MODE_STAGES，与实际生成文件集恒等）
instantiated_stages: [{instantiated}]
# 全模式通用的按需可选目录，不计入上一行；需要时再建，不预建空目录
optional_stages: [00_原始素材, assets]
# 跨胶囊引用只记稳定 ID（如 Tech-105），严禁相对路径强绑定
relations: []
"""


def _stage_template(filename: str, topic: str, today: str, task_id: str) -> str:
    """返回单个阶段文档的完整内容（Front Matter + 骨架章节）。"""
    topic_q = _yaml_str(topic)
    if filename == "01_调研.md":
        return f"""---
type: Research
topic: {topic_q}
date: {today}
author: Researcher
status: draft
---

# {topic} · 业务调研与背景

## 一、调研结论 (Conclusion)

## 二、事实与依据 (Evidence)

## 三、风险与假设 (Risks & Assumptions)

## 四、置信度评估 (Confidence)
"""
    if filename == "02_方案.md":
        return f"""---
type: Proposal
topic: {topic_q}
date: {today}
author: Designer
status: draft
---

# {topic} · 业务与架构方案

## 一、业务目标与预期成效

## 二、系统边界与非目标 (Non-goals)

## 三、业务全流程与架构设计

## 四、核心决策与替代方案对比
| 备选方案 | 优势 | 劣势 | 判定结论 |
|---|---|---|---|
| 方案 A | | | 待定 |
"""
    if filename == "03_设计.md":
        return f"""---
type: Proposal
topic: {topic_q}
date: {today}
author: Designer
status: draft
---

# {topic} · 交互与视觉设计

## 一、页面/组件拓扑

## 二、字段动静映射（前端静态 vs API 动态）

## 三、交互事件与状态处理

## 四、原型索引（放入原型文件后回填相对链接）
"""
    # 04_Spec_<ID>.md
    return f"""---
type: Spec
topic: {topic_q}
date: {today}
author: Builder
id: {task_id}
status: draft
---

# {task_id} · {topic} 实施规格

## 一、API 契约
<!-- Path, Method, Request Schema, Response Schema, ErrorCodes -->

## 二、可观测性：埋点、日志与统计度量规则
- **埋点事件**：待设计（事件名、触发时机、上报参数）；
- **业务与审计日志**：待设计（上下文字段、脱敏规则）；
- **核心度量指标**（按类型二选一）：
  - 用户交互型：功能使用人数/频次、全功能使用占比、DAU/WAU/MAU 活跃贡献、7日/月度留存率；
  - 底层任务型：调用频次、执行耗时 P95/P99、成功率/错误率、吞吐量。

## 三、数据库变更 (DDL)
<!-- 完整迁移脚本与回滚策略；无变更写「无」 -->

## 四、自动化测试与验收契约 (TDD)
- [ ] 待编写
"""


def _acceptance_template(topic: str, today: str, task_id: str) -> str:
    """07_验收报告.md：双阶段验收与价值回收（初始全部为待回填占位，不得预填事实）。"""
    topic_q = _yaml_str(topic)
    return f"""---
type: Report
topic: {topic_q}
date: {today}
author: Maintainer
id: {task_id}
status: draft
---

# {topic} · 验收报告（工程验收 + 价值回收）

## 第一阶段：工程交付验收
- **交付日期**：待回填（代码合入主干且全量测试通过之日）
- **测试回归**：待回填（附真实测试输出或报告链接）
- **发版记录**：待回填（Changelog 条目 / Git Tag）

## 第二阶段：业务价值回收（review_due_at 提醒，closure_deadline 硬截止）
- **数据回收日期**：待回填
- **核心指标达成情况**：待回填（对照 04_Spec 第 2 节预设指标）
- **未度量兜底说明**：待回填（closure_deadline 前仍无法度量时必填，随 lifecycle: archived-unmeasured 封存）
"""


def validate_inputs(topic: str, task_id: str) -> None:
    """R2-m1：主题与 Task ID 的路径合法性强校验，先于任何目录操作。"""
    if not topic or not CHINESE_RE.search(topic):
        raise _error(
            "主题不含中文",
            "改用符合《文件交付》§2.1 中文主命名铁律的主题重试（专有技术名词/代号可原样嵌入中文主题）。",
        )
    if topic != topic.strip():
        raise _error("主题含首尾空白", "去除主题首尾空格或制表符后重试。")
    if CONTROL_RE.search(topic):
        raise _error("主题含控制字符", "去除主题中的不可见控制字符（0x00-0x1F、0x7F）后重试。")
    bad = sorted(set(topic) & TOPIC_RESERVED)
    if bad:
        raise _error(
            f"主题含保留字符 {bad}",
            "跨平台文件名不得含 * : ? \" < > | / \\，替换或删除这些字符后重试。",
        )
    if ".." in topic or ".." in task_id:
        raise _error(
            "主题或 Task ID 含路径穿越片段 '..'",
            "移除 '..' 片段后重试；Task ID 只允许稳定前缀+数字（如 Tech-1）。",
        )
    if not ID_RE.match(task_id):
        raise _error(
            f"Task ID 格式不合法，实得 {task_id!r}",
            "Task ID 须形如 Tech-1 / Bug-31（前缀 Tech|Task|B|R|SC|M|Bug + 数字）。",
        )


def _parsed_topic(raw_value: str, source: str) -> str:
    """把写入文件的 JSON 引号标量反解析回原始主题；失败即视为写入损坏（R5-M1）。"""
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise _error(
            f"{source} 的 topic 字段不是合法 JSON 引号标量: {raw_value!r} ({exc})",
            "这是脚本内部序列化异常，不应由用户输入触发；请汇报至根系统治理流程。",
        ) from exc
    if not isinstance(value, str):
        raise _error(
            f"{source} 的 topic 字段解析出非字符串类型: {type(value).__name__}",
            "这是脚本内部序列化异常，请汇报至根系统治理流程。",
        )
    return value


def _self_check(tmp: Path, task_id: str, topic: str) -> None:
    """生成后自检：Front Matter 可解析、符合脚本内契约、topic 反解析后与原始输入等值；失败即中止并清理。"""
    for md in sorted(tmp.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise _error(f"{md.name} 缺 Front Matter", "脚本内部模板异常，请汇报至根系统治理流程。")
        lines = text.split("\n")
        end = lines.index("---", 1)
        fm = {}
        for line in lines[1:end]:
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        contract = STAGE_CONTRACT.get(md.name)
        if md.name.startswith("04_Spec_"):
            if fm.get("type") != "Spec" or fm.get("id") != task_id:
                raise _error(f"{md.name} 的 Spec Front Matter 不合规: {fm}", "脚本内部模板异常，请汇报至根系统治理流程。")
        elif contract:
            typ, author, status = contract
            if (fm.get("type"), fm.get("author"), fm.get("status")) != (
                typ, author, status
            ):
                raise _error(f"{md.name} 的 Front Matter 不合规: {fm}", "脚本内部模板异常，请汇报至根系统治理流程。")
        if fm.get("status") not in DOC_STATUS_ENUM:
            raise _error(f"{md.name} 的文档状态非法: {fm.get('status')!r}", "脚本内部模板异常，请汇报至根系统治理流程。")
        if _parsed_topic(fm.get("topic", ""), md.name) != topic:
            raise _error(
                f"{md.name} 的 topic 反解析值与原始输入不一致: {fm.get('topic')!r}",
                "脚本内部序列化异常，请汇报至根系统治理流程。",
            )

    manifest = tmp / "capsule.yaml"
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("topic:"):
            if _parsed_topic(line.split(":", 1)[1].strip(), "capsule.yaml") != topic:
                raise _error(
                    f"capsule.yaml 的 topic 反解析值与原始输入不一致: {line!r}",
                    "脚本内部序列化异常，请汇报至根系统治理流程。",
                )
            break
    else:
        raise _error("capsule.yaml 缺 topic 字段", "脚本内部模板异常，请汇报至根系统治理流程。")


def create_capsule(base_dir: Path, topic: str, task_id: str, mode: str) -> Path:
    """原子创建胶囊：临时目录完整生成 + 自检通过后一次性改名落位（R2-m2）。"""
    validate_inputs(topic, task_id)

    name = f"{date.today():%Y%m%d}_{topic}"
    target = base_dir / name
    base_dir.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise _error(
            f"目标胶囊已存在: {target}",
            "换一个未被占用的主题，或先处理/归档既有同名胶囊后重试（本工具遵循 no-clobber，不覆盖既有目录）。",
        )

    today = date.today().isoformat()
    instantiated = ", ".join(_STAGE_DISPLAY[s] for s in MODE_STAGES[mode])

    tmp = Path(tempfile.mkdtemp(prefix=f".{name}.tmp-", dir=base_dir))
    try:
        for stage in MODE_STAGES[mode]:
            filename = (
                f"04_Spec_{task_id}.md" if stage == "04" else stage
            )
            if filename == "07_验收报告.md":
                content = _acceptance_template(topic, today, task_id)
            else:
                content = _stage_template(filename, topic, today, task_id)
            (tmp / filename).write_text(content, encoding="utf-8")
        (tmp / "capsule.yaml").write_text(
            MANIFEST_TEMPLATE.format(
                id=task_id, topic_q=_yaml_str(topic), mode=mode, date=today, instantiated=instantiated
            ),
            encoding="utf-8",
        )
        _self_check(tmp, task_id, topic)
        os.replace(tmp, target)  # 同文件系统原子改名；target 不存在，必然成功
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 Entropaxis 事务胶囊（原子、自检、no-clobber）")
    parser.add_argument("target_dir", type=Path, help="胶囊放置的父级目录")
    parser.add_argument("topic", type=str, help="胶囊中文主题（如：智能质检）")
    parser.add_argument("--id", default="Tech-1", help="Task ID（默认 Tech-1）")
    parser.add_argument(
        "--mode",
        choices=["full", "light", "research"],
        default="full",
        help="full=01/02/03/04/07 · light=01/02/04/07 · research=仅 01（审计报告均由「审计」指令按需创建）",
    )
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 格式输出结果")
    args = parser.parse_args()
    try:
        created = create_capsule(args.target_dir, args.topic, args.id, args.mode)
    except CapsuleInitError as err:
        print(str(err), file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001 - CLI 边界统一报告意外系统异常
        print(f"❌ 意外系统异常: {err}\n👉 修复建议: 请检查目标目录权限与磁盘空间，或汇报至根系统治理流程。", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(
            {
                "status": "success",
                "target": str(created),
                "lifecycle": "draft",
                "mode": args.mode,
                "id": args.id,
                "topic": args.topic,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"✨ 事务胶囊初始化成功（lifecycle: draft）: {created}")
        print("   下一步：编写 01/02 → 触发「审计」出具 05_审计报告（真实指纹）→ 定稿后进入实施")
    return 0


if __name__ == "__main__":
    sys.exit(main())
