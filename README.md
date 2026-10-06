# nl2shell · 基于大语言模型的智能 Shell 系统

面向不熟悉 Linux 命令的用户。用户说明目标，Ollama 理解条件、询问信息并生成操作计划。程序检查计划，显示实际文件清单，获得确认后执行，并检查结果。

## 大模型的作用

- 理解文件类型、时间、大小、路径和查找范围。
- 发现缺失条件，并提出一个具体问题。
- 将目标拆成最多三个步骤。
- 用中文解释方案。

模型不提供可直接执行的 Shell 命令。文件操作由 Python 固定实现执行；系统信息通过程序选定的只读 Bash 命令查询。

## 第一版支持范围

查找、计数、创建空文件及目录、复制、移动、重命名、按文件类型整理、条件备份、回收和恢复。支持查询磁盘、内存和系统信息。

软件安装、权限修改、服务管理、网络操作、任意脚本和永久删除均不支持。旧的自由命令输出会被拒绝，不会自动转换或执行。

## Linux 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
ollama pull qwen2.5-coder:7b
python cli.py
```

Ollama 需先安装。若服务尚未启动，在另一个终端运行 `ollama serve`，并保持它运行。默认使用 `http://localhost:11434/v1` 和 `qwen2.5-coder:7b`。可通过 `.env` 修改模型地址和名称。DeepSeek 接口继续保留，但第一版主验收路径为本地 Ollama。

启动时，当前目录就是工作目录。建议先用专用测试目录，或在交互中输入 `/workspace /path/to/test-directory`。

```text
找出当前目录这一层最近七天修改、超过 10 MB 的 PDF
帮我整理一下这个目录
备份当前目录及子目录最近七天修改的 Python 文件到 backup，保留目录结构
```

## 文件保护

- 所有文件路径必须位于工作目录内。第一版拒绝符号链接和特殊文件。
- 执行前显示操作及实际源、目标路径。需要完整输入 `yes`。
- 同名文件加编号，例如 `report_1.txt`。创建空文件也不清空已有内容。
- 确认后再次检查文件事实。发生变化时重新确认。
- 删除进入 `.nl2shell-trash`，不自动清空。使用 `/trash` 查看，用 `/restore 编号` 恢复。
- 失败时停止后续步骤。已完成操作保留，并逐项报告。跨文件系统移动明确失败。
- 模型不能自行选择验证命令。复制和备份比较文件内容，移动和恢复检查源、目标位置。

确认预览会读取文件内容计算摘要，大目录可能较慢。不要在运行期间由其他程序反复修改同一组文件。工作区规则不是操作系统沙箱；应在可信的个人工作目录运行。

## 常用入口

```bash
python cli.py --preview "统计当前目录下的 Python 文件"
python cli.py --json --preview "找出当前目录这一层的 PDF"
python cli.py --json --yes "查看系统信息"
python cli.py --batch tasks.jsonl
```

`--yes` 只允许受支持的只读任务跳过确认。JSON 模式不交互：缺信息时返回 `needs_clarification`，文件修改返回 `cancelled`。批处理只执行只读任务。旧 `auto-safe` 设置仍可读取，但交互操作仍逐次确认。

Docker 执行器只用于固定系统查询及隔离测试；选择该后端时，本机写入任务被拒绝，不静默改为在主机执行。

## 测试与真实模型评测

```bash
python -m pytest -q
python eval/run_eval.py --backend local
python eval/run_eval.py --backend local --execute-safe
python eval/compare.py eval/第一次结果.json eval/第二次结果.json
```

新评测集覆盖条件理解、多步规划、必要追问和范围拒绝。实际执行只发生在每条用例的独立临时目录，且只有操作和参数匹配预设期望的计划才会执行。结果文件带时间和编号，不覆盖旧结果。

指标包括条件理解正确率、计划正确率、必要追问率、范围拒绝率和实际任务完成率。未执行时，任务完成率为 `null`（未测量）。执行时，用独立的文件结果期望检查任务，失败的计划计为未完成。

自动测试使用模型替身，验证程序逻辑。它不证明真实 Ollama 的理解或规划能力。真实模型评测需另行运行。历史 Bash 数据集保留作旧版本参考，不再由主评测入口执行。

[使用说明](docs/USER_MANUAL.md) · [操作计划设计](docs/adr/0002-structured-operations.md)
