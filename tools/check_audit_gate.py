#!/usr/bin/env python3
"""审计报告 Critical 问题关闭硬门禁 (Audit Critical-Closure Gate).

把《角色协作.md》「问题终态三分」——"Critical 只能由外置 Reviewer 复核关闭；
用户显式风险接受走 waived_by_user，且不得记为 closed"——从纯文字约束转成
可执行的机械阻断。

两种用法：
  只读复核（兼容旧用法）：
    python3 .system/tools/check_audit_gate.py <审计报告路径>
  原子校验并提交（拟写入的关闭后状态一次性校验+落盘，closed 与 waived_by_user 均须过此关）：
    python3 .system/tools/check_audit_gate.py --candidate <候选报告路径> --commit <目标路径>

字段契约见 .system/schemas/audit_report.schema.json。schema_version 缺失
按旧 independence 契约只读解析；**只要 Front Matter 出现 schema_version 键**（无论是否
能解析为合法整数）即视为声明使用新契约，全量校验该契约——不会因为版本号写错就静默
退回旧契约放行（第 4 轮外置复核实测抓到的 fail-open）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
import unicodedata

try:
    from tools import validate_schema as vs
except ImportError:  # 以脚本方式直接运行时 tools/ 自身在 sys.path 上
    import validate_schema as vs

FRONT_MATTER_PATTERN = re.compile(r"^[\s\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff]*---\n(.*?)\n---", re.DOTALL)

# 允许取值的唯一机器真源是 schema 顶层 level_enum/status_enum，此处直接读取，不再
# 在代码里另存一份硬编码——三轮外置复核先后抓到 schema_version 判别、冒号前空格、
# 级别词大小写三处"正则比统一解析器/契约更脆弱"的漂移（R6-M3 指出旧结构的
# pattern/enum 从未被本文件读取，等于两份定义各自维护，只会继续分裂）。
_AUDIT_SCHEMA = vs.load_schema("audit_report")
LEVEL_ENUM = tuple(_AUDIT_SCHEMA["level_enum"])
V2_STATUS_ENUM = tuple(_AUDIT_SCHEMA["status_enum"])

# 候选问题区块：按"边界行"切分正文，边界行 = 标题行 / --- 分隔线 / 空行（包括只有
# 空白的行）。第 7 轮把边界从"级别:"本身改成了标题，第 8 轮外置复核又实测抓到：
# 只要整份报告不含任何标题（或用缩进 ATX/Setext 等本正则不认的标题写法），标题
# 从未出现过，_iter_candidate_sections() 就一次都不会产出候选段，字段齐全的
# Critical+closed 直接放行（R8-C1）。空行边界更基础、更难绕过——markdown 里没有
# 空行分隔的连续文本天然属于同一段落，这是比"标题"更贴近排版事实本身的信号，
# 不依赖某一种具体的标题语法。用 re.split 切分：分隔符（标题/---/空行）本身不
# 进入任何分段，段内只要出现级别/ID/状态三者之一即视为候选问题段。
_SECTION_BOUNDARY_SPLIT = re.compile(r"^(#{1,6}\s.*|-{3,}\s*|[ \t]*)$", re.MULTILINE)
# 问题式标题锚：`### 问题 1` / `### Issue 2` 形态的标题本身即声明"此处是问题
# 记录"。第 11 轮第二批实测：字段行全部藏进 HTML 注释被规范化剥除后，段内
# 不再有任何字段信号，若无标题锚点该段将整体逃过校验——字段被剥空恰恰必须
# 报缺标注，而不是从校验范围消失。
_CANDIDATE_HEADING = re.compile(r"^\s*#{1,6}\s*(?:问题|issue)\s*\d+", re.IGNORECASE)
LEVEL_LINE_PATTERN = re.compile(r"^\s*级别\s*[:：]\s*(\S+)\s*$", re.MULTILINE)
ID_LINE_PATTERN = re.compile(r"^\s*ID\s*[:：]\s*(\S+)\s*$", re.MULTILINE)
STATUS_LINE_PATTERN = re.compile(r"^\s*状态\s*[:：]\s*(\S+)\s*$", re.MULTILINE)
# 第 10 轮外置复核（C-2 补核）抓到"恰好一次"的计数缺口：带取值约束的正则
# （\S+、[0-9a-f]{64}）数出来的是"合法取值的个数"，不是"字段行的出现次数"——
# 重复字段行只要第二个取值非法（空值、大写哈希、行尾空置），就从 findall()
# 中消失，一行合法 + 一行非法重复 = 计数 1，恰好一次被"满足"。计数与取值
# 校验必须分离：计数用"只认字段名+冒号"的出现模式（取值任意，含空），
# 取值合法性另由上面的取值模式单独校验后给出独立报错。
LEVEL_FIELD_PATTERN = re.compile(r"^\s*级别\s*[:：]", re.MULTILINE)
ID_FIELD_PATTERN = re.compile(r"^\s*ID\s*[:：]", re.MULTILINE)
STATUS_FIELD_PATTERN = re.compile(r"^\s*状态\s*[:：]", re.MULTILINE)
# 第 11 轮外置复核实测：三个字段名全部换成英文/繁体变体（Level:/Status:/級別:/狀態:）
# 时候选段完全不产出，字段齐全的 Critical+closed 直接放行。现实失效模式是生成端
# 语言漂移（模型切到英文/繁中输出标注），属可能半意外发生的形态——候选判定按语义
# 等价变体放宽，进入校验后仍要求规范中文标注，变体段以"标注出现 0 次"fail-closed。
# `ID` 本身即英文，无需变体；行首变体标注可能让引用了英文字段行的叙述段落进入候选
# 并报缺标注，属预期的保守失败（人工消歧），不是误放行。这是该类最后一个已知现实
# 变体；再出现新格式变体即触发第 9 轮预登记的升级路径——机器状态移入结构化
# sidecar，不再补正则。
_CANDIDATE_LABEL_VARIANTS = re.compile(
    r"^[ \t]*(?:级别|級別|level|状态|狀態|status)[ \t]*[:：]",
    re.IGNORECASE | re.MULTILINE,
)
LEGACY_CLOSED_PATTERN = re.compile(r"状态\s*[:：]\s*(已关闭|closed)", re.IGNORECASE)
# 旧契约专用的宽松块定位（不要求标题锚点）：历史冻结报告格式不一，有的问题条目
# 并非紧跟 markdown 标题。v2 的严格"恰好一次"架构只用于 v2 校验，不下沉到这里，
# 避免收紧旧契约读取造成兼容回归（SECTION_PATTERN 曾在此处直接复用，导致无标题
# 的旧格式测得 find_closed_critical_blocks() 返回空——单测已实测抓到该回归）。
LEGACY_ISSUE_BLOCK_PATTERN = re.compile(
    r"(级别\s*[:：]\s*\S+.*?)(?=\n#{1,6}\s|\n---|\Z)", re.DOTALL
)

# critical_ack 确认块：### critical_ack <问题ID> 后跟 target_sha256/确认事件/适用范围 三行。
# ID 允许紧跟一个可选冒号（`### critical_ack C-1:` 与 `### critical_ack: C-1` 两种
# 常见笔误），捕获后统一去掉首尾冒号，否则会和正文 `ID: C-1` 的纯净值比不上，
# 把本该通过的确认块误判为"未找到"（R6-m1，过度阻断而非安全绕过，但同样是
# 契约不一致）。
ACK_BLOCK_PATTERN = re.compile(
    r"###\s*critical_ack\s*[:：]?\s*(\S+?):?\s*\n(.*?)(?=\n#{1,3}\s|\Z)", re.DOTALL
)


def _front_matter_text(text: str) -> str | None:
    match = FRONT_MATTER_PATTERN.search(text)
    return match.group(1) if match else None


def parse_front_matter(text: str) -> dict:
    """复用 validate_schema 的扁平 Front Matter 解析；无档头返回空字典。"""
    return vs.parse_front_matter(text) or {}

def is_v3(text: str) -> bool:
    """schema_version 恰为 3：机器状态走 audit-state 围栏契约。"""
    return parse_front_matter(text).get("schema_version") == 3


def is_v2(text: str) -> bool:
    """Front Matter 出现 schema_version 键即为 v2 意图，不要求其值合法——
    值是否合法由完整 schema 校验负责报错，不在此处静默降级。

    直接复用 parse_front_matter()（等价 validate_schema 的解析语义：按首个冒号切分、
    两侧 strip），不再用独立正则检测键是否存在——此前 `schema_version : 2`（冒号前带
    空格）能被 parse_front_matter 正确解析出键，却被本函数的严格正则判定为"键不存在"，
    致使整份 v2 报告被错误地当成旧契约放行（第 5 轮外置复核实测抓到的 fail-open）。
    """
    fm = parse_front_matter(text)
    return "schema_version" in fm and fm.get("schema_version") != 3


def extract_independence(text: str) -> str | None:
    """读取 Front Matter 中的 independence 声明（旧契约）；未声明返回 None。"""
    fm = parse_front_matter(text)
    val = fm.get("independence")
    return str(val).strip() if val is not None else None



def _fm_duplicate_key_issues(text: str) -> list[str]:
    """第 12 轮终验实测：FM 重复键被 dict 字面赋值静默取末值——先写
    `reviewer_mode: session` 再补一行 `reviewer_mode: external`，会话报告
    即获外置特权；重复 `schema_version: 3/2` 可令 v3 围栏被 v2 分支整体
    忽略（原子接口实测可写入）。重复键即歧义载荷，任何版本分支一律拒绝。"""
    fm_text = _front_matter_text(text)
    if fm_text is None:
        return []
    seen: dict[str, int] = {}
    for line in fm_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cuts = [i for i in (line.find(":"), line.find("：")) if i != -1]
        if not cuts:
            continue
        k = unicodedata.normalize(
            "NFKC", "".join(ch for ch in line[: min(cuts)] if unicodedata.category(ch) != "Cf")
        ).strip()
        seen[k] = seen.get(k, 0) + 1
    return [
        f"Front Matter 键 {k!r} 重复出现 {n} 次；重复键即歧义载荷，须恰好一次。"
        for k, n in seen.items()
        if n > 1
    ]

def _normalize_level(raw: str) -> str | None:
    """大小写不敏感匹配 LEVEL_ENUM，返回规范值；不合法返回 None（由调用方判定违规）。"""
    for canonical in LEVEL_ENUM:
        if raw.lower() == canonical.lower():
            return canonical
    return None

def _body_text(text: str) -> str:
    """剥离 Front Matter 后的正文。第 11 轮变体候选判定引入后，FM 行
    `status: active` 会命中 `^[ \t]*status...` 使整个档头被当成候选问题段
    报缺标注（正控实测误伤）——候选段迭代只作用于正文，档头由 schema 校验。"""
    m = FRONT_MATTER_PATTERN.search(text)
    return text[m.end():] if m else text

# 第 11 轮第二批外置复核发现的两个"隐形载体"类绕过，按类收口（规范化，非逐
# 变体枚举）：①HTML 注释包裹（`<!-- 级别: Critical -->`）让字段行在行首锚定下
# 不可见；②Unicode 零宽/方向等格式字符（U+200B 等）插入字段名，视觉不变而
# 模式失配。规范化只用于匹配，不回写文件；被揭示的字段行照常进入校验。
_HTML_COMMENT_COMPLETE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_COMMENT_UNTERMINATED = re.compile(r"<!--.*$", re.MULTILINE)


def _canonical(text: str) -> str:
    """剥 HTML 注释与全部 Cf 类格式字符（零宽/方向标记/BOM 等），再 NFKC 归一。"""
    text = _HTML_COMMENT_COMPLETE.sub("", text)
    text = _HTML_COMMENT_UNTERMINATED.sub("", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return unicodedata.normalize("NFKC", text)


def _iter_candidate_sections(text: str):
    """按空行/标题/---分隔线切分正文，产出"疑似问题记录"候选段。

    候选判据（任一命中）：
    ① 段内出现规范字段行（级别/ID/状态，只认字段名+冒号——第 11 轮实测行内
       重复让取值正则全不满足时整段消失，取值不合法恰恰更该进校验）；
    ② 段内出现语义等价变体字段行（級別/狀態/level/status，语言漂移兜底）；
    ③ 段前分隔符是问题式标题（`### 问题 N`）——标题本身即声明问题记录，
       字段全部藏进 HTML 注释被规范化剥空后，缺标注必须报出来（第 11 轮
       第二批实测），不得让该段从校验范围消失。
    纯叙述性段落（无任何上述信号）不作候选；不要求标题存在（R8-C1）。
    分隔符在捕获组中保留，用于回看每段之前的问题式标题。"""
    parts = _SECTION_BOUNDARY_SPLIT.split(text)
    for i in range(0, len(parts), 2):
        section = parts[i]
        separator = parts[i - 1] if i >= 2 else ""
        if separator and _CANDIDATE_HEADING.match(separator):
            yield section or "\n"
        elif (
            LEVEL_FIELD_PATTERN.search(section)
            or ID_FIELD_PATTERN.search(section)
            or STATUS_FIELD_PATTERN.search(section)
            or _CANDIDATE_LABEL_VARIANTS.search(section)
        ):
            yield section


def find_closed_critical_blocks(text: str) -> list[str]:
    """返回正文中已标注关闭（旧契约"已关闭"/"closed"）的 Critical 问题块。

    旧契约兼容路径，故意宽松、不做 v2 的"恰好一次"强校验，也不要求标题锚点
    （历史冻结报告格式不一）：块以"级别:"本身定位，只要该行规范化为 Critical，
    且块内任意位置能匹配到已关闭标注，即计入。
    """
    blocks = []
    for m in LEGACY_ISSUE_BLOCK_PATTERN.finditer(text):
        block = m.group(1)
        level_match = re.match(r"级别\s*[:：]\s*(\S+)", block)
        level = _normalize_level(level_match.group(1)) if level_match else None
        if level == "Critical" and LEGACY_CLOSED_PATTERN.search(block):
            blocks.append(block)
    return blocks


# 字段行计数与取值校验分离（第 10 轮实测：第二个 target_sha256 为大写哈希时，
# 旧取值正则把它从计数中抹掉，R8-C2 的"恰好一次"形同虚设）。
# 第 11 轮外置复核又抓到两处形态学缺口，全部收口为"行首锚定 + 列表项前缀 +
# 完整行取值"：①不锚定行首时，嵌套列表（`- 证据:` 下的缩进子项）与伪字段名
# 前缀（`not_target_sha256:`、`未确认事件:`）都会作为子串命中真字段名；②
# `([0-9a-f]{64})` 不锚定行尾时，65+ 位 hex 被贪婪截取为前 64 位并与 FM 指纹
# 匹配成功。确认块的合法形态只有一种：`- <字段名>: <值>` 独占一行。
_ACK_FIELD_OCCURRENCE = {
    "target_sha256": re.compile(r"^[ \t]*-[ \t]*target_sha256[ \t]*[:：]", re.MULTILINE),
    "确认事件": re.compile(r"^[ \t]*-[ \t]*确认事件[ \t]*[:：]", re.MULTILINE),
    "适用范围": re.compile(r"^[ \t]*-[ \t]*适用范围[ \t]*[:：]", re.MULTILINE),
}
_ACK_FIELD_VALUE = {
    "target_sha256": re.compile(r"^[ \t]*-[ \t]*target_sha256[ \t]*[:：][ \t]*([0-9a-f]{64})[ \t]*$", re.MULTILINE),
    "确认事件": re.compile(r"^[ \t]*-[ \t]*确认事件[ \t]*[:：][ \t]*(\S.*)$", re.MULTILINE),
    "适用范围": re.compile(r"^[ \t]*-[ \t]*适用范围[ \t]*[:：][ \t]*(\S.*)$", re.MULTILINE),
}


_ACK_FOREIGN_FIELD = re.compile(r"^[ \t]*-[ \t]*(?P<key>\S+?)[ \t]*[:：]", re.MULTILINE)
_ACK_ALLOWED_FIELDS = frozenset({"target_sha256", "确认事件", "适用范围"})


def find_ack_blocks(text: str) -> tuple[dict[str, dict[str, str] | None], list[str]]:
    """解析正文 critical_ack 确认块。

    返回 (acks, block_issues)：
      acks：{问题ID: {target_sha256, 确认事件, 适用范围}}，仅当该 ID 恰好有一个
        结构完整的确认块时才有值；重复确认块或字段重复的 ID 映射为 None，调用方
        必须把 None 当"未通过校验"处理，不能当"没有确认块"直接放行。
      block_issues：确认块自身的结构性违规说明（重复块/重复字段）。

    第 8 轮外置复核实测抓到两处（R8-C2）：①同一问题 ID 出现两个 critical_ack 块，
    旧实现用 dict 字面赋值，后一个静默覆盖前一个；②单个确认块内 target_sha256/
    确认事件/适用范围各字段用 re.search() 只取第一个匹配，重复字段时后一个（可能
    冲突的）取值被无声丢弃。两者都是"一份人工风险豁免不该由行序或覆盖顺序决定
    生效版本"的同一类问题，与 R7-C1 的状态行重复同源，此处一并纳入"恰好一次"。
    """
    raw: dict[str, list[str]] = {}
    for issue_id, body in ACK_BLOCK_PATTERN.findall(text):
        raw.setdefault(issue_id, []).append(body)

    acks: dict[str, dict[str, str] | None] = {}
    block_issues: list[str] = []
    for issue_id, bodies in raw.items():
        if len(bodies) > 1:
            block_issues.append(f"[{issue_id}] critical_ack 确认块出现 {len(bodies)} 次；同一问题只能有一个确认块。")
            acks[issue_id] = None
            continue
        body = bodies[0]
        fields: dict[str, str] = {}
        field_ok = True
        for key in _ACK_FIELD_OCCURRENCE:
            occ = len(_ACK_FIELD_OCCURRENCE[key].findall(body))
            if occ != 1:
                block_issues.append(
                    f"[{issue_id}] critical_ack.{key} 出现 {occ} 次，须恰好 1 次。"
                )
                field_ok = False
                continue
            values = _ACK_FIELD_VALUE[key].findall(body)
            if len(values) != 1:
                block_issues.append(
                    f"[{issue_id}] critical_ack.{key} 字段行唯一但取值为空或不符合格式。"
                )
                field_ok = False
                continue
            fields[key] = values[0].strip()
        # 第 11 轮外置复核实测：嵌套列表的缩进子项在行形态上与直接字段无法区分
        # （`- 证据:` 下的 `  - target_sha256: …` 同样满足行首锚定）。从父键侧识别：
        # 确认块内只允许三个直接字段，出现任何其他 `键:` 形态的列表项即为嵌套/污染。
        for m in _ACK_FOREIGN_FIELD.finditer(body):
            if m.group("key") not in _ACK_ALLOWED_FIELDS:
                block_issues.append(
                    f"[{issue_id}] critical_ack 内存在未定义字段 {m.group('key')!r}；"
                    "确认块只允许 target_sha256/确认事件/适用范围 三个直接字段，嵌套子项整体拒绝。"
                )
                field_ok = False
        acks[issue_id] = fields if field_ok else None
    return acks, block_issues


def check_report_legacy(text: str) -> list[str]:
    """无 schema_version 键：旧 independence 契约（会话内降级不可关闭 Critical）。"""
    independence = extract_independence(text)
    closed = find_closed_critical_blocks(text)
    if not closed:
        return []
    if not independence:
        return [
            f"检测到 {len(closed)} 处 Critical 问题标记为已关闭，但 Front Matter 未声明 "
            "independence。审计独立性无法核验的报告不得关闭 Critical。"
        ]
    if independence.startswith("session-internal"):
        return [
            f"independence={independence!r} 属于会话内降级，但检测到 {len(closed)} 处 Critical "
            "问题标记为已关闭；会话内自评不可关闭 Critical，必须由外置 reviewer 复核或转人工仲裁。"
        ]
    return []


def check_report_v2(text: str) -> list[str]:
    """Front Matter 含 schema_version 键：完整 v2 契约校验，不允许部分校验后放行。"""
    issues: list[str] = []
    fm = parse_front_matter(text)

    # 1) 完整 Front Matter 结构校验（必填字段、类型、枚举、条件必填）——
    #    此前只做了零星正则抽取，缺字段/非法枚举/畸形版本号全部悄悄放行。
    schema = vs.load_schema("audit_report")["properties"]["front_matter"]
    issues += [f"Front Matter {e}" for e in vs.validate(fm, schema)]

    # 2) v2 报告禁止再写已退役的 independence 字段（generic validator 不表达"字段互斥"）。
    if "independence" in fm:
        issues.append("schema_version>=2 的报告不得再声明 independence（已退役字段，只供历史报告只读兼容）。")

    reviewer_mode = fm.get("reviewer_mode")
    target_sha256 = fm.get("target_sha256")
    acks, ack_block_issues = find_ack_blocks(text)
    issues += ack_block_issues
    seen_ids: dict[str, int] = {}

    # critical_ack 区块不参与问题候选段：其内的变体标注续行（如 status: ...）
    # 曾被误判为问题段报缺标注（第 11 轮第二批外置复核实测误阻断）；ack 块由
    # find_ack_blocks 单独校验。
    body = ACK_BLOCK_PATTERN.sub("\n\n", _body_text(text))
    for section in _iter_candidate_sections(body):
        header = section.splitlines()[0].strip().lstrip("#").strip() or "<无标题>"
        # 3) ID：字段行恰好一次（缺失/重复/空值重复都算），取值另校——
        #    计数取"字段名+冒号"的出现次数而非合法取值数（第 10 轮实测：一行合法
        id_fields = ID_FIELD_PATTERN.findall(section)
        if len(id_fields) != 1:
            issues.append(f"[{header}] `ID:` 标注出现 {len(id_fields)} 次，v2 契约要求恰好 1 次。")
            continue
        ids = ID_LINE_PATTERN.findall(section)
        if len(ids) != 1:
            issues.append(f"[{header}] `ID:` 字段行唯一但取值为空或格式不符。")
            continue
        issue_id = ids[0]
        seen_ids[issue_id] = seen_ids.get(issue_id, 0) + 1

        # 4) 级别：字段行恰好一次 + 取值枚举校验（R6-C3 定位与校验解耦 + R7-C1 恰好一次）。
        level_fields = LEVEL_FIELD_PATTERN.findall(section)
        if len(level_fields) != 1:
            issues.append(f"[{issue_id}] `级别:` 标注出现 {len(level_fields)} 次，v2 契约要求恰好 1 次。")
            continue
        levels = LEVEL_LINE_PATTERN.findall(section)
        if len(levels) != 1:
            issues.append(f"[{issue_id}] `级别:` 字段行唯一但取值为空或格式不符。")
            continue
        level = _normalize_level(levels[0])
        if level is None:
            issues.append(f"[{issue_id}] 级别={levels[0]!r} 不在枚举 {LEVEL_ENUM} 内。")
            continue

        # 5) 状态：字段行恰好一次 + 取值枚举校验；v2 只认 open/closed/waived_by_user，其余（含 challenged）一律违规。
        status_fields = STATUS_FIELD_PATTERN.findall(section)
        if len(status_fields) != 1:
            issues.append(f"[{issue_id}] `状态:` 标注出现 {len(status_fields)} 次，v2 契约要求恰好 1 次。")
            continue
        statuses = STATUS_LINE_PATTERN.findall(section)
        if len(statuses) != 1:
            issues.append(f"[{issue_id}] `状态:` 字段行唯一但取值为空或格式不符。")
            continue
        status = statuses[0]
        if status not in V2_STATUS_ENUM:
            issues.append(f"[{issue_id}] 状态={status!r} 不在 v2 枚举 {V2_STATUS_ENUM} 内。")
            continue

        if level == "Critical" and status == "closed":
            if reviewer_mode != "external":
                issues.append(
                    f"[{issue_id}] 状态=closed 但 reviewer_mode={reviewer_mode!r}；"
                    "会话内承载不可将 Critical 置为 closed，必须由外置 reviewer 复核。"
                )
        if status == "waived_by_user":
            ack = acks.get(issue_id)
            if not ack:
                issues.append(f"[{issue_id}] 状态=waived_by_user 但未找到对应 critical_ack 确认块。")
                continue
            missing = [k for k in ("target_sha256", "确认事件", "适用范围") if not ack.get(k)]
            if missing:
                issues.append(f"[{issue_id}] critical_ack 缺字段：{', '.join(missing)}。")
            elif ack.get("target_sha256") != target_sha256:
                issues.append(
                    f"[{issue_id}] critical_ack.target_sha256={ack.get('target_sha256')!r} "
                    f"与报告 target_sha256={target_sha256!r} 不一致，复核对象已变化。"
                )

    # 6) 问题 ID 唯一性：重复 ID 会让一个 critical_ack 确认块同时"覆盖"多条不同问题，
    #    破坏逐问题人工豁免绑定（R6-C4：两条不同 Critical 共用同一 ID 时，各自都能
    #    取到同一个确认块而被判定为已豁免）。
    for dup_id, count in seen_ids.items():
        if count > 1:
            issues.append(f"[{dup_id}] 问题 ID 在本报告内重复出现 {count} 次；事件内 ID 必须唯一。")
    return issues


# ---------- schema_version 3：audit-state 围栏契约（第 12 轮用户裁定重构） ----------

_AUDIT_STATE_FENCE = re.compile(r"```[ \t]*audit-state[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)


def _extract_audit_state(text: str) -> tuple[str | None, list[str]]:
    fences = _AUDIT_STATE_FENCE.findall(text)
    if len(fences) != 1:
        return None, [
            f"audit-state 围栏出现 {len(fences)} 次，schema_version>=3 的报告必须恰好包含 1 个"
            "（机器状态唯一真源，多块即歧义）。"
        ]
    return fences[0], []


def _reject_duplicate_keys(pairs):
    """json.loads object_pairs_hook：JSON 对象出现重复键即抛错。

    第 12 轮外置复核实测：`"status":"closed","status":"open"` 会被 Python
    静默采信最后一个值，前一个在人读渲染时可能被看到——重复键本身就是
    歧义载荷，按 fail-closed 拒绝，不做"取末值"的隐式裁决。"""
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"JSON 对象出现重复键 {key!r}")
        seen.add(key)
    return dict(pairs)

def check_report_v3(text: str) -> list[str]:
    """schema_version 3：机器状态唯一真源为正文内唯一 ```audit-state``` 围栏 JSON。

    第 12 轮裁定（连续 5 轮外置复核证明散文格式变体攻击面无界）：散文问题清单
    降级为人类叙事，不再被任何正则解析；门禁只消费围栏内 JSON——严格解析意味着
    任何篡改形态要么照常解析并受检，要么解析失败 fail-closed，"格式变体"这一
    攻击类整体消失。散文与围栏状态是否一致属人工审阅范畴，不再是机器契约。"""
    issues: list[str] = []
    fm = parse_front_matter(text)
    schema = vs.load_schema("audit_report")
    issues += [f"Front Matter {e}" for e in vs.validate(fm, schema["properties"]["front_matter"])]
    if "independence" in fm:
        issues.append("schema_version>=2 的报告不得再声明 independence（已退役字段，只供历史报告只读兼容）。")

    raw, fence_issues = _extract_audit_state(text)
    issues += fence_issues
    if raw is None:
        return issues
    try:
        state = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        issues.append(f"audit-state 围栏不是合法 JSON（或含重复键）：{exc}（严格解析 fail-closed，不接受修复性解释）。")
        return issues
    issues += [f"audit-state {e}" for e in vs.validate(state, schema["properties"]["sidecar_format"])]
    if not isinstance(state, dict):
        # 第 12 轮第三批复核：顶层为 null/数组/字符串时类型错误已由上面记录，
        # 但不能继续走 state.get()——那会抛未捕获 AttributeError 违反本函数
        # "返回违规说明列表"的接口契约（阻断方向虽对，报错形态不对）。
        issues.append(f"audit-state 顶层必须是 JSON 对象，实得 {type(state).__name__}。")
        return issues

    reviewer_mode = fm.get("reviewer_mode")
    target_sha256 = fm.get("target_sha256")
    seen_ids: dict[str, int] = {}
    raw_issues = state.get("issues")
    if not isinstance(raw_issues, list):
        raw_issues = []  # 类型错误已由 schema 校验记录，这里只防迭代异常
    raw_acks = state.get("critical_acks")
    if not isinstance(raw_acks, list):
        raw_acks = []
    for item in raw_issues:
        if not isinstance(item, dict):
            issues.append(f"audit-state issues 元素必须是对象，实得 {type(item).__name__}。")
            continue
        issue_id, level, status = item.get("id"), item.get("level"), item.get("status")
        if issue_id:
            seen_ids[issue_id] = seen_ids.get(issue_id, 0) + 1
        if level is not None and level not in LEVEL_ENUM:
            issues.append(f"[{issue_id}] level={level!r} 不在枚举 {LEVEL_ENUM} 内。")
        if status is not None and status not in V2_STATUS_ENUM:
            issues.append(f"[{issue_id}] status={status!r} 不在枚举 {V2_STATUS_ENUM} 内。")
        if level == "Critical" and status == "closed" and reviewer_mode != "external":
            issues.append(
                f"[{issue_id}] status=closed 但 reviewer_mode={reviewer_mode!r}；"
                "会话内承载不可将 Critical 置为 closed，必须由外置 reviewer 复核。"
            )
    for dup_id, count in seen_ids.items():
        if count > 1:
            issues.append(f"[{dup_id}] 问题 ID 在 audit-state 内重复出现 {count} 次；事件内 ID 必须唯一。")

    acks: dict[str, dict] = {}
    for ack in raw_acks:
        if not isinstance(ack, dict):
            issues.append(f"audit-state critical_acks 元素必须是对象，实得 {type(ack).__name__}。")
            continue
        aid = ack.get("issue_id")
        if aid in acks:
            issues.append(f"[{aid}] critical_ack 出现多个，同一问题只能有一个确认。")
            continue
        acks[aid] = ack
    for aid in acks:
        if aid not in seen_ids:
            issues.append(f"[{aid}] critical_ack 指向不存在的问题 ID。")
    for item in raw_issues:
        if isinstance(item, dict) and item.get("status") == "waived_by_user":
            ack = acks.get(item.get("id"))
            if not ack:
                issues.append(f"[{item.get('id')}] status=waived_by_user 但 critical_acks 中无对应确认。")
            elif ack.get("target_sha256") != target_sha256:
                issues.append(
                    f"[{item.get('id')}] critical_ack.target_sha256 与报告 target_sha256 不一致，复核对象已变化。"
                )
    return issues


def check_report(text: str) -> list[str]:
    """对审计报告全文做门禁核验，返回违规说明列表（空列表即通过）。

    schema_version 3：机器状态走 audit-state 围栏 JSON，严格解析、不做任何
    散文规范化。v2/legacy：匹配前先做文本规范化（剥 HTML 注释与 Cf 格式
    字符、NFKC 归一）——隐形载体类变体（注释包裹、零宽字符注入）视觉不变
    而模式失配，属整类收口；规范化只用于匹配，不回写文件。"""
    # FM 重复键检测对三个分支统一前置：重复键被 dict 静默取末值，可让会话
    # 报告凭第二行 `reviewer_mode: external` 获得外置特权，或令 v3 围栏被
    # v2 分支整体忽略（第 12 轮终验实测，原子接口可被写入）。
    dup = _fm_duplicate_key_issues(text)
    # 含 audit-state 围栏的报告必然是 v3 意图：Front Matter 被破坏（前导
    # 隐形字符、无法解析）时不允许静默落入超宽 legacy 分支无视围栏状态——
    # 第 12 轮第六批实测的整类降级绕过在此关死。
    if _front_matter_text(text) is None and "audit-state" in text:
        return ["报告包含 audit-state 围栏但 Front Matter 无法解析；不得借 FM 破坏降级到 legacy 分支无视机器状态。"]
    if is_v3(text):
        return dup + check_report_v3(text)
    canonical = _canonical(text)
    return dup + (check_report_v2(canonical) if is_v2(canonical) else check_report_legacy(canonical))


def _find_workspace_root(start: Path) -> Path | None:
    """从 start 向上找到以 `.system` 为子目录的工作区根；找不到返回 None。"""
    current = start.resolve()
    for _ in range(8):
        if (current / ".system").is_dir():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


def check_candidate_commit(candidate_text: str, commit_path: Path) -> list[str]:
    """--candidate/--commit 原子接口的额外校验：target_sha256 与目标受审文件实测哈希一致。

    先跑 check_report()（含完整 Front Matter 校验），字段本身缺失/非法已在那一步拦截；
    这里只做 check_report() 管不到的"文件系统事实校验"——target_path 是否真实存在、
    其内容哈希是否与声明一致。

    **本接口只接受 v2/v3 候选**：legacy（无 schema_version 键）候选只走 independence
    的粗粒度检查，没有指纹绑定与完整字段校验，若放行会让"新写入的关闭动作"伪装成
    旧格式绕过门禁（R6-C1：一份不含 schema_version、伪造 independence 的候选能
    直接通过原子写入）。旧报告仍可通过只读的 check_report()/main() 单路径模式核验，
    只是不能再经这个"拟写入关闭状态"的原子入口。
    """
    if not (is_v2(candidate_text) or is_v3(candidate_text)):
        return ["--candidate/--commit 原子接口只接受 schema_version 2/3 的候选；legacy 格式不得用于写入新的关闭/豁免状态。"]
    issues = check_report(candidate_text)
    fm = parse_front_matter(candidate_text)
    target_path, target_sha256 = fm.get("target_path"), fm.get("target_sha256")
    if not target_path or not target_sha256:
        return issues  # 缺字段已由 check_report() 的 schema 校验报出，这里不重复
    candidates = [commit_path.parent / target_path]
    workspace_root = _find_workspace_root(commit_path.parent)
    if workspace_root:
        candidates.append(workspace_root / target_path)
    actual_file = next((p for p in candidates if p.is_file()), None)
    if actual_file is None:
        issues.append(f"target_path 指向的受审文件不存在：{target_path}")
    else:
        actual_hash = hashlib.sha256(actual_file.read_bytes()).hexdigest()
        if actual_hash != target_sha256:
            issues.append(
                f"target_sha256={target_sha256!r} 与受审文件实测哈希 {actual_hash!r} 不一致；"
                "受审对象已变化，不得据此关闭问题。"
            )
    return issues


def atomic_write(path: Path, content: str) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
        tmp_path = Path(tmp) / path.name
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="审计报告 Critical 关闭门禁")
    parser.add_argument("path", nargs="?", help="只读模式：待校验的审计报告路径")
    parser.add_argument("--candidate", help="原子模式：候选报告路径（拟写入的关闭后状态）")
    parser.add_argument("--commit", help="原子模式：校验通过后写入的目标路径")
    args = parser.parse_args()

    if args.candidate or args.commit:
        if not (args.candidate and args.commit):
            print("用法: --candidate 与 --commit 必须同时提供", file=sys.stderr)
            return 2
        candidate_path = Path(args.candidate)
        commit_path = Path(args.commit)
        if not candidate_path.is_file():
            print(f"[候选报告不存在] {candidate_path}", file=sys.stderr)
            return 2
        candidate_text = candidate_path.read_text(encoding="utf-8")
        violations = check_candidate_commit(candidate_text, commit_path)
        if violations:
            for v in violations:
                print(f"[Critical违规关闭] {candidate_path}: {v}", file=sys.stderr)
            print(f"❌ 校验未通过，未写入 {commit_path}。", file=sys.stderr)
            return 1
        atomic_write(commit_path, candidate_text)
        print(f"✅ 校验通过，已原子写入 {commit_path}。")
        return 0

    if not args.path:
        print("用法: python3 check_audit_gate.py <审计报告路径>", file=sys.stderr)
        print("  或: python3 check_audit_gate.py --candidate <候选> --commit <目标>", file=sys.stderr)
        return 2
    path = Path(args.path)
    if not path.is_file():
        print(f"[审计报告不存在] {path}", file=sys.stderr)
        return 2
    issues = check_report(path.read_text(encoding="utf-8"))
    if issues:
        for issue in issues:
            print(f"[Critical违规关闭] {path}: {issue}", file=sys.stderr)
        return 1
    print(f"✅ {path} 未发现会话内降级下的 Critical 违规关闭。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
