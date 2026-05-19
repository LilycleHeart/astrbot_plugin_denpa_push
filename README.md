# 电波推送 — AstrBot Plugin

监控指定 Twitter/X 账号的最新推文，自动生成Material design 3 动态配色卡片（PNG），支持翻译、图片 OCR。

## 功能

- **推文监控**：后台轮询，新推文自动推送
- **卡片渲染**：本地 Playwright 生成MD3样式动态取色卡片 PNG 
- **深色/浅色模式**：18:00–06:00 自动切换深色模式
- **多语言翻译**：借助 AstrBot LLM 能力翻译推文正文、长文 Article、NoteTweet、图片文字
- **图片自适应排版**：单张全宽、多张自适应网格（1/2/3/4 图响应式布局）
- **图片推送**：Group forward (Node) 合并发送，不刷屏
- **视频/GIF**：直接发送视频消息
- **引用推文**：引用推文以左侧竖线样式在卡片中展示，翻译时一并处理
- **长文 Article & NoteTweet**：支持 Twitter Article（2 万字长文）和 NoteTweet（长推文）全文提取与渲染
- **自然语言控制**：集成 `@filter.llm_tool`，支持通过自然语言添加/移除订阅

## 预览

| 深色模式 | 浅色模式 |
|---------|---------|
| ![Dark](preview_dark.png) | ![Light](preview_light.png) |

## 安装

1. 将本插件目录放入 AstrBot 的 `addons/` 或插件加载路径
2. 安装依赖（已预装在 AstrBot 环境中的可跳过）：

```bash
pip install twikit==2.1.3 playwright jinja2
playwright install chromium
```

3. 安装 easyocr（可选，text_extraction 模式需要）：

```bash
pip install easyocr
```

4. 重启 AstrBot，在 WebUI 插件配置中填入以下内容

## 配置

在 AstrBot WebUI → 插件配置中填写：

| 配置项 | 说明 |
|--------|------|
| `twitter_auth_token` | Twitter Cookie 中的 `auth_token` |
| `twitter_ct0` | Twitter Cookie 中的 `ct0` |
| `text_translate_provider` | 文字翻译用的 LLM Provider ID |
| `image_translate_provider` | 图片翻译用的 LLM Provider ID（可与上方相同） |
| `image_translate_mode` | `multimodal`（多模态）或 `text_extraction`（OCR 后翻译） |
| `translation_language` | 目标语言，默认 `中文` |
| `poll_interval` | 监控轮询间隔（分钟），默认 5 |

### 获取 Cookie

1. 在 Chrome 打开 `x.com` 并登录
2. F12 → Application → Cookies → x.com
3. 复制 `auth_token` 和 `ct0` 的值

## 指令

### 手动指令

| 指令 | 说明 |
|------|------|
| `/twitter add <username>` | 关注并监控用户 |
| `/twitter remove <username>` | 取消关注 |
| `/twitter list` | 列出已关注用户 |
| `/twitter push <url>` | 手动推送单条推文 |
| `/twitter monitor` | 切换本会话自动推送 |

### 自然语言（需 AI 对话启用）

- "关注 ApexLiveComms" → 调用 `twitter_add`
- "取消关注 apex" → 调用 `twitter_remove`（模糊匹配）
- "推送 https://x.com/xxx/status/123" → 调用 `twitter_push`
- "列出已关注的推特账号" → 调用 `twitter_list`
- "开启自动推送" / "关闭自动推送" → 调用 `twitter_monitor`


## 数据文件

监控数据保存在 `data/config/astrbot_plugin_twitter_monitor_data.json`，包括：
- 已关注用户列表及最后推文 ID
- 已开启自动推送的会话列表

## 项目结构

```
astrbot_plugin_twitter_monitor/
├── main.py                 # 插件入口，指令处理，监控循环
├── twitter_client.py       # Twitter API 封装
├── templates/
│   └── tweet_card.html     # MD3 卡片 Jinja2 模板
├── metadata.yaml           # 插件元数据
├── _conf_schema.json       # WebUI 配置 schema
└── requirements.txt        # 依赖声明
```

## 注意

- **QQ Official 平台**不支持 Node 转发消息，仅 OneBot (aiocqhttp) 支持
- 翻译使用 AstrBot 的 `llm_generate()`，需配置可用的 LLM Provider
