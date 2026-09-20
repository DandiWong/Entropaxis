#!/usr/bin/env python3
"""调度回执签发与核验 —— 门禁的信任根。

真源定义: rules/角色协作.md「调度优先序与降级契约」真实留痕条款
回执字段的唯一真源是本文件的 issue()/verify()——曾声明 schemas/dispatch_receipt.schema.json，
但那个文件从未存在过，读者会以为回执格式已被契约管起来（复核 m-02）。回执只在本模块内
签发与核验，没有第二个消费方，不另立 schema；真要外部消费再按 schemas/ 的惯例补。

存在的理由（外置审计 C-03）：
    此前承载信息（`carrier` / `reviewer_mode`）盖在交付物 Front Matter 里，而 Front Matter
    与模型同权限可编辑。实测伪造 `carrier: forged-profile` + 匹配目标哈希 + 关闭 Critical，
    门禁全过。也就是说「第三方可核验」当时并不成立：门禁在核对一份被审对象自己写的声明。

设计不变量:
  1. **信任根在工作目录之外**：回执落在 data/receipts/，不在胶囊内，被调度角色的 --cwd
     够不着；交付物只携带 receipt_id，本身不再是承载事实的载体。
  2. **签发者唯一**：只有 dispatch_role.py 在真实执行完成后签发；HMAC 密钥在
     data/credentials/ 且不随版本库分发，模型改了交付物也伪造不出匹配的 MAC。
  3. **绑定四元组**：角色 + argv 指纹 + 交付物指纹 + 依据指纹。交付物被改一个字节，
     核验即失配——回执证明的是「这一份内容由这条命令产出」，不是「某次调度发生过」。
  4. **缺回执不等于违规**：历史交付物没有 receipt_id，按「空态即初始态」只报未验证，
     不判失败；但声称外置承载却拿不出回执的，一律判违规。

密钥缺失时签发降级为不签（记 unsigned），核验相应报「未签名」——留痕不得反过来阻断被
留痕的动作，但也绝不把未签名当作已验证。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import paths
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore

SYSTEM_ROOT = paths.SYSTEM_DIR
RECEIPT_DIR = SYSTEM_ROOT / "data" / "receipts"
KEY_FILE = SYSTEM_ROOT / "data" / "credentials" / "dispatch_receipt.key"
UNSIGNED = "unsigned"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")





def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def content_sha256(path: Path) -> str:
    """交付物内容指纹：文件取内容哈希，目录取「相对路径 + 各文件指纹」的有序摘要。

    目录型交付物（Designer/Reporter 的原型与汇报容器）此前在签发端落空字符串，核验端
    又只在 `is_file()` 时比对——回执对目录只证明"签发过"，改了目录里的文件照样核验通过
    （复核 M-08）。签发与核验共用本函数，两端对称重算。
    """
    if path.is_file():
        return _sha256_file(path)
    entries = sorted((str(p.relative_to(path)), _sha256_file(p)) for p in path.rglob("*") if p.is_file())
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()


def body_sha256(path_or_text: Path | str) -> str:
    """对交付物正文（Front Matter 之后）取指纹。

    签发发生在盖章之前（盖章需要 receipt_id），若绑定整文件哈希，回执与落盘文件必然
    不等，核验永远失配。Front Matter 是承载元数据的容器、由工具改写；正文才是角色的
    产出本体——绑定正文既能捕获内容篡改，又不与盖章相互矛盾。
    """
    import re as _re
    text = path_or_text if isinstance(path_or_text, str) else path_or_text.read_text(encoding="utf-8", errors="replace")
    m = _re.match(r"\A---\n.*?\n---\n", text, _re.DOTALL)
    return hashlib.sha256((text[m.end():] if m else text).encode("utf-8")).hexdigest()


def load_key(create: bool = False) -> bytes | None:
    """读取签名密钥；create=True 时缺失即生成（0600，仅本机，不随版本库分发）。"""
    if KEY_FILE.is_file():
        data = KEY_FILE.read_bytes().strip()
        return data or None
    if not create:
        return None
    try:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_hex(32).encode()
        fd, tmp = tempfile.mkstemp(dir=KEY_FILE.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        os.chmod(tmp, 0o600)
        os.replace(tmp, KEY_FILE)
        return key
    except OSError:
        return None


def _payload(role: str, argv_sha256: str, deliverable_sha256: str, target_sha256: str,
             carrier: str, issued_at: str) -> str:
    """签名覆盖的字段集合。顺序固定，用 JSON 规范化避免拼接歧义。"""
    return json.dumps({
        "role": role, "argv_sha256": argv_sha256, "deliverable_sha256": deliverable_sha256,
        "target_sha256": target_sha256, "carrier": carrier, "issued_at": issued_at,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def issue(role: str, argv_sha256: str, deliverable: Path | None, target: Path | None,
          carrier: str, carrier_ref: str = "") -> dict[str, Any]:
    """签发一份回执并落盘到工作目录之外，返回含 receipt_id 的记录。"""
    issued_at = _now()
    if deliverable and deliverable.is_file():
        d_sha = body_sha256(deliverable) if deliverable.suffix == ".md" else _sha256_file(deliverable)
    elif deliverable and deliverable.is_dir():
        d_sha = content_sha256(deliverable)  # 目录型：绑内容，不绑"签发过"（复核 M-08）
    else:
        d_sha = ""
    t_sha = _sha256_file(target) if target and target.is_file() else ""
    payload = _payload(role, argv_sha256, d_sha, t_sha, carrier, issued_at)
    key = load_key(create=True)
    mac = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest() if key else UNSIGNED
    receipt_id = f"rcpt-{hashlib.sha256(payload.encode()).hexdigest()[:16]}"
    record = {
        "receipt_id": receipt_id, "role": role, "carrier": carrier, "carrier_ref": carrier_ref,
        "argv_sha256": argv_sha256, "deliverable_sha256": d_sha, "target_sha256": t_sha,
        "deliverable": str(deliverable) if deliverable else "", "issued_at": issued_at, "mac": mac,
    }
    try:
        RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
        (RECEIPT_DIR / f"{receipt_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except OSError:
        pass  # 留痕失败不得阻断被留痕的动作
    return record


def verify(receipt_id: str, deliverable: Path | None = None, *, text: str | None = None,
           carrier: str | None = None, role: str | None = None,
           target_sha256: str | None = None) -> tuple[bool, str]:
    """核验回执：存在性 → MAC → 角色 → 承载一致 → 受审对象一致 → 与当前交付物正文绑定。

    返回 (通过, 说明)。"""
    if not receipt_id:
        return False, "未提供 receipt_id"
    path = RECEIPT_DIR / f"{receipt_id}.json"
    if not path.is_file():
        return False, f"回执不存在: {receipt_id}（交付物声称的调度未留下任何签发记录）"
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"回执无法解析: {exc}"
    payload = _payload(r.get("role", ""), r.get("argv_sha256", ""), r.get("deliverable_sha256", ""),
                       r.get("target_sha256", ""), r.get("carrier", ""), r.get("issued_at", ""))
    key = load_key()
    if r.get("mac") == UNSIGNED or key is None:
        return False, "回执未签名（密钥缺失）；可作线索，不构成承载证据"
    if not hmac.compare_digest(hmac.new(key, payload.encode(), hashlib.sha256).hexdigest(), str(r.get("mac"))):
        return False, "回执 MAC 不匹配：内容被改写或非本机签发"
    # 回执的角色必须是期望角色：一份 role: Architecture 的**有效**回执此前可以充当
    # Reviewer 的独立性证据（复核实测旁路）——签名再严，绑错主体就不成立。
    if role is not None and str(r.get("role")) != role:
        return False, (f"回执由 {r.get('role')!r} 签发，不能充当 {role!r} 的承载证据")
    # 交付物里写的 carrier 必须与回执一致：不比这一项，改一行 carrier 冒充另一个承载
    # 仍能挂着真回执通过（本轮实测的残留旁路）。
    if carrier is not None and str(r.get("carrier")) != carrier:
        return False, (f"交付物声明 carrier={carrier!r}，回执记载 {r.get('carrier')!r}；"
                       "承载以回执为准，文内声明被改写")
    # 受审对象必须是回执绑定的那一份：回执可以有效、角色可以对、正文可以没改，而它证明的
    # 复核对象是另一个文件——独立回执此前不能证明"复核的是报告声明的受审对象"（复核 C-03）。
    if target_sha256 is not None and str(r.get("target_sha256") or "") != target_sha256:
        return False, (f"交付物声明受审对象 {target_sha256[:12]}…，回执绑定 "
                       f"{str(r.get('target_sha256') or '<未绑定>')[:12]}…；"
                       "回执须证明复核的正是报告声明的那一版（调度时须 --target 该对象）")
    if text is not None or (deliverable is not None and deliverable.exists()):
        if text is not None:
            actual = body_sha256(text)
        elif deliverable.is_dir():  # type: ignore[union-attr]
            actual = content_sha256(deliverable)  # type: ignore[arg-type]
        elif deliverable.suffix == ".md":  # type: ignore[union-attr]
            actual = body_sha256(deliverable)  # type: ignore[arg-type]
        else:
            actual = _sha256_file(deliverable)  # type: ignore[arg-type]
        if r.get("deliverable_sha256") and actual != r["deliverable_sha256"]:
            return False, ("交付物正文与回执记录不符：回执证明的是「这一份内容由这条命令产出」，"
                           f"改动后须重新调度（回执 {r['deliverable_sha256'][:12]}… / 实际 {actual[:12]}…）")
    return True, f"回执有效：{r.get('role')} 经 {r.get('carrier')} 产出于 {r.get('issued_at')}"


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("receipt_id", nargs="?", help="核验指定回执；省略则列出最近 10 条")
    p.add_argument("--deliverable", type=Path, help="同时核验交付物内容是否与回执绑定")
    args = p.parse_args(argv)
    if not args.receipt_id:
        files = sorted(RECEIPT_DIR.glob("rcpt-*.json"), key=lambda x: x.stat().st_mtime)[-10:] if RECEIPT_DIR.is_dir() else []
        if not files:
            print(f"暂无回执：{RECEIPT_DIR}")
            return 0
        for f in files:
            r = json.loads(f.read_text(encoding="utf-8"))
            signed = "已签名" if r.get("mac") != UNSIGNED else "未签名"
            print(f"{r['receipt_id']}  {r['issued_at']}  {r['role']:12} {r['carrier']:20} {signed}")
        return 0
    ok, why = verify(args.receipt_id, args.deliverable)
    print(("✅ " if ok else "❌ ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
