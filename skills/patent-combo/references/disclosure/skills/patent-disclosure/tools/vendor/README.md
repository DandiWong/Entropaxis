# 内置 mermaid（供 Playwright 出图，无需 Node / mmdc）

| 文件 | 版本 | 来源 |
|------|------|------|
| `mermaid.min.js` | **11.4.1** | https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js |

与历史 `@mermaid-js/mermaid-cli` 11.4.x 同系列。出图由 `tools/mermaid_render.py` 经 Playwright 加载本文件，**禁止**运行时从 CDN 再拉。

固定 SHA-256：`a43bc1afd446f9c4cc66ac5dd45d02e8d65e26fc5344ec0ef787f88d6ddb6f9e`。`patent-combo/scripts/check_env.py` 在任何阶段前校验该值；不匹配即阻断出图和交付。

升级时请同步改本表版本号，并替换 `mermaid.min.js`。
