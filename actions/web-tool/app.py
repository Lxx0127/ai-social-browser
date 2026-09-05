"""web-tool: 通用网页只读 HTTP 包装。

连接独立的 headless Chrome（web-browser-chrome.service，CDP 9223），跟
douyin-tool/twitter-tool/xiaohongshu-tool 用的是同一套 CDP 连接模式（见
douyin-tool/app.py 的 get_context()），但这个 Chrome 是干净的、不需要
任何登录态——用来查公开网页/搜索资料，跟社交平台那几个账号完全无关，
单独一个实例，出问题不会连累任何账号的登录态。

POST /web
  {"action":"search", "query":"...", "n":5}
  {"action":"read", "url":"https://...", "n":5}
  {"action":"read_links", "url":"https://...", "n":80}
  {"action":"screenshot", "url":"https://...", "full_page":false}

常驻一个"观察页"（get_page()），每次请求在同一个页面上 navigate，不再像
最早的版本那样每次开新标签用完就关——2026-09 加了 watch-relay（见
/opt/ai-social-browser/watch-relay）之后，屏幕上得随时有画面可看，用完就
关的话画面会一直是空白/上一次残留，没法"实时观看 agent 上网"。依然不维持任何
登录态（跟这个 Chrome 本身干净、无账号是两回事），也依然没有"点卡片进
详情"那种需要保留列表页的场景，常驻纯粹是为了给 watch-relay 一个稳定的
截屏目标，不是为了省一次 new_page() 的开销。

只读边界：只有 search / read / read_links / screenshot 四个动作，对目标
网页本身没有任何写操作——这是给"随口查个资料、看个网页"用的，不是浏览器
自动化平台。screenshot 会往本机磁盘写一个图片文件，但那是本地产物，不是
对目标站点的写操作，跟"只读"这条边界不冲突。

screenshot（2026-09 新增）：截一张网页的图（首屏或整页），存到 SCREENSHOT_
DIR，跟调用方项目按同一个绝对路径 bind mount 进它的容器
（见调用方项目的 docker-compose.yml）——这个服务本身跑在宿主机上，容器
里的调用方拿不到宿主机任意路径，必须存在两边路径完全一致的共享目录下，
调用方才读得到，道理跟 douyin-tool 那边"发照片到抖音"功能用 /opt/
douyin-uploads 共享目录是同一个考虑，不能真的存到字面意义上的 /tmp（那是
这个宿主机进程自己的 /tmp，容器里的 /tmp 是另一个完全独立的文件系统）。
调用方（web_context.screenshot_page()）用完这个文件要自己删掉；这里另外
在每次截图前顺手清一遍存了太久的孤儿文件兜底，防止调用方哪次异常退出、
没走到删除那一步时文件永远堆在磁盘上。

read_links（2026-09 新增）：跟 read 一样只是"打开页面、读点东西"，只是
读的内容不同——read 用 innerText 拿纯文本，遇到微博热搜这类页面数字标题
挤在一起，还能靠正则从纯文本里抠；但 36氪/虎嗅/IT之家/知乎热榜这类整页
用 JS 渲染列表的站点，每条目真正的原文链接只存在于 <a href> 属性里，
innerText 完全拿不到，之前只能干看着标题猜不出链接。read_links 直接在
页面上取 <a> 标签的文本+href（相对路径在页面里用 new URL() 解析成绝对
地址），调用方（容器里那个取新闻的模块）自己按每个站点的 URL
路径特征去过滤出真正的新闻条目、丢掉导航栏/页脚这些噪音链接——这一层
"哪些链接才是新闻"的站点特定判断留给调用方，这里只负责如实吐出页面上
所有可点的文字+链接，不做任何过滤，保持通用。
"""

import asyncio
import ipaddress
import os
import re
import socket
import time
import uuid
from contextlib import suppress
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from fastapi import FastAPI, Request
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

CDP_URL = os.environ.get("WEB_CDP_URL", "http://127.0.0.1:9223").rstrip("/")
MAX_ITEMS = 10
MAX_TEXT_CHARS = 6000
MAX_QUERY_CHARS = 200
# read_links 是给"一整页热榜/列表"用的，一页可能有几十条新闻混在导航栏/
# 页脚几十个无关链接中间，调用方要自己筛，所以上限比 MAX_ITEMS（search
# 结果条数）大得多。
MAX_LINK_ITEMS = 150

# screenshot 专用配置：见文件头 screenshot 那段注释。目录路径、共享方式跟
# 调用方项目 docker-compose.yml 里的 bind mount 一一对应，改这个常量要
# 同步改那边的挂载路径。
SCREENSHOT_DIR = os.environ.get("WEB_SCREENSHOT_DIR", "/opt/web-tool-screenshots")
# 截图用的视口比其它动作（DEFAULT_VIEWPORT，1280x1400）矮一些——1280x800
# 是用户明确要的尺寸，且更接近真实浏览器窗口比例，首屏截图看起来更自然。
SCREENSHOT_VIEWPORT = {"width": 1280, "height": 800}
DEFAULT_VIEWPORT = {"width": 1280, "height": 1400}
# 孤儿截图文件清理阈值：见 _cleanup_old_screenshots() 注释。
SCREENSHOT_MAX_AGE_SECONDS = 3600

# 跟 douyin-tool 一样，一次只开一个工作页——这个 Chrome 是独享的，没有人工
# 标签页要避让，但同一时刻多个请求抢着连 CDP/开页还是会互相干扰，串成一条
# 队更简单可靠。
ACTION_LOCK = asyncio.Lock()
_CONN_LOCK = asyncio.Lock()
_pw = None
_browser = None
# 常驻观察页，watch-relay 截屏的目标（见文件头注释）。只在 get_page() 里
# 创建/替换，get_context() 整个 CDP 重连时要记得一起清空——不然会挂着一个
# 属于旧 browser 对象的死 Page 引用。
_page = None

app = FastAPI(title="web-tool", version="1.0.0")


def _bounded_int(value, default, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _is_blocked_host(host):
    """SSRF 防护：这个服务只读、绑在 docker 网关地址上，容器那边（web_
    context.py）会把用户随口贴的任意 URL 原样传进来——不能让它被拿去探测
    内网（宿主机上还跑着 douyin-tool:8274、netease-api:8275 这些无鉴权的
    内部服务，以及云主机常见的 169.254.169.254 元数据接口）。解析域名对应
    的 IP，命中私有/回环/链路本地地址一律拒绝，不发起请求——只做 GET 导航
    这一种攻击面（Chrome 页面跳转不会发 POST），但挡住"能不能碰到"比事后
    判断"碰到了返回什么"更安全。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # 解析失败，交给后面真正导航时报错，不在这里假装拦到了什么
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            return True
    return False


def _safe_read_url(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少 url")
    raw = value.strip()
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("只接受 http(s) URL")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("URL 缺少域名")
    if _is_blocked_host(host):
        raise ValueError("这个地址指向内网/本机，不允许访问")
    return raw


async def get_context():
    """连接（或重连）独立的 headless Chrome；这个 Chrome 没有人工在用，
    不存在"别清理人工标签页"的顾虑，但同样不清空已有 context——跟
    douyin-tool 的 get_context() 保持同一个连接管理模式，方便以后对比。"""
    global _pw, _browser, _page
    async with _CONN_LOCK:
        if _browser is not None and _browser.is_connected():
            if not _browser.contexts:
                raise RuntimeError("CDP 已连接但没有浏览器 context")
            return _browser.contexts[0]

        _browser = None
        _page = None  # 旧 browser 断了，挂在它下面的 Page 对象也跟着失效
        if _pw is not None:
            with suppress(Exception):
                await asyncio.wait_for(_pw.stop(), timeout=5)
            _pw = None

        try:
            _pw = await async_playwright().start()
            _browser = await asyncio.wait_for(
                _pw.chromium.connect_over_cdp(CDP_URL), timeout=12
            )
        except Exception as exc:
            _browser = None
            if _pw is not None:
                with suppress(Exception):
                    await asyncio.wait_for(_pw.stop(), timeout=5)
                _pw = None
            raise RuntimeError(f"连接 web-browser-chrome 失败：{exc}") from exc

        if not _browser.contexts:
            raise RuntimeError("CDP 已连接但没有浏览器 context")
        return _browser.contexts[0]


async def _block_heavy(route):
    # 只读正文/标题，不需要图片/字体/媒体真的加载出来——省内存省带宽，
    # 跟 douyin-tool 的 block_heavy 同一个理由。
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


async def _configure_page(page):
    await page.set_viewport_size(DEFAULT_VIEWPORT)
    page.set_default_timeout(20000)
    # navigator.webdriver=true 是另一个常见的"这是自动化"信号（跟 UA 里的
    # HeadlessChrome 是同一类问题，见 web-browser-chrome.service 里
    # --user-agent 那条注释）。--disable-blink-features=AutomationControlled
    # 不是每个 Chrome 版本都彻底生效，这里在页面加载前用 init script 兜底
    # 覆盖掉这个属性。
    await page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    await page.route("**/*", _block_heavy)
    return page


async def new_work_page(context):
    page = await asyncio.wait_for(context.new_page(), timeout=10)
    return await _configure_page(page)


async def get_page(context):
    """返回常驻观察页，不存在/已关闭才新建或复用。跟 douyin-tool 的常驻
    标签页不是同一回事——那边是为了保住登录态/列表页上下文，这边纯粹是给
    watch-relay 一个稳定的画面来源，navigate 到哪个 URL 由每次的 action
    决定，这里不管。

    优先复用 context.pages 里已有的页面（典型情况是 Chrome 自带的默认
    New Tab），而不是无条件 new_page()——2026-09 实测：无条件新建会在
    默认标签之外再多开一个，两个 page target 同时存在时，watch-relay 靠
    CDP /json 挑目标可能选中那个从没被 navigate 过的空白标签，画面一直
    空白。观察页目标只能有一个，才能保证"看的就是 agent 在用的那个"。"""
    global _page
    if _page is not None and not _page.is_closed():
        return _page
    if context.pages:
        _page = await _configure_page(context.pages[0])
    else:
        _page = await new_work_page(context)
    return _page


READ_EXTRACT_JS = r"""
() => {
  const title = document.title || '';
  const text = document.body ? document.body.innerText : '';
  return { title, text };
}
"""

LINKS_EXTRACT_JS = r"""
(max) => {
  const seen = new Set();
  const items = [];
  const anchors = Array.from(document.querySelectorAll('a[href]'));
  for (const a of anchors) {
    if (items.length >= max) break;
    const text = (a.textContent || '').replace(/\s+/g, ' ').trim();
    // 太短的（图标/"更多"/单个箭头）没有信息量，太长的通常是整段摘要被
    // 包在 <a> 里、不是标题——跟调用方（news_context.py）约定"标题应该
    // 落在这个长度区间"，两边配合，不在这里做站点特定判断。
    if (!text || text.length < 6 || text.length > 60) continue;
    const raw = a.getAttribute('href') || '';
    if (!raw || raw.startsWith('#') || raw.toLowerCase().startsWith('javascript:')) continue;
    let href;
    try {
      href = new URL(raw, location.href).href;
    } catch (e) {
      continue;
    }
    const key = text + '|' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    items.push({ text, href });
  }
  return items;
}
"""

SEARCH_EXTRACT_JS = r"""
(max) => {
  const nodes = Array.from(document.querySelectorAll('.result'));
  const items = [];
  for (const node of nodes) {
    if (items.length >= max) break;
    const linkEl = node.querySelector('.result__title a, .result__a');
    const snippetEl = node.querySelector('.result__snippet');
    if (!linkEl) continue;
    const title = (linkEl.textContent || '').trim();
    const url = linkEl.getAttribute('href') || '';
    const snippet = (snippetEl ? snippetEl.textContent : '').trim();
    if (title && url) items.push({ title, url, snippet });
  }
  return items;
}
"""


def _collapse_blank_lines(text):
    return re.sub(r"\n{3,}", "\n\n", text or "").strip()


async def action_read(page, body):
    url = _safe_read_url(body.get("url"))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except PWTimeout as exc:
        raise RuntimeError("打开网页超时") from exc
    # 给 JS 渲染的现代网页一点点缓冲时间——不像 douyin-tool 那样有明确的
    # "抓到接口 JSON 就算数据到齐"信号，通用网页没有统一的完成标志，退而
    # 求其次固定等一小段时间，简单但对大多数站点够用。
    await page.wait_for_timeout(800)

    data = await page.evaluate(READ_EXTRACT_JS)
    text = _collapse_blank_lines(data.get("text") or "")
    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS] + "…"

    return {
        "ok": True,
        "action": "read",
        "url": page.url,
        "title": (data.get("title") or "").strip(),
        "text": text,
        "truncated": truncated,
    }


async def action_read_links(page, body):
    """跟 action_read 一样打开页面、等一小段缓冲时间，但取的是 <a> 标签的
    文本+链接，不是 innerText——见文件头 read_links 那段注释。"""
    url = _safe_read_url(body.get("url"))
    n = _bounded_int(body.get("n"), 80, MAX_LINK_ITEMS)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
    except PWTimeout as exc:
        raise RuntimeError("打开网页超时") from exc
    await page.wait_for_timeout(800)

    title = await page.evaluate("() => document.title || ''")
    links = await page.evaluate(LINKS_EXTRACT_JS, n)

    return {
        "ok": True,
        "action": "read_links",
        "url": page.url,
        "title": (title or "").strip(),
        "links": links,
        "count": len(links),
    }


def _cleanup_old_screenshots():
    """调用方（容器里的那个项目）正常情况下用完一张截图会自己删掉
    （见 web_context.screenshot_page() 的注释），这里只是兜底：万一某次
    调用方在发送截图的过程中异常退出、没走到删除那一步，孤儿文件会一直
    占磁盘——每次真的要生成一张新截图之前顺手扫一遍，删掉超过
    SCREENSHOT_MAX_AGE_SECONDS 还没被清理的旧文件，不需要额外的定时任务/
    cron。扫描目录不存在就顺手建好；单个文件删除失败（权限问题、文件被
    并发删掉）只跳过，不影响这次正常截图。"""
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        now = time.time()
        for name in os.listdir(SCREENSHOT_DIR):
            path = os.path.join(SCREENSHOT_DIR, name)
            try:
                if now - os.path.getmtime(path) > SCREENSHOT_MAX_AGE_SECONDS:
                    os.remove(path)
            except OSError:
                continue
    except Exception:
        pass


async def action_screenshot(page, body):
    """截一张网页的图（首屏或整页），存到 SCREENSHOT_DIR，返回文件路径——
    见文件头 screenshot 那段注释（为什么不是 base64、为什么不是字面意义上
    的 /tmp）。full_page=True 走 Playwright page.screenshot(full_page=True)，
    截整个可滚动页面的完整长图；默认（False）只截当前视口看到的这一屏。
    Playwright 连的是 CDP（见 get_context()），screenshot() 本身就是走
    Chromium DevTools Protocol 的 Page.captureScreenshot，不需要在这里
    自己再手写一遍 CDP 调用。

    等页面"加载完"分两层：先给 networkidle 一个短暂机会（不少现代网页有
    轮询请求/websocket 永远不会真正 idle，等太久没意义，给 3 秒上限），
    再固定等 2 秒兜底——两者是"或"的关系，不要求同时满足，都是为了让
    JS 渲染的内容有时间跑完，截图截到的不是一张还在加载中的白屏。"""
    url = _safe_read_url(body.get("url"))
    full_page = bool(body.get("full_page"))

    await page.set_viewport_size(SCREENSHOT_VIEWPORT)
    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        except PWTimeout as exc:
            raise RuntimeError("打开网页超时") from exc

        with suppress(PWTimeout):
            await page.wait_for_load_state("networkidle", timeout=3000)
        await page.wait_for_timeout(2000)

        _cleanup_old_screenshots()
        path = os.path.join(SCREENSHOT_DIR, f"{uuid.uuid4().hex}.png")
        try:
            await page.screenshot(path=path, full_page=full_page)
        except PWTimeout as exc:
            raise RuntimeError("截图超时（页面可能过长/渲染卡住）") from exc
    finally:
        # 这个观察页是常驻共用的（见 get_page() 注释），不还原视口的话，
        # 下一次别的动作（read/read_links/search）也会用着截图专用的矮
        # 视口，不管这次截图成不成功都要还原。
        with suppress(Exception):
            await page.set_viewport_size(DEFAULT_VIEWPORT)

    return {
        "ok": True,
        "action": "screenshot",
        "url": page.url,
        "full_page": full_page,
        "path": path,
    }


async def action_search(page, body):
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "search 需要非空 query"}
    query = query.strip()
    if len(query) > MAX_QUERY_CHARS:
        return {"ok": False, "error": f"query 最长 {MAX_QUERY_CHARS} 个字符"}
    n = _bounded_int(body.get("n"), 5, MAX_ITEMS)

    # DuckDuckGo 的无 JS HTML 版本——不需要登录、不需要 API key，对无头
    # 访问也比 Google 宽容得多（Google 对无头请求风控很敏感，容易直接拦），
    # 用真实 Chrome 渲染这个页面（虽然它本身不需要 JS）只是为了跟 read 走
    # 同一套连接/资源拦截逻辑，不用另外维护一条纯 requests 的代码路径。
    search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=20000)
    except PWTimeout as exc:
        raise RuntimeError("打开搜索页超时") from exc

    raw_items = await page.evaluate(SEARCH_EXTRACT_JS, n)
    items = [_resolve_ddg_link(item) for item in raw_items]
    return {"ok": True, "action": "search", "query": query, "items": items, "count": len(items)}


def _resolve_ddg_link(item):
    """DuckDuckGo HTML 版结果里的 <a href> 不是真实目标地址，是它自己的
    重定向跳转链接（形如 //duckduckgo.com/l/?uddg=<url编码的真实地址>&rut=...），
    直接把这种链接丢给用户点开体验很差（先跳一次 DDG 域名，而且协议相对
    路径 // 开头缺 scheme）。这里解出 uddg 参数还原成真实 URL；解不出来
    （DDG 改版/极少数没走这套跳转的结果）就原样保留，好歹链接还能点开，
    不因为这一步失败让整条结果消失。"""
    url = item.get("url") or ""
    parsed = urlparse(url if "//" in url.split("?")[0] else "https:" + url)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path == "/l/":
        qs = parse_qs(parsed.query)
        real = qs.get("uddg")
        if real:
            item["url"] = unquote(real[0])
            return item
    if url.startswith("//"):
        item["url"] = "https:" + url
    return item


ACTIONS = {
    "read": action_read,
    "search": action_search,
    "read_links": action_read_links,
    "screenshot": action_screenshot,
}


@app.get("/health")
async def health():
    try:
        context = await get_context()
        return {
            "ok": True,
            "connected": bool(_browser and _browser.is_connected()),
            "existing_tabs": len(context.pages),
            "actions": sorted(ACTIONS),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/web")
async def web(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "请求体必须是 JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "请求体必须是 JSON object"}

    action = body.get("action")
    if action not in ACTIONS:
        return {"ok": False, "error": "不支持的 action", "allowed_actions": sorted(ACTIONS)}

    async with ACTION_LOCK:
        try:
            context = await get_context()
            page = await get_page(context)
            return await ACTIONS[action](page, body)
        except asyncio.CancelledError:
            raise
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": f"{action} 失败：{exc}"}


@app.on_event("shutdown")
async def shutdown():
    """只断开 Playwright 控制连接，不关闭 headless Chrome 本身（它是独立
    systemd 服务常驻的，见 web-browser-chrome.service）。"""
    global _pw, _browser
    _browser = None
    if _pw is not None:
        with suppress(Exception):
            await asyncio.wait_for(_pw.stop(), timeout=5)
        _pw = None
