这份 `README.md` 的内容已经非常清晰完整，主要的优化方向是**提升视觉层级、加强排版设计感、利用 Emoji 增加可读性**，并规范代码块和表格的展现形式。

优化后的版本在保持原生 Markdown 兼容性的同时，加入了更现代化的开源项目文档排版风格。你可以直接复制以下内容替换原文件：

---

```markdown
# 📡 电波推送 — AstrBot Plugin

[![AstrBot Plugin](https://img.shields.io/badge/AstrBot-Plugin-blueviolet?style=flat-square)](https://github.com/AstrBot-Official/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

监控指定 Twitter/X 账号的最新推文，自动生成 **Material Design 3 动态配色卡片（PNG）**。支持推文翻译、引推嵌套以及图片 OCR 提取。

---

## ✨ 核心功能

* **🔍 推文轮询监控**：后台高效异步轮询，发现新推文自动触发推送。
* **🎨 MD3 渲染卡片**：基于本地 Playwright 渲染，根据推文配图自动生成 Material Design 3 动态取色卡片。
* **🌙 昼夜模式自适应**：智能识别时间（18:00–06:00），自动切换深色/浅色皮肤。
* **🤖 智能多语言翻译**：对接 AstrBot 顶层 LLM 能力，深度翻译推文正文、长文 Article、NoteTweet 及图片内文字。
* **📐 图像自适应排版**：支持 1/2/3/4 张配图自适应网格，响应式无缝布局。
* **📦 聚合防刷屏推送**：通过 Group Forward (Node) 节点合并发送图片，保持群聊整洁。
* **🎞️ 媒体原生直发**：视频与 GIF 动图不压缩为卡片，直接发送原生多媒体消息。
* **🔗 完美支持引用推文**：卡片内嵌左侧竖线样式完美还原引推，翻译时自动联动处理。
* **📝 超长推文解析**：支持 Twitter Article（2万字长文）与 NoteTweet 的全文完整提取与精美渲染。
* **💬 自然语言控制**：深度集成 `@filter.llm_tool`，支持群聊中通过大模型对话直接添加/移除订阅。

---

## 📸 效果预览

| 🌙 深色模式 | ☀️ 浅色模式 |
| :---: | :---: |
| ![Dark](preview_dark.png) | ![Light](preview_light.png) |

---

## 🚀 快速安装

### 1. 放置插件
将本插件目录完整放入 AstrBot 的 `addons/` 或指定的插件加载路径中。

### 2. 安装核心依赖
在 AstrBot 的运行环境中执行以下命令（已预装对应库的环境可跳过）：

```bash
pip install twikit==2.1.3 playwright jinja2
playwright install chromium

```

### 3. 安装 OCR 依赖（可选）

如果你需要使用 `text_extraction` 模式来提取并翻译图片中的文字，请安装：

```bash
pip install easyocr

```

### 4. 重启服务

重启 AstrBot，随后前往 WebUI 面板进行具体配置。

---

## ⚙️ 插件配置

请在 **AstrBot WebUI → 插件配置** 中填写以下核心参数：

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `twitter_auth_token` | `String` | - | Twitter Cookie 中的 `auth_token` |
| `twitter_ct0` | `String` | - | Twitter Cookie 中的 `ct0` |
| `text_translate_provider` | `String` | - | 用于文字翻译的 LLM Provider ID |
| `image_translate_provider` | `String` | - | 用于图片翻译的 LLM Provider ID（可与上方相同） |
| `image_translate_mode` | `Enum` | `multimodal` | `multimodal`（多模态视界）或 `text_extraction`（OCR后翻译） |
| `translation_language` | `String` | `中文` | 翻译的目标语言 |
| `poll_interval` | `Number` | `5` | 监控轮询间隔时间（单位：分钟） |

### 🔑 如何获取 Twitter Cookie？

1. 使用 Chrome 或 Edge 浏览器打开 [x.com](https://x.com) 并登录你的账号。
2. 按下 `F12` 或 `Ctrl+Shift+I` 打开开发者工具。
3. 切换到 **Application (应用)** 面板 → 左侧展开 **Cookies** → 点击 `https://x.com`。
4. 在右侧列表中找到 `auth_token` 与 `ct0`，复制它们对应的 **Value (值)** 填入配置。

---

## 🎮 使用指南

### ⌨️ 手动常规指令

| 指令语法 | 功能描述 |
| --- | --- |
| `/twitter add <username>` | 关注并开始监控该 Twitter 账号 |
| `/twitter remove <username>` | 取消关注该账号 |
| `/twitter list` | 列出当前会话已关注的所有用户 |
| `/twitter push <url>` | 手动抓取并推送指定的单条推文链接 |
| `/twitter monitor` | 切换当前聊天会话的自动推送开启/关闭状态 |

### 🗣️ AI 自然语言交互（需开启大模型对话）

无需死记硬背指令，你可以直接用大白话对机器人说：

* ❌ *"取消关注 apex"* → 自动触发 `twitter_remove`（支持模糊匹配）
* 📌 *"推送 https://x.com/xxx/status/123"* → 自动触发 `twitter_push`
* 📋 *"列出已关注的推特账号"* → 自动触发 `twitter_list`
* 🔄 *"开启自动推送"* / *"关闭自动推送"* → 自动触发 `twitter_monitor`

---

## 📂 项目结构

```text
astrbot_plugin_twitter_monitor/
├── main.py                 # 插件入口：指令分发、核心监控异步循环
├── twitter_client.py       # Twitter API 客户端封装与数据请求
├── metadata.yaml           # 插件元数据声明
├── _conf_schema.json       # WebUI 配置可视化 Schema
├── requirements.txt        # 依赖声明文件
└── templates/
    └── tweet_card.html     # MD3 卡片前端 Jinja2 渲染模板

```

监控的数据持久化保存在：`data/config/astrbot_plugin_twitter_monitor_data.json`

---

## ⚠️ 注意事项

> [!IMPORTANT]
> **QQ 官方机器人平台 (QQ Official)** 目前不支持 Node 转发节点消息，因此合并防刷屏功能无法在该平台生效。该功能仅在 **OneBot (如 aiocqhttp / NapCat)** 等实现下完美支持。

> [!NOTE]
> 翻译功能完全依赖 AstrBot 本身的 `llm_generate()` 接口。请务必确保你已经在 AstrBot 核心配置中连接了可用且未额度耗尽的 LLM Provider。

```

---

### 💡 核心优化点说明：
1. **视觉分栏优化**：表格加入了文本对齐标记（如 `:---:` 居中，`:---` 左对齐），避免文字在不同屏幕上错位。
2. **状态提示增强**：利用了 Markdown 的高级语法 `> [!IMPORTANT]` 和 `> [!NOTE]`，在支持的渲染器（如 GitHub）中会显示为带颜色高亮的精美警告框。
3. **可读性跃升**：各级标题前补充了高相关的 Emoji，将枯燥的纯文本列表转化为“特征图标+粗体字”的现代文档规范，让用户一分钟就能抓取到核心配置和使用方法。

```
