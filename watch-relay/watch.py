#!/usr/bin/env python3
"""Watch Relay — 只读地把 web-tool 那台 headless Chrome（web-browser-chrome.
service, CDP 9223）的画面转出去，让人能在手机/电脑上"实时看着 agent 上网"。

跟 /opt/ai-social-browser/relay/relay.py（Browser Relay）是同一个路子：纯
websockets + 直连 CDP，不用 Playwright，靠 Page.startScreencast 拿 jpeg 帧转
发给所有连上的客户端。CDP 端口没有鉴权，谁连上就能操控里面的一切，但这个
Chrome 本身也没有任何登录态（见 web-browser-chrome.service 的注释），能看的
东西上限就是"公开网页"。

跟 relay.py 最大的不同：这里只看不动手。
  - 没有 X11Input，没有 xvfb——web-browser-chrome 本来就是 headless，截屏
    推流不需要真实显示器，relay.py 那套 X11 鼠标/键盘注入是给"人要操作
    带真实登录态的 headed Chrome"用的，这里用不上也不该有（多一条写入
    路径就多一条误操作/被入侵后拿去乱点的风险，这个 Chrome 完全没必要
    暴露写权限）。
  - 没有 Emulation.setDeviceMetricsOverride / mobile 模拟——web-tool 那边
    自己给页面设了 1280x1400 的 viewport（见 app.py 的 new_work_page），
    这里如实转发就行，不需要另外覆盖一层尺寸。
  - 目标页面不是这个进程自己开的，是 web-tool/app.py 里那个常驻的"观察
    页"（get_page()）——每次用户触发 read/search，web-tool 会在同一个
    页面上 navigate，不再像以前那样开新标签用完就关，所以画面才会一直有
    内容可看。这边只负责"找到当前唯一的 page target 并挂上去看"，找不到
    (Chrome 刚重启、还没人发起过请求) 就等着重试，网页/标签页本身的生命
    周期完全由 web-tool 管理，这里不创建也不关闭任何标签页。
"""

import asyncio
import hashlib
import hmac
import json
import os
import signal
import sys
import urllib.request
from pathlib import Path

try:
    from websockets.asyncio.server import serve
    from websockets import Response
    from websockets.datastructures import Headers
except ImportError:
    print("需要安装: pip3 install websockets")
    sys.exit(1)

DEFAULT_HOST = os.environ.get("WATCH_RELAY_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WATCH_RELAY_PORT", "8277"))
CDP_HOST = os.environ.get("WATCH_RELAY_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("WATCH_RELAY_CDP_PORT", "9223"))
SCREENCAST_QUALITY = 60
# web-tool 给观察页设的 viewport 是 1280x1400（见 app.py new_work_page）；
# maxWidth/maxHeight 按同样尺寸给上限，避免 Chrome 编码出比实际视口还大的帧。
MAX_WIDTH = 1280
MAX_HEIGHT = 1400
# CDP 没有事件通知"哪个 target 死了/换了"，只能定期拿 /json 比对——web-tool
# 那边页面基本不会被重建（除非 Chrome 整个重启），几秒钟的发现延迟换来不用
# 常驻监听 CDP 的 Target.* 事件，够用。
TARGET_POLL_INTERVAL = 3

_PASS = os.environ.get("WATCH_RELAY_PASS", "")
AUTH_TOKEN = _PASS
COOKIE_TOKEN = hashlib.sha256(f"watch-relay-cookie-v1:{_PASS}".encode()).hexdigest()
COOKIE_NAME = "watch_auth"
COOKIE_MAX_AGE = 30 * 24 * 3600

CLIENT_HTML = Path(__file__).parent / "client.html"
LOGIN_HTML = Path(__file__).parent / "login.html"


class WatchRelay:
    def __init__(self):
        self.clients = set()
        self.cdp_ws = None
        self._msg_id = 0
        self._pending = {}
        self._running = True
        self._last_frame = None
        self._send_lock = asyncio.Lock()
        self._reader_task = None
        self._poll_task = None
        self._current_target_id = None

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    async def _cdp_send(self, method, params=None):
        async with self._send_lock:
            mid = self._next_id()
            msg = {"id": mid, "method": method}
            if params:
                msg["params"] = params
            await self.cdp_ws.send(json.dumps(msg))
            return mid

    async def cdp_call(self, method, params=None):
        fut = asyncio.get_event_loop().create_future()
        mid = await self._cdp_send(method, params)
        self._pending[mid] = fut
        try:
            return await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise

    async def cdp_fire(self, method, params=None):
        await self._cdp_send(method, params)

    def _get_pages(self):
        try:
            resp = urllib.request.urlopen(f"http://{CDP_HOST}:{CDP_PORT}/json", timeout=3)
            return [p for p in json.loads(resp.read()) if p.get("type") == "page"]
        except Exception:
            return []

    async def _cdp_reader(self):
        try:
            async for raw in self.cdp_ws:
                data = json.loads(raw)

                msg_id = data.get("id")
                if msg_id and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        if "error" in data:
                            fut.set_result(data["error"])
                        else:
                            fut.set_result(data.get("result", {}))
                    continue

                if data.get("method") == "Page.screencastFrame":
                    params = data["params"]
                    await self.cdp_fire("Page.screencastFrameAck", {
                        "sessionId": params.get("sessionId", 0),
                    })
                    frame_data = json.dumps({"type": "frame", "data": params["data"]})
                    self._last_frame = frame_data
                    if self.clients:
                        await asyncio.gather(
                            *[self._safe_send(c, frame_data) for c in self.clients],
                            return_exceptions=True,
                        )
        except Exception as e:
            if self._running:
                print(f"CDP 读取断开: {e}")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("CDP disconnected"))
            self._pending.clear()
            self.cdp_ws = None
            self._current_target_id = None

    async def _connect_target(self, page):
        from websockets.asyncio.client import connect as ws_connect

        ws_url = page["webSocketDebuggerUrl"]
        try:
            if self.cdp_ws:
                await self.cdp_ws.close()
        except Exception:
            pass
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        self.cdp_ws = await ws_connect(ws_url, max_size=50 * 1024 * 1024, ping_interval=None)
        self._reader_task = asyncio.create_task(self._cdp_reader())

        await self.cdp_call("Page.enable")
        await self.cdp_fire("Page.startScreencast", {
            "format": "jpeg",
            "quality": SCREENCAST_QUALITY,
            "maxWidth": MAX_WIDTH,
            "maxHeight": MAX_HEIGHT,
            "everyNthFrame": 1,
        })
        result = await self.cdp_call("Page.captureScreenshot", {
            "format": "jpeg", "quality": SCREENCAST_QUALITY,
        })
        if "data" in result:
            self._last_frame = json.dumps({"type": "frame", "data": result["data"]})

        self._current_target_id = page["id"]
        print(f"已挂上观察页: {page.get('title', '')} {page.get('url', '')}")

    async def _target_poll_loop(self):
        """定期检查 web-tool 的观察页有没有换（Chrome 重启后 target id 会变），
        换了就重新挂 screencast；还没连上也是靠这个循环兜底建立初次连接。"""
        while self._running:
            try:
                pages = self._get_pages()
                if pages:
                    target = pages[0]
                    if target["id"] != self._current_target_id or self.cdp_ws is None:
                        await self._connect_target(target)
                elif self.cdp_ws is None:
                    pass  # 还没有任何页面（web-tool 尚未处理过请求），继续等
            except Exception as e:
                print(f"target 轮询出错: {e}")
            await asyncio.sleep(TARGET_POLL_INTERVAL)

    async def _safe_send(self, ws, data):
        try:
            await ws.send(data)
        except Exception:
            self.clients.discard(ws)

    async def handle_client(self, ws):
        self.clients.add(ws)
        remote = ws.remote_address
        print(f"客户端连接: {remote}")
        if self._last_frame:
            await self._safe_send(ws, self._last_frame)
        try:
            async for _ in ws:
                pass  # 只读观影，客户端不发任何有意义的消息，收到就丢
        except Exception:
            pass
        finally:
            self.clients.discard(ws)
            print(f"客户端断开: {remote}")

    async def start(self):
        self._poll_task = asyncio.create_task(self._target_poll_loop())

    async def stop(self):
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self.cdp_ws:
            await self.cdp_ws.close()


def _safe_equal(a, b):
    try:
        return hmac.compare_digest(a, b)
    except TypeError:
        return False


def _check_cookie(request):
    cookie_header = request.headers.get("Cookie", "")
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{COOKIE_NAME}="):
            return _safe_equal(part.split("=", 1)[1], COOKIE_TOKEN)
    return False


def process_request(connection, request):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        if not _check_cookie(request):
            return Response(403, "Forbidden", Headers(), b"unauthorized")
        return

    auth_header = request.headers.get("X-Relay-Auth")
    if auth_header is not None:
        if _safe_equal(auth_header, AUTH_TOKEN):
            headers = Headers([(
                "Set-Cookie",
                f"{COOKIE_NAME}={COOKIE_TOKEN}; Path=/; Max-Age={COOKIE_MAX_AGE}; HttpOnly; SameSite=Strict",
            )])
            return Response(204, "No Content", headers, b"")
        return Response(403, "Forbidden", Headers(), b"wrong password")

    if not _check_cookie(request):
        try:
            html = LOGIN_HTML.read_bytes()
            return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8")]), html)
        except FileNotFoundError:
            return Response(500, "Error", Headers(), b"login.html not found")

    try:
        html = CLIENT_HTML.read_bytes()
        return Response(200, "OK", Headers([("Content-Type", "text/html; charset=utf-8")]), html)
    except FileNotFoundError:
        return Response(404, "Not Found", Headers(), b"client.html not found")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Watch Relay")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if not _PASS:
        print("警告: 未设置 WATCH_RELAY_PASS，空密码即可登录 — 只可用于本机调试，绝不要这样暴露到公网")

    relay = WatchRelay()
    await relay.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with serve(
        relay.handle_client, args.host, args.port,
        process_request=process_request,
    ):
        print(f"服务已启动: http://{args.host}:{args.port}")
        await stop_event.wait()

    await relay.stop()


if __name__ == "__main__":
    asyncio.run(main())
