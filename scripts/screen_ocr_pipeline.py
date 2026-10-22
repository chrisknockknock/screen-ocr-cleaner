#!/usr/bin/env python3
"""录屏 OCR + 数据清洗统一流水线。

步骤：ffmpeg 抽帧 → 帧去重 → OCR → 正则提取 → 交叉验证 → 去噪去重 → 输出 TS/JSON + 清洗报告。

用法：
    python3 screen_ocr_pipeline.py --config configs/ant-fund-list.yaml
    python3 screen_ocr_pipeline.py --config configs/cmb-fund-list.yaml --resume

支持断点续跑：OCR 结果缓存到 ocr_cache.json，重跑时自动跳过已处理的帧。
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import cv2
import numpy as np
import yaml

try:
    import easyocr
except ImportError:
    print("ERROR: easyocr not installed. Run: pip install easyocr", file=sys.stderr)
    sys.exit(1)

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    FFMPEG = "ffmpeg"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Defaults
    cfg.setdefault("output_dir", "./output")
    cfg.setdefault("frame", {})
    cfg["frame"].setdefault("fps", 2)
    cfg["frame"].setdefault("diff_threshold", 0.012)
    cfg.setdefault("ocr", {})
    cfg["ocr"].setdefault("languages", ["ch_sim", "en"])
    cfg["ocr"].setdefault("gpu", False)
    cfg["ocr"].setdefault("roi", None)
    cfg.setdefault("extract", {})
    cfg["extract"].setdefault("patterns", [])
    cfg.setdefault("validate", {})
    cfg["validate"].setdefault("min_detections", 2)
    cfg["validate"].setdefault("max_gap", 3)
    cfg["validate"].setdefault("false_positive_patterns", [])
    cfg.setdefault("output", {})
    cfg["output"].setdefault("format", ["json"])
    cfg["output"].setdefault("ts_var_name", "DATA")
    cfg["output"].setdefault("sort", True)
    cfg["output"].setdefault("codes_per_line", 8)
    cfg.setdefault("report", {})
    cfg["report"].setdefault("title", "数据清洗报告")
    return cfg


def resolve_video_path(cfg: dict) -> str:
    video = cfg.get("video", "")
    if not video:
        print("ERROR: config missing 'video' field", file=sys.stderr)
        sys.exit(1)
    if not os.path.isabs(video):
        # Resolve relative to config file location if possible, else cwd
        video = os.path.abspath(video)
    if not os.path.exists(video):
        print(f"ERROR: video not found: {video}", file=sys.stderr)
        sys.exit(1)
    return video


# ---------------------------------------------------------------------------
# Step 1: ffmpeg frame extraction
# ---------------------------------------------------------------------------

def step_extract_frames(video: str, out_dir: str, fps: float) -> list[str]:
    """用 ffmpeg 按 fps 均匀抽帧，返回帧文件路径列表（按文件名排序）。"""
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)

    # 清空已有帧（避免新旧帧混合）
    for f in os.listdir(frames_dir):
        if f.endswith(".png"):
            os.remove(os.path.join(frames_dir, f))

    out_pattern = os.path.join(frames_dir, "frame_%05d.png")
    cmd = [
        FFMPEG, "-y",
        "-i", video,
        "-vf", f"fps={fps}",
        "-q:v", "2",
        out_pattern,
    ]
    print(f"[1/7] 抽帧: fps={fps}", flush=True)
    print(f"       cmd: {' '.join(cmd)}", flush=True)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"ERROR: ffmpeg failed:\n{res.stderr[-2000:]}", file=sys.stderr)
        sys.exit(1)

    frames = sorted(f for f in os.listdir(frames_dir) if f.endswith(".png"))
    print(f"       抽到 {len(frames)} 帧 → {frames_dir}", flush=True)
    return [os.path.join(frames_dir, f) for f in frames]


# ---------------------------------------------------------------------------
# Step 2: Frame dedup (perceptual hash)
# ---------------------------------------------------------------------------

def _make_hash(img: np.ndarray) -> np.ndarray:
    return cv2.resize(img, (48, 96), interpolation=cv2.INTER_AREA)


def _hash_diff(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean() / 255.0)


def step_dedup_frames(
    frame_paths: list[str],
    diff_threshold: float,
    roi: Optional[dict],
) -> list:
    """基于感知哈希的帧去重，返回保留的帧路径列表。"""
    print(f"[2/7] 帧去重: diff_threshold={diff_threshold}", flush=True)
    kept = []
    last_hash = None
    skipped = 0
    for fp in frame_paths:
        img = cv2.imread(fp)
        if img is None:
            continue
        h_full, w_full = img.shape[:2]
        if roi:
            crop = img[
                roi.get("top", 0): h_full - roi.get("bottom", 0),
                roi.get("left", 0): w_full - roi.get("right", 0),
            ]
        else:
            crop = img
        if crop.size == 0:
            crop = img
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        hashv = _make_hash(gray)
        if last_hash is not None:
            d = _hash_diff(hashv, last_hash)
            if d < diff_threshold:
                skipped += 1
                continue
        last_hash = hashv
        kept.append(fp)
    print(f"       保留 {len(kept)} 帧，跳过 {skipped} 相似帧", flush=True)
    return kept


# ---------------------------------------------------------------------------
# Step 3: OCR (with resume cache)
# ---------------------------------------------------------------------------

def step_ocr(
    frame_paths: list[str],
    languages: list[str],
    gpu: bool,
    roi: Optional[dict],
    cache_path: str,
) -> dict:
    """对帧列表执行 OCR，返回 {frame_name: {"text_lines": [...], "codes": [...]}}。

    支持断点续跑：已缓存的帧直接跳过。
    """
    print(f"[3/7] OCR: languages={languages}, gpu={gpu}", flush=True)

    # Load cache
    results = {}
    cached_names = set()
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            results = data.get("results", {})
            cached_names = set(results.keys())
        print(f"       已有缓存 {len(cached_names)} 帧，将跳过", flush=True)

    to_process = [fp for fp in frame_paths if os.path.basename(fp) not in cached_names]
    if to_process:
        print(f"       初始化 EasyOCR reader...", flush=True)
        reader = easyocr.Reader(languages, gpu=gpu, verbose=False)
        print(f"       待处理 {len(to_process)} 帧", flush=True)

    t0 = time.time()
    ocr_count = 0
    for i, fp in enumerate(to_process):
        fname = os.path.basename(fp)
        img = cv2.imread(fp)
        if img is None:
            continue
        h_full, w_full = img.shape[:2]
        if roi:
            crop = img[
                roi.get("top", 0): h_full - roi.get("bottom", 0),
                roi.get("left", 0): w_full - roi.get("right", 0),
            ]
        else:
            crop = img
        if crop.size == 0:
            crop = img

        try:
            ocr_result = reader.readtext(crop, detail=0, paragraph=False)
        except Exception as e:
            print(f"       [{i+1}/{len(to_process)}] {fname}: OCR error {e}", flush=True)
            continue

        text_lines = [str(t).strip() for t in ocr_result if str(t).strip()]
        results[fname] = {"text_lines": text_lines}
        ocr_count += 1

        if ocr_count % 10 == 0 or ocr_count <= 3:
            elapsed = time.time() - t0
            print(
                f"       [{i+1}/{len(to_process)}] {fname}: "
                f"ocr#{ocr_count} lines={len(text_lines)} t={elapsed:.1f}s",
                flush=True,
            )

        # Periodic cache save
        if ocr_count % 20 == 0:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"results": results}, f, ensure_ascii=False)

    # Final cache save
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"results": results}, f, ensure_ascii=False)

    print(f"       OCR 完成: 新增 {ocr_count} 帧，累计 {len(results)} 帧", flush=True)
    return results


# ---------------------------------------------------------------------------
# Step 4: Regex extraction
# ---------------------------------------------------------------------------

def _frame_idx(name: str) -> int:
    m = re.search(r"frame_(\d+)\.png", name)
    return int(m.group(1)) if m else -1


def step_extract_patterns(
    ocr_results: dict[str, dict],
    patterns: list[dict],
) -> list[dict]:
    """对每帧的文本行应用正则，提取候选值。

    返回列表，每项: {"frame": name, "frame_idx": int, "pattern": name, "value": str, "source_line": str}
    """
    print(f"[4/7] 正则提取: {len(patterns)} 个模式", flush=True)
    compiled = []
    for p in patterns:
        compiled.append((p["name"], re.compile(p["regex"])))

    extracted = []
    for fname, payload in ocr_results.items():
        idx = _frame_idx(fname)
        lines = payload.get("text_lines", [])
        for line in lines:
            for pname, rx in compiled:
                for m in rx.finditer(line):
                    extracted.append({
                        "frame": fname,
                        "frame_idx": idx,
                        "pattern": pname,
                        "value": m.group(0),
                        "source_line": line,
                    })

    # Group by value for stats
    by_value = defaultdict(list)
    for e in extracted:
        by_value[e["value"]].append(e["frame_idx"])
    print(f"       提取到 {len(extracted)} 条候选，去重后 {len(by_value)} 个唯一值", flush=True)
    return extracted


# ---------------------------------------------------------------------------
# Step 5: Cross-validation
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    value: str
    frames: list[int]
    detection_count: int
    frame_span: int
    passed: bool
    reason: str = ""


def step_cross_validate(
    extracted: list[dict],
    min_detections: int,
    max_gap: int,
    false_positive_patterns: list[str],
) -> list[ValidationResult]:
    """跨帧交叉验证：频率 + 连续性 + 模式黑名单。"""
    print(
        f"[5/7] 交叉验证: min_detections={min_detections}, "
        f"max_gap={max_gap}, fp_patterns={len(false_positive_patterns)}",
        flush=True,
    )

    # Group by value
    by_value: dict[str, list[int]] = defaultdict(list)
    for e in extracted:
        by_value[e["value"]].append(e["frame_idx"])
    for v in by_value:
        by_value[v] = sorted(set(by_value[v]))

    fp_compiled = [re.compile(p) for p in false_positive_patterns]

    results = []
    for value, frames in by_value.items():
        detection_count = len(frames)
        frame_span = max(frames) - min(frames) if frames else 0

        # Pattern filter
        if any(rx.search(value) for rx in fp_compiled):
            results.append(ValidationResult(
                value=value, frames=frames,
                detection_count=detection_count, frame_span=frame_span,
                passed=False, reason="pattern_blocked",
            ))
            continue

        # Frequency filter
        if detection_count < min_detections:
            results.append(ValidationResult(
                value=value, frames=frames,
                detection_count=detection_count, frame_span=frame_span,
                passed=False, reason=f"frequency<{min_detections}",
            ))
            continue

        # Consecutiveness filter
        has_consecutive = any(
            frames[i] - frames[i - 1] <= max_gap for i in range(1, len(frames))
        )
        if not has_consecutive:
            results.append(ValidationResult(
                value=value, frames=frames,
                detection_count=detection_count, frame_span=frame_span,
                passed=False, reason="not_consecutive",
            ))
            continue

        results.append(ValidationResult(
            value=value, frames=frames,
            detection_count=detection_count, frame_span=frame_span,
            passed=True, reason="ok",
        ))

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    reason_counter = Counter(r.reason for r in failed)
    print(f"       通过 {len(passed)}，过滤 {len(failed)}", flush=True)
    for reason, cnt in reason_counter.most_common():
        print(f"         - {reason}: {cnt}", flush=True)
    return results


# ---------------------------------------------------------------------------
# Step 6: Clean & dedup
# ---------------------------------------------------------------------------

def step_clean_dedupe(
    validation_results: list[ValidationResult],
    sort: bool = True,
) -> list[str]:
    """对通过验证的值去重、排序。"""
    print("[6/7] 去噪去重", flush=True)
    values = [r.value for r in validation_results if r.passed]
    unique = sorted(set(values)) if sort else list(dict.fromkeys(values))
    print(f"       最终值数量: {len(unique)}", flush=True)
    return unique


# ---------------------------------------------------------------------------
# Step 7: Export (TS/JSON) + report
# ---------------------------------------------------------------------------

def step_export(
    final_values: list[str],
    validation_results: list[ValidationResult],
    extracted: list[dict],
    ocr_results: dict[str, dict],
    cfg: dict,
    out_dir: str,
    video: str,
) -> None:
    """导出 TS/JSON 并生成清洗报告。"""
    print("[7/7] 输出 + 清洗报告", flush=True)
    os.makedirs(out_dir, exist_ok=True)
    fmt = cfg["output"]["format"]
    var_name = cfg["output"]["ts_var_name"]
    codes_per_line = cfg["output"]["codes_per_line"]

    if "json" in fmt:
        json_path = os.path.join(out_dir, "result.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(final_values, f, ensure_ascii=False, indent=2)
        print(f"       → {json_path} ({len(final_values)} values)", flush=True)

    if "ts" in fmt:
        ts_path = os.path.join(out_dir, "result.ts")
        header = (
            f"// 从录屏 OCR 提取（screen-ocr-cleaner）\n"
            f"// 源视频: {os.path.basename(video)}\n"
            f"// 共 {len(final_values)} 个值（已去重{'、排序' if cfg['output']['sort'] else ''}）\n"
            f"export const {var_name}: string[] = [\n"
        )
        lines = []
        for i in range(0, len(final_values), codes_per_line):
            chunk = final_values[i:i + codes_per_line]
            trailing = "," if i + codes_per_line < len(final_values) else ""
            lines.append("  " + ", ".join(f'"{c}"' for c in chunk) + trailing)
        body = "\n".join(lines)
        footer = "\n];\n"
        with open(ts_path, "w", encoding="utf-8") as f:
            f.write(header + body + footer)
        print(f"       → {ts_path} ({len(final_values)} values)", flush=True)

    # Also dump intermediate results for auditability
    extracted_path = os.path.join(out_dir, "extracted.json")
    with open(extracted_path, "w", encoding="utf-8") as f:
        json.dump(extracted, f, ensure_ascii=False, indent=2)

    validated_path = os.path.join(out_dir, "validated.json")
    with open(validated_path, "w", encoding="utf-8") as f:
        json.dump([
            {
                "value": r.value,
                "frames": r.frames,
                "detection_count": r.detection_count,
                "frame_span": r.frame_span,
                "passed": r.passed,
                "reason": r.reason,
            }
            for r in validation_results
        ], f, ensure_ascii=False, indent=2)

    # Generate cleaning report
    report_path = os.path.join(out_dir, "cleaning_report.md")
    _generate_report(
        report_path=report_path,
        cfg=cfg,
        video=video,
        ocr_results=ocr_results,
        extracted=extracted,
        validation_results=validation_results,
        final_values=final_values,
    )
    print(f"       → {report_path}", flush=True)


def _generate_report(
    report_path: str,
    cfg: dict,
    video: str,
    ocr_results: dict[str, dict],
    extracted: list[dict],
    validation_results: list[ValidationResult],
    final_values: list[str],
) -> None:
    """生成 Markdown 清洗报告。"""
    passed = [r for r in validation_results if r.passed]
    failed = [r for r in validation_results if not r.passed]
    reason_counter = Counter(r.reason for r in failed)

    # Detection count histogram
    det_hist = Counter(r.detection_count for r in validation_results)

    # Sample of failed values by reason
    failed_samples = {}
    for r in failed:
        failed_samples.setdefault(r.reason, []).append(r.value)
    for k in failed_samples:
        failed_samples[k] = failed_samples[k][:10]

    lines = []
    lines.append(f"# {cfg['report'].get('title', '数据清洗报告')}\n")
    lines.append(f"- **源视频**: `{os.path.basename(video)}`")
    lines.append(f"- **生成时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- **输出格式**: {', '.join(cfg['output']['format'])}")
    lines.append("")

    lines.append("## 1. 流水线概览\n")
    lines.append("| 步骤 | 指标 | 值 |")
    lines.append("|---|---|---|")
    lines.append(f"| ① 抽帧 | fps | {cfg['frame']['fps']} |")
    lines.append(f"| ② 帧去重 | diff_threshold | {cfg['frame']['diff_threshold']} |")
    lines.append(f"| ③ OCR | 帧数 | {len(ocr_results)} |")
    lines.append(f"| ④ 正则提取 | 候选数 | {len(extracted)} |")
    lines.append(f"| ④ 正则提取 | 唯一候选值 | {len(validation_results)} |")
    lines.append(f"| ⑤ 交叉验证 | 通过 | {len(passed)} |")
    lines.append(f"| ⑤ 交叉验证 | 过滤 | {len(failed)} |")
    lines.append(f"| ⑥ 去重后 | 最终值 | {len(final_values)} |")
    lines.append("")

    lines.append("## 2. OCR 统计\n")
    lines_per_frame = [len(v.get("text_lines", [])) for v in ocr_results.values()]
    if lines_per_frame:
        lines.append(f"- 总帧数: {len(ocr_results)}")
        lines.append(f"- 平均文本行/帧: {sum(lines_per_frame)/len(lines_per_frame):.2f}")
        lines.append(f"- 最大文本行/帧: {max(lines_per_frame)}")
    lines.append("")

    lines.append("## 3. 交叉验证过滤明细\n")
    lines.append(f"- **通过验证**: {len(passed)} 个值")
    lines.append(f"- **被过滤**: {len(failed)} 个值")
    lines.append("")
    lines.append("### 过滤原因分布\n")
    lines.append("| 原因 | 数量 |")
    lines.append("|---|---|")
    for reason, cnt in reason_counter.most_common():
        lines.append(f"| {reason} | {cnt} |")
    lines.append("")

    lines.append("### 检测次数分布\n")
    lines.append("| 检测次数 | 值数量 |")
    lines.append("|---|---|")
    for k in sorted(det_hist.keys()):
        lines.append(f"| {k} | {det_hist[k]} |")
    lines.append("")

    lines.append("### 被过滤值样例（每个原因最多 10 个）\n")
    for reason, samples in failed_samples.items():
        lines.append(f"**{reason}** ({reason_counter[reason]} 个):")
        lines.append("")
        lines.append("```")
        lines.append(", ".join(samples))
        lines.append("```")
        lines.append("")

    lines.append("## 4. 最终结果\n")
    lines.append(f"- **最终值数量**: {len(final_values)}")
    lines.append(f"- **排序**: {'是' if cfg['output']['sort'] else '否'}")
    lines.append("")
    if final_values:
        lines.append("### 前 30 个值\n")
        lines.append("```")
        lines.append(", ".join(final_values[:30]))
        lines.append("```")
        lines.append("")
        if len(final_values) > 30:
            lines.append(f"### 后 10 个值\n")
            lines.append("```")
            lines.append(", ".join(final_values[-10:]))
            lines.append("```")
            lines.append("")

    lines.append("## 5. 配置参数\n")
    lines.append("```yaml")
    lines.append(yaml.dump(cfg, allow_unicode=True, default_flow_style=False).strip())
    lines.append("```")
    lines.append("")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="录屏 OCR + 数据清洗流水线")
    parser.add_argument("--config", required=True, help="YAML 配置文件路径")
    parser.add_argument("--resume", action="store_true", help="从 OCR 缓存断点续跑")
    parser.add_argument("--skip-ocr", action="store_true", help="跳过 OCR，直接用已有缓存")
    args = parser.parse_args()

    cfg = load_config(args.config)
    video = resolve_video_path(cfg)
    out_dir = os.path.abspath(cfg["output_dir"])
    os.makedirs(out_dir, exist_ok=True)

    cache_path = os.path.join(out_dir, "ocr_cache.json")

    # Step 1: extract frames
    frame_paths = step_extract_frames(video, out_dir, cfg["frame"]["fps"])

    # Step 2: dedup frames
    kept_frames = step_dedup_frames(frame_paths, cfg["frame"]["diff_threshold"], cfg["ocr"]["roi"])

    # Step 3: OCR (with resume)
    if args.skip_ocr and os.path.exists(cache_path):
        print("[3/7] OCR: 跳过（使用已有缓存）", flush=True)
        with open(cache_path, "r", encoding="utf-8") as f:
            ocr_results = json.load(f)["results"]
    else:
        ocr_results = step_ocr(
            kept_frames,
            cfg["ocr"]["languages"],
            cfg["ocr"]["gpu"],
            cfg["ocr"]["roi"],
            cache_path,
        )

    # Step 4: regex extraction
    extracted = step_extract_patterns(ocr_results, cfg["extract"]["patterns"])

    # Step 5: cross-validation
    validation_results = step_cross_validate(
        extracted,
        cfg["validate"]["min_detections"],
        cfg["validate"]["max_gap"],
        cfg["validate"]["false_positive_patterns"],
    )

    # Step 6: clean & dedup
    final_values = step_clean_dedupe(validation_results, cfg["output"]["sort"])

    # Step 7: export + report
    step_export(final_values, validation_results, extracted, ocr_results, cfg, out_dir, video)

    print("\n=== 流水线完成 ===", flush=True)
    print(f"输出目录: {out_dir}", flush=True)
    print(f"最终值数量: {len(final_values)}", flush=True)


if __name__ == "__main__":
    main()
