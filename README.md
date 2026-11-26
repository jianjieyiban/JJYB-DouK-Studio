# 🎬 JJYB-DouK-Studio

> 基于 DouK-Downloader 的现代化 Web 图形工作台 —— 抖音 / TikTok 视频、图集、合集、评论一站式解析下载与文案采集，内置 **AI 语音识别**自动提取视频解说文案。

[![Release](https://img.shields.io/badge/release-v1.0.0-blue)](../../releases)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](#安装步骤)
[![Python](https://img.shields.io/badge/python-3.10%2B-green)](#安装步骤)

---

## ✨ 功能特性

| 模块 | 说明 |
| --- | --- |
| 🔗 链接解析 | 粘贴分享口令 / 短链 / 主页链接，自动识别视频、图集、合集、评论区 |
| 📥 多清晰度下载 | 1080P / 720P / 音轨自由选择，直连 + yt-dlp 双引擎，断点续传 |
| 📋 批量解析 | 一次粘贴最多 10 条链接，批量解析后统一勾选下载 |
| 👤 账号主页采集 | 输入主页链接 / sec_uid，批量采集博主全部作品并一键下载 |
| ✎ 文案采集 | 单条 / 多条 / 博主主页整包抓取标题文案，支持 TXT / CSV / JSON / Markdown 导出 |
| 🎙 AI 解说文案 | 本地语音识别（faster-whisper）提取视频口播 / 解说文案，可折叠查看、随文案一并导出 |
| 💬 评论区采集 | 抓取视频评论与楼中楼回复，支持导出 |
| 🔐 抖音登录 | 一键扫码登录 / 自动导入浏览器 Cookie / 手动粘贴 Cookie 三种方式，支持有效性校验 |
| 🌐 浏览器兜底 | 引擎签名失效时自动打开可见浏览器窗口抓取真实接口数据，抗风控 |
| ⚙️ 可视化设置 | 下载目录、并发数、引擎参数图形化配置 |

## 🗂 目录结构要求

DouK Studio 是 DouK-Downloader 的「外挂工作台」，需要放在 **DouK-Downloader V5.7 主程序目录内**（与 `main.exe` 同级目录下）：

```text
DouK-Downloader_V5.7/
├── main.exe              ← DouK-Downloader 原版主程序（引擎）
└── DouK-Studio/          ← 本项目放这里
    ├── server.py
    ├── asr_worker.py
    ├── static/
    ├── bin/yt-dlp.exe
    └── 启动 DouK Studio.bat
```

## 🚀 安装步骤

### 1. 环境要求

- Windows 10 / 11
- Python 3.10+（开发环境为 3.13，server 仅用标准库，无需 pip 依赖）
- [DouK-Downloader V5.7](https://github.com/) 主程序（提供解析引擎）

### 2. 启动

```bat
:: 克隆到 DouK-Downloader_V5.7 目录下
git clone https://github.com/jianjieyiban/JJYB-DouK-Studio.git DouK-Studio
cd DouK-Studio
:: 双击「启动 DouK Studio.bat」，或：
python server.py
```

浏览器访问 `http://127.0.0.1:8765` 即可使用。

### 3.（可选）启用 AI 解说文案识别

```bat
cd DouK-Studio
python -m venv .venv
.venv\Scripts\pip install faster-whisper
```

首次使用会自动从镜像下载 whisper small 模型（约 460MB，缓存于 `data/huggingface`），之后完全离线识别。

## 📖 使用说明

1. **下载视频**：粘贴链接 → 选择清晰度 → 加入下载
2. **采集文案**：进入「文案采集」→ 单条 / 多条 / 博主主页三种模式 → 勾选后导出
3. **提取解说**：文案列表勾选条目 → 点击「🎙 提取解说文案（AI）」→ 已下载过的视频自动复用本地文件，秒级出结果
4. **抖音登录**（可选）：设置页 → 登录管理，不登录也可正常解析下载

## 🧠 AI 语音识别说明

- 模型：`faster-whisper small`（int8 量化，CPU 实时）
- 已下载的视频按 `[作品ID]` 自动匹配本地文件，无需重新下载
- 纯音乐 / 无口播视频会标记「未识别到口播语音」
- 识别完全在本地进行，不上传任何数据

## ⚠️ 免责声明

本项目仅供个人学习与研究，请勿用于任何商业用途或批量爬取。请遵守目标平台的服务条款，下载内容的版权归原作者所有。使用本项目产生的任何风险由使用者自行承担。

## 📄 License

[MIT](LICENSE)
