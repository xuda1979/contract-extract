# 06 — Contract-Extract: 合同/标书风险抽取

## 定位
Adobe Acrobat / Document Cloud 线的垂直切片：从合同与标书（PDF/Word）中抽取
风险条款与关键信息，输出结构化 result + 摘要。母市场：Adobe Document Cloud ARR $27.5B。
单人切（中小企业 / 律师 / 采购）：$1K–$100K MRR。

## 主要痛点
- 合同/标书动辄几十页，人工阅读费力
- 关键风险（违约、责任限制、期限、排他、保密、续约）靠人眼遗漏
- 需要标准化输出（评审清单）而不是泛泛摘要

## 功能需求
1. 文档加载：PDF（本地解析，精度不依赖云端）+ DOCX（python-docx）
2. 风险抽取（规则 + LLM 双层）：
   - 条款域：长度/数量、价格、交付时间、验收、付款节点、违约金、责任上限、
     保密条款、知识产权归属、续约/终止、排他、数据保护
   - 风险标记：对赋 flag（如「责任上限无上限」「违约金含 30% 比例」「排他 5 年」）
   - 结构化：JSON Schema 输出（条款、引用页码、原文片段、风险等级）
3. LLM 增强层（可选、按量）：
   - 抽取不可规则化的段落（`LLM_OFFLINE=1` 时用规则 + 模板回退）
   - 生成「评审 Checklist」Markdown
4. CLI + Web demo 两种形态
5. 批量：目录批处理（50 个文件）＋队列

## 非目标
- 电子签名（不在范围）
- 仲裁/诉讼的完整法律分析
- 非合同文档（发票等）深度语义

## 技术栈
- Python 3.14
- PDF: pypdf / pdfplumber（本地）；DOCX: python-docx
- LLM 调用（openai/anthropic SDK 按量，可开关）
- JSON Schema（pydantic）输出模型
- 测试：fixture 合同集（正/反例）

## 低算力守则
本地：PDF/WASM/diff/cron；LLM 按量即 OCR/转写/摘要；`LLM_OFFLINE` mock 可测。

## AC
1. 给定 20 份 fixture 合同，抽取字段 F1 全部命中（per-field ≥ 95% recall）
2. 风险标记正确性 ≥ 90%（审阅专家标注的基准）
3. 所有输出含：条款名、页码、原文片段、风险等级（LOW/MEDIUM/HIGH）
4. LLM 关闭时（offline）仍然可用（规则层工作）— 结构完整
5. 批量处理 100 份 PDF 无内存爆炸（流式）
6. 测试 + README（usage/模型调用成本说明）+ 示例输出

## Phases
- P0: PDF 解析 + Schema + 规则抽取
- P1: LLM 增强 + 评审清单输出
- P2: 批量 + Web demo + 评分模型
- P3: 模板市场（行业条款模板）

## 里程碑
M1: 规则抽取全字段 + offline（MVP）
M2: LLM checklist
M3: 批量 + Web

## 变现
- 按文档计费（$/doc）+ 团队订阅（批量）
