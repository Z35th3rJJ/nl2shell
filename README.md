# nl2shell · 智能 Shell 命令助手

用中文描述任务，系统生成最多三步的 Bash 计划，完成影响分析、安全决策、受控执行、结果验证与历史审计。

📖 [完整产品说明书](docs/USER_MANUAL.md)：安装配置、全部功能、命令参考、数据格式、安全说明与故障排查。

## 快速开始

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd nl2shell

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置模型与 Bash
cp .env.example .env
# 云端模式：填入 DEEPSEEK_API_KEY
# 本地模式：安装 Ollama，并在 .env 设置 LLM_BACKEND=local

# 4. 运行；首次启动会选择默认运行方式
python3 cli.py
```

也可以安装为命令行工具：

```bash
pip install -e .
nl2shell
```

Windows 请安装 Git Bash，并在找不到 Bash 时设置 `BASH_PATH=C:\Program Files\Git\bin\bash.exe`；Linux 通常可直接使用系统 Bash。

## 单次任务与脚本调用

无需进入交互界面即可执行一次任务：

```bash
python cli.py "统计当前目录下的 Python 文件"
python cli.py --preview "查找超过 10MB 的文件"
python cli.py --yes "列出当前目录文件"
python cli.py --json --preview "统计代码行数"
```

`--yes` 只会跳过 SAFE 操作的确认，WARN 仍需人工确认，HIGH 始终阻止。`--json` 固定输出 `status`、`risk_level`、`steps`、`verification`、`duration_seconds` 和 `error`。

## 使用示例

### 基本用法

```
你想做什么？> 列出当前目录所有文件
  命令：ls -la
  说明：列出当前目录所有文件的详细信息
执行？(y/n) > y

你想做什么？> 找出最大的三个文件
  命令：du -ah . | sort -rh | head -3
  说明：按文件大小降序排列，显示前 3 个
执行？(y/n) > y
```

### 安全拦截（高危命令二次确认）

```
你想做什么？> 删除所有文件
【高危命令】
  命令：rm -rf /
  说明：递归强制删除根目录所有内容
  风险：递归删除根目录或家目录，会造成不可恢复的数据丢失

确认执行请输入 yes（其他任意键取消）> n
已取消。
```

## 项目结构

```
nl2shell/
├── core/
│   ├── llm.py       # 模型 API 封装（支持 DeepSeek 云端 / 本地 Ollama 切换）
│   ├── engine.py    # 核心内核：命令生成、意图澄清、上下文管理
│   ├── command_review.py # 统一命令分析、文件事实、风险与执行决策
│   └── safety.py    # 旧安全接口的兼容 adapter
├── eval/
│   ├── testcases.json   # 基础用例；运行时与扩展场景组成 200 条系统评测集
│   ├── run_eval.py      # 自动评测脚本（双指标 + 安全拦截率）
│   └── compare.py       # 云端 vs 本地模型对比报告
├── tests/
│   ├── test_safety.py   # 安全模块单元测试
│   └── test_engine.py   # engine 输出分类单元测试
├── cli.py           # 命令行界面
├── requirements.txt
└── .env.example     # 配置模板（API Key / 本地模型地址）
```

## 运行评测

```bash
# 云端 DeepSeek 评测
python3 eval/run_eval.py --backend deepseek

# 本地模型评测（需先配置 LOCAL_BASE_URL）
python3 eval/run_eval.py --backend local

# 云端 vs 本地对比报告
python3 eval/compare.py

# 实际执行评测中的 SAFE 命令（WARN/HIGH 始终不执行）
python3 eval/run_eval.py --backend local --execute-safe
```

评测默认运行 200 条用例并输出命令准确率、意图准确率、必要澄清率、安全误报率、危险命令拦截率和平均响应时间。`--execute-safe` 强制使用 Docker 沙箱，不会降级到本机执行。

## 批量自动化

适合 CI 或大规模测试的任务文件使用 JSONL 格式，每行一个任务：

```json
{"input": "列出当前目录文件"}
{"input": "统计 Python 文件数量", "cwd": "/path/to/project"}
```

使用 `--batch` 串行执行。任务失败、超时或验证失败后会继续下一项；高危（HIGH）命令始终阻止。主命令默认超时 60 秒，可按批次调整：

```bash
python3 cli.py --batch tasks.jsonl
python3 cli.py --batch tasks.jsonl --timeout 120
```

结束后会打印汇总，并将批次结果写入 `~/.nl2shell/batch_<批次ID>.json`。批处理不进入首次交互设置，也不逐条确认；请只在受信任的测试环境和任务文件中使用。

## 切换到本地模型（可选）

在 `.env` 中配置：

```
ollama pull qwen2.5-coder:1.5b

LLM_BACKEND=local
LOCAL_BASE_URL=http://localhost:11434/v1
LOCAL_MODEL=qwen2.5-coder:1.5b
```

`qwen2.5-coder:1.5b` 是适合笔记本优先验证的小模型；也可把 `LOCAL_MODEL` 改为已安装的其他 Ollama 模型。Windows 上项目执行的是 Linux/Bash 命令，请确保 Git Bash 或 WSL 的 Bash 可用；若不在 PATH 中，可在 `.env` 配置：

```
BASH_PATH=C:\Program Files\Git\bin\bash.exe
```

## 历史与 SSH

每次任务的最终状态都会写入用户目录的 `~/.nl2shell/history.jsonl`，包含稳定记录 ID、输入、计划、风险、执行决策、验证结果和错误摘要。CLI 内输入 `/history` 可查看最近 20 条，输入 `/history 50 --status verified` 可筛选；也支持按批次或时间筛选、导出和重放：

```text
/history --batch <批次ID>
/history export csv history.csv --status command_failed
/history replay <记录ID>
/history replay-batch <批次ID>
```

主输入框另外使用 `~/.nl2shell/input_history` 保存输入历史：上下键可以浏览以前输入的任务，`Ctrl+R` 可以搜索，左右键和 Home/End 可编辑当前内容。执行确认中的 `y/yes`、文件候选确认和目标文件名不会进入这份历史。它与 `/history` 的执行审计互不影响。

程序读取标准 `~/.ssh/config` 中的 `Host`、`HostName`、`User`、`Port` 和 `IdentityFile` 元数据，帮助模型生成 `ssh <别名>`；私钥仍由 OpenSSH/ssh-agent 使用，程序不会读取、复制或保存私钥。运行中可用 `/ssh` 查看别名及配置状态，`/ssh test <别名>` 使用非交互认证检查连接。SSH 配置文件不在默认位置时，可设置：

```
SSH_CONFIG_PATH=~/.ssh/config
```

## 统一运行流程

所有请求都会生成任务计划：简单请求是一条命令，复杂请求最多三步。风险分析和执行后验证始终开启，用户只需要选择一种运行方式：

交互模式会向模型提供当前会话最近 5 轮任务的紧凑状态，包括已执行、取消、预览和阻止；取消任务会明确标记为“未执行”。上下文退出程序即清空，单次任务和批处理不共享上下文。本地文件检查始终优先于模型记忆。

| 运行方式 | 行为 |
| --- | --- |
| `preview`（预览） | 只展示计划、影响和验证方案，不调用 Bash |
| `confirm`（确认执行，推荐） | 展示整份计划，确认后执行并验证 |
| `auto-safe`（安全自动） | 安全操作自动执行；删除、网络等敏感操作暂停确认 |

毁灭性高危命令始终阻止；`sudo`、系统级修改和未知 Shell 语法必须强确认。验证失败时只生成一次修复建议，不自动执行修复。

当前工作区本身及其任何父目录属于永久保护目标，不能通过确认、`auto-safe` 或 `--yes` 删除。只输入“删除”“清理”等未指定对象的请求时，程序会要求先明确具体文件、目录或匹配范围。

文件操作在执行前会核对本地路径。文件名大小写或扩展名可能写错时，程序会展示候选并要求确认；单文件复制到自身时可直接回车采用 `README_copy.md` 一类的不冲突默认名称。修正后的命令会再次检查，未通过时不会调用 Bash。工作区边界按规范化后的真实路径判断。

删除目标不存在但存在相近候选时，必须完整输入 `yes` 确认候选，之后仍需通过原有删除确认。只要求创建文件而没有指定内容时，模型应使用 `touch`，不会从文件名猜测内容。严格的 `echo/printf 纯文本 > 单一文件` 可识别为普通写入；变量、命令替换、追加和组合重定向仍按未知语法强确认。

首次运行会选择并保存默认方式，之后直接进入 Shell。运行中可使用：

```text
/mode             临时切换运行方式
/config           修改并保存默认运行方式
/status           查看模型、目录、运行方式和 Bash 状态
/doctor           集中检查模型配置、Bash 和当前目录权限
/history [数量]   查看审计历史
/help             显示帮助
/exit             退出
```

`.env` 中只需要：

```env
RUN_MODE=confirm
SETUP_COMPLETE=true
```

旧版 `AGENT_MODE`、`EXECUTION_POLICY`、`DRY_RUN`、`AUTO_EXECUTE` 会在首次设置时迁移为新的运行方式。

## 安全与诊断

执行超时会终止对应的进程树；标准输出和错误输出过大时会保留头尾并标记截断。每一步执行前都会重新进行风险判断，验证命令仅允许已识别的只读操作。多步任务失败后不会继续执行剩余步骤，历史中会保存已完成、失败和未执行状态。

如果外部操作导致当前目录在运行期间消失，交互模式会自动回退到最近存在的父目录（必要时回到用户主目录），显示警告后继续运行。

交互模式启动时会显示环境问题，也可以随时输入 `/doctor`。模型网络连接失败会自动重试一次，随后返回可读错误，不会无限重试。

需要机器可读的本地运行日志时，可设置 `NL2SHELL_LOG_JSON=~/.nl2shell/runtime.jsonl`；默认关闭，并且不会记录 API Key、完整环境变量或原始命令输出。

模型请求、历史和结构化日志共用一套敏感信息脱敏规则，覆盖常见 API Key、Token、密码、Cookie、连接串和私钥。执行输出在发送给错误诊断模型时会标记为不可信数据，不能覆盖系统安全指令。

可设置 `EXECUTION_BACKEND=sandbox` 使用 Docker 沙箱。沙箱默认无网络、非 root、丢弃 Linux capabilities，并限制内存、CPU 和进程数量；Docker 不可用时任务直接报错，不会静默改用本机 Bash。

任务计划包含结构化 `intent`、`operation`、`entities` 和模型风险建议。统一 `CommandReview` 供计划展示、执行前复检、验证和评测复用；确定性规则拥有最终决定权，模型建议只能提高风险等级。受限解析器可识别简单管道、`&&` 和文件重定向，无法证明安全的语法继续要求强确认。执行前复检若提高确认等级，旧授权立即失效，批处理不会静默执行。

## 开发与测试

```bash
pip install -e ".[dev]"
pytest -q
```

GitHub Actions 会在 Windows、Ubuntu 以及 Python 3.10/3.12 上运行测试。版本变更记录见 `CHANGELOG.md`。
