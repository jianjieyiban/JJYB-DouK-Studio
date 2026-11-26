#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DouK Studio - local modern control center for DouK-Downloader and yt-dlp."""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------

APP_VERSION = "1.2.0"

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
STATIC = BASE / "static"
DATA = BASE / "data"

SETTINGS_FILE = DATA / "app-settings.json"
TASKS_FILE = DATA / "tasks.json"
HISTORY_FILE = DATA / "history.json"
LOG_FILE = DATA / "app.log"

ORIGINAL_EXE = ROOT / "main.exe"
ORIGINAL_SETTINGS = ROOT / "_internal" / "Volume" / "settings.json"

DATA.mkdir(parents=True, exist_ok=True)
STATIC.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "port": 8765,
    "api_port": 5555,
    "download_dir": str(BASE / "Downloads"),
    "max_concurrent": 3,
    "theme": "light",
    "yt_dlp_path": "",
    "ffmpeg_path": "",
    "default_engine": "auto",
    "default_quality": "best",
    "audio_format": "mp3",
    "subtitle_langs": "zh-Hans,zh-CN,zh,en",
    "write_subtitles": False,
    "embed_subtitles": False,
    "write_thumbnail": False,
    "cookie_file": "",
    "proxy": "",
    "auto_start_engine": False,
    "filename_template": "{title} [{id}]",
}

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

LOCK = threading.RLock()
BROWSER_FALLBACK_LOCK = threading.Lock()
BROWSER_LAST_STATE: dict = {"captcha": False, "title": "", "checked_at": ""}
ORIGINAL_CLI_LOCK = threading.Lock()
LOG_CACHE: list[str] = []


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(message: str, level: str = "INFO") -> None:
    line = f"[{now()}] [{level}] {message}"
    with LOCK:
        LOG_CACHE.append(line)
        if len(LOG_CACHE) > 800:
            del LOG_CACHE[:-600]
        try:
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
    print(line, flush=True)


def load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        log(f"Failed to read {path.name}: {exc}", "WARN")
    return default


def load_json_dict(path: Path) -> dict:
    data = load_json(path, {})
    if not isinstance(data, dict):
        log(f"{path.name} 格式异常（应为对象），已按空配置处理", "WARN")
        return {}
    return data


def safe_int(value, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return default
        return int(float(text))
    except (TypeError, ValueError, OverflowError):
        return default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"COM{i}" for i in range(1, 10)
} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(value: str, max_len: int = 110) -> str:
    value = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(value or "")).strip()
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = (value[:max_len] or "untitled").strip(" .")
    stem = value.split(".", 1)[0].upper().strip()
    if stem in WINDOWS_RESERVED_NAMES:
        value = "_" + value
    return value or "untitled"


def valid_filename_template(value, default: str | None = None) -> str:
    fallback = DEFAULT_SETTINGS["filename_template"] if default is None else default
    text = str(value or "").strip()
    if not text or any(ch in text for ch in ("/", "\\", ":", "\x00")):
        return fallback
    return text


def clean_media_url(url) -> str:
    """Douyin's playwm links carry a watermark; play links do not."""
    text = str(url or "")
    return text.replace("playwm", "play") if "playwm" in text else text


class BadRequest(Exception):
    pass


def parse_size(text: str) -> int:
    m = re.match(r"([\d.]+)\s*([KMGT]?i?B)", str(text), re.I)
    if not m:
        return 0
    value = float(m.group(1))
    unit = m.group(2).upper().replace("I", "")
    mul = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * mul.get(unit, 1))


def human_size(num) -> str:
    try:
        n = float(num or 0)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


def http_request(
    url: str,
    method: str = "GET",
    payload=None,
    timeout: int = 30,
    headers: dict | None = None,
    raw: bool = False,
):
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "DouK-Studio/1.0"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        if raw:
            return body, dict(resp.headers)
        return json.loads(body.decode("utf-8"))


def pick_directory_windows(initial: str = "") -> str:
    """Open a native Windows folder picker without external dependencies."""
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPCWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", wintypes.UINT),
            ("lpfn", ctypes.c_void_p),
            ("lParam", wintypes.LPARAM),
            ("iImage", ctypes.c_int),
        ]

    BIF_RETURNONLYFSDIRS = 0x00000001
    BIF_EDITBOX = 0x00000010
    BIF_NEWDIALOGSTYLE = 0x00000040

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.argtypes = [ctypes.c_void_p]
    ole32.CoInitialize.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []

    display = ctypes.create_unicode_buffer(260)
    path_buf = ctypes.create_unicode_buffer(2048)
    info = BROWSEINFOW()
    info.hwndOwner = None
    info.pidlRoot = None
    info.pszDisplayName = ctypes.cast(display, wintypes.LPCWSTR)
    info.lpszTitle = f"选择下载目录（当前：{initial or '未设置'}）"[:180]
    info.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
    info.lpfn = None
    info.lParam = 0
    info.iImage = 0

    hr = ole32.CoInitialize(None)
    try:
        pidl = shell32.SHBrowseForFolderW(ctypes.byref(info))
        if not pidl:
            return ""
        ok = shell32.SHGetPathFromIDListW(pidl, path_buf)
        ole32.CoTaskMemFree(pidl)
        return path_buf.value if ok else ""
    finally:
        if hr == 0:
            ole32.CoUninitialize()


def _ws_connect(url: str):
    parsed = urllib.parse.urlparse(url)
    sock = socket.create_connection((parsed.hostname, parsed.port), timeout=15)
    key = base64.b64encode(os.urandom(16)).decode()
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("websocket handshake EOF")
        buf += chunk
    if b"101" not in buf.split(b"\r\n", 1)[0]:
        raise RuntimeError(buf[:200].decode(errors="ignore"))
    sock.settimeout(5)
    return sock


def _ws_send(sock, obj) -> None:
    data = json.dumps(obj).encode()
    mask = os.urandom(4)
    head = bytearray([0x81])
    n = len(data)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += struct.pack("!H", n)
    else:
        head.append(0x80 | 127)
        head += struct.pack("!Q", n)
    head += mask
    sock.sendall(head + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _ws_recv(sock) -> dict:
    payload = b""
    while True:
        header = sock.recv(2)
        if len(header) < 2:
            raise RuntimeError("websocket closed")
        fin = header[0] & 0x80
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", sock.recv(8))[0]
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise RuntimeError("websocket closed")
            data += chunk
        if opcode == 0x8:
            raise RuntimeError("websocket closed by peer")
        payload += data
        if fin:
            break
    return json.loads(payload.decode("utf-8", "ignore"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def browser_fetch_douyin_detail(video_url: str, wait_seconds: int = 28) -> dict | None:
    """Read Douyin's own signed detail response from a temporary browser window.

    Visible window by default (browser_headed setting): headless gets a captcha
    wall from Douyin's risk control even with valid login cookies.
    """
    if os.name != "nt":
        return None
    headed = bool(SETTINGS.get("browser_headed", True))
    edge = None
    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).exists():
            edge = candidate
            break
    if not edge:
        return None

    sock = None
    proc = None
    locked = False
    try:
        BROWSER_FALLBACK_LOCK.acquire()
        locked = True
        profile = DATA / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        proc = subprocess.Popen(
            [edge]
            + ([] if headed else ["--headless=new"])
            + [
                "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
                f"--user-agent={ua}", f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}", "--remote-allow-origins=*",
                "--window-size=1200,820", "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pages = []
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                pages = [
                    t for t in tabs
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                ]
                if pages:
                    break
            except Exception:
                time.sleep(0.25)
        if not pages:
            return None
        sock = _ws_connect(pages[0]["webSocketDebuggerUrl"])
        _ws_send(sock, {"id": 1, "method": "Page.enable"})
        _ws_send(sock, {"id": 2, "method": "Network.enable"})
        _ws_send(sock, {"id": 3, "method": "Page.navigate", "params": {"url": video_url}})

        candidates = []
        end = time.time() + wait_seconds
        while time.time() < end:
            try:
                msg = _ws_recv(sock)
            except Exception:
                continue
            if msg.get("method") == "Network.responseReceived":
                resp = msg.get("params", {}).get("response", {})
                if "aweme" in resp.get("url", "") and "detail" in resp.get("url", ""):
                    candidates.append(msg["params"]["requestId"])

        for index, request_id in enumerate(candidates[:8]):
            try:
                _ws_send(sock, {"id": 100 + index, "method": "Network.getResponseBody", "params": {"requestId": request_id}})
                body = None
                end = time.time() + 12
                while time.time() < end:
                    try:
                        msg = _ws_recv(sock)
                    except Exception:
                        break
                    if msg.get("id") == 100 + index:
                        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                        body = result.get("body")
                        if result.get("base64Encoded"):
                            body = base64.b64decode(body or "").decode("utf-8", "ignore")
                        break
            except Exception as exc:
                log(f"browser body read failed for {request_id}: {exc}", "WARN")
                continue
            if not body:
                continue
            try:
                data = json.loads(body)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            detail = data.get("aweme_detail") or nested.get("aweme_detail")
            if isinstance(detail, dict):
                return detail
        return None
    except Exception as exc:
        log(f"browser detail fallback failed: {exc}", "WARN")
        return None
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if locked:
            BROWSER_FALLBACK_LOCK.release()


def browser_fetch_account_awemes(
    sec_uid: str, max_count: int = 100, wait_seconds: int = 50
) -> list[dict]:
    """Scrape a creator's post list from Douyin's own signed browser responses.

    DouK 5.7's signature is outdated for the account endpoint, so we let the
    headless browser sign the requests and simply read the responses.
    Uses the shared browser_capture_json pipeline: login-cookie injection,
    immediate body fetch (Edge evicts buffered bodies quickly), scrolling and
    captcha detection.
    """
    if os.name != "nt" or not sec_uid:
        return []
    nav = f"https://www.douyin.com/user/{urllib.parse.quote(sec_uid, safe='')}?showTab=post"
    bodies = browser_capture_json(
        nav,
        ("aweme/post", "aweme/v1/web/aweme/post"),
        wait_seconds=wait_seconds,
        max_bodies=30,
    )
    awemes = _collect_awemes_from_json(bodies, ("aweme_list",), max_count)
    if awemes:
        log(f"account capture: {len(awemes)} awemes from {len(bodies)} bodies")
    return awemes


def _inject_login_cookies(sock) -> int:
    """Push the saved douyin login cookie into a CDP browser session before navigation."""
    ck = login_cookie_str()
    if not ck:
        return 0
    cmd = 900  # dedicated id space; responses are ignored by the capture loop
    count = 0
    for chunk in ck.split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or len(name) > 64 or len(value) > 4096:
            continue
        cmd += 1
        try:
            _ws_send(sock, {
                "id": cmd,
                "method": "Network.setCookie",
                "params": {
                    "name": name,
                    "value": value,
                    "domain": ".douyin.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                },
            })
            count += 1
        except Exception:
            pass
    if count:
        log(f"injected {count} login cookies into browser fallback")
    return count


def browser_capture_json(
    nav_url: str,
    patterns: tuple[str, ...],
    wait_seconds: int = 40,
    scroll: bool = True,
    click_js: str = "",
    click_interval: float = 6.0,
    url_filter: str = "",
    max_bodies: int = 40,
    headed: bool | None = None,
) -> list:
    """Launch a browser, navigate, and return parsed JSON bodies of matching responses.

    Uses a VISIBLE window by default: Douyin's risk control serves a captcha
    wall to headless browsers even with valid login cookies, while the same
    profile in a headed window loads normally. Set browser_headed=false to
    run headless again.
    """
    if os.name != "nt":
        return []
    if headed is None:
        headed = bool(SETTINGS.get("browser_headed", True))
    browser = None
    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).exists():
            browser = candidate
            break
    if not browser:
        return []

    sock = None
    proc = None
    locked = False
    try:
        BROWSER_FALLBACK_LOCK.acquire()
        locked = True
        profile = DATA / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        proc = subprocess.Popen(
            [browser]
            + ([] if headed else ["--headless=new"])
            + [
                "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", "--disable-blink-features=AutomationControlled",
                f"--user-agent={ua}", f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}", "--remote-allow-origins=*",
                "--window-size=1200,820", "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pages = []
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                pages = [
                    t for t in tabs
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                ]
                if pages:
                    break
            except Exception:
                time.sleep(0.25)
        if not pages:
            return []
        sock = _ws_connect(pages[0]["webSocketDebuggerUrl"])
        _ws_send(sock, {"id": 1, "method": "Page.enable"})
        _ws_send(sock, {"id": 2, "method": "Network.enable"})
        _inject_login_cookies(sock)
        _ws_send(sock, {"id": 3, "method": "Page.navigate", "params": {"url": nav_url}})

        pending: dict[int, str] = {}
        wanted: dict[str, str] = {}
        body_tried: set[str] = set()
        retry_at: dict[str, float] = {}
        collected_bodies: list = []
        end = time.time() + max(20, wait_seconds)
        last_scroll = time.time()
        last_click = time.time()
        last_title_check = 0.0
        title_checks: dict[int, bool] = {}
        captcha_hit = False
        title = ""
        cmd_id = 100
        fetch_id = 500
        while time.time() < end:
            try:
                msg = _ws_recv(sock)
            except Exception:
                continue
            if msg.get("method") == "Network.responseReceived":
                url = msg.get("params", {}).get("response", {}).get("url", "")
                lowered = url.lower()
                if any(p in lowered for p in patterns) and (not url_filter or url_filter in lowered):
                    request_id = msg["params"]["requestId"]
                    wanted[request_id] = url
                    if request_id not in body_tried:
                        # Small responses are usually complete already; large ones
                        # still stream and fail here, retried at loadingFinished.
                        body_tried.add(request_id)
                        fetch_id += 1
                        pending[fetch_id] = request_id
                        try:
                            _ws_send(sock, {
                                "id": fetch_id,
                                "method": "Network.getResponseBody",
                                "params": {"requestId": request_id},
                            })
                        except Exception as exc:
                            log(f"browser body request failed: {exc}", "WARN")
            elif msg.get("method") == "Network.loadingFinished":
                request_id = msg.get("params", {}).get("requestId", "")
                if request_id in wanted and request_id not in body_tried:
                    # The proper moment to read the body: streaming is complete,
                    # so large payloads (multi-MB aweme lists) succeed here.
                    body_tried.add(request_id)
                    fetch_id += 1
                    pending[fetch_id] = request_id
                    try:
                        _ws_send(sock, {
                            "id": fetch_id,
                            "method": "Network.getResponseBody",
                            "params": {"requestId": request_id},
                        })
                    except Exception as exc:
                        log(f"browser body request failed: {exc}", "WARN")
            elif msg.get("id") in pending:
                request_id = pending.pop(msg["id"])
                if "error" in msg:
                    # Body evicted or not ready yet: schedule one delayed retry.
                    if request_id not in retry_at:
                        retry_at[request_id] = time.time() + 1.0
                        body_tried.discard(request_id)
                    result = {}
                else:
                    result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                body = result.get("body")
                if result.get("base64Encoded"):
                    try:
                        body = base64.b64decode(body or "").decode("utf-8", "ignore")
                    except Exception:
                        body = ""
                if body:
                    try:
                        data = json.loads(body)
                    except Exception:
                        data = None
                    if data is not None:
                        collected_bodies.append(data)
                        wanted.pop(request_id, None)
                        if len(collected_bodies) >= max_bodies:
                            break
            elif msg.get("id") in title_checks:
                title_checks.pop(msg["id"], None)
                title = str((msg.get("result") or {}).get("result", {}).get("value") or "")
                if "验证码" in title or "verify" in title.lower():
                    captcha_hit = True
            now_ts = time.time()
            if scroll and now_ts - last_scroll >= 6:
                last_scroll = now_ts
                cmd_id += 1
                _ws_send(sock, {
                    "id": cmd_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "window.scrollTo(0, document.body.scrollHeight);"},
                })
            if click_js and now_ts - last_click >= click_interval:
                last_click = now_ts
                cmd_id += 1
                _ws_send(sock, {
                    "id": cmd_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": click_js},
                })
            if now_ts - last_title_check >= 5:
                last_title_check = now_ts
                cmd_id += 1
                title_checks[cmd_id] = True
                _ws_send(sock, {
                    "id": cmd_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": "document.title", "returnByValue": True},
                })
            if retry_at:
                due = [rid for rid, ts in retry_at.items() if now_ts >= ts]
                for request_id in due:
                    retry_at.pop(request_id, None)
                    if request_id in wanted and request_id not in body_tried:
                        body_tried.add(request_id)
                        fetch_id += 1
                        pending[fetch_id] = request_id
                        try:
                            _ws_send(sock, {
                                "id": fetch_id,
                                "method": "Network.getResponseBody",
                                "params": {"requestId": request_id},
                            })
                        except Exception:
                            pass
            if captcha_hit:
                BROWSER_LAST_STATE.update({"captcha": True, "title": title, "checked_at": now()})
                log(f"browser hit captcha page: {nav_url[:80]}", "WARN")
                break

        if not captcha_hit:
            BROWSER_LAST_STATE.update({"captcha": False, "title": title, "checked_at": now()})
        return collected_bodies
    except Exception as exc:
        log(f"browser capture failed: {exc}", "WARN")
        return []
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if locked:
            BROWSER_FALLBACK_LOCK.release()


def _collect_awemes_from_json(bodies: list, list_keys: tuple[str, ...], max_count: int) -> list[dict]:
    """Extract unique aweme dicts from captured JSON responses."""
    awemes: list[dict] = []
    seen: set[str] = set()

    def push(obj):
        if isinstance(obj, dict):
            aid = str(obj.get("aweme_id") or "")
            if aid and aid not in seen:
                seen.add(aid)
                awemes.append(obj)

    for data in bodies:
        if not isinstance(data, dict):
            continue
        for key in list_keys:
            rows = data.get(key)
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    if key == "data":
                        info = row.get("aweme_info") if isinstance(row.get("aweme_info"), dict) else None
                        if info:
                            push(info)
                        elif row.get("aweme_id"):
                            push(row)
                    else:
                        push(row)
        if len(awemes) >= max_count:
            break
    return awemes[:max_count]


def browser_fetch_mix_awemes(detail_id: str, max_count: int = 50) -> list[dict]:
    """Read the mix (合集) playlist of a video from the browser's own signed responses."""
    if not detail_id:
        return []
    bodies = browser_capture_json(
        f"https://www.douyin.com/video/{urllib.parse.quote(detail_id, safe='')}",
        ("mix/aweme", "aweme/v1/web/mix"),
        wait_seconds=45,
    )
    return _collect_awemes_from_json(bodies, ("aweme_list",), max_count)


def browser_fetch_search_awemes(keyword: str, max_count: int = 20) -> list[dict]:
    """Read Douyin search results (video cards) from the browser's own signed responses."""
    keyword = str(keyword or "").strip()
    if not keyword:
        return []
    nav = f"https://www.douyin.com/search/{urllib.parse.quote(keyword, safe='')}?type=video"
    bodies = browser_capture_json(
        nav,
        ("general/search/single", "search/item", "aweme/v1/web/search"),
        wait_seconds=45,
    )
    return _collect_awemes_from_json(bodies, ("data", "aweme_list"), max_count)


REPLY_CLICK_JS = (
    "(() => {"
    "let n = 0;"
    "document.querySelectorAll('span,p,div,button').forEach(el => {"
    "if (el.children.length === 0 && /\\d+\\s*条回复/.test(el.textContent || '')) { el.click(); n++; }"
    "});"
    "return String(n);"
    "})()"
)


def browser_fetch_replies(detail_id: str, comment_id: str, max_count: int = 40) -> list[dict]:
    """Read comment replies by clicking the reply expanders and reading signed responses."""
    if not detail_id or not comment_id:
        return []
    bodies = browser_capture_json(
        f"https://www.douyin.com/video/{urllib.parse.quote(detail_id, safe='')}",
        ("comment/list/reply",),
        url_filter=f"comment_id={urllib.parse.quote(str(comment_id), safe='')}",
        wait_seconds=55,
        scroll=False,
        click_js=REPLY_CLICK_JS,
        click_interval=5.0,
    )
    replies: list[dict] = []
    seen: set[str] = set()
    for data in bodies:
        if not isinstance(data, dict):
            continue
        rows = data.get("comments") if isinstance(data.get("comments"), list) else []
        for row in rows:
            if isinstance(row, dict):
                cid = str(row.get("cid") or "")
                if cid and cid not in seen:
                    seen.add(cid)
                    replies.append(row)
        if len(replies) >= max_count:
            break
    return replies[:max_count]


def _aweme_list_to_media(awemes: list, platform: str = "douyin") -> list[dict]:
    items: list[dict] = []
    for aweme in awemes:
        if not isinstance(aweme, dict):
            continue
        aweme_id = str(aweme.get("aweme_id") or "")
        detail_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else ""
        media = detail_to_media(aweme, detail_url)
        if media.get("video_url") or media.get("images") or media.get("cover"):
            items.append(media)
    return items


def browser_fetch_media_fallback(endpoint: str, payload: dict) -> tuple[list, bool]:
    """Browser fallbacks for collect endpoints whose DouK signature broke (500)."""
    if endpoint == "douyin/account":
        sec_uid = str(payload.get("sec_user_id") or payload.get("sec_uid") or "").strip()
        limit = max(1, min(500, safe_int(payload.get("count"), 30)))
        awemes = browser_fetch_account_awemes(sec_uid, max_count=limit) if sec_uid else []
        if not awemes:
            return [], False
        log(f"account fallback: browser fetched {len(awemes)} awemes", "INFO")
        return _aweme_list_to_media(awemes), True
    if endpoint == "douyin/mix":
        detail_id = str(payload.get("detail_id") or "").strip()
        count = max(1, min(200, safe_int(payload.get("count"), 50)))
        awemes = browser_fetch_mix_awemes(detail_id, max_count=count) if detail_id else []
        if not awemes:
            return [], False
        log(f"mix fallback: browser fetched {len(awemes)} awemes", "INFO")
        return _aweme_list_to_media(awemes), True
    if endpoint.startswith("douyin/search/"):
        keyword = str(payload.get("keyword") or "").strip()
        count = max(1, min(100, safe_int(payload.get("count"), 20)))
        awemes = browser_fetch_search_awemes(keyword, max_count=count) if keyword else []
        if not awemes:
            return [], False
        log(f"search fallback: browser fetched {len(awemes)} awemes", "INFO")
        return _aweme_list_to_media(awemes), True
    return [], False


def collect_empty_error(endpoint: str, payload: dict) -> str:
    if BROWSER_LAST_STATE.get("captcha"):
        return (
            f"浏览器兜底被抖音验证码拦截（页面：{BROWSER_LAST_STATE.get('title') or '验证码'}）。"
            "这是抖音的风控机制，与登录无关，通常因短时间内自动化访问频率过高触发。"
            "建议：稍等 10-30 分钟后再试；或在「设置 → 抖音登录」完成一次扫码登录（会给浏览器积累正常浏览记录，降低风控等级）；"
            "也可以重启路由器更换公网 IP 后重试"
        )
    if endpoint == "douyin/account":
        sec = str((payload or {}).get("sec_user_id") or "").strip()
        hint = ""
        if sec and not sec.startswith("MS4wLjABAAAA"):
            hint = f"（注意：sec_uid 通常以 MS4wLjABAAAA 开头，你输入的「{sec[:24]}…」格式不符，可能不是 sec_uid）"
        elif sec:
            hint = (
                "（sec_uid 必须逐字符完全一致，手工抄写极易出错——i/l、1/I、0/O、-/_ 等字符肉眼难分。"
                "请勿手抄 sec_uid：在抖音 App 或网页版打开博主主页，点「分享 → 复制链接」，"
                "把得到的完整链接直接粘贴进来，系统会自动识别博主）"
            )
        else:
            hint = "（请输入博主主页链接或该博主任意一条作品链接，系统会自动识别博主）"
        return (
            "未获取到主页作品：DouK 接口与浏览器兜底均无数据。" + hint
        )
    if endpoint == "douyin/mix":
        return (
            "未获取到合集作品：请粘贴合集内任意一条作品的链接或 ID，系统会自动加载该合集；"
            "若该作品不属于任何合集则会为空"
        )
    if endpoint.startswith("douyin/search/"):
        kind = endpoint.rsplit("/", 1)[-1]
        suffix = (
            "可在「设置 → 抖音登录」一键扫码登录后重试，登录态会自动用于采集"
            if not login_cookie_str()
            else "已登录抖音仍无结果，可能是该关键词被限流，请换词重试"
        )
        return (
            f"未获取到「{kind}」搜索结果：抖音网页搜索需要登录账号，DouK 搜索接口签名已失效，"
            f"未登录的浏览器也无法获取。{suffix}"
        )
    if endpoint == "douyin/reply":
        return (
            "未获取到评论回复：DouK 回复接口签名失效，抖音对未登录的浏览器请求也只返回空数据（服务端限制）。"
            "可在「设置 → 抖音登录」一键扫码登录后重试；「评论采集」功能本身可用，其中已包含每条评论的回复数量与 IP 属地信息"
        )
    return "未获取到数据"


def first_media_url(container, *keys):
    if not isinstance(container, dict):
        return ""
    for key in keys:
        obj = container.get(key) or {}
        if not isinstance(obj, dict):
            continue
        urls = obj.get("url_list") or []
        if urls and isinstance(urls[0], str):
            return clean_media_url(urls[0])
    return ""


def quality_formats_from_video(video: dict) -> list[dict]:
    """Build a compact quality list from Douyin's bit_rate/play_addr metadata."""
    video = video if isinstance(video, dict) else {}
    formats: list[dict] = []
    bit_rates = video.get("bit_rate")
    if isinstance(bit_rates, list):
        for index, entry in enumerate(bit_rates):
            if not isinstance(entry, dict):
                continue
            play = entry.get("play_addr") if isinstance(entry.get("play_addr"), dict) else {}
            url = ""
            urls = play.get("url_list") or []
            if urls and isinstance(urls[0], str):
                url = clean_media_url(urls[0])
            if not url:
                continue
            height = safe_int(play.get("height") or entry.get("height"))
            width = safe_int(play.get("width") or entry.get("width"))
            bitrate = safe_int(entry.get("bit_rate"))
            size = safe_int(play.get("data_size"))
            url_key = str(play.get("url_key") or entry.get("url_key") or "")
            lowered = url_key.lower()
            codec = "H.265" if "265" in lowered else ("ByteVC1" if "bytevc" in lowered else "H.264")
            label = f"{height}P · {codec}" if height else f"清晰度 {index + 1} · {codec}"
            details = []
            if width and height:
                details.append(f"{width}×{height}")
            if bitrate:
                details.append(f"{bitrate / 1_000_000:.2f} Mbps")
            if size:
                details.append(human_size(size))
            if details:
                label += " · " + " · ".join(details)
            formats.append({
                "id": f"br-{index}",
                "label": label,
                "height": height,
                "width": width,
                "bitrate": bitrate,
                "size": size,
                "codec": codec,
                "format": str(entry.get("format") or "mp4"),
                "url": url,
            })
    if not formats:
        fallbacks = (
            ("play_addr", "默认"),
            ("play_addr_h264", "H.264"),
            ("play_addr_265", "H.265"),
            ("download_addr", "下载"),
        )
        seen = set()
        for key, codec in fallbacks:
            url = first_media_url(video, key)
            if not url or url in seen:
                continue
            seen.add(url)
            addr = video.get(key) if isinstance(video.get(key), dict) else {}
            height = safe_int(addr.get("height") or video.get("height"))
            formats.append({
                "id": key,
                "label": f"{height}P · {codec}" if height else f"默认 · {codec}",
                "height": height,
                "width": safe_int(addr.get("width") or video.get("width")),
                "bitrate": 0,
                "size": safe_int(addr.get("data_size")),
                "codec": codec,
                "format": str(video.get("format") or "mp4"),
                "url": url,
            })
    formats.sort(key=lambda item: (-safe_int(item.get("height")), -safe_int(item.get("bitrate"))))
    return formats[:20]


def detail_to_media(detail: dict, source_url: str = "") -> dict:
    detail = detail if isinstance(detail, dict) else {}
    video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
    author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
    music_detail = detail.get("music") if isinstance(detail.get("music"), dict) else {}
    image_info = detail.get("image_post_info")
    image_info = image_info if isinstance(image_info, dict) else {}

    def first_url(container, *keys):
        return first_media_url(container, *keys)

    formats = quality_formats_from_video(video)
    for fmt in formats:
        fmt["url"] = clean_media_url(fmt.get("url"))
    play_url = first_url(video, "play_addr", "download_addr", "play_addr_h264")
    if formats and formats[0].get("url"):
        play_url = formats[0]["url"]
    play_url = clean_media_url(play_url)
    cover = clean_media_url(first_url(video, "cover", "dynamic_cover", "origin_cover"))
    music = clean_media_url(first_url(music_detail, "play_url"))
    images = []
    image_list = image_info.get("images")
    if isinstance(image_list, list):
        for image in image_list:
            if not isinstance(image, dict):
                continue
            url = clean_media_url(first_url(image, "display_image", "thumb"))
            if url:
                images.append(url)
    if not images and cover:
        images = [cover]
    duration = video.get("duration") or 0
    if duration and duration > 10000:
        duration = duration / 1000
    return {
        "id": str(detail.get("aweme_id") or ""),
        "title": str(detail.get("desc") or "抖音作品")[:180],
        "author": str(author.get("nickname") or ""),
        "cover": cover,
        "duration": duration,
        "video_url": play_url,
        "audio_url": music,
        "images": images,
        "formats": formats,
        "source": "douk",
        "raw": {
            "aweme_id": detail.get("aweme_id"),
            "source_url": source_url,
            "fallback": "browser-detail",
            "desc": detail.get("desc") or "",
            "create_time": detail.get("create_time") or 0,
            "text_extra": detail.get("text_extra") if isinstance(detail.get("text_extra"), list) else [],
            "author_sec_uid": author.get("sec_uid") or "",
        },
    }


def api_base() -> str:
    api_port = safe_int(SETTINGS.get("api_port"), 5555)
    if not 1 <= api_port <= 65535:
        api_port = 5555
    return f"http://127.0.0.1:{api_port}"


def api_alive(timeout: int = 2) -> bool:
    try:
        result = http_request(f"{api_base()}/token", timeout=timeout)
        return isinstance(result, dict) and bool(result.get("message"))
    except Exception:
        return False


def read_original_settings(strict: bool = False) -> dict:
    try:
        data = json.loads(ORIGINAL_SETTINGS.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("原工具配置格式异常")
        return data
    except Exception as exc:
        if strict:
            raise
        if not isinstance(exc, FileNotFoundError):
            log(f"Failed to read original settings: {exc}", "WARN")
        return {}


def cookie_header(original: dict | None = None) -> str:
    ck = login_cookie_str()
    if ck:
        return ck
    original = original or read_original_settings()
    cookie = original.get("cookie", "")
    if isinstance(cookie, dict):
        return "; ".join(f"{k}={v}" for k, v in cookie.items() if v not in (None, ""))
    return str(cookie or "")


def browser_headers(url: str, original: dict | None = None) -> dict:
    original = original or read_original_settings()
    info = original.get("browser_info") or {}
    ua = info.get("User-Agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    host = urllib.parse.urlparse(url).netloc
    referer = "https://www.douyin.com/" if "douyin" in host else "https://www.tiktok.com/"
    headers = {"User-Agent": ua, "Referer": referer, "Accept": "*/*"}
    ck = cookie_header(original)
    if ck:
        headers["Cookie"] = ck
    return headers


# ---------------------------------------------------------------------------
# Douyin login / cookie management
# ---------------------------------------------------------------------------

LOGIN_FILE = DATA / "douyin-login.json"
LOGIN_WINDOW_SECONDS = 300
LOGIN_STATE: dict = {"phase": "idle", "method": "", "message": "", "saved_at": ""}
LOGIN_STATE_LOCK = threading.Lock()
LOGIN_RUNNING = threading.Lock()


def _login_state(phase: str | None = None, method: str | None = None, message: str | None = None) -> dict:
    with LOGIN_STATE_LOCK:
        if phase is not None:
            LOGIN_STATE["phase"] = phase
        if method is not None:
            LOGIN_STATE["method"] = method
        if message is not None:
            LOGIN_STATE["message"] = str(message)
        return dict(LOGIN_STATE)


def login_status_snapshot() -> dict:
    login = load_login()
    cookie = str(login.get("cookie") or "")
    active = _login_state()
    return {
        "logged_in": bool(cookie),
        "method": str(login.get("method") or ""),
        "saved_at": str(login.get("saved_at") or ""),
        "cookie_count": len(cookie.split(";")) if cookie else 0,
        "phase": active.get("phase") or "idle",
        "message": active.get("message") or "",
        "last_validation": login.get("last_validation") or {},
    }


def load_login() -> dict:
    data = load_json(LOGIN_FILE, {})
    return data if isinstance(data, dict) else {}


def login_cookie_str() -> str:
    return str(load_login().get("cookie") or "")


def normalize_cookie_text(text) -> str:
    """Accept a raw Cookie header string (or JSON dict) and return a clean cookie string."""
    text = str(text or "").strip()
    if not text:
        return ""
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return "; ".join(f"{k}={v}" for k, v in obj.items() if str(v).strip())
        except Exception:
            pass
    if text.startswith("Cookie:"):
        text = text[len("Cookie:"):].strip()
    parts = []
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if "=" in chunk and chunk.split("=", 1)[0].strip():
            parts.append(chunk)
    return "; ".join(parts)


def save_login_cookie(cookie: str, method: str) -> None:
    data = load_login()
    if "previous_cookie_backup" not in data:
        try:
            data["previous_cookie_backup"] = read_original_settings().get("cookie", "")
        except Exception:
            data["previous_cookie_backup"] = ""
    data["cookie"] = cookie
    data["method"] = method
    data["saved_at"] = now()
    save_json(LOGIN_FILE, data)
    _login_state(phase="idle", method=method, message=f"Cookie 已保存（{method}）")
    log(f"douyin login cookie saved via {method}")
    apply_login_cookie(cookie)


def apply_login_cookie(cookie: str) -> None:
    """Sync the login cookie into the original tool's settings and restart the engine."""
    try:
        original = read_original_settings()
        if str(original.get("cookie") or "") == cookie:
            return
        original["cookie"] = cookie
        save_json(ORIGINAL_SETTINGS, original)
        log("login cookie synced into original settings.json")
        if api_alive():
            ok, _ = stop_engine()
            if ok:
                _ok, msg = start_engine()
                log(f"engine restarted to apply cookie: {msg}")
    except Exception as exc:
        log(f"apply login cookie failed: {exc}", "WARN")


def logout_douyin() -> tuple[bool, str]:
    data = load_login()
    if not data.get("cookie"):
        return True, "当前未登录"
    try:
        original = read_original_settings()
        original["cookie"] = data.get("previous_cookie_backup", "")
        save_json(ORIGINAL_SETTINGS, original)
        if api_alive():
            ok, _ = stop_engine()
            if ok:
                _ok, msg = start_engine()
                log(f"engine restarted after logout: {msg}")
    except Exception as exc:
        log(f"logout restore failed: {exc}", "WARN")
    try:
        LOGIN_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    _login_state(phase="idle", method="", message="")
    log("douyin logout: cookie cleared")
    return True, "已退出登录，Cookie 已清除"


def validate_login_cookie() -> dict:
    """Check whether the saved Cookie is still accepted by Douyin.

    Opens a short-lived visible browser on the user's own profile page
    (douyin.com/user/self): a valid session renders the account nickname in
    the title, an invalid one bounces to the login page. The result is stored
    so the UI can display it until the next check.
    """
    cookie = login_cookie_str()
    if not cookie:
        result = {"valid": False, "nickname": "", "message": "尚未保存 Cookie，请先登录", "checked_at": now()}
        _store_validation(result)
        return result
    has_session = any(
        p.strip().lower().startswith(("sessionid=", "sessionid_ss="))
        for p in cookie.split(";")
    )
    if not has_session:
        result = {
            "valid": False,
            "nickname": "",
            "message": "Cookie 中缺少 sessionid（登录凭证缺失），当前 Cookie 无效",
            "checked_at": now(),
        }
        _store_validation(result)
        return result

    browser = _find_chromium()
    if not browser:
        result = {"valid": False, "nickname": "", "message": "未找到 Chrome/Edge，无法在线验证", "checked_at": now()}
        _store_validation(result)
        return result

    proc = None
    sock = None
    try:
        profile = DATA / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        proc = subprocess.Popen(
            [
                browser, "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                "--remote-allow-origins=*", "--window-size=1000,700",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pages = []
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2))
                pages = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    break
            except Exception:
                time.sleep(0.3)
        if not pages:
            raise RuntimeError("浏览器窗口未能启动")
        sock = _ws_connect(pages[0]["webSocketDebuggerUrl"])
        _ws_send(sock, {"id": 1, "method": "Page.enable"})
        _ws_send(sock, {"id": 2, "method": "Network.enable"})
        _inject_login_cookies(sock)
        time.sleep(0.8)
        _ws_send(sock, {"id": 3, "method": "Page.navigate", "params": {
            "url": "https://www.douyin.com/user/self?showTab=post"}})

        # Drain events until the page settles (load fired + small settle delay).
        deadline = time.time() + 30
        loaded = False
        while time.time() < deadline and not loaded:
            try:
                msg = _ws_recv(sock)
            except Exception:
                continue
            if msg.get("method") == "Page.loadEventFired":
                loaded = True
        time.sleep(4.0)

        _ws_send(sock, {"id": 50, "method": "Runtime.evaluate", "params": {
            "expression": (
                "JSON.stringify({title: document.title,"
                " url: location.href,"
                " hasLoginBtn: /(?:^|\\n)登录(?:\\n|$)/.test(document.body.innerText.slice(0,600)),"
                " bodyHead: document.body.innerText.slice(0, 200)})"
            ),
            "returnByValue": True}})
        end = time.time() + 8
        info = {}
        while time.time() < end:
            try:
                msg = _ws_recv(sock)
            except Exception:
                continue
            if msg.get("id") == 50:
                try:
                    info = json.loads(msg.get("result", {}).get("result", {}).get("value") or "{}")
                except Exception:
                    info = {}
                break

        title = str(info.get("title") or "")
        nickname = ""
        if "的抖音" in title:
            nickname = title.split("的抖音")[0].strip()
        url = str(info.get("url") or "")
        valid = bool(nickname) and "verify" not in title.lower() and "验证" not in title
        if not valid and ("login" in url or "passport" in url or "sso" in url or info.get("hasLoginBtn")):
            message = "Cookie 已失效（跳转到了登录页），请重新扫码登录或重新读取浏览器 Cookie"
        elif not valid and "验证" in title:
            message = "页面被风控验证码拦截，暂无法在线验证；Cookie 本身仍在，稍后再试"
        elif nickname:
            message = f"Cookie 有效，当前登录账号：{nickname}"
        else:
            message = f"无法确认登录态（页面标题：{title or '空'}），请稍后重试"
        result = {"valid": valid, "nickname": nickname, "message": message, "checked_at": now()}
        _store_validation(result)
        log(f"cookie validation: valid={valid} nickname={nickname!r}")
        return result
    except Exception as exc:
        result = {"valid": False, "nickname": "", "message": f"验证失败：{exc}", "checked_at": now()}
        _store_validation(result)
        return result
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _store_validation(result: dict) -> None:
    try:
        data = load_login()
        data["last_validation"] = result
        save_json(LOGIN_FILE, data)
    except Exception:
        pass


def _find_chromium() -> str:
    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return ""


def _cdp_all_cookies(port: int, timeout: float = 8.0) -> list:
    """Fetch all cookies from a CDP-enabled browser (browser-level target first)."""
    try:
        version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout))
        ws_url = version.get("webSocketDebuggerUrl")
    except Exception:
        ws_url = None
    if ws_url:
        sock = None
        try:
            sock = _ws_connect(ws_url)
            _ws_send(sock, {"id": 1, "method": "Storage.getCookies"})
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = _ws_recv(sock)
                except Exception:
                    continue
                if msg.get("id") == 1:
                    result = msg.get("result") or {}
                    cookies = result.get("cookies") or []
                    if cookies:
                        return cookies
                    break
            return []
        except Exception:
            pass
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
    # Fallback: page-level Network.getAllCookies
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout))
    pages = [t for t in tabs if t.get("type") == "page"]
    if not pages:
        return []
    sock = None
    try:
        sock = _ws_connect(pages[0]["webSocketDebuggerUrl"])
        _ws_send(sock, {"id": 1, "method": "Network.enable"})
        _ws_send(sock, {"id": 2, "method": "Network.getAllCookies"})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = _ws_recv(sock)
            except Exception:
                continue
            if msg.get("id") == 2:
                return (msg.get("result") or {}).get("cookies") or []
        return []
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


def _cookie_string_from(cookies: list, domain_sub: str = "douyin") -> str:
    parts = []
    for c in cookies:
        if domain_sub and domain_sub not in str(c.get("domain") or ""):
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if name and value:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def _has_login_cookie(cookies: list) -> bool:
    for c in cookies:
        if "douyin" in str(c.get("domain") or "") and str(c.get("name") or "") in ("sessionid", "sessionid_ss"):
            return bool(str(c.get("value") or "").strip())
    return False


def browser_login_douyin() -> tuple[bool, str]:
    """Open a visible browser window for QR login; poll until the sessionid cookie shows up."""
    browser = _find_chromium()
    if not browser:
        _login_state(phase="error", message="未找到 Edge/Chrome 浏览器")
        return False, "未找到可用的浏览器（Edge/Chrome），请手动粘贴 Cookie"
    if not LOGIN_RUNNING.acquire(blocking=False):
        return False, "已有登录窗口进行中，请等待其完成或关闭"
    proc = None
    try:
        _login_state(phase="opening", message="正在打开登录窗口...")
        profile = DATA / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        proc = subprocess.Popen(
            [
                browser, "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
                "--remote-allow-origins=*", "--window-size=1100,820",
                "https://sso.douyin.com/passport/web/login/?next_url=https%3A%2F%2Fwww.douyin.com%2F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ready_until = time.time() + 30
        ready = False
        while time.time() < ready_until:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                ready = True
                break
            except Exception:
                time.sleep(0.3)
        if not ready:
            raise RuntimeError("浏览器调试端口未就绪")
        if proc.poll() is not None:
            raise RuntimeError("浏览器窗口被外部关闭")
        _login_state(phase="waiting", message="请在弹出的浏览器窗口中扫码登录抖音")
        end = time.time() + LOGIN_WINDOW_SECONDS
        while time.time() < end:
            time.sleep(2)
            if proc.poll() is not None:
                # Window closed by the user; last chance to grab cookies.
                try:
                    cookies = _cdp_all_cookies(port, timeout=3)
                except Exception:
                    cookies = []
                if _has_login_cookie(cookies):
                    ck = _cookie_string_from(cookies)
                    save_login_cookie(ck, "qr")
                    _login_state(phase="idle", message="登录成功（窗口已关闭）")
                    return True, "登录成功"
                _login_state(phase="canceled", message="浏览器窗口已关闭，登录未完成")
                return False, "浏览器窗口被关闭，登录未完成"
            try:
                cookies = _cdp_all_cookies(port)
            except Exception:
                continue
            if _has_login_cookie(cookies):
                ck = _cookie_string_from(cookies)
                if ck:
                    save_login_cookie(ck, "qr")
                    _login_state(phase="idle", message="登录成功，Cookie 已保存并同步到引擎")
                    time.sleep(1.5)
                    return True, "登录成功"
            _login_state(message=f"等待扫码确认...（剩余 {max(0, int(end - time.time()))} 秒）")
        _login_state(phase="timeout", message="登录超时，请重试")
        return False, "登录超时（5 分钟），请重试"
    except Exception as exc:
        _login_state(phase="error", message=f"登录失败：{exc}")
        log(f"browser login failed: {exc}", "WARN")
        return False, str(exc)
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        LOGIN_RUNNING.release()


def _cleanup_dir_async(path: Path) -> None:
    """Delete a temp directory in the background so callers never block on cleanup."""

    def worker():
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def _import_from_profile(user_data: Path) -> str:
    """Copy a real browser profile's cookie DB into a temp profile, read cookies via CDP."""
    browser = _find_chromium()
    if not browser:
        return ""
    tmp = DATA / "cookie-import-profile"
    proc = None
    sock = None
    try:
        (tmp / "Default" / "Network").mkdir(parents=True, exist_ok=True)
        src_state = user_data / "Local State"
        src_cookies = user_data / "Default" / "Network" / "Cookies"
        if not src_cookies.exists():
            src_cookies = user_data / "Default" / "Cookies"
        if not (src_state.exists() and src_cookies.exists()):
            return ""
        for attempt in range(3):
            try:
                shutil.copy2(src_state, tmp / "Local State")
                shutil.copy2(src_cookies, tmp / "Default" / "Network" / "Cookies")
                break
            except OSError as exc:
                if attempt == 2:
                    log(f"cookie db copy failed: {exc}", "WARN")
                    return ""
                time.sleep(1.0)
        port = _free_port()
        proc = subprocess.Popen(
            [
                browser, "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", f"--user-data-dir={tmp}",
                f"--remote-debugging-port={port}", "--remote-allow-origins=*", "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ready_until = time.time() + 20
        ready = False
        while time.time() < ready_until:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
                ready = True
                break
            except Exception:
                time.sleep(0.3)
        if not ready:
            return ""
        cookies = _cdp_all_cookies(port, timeout=6)
        if _has_login_cookie(cookies):
            return _cookie_string_from(cookies)
        return ""
    except Exception as exc:
        log(f"cookie import from {user_data.parent.parent.name} failed: {exc}", "WARN")
        return ""
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=6)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _cleanup_dir_async(tmp)


def import_browser_cookie() -> tuple[bool, str]:
    """Auto-detect an existing douyin login in the local Edge/Chrome profile (copy-only, non-destructive)."""
    if os.name != "nt":
        return False, "仅支持 Windows"
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        ("Edge", local / "Microsoft" / "Edge" / "User Data"),
        ("Chrome", local / "Google" / "Chrome" / "User Data"),
    ]
    tried = []
    for label, user_data in candidates:
        if not user_data.exists():
            continue
        tried.append(label)
        cookie = _import_from_profile(user_data)
        if cookie:
            save_login_cookie(cookie, "browser-import")
            _login_state(phase="idle", message=f"已从本机 {label} 读取登录 Cookie")
            return True, f"已从本机 {label} 读取到抖音登录 Cookie"
    if tried:
        return (
            False,
            f"未能从本机 {'/'.join(tried)} 读取到有效的抖音登录态（可能未登录抖音，或浏览器新版加密不支持自动提取）。"
            "推荐使用「一键扫码登录」，或手动粘贴 Cookie",
        )
    return False, "本机未找到 Edge/Chrome 的用户数据，请使用扫码登录或手动粘贴 Cookie"


# ---------------------------------------------------------------------------
# Persistent application state
# ---------------------------------------------------------------------------

def coerce_setting_value(key: str, value):
    """Repair a persisted value so a malformed config cannot crash startup."""
    default = DEFAULT_SETTINGS.get(key)
    if isinstance(default, bool):
        return value if isinstance(value, bool) else default
    if isinstance(default, int):
        number = safe_int(value, default)
        if key == "max_concurrent":
            return max(1, min(10, number))
        if key in ("port", "api_port"):
            return number if 1 <= number <= 65535 else default
        return number
    if isinstance(default, str):
        if key == "download_dir":
            text = str(value).strip() if isinstance(value, str) else ""
            return text or default
        if key == "filename_template":
            return valid_filename_template(value, default)
        return value if isinstance(value, str) else default
    return default


def validate_setting_update(key: str, value):
    """Return (valid, value) for a settings write coming from the UI."""
    if key not in DEFAULT_SETTINGS:
        return False, None
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        return (True, value) if isinstance(value, bool) else (False, None)
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return False, None
        number = safe_int(value, None)
        if number is None:
            return False, None
        if key == "max_concurrent":
            number = max(1, min(10, number))
        elif key in ("port", "api_port") and not (1 <= number <= 65535):
            return False, None
        return True, number
    if isinstance(default, str):
        if not isinstance(value, str):
            return False, None
        if key == "download_dir" and not value.strip():
            return False, None
        if key == "filename_template" and (
            not value.strip() or any(ch in value for ch in ("/", "\\", ":", "\x00"))
        ):
            return False, None
        return True, value
    return False, None


SETTINGS: dict = {}
with LOCK:
    _loaded_settings = load_json_dict(SETTINGS_FILE)
    SETTINGS = {**DEFAULT_SETTINGS, **_loaded_settings}
    for _key in DEFAULT_SETTINGS:
        SETTINGS[_key] = coerce_setting_value(_key, SETTINGS[_key])
    try:
        Path(SETTINGS["download_dir"]).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log(f"无法创建下载目录，已回退到默认目录：{exc}", "WARN")
        SETTINGS["download_dir"] = DEFAULT_SETTINGS["download_dir"]
        Path(SETTINGS["download_dir"]).mkdir(parents=True, exist_ok=True)
    save_json(SETTINGS_FILE, SETTINGS)

TASKS: dict[str, dict] = {}
CONTROLS: dict[str, dict] = {}


def _task_entries(data):
    if isinstance(data, dict):
        return data.items()
    if isinstance(data, list):
        return [(str(item.get("id") or index), item) for index, item in enumerate(data) if isinstance(item, dict)]
    return []


_loaded_tasks = load_json(TASKS_FILE, {})
if isinstance(_loaded_tasks, (dict, list)):
    _task_items = _task_entries(_loaded_tasks)
else:
    log(f"{TASKS_FILE.name} 格式异常（应为对象或数组），已按空任务处理", "WARN")
    _task_items = []
for _id, _task in _task_items:
    if not isinstance(_task, dict):
        log(f"跳过损坏的任务记录：{_id}", "WARN")
        continue
    try:
        if _task.get("status") in ("running", "queued", "paused"):
            _task["status"] = "queued"
            _task["message"] = "Restored after restart"
        TASKS[str(_id)] = _task
    except Exception as exc:
        log(f"跳过损坏的任务记录 {_id}：{exc}", "WARN")

HISTORY: list[dict] = []
_loaded_history = load_json(HISTORY_FILE, [])
if isinstance(_loaded_history, list):
    HISTORY = [item for item in _loaded_history if isinstance(item, dict)]
elif isinstance(_loaded_history, dict):
    HISTORY = [item for item in _loaded_history.values() if isinstance(item, dict)]


def persist_tasks() -> None:
    with LOCK:
        save_json(TASKS_FILE, TASKS)


def persist_history() -> None:
    with LOCK:
        save_json(HISTORY_FILE, HISTORY[-500:])


# ---------------------------------------------------------------------------
# DouK engine management
# ---------------------------------------------------------------------------

ENGINE_PROC: subprocess.Popen | None = None
ENGINE_LOCK = threading.Lock()


def _drain_engine(proc: subprocess.Popen) -> None:
    try:
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip()
            if line:
                log(f"engine: {line}", "ENGINE")
    except Exception:
        pass


def start_engine() -> tuple[bool, str]:
    global ENGINE_PROC
    with ENGINE_LOCK:
        if api_alive():
            return True, "引擎已在运行"
        if not ORIGINAL_EXE.exists():
            return False, f"找不到原工具：{ORIGINAL_EXE}"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            ENGINE_PROC = subprocess.Popen(
                [str(ORIGINAL_EXE)],
                cwd=str(ROOT),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
                creationflags=creationflags,
            )
            if ENGINE_PROC.stdin:
                ENGINE_PROC.stdin.write("7\n")
                ENGINE_PROC.stdin.flush()
            threading.Thread(target=_drain_engine, args=(ENGINE_PROC,), daemon=True).start()
        except Exception as exc:
            return False, f"启动失败：{exc}"
        deadline = time.time() + 75
        while time.time() < deadline:
            if api_alive():
                log("DouK API engine started")
                return True, "引擎已启动"
            if ENGINE_PROC.poll() is not None:
                return False, f"引擎进程已退出，代码 {ENGINE_PROC.returncode}"
            time.sleep(0.5)
        return False, "引擎启动超时，请查看日志"


def stop_engine() -> tuple[bool, str]:
    global ENGINE_PROC
    with ENGINE_LOCK:
        proc = ENGINE_PROC
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            ENGINE_PROC = None
            log("DouK API engine stopped")
            return True, "引擎已停止"
        ENGINE_PROC = None
        return True, "引擎未由本程序启动，无需停止"


# ---------------------------------------------------------------------------
# Original tool console actions
# ---------------------------------------------------------------------------

ORIGINAL_ACTIONS = {
    "cookie_douyin_clipboard": ("1", True, "从剪贴板提取抖音 Cookie"),
    "cookie_douyin_browser": ("2", True, "从浏览器提取抖音 Cookie"),
    "cookie_tiktok_clipboard": ("3", True, "从剪贴板提取 TikTok Cookie"),
    "cookie_tiktok_browser": ("4", True, "从浏览器提取 TikTok Cookie"),
    "terminal": ("5", False, "终端交互模式"),
    "monitor": ("6", False, "后台监听模式"),
    "webui": ("8", False, "Web UI 模式"),
    "history_disable": ("9", False, "禁用作品下载记录"),
    "history_delete": ("10", False, "删除作品下载记录"),
    "log_enable": ("11", False, "启用运行日志记录"),
    "update_check": ("12", False, "检查程序版本更新"),
    "language_toggle": ("13", False, "切换原工具界面语言"),
}


def backup_original_settings() -> str:
    backup = ORIGINAL_SETTINGS.with_name("settings.json.bak-doukstudio")
    if ORIGINAL_SETTINGS.exists() and not backup.exists():
        backup.write_text(
            ORIGINAL_SETTINGS.read_text(encoding="utf-8-sig"), encoding="utf-8"
        )
    return str(backup)


def run_original_action(action: str) -> dict:
    """Drive the original console menu, or open an interactive console window."""
    info = ORIGINAL_ACTIONS.get(action)
    if not info:
        return {"ok": False, "message": "未知的原工具功能"}
    choice, needs_confirm, label = info
    if not ORIGINAL_EXE.exists():
        return {"ok": False, "message": f"找不到原工具：{ORIGINAL_EXE}"}

    with ORIGINAL_CLI_LOCK:
        # Interactive modes need a real console window for the user to type in.
        if action in ("terminal", "monitor", "webui"):
            try:
                if os.name == "nt":
                    subprocess.Popen(
                        [str(ORIGINAL_EXE)],
                        cwd=str(ROOT),
                        creationflags=subprocess.CREATE_NEW_CONSOLE,
                    )
                else:
                    subprocess.Popen([str(ORIGINAL_EXE)], cwd=str(ROOT))
                return {
                    "ok": True,
                    "message": f"已打开原工具窗口，请在窗口中输入 {choice} 进入「{label}」",
                }
            except Exception as exc:
                return {"ok": False, "message": f"打开原工具失败：{exc}"}

        backup_original_settings()
        payload = f"{choice}\n" + ("\n" if needs_confirm else "")
        try:
            proc = subprocess.run(
                [str(ORIGINAL_EXE)],
                input=payload,
                capture_output=True,
                text=True,
                encoding="gbk",
                errors="ignore",
                timeout=150,
                cwd=str(ROOT),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            output = (proc.stdout or "")[-2400:]
            return {
                "ok": proc.returncode == 0,
                "message": f"「{label}」已执行",
                "output": output,
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": f"「{label}」执行超时，请查看原工具窗口", "output": ""}
        except Exception as exc:
            return {"ok": False, "message": f"「{label}」执行失败：{exc}", "output": ""}


# ---------------------------------------------------------------------------
# Media normalization
# ---------------------------------------------------------------------------

def collect_urls(obj, out: dict | None = None, depth: int = 0) -> dict:
    out = out if out is not None else {}
    if depth > 6:
        return out
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = str(key).lower()
            if isinstance(value, str) and value.startswith("http") and any(
                tag in k for tag in ("url", "src", "link", "play", "cover", "image", "thumb", "music")
            ):
                out.setdefault(k, value)
            elif isinstance(value, (dict, list)):
                collect_urls(value, out, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:40]:
            collect_urls(item, out, depth + 1)
    return out


def pick(data: dict, *keys, default=""):
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def normalize_media(data, source: str = "douk") -> list[dict]:
    if data is None:
        return []
    candidates: list = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                candidates.append(item)
    elif isinstance(data, dict):
        for key in ("detail", "data", "aweme_detail", "aweme_list", "list", "works", "items", "result"):
            val = data.get(key)
            if isinstance(val, list):
                candidates.extend([x for x in val if isinstance(x, dict)])
            elif isinstance(val, dict):
                candidates.append(val)
        if not candidates:
            candidates.append(data)

    items: list[dict] = []
    seen: set[str] = set()
    for raw in candidates:
        urls = collect_urls(raw)
        media_id = str(
            pick(
                raw,
                "aweme_id",
                "detail_id",
                "id",
                "mix_id",
                "sec_user_id",
                "comment_id",
                "room_id",
                default="",
            )
        )
        title = str(pick(raw, "desc", "title", "nickname", "text", "content", default=""))
        author = str(pick(raw, "nickname", "author", "uploader", "user_name", default=""))
        cover = str(
            pick(
                raw,
                "cover_url",
                "cover",
                "thumbnail",
                "thumb_url",
                "dynamic_cover",
                default=urls.get("cover_url") or urls.get("cover") or urls.get("thumbnail") or "",
            )
        )
        video_url = clean_media_url(
            str(
                pick(
                    raw,
                    "video_url",
                    "download_url",
                    "play_url",
                    "url",
                    default=urls.get("video_url") or urls.get("play_url") or urls.get("url") or "",
                )
            )
        )
        audio_url = str(
            pick(raw, "music_url", "audio_url", default=urls.get("music_url") or urls.get("audio_url") or "")
        )
        images = raw.get("image_urls") or raw.get("images") or []
        if not isinstance(images, list):
            images = [images]
        images = [clean_media_url(x) for x in images if isinstance(x, str) and x.startswith("http")]
        cover = clean_media_url(cover)
        if not images and cover:
            images = [cover]
        dedup_key = media_id or video_url
        if not dedup_key or dedup_key in seen:
            continue
        seen.add(dedup_key)
        if not (title or author or video_url or cover or images):
            continue
        video_raw = raw.get("video") if isinstance(raw, dict) else None
        if not isinstance(video_raw, dict):
            nested = raw.get("aweme_detail") if isinstance(raw, dict) else None
            video_raw = nested.get("video") if isinstance(nested, dict) else raw
        formats = quality_formats_from_video(video_raw)
        for fmt in formats:
            fmt["url"] = clean_media_url(fmt.get("url"))
        if not video_url and formats:
            video_url = formats[0].get("url") or ""
        if video_url and not any(fmt.get("url") == video_url for fmt in formats):
            formats.insert(0, {
                "id": "default",
                "label": "默认画质",
                "height": 0,
                "width": 0,
                "bitrate": 0,
                "size": 0,
                "codec": "默认",
                "format": "mp4",
                "url": video_url,
            })
        items.append(
            {
                "id": media_id or str(len(items) + 1),
                "title": title[:180] or f"作品 {media_id or len(items) + 1}",
                "author": author,
                "cover": cover,
                "duration": pick(raw, "duration", "video_duration", default=0),
                "video_url": video_url,
                "audio_url": audio_url,
                "images": images,
                "formats": formats,
                "source": source,
                "raw": raw,
            }
        )
    return items


# ---------------------------------------------------------------------------
# Caption (文案) extraction and export
# ---------------------------------------------------------------------------

def _dedupe_tags(tags: list[str]) -> list[str]:
    result = []
    seen = set()
    for tag in tags:
        value = str(tag or "").strip().lstrip("#")
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def aweme_media_url(raw: dict) -> str:
    """Best direct media URL from a raw aweme detail (for ASR / narration)."""
    video = raw.get("video") if isinstance(raw.get("video"), dict) else {}
    for key in ("play_addr", "play_addr_h264", "download_addr", "play_addr_265"):
        urls = (video.get(key) or {}).get("url_list") or []
        for url in urls:
            if isinstance(url, str) and url.startswith("http"):
                return url
    return ""


def caption_from_item(item: dict) -> dict:
    """Convert a normalized media item into a clean, exportable caption row."""
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    author_raw = raw.get("author") if isinstance(raw.get("author"), dict) else {}

    text = str(
        raw.get("desc")
        or raw.get("description")
        or raw.get("title")
        or item.get("title")
        or ""
    ).strip()

    tags = []
    for extra in raw.get("text_extra") or []:
        if isinstance(extra, dict) and extra.get("hashtag_name"):
            tags.append(str(extra["hashtag_name"]))
    if not tags:
        tags = re.findall(r"#([^\s#]+)", text)
    tags = _dedupe_tags(tags)

    author = str(item.get("author") or author_raw.get("nickname") or raw.get("author_name") or "")
    author_id = str(
        raw.get("author_sec_uid")
        or author_raw.get("sec_uid")
        or raw.get("sec_user_id")
        or ""
    )

    created = ""
    ts = raw.get("create_time") or raw.get("create_at") or raw.get("timestamp") or 0
    if ts:
        try:
            created = datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            created = ""

    source = str(item.get("source") or "")
    item_id = str(item.get("id") or "")
    source_url = str(raw.get("source_url") or raw.get("webpage_url") or "")
    if not source_url and item_id:
        if source in ("douk", "douyin"):
            source_url = f"https://www.douyin.com/video/{item_id}"
        elif source == "tiktok":
            source_url = f"https://www.tiktok.com/video/{item_id}"

    platform = "douyin" if source in ("douk", "douyin") else ("tiktok" if source == "tiktok" else "ytdlp")
    return {
        "id": item_id,
        "text": text,
        "hashtags": tags,
        "author": author,
        "author_id": author_id,
        "create_time": created,
        "source_url": source_url,
        "platform": platform,
        "duration": item.get("duration") or 0,
        "media_url": str(item.get("video_url") or aweme_media_url(raw) or ""),
        "narration": str(item.get("narration") or ""),
        "narration_state": str(item.get("narration_state") or ""),
    }


def clean_caption_records(records: list) -> list[dict]:
    cleaned = []
    seen = set()
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        row = {
            "id": str(rec.get("id") or ""),
            "text": str(rec.get("text") or "").strip(),
            "hashtags": _dedupe_tags(rec.get("hashtags") or []),
            "author": str(rec.get("author") or ""),
            "author_id": str(rec.get("author_id") or ""),
            "create_time": str(rec.get("create_time") or ""),
            "source_url": str(rec.get("source_url") or ""),
            "platform": str(rec.get("platform") or ""),
            "duration": rec.get("duration") or 0,
            "media_url": str(rec.get("media_url") or ""),
            "narration": str(rec.get("narration") or ""),
            "narration_state": str(rec.get("narration_state") or ""),
        }
        if not row["text"] and not row["narration"]:
            continue
        key = row["id"] or row["text"][:120]
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(row)
    cleaned.sort(key=lambda r: r.get("create_time") or "", reverse=True)
    return cleaned


def export_captions(records: list, fmt: str = "txt", name: str = "") -> dict:
    rows = clean_caption_records(records)
    if not rows:
        return {"ok": False, "error": "没有可导出的文案"}
    fmt = (fmt or "txt").lower()
    if fmt not in ("txt", "csv", "json", "md"):
        return {"ok": False, "error": f"不支持的格式：{fmt}"}

    directory = Path(SETTINGS["download_dir"]) / "文案导出"
    directory.mkdir(parents=True, exist_ok=True)
    author_hint = name or next((r["author"] for r in rows if r.get("author")), "")
    stem = safe_name(f"{author_hint or '文案'}-{len(rows)}条-{datetime.now():%Y%m%d-%H%M%S}", 90)
    path = directory / f"{stem}.{fmt}"

    if fmt == "json":
        content = json.dumps(rows, ensure_ascii=False, indent=2)
        path.write_text(content, encoding="utf-8")
    elif fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["序号", "作品ID", "作者", "发布时间", "文案", "解说文案", "话题标签", "链接", "平台"])
        for i, r in enumerate(rows, 1):
            writer.writerow([
                i, r["id"], r["author"], r["create_time"], r["text"],
                r.get("narration") or "",
                " ".join(f"#{t}" for t in r["hashtags"]), r["source_url"], r["platform"],
            ])
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            fh.write(buf.getvalue())
    else:
        has_narration = any(r.get("narration") for r in rows)
        lines = [
            "DouK Studio · 文案导出",
            f"作者：{author_hint or '未知'}",
            f"导出时间：{now()}",
            f"共 {len(rows)} 条" + ("（含解说文案）" if has_narration else ""),
            "",
        ]
        if fmt == "txt":
            for i, r in enumerate(rows, 1):
                header = "─" * 40
                lines += [
                    header,
                    f"[{i:02d}] {r['create_time']}" + (f" · {r['author']}" if r["author"] else ""),
                ]
                if r["text"]:
                    lines += [f"标题文案：{r['text']}"]
                    if r["hashtags"]:
                        lines.append(" ".join(f"#{t}" for t in r["hashtags"]))
                narration = r.get("narration") or ""
                if narration:
                    lines += ["解说文案：", narration]
                elif has_narration and r.get("narration_state") == "no-speech":
                    lines.append("解说文案：（未识别到口播语音）")
                if r["source_url"]:
                    lines.append(f"链接：{r['source_url']}")
                lines.append("")
        else:
            lines = ["# DouK Studio · 文案导出", "", f"> 作者：{author_hint or '未知'} · 共 {len(rows)} 条 · 导出时间：{now()}", ""]
            for i, r in enumerate(rows, 1):
                lines += [f"## {i}. {r['create_time'] or '未知时间'}", ""]
                if r["text"]:
                    lines += [r["text"], ""]
                    if r["hashtags"]:
                        lines.append(" ".join(f"`#{t}`" for t in r["hashtags"]))
                        lines.append("")
                narration = r.get("narration") or ""
                if narration:
                    lines += ["**解说文案**", "", narration, ""]
                if r["source_url"]:
                    lines += [f"[打开原作品]({r['source_url']})", ""]
        path.write_text("\n".join(lines), encoding="utf-8")

    log(f"captions exported: {path.name} ({len(rows)} rows)")
    return {
        "ok": True,
        "file": path.name,
        "path": str(path),
        "directory": str(directory),
        "count": len(rows),
        "format": fmt,
    }


# ---------------------------------------------------------------------------
# Narration extraction (ASR via faster-whisper inside the project venv)
# ---------------------------------------------------------------------------

ASR_TMP = DATA / "asr-tmp"
ASR_WORKER = BASE / "asr_worker.py"
ASR_VENV_PY = BASE / ".venv" / "Scripts" / "python.exe"
ASR_MAX_BATCH = 30
ASR_PER_FILE_TIMEOUT = 600


def asr_available() -> bool:
    return ASR_WORKER.is_file() and ASR_VENV_PY.is_file()


def find_local_video(item_id: str) -> str:
    """Locate an already downloaded file for this aweme id (avoids re-download)."""
    item_id = str(item_id or "").strip()
    if not item_id:
        return ""
    marker = f"[{item_id}]"
    directories = [Path(SETTINGS.get("download_dir") or ""), BASE / "Downloads"]
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.suffix.lower() in (".mp4", ".m4a", ".mp3", ".webm", ".mkv", ".flac", ".wav") and marker in path.name:
                return str(path)
    return ""


def _fetch_media_for_asr(url: str, item_id: str) -> str:
    """Download a media file to a temp location for transcription."""
    if not url or not url.startswith("http"):
        return ""
    ASR_TMP.mkdir(parents=True, exist_ok=True)
    suffix = ".mp4"
    dest = ASR_TMP / f"{item_id or 'clip'}-{int(time.time())}{suffix}"
    headers = browser_headers(url)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
            total = 0
            while True:
                chunk = resp.read(128 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                if total > 200 * 1024 * 1024:
                    raise RuntimeError("文件过大，取消转写下载")
        if dest.stat().st_size < 8 * 1024:
            dest.unlink(missing_ok=True)
            return ""
        return str(dest)
    except Exception as exc:
        log(f"ASR media download failed ({item_id}): {exc}", "WARN")
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return ""


def _resolve_asr_source(rec: dict) -> tuple[str, str]:
    """Return (path, kind): kind = local / downloaded / media-url / browser / none."""
    item_id = str(rec.get("id") or "")
    local = find_local_video(item_id)
    if local:
        return local, "local"
    media_url = str(rec.get("media_url") or "")
    if not media_url and rec.get("platform") == "douyin":
        source_url = str(rec.get("source_url") or "")
        if "/video/" in source_url or "/note/" in source_url:
            detail = browser_fetch_douyin_detail(source_url, wait_seconds=20)
            if detail:
                media_url = aweme_media_url(detail)
    if media_url:
        path = _fetch_media_for_asr(media_url, item_id)
        if path:
            return path, "downloaded"
    return "", "none"


def run_asr(files: list, model_name: str = "small") -> list:
    """Transcribe files with one worker call (model loads once per batch)."""
    if not files:
        return []
    if not asr_available():
        return [{"ok": False, "error": "ASR 组件缺失（.venv 或 asr_worker.py 不存在）"} for _ in files]
    payload = {"files": files, "model": model_name, "language": "zh"}
    timeout = 60 + ASR_PER_FILE_TIMEOUT * len(files)
    try:
        proc = subprocess.run(
            [str(ASR_VENV_PY), str(ASR_WORKER)],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            timeout=timeout,
            cwd=str(BASE),
        )
        out = json.loads(proc.stdout.decode("utf-8", "ignore") or "[]")
        if isinstance(out, list) and out:
            return out
        err = proc.stderr.decode("utf-8", "ignore")[-400:] or "无输出"
        log(f"ASR worker bad output: {err}", "WARN")
        return [{"ok": False, "error": f"ASR worker 输出异常：{err}"} for _ in files]
    except Exception as exc:
        log(f"ASR worker failed: {exc}", "WARN")
        return [{"ok": False, "error": str(exc)} for _ in files]


def extract_narrations(records: list, model_name: str = "small") -> dict:
    """Attach narration (ASR text) to caption records, in place."""
    records = [r for r in records if isinstance(r, dict)][:ASR_MAX_BATCH]
    if not records:
        return {"ok": False, "error": "没有可提取的文案记录"}

    plan: list[tuple[dict, str]] = []
    for rec in records:
        if rec.get("narration_state") == "ok" and rec.get("narration"):
            continue  # already extracted
        path, kind = _resolve_asr_source(rec)
        plan.append((rec, path, kind))

    to_transcribe = [(rec, path) for rec, path, kind in plan if path]
    missing = [rec for rec, path, kind in plan if not path]
    log(f"ASR batch: {len(to_transcribe)} files to transcribe, {len(missing)} unresolved")

    results = run_asr([path for _, path in to_transcribe], model_name)
    by_file = {}
    for res in results:
        by_file[res.get("file")] = res

    ok_count = no_speech = failed = 0
    for rec, path in to_transcribe:
        res = by_file.get(path) or {}
        rec["narration"] = str(res.get("text") or "")
        if res.get("ok") and rec["narration"]:
            rec["narration_state"] = "ok"
            ok_count += 1
        elif res.get("ok"):
            rec["narration_state"] = "no-speech"
            no_speech += 1
        else:
            rec["narration_state"] = "failed"
            rec["narration_error"] = str(res.get("error") or "转写失败")
            failed += 1
    for rec in missing:
        rec["narration"] = ""
        rec["narration_state"] = "failed"
        rec["narration_error"] = "无法获取视频文件（未下载且无法解析直链）"
        failed += 1

    return {
        "ok": ok_count + no_speech > 0,
        "records": records,
        "ok_count": ok_count,
        "no_speech": no_speech,
        "failed": failed,
        "total": len(records),
    }


def fetch_account_captions(
    sec_uid: str,
    platform: str = "douyin",
    count: int = 100,
    tab: str = "post",
    export: bool = False,
    name: str = "",
) -> dict:
    sec_uid = str(sec_uid or "").strip()
    if not sec_uid:
        return {"ok": False, "error": "缺少 sec_uid", "records": []}
    if platform != "douyin":
        return {"ok": False, "error": "TikTok 主页文案请使用链接解析模式", "records": []}

    limit = max(1, min(500, int(count or 100)))
    awemes: list[dict] = []
    if api_alive() or start_engine()[0]:
        try:
            resp = http_request(
                f"{api_base()}/douyin/account",
                "POST",
                {"sec_user_id": sec_uid, "tab": tab, "count": max(limit, 18), "source": False},
                timeout=120,
            )
            awemes = [
                item["raw"] for item in normalize_media(resp.get("data"), "douyin")
                if isinstance(item.get("raw"), dict)
            ]
        except Exception as exc:
            log(f"DouK account captions failed: {exc}", "WARN")

    fallback = False
    if not awemes:
        awemes = browser_fetch_account_awemes(sec_uid, max_count=limit)
        fallback = True

    records = []
    for aweme in awemes:
        aweme_id = str(aweme.get("aweme_id") or "")
        detail_url = f"https://www.douyin.com/video/{aweme_id}" if aweme_id else f"https://www.douyin.com/user/{sec_uid}"
        item = detail_to_media(aweme, detail_url)
        if item.get("raw", {}).get("desc") or item.get("title"):
            records.append(caption_from_item(item))
    records = clean_caption_records(records)[:limit]
    if not records:
        return {
            "ok": False,
            "error": "未获取到主页作品文案；请确认 sec_uid 正确，或稍后重试",
            "records": [],
            "fallback": fallback,
        }

    author = next((r["author"] for r in records if r.get("author")), "")
    result = {
        "ok": True,
        "records": records,
        "count": len(records),
        "author": author,
        "fallback": fallback,
        "sec_uid": sec_uid,
    }
    if export:
        result["export"] = export_captions(records, "txt", name or author or "主页文案")
    return result


# ---------------------------------------------------------------------------
# URL resolving
# ---------------------------------------------------------------------------

VIDEO_ID_PATTERNS = [
    re.compile(r"/video/(\d+)"),
    re.compile(r"[?&]modal_id=(\d+)"),
    re.compile(r"/note/(\d+)"),
    re.compile(r"[?&]aweme_id=(\d+)"),
    re.compile(r"/v/(\d+)"),
]


def extract_video_id(text: str) -> str:
    for pattern in VIDEO_ID_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    digits = re.findall(r"\d{8,}", text)
    return digits[0] if digits else ""


def extract_sec_uid(text: str) -> str:
    m = re.search(r"[?&]sec_uid=([^&#]+)", text)
    if m:
        return urllib.parse.unquote(m.group(1))
    m = re.search(r"/user/([^/?#]+)", text)
    return urllib.parse.unquote(m.group(1)) if m else ""


def resolve_share(text: str, platform: str) -> str:
    try:
        endpoint = "douyin/share" if platform == "douyin" else "tiktok/share"
        result = http_request(
            f"{api_base()}/{endpoint}", "POST", {"text": text}, timeout=25
        )
        return str(result.get("url") or text)
    except Exception as exc:
        log(f"share resolve failed: {exc}", "WARN")
        return text


def ytdlp_metadata(url: str) -> tuple[list[dict], str]:
    exe = SETTINGS.get("yt_dlp_path") or shutil.which("yt-dlp")
    if not exe:
        return [], "未找到 yt-dlp，请在设置中指定路径"
    args = [exe, "-J", "--no-warnings", "--skip-download", url]
    if SETTINGS.get("proxy"):
        args += ["--proxy", SETTINGS["proxy"]]
    if SETTINGS.get("cookie_file"):
        args += ["--cookies", SETTINGS["cookie_file"]]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except Exception as exc:
        return [], f"yt-dlp 执行失败：{exc}"
    if proc.returncode != 0:
        return [], (proc.stderr or proc.stdout or "yt-dlp 解析失败").strip()[-1200:]
    try:
        info = json.loads(proc.stdout)
    except Exception as exc:
        return [], f"yt-dlp 输出解析失败：{exc}"
    entries = info.get("entries") if isinstance(info.get("entries"), list) else [info]
    items = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        items.append(
            {
                "id": str(entry.get("id") or ""),
                "title": str(entry.get("title") or entry.get("webpage_title") or "未命名"),
                "author": str(entry.get("uploader") or entry.get("channel") or ""),
                "cover": str(entry.get("thumbnail") or ""),
                "duration": entry.get("duration") or 0,
                "video_url": str(entry.get("webpage_url") or entry.get("url") or url),
                "audio_url": "",
                "images": [],
                "source": "ytdlp",
                "raw": {
                    "id": entry.get("id"),
                    "ext": entry.get("ext"),
                    "format_id": entry.get("format_id"),
                    "webpage_url": entry.get("webpage_url") or url,
                    "playlist": info.get("_type") == "playlist",
                    "playlist_index": entry.get("playlist_index"),
                },
            }
        )
    return items, ""


def resolve_url(text: str, engine: str = "auto") -> dict:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "请输入链接", "items": []}

    # The input is often a whole share sentence, e.g. "8.01 ... https://v.douyin.com/xxx/ 复制此链接...".
    match = re.search(r"https?://[^\s]+", text)
    link = match.group(0).rstrip("，。！？；、,.;!?)]】") if match else text
    lower = link.lower()
    is_douyin = "douyin.com" in lower or "iesdouyin.com" in lower
    is_tiktok = "tiktok.com" in lower
    platform = "douyin" if is_douyin else ("tiktok" if is_tiktok else "")

    if platform and (engine in ("auto", "douk")):
        if not api_alive():
            started, msg = start_engine()
            if not started:
                return {"ok": False, "error": msg, "items": []}
        short_domains = ("v.douyin.com", "vm.tiktok.com", "vt.tiktok.com")
        full = resolve_share(link, platform) if any(d in lower for d in short_domains) else link
        item_id = extract_video_id(full)
        sec_uid = extract_sec_uid(full)
        if item_id:
            endpoint = f"{platform}/detail"
            payload = {"detail_id": item_id, "source": False}
            items: list = []
            resp: dict = {}
            try:
                resp = http_request(f"{api_base()}/{endpoint}", "POST", payload, timeout=90)
                if isinstance(resp, dict):
                    items = normalize_media(resp.get("data"), platform)
            except Exception as exc:
                log(f"DouK detail request failed: {exc}", "WARN")
                resp = {}
            if items:
                return {"ok": True, "items": items, "source": platform, "raw": resp}
            # DouK 5.7's signature algorithm is outdated against current Douyin (403/500).
            # Fall back to reading the signed response from a temporary headless browser.
            log("DouK detail empty, trying browser fallback", "WARN")
            detail = browser_fetch_douyin_detail(full)
            if detail:
                item = detail_to_media(detail, full)
                if item.get("video_url") or item.get("images"):
                    return {
                        "ok": True,
                        "items": [item],
                        "source": "douk",
                        "fallback": "browser-detail",
                        "raw": {"message": "Browser fallback succeeded", "aweme_id": item["id"]},
                    }
            return {
                "ok": False,
                "error": (resp.get("message") if isinstance(resp, dict) else "") or "未获取到作品数据（DouK 接口与浏览器兜底均失败）",
                "items": [],
                "raw": resp,
            }
        if sec_uid:
            return {
                "ok": True,
                "source": platform,
                "account": {"sec_user_id": sec_uid, "url": full},
                "items": [],
                "message": "已识别账号主页，可切换到「账号采集」继续",
            }

    items, error = ytdlp_metadata(link)
    if items:
        return {"ok": True, "items": items, "source": "ytdlp"}
    return {"ok": False, "error": error or "无法解析该链接", "items": []}


# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

TASK_SEMAPHORE = threading.Semaphore(max(1, safe_int(SETTINGS.get("max_concurrent", 3), 3)))
EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="douktask")
ACTIVE_TASKS = 0
PERSIST_STATE: dict[str, tuple[float, float]] = {}


def rebuild_semaphore(limit: int) -> None:
    """Apply the concurrency limit changed in the settings page without restart."""
    global TASK_SEMAPHORE
    limit = max(1, min(10, safe_int(limit, 3)))
    if limit == getattr(rebuild_semaphore, "last", 0):
        return
    if ACTIVE_TASKS > 0:
        log(f"有 {ACTIVE_TASKS} 个任务在执行，并发上限调整已延后", "WARN")
        return
    TASK_SEMAPHORE = threading.Semaphore(limit)
    rebuild_semaphore.last = limit


def update_task(task_id: str, **changes) -> None:
    with LOCK:
        task = TASKS.get(task_id)
        if task is None:
            return
        task.update(changes)
        terminal = changes.get("status") in ("completed", "failed", "canceled", "paused")
        if terminal:
            task["finished_at"] = now()
        progress = safe_int(task.get("progress"), 0)
        last_time, last_progress = PERSIST_STATE.get(task_id, (0.0, -1))
        current = time.time()
        if terminal or current - last_time >= 0.5 or abs(progress - last_progress) >= 1:
            persist_tasks()
            PERSIST_STATE[task_id] = (current, progress)


def task_control(task_id: str) -> dict:
    with LOCK:
        if task_id not in CONTROLS:
            CONTROLS[task_id] = {
                "cancel": threading.Event(),
                "pause": threading.Event(),
                "proc": None,
                "future": None,
                "active": False,
                "generation": 0,
            }
        return CONTROLS[task_id]


def submit_task(task_id: str):
    """Track the executor future so resume/retry cannot start a second worker."""
    with LOCK:
        control = task_control(task_id)
        future = EXECUTOR.submit(execute_task, task_id)
        control["future"] = future
        return future


def wait_if_paused(task_id: str) -> bool:
    control = task_control(task_id)
    while control["pause"].is_set():
        if control["cancel"].is_set():
            return False
        time.sleep(0.2)
    return not control["cancel"].is_set()


def direct_download(task: dict, url: str, destination: Path, final: bool = True) -> None:
    task_id = task["id"]
    control = task_control(task_id)
    original = read_original_settings()
    headers = browser_headers(url, original)
    part = destination.with_name(destination.name + ".part")
    existing = part.stat().st_size if part.exists() else 0

    def open_response():
        req_headers = dict(headers)
        if existing:
            req_headers["Range"] = f"bytes={existing}-"
        req = urllib.request.Request(url, headers=req_headers)
        return urllib.request.urlopen(req, timeout=60)

    update_task(task_id, status="running", message="Connecting...", downloaded=existing)
    try:
        response = open_response()
    except urllib.error.HTTPError as exc:
        if exc.code != 416 or not existing:
            raise
        log(f"range 无效，删除 .part 后重新下载：{part.name}", "WARN")
        try:
            part.unlink()
        except OSError:
            pass
        existing = 0
        headers.pop("Range", None)
        response = open_response()
    with response:
        total_header = response.headers.get("Content-Length")
        total = existing + safe_int(total_header)
        mode = "ab" if existing and getattr(response, "status", 200) == 206 else "wb"
        if mode == "wb":
            existing = 0
        update_task(task_id, total=total, downloaded=existing)
        last = time.time()
        chunk_size = 64 * 1024
        with part.open(mode) as fh:
            while True:
                if control["cancel"].is_set():
                    update_task(task_id, status="canceled", message="已取消")
                    return
                if not wait_if_paused(task_id):
                    update_task(task_id, status="canceled", message="已取消")
                    return
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                existing += len(chunk)
                elapsed = max(time.time() - last, 0.001)
                speed = len(chunk) / elapsed
                last = time.time()
                progress = min(99, int(existing * 100 / total)) if total else 0
                update_task(
                    task_id,
                    downloaded=existing,
                    total=total,
                    speed=speed,
                    progress=progress,
                    message=f"{human_size(existing)} / {human_size(total)}",
                )
    if part.exists():
        part.replace(destination)
    if final:
        update_task(
            task_id,
            status="completed",
            progress=100,
            downloaded=existing,
            total=total,
            speed=0,
            file=str(destination),
            message="下载完成",
        )
        add_history(task, str(destination))
    else:
        update_task(task_id, message=f"已保存 {destination.name}")


def build_ytdlp_args(task: dict, output_pattern: Path) -> list[str]:
    exe = SETTINGS.get("yt_dlp_path") or shutil.which("yt-dlp")
    if not exe:
        raise RuntimeError("未找到 yt-dlp，请在设置中指定路径")
    kind = task.get("kind", "video")
    args = [exe, "--no-warnings", "--newline", "-o", str(output_pattern)]
    ffmpeg = SETTINGS.get("ffmpeg_path") or shutil.which("ffmpeg")
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    if SETTINGS.get("proxy"):
        args += ["--proxy", SETTINGS["proxy"]]
    if SETTINGS.get("cookie_file"):
        args += ["--cookies", SETTINGS["cookie_file"]]
    elif "douyin.com" in str(task.get("source_url") or "") and login_cookie_str():
        args += ["--add-headers", f"Cookie: {login_cookie_str()}"]
    if kind == "audio":
        args += ["-x", "--audio-format", SETTINGS.get("audio_format", "mp3"), "--audio-quality", "0"]
    elif kind == "thumbnail":
        args += ["--skip-download", "--write-thumbnail", "--convert-thumbnails", "jpg"]
    else:
        fmt = task.get("format") or SETTINGS.get("default_quality") or "bestvideo+bestaudio/best"
        args += ["-f", fmt]
        if SETTINGS.get("write_subtitles"):
            args += ["--write-subs", "--write-auto-subs", "--sub-langs", SETTINGS.get("subtitle_langs", "zh-Hans,en")]
            if SETTINGS.get("embed_subtitles"):
                args.append("--embed-subs")
    if SETTINGS.get("write_thumbnail") and kind != "thumbnail":
        args += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    args.append(task.get("source_url", ""))
    return args


PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+([\d.]+)\s*([KMGT]?i?B)"
    r"(?:.*?at\s+([\d.]+)\s*([KMGT]?i?B/s))?.*?ETA\s+(\S+)"
)


def ytdlp_download(task: dict) -> None:
    task_id = task["id"]
    control = task_control(task_id)
    download_dir = Path(SETTINGS["download_dir"])
    download_dir.mkdir(parents=True, exist_ok=True)
    media_id = safe_name(task.get("item", {}).get("id") or task_id, 60)
    output_pattern = download_dir / f"{media_id}.%(ext)s"
    args = build_ytdlp_args(task, output_pattern)
    update_task(task_id, status="running", message="Starting yt-dlp...", progress=1)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        args,
        cwd=str(download_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=creationflags,
    )
    control["proc"] = proc
    tail: list[str] = []
    try:
        for raw_line in iter(proc.stdout.readline, ""):
            line = raw_line.rstrip()
            if not line:
                continue
            tail.append(line)
            if len(tail) > 60:
                tail.pop(0)
            log(f"[{task_id[:6]}] {line}", "YTDLP")
            if control["cancel"].is_set():
                proc.terminate()
                update_task(task_id, status="canceled", message="已取消")
                return
            m = PROGRESS_RE.search(line)
            if m:
                percent = float(m.group(1))
                total = parse_size(f"{m.group(2)} {m.group(3)}")
                speed_txt = f"{m.group(4)} {m.group(5)}" if m.group(4) else ""
                update_task(
                    task_id,
                    progress=min(99, percent),
                    total=total,
                    downloaded=int(total * percent / 100) if total else 0,
                    speed=parse_size(speed_txt),
                    eta=m.group(6),
                    message=f"{percent:.1f}%  {speed_txt}  ETA {m.group(6)}",
                )
            elif "Destination:" in line:
                update_task(task_id, message="开始写入文件...")
        code = proc.wait(timeout=10)
        if code != 0:
            update_task(task_id, status="failed", message="yt-dlp 下载失败", error="\n".join(tail[-12:]))
            return
        base_title = safe_name(task.get("title") or task.get("item", {}).get("title") or media_id)
        template = valid_filename_template(SETTINGS.get("filename_template"))
        final_name = template.replace("{title}", base_title).replace("{id}", media_id)
        candidates = sorted(
            [p for p in download_dir.glob(media_id + ".*") if not p.name.endswith((".part", ".ytdl"))],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            update_task(task_id, status="failed", message="未找到下载文件", error="\n".join(tail[-12:]))
            return
        source_file = candidates[0]
        destination = source_file.with_name(f"{final_name}{source_file.suffix}")
        if destination.exists():
            destination = source_file.with_name(f"{final_name}-{int(time.time())}{source_file.suffix}")
        source_file.replace(destination)
        update_task(
            task_id,
            status="completed",
            progress=100,
            file=str(destination),
            message="下载完成",
            downloaded=destination.stat().st_size,
            total=destination.stat().st_size,
            speed=0,
            eta="",
        )
        add_history(task, str(destination))
    except Exception as exc:
        update_task(task_id, status="failed", message="任务异常", error=f"{exc}\n{traceback.format_exc()[-800:]}")
    finally:
        control["proc"] = None


def add_history(task: dict, file_path: str) -> None:
    try:
        with LOCK:
            HISTORY.insert(
                0,
                {
                    "id": task.get("id", ""),
                    "title": task.get("title", ""),
                    "source_url": task.get("source_url", ""),
                    "engine": task.get("engine", ""),
                    "kind": task.get("kind", ""),
                    "file": file_path,
                    "at": now(),
                },
            )
    except Exception as exc:
        log(f"记录下载历史失败：{exc}", "WARN")
        return
    try:
        persist_history()
    except Exception as exc:
        log(f"保存下载历史失败：{exc}", "WARN")


def task_engine(task: dict) -> str:
    """auto 引擎按来源域名归一化：抖音/TikTok 走 douk，其余走 yt-dlp。"""
    engine = str(task.get("engine") or "auto").strip().lower()
    if engine in ("douk", "douyin", "tiktok"):
        return "douk"
    if engine in ("ytdlp", "yt-dlp", "yt_dlp"):
        return "ytdlp"
    source_url = str(task.get("source_url") or "").lower()
    if any(domain in source_url for domain in ("douyin.com", "iesdouyin.com", "tiktok.com")):
        return "douk"
    return "ytdlp"


def execute_task(task_id: str) -> None:
    global ACTIVE_TASKS
    generation = None
    with LOCK:
        ACTIVE_TASKS += 1
    try:
        with LOCK:
            control = task_control(task_id)
            if control["active"]:
                log(f"任务 {task_id[:8]} 已有执行中的 worker，跳过重复提交", "WARN")
                return
            control["active"] = True
            generation = safe_int(control.get("generation"), 0) + 1
            control["generation"] = generation
        with TASK_SEMAPHORE:
            with LOCK:
                task = TASKS.get(task_id)
                if not task or control["cancel"].is_set():
                    return
            if not wait_if_paused(task_id):
                return
            update_task(task_id, status="running", started_at=now(), message="处理中...")
            try:
                task = TASKS[task_id]
                if task_engine(task) == "douk":
                    kind = task.get("kind", "video")
                    item = task.get("item") if isinstance(task.get("item"), dict) else {}
                    template = valid_filename_template(SETTINGS.get("filename_template"))
                    name = (
                        template.replace("{title}", safe_name(task.get("title", "untitled")))
                        .replace("{id}", safe_name(str(item.get("id", task_id)), 50))
                    )
                    if kind == "gallery":
                        urls = [u for u in (item.get("images") or []) if str(u).startswith("http")]
                        if not urls:
                            raise RuntimeError("该作品没有可用的图集图片")
                        folder = Path(SETTINGS["download_dir"]) / name
                        folder.mkdir(parents=True, exist_ok=True)
                        for index, image_url in enumerate(urls):
                            if not wait_if_paused(task_id):
                                update_task(task_id, status="canceled", message="已取消")
                                return
                            is_last = index == len(urls) - 1
                            target = folder / f"{name}_{index + 1:02d}.jpg"
                            direct_download(task, image_url, target, final=is_last)
                            if not is_last:
                                update_task(
                                    task_id,
                                    status="running",
                                    message=f"图集 {index + 1}/{len(urls)}",
                                )
                        return
                    url = ""
                    if kind == "audio":
                        url = item.get("audio_url") or ""
                    elif kind == "thumbnail":
                        url = item.get("cover") or ""
                    elif kind == "image":
                        url = (item.get("images") or [""])[0]
                    else:
                        url = item.get("video_url") or (item.get("images") or [""])[0]
                        selected_format = str(task.get("format") or "")
                        if selected_format:
                            for fmt in item.get("formats") or []:
                                if not isinstance(fmt, dict):
                                    continue
                                if str(fmt.get("id") or "") == selected_format or str(fmt.get("label") or "") == selected_format:
                                    if fmt.get("url"):
                                        url = fmt["url"]
                                    break
                    if not url:
                        raise RuntimeError("该作品没有可用的直链，可尝试用 yt-dlp 引擎")
                    ext = "jpg" if kind in ("thumbnail", "image") else ("mp3" if kind == "audio" else "mp4")
                    dest = Path(SETTINGS["download_dir"]) / f"{name}.{ext}"
                    direct_download(task, url, dest)
                else:
                    ytdlp_download(task)
            except Exception as exc:
                update_task(
                    task_id,
                    status="failed",
                    message="任务失败",
                    error=f"{exc}\n{traceback.format_exc()[-800:]}",
                )
    finally:
        with LOCK:
            ACTIVE_TASKS -= 1
            control = CONTROLS.get(task_id)
            if generation is not None and isinstance(control, dict) and safe_int(control.get("generation")) == generation:
                control["active"] = False
                control["future"] = None


def create_task(task_data: dict) -> dict:
    task_id = uuid_id()
    task = {
        "id": task_id,
        "engine": task_data.get("engine", "ytdlp"),
        "kind": task_data.get("kind", "video"),
        "source_url": task_data.get("source_url", ""),
        "title": task_data.get("title", ""),
        "format": task_data.get("format", ""),
        "item": task_data.get("item", {}),
        "status": "queued",
        "progress": 0,
        "downloaded": 0,
        "total": 0,
        "speed": 0,
        "eta": "",
        "message": "排队中",
        "file": "",
        "error": "",
        "created_at": now(),
        "finished_at": "",
    }
    with LOCK:
        TASKS[task_id] = task
        persist_tasks()
    task_control(task_id)
    submit_task(task_id)
    log(f"task queued: {task_id[:8]} {task['title'][:50]}")
    return task


def uuid_id() -> str:
    import uuid

    return uuid.uuid4().hex


# Resume tasks that were still active when the console was closed.
for _tid, _task in list(TASKS.items()):
    if _task.get("status") == "queued":
        submit_task(_tid)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

API_FETCH_ENDPOINTS = {
    "douyin/detail", "douyin/account", "douyin/mix", "douyin/share", "douyin/live",
    "douyin/search", "douyin/comment", "douyin/reply",
    "tiktok/detail", "tiktok/account", "tiktok/mix", "tiktok/share", "tiktok/live",
}
API_PROXY_ROOTS = {"token", "douyin", "tiktok"}


def allowed_api_endpoint(endpoint, proxy: bool = False) -> bool:
    text = str(endpoint or "").strip().lstrip("/")
    if not text or len(text) > 120:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*(?:/[A-Za-z0-9_-]+)*", text):
        return False
    lowered = text.lower()
    if proxy:
        return lowered.split("/", 1)[0] in API_PROXY_ROOTS
    return any(lowered == item or lowered.startswith(item + "/") for item in API_FETCH_ENDPOINTS)


class Handler(BaseHTTPRequestHandler):
    server_version = "DouKStudio/1.0"

    def log_message(self, fmt, *args):
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            origin = (self.headers.get("Origin") or "").strip()
            if origin and self.origin_allowed():
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def json_response(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def error_response(self, message: str, status: int = 400) -> None:
        self.json_response({"ok": False, "error": message}, status)

    def origin_allowed(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        try:
            parsed = urllib.parse.urlparse(origin)
            if parsed.scheme not in ("http", "https"):
                return False
            if (parsed.hostname or "").lower() not in ("127.0.0.1", "localhost"):
                return False
            allowed_ports = {8765, safe_int(SETTINGS.get("port"), 8765)}
            return safe_int(parsed.port, 0) in allowed_ports
        except Exception:
            return False

    def allowed_download_path(self, raw):
        """Return the resolved path only when it stays inside download_dir."""
        try:
            base = Path(SETTINGS["download_dir"]).resolve()
            target = Path(str(raw or "")).resolve()
            if target == base or base in target.parents:
                return target
        except Exception:
            pass
        return None

    def read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            raise BadRequest("请求长度无效")
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise BadRequest("请求 JSON 格式无效")
        if not isinstance(data, dict):
            raise BadRequest("请求必须是 JSON 对象")
        return data

    def serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        file = (STATIC / rel).resolve()
        if STATIC.resolve() not in file.parents and file != STATIC.resolve() / "index.html":
            self.error_response("Forbidden", 403)
            return
        if not file.exists() or not file.is_file():
            if Path(rel).suffix:
                self.error_response("Not Found", 404)
                return
            file = STATIC / "index.html"
        ctype = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, file.read_bytes(), ctype)

    def do_OPTIONS(self):
        if not self.origin_allowed():
            self.error_response("Forbidden origin", 403)
            return
        self._send(204, b"", "text/plain")

    def do_GET(self):
        if not self.origin_allowed():
            self.error_response("Forbidden origin", 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            if path == "/api/status":
                exe = SETTINGS.get("yt_dlp_path") or shutil.which("yt-dlp")
                ffmpeg = SETTINGS.get("ffmpeg_path") or shutil.which("ffmpeg")
                original = read_original_settings()
                cookie = original.get("cookie", {})
                cookie_count = len(cookie) if isinstance(cookie, dict) else (1 if cookie else 0)
                with LOCK:
                    counts = {
                        "all": len(TASKS),
                        "running": sum(1 for t in TASKS.values() if t.get("status") == "running"),
                        "queued": sum(1 for t in TASKS.values() if t.get("status") == "queued"),
                        "completed": sum(1 for t in TASKS.values() if t.get("status") == "completed"),
                        "failed": sum(1 for t in TASKS.values() if t.get("status") == "failed"),
                    }
                self.json_response(
                    {
                        "ok": True,
                        "engine_running": api_alive(),
                        "api_url": api_base(),
                        "yt_dlp": exe or "",
                        "ffmpeg": ffmpeg or "",
                        "cookie_fields": cookie_count,
                        "tiktok_cookie": bool(original.get("cookie_tiktok")),
                        "download_dir": SETTINGS["download_dir"],
                        "counts": counts,
                        "history_count": len(HISTORY),
                        "version": APP_VERSION,
                        "douyin_login": login_status_snapshot(),
                    }
                )
            elif path == "/api/settings":
                original = read_original_settings()
                public_keys = [
                    "root",
                    "folder_name",
                    "name_format",
                    "desc_length",
                    "name_length",
                    "date_format",
                    "split",
                    "folder_mode",
                    "music",
                    "chunk",
                    "timeout",
                    "max_retry",
                    "max_size",
                    "proxy",
                    "download",
                    "dynamic_cover",
                    "static_cover",
                    "date_format",
                    "split",
                    "name_length",
                    "music",
                    "truncate",
                    "storage_format",
                    "proxy_tiktok",
                    "twc_tiktok",
                    "max_size",
                    "max_pages",
                    "live_qualities",
                    "douyin_platform",
                    "tiktok_platform",
                    "run_command",
                ]
                original_public = {k: original.get(k) for k in public_keys}
                self.json_response(
                    {
                        "ok": True,
                        "app": SETTINGS,
                        "original": original_public,
                        "cookie_configured": bool(original.get("cookie")),
                    }
                )
            elif path == "/api/tasks":
                with LOCK:
                    tasks = sorted(TASKS.values(), key=lambda t: t.get("created_at", ""), reverse=True)
                self.json_response({"ok": True, "tasks": tasks})
            elif path == "/api/history":
                self.json_response({"ok": True, "history": HISTORY[:100]})
            elif path == "/api/logs":
                with LOCK:
                    lines = LOG_CACHE[-200:]
                self.json_response({"ok": True, "logs": lines})
            elif path == "/api/login/status":
                self.json_response({"ok": True, **login_status_snapshot()})
            elif path == "/api/files":
                directory = Path(SETTINGS["download_dir"])
                files = []
                if directory.exists():
                    for item in sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:300]:
                        st = item.stat()
                        is_dir = item.is_dir()
                        if item.is_file() or is_dir:
                            files.append(
                                {
                                    "name": item.name + ("/" if is_dir else ""),
                                    "path": str(item),
                                    "size": 0 if is_dir else st.st_size,
                                    "size_text": "文件夹" if is_dir else human_size(st.st_size),
                                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                    "is_dir": is_dir,
                                }
                            )
                self.json_response({"ok": True, "files": files, "directory": str(directory)})
            elif path == "/api/proxy":
                target = query.get("target", [""])[0]
                if not allowed_api_endpoint(target, proxy=True):
                    self.error_response("不支持的代理端点", 400)
                    return
                result = http_request(f"{api_base()}/{target.lstrip('/')}", "GET", timeout=60)
                if not isinstance(result, dict):
                    self.error_response("接口返回数据格式异常", 502)
                    return
                self.json_response({"ok": True, "data": result})
            else:
                self.serve_static(path)
        except Exception as exc:
            self.error_response(str(exc), 500)

    def do_POST(self):
        if not self.origin_allowed():
            self.error_response("Forbidden origin", 403)
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self.read_body()
        except BadRequest as exc:
            self.error_response(str(exc), 400)
            return

        try:
            if path == "/api/engine/start":
                ok, msg = start_engine()
                self.json_response({"ok": ok, "message": msg})
            elif path == "/api/engine/stop":
                ok, msg = stop_engine()
                self.json_response({"ok": ok, "message": msg})
            elif path == "/api/resolve":
                result = resolve_url(body.get("url", ""), body.get("engine", "auto"))
                self.json_response(result)
            elif path == "/api/resolve/batch":
                urls = [str(u).strip() for u in (body.get("urls") or []) if str(u).strip()]
                if not urls:
                    self.error_response("没有可解析的链接")
                    return
                if len(urls) > 10:
                    self.error_response("一次最多解析 10 条链接")
                    return
                engine = body.get("engine", "auto")
                results = []
                for index, link in enumerate(urls):
                    log(f"batch resolve {index + 1}/{len(urls)}: {link[:90]}")
                    try:
                        res = resolve_url(link, engine)
                    except Exception as exc:
                        res = {"ok": False, "error": str(exc), "items": []}
                    res["url"] = link
                    results.append(res)
                items = []
                for res in results:
                    for item in res.get("items") or []:
                        if item not in items:
                            items.append(item)
                self.json_response(
                    {
                        "ok": True,
                        "items": items,
                        "results": results,
                        "failed": sum(1 for r in results if not r.get("ok")),
                    }
                )
            elif path == "/api/caption/account":
                result = fetch_account_captions(
                    str(body.get("sec_uid") or ""),
                    str(body.get("platform") or "douyin"),
                    int(body.get("count") or 100),
                    str(body.get("tab") or "post"),
                    bool(body.get("export")),
                    str(body.get("name") or ""),
                )
                self.json_response(result)
            elif path == "/api/caption/deep":
                records = body.get("records")
                if not records and body.get("items"):
                    records = [caption_from_item(i) for i in body["items"] if isinstance(i, dict)]
                if not records:
                    self.error_response("没有可提取解说文案的记录")
                    return
                if not asr_available():
                    self.error_response("ASR 组件未安装：项目内缺少 .venv（faster-whisper）")
                    return
                model_name = str(body.get("model") or "small")
                result = extract_narrations(records, model_name)
                self.json_response(result)
            elif path == "/api/caption/open-dir":
                directory = Path(SETTINGS["download_dir"]) / "文案导出"
                directory.mkdir(parents=True, exist_ok=True)
                if os.name == "nt":
                    subprocess.Popen(["explorer", str(directory)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(directory)])
                else:
                    subprocess.Popen(["xdg-open", str(directory)])
                self.json_response({"ok": True, "path": str(directory)})
            elif path == "/api/caption/export":
                records = body.get("records")
                if not records and body.get("items"):
                    records = [caption_from_item(i) for i in body["items"] if isinstance(i, dict)]
                self.json_response(
                    export_captions(
                        records or [],
                        str(body.get("format") or "txt"),
                        str(body.get("name") or ""),
                    )
                )
            elif path == "/api/original/action":
                result = run_original_action(str(body.get("action", "")))
                self.json_response(result)
            elif path == "/api/login/qr":
                if not _find_chromium():
                    self.error_response("未找到可用的浏览器（Edge/Chrome），请手动粘贴 Cookie")
                    return
                state_now = _login_state()
                if state_now.get("phase") in ("opening", "waiting"):
                    self.json_response({"ok": False, "error": "已有登录窗口进行中", "state": state_now})
                    return
                threading.Thread(target=browser_login_douyin, daemon=True).start()
                self.json_response({"ok": True, "message": "正在打开登录窗口", "state": _login_state()})
            elif path == "/api/login/import":
                ok, message = import_browser_cookie()
                self.json_response({"ok": ok, "message": message, "state": _login_state()})
            elif path == "/api/login/manual":
                cookie = normalize_cookie_text(body.get("cookie"))
                if not cookie:
                    self.error_response("Cookie 内容为空")
                    return
                if "sessionid" not in cookie:
                    self.error_response(
                        "Cookie 中缺少 sessionid（未登录状态），请在已登录抖音的浏览器中复制完整 Cookie"
                    )
                    return
                save_login_cookie(cookie, "manual")
                self.json_response({"ok": True, "message": "Cookie 已保存并同步到引擎", "state": _login_state()})
            elif path == "/api/login/logout":
                ok, message = logout_douyin()
                self.json_response({"ok": ok, "message": message, "state": _login_state()})
            elif path == "/api/login/validate":
                result = validate_login_cookie()
                self.json_response({"ok": True, "validation": result, "state": login_status_snapshot()})
            elif path == "/api/history/clear":
                with LOCK:
                    HISTORY.clear()
                    persist_history()
                log("download history cleared")
                self.json_response({"ok": True, "message": "下载历史已清空"})
            elif path == "/api/tasks/clear":
                scope = str(body.get("scope") or "all")
                with LOCK:
                    removable = []
                    for tid, t in TASKS.items():
                        status = str(t.get("status") or "")
                        if status in ("running", "queued"):
                            continue
                        if scope == "completed" and status != "completed":
                            continue
                        if scope == "failed" and status not in ("failed", "canceled"):
                            continue
                        removable.append(tid)
                    for tid in removable:
                        control = CONTROLS.get(tid)
                        if control:
                            control["cancel"].set()
                        TASKS.pop(tid, None)
                        CONTROLS.pop(tid, None)
                    if removable:
                        persist_tasks()
                log(f"task queue cleared ({scope}): {len(removable)} removed")
                self.json_response({"ok": True, "removed": len(removable)})
            elif path == "/api/fetch":
                endpoint = str(body.get("endpoint", "")).lstrip("/")
                payload = body.get("payload") or {}
                if not endpoint:
                    self.error_response("缺少 endpoint")
                    return
                if not allowed_api_endpoint(endpoint):
                    self.error_response("不支持的接口端点", 400)
                    return
                if not isinstance(payload, dict):
                    self.error_response("请求参数必须是对象")
                    return
                if not api_alive():
                    ok, msg = start_engine()
                    if not ok:
                        self.error_response(msg)
                        return
                service_type = body.get("type", "media")
                browser_ok_endpoints = (
                    "douyin/account",
                    "douyin/mix",
                    "douyin/search/general",
                    "douyin/search/video",
                    "douyin/search/user",
                    "douyin/search/live",
                    "douyin/reply",
                )
                try:
                    result = http_request(f"{api_base()}/{endpoint}", "POST", payload, timeout=180)
                except Exception as exc:
                    if endpoint == "douyin/comment" and payload.get("reply"):
                        retry_payload = dict(payload)
                        retry_payload["reply"] = False
                        try:
                            result = http_request(f"{api_base()}/{endpoint}", "POST", retry_payload, timeout=180)
                        except Exception as exc2:
                            self.error_response(f"引擎接口调用失败：{exc2}", 502)
                            return
                        if isinstance(result, dict):
                            result["message"] = "获取成功（已自动去掉「包含回复」：DouK 回复接口当前不可用）"
                            log("comment retry without reply succeeded", "WARN")
                    elif endpoint not in browser_ok_endpoints:
                        self.error_response(f"引擎接口调用失败：{exc}", 502)
                        return
                    else:
                        log(f"DouK {endpoint} fetch failed: {exc}", "WARN")
                        result = {}
                if not isinstance(result, dict):
                    if endpoint not in browser_ok_endpoints:
                        self.error_response("接口返回数据格式异常", 502)
                        return
                    result = {}
                response = {"ok": bool(result.get("message")), "raw": result}
                if endpoint == "douyin/reply" and not (isinstance(result.get("data"), list) and result.get("data")):
                    replies = browser_fetch_replies(
                        str(payload.get("detail_id") or "").strip(),
                        str(payload.get("comment_id") or "").strip(),
                    )
                    if replies:
                        log(f"reply fallback: browser fetched {len(replies)} replies", "INFO")
                        result = {"message": "浏览器兜底获取成功", "data": replies}
                        response = {"ok": True, "raw": result, "fallback": True}
                if service_type in ("media", "auto"):
                    items = normalize_media(result.get("data"), endpoint.split("/")[0])
                    if not items and endpoint in browser_ok_endpoints:
                        items, fallback_ok = browser_fetch_media_fallback(endpoint, payload)
                        if fallback_ok:
                            response["fallback"] = True
                    if not items and endpoint in browser_ok_endpoints:
                        response["error"] = collect_empty_error(endpoint, payload)
                    response["items"] = items
                    # 兜底/引擎拿到数据即视为成功，message 字段缺失不应丢掉结果
                    if items:
                        response["ok"] = True
                        if not result.get("message"):
                            result["message"] = "获取成功（浏览器兜底）"
                            response["raw"] = result
                elif endpoint == "douyin/reply" and not response.get("fallback"):
                    response["error"] = collect_empty_error(endpoint, payload)
                self.json_response(response)
            elif path == "/api/download":
                items = body.get("items") or []
                if not items and body.get("item"):
                    items = [body["item"]]
                if not items:
                    self.error_response("没有可下载的项目")
                    return
                created = []
                for entry in items:
                    item = entry.get("item") if "item" in entry else entry
                    created.append(
                        create_task(
                            {
                                "engine": entry.get("engine") or SETTINGS.get("default_engine", "auto"),
                                "kind": entry.get("kind", "video"),
                                "source_url": entry.get("source_url") or item.get("video_url") or "",
                                "title": entry.get("title") or item.get("title", ""),
                                "format": entry.get("format", ""),
                                "item": item,
                            }
                        )
                    )
                self.json_response({"ok": True, "tasks": created})
            elif path == "/api/task/action":
                task_id = body.get("id", "")
                action = body.get("action", "")
                control = task_control(task_id)
                with LOCK:
                    task = TASKS.get(task_id)
                if not task:
                    self.error_response("任务不存在", 404)
                    return
                if action == "cancel":
                    control["cancel"].set()
                    proc = control.get("proc")
                    if proc and proc.poll() is None:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    update_task(task_id, status="canceled", message="正在取消...")
                elif action == "pause":
                    control["pause"].set()
                    update_task(task_id, status="paused", message="已暂停")
                elif action == "resume":
                    control["pause"].clear()
                    future = control.get("future")
                    if control["active"] or (future is not None and not future.done()):
                        update_task(task_id, status="running", message="已继续")
                    else:
                        update_task(task_id, status="queued", message="排队中")
                        submit_task(task_id)
                elif action == "retry":
                    future = control.get("future")
                    if (
                        control["active"]
                        or (future is not None and not future.done())
                        or task.get("status") in ("running", "queued", "paused")
                    ):
                        self.error_response("任务仍在执行中，请先取消后再重试")
                        return
                    control["cancel"].clear()
                    control["pause"].clear()
                    update_task(
                        task_id,
                        status="queued",
                        progress=0,
                        error="",
                        message="排队中",
                        finished_at="",
                        file="",
                    )
                    submit_task(task_id)
                elif action == "remove":
                    control["cancel"].set()
                    with LOCK:
                        TASKS.pop(task_id, None)
                        CONTROLS.pop(task_id, None)
                        persist_tasks()
                else:
                    self.error_response("未知操作")
                    return
                self.json_response({"ok": True})
            elif path == "/api/settings":
                updates = body.get("app") or {}
                original_updates = body.get("original") or {}
                if not isinstance(updates, dict) or not isinstance(original_updates, dict):
                    self.error_response("设置格式无效")
                    return
                validated: dict = {}
                for key, value in updates.items():
                    if key not in DEFAULT_SETTINGS:
                        continue
                    valid, cleaned = validate_setting_update(key, value)
                    if not valid:
                        self.error_response(f"设置项 {key} 的值无效", 400)
                        return
                    validated[key] = cleaned
                new_dir = validated.get("download_dir", SETTINGS["download_dir"])
                try:
                    Path(new_dir).mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    self.error_response(f"下载目录无效：{exc}", 400)
                    return
                with LOCK:
                    SETTINGS.update(validated)
                    SETTINGS["filename_template"] = valid_filename_template(
                        SETTINGS.get("filename_template")
                    )
                    save_json(SETTINGS_FILE, SETTINGS)
                    rebuild_semaphore(SETTINGS.get("max_concurrent", 3))
                if original_updates:
                    try:
                        original = read_original_settings(strict=True)
                        backup_original_settings()
                        for key, value in original_updates.items():
                            if key not in original:
                                continue
                            current = original[key]
                            if isinstance(current, bool):
                                if not isinstance(value, bool):
                                    continue
                            elif isinstance(current, int) and not isinstance(current, bool):
                                number = safe_int(value, None)
                                if number is None:
                                    continue
                                value = number
                            elif isinstance(current, str):
                                if not isinstance(value, str):
                                    continue
                            else:
                                continue
                            original[key] = value
                        atomic_write_text(
                            ORIGINAL_SETTINGS,
                            json.dumps(original, ensure_ascii=False, indent=4),
                        )
                        log("原工具配置已更新（备份：settings.json.bak-doukstudio）")
                    except Exception as exc:
                        log(f"原工具配置读取或写回失败，已跳过写回：{exc}", "WARN")
                self.json_response({"ok": True, "settings": SETTINGS})
            elif path == "/api/files/pick":
                current = str(body.get("current") or SETTINGS["download_dir"])
                chosen = pick_directory_windows(current)
                if chosen and Path(chosen).is_dir():
                    self.json_response({"ok": True, "path": chosen})
                else:
                    self.json_response({"ok": False, "error": "未选择有效目录"})
            elif path == "/api/files/open":
                directory = self.allowed_download_path(body.get("path") or SETTINGS["download_dir"])
                if directory is None:
                    self.error_response("只能打开下载目录内的路径", 403)
                    return
                if os.name == "nt":
                    os.startfile(str(directory))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(directory)])
                else:
                    subprocess.Popen(["xdg-open", str(directory)])
                self.json_response({"ok": True})
            elif path == "/api/files/reveal":
                raw_path = body.get("path", "")
                if not str(raw_path or "").strip():
                    self.error_response("缺少文件路径")
                    return
                file = self.allowed_download_path(raw_path)
                if file is None:
                    self.error_response("只能访问下载目录内的文件", 403)
                    return
                if not file.exists():
                    self.error_response("文件不存在", 404)
                    return
                if os.name == "nt":
                    subprocess.Popen(["explorer", "/select,", str(file)])
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", "-R", str(file)])
                else:
                    subprocess.Popen(["xdg-open", str(file.parent)])
                self.json_response({"ok": True})
            else:
                self.error_response("Not found", 404)
        except Exception as exc:
            log(f"HTTP error: {exc}\n{traceback.format_exc()[-600:]}", "ERROR")
            self.error_response(str(exc), 500)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    port = safe_int(SETTINGS.get("port"), 8765)
    if not 1 <= port <= 65535:
        port = 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log(f"DouK Studio ready at http://127.0.0.1:{port}")
    if SETTINGS.get("auto_start_engine"):
        threading.Thread(target=start_engine, daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_engine()
        server.server_close()


if __name__ == "__main__":
    main()
