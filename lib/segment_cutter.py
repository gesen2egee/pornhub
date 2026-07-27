# -*- coding: utf-8 -*-
"""長停頓切除、前後緩衝、字幕重對位與 Meta 更新模組。"""

import re
import os
from pathlib import Path


def parse_srt_time(ts_str: str) -> float:
    time_part, ms_part = ts_str.strip().split(',')
    h, m, s = map(int, time_part.split(':'))
    ms = int(ms_part)
    return h * 3600 + m * 60 + s + ms / 1000.0


def format_srt_time(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        s += 1
        ms -= 1000
        if s >= 60:
            m += 1
            s -= 60
            if m >= 60:
                h += 1
                m -= 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def load_srt_entries(srt_path: str | Path) -> list[dict]:
    """解析 SRT 檔案，回傳 list of dict: [{'start': float, 'end': float, 'text': str}]"""
    srt_path = Path(srt_path)
    if not srt_path.exists():
        return []

    with open(srt_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    blocks = re.split(r'\n\s*\n', content.strip())
    entries = []
    pattern = re.compile(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})')

    for b in blocks:
        lines = b.strip().split('\n')
        if len(lines) >= 2:
            time_line_idx = -1
            for idx, line in enumerate(lines):
                if '-->' in line:
                    time_line_idx = idx
                    break
            if time_line_idx != -1:
                m = pattern.search(lines[time_line_idx])
                if m:
                    start_sec = parse_srt_time(m.group(1))
                    end_sec = parse_srt_time(m.group(2))
                    text = "\n".join(lines[time_line_idx + 1:])
                    entries.append({'start': start_sec, 'end': end_sec, 'text': text})
    return entries


def calculate_net_dialogue_duration(entries: list[dict]) -> float:
    """計算非重疊的字幕對話純聲音淨總長"""
    if not entries:
        return 0.0

    sorted_inv = sorted([(e['start'], e['end']) for e in entries], key=lambda x: x[0])
    merged = []
    for s, e in sorted_inv:
        if not merged:
            merged.append((s, e))
        else:
            prev_s, prev_e = merged[-1]
            if s <= prev_e:
                merged[-1] = (prev_s, max(prev_e, e))
            else:
                merged.append((s, e))
    return sum(e - s for s, e in merged)


# 剪片預設：對白之間停頓 ≥ 此值就切開（中間空白丟掉）
DEFAULT_MAX_GAP = 1.5
# 所有流程預設不延伸；呼叫端仍可明確指定正數保留呼吸／語尾
DEFAULT_EDGE_PADDING = 0.0


def build_continuous_segments(
    entries: list[dict],
    max_gap: float = DEFAULT_MAX_GAP,
    max_dur: float = 99999.0,
    edge_padding: float = DEFAULT_EDGE_PADDING,
) -> list[tuple[float, float]]:
    """
    依字幕之間的停頓建立影片保留區段。

    剪片規則：
    - 相鄰對白停頓 **≥ max_gap**（預設 1.5s）→ 切開，中間長靜音剪掉
    - 停頓 **< max_gap** → 併成同一保留段
    - 每段對白前後各延伸 **edge_padding**（預設 0s，不延伸），
      只加在保留範圍，不參與「要不要切」的判斷

    預設 max_gap=1.5、edge_padding=0 時，停頓 ≥1.5s 就完整剪掉；
    明確指定 edge_padding 才會在切點兩側留下額外內容。
    """
    if not entries:
        return []

    if max_gap < 0:
        raise ValueError("max_gap 不得小於 0")
    if edge_padding < 0:
        raise ValueError("edge_padding 不得小於 0")

    intervals = []
    for entry in entries:
        start = max(0.0, float(entry["start"]))
        end = min(float(max_dur), float(entry["end"]))
        if end > start:
            intervals.append((start, end))
    intervals.sort(key=lambda interval: interval[0])
    if not intervals:
        return []

    segments: list[tuple[float, float]] = []
    first_start, first_end = intervals[0]
    current_start = max(0.0, first_start - edge_padding)
    current_end = min(float(max_dur), first_end + edge_padding)
    previous_dialogue_end = first_end
    for start, end in intervals[1:]:
        # 是否切段只看原始對白的間距；緩衝僅影響保留範圍。
        pause = start - previous_dialogue_end
        if pause < max_gap:
            # 短停頓（< 1.5s）：併段；僅在明確設定時延伸尾端
            current_end = max(current_end, min(float(max_dur), end + edge_padding))
        else:
            # 停頓 ≥ max_gap：切開；僅在明確設定時延伸兩端
            segments.append((current_start, current_end))
            current_start = max(0.0, start - edge_padding)
            current_end = min(float(max_dur), end + edge_padding)
        previous_dialogue_end = max(previous_dialogue_end, end)
    segments.append((current_start, current_end))
    return segments


def retime_subtitles(entries: list[dict], video_segments: list[tuple[float, float]]) -> list[dict]:
    """根據剪輯後的影片保留區段 (video_segments)，對字幕進行時間戳重新對位 (Retiming)"""
    new_entries = []

    seg_timeline = []
    current_new_time = 0.0
    for src_s, src_e in video_segments:
        dur = src_e - src_s
        seg_timeline.append({
            'src_start': src_s,
            'src_end': src_e,
            'new_start': current_new_time,
            'new_end': current_new_time + dur
        })
        current_new_time += dur

    for entry in entries:
        cap_s = entry['start']
        cap_e = entry['end']
        text = entry['text']

        for seg in seg_timeline:
            if cap_s >= seg['src_start'] and cap_e <= seg['src_end']:
                offset_s = cap_s - seg['src_start']
                offset_e = cap_e - seg['src_start']
                new_s = seg['new_start'] + offset_s
                new_e = seg['new_start'] + offset_e
                new_entries.append({'start': new_s, 'end': new_e, 'text': text})
                break
            elif cap_s < seg['src_end'] and cap_e > seg['src_start']:
                clamped_s = max(cap_s, seg['src_start'])
                clamped_e = min(cap_e, seg['src_end'])
                offset_s = clamped_s - seg['src_start']
                offset_e = clamped_e - seg['src_start']
                new_s = seg['new_start'] + offset_s
                new_e = seg['new_start'] + offset_e
                new_entries.append({'start': new_s, 'end': new_e, 'text': text})
                break

    return new_entries


def write_srt_file(entries: list[dict], out_srt_path: str | Path) -> None:
    out_srt_path = Path(out_srt_path)
    out_srt_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for idx, entry in enumerate(entries, 1):
        lines.append(str(idx))
        lines.append(f"{format_srt_time(entry['start'])} --> {format_srt_time(entry['end'])}")
        lines.append(entry['text'])
        lines.append("")

    with open(out_srt_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))


def retime_video_meta(meta_dict: dict, video_segments: list[tuple[float, float]]) -> dict:
    """同步調整影片 meta 中的時長紀錄與區段標記"""
    new_meta = dict(meta_dict)
    new_dur = sum(e - s for s, e in video_segments)
    new_meta['duration'] = new_dur
    new_meta['trimmed_segments'] = video_segments
    return new_meta
