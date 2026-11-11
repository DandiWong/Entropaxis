#!/usr/bin/env python3
"""
scaffold_demo.py - 单文件可交互 HTML Demo 脚手架生成与规范体检工具

契约规范：
1. 纯标准库实现（argparse, pathlib, tempfile, re, sys, html）
2. 原子组装与幂等写入（写临时目录后原子替换）
3. 契合交互偏好：
   - 顶部 Stepper（阶段步进，初始仅阶段 1 激活，后续阶段置灰锁定）
   - IPO 三栏栅格布局（左 Input 280px，中 Process 1fr，右 Output 630px 宽屏排版，呼吸感充足）
   - 初始空态占位（Empty State）与卡片点击 Mock 切换就绪
   - 上阶段 Output 自动注入下阶段 Input，并显式展示继承提示框
   - “录入报错提示语（讲人话）”暖黄气泡卡片，与等宽代码块高亮对比
   - 杜绝黑框终端命令行纯装饰，用真实表单与交互呈现产品价值
4. 提供 --lint 门禁，对已有 HTML Demo 实施自动化结构与体验规范体检
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
DEFAULT_STAGES = [
    "资料导入解析",
    "规则生成证据",
    "查漏排错基线",
    "标准规格导出",
    "规则验证沙箱"
]

def generate_demo_html(title: str, stages: list[str]) -> str:
    """生成完全自包含、免构建的单文件 HTML 交互原型骨架。"""
    if not stages:
        stages = DEFAULT_STAGES

    stepper_items = []
    stage_sections = []

    for i, s_name in enumerate(stages, 1):
        is_first = (i == 1)
        step_class = "step active" if is_first else "step locked"
        status_text = "进行中" if is_first else "待解锁"
        stepper_items.append(f"""
        <div class="{step_class}" id="step-nav-{i}" onclick="switchStage({i})">
          <div class="step-num">{i}</div>
          <div class="step-info">
            <div class="step-title">{s_name}</div>
            <div class="step-desc" id="step-status-{i}">{status_text}</div>
          </div>
        </div>""")

        display_style = "display: block;" if is_first else "display: none;"
        prev_hint = f"""
          <div class="inherited-banner" id="inherited-banner-{i}" style="display: {'block' if not is_first else 'none'};">
            <span class="icon">✔</span> 已继承上一阶段产出要素，数据通道已就绪。
          </div>""" if not is_first else ""

        stage_sections.append(f"""
      <!-- 阶段 {i}: {s_name} -->
      <section class="stage-section" id="stage-panel-{i}" style="{display_style}">
        <div class="stage-header">
          <div class="stage-badge">PHASE 0{i}</div>
          <h2 class="stage-heading">{s_name}</h2>
          <p class="stage-caption">用界面控件与真实业务交互呈现需求，拒绝说明文本堆砌。</p>
        </div>

        <div class="ipo-grid">
          <!-- 左栏: Input 输入端 (280px) -->
          <div class="ipo-col ipo-input">
            <div class="col-header">
              <span class="col-tag tag-input">INPUT</span>
              <span class="col-title">输入端资料</span>
            </div>
            {prev_hint}
            <div class="card-list" id="input-cards-{i}">
              <div class="file-dropzone" id="dropzone-{i}-1" onclick="toggleMock({i}, 1)">
                <div class="drop-status" id="drop-status-{i}-1">未加载（轻点载入脱敏样本）</div>
                <div class="drop-filename" id="drop-name-{i}-1">业务原始资料模板_{i}.docx</div>
              </div>
              <div class="file-dropzone" id="dropzone-{i}-2" onclick="toggleMock({i}, 2)">
                <div class="drop-status" id="drop-status-{i}-2">未加载（轻点载入脱敏样本）</div>
                <div class="drop-filename" id="drop-name-{i}-2">表单结构与历史经验_{i}.xlsx</div>
              </div>
            </div>
          </div>

          <!-- 中栏: Process 处理与控制端 (自适应 1fr) -->
          <div class="ipo-col ipo-process">
            <div class="col-header">
              <span class="col-tag tag-process">PROCESS</span>
              <span class="col-title">处理动作与操作工作台</span>
            </div>
            <div class="process-body">
              <div class="action-bar">
                <button class="btn btn-primary" id="btn-run-{i}" onclick="runProcess({i})">
                  ▶ 启动阶段 {i} 智能处理
                </button>
                <div class="loading-spinner" id="spinner-{i}" style="display: none;"></div>
              </div>

              <!-- 讲人话的提示语控件（非技术黑话） -->
              <div class="query-card" id="query-card-{i}">
                <div class="query-card-title">💬 录入报错提示语（数据录错时，弹给业务一线看的话）</div>
                <input type="text" class="form-input" id="query-input-{i}"
                       value="当前检测值异常，不符合基线准入规范，请临床医生或专员复核..."
                       placeholder="输入一线通俗人话，严禁生硬代码报错" />
              </div>

              <div class="code-preview" id="code-box-{i}">
                <div class="code-title">逻辑执行条件</div>
                <pre class="code-text"><code>// 阶段 {i} 规则判定表达式
IF Form[{i}].Field_A > 0 AND Form[{i}].Field_B IS NULL
THEN TRIGGER_CHECK(SEVERITY='WARNING');</code></pre>
              </div>

              <div class="process-footer">
                <button class="btn btn-success" id="btn-next-{i}" onclick="unlockNext({i})" disabled>
                  完成当前阶段并解锁下一步 →
                </button>
              </div>
            </div>
          </div>

          <!-- 右栏: Output 产出展示端 (宽屏 630px，呼吸感排版) -->
          <div class="ipo-col ipo-output">
            <div class="col-header">
              <span class="col-tag tag-output">OUTPUT</span>
              <span class="col-title">结构化产出成果</span>
            </div>
            <div class="output-container" id="output-box-{i}">
              <div class="empty-state" id="empty-box-{i}">
                <div class="empty-icon">📂</div>
                <div class="empty-text">待启动分析，请点击中栏按钮执行处理</div>
              </div>
              <div class="result-table-wrapper" id="result-wrapper-{i}" style="display: none;">
                <table class="result-table">
                  <thead>
                    <tr>
                      <th>条目编号</th>
                      <th>业务实体/字段</th>
                      <th>校验规则与逻辑定义</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody id="result-rows-{i}">
                    <tr>
                      <td><code>REQ-0{i}-101</code></td>
                      <td><strong>核心指标_{i}</strong></td>
                      <td>值域边界约束与跨表单时序逻辑校验</td>
                      <td><span class="badge badge-ready">已就绪</span></td>
                    </tr>
                    <tr>
                      <td><code>REQ-0{i}-102</code></td>
                      <td><strong>随访关联_{i}</strong></td>
                      <td>访视超窗排查与双向证据链锚定</td>
                      <td><span class="badge badge-ready">已校验</span></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </section>""")

    total_stages = len(stages)

    html_template = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --surface: #ffffff;
      --border: #e2e8f0;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --primary: #2563eb;
      --primary-hover: #1d4ed8;
      --success: #16a34a;
      --warning: #f59e0b;
      --warning-bg: #fffbeb;
      --warning-border: #fef3c7;
      --code-bg: #1e293b;
      --code-fg: #f1f5f9;
      --col-input-w: 280px;
      --col-output-w: 630px;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      background: var(--bg);
      color: var(--text-main);
      padding: 24px;
      line-height: 1.5;
    }}
    .app-container {{
      max-width: 1680px;
      margin: 0 auto;
      background: var(--surface);
      border-radius: 12px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.05);
      border: 1px solid var(--border);
      overflow: hidden;
    }}
    /* Top Bar */
    .app-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 16px 24px;
      border-bottom: 1px solid var(--border);
      background: #ffffff;
    }}
    .app-title-group h1 {{ font-size: 18px; font-weight: 700; color: var(--text-main); }}
    .app-title-group p {{ font-size: 12px; color: var(--text-muted); margin-top: 2px; }}
    /* Stepper */
    .stepper-nav {{
      display: flex;
      background: #f1f5f9;
      border-bottom: 1px solid var(--border);
      overflow-x: auto;
    }}
    .step {{
      flex: 1;
      min-width: 180px;
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 18px;
      cursor: pointer;
      border-right: 1px solid var(--border);
      transition: all 0.2s ease;
      user-select: none;
    }}
    .step.active {{ background: #ffffff; border-bottom: 2px solid var(--primary); }}
    .step.completed .step-num {{ background: var(--success); color: #fff; }}
    .step.locked {{ opacity: 0.5; cursor: not-allowed; }}
    .step-num {{
      width: 28px;
      height: 28px;
      border-radius: 50%;
      background: #cbd5e1;
      color: #334155;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      font-size: 13px;
    }}
    .step.active .step-num {{ background: var(--primary); color: #fff; }}
    .step-title {{ font-size: 13px; font-weight: 600; color: var(--text-main); }}
    .step-desc {{ font-size: 11px; color: var(--text-muted); }}
    /* Stage View */
    .stage-section {{ padding: 24px; }}
    .stage-header {{ margin-bottom: 20px; }}
    .stage-badge {{ display: inline-block; font-size: 11px; font-weight: 700; color: var(--primary); background: #eff6ff; padding: 2px 8px; border-radius: 4px; margin-bottom: 6px; }}
    .stage-heading {{ font-size: 20px; font-weight: 700; }}
    .stage-caption {{ font-size: 13px; color: var(--text-muted); margin-top: 4px; }}
    /* IPO 3-Column Grid: 280px / 1fr / 630px */
    .ipo-grid {{
      display: grid;
      grid-template-columns: var(--col-input-w) minmax(340px, 1fr) var(--col-output-w);
      gap: 20px;
      align-items: start;
    }}
    .ipo-col {{
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      min-height: 520px;
    }}
    .col-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 16px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }}
    .col-tag {{ font-size: 10px; font-weight: 800; padding: 2px 6px; border-radius: 4px; }}
    .tag-input {{ background: #e0f2fe; color: #0369a1; }}
    .tag-process {{ background: #fef3c7; color: #b45309; }}
    .tag-output {{ background: #dcfce7; color: #15803d; }}
    .col-title {{ font-size: 14px; font-weight: 700; }}
    /* Input Dropzone */
    .inherited-banner {{
      background: #ecfdf5;
      border: 1px solid #a7f3d0;
      color: #065f46;
      font-size: 12px;
      padding: 10px;
      border-radius: 6px;
      margin-bottom: 14px;
    }}
    .file-dropzone {{
      border: 2px dashed #cbd5e1;
      border-radius: 6px;
      padding: 14px;
      background: #ffffff;
      margin-bottom: 12px;
      cursor: pointer;
      transition: all 0.2s ease;
    }}
    .file-dropzone:hover {{ border-color: var(--primary); background: #f8fafc; }}
    .file-dropzone.ready {{ border-style: solid; border-color: var(--success); background: #f0fdf4; }}
    .drop-status {{ font-size: 11px; color: var(--text-muted); }}
    .file-dropzone.ready .drop-status {{ color: var(--success); font-weight: 600; }}
    .drop-filename {{ font-size: 13px; font-weight: 600; margin-top: 4px; color: var(--text-main); }}
    /* Process Work Area */
    .action-bar {{ display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }}
    .btn {{
      padding: 9px 18px;
      font-size: 13px;
      font-weight: 600;
      border-radius: 6px;
      cursor: pointer;
      border: none;
      transition: background 0.15s ease;
    }}
    .btn-primary {{ background: var(--primary); color: #fff; }}
    .btn-primary:hover {{ background: var(--primary-hover); }}
    .btn-success {{ background: var(--success); color: #fff; }}
    .btn-success:disabled {{ background: #cbd5e1; cursor: not-allowed; }}
    .btn-secondary {{ background: #e2e8f0; color: #334155; }}
    .loading-spinner {{
      width: 18px;
      height: 18px;
      border: 2px solid #cbd5e1;
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    /* Human Query Card */
    .query-card {{
      background: var(--warning-bg);
      border: 1px solid var(--warning-border);
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 16px;
    }}
    .query-card-title {{ font-size: 12px; font-weight: 700; color: #92400e; margin-bottom: 6px; }}
    .form-input {{
      width: 100%;
      padding: 8px 10px;
      border: 1px solid #fde68a;
      border-radius: 4px;
      font-size: 12px;
      background: #fff;
    }}
    /* Code Box */
    .code-preview {{
      background: var(--code-bg);
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 16px;
    }}
    .code-title {{ font-size: 11px; color: #94a3b8; margin-bottom: 6px; font-family: monospace; }}
    .code-text {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 12px; color: var(--code-fg); overflow-x: auto; }}
    .process-footer {{ margin-top: 20px; }}
    /* Output Wide Panel (630px) */
    .output-container {{
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 6px;
      min-height: 420px;
      padding: 12px;
    }}
    .empty-state {{
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 380px;
      color: var(--text-muted);
      border: 2px dashed #e2e8f0;
      border-radius: 6px;
    }}
    .empty-icon {{ font-size: 32px; margin-bottom: 8px; }}
    .empty-text {{ font-size: 13px; }}
    .result-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .result-table th, .result-table td {{
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }}
    .result-table th {{ background: #f8fafc; font-weight: 600; color: var(--text-muted); }}
    .badge {{ font-size: 10px; padding: 2px 6px; border-radius: 4px; font-weight: 600; }}
    .badge-ready {{ background: #dcfce7; color: #166534; }}
  </style>
</head>
<body>
  <div class="app-container">
    <header class="app-header">
      <div class="app-title-group">
        <h1>{title}</h1>
        <p>基于业务事实真源驱动 · 5 阶段 IPO 步进流水线 · 交互呈现价值</p>
      </div>
      <div>
        <button class="btn btn-secondary" onclick="resetDemo()">↺ 重置演示状态</button>
      </div>
    </header>

    <nav class="stepper-nav">
      {''.join(stepper_items)}
    </nav>

    <main class="app-main">
      {''.join(stage_sections)}
    </main>
  </div>

  <script>
    const TOTAL_STAGES = {total_stages};
    let currentStage = 1;
    let stageCompleted = Array(TOTAL_STAGES + 1).fill(false);

    function switchStage(stageIdx) {{
      const stepElem = document.getElementById(`step-nav-${{stageIdx}}`);
      if (stepElem.classList.contains('locked')) {{
        alert('该阶段尚未解锁，请先完成前序阶段的核心操作！');
        return;
      }}
      currentStage = stageIdx;
      for (let i = 1; i <= TOTAL_STAGES; i++) {{
        const panel = document.getElementById(`stage-panel-${{i}}`);
        const nav = document.getElementById(`step-nav-${{i}}`);
        if (panel) panel.style.display = (i === stageIdx) ? 'block' : 'none';
        if (nav) {{
          if (i === stageIdx) nav.classList.add('active');
          else nav.classList.remove('active');
        }}
      }}
    }}

    function toggleMock(stageIdx, dropIdx) {{
      const zone = document.getElementById(`dropzone-${{stageIdx}}-${{dropIdx}}`);
      const status = document.getElementById(`drop-status-${{stageIdx}}-${{dropIdx}}`);
      if (!zone) return;
      zone.classList.toggle('ready');
      if (zone.classList.contains('ready')) {{
        status.textContent = '✔ 脱敏样本已就绪';
      }} else {{
        status.textContent = '未加载（轻点载入脱敏样本）';
      }}
    }}

    function runProcess(stageIdx) {{
      const spinner = document.getElementById(`spinner-${{stageIdx}}`);
      const runBtn = document.getElementById(`btn-run-${{stageIdx}}`);
      const emptyBox = document.getElementById(`empty-box-${{stageIdx}}`);
      const resultWrap = document.getElementById(`result-wrapper-${{stageIdx}}`);
      const nextBtn = document.getElementById(`btn-next-${{stageIdx}}`);

      if (spinner) spinner.style.display = 'inline-block';
      if (runBtn) runBtn.disabled = true;

      setTimeout(() => {{
        if (spinner) spinner.style.display = 'none';
        if (runBtn) runBtn.disabled = false;
        if (emptyBox) emptyBox.style.display = 'none';
        if (resultWrap) resultWrap.style.display = 'block';
        if (nextBtn) nextBtn.disabled = false;
      }}, 600);
    }}

    function unlockNext(stageIdx) {{
      stageCompleted[stageIdx] = true;
      const navCurrent = document.getElementById(`step-nav-${{stageIdx}}`);
      const statusCurrent = document.getElementById(`step-status-${{stageIdx}}`);
      if (navCurrent) navCurrent.classList.add('completed');
      if (statusCurrent) statusCurrent.textContent = '已完成';

      const nextStage = stageIdx + 1;
      if (nextStage <= TOTAL_STAGES) {{
        const nextNav = document.getElementById(`step-nav-${{nextStage}}`);
        const nextStatus = document.getElementById(`step-status-${{nextStage}}`);
        if (nextNav) nextNav.classList.remove('locked');
        if (nextStatus) nextStatus.textContent = '可操作';
        switchStage(nextStage);
      }} else {{
        alert('恭喜！全流程交互闭环验证已全部完成！');
      }}
    }}

    function resetDemo() {{
      stageCompleted = Array(TOTAL_STAGES + 1).fill(false);
      currentStage = 1;
      for (let i = 1; i <= TOTAL_STAGES; i++) {{
        const nav = document.getElementById(`step-nav-${{i}}`);
        const status = document.getElementById(`step-status-${{i}}`);
        const emptyBox = document.getElementById(`empty-box-${{i}}`);
        const resultWrap = document.getElementById(`result-wrapper-${{i}}`);
        const nextBtn = document.getElementById(`btn-next-${{i}}`);
        if (nav) {{
          nav.classList.remove('active', 'completed');
          if (i > 1) nav.classList.add('locked');
        }}
        if (status) status.textContent = (i === 1) ? '进行中' : '待解锁';
        if (emptyBox) emptyBox.style.display = 'flex';
        if (resultWrap) resultWrap.style.display = 'none';
        if (nextBtn) nextBtn.disabled = true;

        for (let d = 1; d <= 2; d++) {{
          const zone = document.getElementById(`dropzone-${{i}}-${{d}}`);
          const dstatus = document.getElementById(`drop-status-${{i}}-${{d}}`);
          if (zone) zone.classList.remove('ready');
          if (dstatus) dstatus.textContent = '未加载（轻点载入脱敏样本）';
        }}
      }}
      switchStage(1);
    }}
  </script>
</body>
</html>
"""
    return html_template


def lint_demo_html(html_file: Path) -> tuple[bool, list[str]]:
    """门禁体检：对 HTML 交互原型实施结构与设计规范检查。"""
    issues = []
    if not html_file.exists():
        return False, [f"文件不存在: {html_file}"]

    content = html_file.read_text(encoding="utf-8", errors="replace")

    # 1. 检查是否存在 Stepper 导航
    if not re.search(r"step(?:per)?[-_]nav|class=[\"'].*step.*[\"']", content, re.IGNORECASE):
        issues.append("未检测到阶段步进器（Stepper）结构，缺乏清晰的流程阶段感知。")

    # 2. 检查是否符合 IPO 三栏核心语义
    has_input = bool(re.search(r"input|输入", content, re.IGNORECASE))
    has_process = bool(re.search(r"process|处理|工作台", content, re.IGNORECASE))
    has_output = bool(re.search(r"output|产出|成果", content, re.IGNORECASE))
    if not (has_input and has_process and has_output):
        issues.append("未完全满足 Input-Process-Output（输入-处理-产出）三栏结构划分。")

    # 3. 检查右侧 Output 是否有充足的横向呼吸感（拒绝拥挤）
    if re.search(r"grid-template-columns", content):
        col_match = re.search(r"grid-template-columns\s*:\s*([^;]+);", content)
        if col_match:
            cols = col_match.group(1).strip()
            output_width = None
            var_match = re.search(r"var\((--[a-zA-Z0-9_-]+)\)", cols.split()[-1])
            if var_match:
                var_name = var_match.group(1)
                val_match = re.search(rf"{re.escape(var_name)}\s*:\s*(\d+)px", content)
                if val_match:
                    output_width = int(val_match.group(1))
            else:
                widths = re.findall(r"(\d+)px", cols)
                if widths and "minmax" not in cols.split()[-1]:
                    output_width = int(widths[-1])

            if output_width is not None and output_width < 480:
                issues.append(f"右侧 Output 栏位宽度为 {output_width}px，存在表格排版挤压换行风险；建议扩至 600px+ 保持呼吸感。")
    # 4. 检查是否存在过度设计的纯黑框终端（Terminal / Linux 命令行）
    if re.search(r"terminal|medaikaid-agent\s+parse|\$\s+python", content, re.IGNORECASE) and "class=\"terminal\"" in content:
        issues.append("检测到过度设计的黑框终端命令行（Terminal），对于业务一线造成额外认知负荷，建议剔除并改为业务步骤卡片。")

    # 5. 检查是否包含“讲人话”的报错提示语概念（杜绝生硬技术代码）
    if not re.search(r"报错提示语|提示医生|提示语|讲人话|通俗", content):
        issues.append("未检测到通俗易懂的“录入报错提示语”，表单校验文案可能过于技术化。")

    # 6. 检查是否包含初始空状态（Empty State）
    if not re.search(r"empty[-_]state|空状态|未加载|等待资料导入", content):
        issues.append("缺少初始空状态（Empty State）占位，页面首屏数据可能过于杂乱。")

    is_valid = (len(issues) == 0)
    return is_valid, issues


def main():
    parser = argparse.ArgumentParser(description="单文件 HTML 交互原型脚手架生成与规范体检工具")
    parser.add_argument("--title", default="业务全流程交互Demo", help="Demo 页面标题")
    parser.add_argument("--stages", help="阶段列表，逗号分隔（例如：资料导入解析,规则生成证据,查漏排错基线,太美规格导出,执行闭环）")
    parser.add_argument("--from-reqs", help="从已提取的需求清单 JSON 文件自动读取阶段生成骨架")
    parser.add_argument("--out", help="生成的 HTML 输出文件路径")
    parser.add_argument("--lint", help="对指定的 HTML Demo 文件实施设计规范与体验门禁体检")

    args = parser.parse_args()

    if args.lint:
        target = Path(args.lint)
        passed, issues = lint_demo_html(target)
        if passed:
            print(f"✅ Demo 规范体检全绿通过: {target} (满足 IPO 架构、空态 Mock、宽屏呼吸感与通俗文案门禁)")
            sys.exit(0)
        else:
            print(f"❌ Demo 规范体检发现 {len(issues)} 项不合规问题: {target}")
            for iss in issues:
                print(f"  • {iss}")
            print("👉 修复建议: 参照《工作流指令》「出 Demo / 交互原型」与 produce-demo Skill 优化布局与文案。")
            sys.exit(1)

    if not args.out:
        print("❌ 缺少必要参数: 必须指定 --out <path.html> 或使用 --lint <path.html>")
        print("👉 修复建议: 执行 python3 .entropaxis/tools/scaffold_demo.py --out demo.html [--from-reqs reqs.json | --stages <阶段1,阶段2...>]")
        sys.exit(1)

    stages = []
    if args.from_reqs:
        req_file = Path(args.from_reqs)
        if not req_file.exists():
            print(f"❌ 需求清单文件不存在: {req_file}")
            sys.exit(1)
        try:
            req_data = json.loads(req_file.read_text(encoding="utf-8"))
            if isinstance(req_data, list):
                for item in req_data:
                    s_name = item.get("场景概述") or item.get("需求名称") or item.get("title") or item.get("name")
                    if s_name:
                        clean_name = re.sub(r"^【[^】]+】\s*", "", str(s_name)).strip()
                        if clean_name not in stages and len(stages) < 6:
                            stages.append(clean_name)
        except Exception as e:
            print(f"❌ 解析需求清单 JSON 失败: {e}")
            sys.exit(1)

    if not stages:
        stages = [s.strip() for s in args.stages.split(",")] if args.stages else DEFAULT_STAGES

    out_path = Path(args.out).resolve()
    html_content = generate_demo_html(args.title, stages)

    # 原子写入临时目录后替换
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out_dir) as tmpdir:
        tmp_file = Path(tmpdir) / "output.html"
        tmp_file.write_text(html_content, encoding="utf-8")
        tmp_file.replace(out_path)

    print(f"✅ 已成功原子生成交互 Demo 脚手架: {out_path} ({len(stages)} 个阶段: {' ➔ '.join(stages)})")


if __name__ == "__main__":
    main()
