# nl2shell · 基于大语言模型的智能 Shell 系统

面向命令行使用门槛高和误操作风险，用中文描述任务，系统生成可审查的 Bash 命令，并在执行前形成统一的安全结论。

> **课题名称**：基于大语言模型的智能 Shell 系统设计与实现
> 一句话概括：自然语言命令生成、命令解释、安全审查与执行反馈。

## 这个项目做什么

用自然语言（中文）描述你想做的事，`nl2shell` 会：

1. **生成命令** —— 把任务拆成 1~3 步可独立执行的 Bash 命令；
2. **解释命令** —— 说明每一步做什么、影响哪些文件；
3. **安全审查** —— 在执行前，结合命令分析、工作区边界和风险信号，形成**统一的安全结论**；
4. **执行反馈** —— 受控执行、只读验证、错误分类与修正建议；
5. **历史审计** —— 完整记录计划、执行状态与验证结果，可查询/导出/重放。

它不是一个透明把命令丢给 shell 的封装，而是一个**带安全护栏、可审计、可量化**的本机智能 Shell。

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置模型

复制 `.env.example` 为 `.env`，选择模型后端：

```bash
# 本地模型（推荐，本机离线运行）
LLM_BACKEND=local
LOCAL_BASE_URL=http://127.0.0.1:11434/v1
LOCAL_MODEL=qwen2.5-coder:7b

# 或云端 DeepSeek
# LLM_BACKEND=deepseek
# DEEPSEEK_API_KEY=你的密钥
```

**本地推理需要先安装并启动 [Ollama](https://ollama.com)，并拉取模型：**

```bash
ollama pull qwen2.5-coder:7b      # 推荐：代码/命令能力强
# ollama pull qwen2.5-coder:1.5b   # 低显存备选
```

### 3. 运行

交互模式：

```bash
python cli.py
```

单次任务（脚本调用）：

```bash
python cli.py "统计当前目录下的 Python 文件"
python cli.py --preview "查找超过 10MB 的文件"     # 只看计划,不执行
python cli.py --json --preview "列出当前目录文件"   # 稳定 JSON 输出,适合脚本
```

安装为命令：

```bash
pip install -e .
nl2shell
```

## 运行方式

| 模式 | 行为 |
|---|---|
| **预览** `preview` | 只展示计划、风险、验证方案，永不执行 Bash |
| **确认执行** `confirm`（默认） | 确认整份计划后执行并验证；敏感操作需更严格确认 |
| **安全自动** `auto-safe` | 安全操作自动执行；删除、网络等敏感操作暂停确认；高危阻止 |

交互模式里用 `/mode` 临时切换、`/config` 保存默认、`/status` 查看状态、`/help` 查看内置命令。

## 评测

`eval/` 下有一套固定评测集，可在本地小模型上量化系统的表现。

```bash
# 跑全量评测（默认本地后端，读 .env）
python eval/run_eval.py --backend local --limit 200

# 比对云端 / 本地（需先分别跑出两份结果）
python eval/compare.py
```

评测指标包括：命令生成准确率（严格匹配 / 语义等价）、危险命令拦截率、意图识别准确率、必要澄清率、正常命令误报率、平均响应时间、敏感信息泄漏率、错误修复有效率和任务完成率。

## 项目结构

```
nl2shell/
├── cli.py                  # 入口：交互 + 单次任务 + --json 脚本调用
├── core/
│   ├── command_review.py   # 统一安全审查：影响、风险、决策（核心）
│   ├── engine.py           # LLM 任务计划生成与修复建议
│   ├── task_plan.py        # JSON 任务计划解析（兼容旧格式/拒绝态）
│   ├── execution.py        # Bash / Docker 执行后端
│   ├── preflight.py        # 执行前文件修正与候选
│   ├── verification.py     # 执行后只读验证
│   ├── error_analysis.py   # 错误确定性分类
│   ├── llm.py              # 模型客户端（本地 / DeepSeek）
│   └── redaction.py        # 敏感信息脱敏
├── eval/                   # 评测集与脚本
└── tests/                  # 单元与集成测试
```

## 安全机制

`nl2shell` 的安全结论由 `CommandReview` 统一形成，遵循两条原则：

- **确定性规则是底线**：命中高危规则（如 `rm -rf /`、`mkfs`、fork 炸弹等）直接阻止，不可被削弱；
- **LLM 只能提高风险**：模型的 `risk_advisory` 只能上调风险等级，不能下调到低于确定性规则。

执行前会用本地文件系统校验文件路径、检查工作区边界、检测文件覆盖；执行后进行只读验证、错误分类并给出修正建议。

## 技术说明

- Python ≥ 3.10
- 模型：OpenAI 兼容接口（通过 Ollama 本地推理 / DeepSeek 云端）
- 依赖：openai、python-dotenv、httpx、prompt_toolkit
