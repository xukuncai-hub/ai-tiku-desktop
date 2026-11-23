# AI 题库控制台

**通过本地 API 一键接入 [OCS 网课助手](https://docs.ocsjs.com/) 的跨平台 AI 自动刷题工具——开箱即用，让所有人都能轻松完成本地部署。**

你只需要填入自己的大模型 API Key（支持 DeepSeek 及任意 OpenAI 兼容接口），即可在本地启动一个高并发答题 API 服务，并一键复制配置接入 OCS。后台结合 **远程免费题库 + AI 极速直出 + 抗乱序共享缓存**，兼顾准确率、速度与成本。

---

## 核心特性

- **极速 3 步答题链路**：
  1. **抗乱序共享缓存**：自动记录作答结果，遇到同一题目即使平台打乱选项顺序也能 `0ms` 零成本秒中并重映射选项字母。
  2. **远程题库快查**：接入 `tikuAdapter`（默认 `2s` 快速超时），公共课/通识课直接采信命中。
  3. **AI 兜底直出**：自动处理字体反爬形近错字，支持单选/多选/判断/填空分录题，支持多模态图片题直传。
- **高并发生产服务**：后端采用 Waitress 多线程 WSGI 服务器（默认 16 并发线程），支持多账号多窗口同时挂机。
- **桌面端监控台（Linux / Windows）**：
  - 一键启动 / 停止 / 重启服务，支持系统托盘常驻与单实例唤醒。
  - **实时成本与余额监控**：直连 DeepSeek `/user/balance` 查询账户真实余额（低余额标红提醒），本地精确统计今日/累计 Token 消耗与均价。
  - **作答记录与错题管理**：按来源筛选、关键词搜索、单条删除与清理缓存，支持导入/导出 CSV 与实时日志过滤。
  - **可视化配置中心**：图形化修改 `.env` 参数，内置 OCS 题库配置一键复制。

---

## 目录结构

```text
ai-tiku-desktop/
├─ desktop/                 # Rust + Tauri v2 桌面控制台源码
│  ├─ ui/                   # 前端界面（原生 HTML/CSS/JS，深浅双主题）
│  └─ src-tauri/            # Rust 后端（跨平台进程管理、SQLite 统计、托盘）
├─ ai-server/               # Python 答题后端（Flask + Waitress）
│  ├─ main.py               # 核心答题服务
│  ├─ utils.py              # 公共工具与数据库封装
│  ├─ ocs_config.json       # OCS 网课助手题库配置模板
│  ├─ .env.template         # 环境变量配置模板
│  ├─ 导出题库.py           # 导出缓存/历史/错题为 CSV
│  ├─ 导入题库.py           # 批量导入 CSV/JSON 到缓存或本地库
│  └─ 应用修正.py           # 批量应用人工修正 CSV
├─ tiku-adapter/            # 上游免费题库适配器（默认监听 :8060）
└─ .github/workflows/       # GitHub Actions 双平台自动打包工作流
```

---

## 快速开始

### 1. 安装桌面端
- **Linux (Debian / Ubuntu)**：前往仓库 **Actions → Artifacts** 下载 `.deb` 安装包（或本地编译后使用）：
  ```bash
  sudo dpkg -i AI题库控制台_0.1.0_amd64.deb
  ```
- **Windows**：前往仓库 **Actions → Artifacts** 下载 `ai-tiku-windows-installer` 中的 `.exe` 或 `.msi` 安装包安装。

### 2. 准备运行环境
1. 安装 Python 包管理器 **[uv](https://docs.astral.sh/uv/)**（用于自动管理 `ai-server` 虚拟环境与依赖）。
2. 复制 `ai-server/.env.template` 为 `ai-server/.env`，填入你的 `OPENAI_API_KEY` 与 `OPENAI_BASE_URL`（如 `https://api.deepseek.com/v1`）。
3. *(仅 Windows 需要)* 下载对应系统的 `tikuAdapter.exe` 放入 `tiku-adapter/` 目录。

### 3. 启动与接入 OCS
1. 打开 **AI 题库控制台**，点击 **「启动服务」**（确认 `:5000` 与 `:8060` 指示灯均变绿）。
2. 进入左侧 **「OCS 接入」** 页面，点击 **「一键复制配置」**。
3. 打开 OCS 网课助手，粘贴到「题库配置」即可开始自动答题。

---

## 常用环境变量说明（`ai-server/.env`）

| 变量名 | 默认值 | 说明 |
|---|---|---|
| `OPENAI_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |
| `OPENAI_API_KEY` | - | API 密钥 |
| `OPENAI_MODEL` | `deepseek-flash` | 默认答题模型 |
| `OPENAI_MODEL_HARD` | 跟随默认 | 多选题专用模型 |
| `OPENAI_THINKING` | `auto` | 思考档位（`auto` / `low` / `medium` / `high` / `disabled`） |
| `OPENAI_SAMPLES` | `1` | 采样次数（`1` 为单次直出） |
| `OPENAI_MAX_TOKENS` | `4096` | 单次最大输出 Token |
| `BANK_FIRST` | `1` | 是否优先查询远程题库 |
| `BANK_USE` | `buguake` | `tikuAdapter` 题库源 |
| `BANK_TIMEOUT` | `2` | 远程题库请求超时（秒） |
| `ARBITER` | `direct` | 题库仲裁策略（`direct` 直接采信 / `agree` AI 复核） |
| `THREADS` | `16` | Waitress 并发服务线程数 |
| `BIND_HOST` | `127.0.0.1` | 服务端监听地址 |

---

## 本地开发与编译

```bash
# 运行 Python 后端单元测试
cd ai-server
uv run --locked python -m unittest discover -s tests -v

# 编译桌面端 Release / 安装包
cd ../desktop
npx --yes @tauri-apps/cli@2 build
```
