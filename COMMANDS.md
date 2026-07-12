# 电波推送 / DenpaPush 指令文档

## 插件简介

电波推送（DenpaPush）是一个 AstrBot 插件，用于监控 Twitter/X 推文并自动推送。支持 MD3 动态配色卡片、LLM 翻译、图片 OCR、深色模式、媒体合并发送等功能。

---

## 指令列表

### /twitter add - 关注用户

添加一个 Twitter 用户到监控列表，开始跟踪其推文并自动推送新内容。

**格式：**
```
/twitter add <username>
```

**参数：**
- `username` — Twitter 用户名（不含 @）

**示例：**
```
/twitter add ApexLiveComms
/twitter add Utsusemi_1024
```

**说明：**
- 用户名不区分大小写
- 重复关注会提示「本群已关注」
- 关注后会自动启动后台轮询，检查新推文

---

### /twitter remove - 取消关注

从监控列表中移除一个 Twitter 用户，停止跟踪其推文。

**格式：**
```
/twitter remove <username>
```

**参数：**
- `username` — Twitter 用户名（不含 @）

**示例：**
```
/twitter remove ApexLiveComms
```

**说明：**
- 如果当前群聊未关注该用户，会提示「本群未关注」
- 移除后如果没有任何群聊关注任何用户，会停止后台轮询

---

### /twitter list - 查看关注列表

列出当前群聊已关注的所有 Twitter 用户。

**格式：**
```
/twitter list
```

**示例输出：**
```
已关注用户:
  @ApexLiveComms
  @Utsusemi_1024
```

---

### /twitter push - 手动推送推文

手动推送一条指定的推文，获取内容、翻译、生成卡片并发送图片/视频。

**格式：**
```
/twitter push <url>
```

**参数：**
- `url` — 推文链接，格式为 `https://x.com/username/status/123456` 或 `https://twitter.com/username/status/123456`

**示例：**
```
/twitter push https://x.com/Utsusemi_1024/status/2067588056597885119
```

**说明：**
- 会获取推文内容、翻译成目标语言
- 生成 Material Design 3 动态配色卡片
- 自动发送图片、视频、GIF 等媒体文件
- 支持引用推文、转推、长文章等复杂推文

---

### /twitter monitor - 切换自动推送

开启或关闭当前会话的自动推送功能。

**格式：**
```
/twitter monitor
```

**说明：**
- 开启后，当关注的用户发布新推文时会自动推送到当前会话
- 关闭后，不再自动推送，但关注列表保留
- 每个会话独立控制

---

## 自然语言控制

插件集成了 AstrBot 的 LLM 工具系统，支持使用自然语言控制插件。AI 会自动识别用户意图并调用对应工具。

### 关注用户

**触发词：** 关注、订阅、跟踪

**示例：**
- `关注 ApexLiveComms`
- `订阅这几个推特账号：ApexLiveComms, Utsusemi_1024`
- `帮我跟踪 @MimikuWo`

### 取消关注

**触发词：** 取消关注、取关、删除订阅

**示例：**
- `取消关注 apex`（支持模糊匹配）
- `取关 ApexLiveComms`

### 推送推文

**触发词：** 推送、翻译、看看、读取、解析

**示例：**
- `推送 https://x.com/xxx/status/123456`
- `帮我看看这个推文 https://x.com/xxx/status/123456`
- `翻译这个推特 https://x.com/xxx/status/123456`

### 查看关注列表

**触发词：** 列出、查看、已关注

**示例：**
- `列出已关注账号`
- `我关注了哪些推特用户？`

### 开启/关闭自动推送

**触发词：** 开启、关闭、自动推送

**示例：**
- `开启自动推送`
- `关闭自动推送`

---

## 配置项

### 基础配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `twitter_auth_token` | Twitter Cookie 中的 `auth_token` | 无 |
| `twitter_ct0` | Twitter Cookie 中的 `ct0` | 无 |
| `poll_interval` | 轮询间隔（分钟） | 5 |
| `proxy` | 代理地址 | 无 |

### 翻译配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `text_translate_provider` | 文字翻译使用的 AI 提供商 | 无 |
| `image_translate_provider` | 图片翻译使用的 AI 提供商 | 无 |
| `image_translate_mode` | 图片翻译模式 | `multimodal` |
| `translation_language` | 翻译目标语言 | `中文` |

### Prompt 配置

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `text_translate_prompt` | 文字翻译 Prompt | `请将以下内容翻译成{lang}，只返回翻译结果:\n\n{prefix}{text}` |
| `image_translate_prompt` | 图片翻译 Prompt（多模态模式） | `理解图片内容并翻译成{lang}，自行组织格式使用户能简单直接理解。尽量简短，不要使文本量过大影响阅读。如果图片中没有文字输出'(无文字)'。` |
| `color_source` | 卡片取色来源 | `avatar`（默认） |

**Prompt 可用变量：**
- `{lang}` — 翻译目标语言
- `{text}` — 原文（仅文字翻译）
- `{prefix}` — 分段标记（仅文字翻译）

---

## 图片翻译模式

### multimodal（多模态直接翻译）

使用多模态 AI 模型直接理解图片内容并翻译。需要 AI 提供商支持多模态能力。

**优点：** 翻译质量高，能理解图片上下文
**缺点：** 需要多模态模型支持

### text_extraction（提取文字后翻译）

先使用 OCR 提取图片中的文字，再调用 AI 翻译提取的文字。

**优点：** 不需要多模态模型
**缺点：** 需要安装 easyocr，翻译质量可能较低

**安装 easyocr：**
```bash
pip install easyocr
```

---

## 获取 Twitter Cookie

1. 打开 `https://x.com` 并登录账号
2. 使用 cookie editor 插件（如 EditThisCookie）
3. 复制以下字段：
   - `auth_token`
   - `ct0`
4. 填写到插件配置中即可

---

## 媒体支持

### 图片
- 单图全宽展示
- 多图智能网格布局（1 / 2 / 3 / 4 图）
- 图片翻译（多模态或 OCR）

### 视频
- 自动下载并发送
- MP4 格式直接发送

### GIF
- Twitter MP4 GIF 自动转换为 GIF 格式
- 需要系统安装 ffmpeg：`apt install ffmpeg`

### 引用推文
- 引用推文内容会显示在卡片中
- 引用推文的图片也会发送

### 转推
- 转推内容会递归解析
- 显示原推文内容和媒体

---

## 常见问题

### Q: 为什么图片出现两份？
A: 已修复。原因是转推的 `retweeted_tweet` 被错误当作引用推文处理，导致图片重复。

### Q: 为什么 GIF 转换失败？
A: 需要系统安装 ffmpeg。运行 `apt install ffmpeg` 安装。

### Q: 为什么头像下载失败？
A: `pbs.twimg.com` CDN 可能从服务器访问不稳定。可以配置代理地址。

### Q: 为什么翻译超时？
A: 图片翻译默认每张图 60 秒超时，总共 120 秒超时。可以尝试更换更快的 AI 提供商。

### Q: 如何自定义翻译风格？
A: 在配置中修改 `text_translate_prompt` 和 `image_translate_prompt`，使用 `{lang}`、`{text}`、`{prefix}` 变量。

---

## 数据存储

插件数据保存在：
```
data/config/astrbot_plugin_denpa_push_data.json
```

包含：
- 已关注账号列表
- 最后推送推文 ID
- 自动推送会话列表

---

## 依赖说明

### Python 包

```
twikit==2.1.3           # Twitter API 封装
jinja2                   # 模板引擎
playwright               # 浏览器渲染
PyMCUlib>=1.0.0          # Material Color Utilities
material-color-utilities>=0.2.0  # 备用实现
```

### 系统依赖

```
ffmpeg                   # GIF 转换，通过 apt install ffmpeg
```

### 可选依赖

```
easyocr                  # OCR 文字识别模式
```
