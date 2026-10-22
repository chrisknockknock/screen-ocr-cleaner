# 清洗报告 Schema

`cleaning_report.md` 是流水线的最终审计产物，记录从原始录屏到结构化数据的完整处理过程。

## 报告结构

### 1. 元信息

| 字段 | 说明 |
|---|---|
| 源视频 | 录屏文件名 |
| 生成时间 | 报告生成的时间戳 |
| 输出格式 | ts / json |

### 2. 流水线概览

7 步流水线的关键指标：

| 步骤 | 指标 | 说明 |
|---|---|---|
| ① 抽帧 | fps | 每秒抽帧数 |
| ② 帧去重 | diff_threshold | 帧差异阈值 |
| ③ OCR | 帧数 | 实际 OCR 的帧数 |
| ④ 正则提取 | 候选数 | 所有正则匹配结果数 |
| ④ 正则提取 | 唯一候选值 | 去重后的候选值数 |
| ⑤ 交叉验证 | 通过 | 通过验证的值数 |
| ⑤ 交叉验证 | 过滤 | 被过滤的值数 |
| ⑥ 去重后 | 最终值 | 最终输出的值数 |

### 3. OCR 统计

- 总帧数
- 平均文本行/帧
- 最大文本行/帧

**用途**: 判断 OCR 是否正常工作。如果平均文本行/帧很低（< 1），可能 ROI 裁剪有误或 OCR 语言不对。

### 4. 交叉验证过滤明细

#### 过滤原因分布

| 原因 | 说明 |
|---|---|
| `pattern_blocked` | 匹配误识别黑名单正则 |
| `frequency<N` | 检测次数低于阈值 |
| `not_consecutive` | 没有连续帧检测（非平滑滚动特征） |
| `ok` | 通过验证 |

#### 检测次数分布

值在多少帧中被检测到的直方图。正常录屏滚动场景下，检测次数应集中在 2-4 次。

#### 被过滤值样例

每个过滤原因最多展示 10 个被过滤的值，用于人工核查过滤是否合理。

### 5. 最终结果

- 最终值数量
- 是否排序
- 前 30 个值
- 后 10 个值（如果总数 > 30）

### 6. 配置参数

完整的 YAML 配置，用于复现。

## 中间产物 Schema

### extracted.json

```json
[
  {
    "frame": "frame_00001.png",
    "frame_idx": 1,
    "pattern": "fund_code",
    "value": "000368",
    "source_line": "000368  易方达蓝筹精选"
  }
]
```

### validated.json

```json
[
  {
    "value": "000368",
    "frames": [1, 2, 3, 4],
    "detection_count": 4,
    "frame_span": 3,
    "passed": true,
    "reason": "ok"
  },
  {
    "value": "111111",
    "frames": [5],
    "detection_count": 1,
    "frame_span": 0,
    "passed": false,
    "reason": "pattern_blocked"
  }
]
```

**字段说明**:
- `frames`: 该值被检测到的所有帧序号（已排序去重）
- `detection_count`: 检测次数（= len(frames)）
- `frame_span`: 首次与末次检测的帧间隔
- `passed`: 是否通过验证
- `reason`: 过滤原因（`ok` / `pattern_blocked` / `frequency<N` / `not_consecutive`）

### ocr_cache.json

```json
{
  "results": {
    "frame_00001.png": {
      "text_lines": ["000368  易方达蓝筹精选", "000457  易方达中小盘"]
    }
  }
}
```
