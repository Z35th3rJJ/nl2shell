# NL2Shell

**用中文说明目标，查看计划，确认后执行。**

NL2Shell 是一个面向 Linux 新手的智能 Shell 助手。它使用大语言模型理解需求，检查操作与路径，显示执行预览，并记录实际结果。

默认使用白名单策略。实验黑名单允许更多系统查询和受控原生写入。两种模式的能力与验证保证不同，见[策略对比](#白名单与实验黑名单)。

[快速开始](#快速开始) · [支持的功能](#支持的功能) · [命令行用法](#命令行与交互) · [测试与评测](#测试与真实模型评测) · [限制](#安全边界与限制)

## 快速开始

### 1. 准备环境

推荐在 **Ubuntu 24.04 / WSL 的 Linux 文件系统**中运行，使用 Python 3.10 或更新版本，以及已安装并启动的 Ollama。

Windows 用于开发。Linux 原生命令执行需要 Ubuntu / WSL；实验黑名单还需要 `bwrap`、`prlimit`、GNU 工具和可用的 Linux 用户命名空间。缺少隔离条件时会停止，不会降级到主机执行。

### 2. 首次安装

在 Linux 终端执行。以下示例将仓库放在 `~/projects/nl2shell`：

```bash
mkdir -p "$HOME/projects" &&
git clone https://github.com/Z35th3rJJ/nl2shell.git "$HOME/projects/nl2shell" &&
cd "$HOME/projects/nl2shell" &&
python3 -m venv .venv &&
.venv/bin/python -m pip install -e ".[dev]" &&
{ test -e .env || cp .env.example .env; } &&
ollama pull qwen2.5-coder:7b
```

已有仓库时，进入仓库后使用 `git pull --ff-only origin main` 更新。已有 `.env` 不会被上面的命令覆盖；上一步失败时，后续安装命令不会继续。

默认模型为 `qwen2.5-coder:7b`，接口为 `http://localhost:11434/v1`。若 Ollama 服务未启动，在另一个终端执行 `ollama serve`。其他配置见 [.env.example](.env.example)。

### 3. 创建测试目录并启动

复制整段命令执行。每次都会创建新目录，保留旧文件：

```bash
repo="$HOME/projects/nl2shell"
workdir="$(mktemp -d "$HOME/nl2shell-demo.XXXXXX")" &&
mkdir -p "$workdir/backup" "$workdir/logs/nested" &&
printf 'INFO start\nERROR.* literal match\nitem [1] ready\n' > "$workdir/sample.txt" &&
printf 'first log\n' > "$workdir/logs/app.log" &&
printf 'nested error log\n' > "$workdir/logs/nested/child.log" &&
cd "$workdir" &&
RUN_MODE=confirm SETUP_COMPLETE=true \
"$repo/.venv/bin/python" "$repo/cli.py" --policy whitelist
```

出现“你想做什么？”后，输入：

```text
将 sample.txt 复制到 backup 并命名为 sample_copy.txt
```

预览应显示 `sample.txt → backup/sample_copy.txt`。只有预览正确时才输入 `yes`。输入 `no` 取消，输入 `/exit` 退出。

启动时的当前目录就是工作目录。程序只处理该目录允许范围内的文件。

## 支持的功能

下列示例可在上面的测试目录中使用。自然语言理解仍可能出错，应以实际预览为准。

| 功能 | 输入示例 |
| --- | --- |
| 目录浏览 | `列出当前目录的内容，包括隐藏文件` |
| 文件名筛选 | `递归查找 logs 目录中名为 *.log 的普通文件` |
| 文件计数 | `统计 logs 目录及子目录中的文件数量` |
| 字面文本搜索 | `在 sample.txt 中搜索字面文本 ERROR.*` |
| 文本统计与首尾查看 | `统计 sample.txt 的行数`、`查看 sample.txt 的前两行` |
| inode 查询 | `显示 sample.txt 的 inode` |
| 最大文件查询 | `查找 logs 目录及子目录中最大的文件` |
| 复制与指定新文件名 | `将 sample.txt 复制到 backup 并命名为 sample_copy.txt` |
| 移动与重命名 | `将 backup/sample_copy.txt 重命名为 backup/renamed.txt`，需先生成该副本 |
| 创建、回收与恢复 | 创建空文件或目录、移入回收区、按编号恢复 |
| 条件备份与整理 | 按文件名、时间、大小筛选后备份；按扩展名整理 |
| 系统资源查询 | `查看当前工作目录所在文件系统的磁盘空间`、`查看内存使用情况` |

真正缺少条件时，程序会追问。例如输入“将 sample.txt 复制到合适的目录”，应询问目标目录；回答“backup”后，再显示实际复制预览。

重复复制时，已有目标会通过编号避让。最终名称以实际预览为准。

## 白名单与实验黑名单

命令策略与运行方式分别配置。日常建议保留默认白名单。

| 项目 | 白名单 `whitelist` | 实验黑名单 `blacklist` |
| --- | --- | --- |
| 命令准入 | 支持的操作与固定参数模板 | 系统命令参数数组，通过危险命令及参数检查 |
| 查询能力 | 上表中的受支持查询 | 扩展到日期、目录大小、校验值、Linux 块设备与挂载信息等低风险查询 |
| 查询结果 | 使用对应的独立结果检查 | 无通用目标验证器，执行后记录为 `unverified` |
| 文件写入 | 受保护的 Python 工具 | 复制用 `cp`，移动、改名、整理、回收及恢复用 `mv`，创建目录用 `mkdir`，创建空文件用固定参数的 `dd` |
| 写入保护 | 路径检查、防覆盖、人工确认、结果验证 | 保留这些保护，原生命令绑定已授权的文件清单 |
| 执行环境 | 原生 Linux 查询；Python 文件操作 | 扩展查询在只读沙箱运行；受控原生写入使用可写工作区沙箱 |
| 使用建议 | 默认、主要使用路径 | 实验与对比，使用独立测试目录 |

退出助手后，可在同一测试目录启动黑名单：

```bash
RUN_MODE=confirm SETUP_COMPLETE=true \
"$HOME/projects/nl2shell/.venv/bin/python" \
"$HOME/projects/nl2shell/cli.py" --policy blacklist
```

可输入“我们挂载了几个硬盘”“列出当前 Linux 环境中已挂载的文件系统”“显示当前UTC日期，格式为 YYYY-MM-DD”或“计算 sample.txt 的 SHA-256 校验值”。

黑名单按危险程序、参数副作用和隔离条件判断，不要求匹配固定操作模板。允许 `mount` 仅列出信息、普通 `awk` 单行过滤及 `sed --sandbox`；`rm`、提权、实际挂载变更、脚本执行和文件重定向仍受阻断。

黑名单优先复用已有查询能力。能够从完整原始请求确定的目录浏览、文件计数、字面搜索、inode 和最大文件查询，使用共享需求规则生成参数，不再让通用模型重复判断是否支持。这些命令仍在只读 Linux 沙箱内执行，保留参数与路径检查、人工确认和独立结果验证。额外条件或歧义不能完整解释时，不使用该确定性路径；扩展查询仍由模型生成并保留“未独立验证”状态。

也可在 `.env` 设置 `COMMAND_POLICY=blacklist`。命令行 `--policy` 只覆盖本次启动。黑名单不是任意 Shell 执行入口，也不开放任意写脚本、覆盖或永久删除。

<details>
<summary>黑名单的隔离条件与资源限制</summary>

- 命令子进程隔离网络、PID 等命名空间，清空环境并移除能力权限。模型请求仍由主进程发起。
- 工作目录不接受符号链接、硬链接或特殊文件；敏感路径与审计记录在沙箱中遮蔽。
- 工作区扫描上限一万项、五秒；超过时需要缩小工作目录。
- 子进程内存限制为 512 MiB，CPU 时间为 20 秒，并限制进程数及文件描述符。
- 单文件写入上限：扩展查询为 1 MiB，受控写入为 1 GiB；同时受任务超时限制。
- `lsblk`、`findmnt` 绑定当前 Linux 的只读系统元数据快照，`mount` 读取该快照的挂载表；原始模型参数与输入绑定会记录。不会把沙箱自身的临时挂载当成 WSL 主环境。
- 预览和结果区说明数据范围，分别列出可见块设备、已挂载块设备及挂载记录数。设备编号去重，交换设备不当作文件系统挂载；这些值不能推断 Windows 主机物理硬盘数量。
- `/sys` 仅以只读方式提供内核元数据。普通查询的 `/proc` 是私有 PID 命名空间内的只读视图；挂载与设备查询使用受限元数据快照。不映射真实磁盘设备、其他用户目录或 Windows 文件系统。
- 原生写入依赖 GNU `cp`、`mv` 的 `--update=none`、`--no-copy` 等参数；旧工具不兼容时会停止。
- 空文件使用固定的 `dd if=/dev/null ... conv=excl`，目标已存在时不会覆盖；普通的 `dd` 请求仍在黑名单中。缺失的目标父目录由受控 `mkdir` 创建并显示在预览中。
- 回收及恢复使用受控 `mv` 移动文件。回收区索引由程序维护；沙箱只开放本次授权的记录内容，其他记录内容保持遮蔽。
- 扩展查询必须人工确认；`--yes`、批处理不替代授权，实验黑名单暂不支持 Agent。

实现见 [core/command_policy.py](core/command_policy.py)。

</details>

## 程序如何完成任务

白名单中的只读查询：

```mermaid
flowchart LR
    A[用户需求与补充回答] --> B[意图识别与计划生成]
    B --> C[参数及路径检查]
    C --> D[独立需求比较]
    D --> E[候选命令参数审查]
    E --> F[用户确认]
    F --> G[实际执行]
    G --> H[独立结果检查与历史记录]
```

模型负责理解与提议。程序负责参数、路径、授权和执行事实。有限的共享规则可从完整原始需求恢复已知默认值或模型误判，不能以主计划复制品冒充独立参考。

白名单文件写入使用准备好的实际源、目标清单，经确认后调用 Python 工具。实验黑名单的扩展查询使用不同入口：危险规则检查、沙箱检查、确认、执行和退出状态记录；不会把它描述为已经通过白名单的独立结果验证。

### 如何理解结果

| 显示结果 | 含义 |
| --- | --- |
| 仅预览、审查通过 | 还没有执行 |
| 已验证成功 | 实际结果通过对应的独立检查 |
| 无匹配 | 查询完成但没有匹配项，不等于命令执行失败 |
| 已执行但未验证 | 命令运行过，目标结果尚未独立确认 |
| 已停止、已取消 | 查看具体阶段及“执行状态”；不能仅凭最终状态猜测是否有修改 |

多步骤任务失败后会停止后续步骤。历史区分执行状态和文件变化状态；无法确认的结果保留为未知，不宣称成功。

## 命令行与交互

以下命令在仓库目录、已激活虚拟环境时执行：

```bash
cd "$HOME/projects/nl2shell"
source .venv/bin/activate

python cli.py --policy whitelist
python cli.py --preview "列出当前目录的内容"
python cli.py --json --yes "查看系统信息"
python cli.py --batch tasks.jsonl
```

`--yes` 只可跳过受支持只读任务的确认，不能授权文件写入。JSON 模式不提供交互授权；批处理只执行只读任务。

`tasks.jsonl` 每行一个任务，可包含已有工作目录 `cwd`：

```json
{"input":"查看系统信息"}
```

| 交互命令 | 作用 |
| --- | --- |
| `/help`、`/status`、`/doctor` | 查看帮助、运行信息、环境检查 |
| `/workspace 路径` | 切换到已有工作目录 |
| `/history` | 查看历史；导出与重放参数见 `/help` |
| `/trash`、`/restore 编号` | 查看回收区、恢复文件 |
| `/voice` | 录音并确认识别文字 |
| `/mode`、`/config` | 临时或持久化调整运行方式 |
| `/exit` | 退出；在澄清阶段输入也会取消任务 |

`RUN_MODE=confirm` 为默认确认方式；`preview` 只生成预览。旧 `auto-safe` 设置仍兼容，但不自动授权写操作。命令策略通过 `--policy` 或 `COMMAND_POLICY` 设置。

### 语音输入（可选）

Linux / WSL 需要可用的麦克风与 PulseAudio 的 `parec`（Ubuntu 软件包 `pulseaudio-utils`）；没有 `parec` 时使用 PipeWire 的 `pw-record`。在项目目录安装可选依赖：

```bash
.venv/bin/python -m pip install -e '.[voice,voice-gpu]'
python3 -m venv "$HOME/.local/share/nl2shell/qwen-asr"
"$HOME/.local/share/nl2shell/qwen-asr/bin/python" -m pip install 'torch==2.6.0' 'qwen-asr==0.0.6'
```

启动助手后输入 `/voice`，说完后按回车结束录音；录音时也可输入 `/cancel` 取消。程序显示识别文字后，输入 `yes` 使用，输入完整文字可修改，输入 `/cancel` 取消。识别文字仍会经过原有计划、审查和执行确认；这个 `yes` 只确认文字，不授权文件操作。

默认使用本地 Qwen3-ASR-0.6B，以 NVIDIA GPU 运行；需要可用的 CUDA 驱动和显存，首次使用需要下载模型。设置 `NL2SHELL_VOICE_MODEL=whisper` 可改用原有 faster-whisper `medium`。GPU 不可用时会提示，不会静默改用 CPU。识别结果默认转换为简体；发生转换时同时显示原文，文件名使用繁体时请在确认步骤输入原文。临时录音处理后删除。模型或麦克风不可用时可继续使用键盘。确认后的文字按所选模型后端处理，云端后端可能接收文字。

<details>
<summary>受限 ReAct Agent 与受控修复</summary>

Agent 仅支持白名单，每轮读取实际观察，再决定下一步：

```bash
python cli.py --policy whitelist --agent --timeout 180 "先查找当前目录这一层的 *.log 文件。有匹配时把本次查到的全部文件复制到 archive；没有匹配则不修改。"
```

- 条件仅支持实际文件数量与明确整数比较；文件选择来自已验证的查找结果。
- 写入逐次预览、确认并验证，`--yes` 不授权写入。
- 最多五次工具调用；失败、取消、未知状态或超时会停止。
- 查找清单需要传给模型时最多三十个名称；不静默处理部分清单。
- 仅符合条件的临时只读失败可申请一次受控修复，仍须重新审查并确认。写操作不会自动重放。
- 普通任务最多三个步骤；复杂条件不能保证完成。Agent 历史不能通过普通固定计划入口直接重放。

</details>

## 安全边界与限制

- **工作区隔离**：任务中的文件路径必须位于当前工作目录内。链接、特殊文件及部分敏感路径受到额外限制。
- **写入授权**：先显示实际路径再确认。已有目标通过编号避让，不直接覆盖；创建空文件不清空原文件。
- **执行前检查**：准备阶段检查整份计划的已知源文件；确认后复查。缺失源文件可在写入前阻断。
- **执行后检查**：复制核对内容与源文件状态，移动核对源、目标位置；退出码 0 不直接等于任务成功。
- **失败保留证据**：已完成的修改保留，后续步骤停止。没有事务式回滚保证，不自动重放失败写入。
- **数据保护**：模型请求与历史使用脱敏，但不能保证识别所有敏感内容。云端后端会接收脱敏后的请求与必要上下文。

白名单的路径检查不是操作系统沙箱。请使用可信的个人工作目录，避免运行期间由其他程序并发修改相同文件。WSL 验收使用 Linux 文件系统，通过 Git 同步代码，不需要挂载 Windows 磁盘。

当前不支持软件安装、权限修改、服务管理、网络或远程操作、任意脚本和永久删除。语音输入依赖本机麦克风与可选模型；WSL 可见设备不等于主机物理硬盘清单。

<details>
<summary>白名单命令与查询限制</summary>

| 命令 | 允许的用途 |
| --- | --- |
| `pwd -P` | 当前工作目录 |
| `ls` | 当前层内容，可按计划包含隐藏项 |
| `find` | 普通文件筛选、计数、最大文件元数据；固定范围、排除规则与 NUL 分隔 |
| `grep -F -n` | 单文件、大小写敏感的字面文本搜索 |
| `wc` | 行数、普通空白分隔单词数、UTF-8 字节数 |
| `head`、`tail` | 明确的首尾位置与 1 至 1000 行 |
| `stat -c %i` | 普通文件 inode |
| `df`、`free`、`uname` | 工作区文件系统空间、内存、系统名及内核架构 |

查询命令使用参数数组，不通过 Shell 解释模型字符串；额外参数或条件不一致时拒绝。

文本查询限单个、不超过 1 MiB 的普通 UTF-8 文件。正则搜索、递归文本搜索及换行文件名暂不支持。单词统计不做中文分词。

inode 与最大文件查询只读取元数据。最大文件按字节数比较，保留全部并列结果；支持当前层或明确的递归范围，不支持额外筛选或前 N 名。计数和最大值由程序核对命令实际输出后计算。

实现见 [core/bash_queries.py](core/bash_queries.py)。旧 Docker 接口用于单独的隔离测试，不支持新增参数数组入口，也不会自动降级到本机执行。

</details>

## Coding Agent 运行诊断

CLI 单任务、批处理中的每项任务及完整 Agent 任务自动分配唯一 `task_id`，旁路记录实际事件。Agent 中间工具调用沿用同一任务标识。诊断记录不改变历史审计字段、JSON 输出、终端布局、确认、安全审查或执行策略。

诊断包位于本机应用数据目录：Windows 为 `%LOCALAPPDATA%/nl2shell/diagnostics/<task_id>/`，Linux 为 `${XDG_DATA_HOME:-~/.local/share}/nl2shell/diagnostics/<task_id>/`，macOS 为 `~/Library/Application Support/nl2shell/diagnostics/<task_id>/`。目录解析后若位于 Git 仓库内则拒绝保存，不回退到项目目录。

每次运行保留 `manifest.json`、顺序递增的 `trace.jsonl`、去重的 `evidence/` 和 `AI_README.md`；失败、拒绝、取消和未完成任务另存 `failure.json`。清单包含 Git SHA、工作树是否有改动、实际加载模块路径及摘要、Python/系统/依赖版本、模型和运行参数。SHA 只标识提交；工作树有改动时需同时核对模块摘要。原始脱敏请求及实际模型响应保留为固定证据，不保证重跑模型可复现同一次输出。

事件通过 `parent_event_id`、输入/输出证据引用和明确异常因果关联。`failed` 表示观察到异常，`propagated` 表示同一异常向外传播；未调用的阶段标为 `blocked` 或 `skipped` 并说明原因。`first_failure` 保留历史首错，`terminal_failure` 表示最终停止故障；直接错误和阻止原因引用后者。每次执行通过 `execution_id` 和 `step` 区分，前一步验证不能代替后一步的记录。格式修正仅统计 `requested`，意图复核另记；曾修正成功的早期异常仍留在轨迹中。

诊断先脱敏再落盘，省略密钥、令牌、认证字段及环境变量映射。已识别的敏感环境变量值仅用于本机脱敏，不写入包。大型文本以内容摘要引用去重，单个证据文件最多 64 KiB，截断及省略字段在索引中标记。诊断存储不可用时不改变原流程，原审计写入失败仍会阻止执行；部分记录失败会在可写的清单中标记。未知格式的敏感信息仍可能无法识别，包只供本机审核，不应上传。

处理预算在复制及序列化前生效：限制单条文本、文本总量、容器项数、节点数和深度，具体上限见 `core/runtime_diagnostics.py`。截断后的内容摘要仅代表有界脱敏内容，不代表完整原始输入。读取附件使用 `read_evidence` 校验当前索引、规范化路径、大小和摘要，拒绝目录外链接；原始业务数据中的 `evidence_ref` 会标为 `untrusted_evidence_ref`，不能自动跟随。

Coding Agent 从包内 `AI_README.md` 开始：读首错、核对原始证据和出错源码，检查测试后提出最小修复及成功标准；获得授权后才修改代码。修复前后保留独立证据。复现默认使用保存响应的测试替身，不自动运行危险命令或真实模型。Python 文件操作的现有检查与写入位于同一工具内，诊断如实记录，不声称额外运行了独立验证器。

## 测试与真实模型评测

从仓库根目录、已激活虚拟环境时运行：

```bash
# 自动化回归
python -m pytest -q

# 黑名单专项：需要满足上述 Linux 隔离条件
python -m unittest discover -s tests -p test_command_policy.py -v

# 真实模型规划评测：少量任务，不执行计划
python eval/run_eval.py --backend local --limit 3
```

[tests/](tests/) 包含程序回归；[eval/operation_cases.py](eval/operation_cases.py) 定义自然语言任务与期望结果。自动化测试、响应回放和真实模型验收应分别统计，不能互相替代。

普通 CLI 历史新增 `measurements`：分别记录模型 SDK 调用耗时、实际 Linux 进程耗时、等待输入时间和记录时的任务总耗时。`active_seconds` 排除等待输入时间。SDK 内部的 HTTP 自动重试不单独计数；`model_call_count` 不是 HTTP 请求数。Python 文件操作没有 Linux 命令耗时，不能将其写成 `cp` 的执行时间。运行信息只保存版本、CPU、内存、Python 和系统信息，不保存完整环境变量。

黑名单扩展查询的任务预算不包含等待澄清回答或执行确认的时间。模型生成、准备检查和执行仍消耗预算；确认后不会重置这些已用时间。记录中的总耗时仍包含人工等待。

白名单与黑名单使用同一固定任务集进行真实 CLI 对比：

```bash
python eval/policy_compare.py --repeats 3
```

此命令需要 Linux、Ollama 和黑名单沙箱依赖。它创建独立合成目录，核对预览后才确认测试写入；不处理用户文件。结果保存在仓库外的 `~/nl2shell-evaluations/policies/`，每轮保留终端输出、历史、文件摘要、JSON 和 CSV。预期拒绝单独计入行为符合数，不能当作执行成功。扩展查询的程序状态仍为“未独立验证”，评测的外部核对结果另行记录。可用 `--cases P07 P10` 选择子集，报告保留实际任务数。

若要在评测工具创建的临时夹具中**真实执行**匹配期望的操作，可增加 `--execute-safe`。它包含文件写入，结果默认保存在仓库外的 `~/nl2shell-evaluations/legacy`。

真人 CLI 验收应保留版本、模型、输入、澄清、预览、确认、实际命令及输出；写入后另行检查真实文件内容。记录模型耗时与命令耗时，区分完成、拒绝、取消和未验证结果，不把未运行项目计为通过。

本 README 不维护容易过时的测试总数或成功率。具体结果以对应提交的执行证据和 [CI 配置](.github/workflows/test.yml)为准。

<details>
<summary>A/B/C 研究评测与提示词对比</summary>

```bash
python eval/study.py --output-dir "$HOME/nl2shell-evaluations/study" --variants A B C --strategy basic
python eval/study.py --output-dir "$HOME/nl2shell-evaluations/study" --variants C --strategy structured
python eval/study.py --output-dir "$HOME/nl2shell-evaluations/study" --variants C --strategy fewshot
python eval/study_supplementary.py --output-dir "$HOME/nl2shell-evaluations/study"
```

A 直接生成候选，B 增加计划，C 增加独立一致性检查。三组共享公共路径与授权检查；A/B/C 不是白名单、黑名单策略的别名。

真实研究要求工作区已提交、输出目录在仓库外。输出登记任务集、版本、时间与模型；不同协议、任务集或未完成结果不能直接比较。研究入口的写命令仍转译到受保护工具，不能据此宣称执行了实验黑名单原生命令。

相关入口：[study.py](eval/study.py)、[study_cases.py](eval/study_cases.py)、[compare.py](eval/compare.py)。

报告指标时保留分母，区分任务数与实际产生的候选数；零分母记为不适用。A 不生成计划，其计划指标不适用。三种提示词均使用接口必需的 JSON 格式，比较的是文案约束，不是不同 API 模式。

</details>

## 项目结构与贡献

```text
cli.py                      交互、预览、确认与历史
core/                       需求、计划、命令策略、执行与验证
tests/                      自动化回归与平台专项测试
eval/                       任务集与真实模型评测
scripts/check_upload.py     上传内容检查
.env.example                示例配置
.github/workflows/          CI 配置
```

开发时只修改必要文件，按风险运行相关测试。提交前检查差异并正常运行上传钩子：

```bash
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push
git diff --check
python scripts/check_upload.py --tracked
```

提交检查暂存内容，推送检查新增提交。不要绕过检查或强制推送。

仓库保留源码、测试、合成夹具、依赖、CI 和示例配置。论文、真实评测结果、日志、密钥及个人配置留在仓库外。完整提交历史用于追踪版本，不以删除失败证据的方式展示成功。
