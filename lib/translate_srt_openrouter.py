"""使用 OpenRouter 模型將字幕翻譯為繁體中文。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# 所有未指定層級的翻譯預設使用 Grok 4.3；失敗時由 Grok 4.5 作為後備。
DEFAULT_MODEL = "x-ai/grok-4.3"
# 預設每 60 條一批；設 0 或 TRANSLATE_BATCH_SIZE=0 則整份一次。
DEFAULT_BATCH_SIZE = 60
THREE_PHASE_SOFT_OVERAGE = 2
SPEAKER_LABEL_PATTERN = re.compile(r"^\s*\[S\d+\]\s*", re.IGNORECASE)
# 扁平格式：每行「id|正文」（無 JSON 殼、無前後文）
LINE_PATTERN = re.compile(r"^\s*(\d+)\s*[|｜]\s*(.*?)\s*$")

# 最近一次 translate_cues 的用量統計（時間 / tokens / cost），供測試腳本讀取。
LAST_TRANSLATE_STATS: dict[str, Any] = {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """先寫同目錄暫存檔再替換，避免中斷時留下半份 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _cue_fingerprint(cues: list[dict[str, Any]]) -> str:
    """只以會影響翻譯的 id、時間與文字辨識同一批來源字幕。"""
    source = [
        {
            "id": int(cue["id"]),
            "time": str(cue.get("time") or ""),
            "text": str(cue.get("text") or ""),
        }
        for cue in cues
    ]
    encoded = json.dumps(
        source,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

SYSTEM_PROMPT = (
    "你是字幕校正與翻譯專家。每行格式為 id|原文（ASR 聽寫）。"
    "先校正錯字/漏字/誤聽，再翻成自然口語的繁體中文。"
    "輸出的 text 必須嚴格全為繁體中文：人名、動名詞與所有專有稱呼也必須翻成繁體中文，"
    "禁止英文、日文、韓文、簡體或其他外語殘留。"
    "只輸出譯文；保留原意、語氣與成人內容，不要摘要或加說明。"
    "輸出格式必須與輸入相同：每行一條 id|譯文，id 不可改、不可漏、不可合併。"
    "不要 Markdown、不要 JSON、不要前後文說明。"
)

# 精選下載：劇情整理 + 只譯必要句。歌詞則改完整翻譯（給 LLM 的備註）。
SELECTIVE_SYSTEM_PROMPT = (
    "你是成人影片字幕編輯與繁中翻譯。你會一次收到整部影片的 ASR 字幕"
    "（每行 id|原文，可能是日文/英文/其他）。\n"
    "\n"
    "請嚴格依下列兩段輸出，不要 Markdown、不要 JSON、不要多餘說明：\n"
    "\n"
    "===PLOT===\n"
    "用繁體中文整理本片劇情與對白主線，150–300 字。\n"
    "寫清楚：人物關係、場景推進、關鍵衝突/指令/轉折、情緒走向。\n"
    "不要逐句翻譯。\n"
    "\n"
    "===TRANSLATIONS===\n"
    "只輸出「拿掉就會讓上下文看不懂或劇情斷裂」的字幕譯文。\n"
    "格式：每行一條 id|繁體中文譯文\n"
    "\n"
    "刪句原則（寧可多刪、少留）：\n"
    "1. 上下文看起來拿掉也沒問題的，就拿掉。\n"
    "2. 可省略：填充語、重複句、無意義感嘆/呻吟、客套寒暄、"
    "資訊已被前後句覆蓋的句子。\n"
    "3. 可濃縮：連續同義句只留一句代表；不必每句都譯。\n"
    "4. 必須保留：推進劇情、交代關係/動機、關鍵指令或轉折、"
    "沒有它會聽不懂的句子。\n"
    "5. id 必須是輸入原編號，不可改號、不可合併多 id 成一行。\n"
    "6. 允許跳號（1|… 下一行直接 6|…）。\n"
    "7. 譯文：自然口語繁體中文；人名、動名詞與所有專有稱呼也必須翻成繁體中文；"
    "禁止英文/日文/簡體殘留；保留成人語氣。\n"
    "8. 不要輸出未出現在輸入的 id。\n"
    "\n"
    "【重要備註】若你判斷這些字幕主要是歌詞（歌曲/MV/卡拉 OK 唱詞、"
    "反覆副歌等），不啟用精選省略：請在 ===TRANSLATIONS=== "
    "對輸入的每一條 id 都輸出完整譯文，不要跳號刪句。"
)


def _cue_duration_seconds(cue: dict[str, Any]) -> float:
    """讀取 SRT cue 時長；格式無效時安全略過。"""
    try:
        start, end = str(cue["time"]).split("-->", 1)
        def parse(value: str) -> float:
            hms, ms = value.strip().replace(".", ",").split(",", 1)
            hour, minute, second = (int(part) for part in hms.split(":"))
            return hour * 3600 + minute * 60 + second + int(ms) / 1000
        return max(0.0, parse(end) - parse(start))
    except (KeyError, TypeError, ValueError):
        return 0.0


def three_phase_budget(cues: list[dict[str, Any]]) -> dict[str, int | float]:
    """以 30 秒／平均句長計算 N，回傳三篇與可選結尾篇的句數上限。"""
    if not cues:
        raise ValueError("三段精選需要至少一條字幕")
    total = sum(_cue_duration_seconds(cue) for cue in cues)
    average = total / len(cues) if total > 0 else 0.0
    if average <= 0:
        raise ValueError("無法由字幕時間軸計算平均語音長度")
    n = max(2, math.ceil(30.0 / average))
    ending = math.ceil(0.5 * n)
    return {
        "n": n,
        "average_seconds": average,
        "plot": n - 1,
        "middle": 2 * n,
        "climax": 3 * n,
        "ending": ending,
        "total": 6 * n - 1 + ending,
    }


def strip_speaker_labels(
    cues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """移除 MOSS 匿名說話者標籤，只保留字幕正文。"""
    cleaned: list[dict[str, Any]] = []
    for cue in cues:
        item = dict(cue)
        item["text"] = SPEAKER_LABEL_PATTERN.sub(
            "",
            str(item.get("text", "")),
        ).strip()
        cleaned.append(item)
    return cleaned


def parse_srt(content: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    for block in content.replace("\r\n", "\n").strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        try:
            cue_id = int(lines[0].strip())
        except ValueError as exc:
            raise ValueError(f"SRT 字幕編號無效：{lines[0]!r}") from exc
        cues.append(
            {
                "id": cue_id,
                "time": lines[1].strip(),
                "text": "\n".join(lines[2:]).strip(),
            }
        )
    return cues


def format_srt(cues: list[dict[str, Any]]) -> str:
    """組成標準 SRT（\\n 換行、cue 之間一個空行）。跳過空白正文。"""
    blocks: list[str] = []
    for cue in cues:
        text = str(cue.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        text = "\n".join(
            part.strip() for part in text.split("\n") if part.strip()
        ).strip()
        if not text:
            continue
        cue_id = cue.get("id", len(blocks) + 1)
        time = str(cue.get("time") or "").strip()
        blocks.append(f"{cue_id}\n{time}\n{text}")
    # 重新編號，避免跳過空白後 id 不連續
    renumbered: list[str] = []
    for index, block in enumerate(blocks, 1):
        _id, time, *text_parts = block.split("\n")
        renumbered.append(
            f"{index}\n{time}\n" + "\n".join(text_parts)
        )
    return "\n\n".join(renumbered) + ("\n" if renumbered else "")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    return str(content)


def _format_flat_lines(items: list[dict[str, Any]]) -> str:
    """id|text 每行一條；正文內換行壓成空白。"""
    lines: list[str] = []
    for item in items:
        cue_id = int(item["id"])
        text = str(item.get("text") or "").replace("\r\n", "\n")
        text = " ".join(part.strip() for part in text.split("\n") if part.strip())
        lines.append(f"{cue_id}|{text}")
    return "\n".join(lines)


def _parse_flat_lines(content: str) -> dict[int, str]:
    """解析 id|譯文；相容舊 JSON array 回傳。"""
    content = content.strip()
    if content.startswith("```"):
        content = content.replace("```json", "", 1).replace("```", "").strip()

    result: dict[int, str] = {}
    # 優先扁平行
    for raw in content.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        match = LINE_PATTERN.match(line)
        if not match:
            continue
        cue_id = int(match.group(1))
        text = match.group(2).strip()
        if text:
            result[cue_id] = text
    if result:
        return result

    # 後備：舊 JSON [{"id":1,"text":"..."}]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("[")
        end = content.rfind("]")
        if start < 0 or end <= start:
            return {}
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if isinstance(parsed, dict) and isinstance(parsed.get("translations"), list):
        parsed = parsed["translations"]
    if not isinstance(parsed, list):
        return {}
    for item in parsed:
        if not isinstance(item, dict):
            continue
        if "id" not in item or "text" not in item:
            continue
        try:
            cue_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        text = str(item["text"]).strip()
        if text:
            result[cue_id] = text
    return result


def _usage_from_response(data: dict[str, Any]) -> dict[str, Any]:
    """從 OpenRouter chat.completions 回傳抽出 tokens / cost。"""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt + completion))
    cost = usage.get("cost")
    if cost is None:
        cost = data.get("cost")
    try:
        cost_f = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_f = None
    details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = details.get("reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = usage.get("native_tokens_reasoning")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "reasoning_tokens": int(reasoning_tokens or 0),
        "cost_usd": cost_f,
        "model": data.get("model"),
        "id": data.get("id"),
    }


def _translate_batch(
    cues: list[dict[str, Any]],
    api_key: str,
    model: str,
    session: requests.Session,
    stats_sink: list[dict[str, Any]] | None = None,
    reasoning_effort: str | None = None,
) -> dict[int, str]:
    items = [{"id": cue["id"], "text": cue["text"]} for cue in cues]
    max_tokens = int(os.getenv("TRANSLATE_MAX_TOKENS", "32000"))
    effort = (
        reasoning_effort
        or os.getenv("TRANSLATE_REASONING_EFFORT", "minimal")
    ).strip() or "minimal"
    body_template = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": ""},
        ],
        "reasoning": {"effort": effort, "exclude": True},
        "max_tokens": max_tokens,
        "stream": False,
        "usage": {"include": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/openrouter-ai/openrouter",
        "X-Title": "pornhub subtitle translator",
    }

    expected_ids = {int(cue["id"]) for cue in cues}
    cue_by_id = {int(cue["id"]): cue for cue in cues}
    result: dict[int, str] = {}
    last_error: Exception | None = None
    read_timeout = int(os.getenv("TRANSLATE_TIMEOUT_SECONDS", "600"))

    def _post(batch_items: list[dict[str, Any]], *, note: str = "") -> dict[int, str]:
        nonlocal last_error
        user_content = _format_flat_lines(batch_items)
        if note:
            user_content = f"{note}\n{user_content}"
        req_body = dict(body_template)
        req_body["messages"] = [
            body_template["messages"][0],
            {"role": "user", "content": user_content},
        ]
        parsed_batch: dict[int, str] = {}
        for attempt in range(3):
            try:
                t0 = time.perf_counter()
                response = session.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json=req_body,
                    timeout=(20, read_timeout),
                )
                wall = time.perf_counter() - t0
                if response.status_code == 429 or response.status_code >= 500:
                    raise RuntimeError(
                        f"OpenRouter 暫時錯誤 HTTP {response.status_code}"
                    )
                if response.status_code == 400:
                    err_text = (response.text or "")[:400]
                    if (
                        "reasoning" in err_text.casefold()
                        and "mandatory" in err_text.casefold()
                        and req_body.get("reasoning", {}).get("effort") == "none"
                    ):
                        req_body = dict(req_body)
                        req_body["reasoning"] = {
                            "effort": "minimal",
                            "exclude": True,
                        }
                        print(
                            "  [translate] reasoning=none 被拒，改 minimal 重試",
                            flush=True,
                        )
                        response = session.post(
                            OPENROUTER_URL,
                            headers=headers,
                            json=req_body,
                            timeout=(20, read_timeout),
                        )
                        wall = time.perf_counter() - t0
                    else:
                        response.raise_for_status()
                response.raise_for_status()
                data = response.json()
                usage_row = _usage_from_response(data)
                usage_row["wall_sec"] = round(wall, 3)
                usage_row["items"] = len(batch_items)
                usage_row["attempt"] = attempt + 1
                usage_row["note"] = note or "main"
                usage_row["user_chars"] = len(user_content)
                if stats_sink is not None:
                    stats_sink.append(usage_row)
                content = _content_to_text(data["choices"][0]["message"]["content"])
                raw = _parse_flat_lines(content)
                want = {int(item["id"]) for item in batch_items}
                for cue_id, text in raw.items():
                    if cue_id in want and text:
                        parsed_batch[cue_id] = text
                if parsed_batch:
                    return parsed_batch
                raise ValueError("API 回傳沒有可用的翻譯項目")
            except (
                requests.RequestException,
                KeyError,
                TypeError,
                ValueError,
                RuntimeError,
            ) as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
                else:
                    break
        return parsed_batch

    result.update(_post(items))

    for refill in range(2):
        missing = sorted(expected_ids - set(result))
        if not missing:
            break
        print(
            f"OpenRouter 翻譯補漏 round {refill + 1}：缺 {len(missing)} 條 "
            f"（例：{missing[:8]}{'…' if len(missing) > 8 else ''}）",
            flush=True,
        )
        refill_items = [
            {"id": cue_by_id[i]["id"], "text": cue_by_id[i]["text"]}
            for i in missing
        ]
        result.update(
            _post(
                refill_items,
                note=f"# 補翻以下 {len(refill_items)} 條，每行 id|譯文全部回傳",
            )
        )

    missing = sorted(expected_ids - set(result))
    if missing:
        raise RuntimeError(
            f"OpenRouter 翻譯失敗：API 遺漏字幕編號：{missing}；"
            f"last_error={last_error}"
        )
    return result


def translate_cues(
    cues: list[dict[str, Any]],
    api_key: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    *,
    checkpoint_path: Path | None = None,
    reasoning_effort: str | None = None,
) -> list[dict[str, Any]]:
    """翻譯字幕。預設每 batch_size 條一批；batch_size<=0 則整份一次。

    輸入輸出皆為扁平 id|text，無 JSON 殼、無前後文。
    用量寫入全域 LAST_TRANSLATE_STATS。
    """
    global LAST_TRANSLATE_STATS
    translated_cues: list[dict[str, Any]] = []
    speaker_prefixes: dict[int, str] = {}
    for source in cues:
        cue = dict(source)
        text = str(cue.get("text", ""))
        match = SPEAKER_LABEL_PATTERN.match(text)
        speaker_prefixes[int(cue["id"])] = match.group(0).strip() if match else ""
        cue["text"] = SPEAKER_LABEL_PATTERN.sub("", text).strip()
        translated_cues.append(cue)

    if not translated_cues:
        LAST_TRANSLATE_STATS = {
            "model": model,
            "cues": 0,
            "batches": [],
            "totals": {},
        }
        return translated_cues

    env_bs = os.getenv("TRANSLATE_BATCH_SIZE", "").strip()
    if env_bs:
        try:
            batch_size = int(env_bs)
        except ValueError:
            pass
    step = len(translated_cues) if batch_size <= 0 else max(1, batch_size)
    single_shot = step >= len(translated_cues)
    requested_effort = (
        reasoning_effort
        or os.getenv("TRANSLATE_REASONING_EFFORT", "minimal")
    ).strip() or "minimal"
    stats_sink: list[dict[str, Any]] = []
    t_all = time.perf_counter()
    fingerprint = _cue_fingerprint(cues)
    saved_translations: dict[int, str] = {}
    if checkpoint_path is not None and checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (
                checkpoint.get("schema") == "translation_v1"
                and checkpoint.get("source_fingerprint") == fingerprint
                and checkpoint.get("model") == model
                and checkpoint.get("reasoning_effort") == requested_effort
            ):
                saved_translations = {
                    int(cue_id): str(text)
                    for cue_id, text in dict(
                        checkpoint.get("translations") or {}
                    ).items()
                    if str(text).strip()
                }
        except (OSError, ValueError, TypeError):
            saved_translations = {}

    with requests.Session() as session:
        for start in range(0, len(translated_cues), step):
            batch = translated_cues[start : start + step]
            missing_batch = [
                cue
                for cue in batch
                if int(cue["id"]) not in saved_translations
            ]
            if missing_batch:
                batch_kwargs: dict[str, Any] = {"stats_sink": stats_sink}
                if reasoning_effort is not None:
                    batch_kwargs["reasoning_effort"] = reasoning_effort
                translations = _translate_batch(
                    missing_batch,
                    api_key,
                    model,
                    session,
                    **batch_kwargs,
                )
                saved_translations.update(translations)
                if checkpoint_path is not None:
                    _atomic_write_json(
                        checkpoint_path,
                        {
                            "schema": "translation_v1",
                            "source_fingerprint": fingerprint,
                            "model": model,
                            "reasoning_effort": requested_effort,
                            "complete": len(saved_translations)
                            >= len(translated_cues),
                            "completed_ids": sorted(saved_translations),
                            "translations": {
                                str(cue_id): saved_translations[cue_id]
                                for cue_id in sorted(saved_translations)
                            },
                        },
                    )
            else:
                translations = saved_translations
                print(
                    f"OpenRouter 翻譯 checkpoint 重用 "
                    f"{start + 1}-{start + len(batch)}/{len(cues)}",
                    flush=True,
                )
            for cue in batch:
                body = SPEAKER_LABEL_PATTERN.sub(
                    "",
                    saved_translations[int(cue["id"])],
                ).strip()
                prefix = speaker_prefixes[int(cue["id"])]
                cue["text"] = f"{prefix} {body}".strip()
            if single_shot:
                print(
                    f"OpenRouter 翻譯字幕 一次完成 {len(cues)} 條（model={model}）",
                    flush=True,
                )
            else:
                print(
                    f"OpenRouter 翻譯字幕 {start + 1}-{start + len(batch)}/{len(cues)}",
                    flush=True,
                )

    wall = time.perf_counter() - t_all
    prompt = sum(int(r.get("prompt_tokens") or 0) for r in stats_sink)
    completion = sum(int(r.get("completion_tokens") or 0) for r in stats_sink)
    total_tok = sum(int(r.get("total_tokens") or 0) for r in stats_sink)
    reasoning = sum(int(r.get("reasoning_tokens") or 0) for r in stats_sink)
    costs = [r.get("cost_usd") for r in stats_sink if r.get("cost_usd") is not None]
    cost_sum = sum(float(c) for c in costs) if costs else None
    LAST_TRANSLATE_STATS = {
        "model": model,
        "reasoning_effort": requested_effort,
        "format": "flat_id_pipe",
        "batch_size": 0 if single_shot else step,
        "cues": len(cues),
        "api_calls": len(stats_sink),
        "wall_sec": round(wall, 3),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "reasoning_tokens": reasoning,
        "total_tokens": total_tok or (prompt + completion),
        "cost_usd": round(cost_sum, 6) if cost_sum is not None else None,
        "batches": stats_sink,
    }
    return translated_cues


def parse_plot_and_translations(raw: str) -> tuple[str, dict[int, str]]:
    """解析精選翻譯回傳：===PLOT=== 與 ===TRANSLATIONS===。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.replace("```", "").strip()

    plot = ""
    trans_body = text
    plot_m = re.search(
        r"===PLOT===\s*(.*?)(?====TRANSLATIONS===|\Z)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    trans_m = re.search(
        r"===TRANSLATIONS===\s*(.*)\Z",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if plot_m:
        plot = plot_m.group(1).strip()
    if trans_m:
        trans_body = trans_m.group(1).strip()
    elif not plot_m:
        lines = text.splitlines()
        plot_lines: list[str] = []
        body_lines: list[str] = []
        hit_trans = False
        for line in lines:
            if LINE_PATTERN.match(line.strip()):
                hit_trans = True
                body_lines.append(line)
            elif not hit_trans:
                plot_lines.append(line)
            else:
                body_lines.append(line)
        plot = "\n".join(plot_lines).strip()
        trans_body = "\n".join(body_lines)

    translations = _parse_flat_lines(trans_body)
    cleaned: dict[int, str] = {}
    for cue_id, value in translations.items():
        body = SPEAKER_LABEL_PATTERN.sub("", value).strip()
        if body:
            cleaned[int(cue_id)] = body
    return plot, cleaned


def is_selective_full_translation(
    input_ids: set[int],
    translated_ids: set[int],
    *,
    full_ratio: float = 0.95,
) -> bool:
    """保留比例夠高時視為完整翻譯（含 LLM 判斷為歌詞的情況）。"""
    if not input_ids:
        return True
    if not translated_ids:
        return False
    return len(translated_ids & input_ids) >= max(
        1, int(len(input_ids) * full_ratio + 0.999)
    )


def translate_cues_selective(
    cues: list[dict[str, Any]],
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> dict[str, Any]:
    """一次送全字幕：劇情整理 + 精選翻譯（可跳號）。

    回傳：
      plot, translations{id:text}, kept_cues, dropped_ids,
      is_full（歌詞等完整翻譯）, raw, usage
    用量寫入 LAST_TRANSLATE_STATS。
    """
    global LAST_TRANSLATE_STATS

    prepared: list[dict[str, Any]] = []
    speaker_prefixes: dict[int, str] = {}
    by_id: dict[int, dict[str, Any]] = {}
    for source in cues:
        cue = dict(source)
        text = str(cue.get("text", ""))
        match = SPEAKER_LABEL_PATTERN.match(text)
        cid = int(cue["id"])
        speaker_prefixes[cid] = match.group(0).strip() if match else ""
        cue["text"] = SPEAKER_LABEL_PATTERN.sub("", text).strip()
        prepared.append(cue)
        by_id[cid] = cue

    if not prepared:
        LAST_TRANSLATE_STATS = {
            "model": model,
            "mode": "selective",
            "cues": 0,
            "kept": 0,
            "is_full": True,
        }
        return {
            "plot": "",
            "translations": {},
            "kept_cues": [],
            "dropped_ids": [],
            "is_full": True,
            "raw": "",
            "usage": {},
        }

    flat = _format_flat_lines(
        [{"id": c["id"], "text": c["text"]} for c in prepared]
    )
    user_content = (
        f"以下是整部影片共 {len(prepared)} 條字幕（id|原文）。"
        f"請先寫 ===PLOT=== 再寫 ===TRANSLATIONS===。\n"
        f"刪句原則：上下文拿掉也沒問題的就拿掉；"
        f"若判斷是歌詞則對每條 id 完整翻譯、不要省略。\n\n"
        f"{flat}"
    )

    effort = (
        os.getenv("TRANSLATE_REASONING_EFFORT", "minimal").strip() or "minimal"
    )
    model_l = model.casefold()
    if effort == "none" and ("grok-4.5" in model_l or "grok-4-5" in model_l):
        effort = "minimal"
    max_tokens = int(os.getenv("TRANSLATE_MAX_TOKENS", "32000"))
    read_timeout = int(os.getenv("TRANSLATE_TIMEOUT_SECONDS", "1200"))
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SELECTIVE_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "reasoning": {"effort": effort, "exclude": True},
        "max_tokens": max_tokens,
        "stream": False,
        "usage": {"include": True},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/openrouter-ai/openrouter",
        "X-Title": "pornhub selective download translate",
    }

    t0 = time.perf_counter()
    last_err: Exception | None = None
    data: dict[str, Any] | None = None
    for attempt in range(3):
        try:
            with requests.Session() as session:
                response = session.post(
                    OPENROUTER_URL,
                    headers=headers,
                    json=body,
                    timeout=(20, read_timeout),
                )
            if response.status_code == 400:
                err_text = (response.text or "")[:400]
                if (
                    "reasoning" in err_text.casefold()
                    and "mandatory" in err_text.casefold()
                    and body.get("reasoning", {}).get("effort") == "none"
                ):
                    body = dict(body)
                    body["reasoning"] = {
                        "effort": "minimal",
                        "exclude": True,
                    }
                    continue
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(
                    f"OpenRouter 暫時錯誤 HTTP {response.status_code}"
                )
            response.raise_for_status()
            data = response.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            if attempt < 2:
                time.sleep(2**attempt)
            else:
                raise RuntimeError(
                    f"精選翻譯失敗：{last_err}"
                ) from last_err
    assert data is not None
    wall = time.perf_counter() - t0
    usage = _usage_from_response(data)
    usage["wall_sec"] = round(wall, 3)
    raw = _content_to_text(data["choices"][0]["message"]["content"])
    plot, translations = parse_plot_and_translations(raw)
    if not translations:
        raise RuntimeError("精選翻譯回傳沒有可用的 id|譯文")

    input_ids = {int(c["id"]) for c in prepared}
    kept_ids = sorted(cid for cid in translations if cid in input_ids)
    dropped_ids = sorted(input_ids - set(kept_ids))
    is_full = is_selective_full_translation(input_ids, set(kept_ids))

    kept_cues: list[dict[str, Any]] = []
    for cid in kept_ids:
        src = by_id[cid]
        body_text = translations[cid]
        prefix = speaker_prefixes.get(cid, "")
        kept_cues.append(
            {
                "id": cid,
                "time": src.get("time"),
                "text": f"{prefix} {body_text}".strip(),
                "source_text": src.get("text"),
            }
        )

    print(
        f"OpenRouter 精選翻譯：保留 {len(kept_cues)}/{len(prepared)} 條"
        f"（{'完整/歌詞' if is_full else '精選省略'}；model={model}）",
        flush=True,
    )

    LAST_TRANSLATE_STATS = {
        "model": data.get("model") or model,
        "mode": "selective",
        "reasoning_effort": effort,
        "format": "plot_plus_flat_id_pipe",
        "cues": len(prepared),
        "kept": len(kept_cues),
        "dropped": len(dropped_ids),
        "is_full": is_full,
        "plot_chars": len(plot),
        "api_calls": 1,
        "wall_sec": round(wall, 3),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": usage.get("cost_usd"),
        "batches": [usage],
    }
    return {
        "plot": plot,
        "translations": {c["id"]: c["text"] for c in kept_cues},
        "kept_cues": kept_cues,
        "dropped_ids": dropped_ids,
        "is_full": is_full,
        "raw": raw,
        "usage": usage,
    }


def _parse_three_phase_selection(raw: str) -> tuple[str, dict[str, list[int]]]:
    """解析模型的三段選句；相容舊版可能帶有的設計稿。"""
    plot_match = re.search(
        r"===PLOT===\s*(.*?)(?====SELECTION===|\Z)",
        raw or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    selection_match = re.search(
        r"===SELECTION===\s*(.*)\Z",
        raw or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    phases = {"plot": [], "middle": [], "climax": [], "ending": []}
    for line in (selection_match.group(1) if selection_match else "").splitlines():
        name, separator, values = line.partition("|")
        key = name.strip().casefold()
        if separator and key in phases:
            phases[key] = [int(item) for item in re.findall(r"\d+", values)]
    return (plot_match.group(1).strip() if plot_match else ""), phases


def select_cues_three_phase(
    cues: list[dict[str, Any]],
    api_key: str,
    model: str = DEFAULT_MODEL,
    *,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """依 30 秒 N 預算選出連續可承接字幕，只要求固定選句格式。"""
    global LAST_TRANSLATE_STATS
    budget = three_phase_budget(cues)
    prepared = strip_speaker_labels(cues)
    by_id = {int(cue["id"]): cue for cue in prepared}
    flat = _format_flat_lines(prepared)
    system = (
        "你是精通蒙太奇與動態剪輯的成人影片精選剪輯師。完整閱讀所有 id|原文，"
        "先梳理劇情，再規劃並選出可承接的三篇：情節篇、中段精華篇、高潮篇。\n\n"
        "情節篇只用最少句數交代必要背景、人物關係；中段篇是自由剪取全片最有吸引力的精華，"
        "可混合任何能挑動觀看者慾望的內容；高潮篇以最激烈節奏的精華集中爆發並收尾；"
        "結尾篇只在需要時補上結尾前後呼應。\n\n"
        f"句數限制：情節篇最多 {budget['plot']} 句；中段篇最多 {budget['middle']} 句；"
        f"高潮篇最多 {budget['climax']} 句；可選結尾篇最多 {budget['ending']} 句；"
        f"情節＋中段合計最多 {budget['plot'] + budget['middle']} 句；全部最多 {budget['total']} 句。"
        "結尾篇不一定要，只有原片有交代結尾、前後呼應時才使用。\n\n"
        "若前段不足，優先以後段可承接內容補足，盡量不要超過上限。"
        "每個 id 只能選一次，各篇合併後 id 必須嚴格遞增。\n\n"
        "你精通蒙太奇與動態剪輯，必須刪除不必要支線與重複句，安排鏡頭節奏，"
        "只讓留下的整篇劇情發展是能自圓其說、前後呼應。整個剪輯重點不是完整呈現劇情，"
        "而是挑動觀看者慾望曲線，最後把慾望集中推到高潮。\n\n"
        "你會看到依原片順序排列的 id|原文，請依 id 與上下文判斷連續性。\n\n"
        "輸出格式完全固定，不要 Markdown、JSON 或額外說明：\n\n"
        "===SELECTION===\n"
        f"PLOT|最多 {budget['plot']} 個逗號分隔 id\n"
        f"MIDDLE|最多 {budget['middle']} 個逗號分隔 id\n"
        f"CLIMAX|最多 {budget['climax']} 個逗號分隔 id\n"
        f"ENDING|最多 {budget['ending']} 個逗號分隔 id；若不需要請留空"
    )
    effort = (
        reasoning_effort
        or os.getenv("THREE_PHASE_SELECTION_REASONING", "minimal")
    ).strip() or "minimal"
    if effort == "none" and "grok-4.5" in model.casefold():
        effort = "minimal"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"以下是完整影片共 {len(prepared)} 條 ASR 字幕：\n\n{flat}"},
        ],
        "reasoning": {"effort": effort, "exclude": True},
        "max_tokens": int(os.getenv("TRANSLATE_MAX_TOKENS", "32000")),
        "stream": False,
        "usage": {"include": True},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    started = time.perf_counter()
    response = requests.post(
        OPENROUTER_URL,
        headers=headers,
        json=body,
        timeout=(20, int(os.getenv("TRANSLATE_TIMEOUT_SECONDS", "1200"))),
    )
    response.raise_for_status()
    data = response.json()
    raw = _content_to_text(data["choices"][0]["message"]["content"])
    plot, phases = _parse_three_phase_selection(raw)
    expected = {name: int(budget[name]) for name in phases}
    actual = {name: len(ids) for name, ids in phases.items()}
    ids = [item for name in ("plot", "middle", "climax", "ending") for item in phases[name]]
    cumulative_middle = actual["plot"] + actual["middle"]
    if (
        not all(actual[name] for name in ("plot", "middle", "climax"))
        or actual["plot"] > expected["plot"] + THREE_PHASE_SOFT_OVERAGE
        or actual["middle"] > expected["middle"] + THREE_PHASE_SOFT_OVERAGE
        or actual["climax"] > expected["climax"] + THREE_PHASE_SOFT_OVERAGE
        or cumulative_middle > expected["plot"] + expected["middle"] + THREE_PHASE_SOFT_OVERAGE
        or actual["ending"] > expected["ending"] + THREE_PHASE_SOFT_OVERAGE
        or len(ids) > int(budget["total"]) + THREE_PHASE_SOFT_OVERAGE
    ):
        raise RuntimeError(
            f"三段精選超出預算或有空篇：上限 {expected}，實際 {actual}"
        )
    if ids != sorted(ids) or len(ids) != len(set(ids)) or any(item not in by_id for item in ids):
        raise RuntimeError("三段精選 id 不合法、重複或非嚴格遞增")
    usage = _usage_from_response(data)
    usage["wall_sec"] = round(time.perf_counter() - started, 3)
    LAST_TRANSLATE_STATS = {
        "model": data.get("model") or model,
        "mode": "three_phase_30s",
        "reasoning_effort": effort,
        "n": budget["n"],
        "phase_counts": actual,
        "kept": len(ids),
        "api_calls": 1,
        "usage": usage,
    }
    return {
        "plot": "",
        "phases": phases,
        "kept_cues": [by_id[item] for item in ids],
        "raw": raw,
        "budget": budget,
        "usage": usage,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_srt", type=Path)
    parser.add_argument("output_srt", type=Path)
    args = parser.parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise SystemExit("找不到 OPENROUTER_API_KEY 環境變數。")
    cues = parse_srt(args.input_srt.read_text(encoding="utf-8-sig"))
    translated = translate_cues(cues, api_key)
    args.output_srt.parent.mkdir(parents=True, exist_ok=True)
    args.output_srt.write_text(format_srt(translated), encoding="utf-8-sig")


if __name__ == "__main__":
    main()
