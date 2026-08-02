# 空间壁纸（2.5D Parallax Photo）设计文档

日期：2026-08-02
状态：已批准（用户确认整体设计 OK）

## 1. 目标

将一张普通 2D 图片转换为具有空间感的动态壁纸：深度估计 → 图层分离 → AI 背景补全 → WebGL 实时视差渲染。用户移动鼠标时产生真实的视差效果，接近 iPhone 空间照片（Spatial Wallpaper）体验。

范围：仅桌面本地预览（127.0.0.1），鼠标/触摸板交互；架构预留移动端陀螺仪接口但不实现 HTTPS 部署。

## 2. 环境约束（已确认）

- 系统 Python 3.14.4，PyTorch 2.13.0 存在 cp314 wheel（已核实）
- NVIDIA RTX 3070 8GB，CUDA 可用；无 GPU 时自动回退 CPU
- 无 Node.js → 前端零构建，原生 ES Module + 本地化（vendored）Three.js
- 有外网，可下载 PyPI 包与模型权重
- 项目位于当前仓库 `spatial-wallpaper/` 独立子目录，不触碰现有 AstrBot 插件代码

## 3. 整体架构

```
spatial-wallpaper/
├── server/                        # FastAPI 后端
│   ├── app/
│   │   ├── main.py                # 入口：API 路由 + 静态托管 web/
│   │   ├── config.py              # 路径、分辨率上限、任务参数
│   │   ├── tasks.py               # 线程池任务队列（状态/进度/错误）
│   │   ├── api.py                 # POST /api/upload, GET /api/tasks/{id}, GET /api/assets/{id}/...
│   │   └── pipeline/
│   │       ├── depth.py           # DepthAnythingV2-S → depth map
│   │       ├── layers.py          # 深度平滑 + 前景掩码 + 羽化
│   │       ├── inpaint.py         # LaMa big-lama → 补全背景
│   │       └── assets.py          # 四资产输出 + 元数据
│   ├── models/                    # 权重目录（首次自动下载）
│   ├── scripts/download_models.py # 手动预下载
│   ├── tests/                     # pytest
│   └── requirements.txt
├── web/                           # 纯静态前端（零构建）
│   ├── index.html                 # 单页：上传区 → 处理进度 → 预览
│   ├── css/app.css
│   ├── vendor/three.min.js        # 本地化 Three.js
│   └── js/
│       ├── app.js                 # 页面状态机（upload/processing/preview）
│       └── parallax/
│           ├── scene.js           # 场景构建：网格 + 背景平面 + 过扫
│           ├── camera.js          # 鼠标视差相机（阻尼跟随 + idle 摇晃）
│           └── mesh.js            # 位移顶点着色器封装
└── README.md
```

### 数据流

上传图片 → 任务队列 → 预处理 → 深度估计 → 深度平滑 → 掩码提取 → LaMa 背景补全 → 四资产落盘 → 前端轮询任务状态 → 加载资产 → WebGL 渲染

## 4. 后端处理流水线

| 步骤 | 实现 | 要点 |
|---|---|---|
| 预处理 | Pillow + OpenCV | EXIF 方向修正，最长边限制 2048，校验类型/损坏/大小 |
| 深度估计 | DepthAnythingV2-Small（HF hub 权重） | 归一化 depth：近=1、远=0；GPU 秒级 |
| 深度平滑 | OpenCV 引导滤波 + 小半径高斯 | 抑制轮廓噪声与顶点位移伪影 |
| 掩码提取 | 深度阈值（前景 = depth > 分位数）→ 形态学闭运算 → 高斯羽化 12px | 生成前景羽化 mask |
| 背景补全 | LaMa big-lama（自研 torch 推理模块，约 120 行） | 掩码区域补全为无缝背景，权重首次自动下载 |
| 资产输出 | 四文件 + JSON 元数据 | `original.jpg`、`depth.png`(16bit)、`mask.png`(羽化)、`background.jpg` |

任务在后台线程运行（首次含模型加载约 30s，之后秒级），前端轮询 `/api/tasks/{id}` 获取状态/进度/错误。

## 5. 前端渲染（Three.js，方案 A：网格位移）

- 前景网格：原图铺于 256×256 细分平面，顶点着色器按 depth 纹理沿 Z 轴位移（近处 z 大），图片过扫 112% 防边缘露空
- 背景平面：LaMa 补全图置于 z=0 静态层，网格位移露出的遮挡区域自然透出补全内容 → 无空洞
- 相机：透视相机，鼠标归一化坐标经平滑阻尼（lerp）驱动相机 x/y 平移；无操作 2s 后进入缓慢 idle 摇晃（正弦）
- 视差：相机平移幅度 × 顶点深度自动产生"近大远小"真实视差
- 调参面板：视差强度 / 深度反转 / 过扫量 三个滑块（localStorage 持久化）

## 6. 错误处理

- 上传校验：类型（JPG/PNG）、大小上限、EXIF 损坏 → 400 明确错误信息
- 无 GPU → 自动 CPU 回退并提示处理变慢
- 模型权重缺失 → 提示运行 `scripts/download_models.py`，页面可重试
- 任务失败 → 状态记录错误详情，前端展示并允许重新上传

## 7. 测试策略

- pytest 快速测试（默认运行，不依赖 GPU/网络）：
  - 用合成渐变图 + 几何形状输入，验证 pipeline 各模块输出尺寸/范围
  - 掩码连通性与羽化边界验证
  - 资产输出完整性（四文件 + JSON 可解析）
- GPU/真实模型测试：`@pytest.mark.gpu` 标记，默认跳过
- 前端无自动化测试（无 Node），提供手动验证步骤清单于 README

## 8. 依赖清单

- 后端：torch, torchvision, opencv-python-headless, numpy, Pillow, fastapi, uvicorn, python-multipart, huggingface_hub
- 前端：Three.js（vendored，固定版本）
- 模型：depth-anything/Depth-Anything-V2-Small-hf（HF hub），big-lama（官方 release）

## 9. 非目标（YAGNI）

- 不做移动端 HTTPS/陀螺仪部署（仅预留接口思路）
- 不做视频输入、批量处理、在线分享
- 不做前端自动化测试框架
