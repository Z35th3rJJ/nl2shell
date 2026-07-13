# nl2shell 完整产品说明书

> 适用版本：0.1.0
> 文档语言：简体中文

## 目录

- [1. 产品简介](#1-产品简介)
- [2. 安装](#2-安装)
- [3. 模型与运行环境配置](#3-模型与运行环境配置)
- [4. 快速上手](#4-快速上手)
- [5. 运行方式](#5-运行方式)
- [6. 完整功能说明](#6-完整功能说明)
- [7. 命令行参数参考](#7-命令行参数参考)
- [8. 交互模式命令参考](#8-交互模式命令参考)
- [9. 数据格式与文件位置](#9-数据格式与文件位置)
- [10. 安全机制与边界](#10-安全机制与边界)
- [11. 故障排查](#11-故障排查)
- [12. 技术与开发附录](#12-技术与开发附录)

## 1. 产品简介

nl2shell 是一个面向中文用户的自然语言 Bash 助手。用户描述希望完成的任务后，程序会生成最多三步的 Bash 计划，并依次完成影响分析、安全决策、执行前文件检查、用户确认、受控执行、结果验证和历史审计。

### 1.1 适用场景

- 不熟悉 Bash 语法，希望用中文完成常见文件、查询或系统信息任务。
- 执行命令前希望先看到命令、影响范围、风险和验证方式。
- 需要把自然语言任务以 JSONL 文件批量运行。
- 需要保留任务计划、执行状态和验证结果，方便审计或重放。
- 希望比较 DeepSeek 云端模型和本地 Ollama 模型的生成效果。

### 1.2 不适用场景

- 本项目只生成和执行 Bash 命令，不原生支持 PowerShell 或 CMD。
- 它不是容器或虚拟机沙箱，不能替代操作系统权限隔离。
- 风险检查采用保守规则，不能保证识别所有 Bash 写法或第三方程序的副作用。
- 不应把它当作无人值守的系统管理工具；敏感任务仍需人工检查。

### 1.3 一次任务的完整流程

```text
中文任务
  → 模型生成 1～3 步计划
  → 必要时询问缺失信息
  → 文件路径与候选名称检查
  → 风险、影响和工作区边界分析
  → 预览、确认或安全自动决策
  → 逐步执行（失败即停止）
  → 只读验证
  → 保存历史和修复建议
```

## 2. 安装

### 2.1 系统要求

- Python 3.10 或更高版本。
- 可用的 Bash。
- DeepSeek API Key，或者本机/局域网内兼容 OpenAI API 的 Ollama 服务。

### 2.2 Linux 安装

```bash
git clone https://github.com/Z35th3rJJ/nl2shell.git
cd nl2shell
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 cli.py
```

### 2.3 Windows 安装

Windows 上仍然执行 Bash 命令。推荐先安装 Git for Windows，并确保 Git Bash 可用。

```powershell
git clone https://github.com/Z35th3rJJ/nl2shell.git
cd nl2shell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python cli.py
```

如果 Bash 不在 `PATH` 中，在 `.env` 中设置：

```env
BASH_PATH=C:\Program Files\Git\bin\bash.exe
```

也可以使用 WSL 的 Bash，但必须保证配置的可执行文件能够从当前 Windows Python 进程调用。

### 2.4 安装为 `nl2shell` 命令

在项目根目录执行：

```bash
pip install -e .
nl2shell --help
```

`-e` 表示可编辑安装，修改本地源代码后不需要重新安装。普通使用也可以继续运行 `python cli.py`。

## 3. 模型与运行环境配置

程序启动时从项目根目录的 `.env` 加载配置。不要提交包含真实密钥的 `.env`。

### 3.1 DeepSeek 云端模式

DeepSeek 是未指定 `LLM_BACKEND` 时的默认后端：

```env
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_MODEL=deepseek-v4-flash
```

`DEEPSEEK_MODEL` 可省略，代码默认使用 `deepseek-v4-flash`。

### 3.2 Ollama 本地模式

先准备模型：

```bash
ollama pull qwen2.5-coder:1.5b
```

然后配置：

```env
LLM_BACKEND=local
LOCAL_BASE_URL=http://localhost:11434/v1
LOCAL_MODEL=qwen2.5-coder:1.5b
```

示例推荐 `1.5b` 版本，便于普通笔记本快速体验。如果省略 `LOCAL_MODEL`，当前代码的后备默认值是 `qwen2.5-coder:7b-instruct`。请确保 Ollama 中实际存在所配置的模型。

### 3.3 环境变量参考

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无 | DeepSeek 云端认证密钥 |
| `DEEPSEEK_MODEL` | `deepseek-v4-flash` | DeepSeek 模型名 |
| `LLM_BACKEND` | `deepseek` | 设置为 `local` 时使用本地后端 |
| `LOCAL_BASE_URL` | `http://localhost:11434/v1` | 本地 OpenAI 兼容 API 地址 |
| `LOCAL_MODEL` | `qwen2.5-coder:7b-instruct` | 未显式设置时的本地模型后备值 |
| `BASH_PATH` | 自动查找 | 显式指定 Bash 可执行文件，优先级最高 |
| `SHELL_EXECUTABLE` | `bash` | 未设置 `BASH_PATH` 时使用的命令名 |
| `SSH_CONFIG_PATH` | `~/.ssh/config` | OpenSSH 配置文件位置 |
| `RUN_MODE` | `confirm` | `preview`、`confirm` 或 `auto-safe` |
| `SETUP_COMPLETE` | `false` | 首次运行向导是否已完成 |
| `NL2SHELL_LOG_JSON` | 空（关闭） | 启用结构化运行日志并指定 JSONL 文件 |

首次交互运行会选择默认运行方式，并把 `RUN_MODE` 与 `SETUP_COMPLETE=true` 写回 `.env`。旧配置项 `AGENT_MODE`、`EXECUTION_POLICY`、`DRY_RUN` 和 `AUTO_EXECUTE` 会在首次设置时迁移。

## 4. 快速上手

### 4.1 交互模式

```bash
python cli.py
```

在提示符中输入中文任务：

```text
你想做什么？> 统计当前目录下有多少个 Python 文件
```

程序会展示每一步的命令、说明、预期结果、验证方式、影响和执行决策。确认后才会按顺序执行。

### 4.2 单次任务

```bash
python cli.py "列出当前目录中的隐藏文件"
```

安装命令行入口后也可以使用：

```bash
nl2shell "列出当前目录中的隐藏文件"
```

单次任务不会进入首次运行向导，但仍会使用 `.env` 中的运行方式和完整安全策略。

### 4.3 只预览计划

```bash
python cli.py --preview "找出最大的五个文件"
```

预览模式会调用模型、生成并检查计划，但绝不会调用 Bash 执行计划。

### 4.4 对安全计划跳过确认

```bash
python cli.py --yes "列出当前目录文件"
```

`--yes` 只对全部满足自动许可条件的计划生效。WARN、未知语法、网络、删除、越界写入和其他敏感操作仍不会被静默放行；HIGH 永久阻止。

### 4.5 JSON 输出

```bash
python cli.py --json --preview "统计代码行数"
```

标准输出只包含一个 JSON 对象，适合脚本解析。涉及人工确认的非预览任务仍可能读取标准输入。

## 5. 运行方式

| 方式 | 配置值 | 行为 |
| --- | --- | --- |
| 预览 | `preview` | 展示计划、风险与验证方案，永不执行 Bash |
| 确认执行 | `confirm` | 展示整份计划，用户确认后执行；推荐作为默认值 |
| 安全自动 | `auto-safe` | 自动执行满足自动许可条件的计划；敏感操作暂停确认 |

交互中使用 `/mode` 临时切换当前进程的运行方式；使用 `/config` 修改并保存默认方式。

### 5.1 确认提示

- 普通确认：输入 `y` 执行，其他输入取消。
- 强确认：未知复杂语法、`sudo` 或系统级操作必须完整输入 `yes`。
- 输入 `r`：放弃当前计划并重新调用模型生成。
- 输入 `e`：重新描述任务并生成新计划。
- HIGH：不提供确认入口，直接阻止。

## 6. 完整功能说明

### 6.1 任务计划与澄清

- 每个任务生成一到三步计划。
- 每一步包含 Bash 命令、中文说明、预期结果和可选验证命令。
- 复制或移动任务缺少目标位置等关键信息时，程序会询问一次并重新生成计划。
- 模型返回的 JSON 不合法或为空时，任务以计划生成失败结束，不执行 Bash。
- 模型连接或超时错误会自动重试一次；再次失败后返回可读错误。

### 6.2 风险与影响分析

程序分别分析命令风险和影响范围：

- `SAFE`：没有命中危险规则。
- `WARN`：递归删除、通配符删除、命令替换、电源操作等需要确认的操作。
- `HIGH`：根目录/家目录递归删除、格式化设备、Fork 炸弹、管道直接交给 Shell 等毁灭性操作。

影响标签包括读取、写入、删除、网络、提权和未知语法。风险等级只是输入之一；即使命令为 `SAFE`，未知语法、网络访问或工作区外写入仍可能要求确认。

### 6.3 文件执行前检查

程序对能够可靠解析的本地文件操作进行检查：

- 源文件不存在时查找大小写或扩展名相近的候选。
- 单个候选可以直接确认；多个候选需要选择编号。
- `cp` 或 `mv` 缺少目标时要求补充目标。
- 单文件复制到自身时，可直接回车采用类似 `README_copy.md` 的不冲突名称。
- 修正命令后重新检查；仍然不合法时不会执行。
- 路径会规范化后再判断是否位于当前工作区，降低 `..` 和符号链接逃逸风险。

### 6.4 受控执行

- 多步计划按照原顺序串行运行。
- 每一步执行前重新计算风险和影响；如果变成 HIGH，则在该步执行前阻止。
- 主命令默认超时 60 秒；批处理可通过 `--timeout` 修改。
- 超时后终止对应进程树，而不只终止最外层 Bash。
- stdout 和 stderr 默认最多各保留约 200,000 字节；超过限制时保留头尾并插入截断标记。
- 前一步执行或验证失败后，剩余步骤标记为 `not_executed`。

### 6.5 结果验证与修复建议

- 主命令超时或退出码非 0 时，直接判定主命令失败。
- 没有验证命令时，按主命令退出码判定为 `exit_code_only`。
- 验证命令必须是已识别的安全只读命令，否则标记为 `invalid_verifier`。
- 验证命令固定最多运行 10 秒。
- 验证失败后，模型只生成一次文字修复建议；建议不会自动执行。

### 6.6 历史审计

所有任务最终状态写入 `~/.nl2shell/history.jsonl`。记录包括输入、工作目录、命令、风险、决策、预检、逐步验证结果、是否超时和修复建议等信息。

常用命令：

```text
/history
/history 50
/history 50 --status verified
/history --batch <批次ID>
/history 20 --since 2026-01-01T00:00:00+00:00
/history export jsonl history.jsonl --status command_failed
/history export csv history.csv
/history replay <记录ID>
/history replay-batch <批次ID>
```

重放会根据历史中的自然语言输入重新生成计划，不会盲目执行旧命令，因此仍然经过当前版本的全部安全策略。

### 6.7 输入历史

交互输入保存在 `~/.nl2shell/input_history`：

- 上下方向键浏览以前输入的任务。
- `Ctrl+R` 搜索历史。
- 左右方向键、Home 和 End 编辑当前输入。

执行确认、候选文件选择和目标文件名不会写入这份输入历史。输入历史与执行审计是两套独立数据。

### 6.8 SSH 支持

程序读取 OpenSSH 配置中的 `Host`、`HostName`、`User`、`Port` 和 `IdentityFile` 元数据，并把 Host 别名提供给模型。

```text
/ssh
/ssh test my-server
```

连接测试使用非交互认证和 10 秒连接超时。私钥仍由 OpenSSH 或 ssh-agent 使用；程序不会读取、复制或保存私钥内容。

### 6.9 环境诊断

交互启动时会显示发现的问题，也可以主动运行：

```text
/doctor
```

诊断项目包括模型配置、Bash 可用性、当前目录可访问性和写权限。诊断只检查本地配置与环境，不会发送测试模型请求。

### 6.10 批量自动化

任务文件采用 JSONL 格式，每行一个 JSON 对象：

```jsonl
{"input":"列出当前目录文件"}
{"input":"统计 Python 文件数量","cwd":"/path/to/project"}
```

执行方式：

```bash
python cli.py --batch tasks.jsonl
python cli.py --batch tasks.jsonl --timeout 120
```

批处理规则：

- 按文件顺序串行处理，单个任务失败后继续下一条。
- 只自动执行所有步骤均满足自动许可条件的任务。
- 需要普通确认或强确认的任务在无交互批处理中取消，不会静默执行。
- HIGH 任务永久阻止。
- 结束后输出成功、失败、阻止和超时数量。
- 汇总保存到 `~/.nl2shell/batch_<批次ID>.json`。

### 6.11 结构化运行日志

默认不写运行日志。需要机器可读日志时设置：

```env
NL2SHELL_LOG_JSON=~/.nl2shell/runtime.jsonl
```

日志记录任务结束时间、状态、风险、是否执行和工作目录，不记录 API Key、完整环境变量或原始命令输出。包含常见 `token`、`password`、`secret`、`api_key` 形式的字符串会被脱敏。

### 6.12 模型评测

```bash
python eval/run_eval.py --backend deepseek
python eval/run_eval.py --backend local
python eval/run_eval.py --backend local --limit 10
python eval/run_eval.py --backend local --execute-safe
python eval/compare.py
```

`--execute-safe` 只实际执行评测中被识别为 SAFE 的命令，WARN/HIGH 跳过。评测结果写入 `eval/eval_result_<后端>.json`，该文件默认不进入 Git。

## 7. 命令行参数参考

```text
nl2shell [-h] [--batch FILE] [--timeout SECONDS] [--preview] [--yes] [--json] [task]
```

| 参数 | 说明 |
| --- | --- |
| `task` | 可选的自然语言任务；提供后运行一次并退出 |
| `--batch FILE` | 从 JSONL 文件运行批量任务 |
| `--timeout SECONDS` | 主命令超时秒数，默认 60，必须大于 0 |
| `--preview` | 单次任务使用预览模式，不执行 Bash |
| `--yes` | 仅当整份计划可自动许可时跳过确认 |
| `--json` | 单次任务输出稳定 JSON 对象 |
| `-h`、`--help` | 显示帮助 |

`--preview`、`--yes` 和 `--json` 面向单次任务。批处理使用自身的非交互安全策略。

## 8. 交互模式命令参考

| 命令 | 作用 |
| --- | --- |
| `/mode` | 临时切换当前运行方式，不写配置 |
| `/config` | 切换并保存默认运行方式到 `.env` |
| `/status` | 查看模型、工作目录、运行方式和 Bash 状态 |
| `/doctor` | 检查模型配置、Bash、目录访问与写权限 |
| `/history [数量]` | 查看执行历史，可附加筛选条件 |
| `/history export ...` | 导出 JSONL 或 CSV |
| `/history replay <ID>` | 重放一条历史任务 |
| `/history replay-batch <ID>` | 按原顺序重放批次任务 |
| `/ssh` | 显示 SSH Host 配置摘要 |
| `/ssh test <别名>` | 非交互测试 SSH 连接和认证 |
| `/help` | 显示内置命令帮助 |
| `/exit` | 退出程序 |

也可以输入 `exit`、`quit` 或 `退出` 结束交互。

## 9. 数据格式与文件位置

### 9.1 `--json` 输出

固定顶层字段如下：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `status` | string | 最终任务状态 |
| `risk_level` | string | 整份计划的最高风险等级 |
| `steps` | array | 步骤结果；未执行场景至少包含命令和状态 |
| `verification` | array | 逐步执行与验证详情 |
| `duration_seconds` | number | 已执行步骤耗时总和 |
| `error` | string | 首个失败详情；无错误时为空字符串 |

预览输出示例：

```json
{
  "status": "preview",
  "risk_level": "SAFE",
  "steps": [{"status": "preview", "command": "ls -la"}],
  "verification": [],
  "duration_seconds": 0,
  "error": ""
}
```

### 9.2 常见任务状态

| 状态 | 含义 |
| --- | --- |
| `plan_failed` | 模型调用或计划解析失败 |
| `preflight_failed` | 澄清或文件执行前检查失败 |
| `blocked` | HIGH 风险计划被永久阻止 |
| `preview` | 已展示计划但未执行 |
| `bash_unavailable` | 找不到或无法调用 Bash |
| `cancelled` | 用户取消，或批处理任务需要交互确认 |
| `command_failed` | 主命令超时或退出码非 0 |
| `exit_code_only` | 主命令成功，未提供额外验证命令 |
| `verified` | 主命令和验证命令均成功 |
| `verification_failed` | 验证命令超时或退出码非 0 |
| `invalid_verifier` | 验证命令不是安全、已识别的只读命令 |
| `execution_error` | 执行过程中出现未预期异常 |
| `not_executed` | 因前序步骤失败而跳过 |

这些是任务数据中的状态，不等同于 CLI 进程退出码。当前程序主要通过输出和 JSON/历史状态表达任务结果。

### 9.3 历史 JSONL

历史文件每行是一条独立 JSON 记录，典型字段包括：

```json
{
  "record_id": "唯一记录ID",
  "timestamp": "UTC ISO 8601 时间",
  "input": "用户原始任务",
  "cwd": "/工作目录",
  "command": "计划命令",
  "risk": "SAFE",
  "status": "verified",
  "executed": true,
  "run_mode": "confirm",
  "decisions": ["auto-allow"],
  "verification": []
}
```

后续版本可能增加字段；消费历史数据时应忽略未知字段。

### 9.4 本地文件位置

| 文件 | 默认位置 |
| --- | --- |
| 执行历史 | `~/.nl2shell/history.jsonl` |
| 输入历史 | `~/.nl2shell/input_history` |
| 批次汇总 | `~/.nl2shell/batch_<批次ID>.json` |
| 可选运行日志 | `NL2SHELL_LOG_JSON` 指定的位置 |
| 项目配置 | 项目根目录 `.env` |

## 10. 安全机制与边界

### 10.1 决策原则

- HIGH 风险始终阻止，任何运行模式和 `--yes` 都不能绕过。
- 删除、网络访问、工作区外写入和 WARN 操作至少需要普通确认。
- `sudo`、系统级修改、未知命令和复杂 Shell 语法需要完整输入 `yes`。
- 只有已识别、非敏感且满足工作区策略的计划可以自动执行。
- 验证命令不能写入、删除、联网或使用未知复杂语法。

### 10.2 已重点检查的危险形式

- `rm -rf /`、家目录递归删除和递归强制删除。
- `mkfs`、向磁盘设备写入、系统文件清空。
- Fork 炸弹、终止全部进程和系统电源操作。
- `curl ... | bash` 等下载内容直接交给 Shell。
- `find -delete`、`find -exec rm`、通配符删除和命令替换。
- 用 `;`、`&&` 或管道把危险操作藏在组合命令中。

### 10.3 用户仍需承担的判断

- 模型生成的命令是否真正符合业务意图。
- 第三方程序自身是否会修改系统或远端数据。
- 当前目录中的符号链接、挂载点和权限配置是否可信。
- 网络命令连接的主机、下载内容和远程脚本是否可信。
- 文件删除、覆盖、移动和远程操作是否已有备份。

执行重要任务前，推荐先使用 `preview`，并在受限账号、测试目录或独立环境中验证。

## 11. 故障排查

### 11.1 提示缺少 `DEEPSEEK_API_KEY`

确认项目根目录存在 `.env`：

```env
DEEPSEEK_API_KEY=你的真实密钥
```

如果准备使用 Ollama，还需要设置 `LLM_BACKEND=local`，否则程序仍会选择 DeepSeek。

### 11.2 模型连接失败或超时

- DeepSeek：检查网络、密钥、模型名和服务状态。
- Ollama：运行 `ollama list`，确认模型存在；检查 `LOCAL_BASE_URL` 是否可访问。
- 代理环境：当前模型 HTTP 客户端不信任系统代理环境变量，请确保目标地址可以直接访问。
- 程序会自动重试一次；持续失败需要修复网络或配置，而不是重复确认命令。

### 11.3 找不到 Bash

运行 `/doctor` 或 `/status`。Windows 推荐配置：

```env
BASH_PATH=C:\Program Files\Git\bin\bash.exe
```

Linux 可运行 `which bash` 检查。`BASH_PATH` 必须指向文件，不能指向目录。

### 11.4 文件执行前检查失败

- 检查当前工作目录是否正确。
- 核对文件名大小写和扩展名。
- 如果程序给出候选，确认候选确实是目标文件。
- `cp`、`mv` 等命令必须提供有效目标路径。
- 工作区外路径可能被要求额外确认。

### 11.5 验证失败

主命令成功不代表最终状态一定是 `verified`。验证命令可能因为预期不成立、权限、路径或超时失败。查看验证详情和一次性修复建议；修复建议不会自动执行。

### 11.6 命令超时

单次任务和交互任务默认 60 秒。批处理可以调整：

```bash
python cli.py --batch tasks.jsonl --timeout 120
```

不要仅为绕过问题无限提高超时。先确认命令是否在等待输入、网络、锁或持续输出。

### 11.7 Windows 显示或路径问题

- 优先使用 UTF-8 终端，例如 Windows Terminal。
- `.env` 中 Windows 路径按示例直接填写，不加额外引号通常最稳妥。
- Bash 命令中的路径采用 Bash 能理解的形式；Windows Python 的当前目录仍由程序传给 Bash。
- 如果 PowerShell 禁止激活虚拟环境，可直接使用 `.\.venv\Scripts\python.exe cli.py`。

### 11.8 历史或日志无法写入

检查用户主目录和目标日志目录是否可写。运行 `/doctor` 只检查当前工作目录，不检查所有历史或日志目标路径。

## 12. 技术与开发附录

### 12.1 核心模块

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 参数解析、交互循环、任务编排、批处理、历史与 SSH 命令 |
| `core/engine.py` | 模型提示、任务计划生成、澄清和修复建议 |
| `core/llm.py` | DeepSeek/Ollama 客户端与网络重试 |
| `core/task_plan.py` | 任务步骤类型和模型 JSON 解析 |
| `core/safety.py` | SAFE/WARN/HIGH 风险规则和结构化命中结果 |
| `core/impact.py` | 读取、写入、删除、网络、提权和路径影响分析 |
| `core/decision.py` | 将风险和影响映射为自动许可、确认、强确认或阻止 |
| `core/preflight.py` | 文件路径、候选名称和默认目标检查 |
| `core/execution.py` | Bash 调用、进程树终止、超时和输出截断 |
| `core/verification.py` | 只读验证和最终状态判断 |
| `core/history.py` | JSONL 历史查询、导出和检索 |
| `core/settings.py` | 运行方式、首次向导和旧配置迁移 |
| `core/ssh_config.py` | OpenSSH Host 元数据读取 |
| `core/diagnostics.py` | 启动环境检查 |
| `core/structured_log.py` | 可选脱敏 JSONL 运行日志 |

### 12.2 主要内部数据类型

- `TaskPlan`：任务步骤元组和可选澄清问题。
- `TaskStep`：`command`、`explanation`、`expected`、`verification`。
- `SafetyAssessment`：`level`、`reason`、`rule`、`fragment`。
- `CommandImpact`：影响标签、读写路径、是否已识别和摘要。
- `ExecutionDecision`：决策级别和原因。
- `ExecutionResult`：退出码、stdout、stderr、耗时、超时和截断标记。
- `VerificationResult`：验证状态、说明和可选执行结果。

### 12.3 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m compileall -q cli.py core
python -m build
```

Windows 激活命令替换为：

```powershell
.\.venv\Scripts\Activate.ps1
```

### 12.4 自动测试与 CI

测试覆盖计划解析、安全判断、影响分析、文件预检、执行、验证、历史、批处理、SSH、设置和 CLI 流程。GitHub Actions 在以下组合运行测试：

- Ubuntu 与 Windows。
- Python 3.10 与 Python 3.12。

CI 不需要真实 API Key；模型调用由测试替身代替。

### 12.5 版本与变更

当前版本为 0.1.0。版本信息位于 `core/__init__.py` 和 `pyproject.toml`，变更记录见项目根目录的 `CHANGELOG.md`。
