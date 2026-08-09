"""Lightweight local review/edit server for an EDL.

Serves a single-page timeline editor (review_ui.html) against edl.json: scrub
cuts with synced video+audio (one <video> element — audio/video are the same
track, so there's nothing to fall out of sync), drag clip edges to trim, drag
clips to reorder, split at the playhead, delete a clip. Save writes the
edited ranges back to edl.json (timestamped backup written first).

No new dependencies — stdlib http.server only. Source media is streamed with
HTTP Range support so the browser can seek without buffering the whole file.

Usage:
    python helpers/review_server.py <edl.json>
    python helpers/review_server.py <edl.json> --port 8899 --no-browser
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import resolve_path  # noqa: E402

UI_PATH = Path(__file__).resolve().parent / "review_ui.html"
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


class Handler(BaseHTTPRequestHandler):
    edl_path: Path
    edit_dir: Path

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._serve_ui()
        if self.path == "/api/edl":
            return self._serve_edl()
        if self.path.startswith("/media/"):
            return self._serve_media()
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/save":
            return self._save_edl()
        self.send_error(404)

    def _serve_ui(self):
        body = UI_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_edl(self):
        edl = json.loads(self.edl_path.read_text())
        sources = edl.get("sources", {})
        durations = {
            name: probe_duration(resolve_path(rel, self.edit_dir))
            for name, rel in sources.items()
        }
        payload = {
            "ranges": edl.get("ranges", []),
            "source_durations": durations,
            "total_duration_s": edl.get("total_duration_s", 0),
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_media(self):
        name = unquote(self.path[len("/media/"):].split("?")[0])
        edl = json.loads(self.edl_path.read_text())
        sources = edl.get("sources", {})
        if name not in sources:
            return self.send_error(404, "unknown source")
        path = resolve_path(sources[name], self.edit_dir)
        if not path.exists():
            return self.send_error(404, "file missing")

        file_size = path.stat().st_size
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        range_header = self.headers.get("Range")

        start, end = 0, file_size - 1
        status = 200
        if range_header:
            m = RANGE_RE.match(range_header)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()

        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            chunk = 1024 * 1024
            while remaining > 0:
                data = f.read(min(chunk, remaining))
                if not data:
                    break
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(data)

    def _save_edl(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        ranges = body.get("ranges")
        if not isinstance(ranges, list) or not ranges:
            return self.send_error(400, "ranges required")

        edl = json.loads(self.edl_path.read_text())
        backup = self.edl_path.with_name(
            f"edl.backup.{datetime.now().strftime('%Y%m%d%H%M%S')}.json"
        )
        shutil.copy2(self.edl_path, backup)

        edl["ranges"] = ranges
        edl["total_duration_s"] = round(sum(r["end"] - r["start"] for r in ranges), 3)
        self.edl_path.write_text(json.dumps(edl, indent=2))

        resp = json.dumps({"ok": True, "total_duration_s": edl["total_duration_s"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


def main() -> None:
    ap = argparse.ArgumentParser(description="Local review/edit server for an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("--port", type=int, default=0, help="Port (0 = pick automatically)")
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    Handler.edl_path = edl_path
    Handler.edit_dir = edl_path.parent

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"review server running at {url}")
    print("drag edges to trim, drag clips to reorder, S to split, Del to remove, Ctrl/Cmd+Z to undo")
    print("click Save to write back to edl.json (a timestamped backup is written first)")
    print("Ctrl+C to stop")

    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
