"""内容关联证据：名称优先 → 文本次之 → 二进制兜底的三级读取阶梯与主题聚类。

只读本地文件：.md/.txt/.csv 等取头部文本，.docx 取 word/document.xml，.pdf 取内容流里的
字面字符串（尽力而为）。不外传、不落盘、不解压超限文件、不跟随软链接。

三级阶梯的用意：能用目录名和文件名判断的不读文件内容；名不明确才读轻量文本；文本仍
不明确才碰 pdf/docx。每一级都只用于产出候选与证据，人工确认前不写任何目录。
"""
from __future__ import annotations

import re
import zipfile
import zlib
from pathlib import Path

SKIP_DIRS = {"node_modules", "vendor", "dist", "build", "target", "__pycache__", "archive", "output"}
TEXT_SUFFIXES = {".md", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".rst", ".log"}
BINARY_SUFFIXES = {".pdf", ".docx"}

HEAD_BYTES = 2048
MAX_BINARY_BYTES = 8 * 1024 * 1024
MAX_FILES = 200
MAX_BINARY_FILES = 20
MAX_DEPTH = 3

# 虚词不进 token：否则「的项目」这类前缀会在不同文档间凑出假相似。
_PARTICLES = set("的了和与及在是把被从到向给对为以于就都也")
_STOP_SINGLE = set("的了和与及在是我这那有个为之其将就都也很更最要会能可做说让把被从到向给该本次或并等它他不中上下")

_CJK = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z][a-z0-9_+-]{1,}")
_LITERAL = re.compile(rb"\((?:\\.|[^\\()])*\)")
_STREAM = re.compile(rb"stream\r?\n")
_ESCAPE = re.compile(rb"\\([()\\])")


def tokenize(text: str) -> set[str]:
    """中文按二元组、英文按单词取 token；虚词与单字停用词剔除。"""
    tokens: set[str] = set()
    for run in _CJK.findall(text):
        if len(run) == 1:
            if run not in _STOP_SINGLE:
                tokens.add(run)
            continue
        for index in range(len(run) - 1):
            pair = run[index:index + 2]
            if pair[0] in _PARTICLES or pair[1] in _PARTICLES:
                continue
            tokens.add(pair)
    for word in _WORD.findall(text.lower()):
        if word not in _STOP_SINGLE:
            tokens.add(word)
    return tokens


def read_head(path: Path) -> str:
    """取文件头部文本；读不到按空处理，不抛异常（损坏文件不该中断整轮扫描）。"""
    try:
        with path.open("rb") as handle:
            return handle.read(HEAD_BYTES).decode("utf-8", "ignore")
    except OSError:
        return ""


def read_docx_text(path: Path) -> str:
    """docx 是 zip，取 word/document.xml 去标签；输入先查大小、解压成员有界，非 docx 结构即报错。"""
    try:
        if path.stat().st_size > MAX_BINARY_BYTES:
            raise ValueError(f"docx 超出读取上限: {path.name}")
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith(".xml") and "document" in n]
            if not names:
                raise ValueError("docx 缺少 document.xml")
            with archive.open(sorted(names, key=len)[0]) as member:
                raw = member.read(MAX_BINARY_BYTES + 1)  # 解压到限即止：小压缩包大展开也拒（M-3）
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ValueError(f"docx 解析失败: {path.name}: {exc}") from exc
    if len(raw) > MAX_BINARY_BYTES:
        raise ValueError(f"docx XML 成员超出读取上限: {path.name}")
    return re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "ignore"))


def read_pdf_text(path: Path) -> str:
    """pdf 取内容流里的字面字符串；输入 stat 先行限长、解压输出有界，解析不出即报错。"""
    try:
        if path.stat().st_size > MAX_BINARY_BYTES:
            raise ValueError(f"pdf 超出读取上限: {path.name}")
        with path.open("rb") as handle:
            data = handle.read(MAX_BINARY_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"pdf 读取失败: {path.name}: {exc}") from exc
    if len(data) > MAX_BINARY_BYTES:
        raise ValueError(f"pdf 超出读取上限: {path.name}")
    chunks: list[bytes] = []
    for match in _STREAM.finditer(data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end == -1:
            continue
        raw = data[start:end]
        try:
            dec = zlib.decompressobj()
            raw = dec.decompress(raw, MAX_BINARY_BYTES + 1)
            if len(raw) > MAX_BINARY_BYTES or dec.unconsumed_tail:
                raise ValueError(f"pdf 流解压超出上限: {path.name}")
        except zlib.error:
            pass
        for literal in _LITERAL.findall(raw):
            chunks.append(_ESCAPE.sub(rb"\1", literal[1:-1]))
        if sum(len(c) for c in chunks) > HEAD_BYTES:
            break
    if not chunks:
        raise ValueError(f"pdf 未解析出文本: {path.name}")
    return b" ".join(chunks).decode("utf-8", "ignore")


def read_binary_text(path: Path) -> str:
    """按后缀分派二进制读取；不支持的格式返回空串。"""
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return read_docx_text(path)
    if suffix == ".pdf":
        return read_pdf_text(path)
    return ""


class DirEvidence:
    """一个目录的内容证据：名称 token + 按需升级的文件内容 token。

    tokens(1) 只用名字；tokens(2) 追加轻量文本；tokens(3) 追加 pdf/docx。
    读取按需触发并缓存，不会因为扫一遍工作区就把所有文件读进内存。
    """

    def __init__(self, directory: Path, root: Path, skip=None):
        self.directory = directory
        self.root = root
        self.files: list[Path] = []
        self.errors: list[str] = []
        self.related_to: dict[str, dict] = {}
        self.name_tokens: set[str] = set()
        self._cache: dict[int, set[str]] = {}
        self._skip = skip  # callable(entry)->bool：registry 排除/归属边界外的材料不算本目录证据（M-2）
        self.skipped_by_boundary = 0
        self.binary_ok = 0
        self._collect()

    def relative(self) -> str:
        try:
            return self.directory.relative_to(self.root).as_posix()
        except ValueError:
            return self.directory.name

    def _collect(self) -> None:
        self.name_tokens |= tokenize(self.directory.name)
        stack: list[tuple[Path, int]] = [(self.directory, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = sorted(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                if self._skip is not None and self._skip(entry):
                    self.skipped_by_boundary += 1
                    continue
                if entry.is_dir():
                    if depth >= MAX_DEPTH or entry.name.lower() in SKIP_DIRS:
                        continue
                    self.name_tokens |= tokenize(entry.name)
                    stack.append((entry, depth + 1))
                elif entry.is_file():
                    if len(self.files) >= MAX_FILES:
                        continue
                    self.files.append(entry)
                    self.name_tokens |= tokenize(entry.stem)

    def tokens(self, tier: int) -> set[str]:
        if tier in self._cache:
            return self._cache[tier]
        tokens = set(self.name_tokens)
        if tier >= 2:
            for path in self.files:
                if path.suffix.lower() in TEXT_SUFFIXES:
                    tokens |= tokenize(read_head(path))
        if tier >= 3:
            binaries = [path for path in self.files if path.suffix.lower() in BINARY_SUFFIXES]
            for path in binaries[:MAX_BINARY_FILES]:
                try:
                    tokens |= tokenize(read_binary_text(path))
                    self.binary_ok += 1
                except ValueError as exc:
                    self.errors.append(str(exc))
        self._cache[tier] = tokens
        return tokens

    def topic(self, limit: int = 6) -> list[str]:
        """取名里最有信息量的几个 token：短目录名按字符两两拼，长名按出现顺序。"""
        return sorted(self.name_tokens, key=lambda t: (-len(t), t))[:limit]

    def insufficient(self) -> list[str]:
        """内容只落在二进制且全部解析失败/超限时返回失败身份；内容主张须降级为证据不足（M-4）。"""
        self.tokens(3)
        if any(p.suffix.lower() in TEXT_SUFFIXES for p in self.files) or self.binary_ok or not self.errors:
            return []
        return list(self.errors)


def similarity(left: set[str], right: set[str]) -> tuple[float, set[str]]:
    if not left or not right:
        return 0.0, set()
    shared = left & right
    return len(shared) / len(left | right), shared


def related(left: DirEvidence, right: DirEvidence, threshold: float = 0.30,
            min_shared: int = 3) -> dict | None:
    """两级以上都没坐实就算无关；返回命中的层级与共享词，供人复核。"""
    for tier, label in ((1, "名称"), (2, "文本"), (3, "文档")):
        score, shared = similarity(left.tokens(tier), right.tokens(tier))
        if score >= threshold and len(shared) >= min_shared:
            return {"tier": tier, "label": label, "score": round(score, 2),
                    "shared": sorted(shared)}
    return None


def cluster(evidences: list[DirEvidence], threshold: float = 0.30,
            min_shared: int = 3) -> list[list[DirEvidence]]:
    """按两两相关关系做连通分量聚类；关联证据记在每个成员上。"""
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for index, left in enumerate(evidences):
        find(left.relative())
        for right in evidences[index + 1:]:
            hit = related(left, right, threshold, min_shared)
            if not hit:
                continue
            left.related_to[right.relative()] = hit
            right.related_to[left.relative()] = hit
            union(left.relative(), right.relative())
    buckets: dict[str, list[DirEvidence]] = {}
    for evidence in evidences:
        buckets.setdefault(find(evidence.relative()), []).append(evidence)
    return [sorted(bucket, key=lambda e: e.relative()) for bucket in buckets.values()]
