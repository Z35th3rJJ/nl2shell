# nl2shell · 智能 Shell 命令助手

用中文描述你想做的事，自动生成并执行对应的 Linux 命令。核心特性：**安全审查拦截 + 命令解释 + 意图模糊澄清**。

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

# 4. 运行（默认每条命令确认）
python3 cli.py

# 自动模式：仅自动执行 SAFE 命令，WARN/HIGH 一律跳过并记入 history
python3 cli.py --auto
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

### 意图模糊澄清（多轮对话）

当描述不够明确时，系统会追问一个具体问题，而不是猜测或拒绝：

```
你想做什么？> 清理一下
❓ 你是要删除 .log 日志文件、.tmp 临时文件，还是两者都删？
你的回答> 删除所有 .log 文件
  命令：find . -name "*.log" -delete
  说明：递归删除当前目录下所有 .log 文件
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

每次生成后的最终状态都会写入用户目录的 `~/.nl2shell/history.jsonl`，包含输入、命令、风险等级、执行结果和错误摘要。CLI 内输入 `history` 可查看最近 20 条，输入 `history 50` 可查看最近 50 条。

程序只读取标准 `~/.ssh/config` 中的 `Host` 别名，帮助模型生成 `ssh <别名>`；私钥仍由 OpenSSH/ssh-agent 使用，程序不会读取或保存私钥。SSH 配置文件不在默认位置时，可设置：

```
SSH_CONFIG_PATH=~/.ssh/config
```
