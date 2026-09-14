"""控制面与实例面的路径单一真源。任何工具不得再自行推导层级或硬编码目录名。"""
from pathlib import Path

SYSTEM_DIRNAME = ".entropaxis"          # 安装器/收件方重建/门禁排除项使用的字面名
LEGACY_SYSTEM_DIRNAME = ".system"       # 仅供残留检测：旧版遗留，或写入方未按契约在根目录误建
LEGACY_DATA_DIRNAME = ".data"           # 同上

TOOLS_DIR = Path(__file__).resolve().parent
SYSTEM_DIR = TOOLS_DIR.parent           # .entropaxis/
WORKSPACE_ROOT = SYSTEM_DIR.parent      # 工作区根
DATA_DIR = SYSTEM_DIR / "data"


def legacy_layout_present(workspace_root: Path = WORKSPACE_ROOT) -> list[str]:
    """检测工作区根目录下是否存在旧布局残留（.system/ 或 .data/）。

    两种成因（旧版本升级遗留 / 某写入方未按契约在根目录误建）在文件系统上
    现场同构，无法区分，因此不区分来源，只报现象并给出统一迁移指引。
    """
    found = []
    for name in (LEGACY_SYSTEM_DIRNAME, LEGACY_DATA_DIRNAME):
        if (workspace_root / name).is_dir():
            found.append(name)
    return found
