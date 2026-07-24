"""零額外依賴的本機 Web UI 伺服器。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_paths import PREVIEW_VIDEOS_DIR, VIDEOS_DIR
from web_app.services import (
    TASKS,
    decode_media_id,
    list_sites,
    resolve_remote,
    scan_library,
    search_sites,
    srt_to_vtt,
)


STATIC_DIR = Path(__file__).with_name("static")
ALLOWED_MEDIA_ROOTS = (Path(PREVIEW_VIDEOS_DIR), Path(VIDEOS_DIR))
MIME_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".vtt": "text/vtt; charset=utf-8",
}


class MuseHandler(BaseHTTPRequestHandler):
    server_version = "MuseLocal/1.0"

    def log_message(self, format: str, *args: object) -> None:
        print(f"[UI] {self.address_string()} - {format % args}")

    def _send_json(
        self,
        payload: object,
        status: int = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; "
            "media-src 'self' https: blob:; connect-src 'self'; "
            "style-src 'self'; script-src 'self';",
        )

    def _read_json(self) -> dict:
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 1_000_000)
            raw = self.rfile.read(length)
            value = json.loads(raw or b"{}")
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("請求格式錯誤") from exc

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/health":
                self._send_json({"ready": True, "name": "Muse", "version": 1})
                return
            if path == "/api/sites":
                self._send_json({"sites": list_sites()})
                return
            if path == "/api/library":
                items = scan_library()
                self._send_json({"items": items, "count": len(items)})
                return
            if path == "/api/tasks":
                self._send_json({"tasks": TASKS.all()})
                return
            if path == "/api/search":
                query = parse_qs(parsed.query)
                keyword = (query.get("q") or [""])[0]
                selected = [
                    item
                    for raw in query.get("sites", [])
                    for item in raw.split(",")
                    if item
                ]
                payload = search_sites(
                    keyword,
                    selected,
                    pages=int((query.get("pages") or ["1"])[0]),
                    limit=int((query.get("limit") or ["48"])[0]),
                )
                self._send_json(payload)
                return
            if path.startswith("/media/"):
                self._send_media(path.removeprefix("/media/"))
                return
            if path.startswith("/subtitles/") and path.endswith(".vtt"):
                media_id = path.removeprefix("/subtitles/").removesuffix(".vtt")
                self._send_subtitle(media_id)
                return
            self._send_static(path)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except FileNotFoundError:
            self._send_json({"error": "找不到指定內容"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json(
                {"error": f"服務暫時無法完成：{type(exc).__name__}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/queue":
                task = TASKS.start_capture(
                    list(payload.get("items") or []),
                    str(payload.get("quality") or "preview"),
                )
                self._send_json({"task": task}, HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/resolve":
                item = resolve_remote(str(payload.get("url") or ""))
                self._send_json({"item": item})
                return
            if parsed.path == "/api/download/start":
                task = TASKS.start_download()
                self._send_json({"task": task}, HTTPStatus.ACCEPTED)
                return
            self._send_json({"error": "未知的操作"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json(
                {"error": f"服務暫時無法完成：{type(exc).__name__}"},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _send_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError as exc:
            raise FileNotFoundError from exc
        if not target.is_file():
            if "." not in Path(relative).name:
                target = STATIC_DIR / "index.html"
            else:
                raise FileNotFoundError
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            MIME_TYPES.get(target.suffix, "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control",
            "no-cache" if target.suffix == ".html" else "public, max-age=300",
        )
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_media(self, media_id: str) -> None:
        path = decode_media_id(media_id, ALLOWED_MEDIA_ROOTS)
        if not path.is_file():
            raise FileNotFoundError
        total = path.stat().st_size
        start, end = 0, total - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if match:
            if match.group(1):
                start = int(match.group(1))
            if match.group(2):
                end = min(int(match.group(2)), total - 1)
            if not match.group(1) and match.group(2):
                start = max(0, total - int(match.group(2)))
            if start > end or start >= total:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{total}")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        content_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self._security_headers()
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 512, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_subtitle(self, media_id: str) -> None:
        media = decode_media_id(media_id, ALLOWED_MEDIA_ROOTS)
        subtitle = media.with_suffix(".srt")
        if not subtitle.is_file():
            raise FileNotFoundError
        text = subtitle.read_text(encoding="utf-8-sig", errors="replace")
        body = srt_to_vtt(text).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), MuseHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="啟動 Muse 本機影片研究介面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert STATIC_DIR.joinpath("index.html").is_file()
        assert list_sites()
        print("[OK] Muse Web UI is ready.")
        return 0
    server = create_server(args.host, args.port)
    url = f"http://{args.host}:{server.server_port}/"
    print(f"[READY] Muse is running at {url}")
    print("[INFO] Keep this window open. Press Ctrl+C to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP] Muse has stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
