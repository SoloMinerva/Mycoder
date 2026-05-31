# MyCoder

> 一个轻量级的代码助手 CLI，由大语言模型驱动。  
> 支持 Anthropic 和 OpenAI 兼容接口（SiliconFlow、DeepSeek 等）

---

## 功能特性

| | |
|---|---|
| 💬 | 交互式 REPL 和单次执行两种模式 |
| 🛠️ | 7 个内置工具 — 读 / 写 / 编辑文件、目录列表、内容搜索、终端命令、网页抓取 |
| 💾 | 对话自动保存，支持恢复上次会话 |
| 🧠 | 持久化记忆系统 |
| 📦 | 接近 token 上限时自动压缩上下文 |
| 🔒 | 细粒度权限控制模式 |

---

## 安装

```bash
pip install -e .
```

---

## 配置

将 `.env.example` 复制为 `.env`，填入你的 API Key。

**方式 A — Anthropic**

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

**方式 B — OpenAI 兼容接口**（SiliconFlow、DeepSeek、本地 Ollama 等）

```bash
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
```

---

## 使用方法

```bash
mycoder                                      # 启动交互式 REPL
mycoder "帮我修复 app.py 里的 bug"            # 单次执行
mycoder --model deepseek-ai/DeepSeek-V3     # 指定模型
mycoder --yolo "跑一遍所有测试"              # 跳过所有确认
mycoder --resume                             # 恢复上次会话
```

---

## REPL 内置命令

| 命令 | 说明 |
|------|------|
| `/clear` | 清空对话历史 |
| `/cost` | 查看 token 用量和费用估算 |
| `/compact` | 手动压缩上下文 |
| `/memory` | 查看已保存的记忆 |
| `exit` · `quit` | 退出 MyCoder |

---

## 权限模式

| 参数 | 行为 |
|------|------|
| *(默认)* | 编辑文件和执行命令前询问确认 |
| `--accept-edits` | 自动同意文件编辑，命令仍需确认 |
| `--dont-ask` | 自动拒绝所有确认 |
| `--yolo` | 跳过所有确认提示 |
