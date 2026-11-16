#!/usr/bin/env python3
"""OrangeMouse: use your phone as a wireless mouse / trackpad."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path

from aiohttp import WSCloseCode, WSMsgType, web

DEFAULT_PORT = 8765


def app_root() -> Path:
    """Directory that contains static/ — works in source and in a PyInstaller bundle."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


ROOT = app_root()
STATIC = ROOT / "static"
IS_FROZEN = bool(getattr(sys, "frozen", False))


def show_error(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
    if not IS_FROZEN:
        return
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror("OrangeMouse", message)
        root.destroy()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Input backend (pynput, or a dry-run mock when no display is available)
# ---------------------------------------------------------------------------

class DummyMouse:
    def __init__(self) -> None:
        self.position = (0, 0)

    def move(self, dx: int, dy: int) -> None:
        x, y = self.position
        self.position = (x + int(dx), y + int(dy))
        print(f"[dry-run] move {dx:+.1f},{dy:+.1f} -> {self.position}", flush=True)

    def scroll(self, dx: int, dy: int) -> None:
        print(f"[dry-run] scroll dx={dx} dy={dy}", flush=True)

    def click(self, button, count: int = 1) -> None:
        print(f"[dry-run] click {button} x{count}", flush=True)

    def press(self, button) -> None:
        print(f"[dry-run] press {button}", flush=True)

    def release(self, button) -> None:
        print(f"[dry-run] release {button}", flush=True)


class DummyKeyboard:
    def tap(self, key) -> None:
        print(f"[dry-run] key {key}", flush=True)

    def press(self, key) -> None:
        print(f"[dry-run] key press {key}", flush=True)

    def release(self, key) -> None:
        print(f"[dry-run] key release {key}", flush=True)


class DummyButton:
    left = "left"
    right = "right"
    middle = "middle"


class DummyKey:
    page_up = "page_up"
    page_down = "page_down"
    home = "home"
    end = "end"
    alt = "alt"
    cmd = "cmd"
    left = "left"


def _try_pynput():
    from pynput.mouse import Button, Controller as MouseController
    from pynput.keyboard import Controller as KeyboardController, Key

    mouse = MouseController()
    keyboard = KeyboardController()
    # Accessing position fails early on headless Linux / missing permissions.
    _ = mouse.position
    return mouse, keyboard, Button, Key


def init_backend(dry_run: bool):
    if dry_run:
        print("Dry-run mode: no real mouse/keyboard control. / 演练模式：不会真正移动鼠标。", flush=True)
        return DummyMouse(), DummyKeyboard(), DummyButton, DummyKey, True

    try:
        mouse, keyboard, Button, Key = _try_pynput()
        return mouse, keyboard, Button, Key, False
    except Exception as exc:
        print(
            "Could not initialize real mouse control; switching to dry-run.\n"
            "无法初始化真实鼠标控制，改为演练模式。\n"
            f"Reason / 原因: {exc}\n"
            "On macOS, grant Accessibility permission to OrangeMouse (or Terminal / Python).\n"
            "若在 macOS 上，请给 OrangeMouse（或终端 / Python）开启「辅助功能」权限。\n"
            "On a headless Linux machine, this is expected.\n"
            "若在 Linux 无图形界面的环境，这是预期行为。",
            flush=True,
        )
        return DummyMouse(), DummyKeyboard(), DummyButton, DummyKey, True


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

VIRTUAL_ADAPTER_RE = re.compile(
    r"vethernet|vmware|virtualbox|\bvbox\b|wsl|hyper-v|\bhyperv\b|"
    r"docker|tailscale|zerotier|npcap|loopback|bluetooth",
    re.IGNORECASE,
)

FIREWALL_RULE_NAME = "OrangeMouse"


def _is_loopback_ip(ip: str) -> bool:
    return ip.startswith("127.") or ip in ("0.0.0.0", "::1")


def _is_link_local_ip(ip: str) -> bool:
    return ip.startswith("169.254.")


def _is_rfc1918_172(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        return int(parts[0]) == 172 and 16 <= int(parts[1]) <= 31
    except ValueError:
        return False


def _is_preferred_private(ip: str) -> bool:
    return ip.startswith("192.168.") or ip.startswith("10.")


def is_virtual_adapter(name: str) -> bool:
    return bool(name and VIRTUAL_ADAPTER_RE.search(name))


def default_route_ip() -> str | None:
    """Best-effort IP of the interface used for outbound IPv4 traffic."""
    for target in (("8.8.8.8", 80), ("1.1.1.1", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect(target)
            ip = s.getsockname()[0]
            s.close()
            if ip and not _is_loopback_ip(ip) and not _is_link_local_ip(ip):
                return ip
        except OSError:
            continue
    return None


def _collect_ipv4() -> list[dict]:
    """Collect IPv4 addresses: psutil (with adapter names) plus socket fallbacks."""
    found: dict[str, dict] = {}

    def add(ip: str, name: str = "", source: str = "") -> None:
        ip = (ip or "").strip()
        if not ip or ":" in ip or _is_loopback_ip(ip) or _is_link_local_ip(ip):
            return
        rec = found.get(ip)
        if rec is None:
            found[ip] = {
                "ip": ip,
                "name": name or "",
                "virtual": is_virtual_adapter(name),
                "source": source,
            }
        else:
            if name and not rec["name"]:
                rec["name"] = name
                rec["virtual"] = rec["virtual"] or is_virtual_adapter(name)
            elif name and is_virtual_adapter(name):
                rec["virtual"] = True
                if not rec["name"]:
                    rec["name"] = name

    try:
        import psutil  # type: ignore
    except ImportError:
        psutil = None  # type: ignore

    if psutil is not None:
        try:
            stats = {}
            try:
                stats = psutil.net_if_stats()
            except Exception:
                stats = {}
            for if_name, addrs in psutil.net_if_addrs().items():
                st = stats.get(if_name)
                if st is not None and not st.isup:
                    continue
                for addr in addrs:
                    family = getattr(addr, "family", None)
                    if family not in (socket.AF_INET, getattr(socket, "AF_INET", 2)):
                        # Some platforms use integer 2 for IPv4.
                        if family != 2:
                            continue
                    add(addr.address, if_name, "psutil")
        except Exception:
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0], "", "getaddrinfo")
    except OSError:
        pass

    route = default_route_ip()
    if route:
        add(route, found.get(route, {}).get("name", ""), "udp-route")

    return list(found.values())


def _rank(candidate: dict, route_ip: str | None) -> tuple:
    ip = candidate["ip"]
    virtual = bool(candidate.get("virtual"))
    if _is_preferred_private(ip):
        family = 0 if ip.startswith("192.168.") else 1
    elif _is_rfc1918_172(ip):
        family = 4
    else:
        family = 3
    # Prefer the default-route IP when it is a real LAN address.
    route_boost = 0
    if route_ip == ip and not virtual and not _is_rfc1918_172(ip):
        route_boost = -1
    return (virtual, family, route_boost, ip)


def lan_candidates() -> list[dict]:
    """Ranked IPv4 candidates for the phone URL (no loopback / link-local).

    Prefer 192.168.x and 10.x. Deprioritize 172.16–31 (often WSL/Hyper-V/Docker).
    Skip virtual adapters (vEthernet, VMware, WSL, Docker, Tailscale, …) when
    a real adapter exists; otherwise keep them so the user can still try.
    """
    route_ip = default_route_ip()
    all_cands = _collect_ipv4()
    real = [c for c in all_cands if not c.get("virtual")]
    chosen = real or all_cands
    chosen.sort(key=lambda c: _rank(c, route_ip))
    return chosen


def lan_ips() -> list[str]:
    ips = [c["ip"] for c in lan_candidates()]
    return ips or ["127.0.0.1"]


def firewall_allow_command(port: int, exe: str | None = None) -> str:
    program = exe or (sys.executable if IS_FROZEN else "OrangeMouse.exe")
    return (
        f'netsh advfirewall firewall add rule name="{FIREWALL_RULE_NAME}" '
        f"dir=in action=allow enable=yes profile=private,public "
        f'program="{program}" protocol=TCP localport={int(port)}'
    )


def ensure_windows_firewall(port: int) -> dict:
    """On a frozen Windows build, try to add an inbound allow rule for this exe/port."""
    needed = bool(IS_FROZEN and sys.platform == "win32")
    exe = sys.executable if IS_FROZEN else ""
    info = {
        "needed": needed,
        "ok": not needed,
        "command": firewall_allow_command(port, exe or None) if needed or sys.platform == "win32" else "",
        "exePath": exe,
        "port": port,
    }
    if not needed:
        return info

    creationflags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW

    def run_netsh(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
        )

    try:
        run_netsh(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "delete",
                "rule",
                f"name={FIREWALL_RULE_NAME}",
            ]
        )
        added = run_netsh(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                f"name={FIREWALL_RULE_NAME}",
                "dir=in",
                "action=allow",
                "enable=yes",
                "profile=private,public",
                f"program={exe}",
                "protocol=TCP",
                f"localport={int(port)}",
            ]
        )
        if added.returncode == 0:
            info["ok"] = True
            print(
                f"Windows Firewall: allow rule added for port {port}. / 已添加防火墙放行规则（端口 {port}）。",
                flush=True,
            )
            return info
        info["ok"] = False
        detail = (added.stderr or added.stdout or "").strip()
        print(
            "Windows Firewall: could not add an allow rule (usually needs Administrator).\n"
            "无法添加防火墙放行规则（通常需要管理员权限）。",
            flush=True,
        )
        if detail:
            print(detail, flush=True)
        print("Run this in an Administrator PowerShell: / 请在管理员 PowerShell 中运行：", flush=True)
        print(f"  {info['command']}", flush=True)
    except Exception as exc:
        info["ok"] = False
        print(f"Windows Firewall: {exc}", flush=True)
    return info



def print_qr(url: str) -> None:
    try:
        import qrcode
    except ImportError:
        print("  (qrcode not installed; skip terminal QR. pip install qrcode)", flush=True)
        print("  （未安装 qrcode，跳过终端二维码。可执行: pip install qrcode）", flush=True)
        return
    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    print(flush=True)
    try:
        qr.print_ascii(invert=True)
    except Exception:
        qr.print_tty()
    print(flush=True)


def make_qr_svg(url: str) -> str | None:
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        return None
    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(image_factory=SvgPathImage).save(buf)
    return buf.getvalue().decode("utf-8")


def is_localhost(request: web.Request) -> bool:
    peer = request.remote or ""
    return peer in ("127.0.0.1", "::1")


# ---------------------------------------------------------------------------
# Action controller
# ---------------------------------------------------------------------------

class Controller:
    def __init__(self, mouse, keyboard, Button, Key, dry_run: bool) -> None:
        self.mouse = mouse
        self.keyboard = keyboard
        self.Button = Button
        self.Key = Key
        self.dry_run = dry_run
        self._sx = 0.0
        self._sy = 0.0
        self._mx = 0.0
        self._my = 0.0
        self._held: set[str] = set()
        self._is_mac = sys.platform == "darwin"

    def _button(self, name: str):
        name = (name or "left").lower()
        if name == "right":
            return self.Button.right
        if name == "middle":
            return self.Button.middle
        return self.Button.left

    def move(self, dx: float, dy: float) -> None:
        # pynput expects ints; accumulate fractional pixels for smoothness.
        self._mx += float(dx)
        self._my += float(dy)
        ix, iy = int(self._mx), int(self._my)
        if ix or iy:
            self._mx -= ix
            self._my -= iy
            self.mouse.move(ix, iy)

    def scroll(self, dx: float, dy: float) -> None:
        self._sx += float(dx)
        self._sy += float(dy)
        ix, iy = int(self._sx), int(self._sy)
        if ix or iy:
            self._sx -= ix
            self._sy -= iy
            self.mouse.scroll(ix, iy)

    def click(self, button: str, count: int = 1) -> None:
        count = max(1, min(int(count or 1), 3))
        self.mouse.click(self._button(button), count)

    def press(self, button: str) -> None:
        name = (button or "left").lower()
        if name in self._held:
            return
        self.mouse.press(self._button(name))
        self._held.add(name)

    def release(self, button: str) -> None:
        name = (button or "left").lower()
        self.mouse.release(self._button(name))
        self._held.discard(name)

    def release_all(self) -> None:
        for name in list(self._held):
            try:
                self.mouse.release(self._button(name))
            except Exception:
                pass
        self._held.clear()

    def key(self, name: str) -> None:
        name = (name or "").lower()
        mapping = {
            "page_up": self.Key.page_up,
            "page_down": self.Key.page_down,
            "home": self.Key.home,
            "end": self.Key.end,
        }
        if name in mapping:
            self.keyboard.tap(mapping[name])
            return
        if name == "back":
            modifier = self.Key.cmd if self._is_mac else self.Key.alt
            self.keyboard.press(modifier)
            try:
                self.keyboard.press(self.Key.left)
                self.keyboard.release(self.Key.left)
            finally:
                self.keyboard.release(modifier)
            return
        raise ValueError(f"unknown key: {name}")

    def handle(self, msg: dict) -> None:
        typ = msg.get("type")
        if typ == "move":
            self.move(msg.get("dx", 0), msg.get("dy", 0))
        elif typ == "scroll":
            self.scroll(msg.get("dx", 0), msg.get("dy", 0))
        elif typ == "click":
            self.click(msg.get("button", "left"), msg.get("count", 1))
        elif typ == "press":
            self.press(msg.get("button", "left"))
        elif typ == "release":
            self.release(msg.get("button", "left"))
        elif typ == "key":
            self.key(msg.get("key", ""))
        elif typ == "ping":
            return
        else:
            raise ValueError(f"unknown type: {typ}")


# ---------------------------------------------------------------------------
# HTTP / WebSocket app
# ---------------------------------------------------------------------------

FORBIDDEN_TEXT = (
    "Invalid or expired link. Use the full URL shown by OrangeMouse (including the token).\n"
    "链接无效或已过期。请使用 OrangeMouse 显示的完整网址（含令牌）。\n"
)


def _url_candidates(port: int, token: str, host: str) -> list[dict]:
    if host not in ("0.0.0.0", "::", ""):
        items = [{"ip": host, "name": "bind", "virtual": False}]
        if _is_loopback_ip(host):
            items = [{"ip": host, "name": "localhost", "virtual": False}]
    else:
        items = lan_candidates()
    out = []
    for c in items:
        url = f"http://{c['ip']}:{port}/?t={token}"
        out.append(
            {
                "ip": c["ip"],
                "name": c.get("name") or "",
                "virtual": bool(c.get("virtual")),
                "preferred": _is_preferred_private(c["ip"]) and not c.get("virtual"),
                "url": url,
                "qrSvg": make_qr_svg(url),
            }
        )
    return out


def make_app(token: str, controller: Controller, stop: asyncio.Event, port: int) -> web.Application:
    app = web.Application()
    app["token"] = token
    app["controller"] = controller
    app["clients"] = set()
    app["stop"] = stop
    # Mutable bag so later fields can change after the app has started.
    app["state"] = {
        "port": port,
        "bind_host": "0.0.0.0",
        "firewall": {"needed": False, "ok": True, "command": "", "port": port},
        "url_candidates": [],
    }

    def token_ok(request: web.Request) -> bool:
        return request.query.get("t", "") == token

    def phone_urls() -> list[str]:
        host = app["state"].get("bind_host", "0.0.0.0")
        p = app["state"]["port"]
        if host in ("0.0.0.0", "::", ""):
            return [f"http://{ip}:{p}/?t={token}" for ip in lan_ips()]
        return [f"http://{host}:{p}/?t={token}"]

    def primary_phone_url() -> str:
        urls = [u for u in phone_urls() if "127.0.0.1" not in u and "localhost" not in u]
        return (urls or phone_urls())[0]

    async def index(request: web.Request) -> web.StreamResponse:
        if not token_ok(request):
            return web.Response(
                status=403, text=FORBIDDEN_TEXT, content_type="text/plain", charset="utf-8"
            )
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def setup_page(request: web.Request) -> web.StreamResponse:
        if not (token_ok(request) or is_localhost(request)):
            return web.Response(
                status=403, text=FORBIDDEN_TEXT, content_type="text/plain", charset="utf-8"
            )
        html = (STATIC / "setup.html").read_text(encoding="utf-8")
        url = primary_phone_url()
        st = app["state"]
        p = st["port"]
        config = {
            "phoneUrls": phone_urls(),
            "primaryUrl": url,
            "localUrl": f"http://127.0.0.1:{p}/?t={token}",
            "token": token,
            "port": p,
            "bindHost": st.get("bind_host", "0.0.0.0"),
            "qrSvg": make_qr_svg(url),
            "candidates": st.get("url_candidates") or _url_candidates(p, token, st.get("bind_host", "0.0.0.0")),
            "firewall": st.get("firewall") or {"needed": False, "ok": True, "command": "", "port": p},
        }
        html = html.replace(
            "__ORANGEMOUSE_CONFIG__",
            json.dumps(config, ensure_ascii=False).replace("</", "<\\/"),
            1,
        )
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def api_info(request: web.Request) -> web.StreamResponse:
        if not (token_ok(request) or is_localhost(request)):
            return web.json_response({"error": "forbidden"}, status=403)
        url = primary_phone_url()
        st = app["state"]
        p = st["port"]
        return web.json_response(
            {
                "phoneUrls": phone_urls(),
                "primaryUrl": url,
                "localUrl": f"http://127.0.0.1:{p}/?t={token}",
                "token": token,
                "port": p,
                "bindHost": st.get("bind_host", "0.0.0.0"),
                "candidates": st.get("url_candidates") or _url_candidates(p, token, st.get("bind_host", "0.0.0.0")),
                "firewall": st.get("firewall") or {"needed": False, "ok": True, "command": "", "port": p},
            }
        )

    async def api_quit(request: web.Request) -> web.StreamResponse:
        if not is_localhost(request):
            return web.json_response({"error": "forbidden"}, status=403)
        print("Quit requested from local page. / 本机页面请求退出。", flush=True)
        stop.set()
        return web.json_response({"ok": True})

    async def ws_handler(request: web.Request) -> web.StreamResponse:
        if not token_ok(request):
            return web.Response(status=403, text="invalid token")
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        app["clients"].add(ws)
        n = len(app["clients"])
        print(f"Phone connected ({n} client{'s' if n != 1 else ''}) / 手机已连接（当前 {n} 台）", flush=True)
        try:
            await ws.send_str(json.dumps({"type": "hello", "ok": True}))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        await ws.send_str(json.dumps({"type": "error", "error": "bad json"}))
                        continue
                    if not isinstance(data, dict):
                        continue
                    try:
                        controller.handle(data)
                    except Exception as exc:
                        await ws.send_str(json.dumps({"type": "error", "error": str(exc)}))
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
        finally:
            app["clients"].discard(ws)
            controller.release_all()
            left = len(app["clients"])
            print(
                f"Phone disconnected ({left} remaining) / 手机已断开（剩余 {left} 台）",
                flush=True,
            )
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/setup", setup_page)
    app.router.add_get("/api/info", api_info)
    app.router.add_post("/api/quit", api_quit)
    app.router.add_get("/ws", ws_handler)
    return app


async def start_listening(runner: web.AppRunner, host: str, port: int, auto_fallback: bool) -> int:
    candidates = [port]
    if auto_fallback:
        candidates = list(range(port, port + 10))
    last_exc: OSError | None = None
    for p in candidates:
        site = web.TCPSite(runner, host, p)
        try:
            await site.start()
            if p != port:
                print(f"Port {port} is in use; listening on {p} instead. / 端口 {port} 已被占用，改用 {p}。", flush=True)
            return p
        except OSError as exc:
            last_exc = exc
            continue
    msg = f"Could not listen on {host}:{port}: {last_exc}\n无法监听 {host}:{port}：{last_exc}"
    show_error(msg)
    await runner.cleanup()
    raise SystemExit(1)


async def run(host: str, port: int, token: str, controller: Controller, open_browser: bool, auto_fallback: bool) -> None:
    stop = asyncio.Event()
    app = make_app(token, controller, stop, port)
    app["state"]["bind_host"] = host
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        port = await start_listening(runner, host, port, auto_fallback)
    except SystemExit:
        raise
    fw = ensure_windows_firewall(port)
    url_cands = _url_candidates(port, token, host)
    app["state"].update({"port": port, "firewall": fw, "url_candidates": url_cands})

    local_setup = f"http://127.0.0.1:{port}/setup?t={token}"
    local_debug = f"http://127.0.0.1:{port}/?t={token}"
    phone_urls = [c["url"] for c in url_cands] or [local_debug]
    remote = [u for u in phone_urls if "127.0.0.1" not in u and "localhost" not in u]
    qr_url = (remote or phone_urls)[0]

    print("", flush=True)
    print("=" * 56, flush=True)
    print("  OrangeMouse is running / 已启动", flush=True)
    print("=" * 56, flush=True)
    print(f"  Listening  / 监听地址:  {host}:{port}  (all interfaces)" if host in ("0.0.0.0", "::", "") else f"  Listening  / 监听地址:  {host}:{port}", flush=True)
    print(f"  Local page / 本机页面:  {local_setup}", flush=True)
    print(f"  Local pad  / 本机调试:  {local_debug}", flush=True)
    if url_cands:
        print("  Phone URLs / 手机地址:", flush=True)
        for i, c in enumerate(url_cands):
            mark = "  <-- scan this / 请扫这个" if c["url"] == qr_url else ""
            label = c["name"] or c["ip"]
            extra = " [virtual]" if c.get("virtual") else ""
            print(f"    {c['url']}  ({label}{extra}){mark}", flush=True)
    elif remote:
        for url in remote:
            print(f"  Phone URL  / 手机打开:  {url}", flush=True)
    if host not in ("0.0.0.0", "::", "") and host in ("127.0.0.1", "localhost"):
        print(
            "  Bound to localhost only; the phone cannot connect. Drop --host to serve the LAN.\n"
            "  当前只监听本机，手机无法连接。去掉 --host 即可给手机用。",
            flush=True,
        )
    print("", flush=True)
    print("  Scan the QR code with your phone (same Wi-Fi):", flush=True)
    print("  请用手机扫描下面的二维码（手机和电脑需在同一 Wi-Fi）：", flush=True)
    print_qr(qr_url)
    print("  Press Ctrl+C to quit. / 按 Ctrl+C 结束。", flush=True)
    print("=" * 56, flush=True)

    if open_browser:
        def _open() -> None:
            try:
                webbrowser.open(local_setup)
            except Exception as exc:
                print(f"Could not open a browser: {exc}", flush=True)

        asyncio.get_running_loop().call_later(0.4, _open)

    loop = asyncio.get_running_loop()
    try:
        import signal

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                # Windows: signal handlers are limited in asyncio.
                signal.signal(sig, lambda *_: stop.set())
    except Exception:
        pass

    try:
        await stop.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        controller.release_all()
        for ws in list(app["clients"]):
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"server shutdown")
        await runner.cleanup()
        print("Stopped. / 已退出。", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="OrangeMouse — use your phone as a wireless trackpad (cursor, clicks, drag, scroll)"
    )
    p.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port (default: {DEFAULT_PORT})")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions instead of moving the real mouse",
    )
    p.add_argument("--token", default=None, help="Access token (a random one is generated by default)")
    p.add_argument("--no-browser", action="store_true", help="Do not open the local setup page")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    dry = args.dry_run or os.environ.get("ORANGEMOUSE_DRY_RUN") == "1" or os.environ.get("PHONE_SCROLL_DRY_RUN") == "1"
    token = args.token or secrets.token_urlsafe(9)
    mouse, keyboard, Button, Key, dry = init_backend(dry)
    controller = Controller(mouse, keyboard, Button, Key, dry)
    auto_fallback = IS_FROZEN and args.port == DEFAULT_PORT
    try:
        asyncio.run(
            run(
                args.host,
                args.port,
                token,
                controller,
                open_browser=not args.no_browser,
                auto_fallback=auto_fallback,
            )
        )
    except KeyboardInterrupt:
        controller.release_all()
        print("\nStopped. / 已退出。", flush=True)


if __name__ == "__main__":
    main()
