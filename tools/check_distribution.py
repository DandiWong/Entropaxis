#!/usr/bin/env python3
"""分发就绪核验：以**收件方视角**重建版本库内容并实跑初始化链路。

存在理由：其余门禁（unittest / lint_workspace）全部在"开发者本机、已初始化、`data/`
齐备、私有 Skill 在场"这一个环境状态上求值，而分发是另一个状态。同一份代码在两个状态
下会给出两个不同的真相——本工具补上第二个求值点。

它做四件本机门禁做不到的事：
  1. 用 `git ls-files` 重建收件方真正拿到的文件树（未跟踪文件物理存在 ≠ 会被分发）；
  2. 在该树上扫实体词、凭据特征串与家目录绝对路径（泄露只在分发面才成立）；
  3. **实际执行** bootstrap 并检查输出（静态编译检查看不见运行期缺陷）；
  4. 在该树上重跑 lint 与单测（本机全绿不代表新环境全绿）。

遵循 ApX 工具契约：纯标准库、行动导向错误、结构化输出、只读源工作区（写入全部发生在
临时目录，退出即销毁）。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from . import paths
except ImportError:
    import paths

HERE = Path(__file__).resolve().parent
SYSTEM_DIR = paths.SYSTEM_DIR
WORKSPACE = paths.WORKSPACE_ROOT

SUBPROCESS_TIMEOUT = 300

# 收件方工作区探针目录名：刻意取一个不会与任何真实工作区、也不会与注册表排除规则
# （repo/、Archive/、node_modules/ …）撞名的名字，用来求值"工作区非空"这个状态。
FOREIGN_DIR_PROBE = "收件方既有目录探针"

# 凭据特征串：只匹配"敏感名 = 长字面量"的赋值形态。
# 裸词 token/secret 在解析器代码里是普通变量名（如 `token = payload[0]`），
# 宽匹配会产生大量噪声，把真信号淹掉。
CREDENTIAL_PATTERN = re.compile(
    r"""(?ix)
    (api[_-]?key|apikey|secret|password|passwd|access[_-]?token|bearer)
    \s*[=:]\s*
    ["'][A-Za-z0-9_\-./+]{16,}["']
    """
)
PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
# 家目录绝对路径：只认三种家目录形态，避免误伤 /usr/share、/Applications 等系统路径
HOME_PATH_PATTERN = re.compile(r"(/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|[Cc]:\\\\?Users\\\\?)")

# 二进制与冻结上游资产不参与文本扫描
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".docx", ".xlsx", ".pptx", ".pyc", ".woff", ".woff2", ".ttf", ".otf",
}


class ToolError(Exception):
    """工具可恢复业务异常，包含行动导向修复指引。"""


def _git(system: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(system), *args],
        capture_output=True, text=True, timeout=30,
    )


def distribution_files(system: Path) -> list[str]:
    """收件方实际拿到的文件清单（版本库跟踪集）。"""
    try:
        proc = _git(system, "ls-files", "-z")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ToolError(
            f"❌ 无法执行 git: {exc}\n"
            f"👉 修复建议: 分发核验以版本库跟踪集为准，请确认已安装 git 且 {system} 是一个仓库。"
        ) from exc
    if proc.returncode != 0:
        raise ToolError(
            f"❌ {system} 不是 git 仓库（git ls-files 退出码 {proc.returncode}）。\n"
            f"👉 修复建议: 分发前必须先纳入版本库；未跟踪的文件不会被 clone/archive 带走。"
        )
    return [p for p in proc.stdout.split("\0") if p]


def pending_files(system: Path) -> list[str]:
    """存在于工作区、未被忽略、但尚未纳入版本库的文件。

    它们在本机可见可用，却不会随分发到达收件方——这是"我本机好好的"类故障的常见源头。
    """
    try:
        proc = _git(system, "ls-files", "--others", "--exclude-standard", "-z")
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.split("\0") if p]


def build_recipient_tree(system: Path, files: list[str], dest: Path) -> int:
    """把跟踪集复制成一棵独立的收件方工作区（保留可执行位）。"""
    system_dest = dest / paths.SYSTEM_DIRNAME
    copied = 0
    for rel in files:
        src = system / rel
        if not src.is_file():
            continue
        target = system_dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied += 1
    if copied == 0:
        raise ToolError(
            "❌ 版本库跟踪集为空，无法构建收件方视图。\n"
            "👉 修复建议: 确认当前分支已提交内容，或检查是否误在空仓库上执行。"
        )
    # 播一个与任何既有工作区都不同名的业务目录：收件方的工作区在接入本系统之前就已经
    # 有自己的目录结构。只放 .entropaxis/ 的空壳树跑不出"拿空注册表去判用户既有目录未注册"
    # 这类缺陷——首检红屏只在工作区非空时才成立。
    (dest / FOREIGN_DIR_PROBE).mkdir(parents=True, exist_ok=True)
    return copied


def _forbidden_terms(workspace: Path) -> list[str]:
    """复用 lint 的实体词表解析，避免同一份格式契约出现第二处实现。"""
    sys.path.insert(0, str(HERE))
    try:
        import lint_workspace
    except Exception:  # noqa: BLE001 - lint 不可用时降级为仅结构性扫描
        return []
    finally:
        if str(HERE) in sys.path:
            sys.path.remove(str(HERE))
    return lint_workspace._forbidden_bindings(workspace)


def scan_leaks(tree: Path, terms: list[str]) -> list[str]:
    """在收件方树上扫描实体词、凭据特征串、家目录绝对路径与工作区残渣。"""
    issues = []
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(tree)
        name = path.name
        if name in (".DS_Store",) or "__pycache__" in rel.parts:
            issues.append(f"[工作区残渣] {rel} 属本机缓存/系统文件，不应进入版本库。")
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lowered = text.lower()
        for term in terms:
            if term in lowered:
                issues.append(
                    f"[实体名泄露] {rel} 含禁用实体词「{term}」；"
                    "该文件会随版本库分发给每一位收件方。"
                )
        if CREDENTIAL_PATTERN.search(text) or PRIVATE_KEY_PATTERN.search(text):
            issues.append(
                f"[疑似凭据] {rel} 命中凭据特征串；凭据一律外置于 .entropaxis/data/credentials/，不得入库。"
            )
        if HOME_PATH_PATTERN.search(text):
            issues.append(
                f"[家目录绝对路径] {rel} 含开发者机器的家目录路径；收件方环境下必然失效。"
            )
    return issues


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT
    )


def run_init_chain(workspace: Path) -> tuple[list[str], list[str]]:
    """在收件方树上实跑 bootstrap → lint → 单测，返回 (阻断项, 建议项)。"""
    blocking, advisory = [], []

    boot = _run([sys.executable, f"{paths.SYSTEM_DIRNAME}/tools/bootstrap.py"], workspace)
    output = boot.stdout + boot.stderr
    if boot.returncode != 0:
        blocking.append(f"[初始化失败] bootstrap.py 退出码 {boot.returncode}；输出尾部：{output.strip()[-300:]}")
    # 退出码为 0 但输出里带失败标记，同样是缺陷：新人第一屏看到的就是这些字
    for marker in ("❌", "损坏", "Traceback"):
        if marker in output:
            blocking.append(
                f"[初始化告警] bootstrap.py 输出含「{marker}」；"
                "新工作区首次初始化不应出现失败或自相矛盾的提示。"
            )
    # 幂等重入：连跑两次不得出现新的失败标记
    again = _run([sys.executable, f"{paths.SYSTEM_DIRNAME}/tools/bootstrap.py"], workspace)
    if again.returncode != 0:
        blocking.append(f"[初始化不幂等] 第二次运行 bootstrap.py 退出码 {again.returncode}。")

    lint = _run([sys.executable, f"{paths.SYSTEM_DIRNAME}/tools/lint_workspace.py"], workspace)
    if lint.returncode != 0:
        failed = [ln.strip() for ln in lint.stdout.splitlines() if ln.strip().startswith("•")]
        blocking.append(
            f"[新环境体检失败] lint_workspace.py 退出码 {lint.returncode}；"
            f"阻断项：{'; '.join(failed) or '见完整输出'}"
        )
    advisory_count = sum(1 for ln in lint.stdout.splitlines() if "⚠️ 建议" in ln)
    if advisory_count:
        advisory.append(f"[新环境体检建议] 收件方首检有 {advisory_count} 类建议项（不阻断）。")

    tests = _run(
        [sys.executable, "-m", "unittest", "discover", "-s", f"{paths.SYSTEM_DIRNAME}/tests", "-t", paths.SYSTEM_DIRNAME],
        workspace,
    )
    if tests.returncode != 0:
        blocking.append(
            f"[新环境单测失败] 退出码 {tests.returncode}；尾部：{(tests.stderr or tests.stdout).strip()[-300:]}"
        )
    return blocking, advisory


def dangling_skill_routes(system: Path, tree: Path, files: list[str]) -> list[str]:
    """在收件方树上找出「引用了一个不随分发的 Skill」的文件。

    私有 Skill 不随分发本身是设计（《技能设计》2.2 自封装排除），只报它不构成缺陷；
    真正的缺陷是**版本库里留下了指向它的指令或规则**——收件方会读到一条无执行体的路由。
    因此只对"被跟踪文件实际引用"的私有 Skill 告警，干净自封装的一律不列
    （逐次列出私有能力清单本身也是一张"哪些能力是私有的"名单）。
    """
    if not (system / "skills").is_dir():
        return []
    local = {p.name for p in (system / "skills").iterdir() if p.is_dir()}
    shipped = {rel.split("/")[1] for rel in files if rel.startswith("skills/") and "/" in rel[7:]}
    private = local - shipped
    if not private:
        return []

    issues = []
    for path in sorted(tree.rglob("*")):
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in sorted(private):
            if name in text:
                issues.append(
                    f"[私有能力路由悬空] {path.relative_to(tree)} 引用了不随分发的 Skill「{name}」；"
                    "收件方会读到一条没有执行体的路由——把该指令与其规则一并内化进该 Skill。"
                )
    return issues


def check_distribution(workspace: Path = WORKSPACE, *, keep_tree: bool = False) -> dict:
    """核验分发就绪度，返回结构化结果字典。"""
    system = workspace / paths.SYSTEM_DIRNAME
    if not system.is_dir():
        raise ToolError(
            f"❌ 未找到控制面目录: {system}\n"
            f"👉 修复建议: 请在包含 {paths.SYSTEM_DIRNAME}/ 的工作区根目录下运行本工具。"
        )

    files = distribution_files(system)
    terms = _forbidden_terms(workspace)
    blocking, advisory = [], []
    if not terms:
        advisory.append(
            "[实体词表未声明] 未读到 .entropaxis/data/rules/零系统绑定词表.md，实体名扫描已降级停用；"
            "凭据与绝对路径扫描不受影响。"
        )

    tmpdir = tempfile.mkdtemp(prefix="entropaxis-dist-")
    recipient = Path(tmpdir)
    try:
        copied = build_recipient_tree(system, files, recipient)
        blocking += scan_leaks(recipient / paths.SYSTEM_DIRNAME, terms)
        advisory += dangling_skill_routes(system, recipient / paths.SYSTEM_DIRNAME, files)
        chain_blocking, chain_advisory = run_init_chain(recipient)
        blocking += chain_blocking
        advisory += chain_advisory
    finally:
        if not keep_tree:
            shutil.rmtree(tmpdir, ignore_errors=True)

    for rel in pending_files(system):
        advisory.append(f"[未入库] {rel} 未纳入版本库，收件方不会拿到；确认是否遗漏 git add。")

    return {
        "status": "fail" if blocking else "pass",
        "distributed_files": copied,
        "entity_terms": len(terms),
        "blocking": blocking,
        "advisory": advisory,
        "recipient_tree": tmpdir if keep_tree else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分发就绪核验：以收件方视角重建版本库内容并实跑初始化链路",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="以结构化 JSON 格式输出结果")
    parser.add_argument("--keep-tree", action="store_true", help="保留重建的收件方工作区以便人工排查")
    args = parser.parse_args()

    try:
        res = check_distribution(keep_tree=args.keep_tree)
    except ToolError as err:
        print(str(err), file=sys.stderr)
        return 1
    except Exception as err:  # noqa: BLE001
        print(
            f"❌ 意外系统异常: {err}\n👉 修复建议: 请检查环境权限或汇报至根系统治理流程。",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"分发文件数: {res['distributed_files']}")
        print(f"实体词表条目: {res['entity_terms']}")
        print(f"阻断项: {len(res['blocking'])}")
        for issue in res["blocking"]:
            print(f"   • {issue}")
        print(f"建议项: {len(res['advisory'])}")
        for issue in res["advisory"]:
            print(f"   • {issue}")
        if res["recipient_tree"]:
            print(f"收件方视图: {res['recipient_tree']}")
        print("结论: " + ("❌ 未就绪，禁止分发" if res["blocking"] else "✅ 分发就绪"))
    return 1 if res["blocking"] else 0


if __name__ == "__main__":
    sys.exit(main())
