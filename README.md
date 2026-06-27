# nl2shell · 智能 Shell 命令助手

用中文描述你想做的事，自动生成并执行对应的 Linux 命令。

## 快速开始

```bash
# 1. 克隆项目
git clone <your-repo-url>
cd nl2shell

# 2. 创建虚拟环境并安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置 API Key
cp .env.example .env
# 编辑 .env，填入你的 DeepSeek API Key

# 4. 运行
python3 cli.py
```

## 使用示例

```
你想做什么？> 列出当前目录所有文件
命令：ls -la
执行？(y/n) > y

你想做什么？> 找出最大的三个文件
命令：du -ah . | sort -rh | head -3
执行？(y/n) > y
```

## 项目结构

```
nl2shell/
├── core/
│   ├── llm.py       # DeepSeek API 封装
│   └── engine.py    # 核心内核：命令生成与上下文管理
├── cli.py           # 命令行界面
├── requirements.txt
└── .env.example     # API Key 配置模板
```
