# Screen OCR Cleaner

> 录屏 OCR + 数据清洗流水线：从手机/电脑录屏中提取列表型结构化数据，输出 TS/JSON + 可审计的清洗报告。

[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

## 功能

- 从滚动录屏中提取列表型数据（基金代码、股票代码、商品编号等）
- 7 步流水线：抽帧 → 帧去重 → OCR → 正则提取 → 交叉验证 → 去噪去重 → 输出
- 交叉验证去噪：利用"真实数据项在连续多帧出现"的滚动特征过滤 OCR 误识别
- 断点续跑：OCR 结果自动缓存，中断后可从上次进度继续
- 可审计清洗报告：每步统计 + 过滤原因分布 + 被过滤值样例
- 多格式输出：TypeScript / JSON

## 流水线

```
录屏文件
  │  ① ffmpeg 抽帧（按 fps 均匀采样）
  ▼
帧序列
  │  ② 帧去重（感知哈希，跳过几乎相同的帧）
  ▼
去重后的帧序列
  │  ③ OCR（可选 ROI 裁剪，EasyOCR 中英文）
  ▼
帧 → 文本行映射
  │  ④ 正则提取（按目标模式提取候选值）
  ▼
帧 → 候选值映射
  │  ⑤ 交叉验证（频率 + 连续性 + 模式黑名单）
  ▼
候选值 → 验证标签
  │  ⑥ 去噪去重（去重 + 排序）
  ▼
最终结构化值列表
  │  ⑦ 输出（TS/JSON）+ 清洗报告（Markdown）
```

### 交叉验证原理

录屏滚动的本质特征：**一个真实数据项会在连续多帧中出现**（随滚动逐渐进入/离开视野），而 OCR 误识别通常只在单帧孤立出现。

三重过滤：

| 过滤层 | 规则 | 说明 |
|---|---|---|
| 模式黑名单 | 匹配 `false_positive_patterns` | 全相同数字、末尾多个 0 等明显误识别 |
| 频率 | 检测帧数 ≥ `min_detections` | 单帧孤立出现的值大概率是误识别 |
| 连续性 | 至少一对检测帧间隔 ≤ `max_gap` | 平滑滚动特征，过滤分散的偶发误识别 |

## 快速开始

### 1. 安装依赖

```bash
pip install -r scripts/requirements.txt
```

依赖：`easyocr`、`opencv-python`、`numpy`、`imageio-ffmpeg`、`PyYAML`

### 2. 配置

复制并修改配置文件：

```bash
cp configs/ant-fund-list.yaml configs/my-config.yaml
```

关键配置项：

```yaml
video: "path/to/your/video.mp4"      # 录屏文件路径
output_dir: "./output"                # 输出目录

frame:
  fps: 2                              # 每秒抽帧数
  diff_threshold: 0.012               # 帧差异阈值

ocr:
  languages: ["ch_sim", "en"]         # OCR 语言
  gpu: false
  roi:                                # 可选：裁剪区域（像素）
    top: 350
    bottom: 200
    left: 0
    right: 320

extract:
  patterns:                           # 正则提取目标
    - name: fund_code
      regex: "(?<!\\d)\\d{6}(?!\\d)"

validate:
  min_detections: 2                   # 至少出现帧数
  max_gap: 3                          # 连续帧最大间隔
  false_positive_patterns:            # 误识别黑名单
    - "^(\\d)\\1{5}$"
    - "0{4}$"

output:
  format: ["ts", "json"]
  ts_var_name: "FUND_CODES"
```

### 3. 运行

```bash
python3 scripts/screen_ocr_pipeline.py --config configs/my-config.yaml
```

### 4. 断点续跑

OCR 结果自动缓存到 `output/ocr_cache.json`，中断后重跑会自动跳过已处理帧。如需跳过 OCR 直接用已有缓存跑后续步骤：

```bash
python3 scripts/screen_ocr_pipeline.py --config configs/my-config.yaml --skip-ocr
```

## 输出结构

```
output/
├── frames/                # 抽取的帧（去重后保留）
├── ocr_cache.json         # OCR 原始结果（断点续跑）
├── extracted.json         # 正则提取的候选值（含帧索引）
├── validated.json         # 交叉验证结果（含过滤原因）
├── result.ts              # 最终 TS 输出
├── result.json            # 最终 JSON 输出
└── cleaning_report.md     # 清洗报告
```

### 清洗报告包含

- 流水线各步骤统计
- OCR 统计（帧数、文本行/帧）
- 交叉验证过滤明细（原因分布、检测次数分布、被过滤值样例）
- 最终结果（值数量、前 30 / 后 10）
- 完整配置参数（可复现）

## 测试用例

首个测试用例：蚂蚁金选基金名单录屏（`configs/ant-fund-list.yaml`）。

| 指标 | 值 |
|---|---|
| 源视频 | 蚂蚁金选基金名单 2026.7.13 |
| 抽帧 | 628 帧（fps=2） |
| 去重后 | 562 帧 |
| OCR | 566 帧 |
| 候选值 | 1334 个 |
| 验证通过 | 1195 个基金代码 |
| 被过滤 | 139 个（137 频次不足、1 模式黑名单、1 非连续） |

招商银行（CMB）配置模板见 [`configs/cmb-fund-list.yaml`](configs/cmb-fund-list.yaml)，需根据实际录屏布局调整 ROI 和正则模式。

## 调优指南

| 现象 | 调整方向 |
|---|---|
| OCR 结果为空 | 去掉 `roi` 先全图跑，确认文本位置后再裁剪 |
| 提取值太少 | 降低 `min_detections` 至 1；调高 `fps` |
| 误识别太多 | 在清洗报告中查看可疑值，补充 `false_positive_patterns` |
| OCR 速度慢 | 启用 ROI 裁剪；降低 `fps` 至 1-2 |
| 帧去重太激进 | 降低 `diff_threshold`（如 0.008） |

详细实现原理见 [`references/pipeline-details.md`](references/pipeline-details.md)，清洗报告 Schema 见 [`references/report-schema.md`](references/report-schema.md)。

## 项目结构

```
screen-ocr-cleaner/
├── SKILL.md                          # Skill 元信息与使用说明
├── README.md                         # 本文件
├── scripts/
│   ├── screen_ocr_pipeline.py        # 统一流水线脚本
│   └── requirements.txt              # Python 依赖
├── configs/
│   ├── ant-fund-list.yaml            # 蚂蚁金选测试用例
│   └── cmb-fund-list.yaml            # 招商银行配置模板
└── references/
    ├── pipeline-details.md           # 流水线实现细节
    └── report-schema.md              # 清洗报告 Schema
```

## License

MIT
