---
name: "screen-ocr-cleaner"
description: "录屏 OCR + 数据清洗流水线。输入录屏文件，输出结构化数据(TS/JSON) + 清洗报告。核心步骤：ffmpeg 抽帧 → 帧去重 → OCR → 正则提取 → 交叉验证 → 去噪去重 → 输出。适用于从手机/电脑录屏中提取列表型结构化数据（如基金名单、股票列表、商品清单等）。"
---

# Screen OCR Cleaner · 录屏 OCR + 数据清洗流水线

将录屏文件中的列表型数据（如基金代码、股票代码、商品编号等）通过 OCR 提取为结构化数据，并输出可审计的清洗报告。

## 适用场景

- 从手机录屏中提取滚动列表数据（基金名单、股票自选、商品清单、通讯录等）
- OCR 结果存在噪声（误识别、重复、漏识别），需要多帧交叉验证去噪
- 需要可复现、可审计的数据提取流程（每一步都有中间产物和统计）

## 核心流水线（7 步）

```
录屏文件
  │
  ▼ ① ffmpeg 抽帧（按 fps 均匀采样）
帧序列
  │
  ▼ ② 帧去重（感知哈希，跳过几乎相同的帧）
去重后的帧序列
  │
  ▼ ③ OCR（可选 ROI 裁剪，提取文本行）
帧 → 文本行映射
  │
  ▼ ④ 正则提取（按目标模式提取候选值）
帧 → 候选值映射
  │
  ▼ ⑤ 交叉验证（跨帧连续性/频率验证，过滤孤立噪声）
候选值 → 验证标签
  │
  ▼ ⑥ 去噪去重（模式过滤 + 频率 + 连续性，合并去重）
最终结构化值列表
  │
  ▼ ⑦ 输出（TS/JSON）+ 清洗报告（Markdown）
```

## 快速开始

### 1. 准备配置文件

复制 `configs/ant-fund-list.yaml` 并修改为你的场景。关键配置项：

```yaml
video: "path/to/your/video.mp4"
output_dir: "./output"
frame:
  fps: 2                    # 每秒抽帧数
  diff_threshold: 0.012     # 帧差异阈值（越低越敏感，OCR 更多帧）
ocr:
  languages: ["ch_sim", "en"]
  roi:                      # 可选：裁剪区域（像素），跳过无关区域
    top: 350
    bottom: 200
    left: 0
    right: 320
extract:
  patterns:                 # 正则提取目标值
    - name: fund_code
      regex: "(?<!\\d)\\d{6}(?!\\d)"
validate:
  min_detections: 2         # 至少出现在多少帧
  max_gap: 3                # 连续帧最大间隔
  false_positive_patterns:  # 误识别模式黑名单
    - "^(\\d)\\1{5}$"       # 全相同数字
    - "0{4}$"               # 末尾 4 个 0
output:
  format: ["ts", "json"]    # 输出格式
  ts_var_name: "FUND_CODES" # TS 导出变量名
```

### 2. 运行流水线

```bash
cd /Users/chrisliang/AI/Skills/screen-ocr-cleaner
python3 scripts/screen_ocr_pipeline.py --config configs/ant-fund-list.yaml
```

### 3. 查看输出

```
output/
├── frames/                 # 抽取的帧（去重后保留的帧）
├── ocr_cache.json          # OCR 原始结果（可断点续跑）
├── extracted.json          # 正则提取后的候选值（含帧索引）
├── validated.json          # 交叉验证后的结果（含过滤原因）
├── result.ts               # 最终 TS 输出
├── result.json             # 最终 JSON 输出
└── cleaning_report.md      # 清洗报告（统计 + 过滤明细）
```

## 关键设计原则

### 帧去重（②）
- 使用 OpenCV 灰度图 + 缩放至 48×96 计算感知哈希
- 相邻帧差异 < `diff_threshold` 时跳过，避免对静止画面重复 OCR
- 录屏滚动场景下，每帧约 6 个可见项，滚动时每帧约 1-2 个新项

### 交叉验证（⑤）
录屏滚动的本质特征：**一个真实数据项会在连续多帧中出现**（随滚动逐渐进入/离开视野），而 OCR 误识别通常只在单帧孤立出现。

验证策略：
1. **频率过滤**：候选值必须出现在 ≥ `min_detections` 帧
2. **连续性过滤**：至少有一对检测帧的间隔 ≤ `max_gap`（平滑滚动特征）
3. **模式过滤**：匹配 `false_positive_patterns` 的候选值直接丢弃

### 去噪去重（⑥）
- 对通过验证的候选值按值去重
- 可选：按值排序
- 记录每个被过滤值的过滤原因，用于清洗报告

### 断点续跑
- OCR 结果缓存到 `ocr_cache.json`，中断后重跑会从上次进度继续
- 已处理的帧不会重复 OCR

## 测试用例

首个测试用例：EasyStrategy 项目的蚂蚁金选基金名单录屏（`/Users/chrisliang/AI/EasyStrategy/data/蚂蚁金选基金名单2026.7.13.MP4`）。

```bash
python3 scripts/screen_ocr_pipeline.py --config configs/ant-fund-list.yaml
```

该配置已根据该录屏的实际布局调优（ROI 裁剪左侧代码列、跳过顶部导航和底部 Tab 栏），可直接复现提取 200+ 基金代码。

> 招商银行（CMB）录屏配置模板见 `configs/cmb-fund-list.yaml`，需根据实际录屏布局调整 ROI 和正则模式。

## 边界与约束

- ✅ 支持：列表型滚动录屏、6 位基金/股票代码、编号类数据
- ✅ 支持：中文+英文 OCR、ROI 裁剪、断点续跑、多格式输出
- ✅ 支持：可审计清洗报告（每步统计 + 过滤原因）
- ❌ 不支持：非列表型录屏（如视频字幕、动态图表）—— 需要不同的 OCR 策略
- ❌ 不支持：手写文字识别（OCR 引擎针对印刷体优化）
- ❌ 不支持：实时录屏处理（仅支持已录制的视频文件）

## 故障排查

| 现象 | 可能原因 | 解决 |
|---|---|---|
| OCR 结果为空 | ROI 裁剪区域不对 | 去掉 `roi` 配置先跑全图，确认文本位置后再裁剪 |
| 提取值太少 | `min_detections` 过高或 `diff_threshold` 过低 | 降低 `min_detections` 至 1，提高 `diff_threshold` |
| 误识别太多 | `false_positive_patterns` 不足 | 在清洗报告中查看被保留的可疑值，补充黑名单模式 |
| OCR 速度慢 | 全图 OCR 或 fps 过高 | 启用 ROI 裁剪、降低 fps 至 1-2 |

详细的流水线实现细节见 `references/pipeline-details.md`，清洗报告 schema 见 `references/report-schema.md`。
