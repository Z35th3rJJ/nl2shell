# nl2shell · 智能 Shell 命令助手

用中文描述任务，系统生成最多三步的 Bash 计划，完成影响分析、安全决策、受控执行、结果验证与历史审计。

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
│   └── safety.py    # 安全审查：危险命令检测与风险分级
├── eval/
│   ├── testcases.json   # 50 条中文指令测试集
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

| 运行方式 | 行为 |
| --- | --- |
| `preview`（预览） | 只展示计划、影响和验证方案，不调用 Bash |
| `confirm`（确认执行，推荐） | 展示整份计划，确认后执行并验证 |
| `auto-safe`（安全自动） | 安全操作自动执行；删除、网络等敏感操作暂停确认 |

毁灭性高危命令始终阻止；`sudo`、系统级修改和未知 Shell 语法必须强确认。验证失败时只生成一次修复建议，不自动执行修复。

文件操作在执行前会核对本地路径。文件名大小写或扩展名可能写错时，程序会展示候选并要求确认；单文件复制到自身时可直接回车采用 `README_copy.md` 一类的不冲突默认名称。修正后的命令会再次检查，未通过时不会调用 Bash。工作区边界按规范化后的真实路径判断。

首次运行会选择并保存默认方式，之后直接进入 Shell。运行中可使用：

```text
/mode             临时切换运行方式
/config           修改并保存默认运行方式
/status           查看模型、目录、运行方式和 Bash 状态
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
