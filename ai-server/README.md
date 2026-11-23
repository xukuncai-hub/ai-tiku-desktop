# OCS AI Answerer Server

一个基于 Python Flask 和 OpenAI 接口（支持 DeepSeek/Qwen 等）的 OCS 网课助手题库服务器。

本项目旨在为 [OCS 网课助手](https://docs.ocsjs.com/) 提供一个本地化的、高智能的 AI 查题后端。它接收 OCS 发送的题目，通过调用大模型（LLM）进行推理，并将答案格式化返回给 OCS 脚本自动答题。

## ✨ 特性

-   **🤖 多模型支持**: 支持兼容 OpenAI 的 Chat Completions 和 Responses API，需要模型和服务商支持严格 JSON Schema 输出。
-   **🧩 结构化输出**: 用 JSON Schema 约束答案和解析，并在返回 OCS 前校验字段。
-   **🎨 炫彩日志**: 控制台实时显示彩色日志，清晰展示题目、选项、AI 推理结果及解析。
-   **🧹 智能清洗**: 自动去除选项中的多余空行和格式杂质，提高 AI 识别准确率。
-   **🧩 多题型适配**: 针对单选、多选、判断、填空题定制不同的 Prompt，大幅提升准确率。
-   **✅ 判断题适配**: 将判断题的选项字母按实际选项对应成“对”或“错”；无法确定时返回错误，不猜测选项顺序。


## 🛠️ 安装与运行

### 直接运行

下载Release包，解压后复制 `.env.template` 为 `.env` ，并修改其中的`OPENAI_API_KEY`和`OPENAI_BASE_URL`，然后运行`OCSAnswererWrapper.exe`即可。

### 1. 克隆或下载本项目
```bash
git clone https://github.com/Zephyr-Lyu/OCSLLMAnswerer.git
cd OCSLLMAnswerer
```

### 2. 使用 uv 安装依赖

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。项目要求 Python 3.12 或以上；uv 会按 `.python-version` 创建项目虚拟环境，并根据 `uv.lock` 安装依赖：

```bash
uv sync --locked
```

### 3. 配置环境变量
复制 `.env.template` 为 `.env`（如果不存在则新建），并填入您的 API Key。模板使用 OpenRouter：

```ini
OPENAI_BASE_URL="https://openrouter.ai/api/v1"
OPENAI_API_KEY="your-openrouter-api-key"
OPENAI_MODEL="openai/gpt-4o-mini"
OPENAI_API_FORMAT="chat_completions"
OPENAI_TIMEOUT_SECONDS="50"
```

`OPENAI_API_FORMAT` 可选 `chat_completions` 或 `responses`。两个协议均要求模型和服务商支持 `json_schema` 严格结构化输出；不支持时会返回错误，不会自动改用普通 JSON 模式。使用 OpenRouter 时会设置 `require_parameters`，请检查所选模型和上游提供商是否支持结构化输出。已有 `.env` 不会自动更新。

`OPENAI_TIMEOUT_SECONDS` 是 SDK 的网络操作超时，不保证整个请求在该时间内结束；SDK 自动重试已关闭。OCS 的超时时间可能因版本和设置而异，控制台日志中的耗时可以帮助排查。服务端打印答案或 HTTP 200 并不证明 OCS 已收到或已匹配答案。

启动服务：

```bash
uv run --locked python main.py
```

运行测试：

```bash
uv run --locked python -m unittest discover -s tests -v
```

**启动成功后，服务器默认运行在 `http://0.0.0.0:5000`。**

## 🖥️ OCS 脚本配置

打开 OCS 网课助手的【全局设置】 -> 【题库配置】，点击【新建】，粘贴以下配置：

```json
[
    {
        "name": "AI题库",
        "homepage": "http://localhost:5000",
        "url": "http://localhost:5000/search",
        "method": "post",
        "type": "GM_xmlhttpRequest",
        "contentType": "json",
        "data": {
            "title": "${title}",
            "options": "${options}",
            "type": "${type}"
        },
        "handler": "return (res) => res.code === 1 ? [res.question, res.answer, {ai: res.analysis}] : [res.msg || '题库查询失败', undefined]"
    }
]
```
> **注意**: 如果您部署在云服务器上，请将 `localhost` 替换为服务器 IP。
> 配置导入后才会更新 OCS 中的处理器；如果继续使用旧配置，失败时 OCS 不会显示服务端返回的提示。错误的详情和请求编号可在服务端日志查看。



## ⚠️ 免责声明

本项目仅供学习交流使用，请勿用于违反学校规定或法律法规的用途。开发者不对使用本项目产生的任何后果负责。
