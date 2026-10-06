# nl2shell 使用说明

## 1. 运行前准备

主要环境是 Linux、Python 3.10 及以上、Ollama。文件操作不依赖 Bash；磁盘、内存和系统信息查询需要可用的 Linux Bash。

安装依赖并复制 `.env.example`。默认使用本地 `qwen2.5-coder:7b`。已有其他模型时，设置 `LOCAL_MODEL`，并确认模型已拉取且 Ollama 服务可访问。`/doctor` 检查配置和执行环境，不发送模型请求；配置就绪不代表模型服务已经可用。

## 2. 工作目录

程序启动目录就是工作目录。使用 `/workspace` 查看，使用 `/workspace /path/to/directory` 切换到已有目录。切换后清空旧任务上下文。

全部文件操作限制在这个目录内。工作目录本身不能复制、移动或回收。文件路径支持相对路径；绝对路径也必须在工作目录内。第一版不跟随符号链接，不处理设备文件。

## 3. 提出需求

说明要处理的对象、条件、目标和范围。例如：

```text
找出当前目录这一层最近七天修改、超过 10 MB 的 PDF
按文件类型整理 organize 目录这一层，不删除文件
备份当前目录及子目录最近七天修改的 Python 文件到 backup，保留目录结构
```

模型最多规划三个步骤。后续复制、移动或回收步骤可引用前面的查找结果。程序生成实际文件清单，模型不编造文件名单。

信息不足时，程序询问用户。每个请求最多进行三轮补充；仍缺信息则停止，返回 `needs_clarification`。模型格式错误最多修正一次。模型连接失败、拒绝或非法输出均不会执行文件操作。

时间条件“最近七天”指最近 7×24 小时。MB 采用十进制，即 1 MB=1000000 字节。“当前目录”是当前层；包含子目录时应明确说明。扩展名匹配区分大小写。

## 4. 确认与结果

程序显示操作、实际文件清单和最终目标名称。完整输入 `yes` 才执行。输入其他内容、结束输入或在确认时中断，均不执行。

已有同名文件时加编号，例如 `report_1.txt`、`archive_1.tar.gz`。不会覆盖已有内容。确认后文件清单变化，程序要求再次确认；连续变化则停止。

结果包含逐步状态和逐文件状态。部分成功后失败，保留已经完成的结果，停止后续步骤。不自动回滚，不自动执行修复建议。复制过程中失败时，可能保留不完整副本；失败项会显示目标路径，请检查后再处理。

跨文件系统的移动、回收或恢复会失败。程序不会暗中改成复制后删除。

## 5. 回收与恢复

删除只移入当前工作目录的 `.nl2shell-trash`。记录原路径、时间和恢复编号。

```text
/trash
/restore 32位恢复编号
```

恢复前也需要确认。同名时生成新名称，保留两份。没有自动清空功能。普通任务不能直接修改回收区。文件查找排除回收区；条件备份同时排除本次备份目标目录。

第一版按类型整理时，将文件移动到其所在目录下的扩展名分类目录，例如 `pdf`、`txt`；无扩展名文件进入 `no_extension`。已经位于对应分类目录的文件不再重复整理。

## 6. 模型计划格式

完整计划：

```json
{"status":"ready","steps":[
  {"operation":"find_files","parameters":{"path":".","recursive":true,"pattern":"*.py","modified_within_days":7},"explanation":"查找最近修改的 Python 文件"},
  {"operation":"copy_files","parameters":{"source_step":1,"destination":"backup","preserve_structure":true},"explanation":"备份并保留目录结构"}
]}
```

缺信息：

```json
{"status":"need_clarification","clarification":"是否包含子目录？"}
```

不支持：

```json
{"status":"unsupported","reason":"第一版不支持安装软件"}
```

步骤只包含 `operation`、`parameters`、`explanation`。旧 `command`、模型指定的验证命令和未知字段都不允许进入主执行流程。

| 操作 | 参数 |
| --- | --- |
| `find_files`、`count_files` | 必需 `path`、`recursive`；可选 `pattern`、`modified_within_days`、`size_gt_bytes` |
| `create_file`、`create_directory` | `path` |
| `copy_files` | `sources` 或 `source_step`、`destination`；可选 `preserve_structure` |
| `move_files` | `sources` 或 `source_step`、`destination` |
| `rename` | `source`、`destination` |
| `trash` | `sources` 或 `source_step` |
| `restore` | `trash_id` |
| `list_trash` | 空对象 |
| `organize_files` | `path`、`recursive`、`group_by=extension` |
| `system_info` | `query=disk/memory/system` |

`source_step` 从 1 开始，只能引用前面的 `find_files`。复制或移动的 `destination` 是目录；重命名的 `destination` 是完整目标路径。操作对象在预览时必须已经存在；创建后再处理同一个新文件，请分为两次任务。

## 7. 脚本与历史

`--json` 的顶层字段仍为 `status`、`risk_level`、`steps`、`verification`、`duration_seconds`、`error`。步骤结果现在包含固定操作及实际文件结果。

JSON 模式不询问：需要补充时返回 `needs_clarification`；写入任务因没有人工确认而返回 `cancelled`。`--yes` 仅跳过只读任务的确认。批处理同样只执行只读任务。

任务状态：`preview`、`needs_clarification`、`unsupported`、`blocked`、`cancelled`、`plan_failed`、`verified`、`unverified`、`execution_failed`、`interrupted`、`audit_failed`。

历史位置仍是 `~/.nl2shell/history.jsonl`。执行前记录授权，执行后记录结果。审计不可写时不开始修改文件。重放按原请求重新规划和检查，不直接执行旧记录里的命令。

输入历史、任务历史和运行日志使用统一脱敏器。不要在任务中提供私钥或密码。脱敏是已知格式的保护，不代表能识别所有敏感信息。

## 8. 验证与评测

复制比较内容摘要。移动、回收、恢复检查原位置、目标位置及内容。创建检查文件类型和空文件内容。查询检查执行状态及输出。模型不能决定检查命令。

```bash
python -m pytest -q
python eval/run_eval.py --backend local
python eval/run_eval.py --backend local --execute-safe
python eval/compare.py eval/结果A.json eval/结果B.json
```

评测使用独立临时目录，逐条准备相同样本。只有匹配预设操作和参数的模型计划才执行。独立检查结果包含应新增、移动或保留的文件，额外修改也算失败。

报告保存模型名称、模型地址、代码提交、工作区是否有未提交改动、用例版本和逐条结果。未执行时任务完成率为 `null`。必要追问率目前检查是否追问；问题本身是否有帮助，还需要人工阅读逐条结果。

真实 Ollama 测试与使用模型替身的程序测试是两件事。不能用替身测试的成功率作为模型效果。
