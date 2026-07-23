"""使用 OpenRouter Grok 4.5 將字幕翻譯為台灣繁體中文。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "x-ai/grok-4.5"
DEFAULT_BATCH_SIZE = 60


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
    return "\n\n".join(
        f"{cue['id']}\n{cue['time']}\n{cue['text']}" for cue in cues
    ) + ("\n" if cues else "")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    return str(content)


def _parse_json_array(content: str) -> list[dict[str, Any]]:
    content = content.strip()
    if content.startswith("```"):
        content = content.replace("```json", "", 1).replace("```", "").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("[")
        end = content.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("API 沒有回傳可解析的 JSON array")
        parsed = json.loads(content[start : end + 1])

    if isinstance(parsed, dict) and isinstance(parsed.get("translations"), list):
        parsed = parsed["translations"]
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("API 回傳格式不是 JSON array")
    return parsed


def _translate_batch(
    cues: list[dict[str, Any]],
    api_key: str,
    model: str,
    session: requests.Session,
) -> dict[int, str]:
    minimal_input = [{"id": cue["id"], "text": cue["text"]} for cue in cues]
    user_payload = json.dumps(minimal_input, ensure_ascii=False, separators=(",", ":"))
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是字幕校正與翻譯專家。每個 text 可能是 Whisper 聽寫結果，"
                    "請先依上下文校正錯字、漏字、同音誤聽、專有名詞與標點，"
                    "再翻譯成自然、準確、口語流暢的台灣繁體中文。"
                    "翻譯結果只保留繁體中文，不要輸出校正原文。"
                    "保留原意、語氣、成人內容與說話者情緒，不要摘要、解釋或審查；"
                    "原文不確定時不要自行捏造內容。"
                    "只能回傳 JSON array，每個元素只能有 id 與 text，id 必須完全保留。"
                    "不要使用 Markdown、不要加前後說明。"
                ),
            },
            {
                "role": "user",
                "content": user_payload,
            },
        ],
        "reasoning": {"effort": "minimal", "exclude": True},
        "max_tokens": 8000,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/openrouter-ai/openrouter",
        "X-Title": "pornhub subtitle translator",
    }

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.post(
                OPENROUTER_URL,
                headers=headers,
                json=body,
                timeout=(20, 300),
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(f"OpenRouter 暫時錯誤 HTTP {response.status_code}")
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            translated = _parse_json_array(_content_to_text(content))
            expected_ids = {int(cue["id"]) for cue in cues}
            result: dict[int, str] = {}
            for item in translated:
                if "id" not in item or "text" not in item:
                    raise ValueError("API 回傳項目缺少 id 或 text")
                cue_id = int(item["id"])
                text = str(item["text"]).strip()
                if cue_id not in expected_ids or not text:
                    raise ValueError("API 回傳了無效字幕編號或空白翻譯")
                if cue_id in result:
                    raise ValueError("API 重複回傳字幕編號")
                result[cue_id] = text
            if set(result) != expected_ids:
                missing = sorted(expected_ids - set(result))
                raise ValueError(f"API 遺漏字幕編號：{missing}")
            return result
        except (requests.RequestException, KeyError, TypeError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2**attempt)
            else:
                break
    raise RuntimeError(f"OpenRouter 翻譯失敗：{last_error}") from last_error


def translate_cues(
    cues: list[dict[str, Any]],
    api_key: str,
    model: str = DEFAULT_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[dict[str, Any]]:
    translated_cues = [dict(cue) for cue in cues]
    with requests.Session() as session:
        for start in range(0, len(cues), batch_size):
            batch = cues[start : start + batch_size]
            translations = _translate_batch(batch, api_key, model, session)
            for cue in translated_cues[start : start + len(batch)]:
                cue["text"] = translations[int(cue["id"])]
            print(
                f"OpenRouter 翻譯字幕 {start + 1}-{start + len(batch)}/{len(cues)}",
                flush=True,
            )
    return translated_cues


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
