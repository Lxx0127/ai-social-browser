"""douyin-tool: 抖音只读 HTTP 包装。

连接 browser-relay 的常驻 headed Chrome（CDP 9333），复用人工登录态。
调用方：任何会发 HTTP 的 agent 或脚本。架构上是 xiaohongshu-tool 的同一套
模式搬过来的（CDP 连接管理、孤儿标签页回收、browse session 列表复用、
challenge 识别、频率无关的只读边界），不是另起一套。

POST /douyin
  {"action":"feed", "n":10}
  {"action":"search", "query":"猫", "n":10}
  {"action":"read", "url":"https://www.douyin.com/video/...", "n":10}
  {"action":"profile", "url":"https://www.douyin.com/user/...", "n":10}
  {"action":"user_posts", "sec_uid":"MS4wLjAB...", "n":10}   sec_uid 可省，省了看自己
  {"action":"im_conversations"}                              私信会话列表（只读）
  {"action":"im_messages", "conversation_id":"0:1:a:b", "limit":50}  某个会话的消息（只读）
  {"action":"im_send", "conversation_id":"0:1:a:b", "text":"在忙吗"}   发一条私信（写动作）
  {"action":"post", "video_path":"/abs/path/on/this/host.mp4", "caption":"...", "tags":["猫"]}
  {"action":"post_images", "image_paths":["/abs/path/on/this/host.jpg"], "caption":"...", "tags":["猫"]}

read/profile 的 url 支持 App 分享的 v.douyin.com 短链，会先自动展开成主站
链接（跟 xiaohongshu-tool 处理 xhslink.com 短链是同一个思路：只手动跟
Location 跳转、只向短链域名发请求，绝不代抓最终页面）。

原本的边界：这个文件很长一段时间里是纯只读的——允许为浏览/搜索进行导航、
点击、输入、滚动和数据读取，不包含点赞、关注、评论、发布这类互动写动作。
理由白纸黑字写过：抖音的风控比小红书更激进（公开可观察到的共识），跟
xiaohongshu-tool 当初"只读已经够用，写操作的封号风险和收益不成比例"是
同一个判断。

2026-08-23 加了一个例外：`post`（发布视频）。这是用户在知道上面这条判断、
明确要求用专号承担封号风险的前提下要的功能，不是随手加的——只读边界对
点赞/关注/评论依然成立，这里不打算再加别的互动类写动作。做法照抄 twitter-tool
写动作那一套（三态 outcome、频率闸、actions.log 审计、ChallengeDetected
统一处理），而不是另起一套，见下面"写动作"一节和 action_post()。跟文件
头这条一贯的提醒一样：上传页的选择器同样是没有对着真实登录态跑通过的
最佳猜测，部署后必须先跑一遍真实的 post 冒烟测试。

同一天又加了 `post_images`（发布图文）——调用方那边要支持"发照片给
机器人、机器人自动写文案发抖音"，图文发布跟视频发布是创作者后台完全不同
的一个模式（先切"图文"tab、不需要等转码），不是给 post 加个参数就能糊
过去的，所以单独一个动作，跟 post 共用同一个 publish 频率闸（图文和视频
都算"发布一条作品"，风险量级一样，没必要分开算）。跟 post 一样没有对着
真实登录态跑通过，选择器（尤其是"切到图文 tab"这一步）是最佳猜测，部署
后同样需要冒烟测试校准。

写动作：
  post  [video_path, caption, tags?]  上传本机视频文件并发布，频率闸见
        RATE_LIMITS["publish"]（默认每小时 2 次，比 twitter-tool 的 5 次更
        保守——发视频本身审核更严格，出问题的代价也更高）。video_path 必须
        是 douyin-tool 这个进程能直接读到的**本机绝对路径**（这个服务是
        systemd 起在宿主机上的，不是跑在调用方的 docker 容器里，
        容器内路径在这里访问不到）。设置了 DY_POST_MEDIA_ROOT 时，
        video_path 必须落在这个目录下——给"往这个目录扔视频"这种固定
        约定用，不是必须。
  post_images  [image_paths, caption, tags?]  上传 1~35 张本机图片、切到
        图文模式发布，跟 post 共用同一个 publish 频率闸。image_paths 每一
        项的路径规则跟 video_path 完全一样（本机绝对路径、受
        DY_POST_MEDIA_ROOT 约束）。
  im_send  [conversation_id, text]  往一个已有会话发一条文字私信，频率闸
        见 RATE_LIMITS["im_send"]（默认每小时 6 条，跟发布那个闸分开算：
        发一条私信和发一条公开作品完全不是一个风险量级）。2026-09-05 加的，
        理由跟 post 那条一样是"用户在知道风险的前提下明确要的"：只读边界
        对点赞/关注/评论依然成立。跟 post 不同的是这个动作**验证得到**——
        发完能在 conversationStore 里看见自己那条消息，所以正常路径下拿到的
        是 confirmed 而不是 uncertain，见 action_im_send()。

────────────────────────────────────────────────────────────────
⚠️ 重要提醒（写这份文件时没有真实登录态、没有能实时访问抖音网页版的环境）：

跟 xiaohongshu-tool 不一样的地方先说清楚——小红书的卡片信息大多直接渲染在
语义化的 DOM class 里（.note-item / .author 这种，改版频率相对低），
xiaohongshu-tool 是直接解析这些 DOM。抖音网页版的 CSS class 名基本是编译时
哈希出来的随机串，页面一次不大的构建就可能全部作废，比小红书更不适合硬编码
class 选择器。但抖音的卡片数据本身是结构化 JSON——要么内嵌在首屏 HTML 的
`<script id="RENDER_DATA">` 里，要么客户端滚动加载时走 XHR/fetch 拿。所以
这个文件反过来，**以"抓接口 JSON 数据"为主，DOM 只用来做"页面有没有加载
出来 / 有没有弹登录框 / 有没有验证码"这类粗粒度判断，不用来抠具体字段**——
这是比照抖音数据渲染方式做的架构选择，不是随手照抄小红书。

即便如此，下面这些 URL 结构、接口路径关键词（比如 `/aweme/v1/web/tab/feed`
这类）、RENDER_DATA 里对象的字段名，都是根据抖音网页版公开可观察到的一般
结构写的最佳猜测，**没有对着真实登录态跑通过**。部署后第一件事必须是拿
真实登录态跑一遍 feed/search/read/profile 冒烟测试——跟根 README"选择器
绑定日期，改版后会以等待超时的形式明确失败，不会静默返回错数据"的原则
一致，这里失败也会是清楚的报错，不是"看起来能用但数据是错的"，但大概率
第一次跑不通，需要对着真实页面结构调整这个文件里的接口关键词匹配和字段
映射——这是把这套模式移植到一个新平台必经的一步，不是这份代码本身有 bug。
────────────────────────────────────────────────────────────────
"""

import asyncio
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, Request
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright


CDP_URL = os.environ.get("DY_CDP_URL", "http://127.0.0.1:9333").rstrip("/")
BASE = "https://www.douyin.com"
CREATOR_BASE = "https://creator.douyin.com"
MAX_ITEMS = 30
MAX_COMMENTS = 40
BROWSE_TTL_SECONDS = max(30, int(os.environ.get("DY_BROWSE_TTL_SECONDS", "180")))

# ================= 写动作（post）基础设施 =================
# 跟 twitter-tool 的 actions.log / RATE_LIMITS / 三态 outcome 是同一套模式，
# 见文件头 2026-08-23 那条说明——douyin-tool 只加了这一个写动作，其它互动
# 类写操作依然不做。

LOG_PATH = Path(__file__).parent / "actions.log"
STATE_PATH = Path(os.environ.get(
    "DY_STATE_PATH", str(Path(__file__).parent / "runtime-state.json")))

RATE_LIMITS = {
    "publish": max(1, int(os.environ.get("DY_POST_HOURLY_LIMIT", "2"))),
    # 私信跟发布分开算，两件事的风险量级差得远：发一条公开作品是"全网
    # 可见 + 要过审核"，发一条私信只是跟一个已经互关的人说句话。这个闸
    # 也不是用来限制正常聊天的（轮询默认十分钟一次、一轮最多回一条，正常
    # 用法一小时也就 6 条），是防"某个 bug 让同一条消息被反复发出去"这种
    # 失控情况把对面刷屏，见 action_im_send()。
    "im_send": max(1, int(os.environ.get("DY_IM_HOURLY_LIMIT", "6"))),
}
RATE_GROUP = {"post": "publish", "post_images": "publish", "im_send": "im_send"}
_rate_windows = {g: deque() for g in RATE_LIMITS}  # epoch seconds，需跨重启保留
_state_loaded = False

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".flv", ".webm", ".mkv"}
MAX_VIDEO_MB = int(os.environ.get("DY_POST_MAX_VIDEO_MB", "4096"))
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# 35 是抖音图文发布公开可观察到的一般上限，没有对着真实账号验证过精确
# 数字，只是防止传一个离谱大的列表把上传页面卡死。
MAX_IMAGES_PER_POST = 35
MAX_IMAGE_MB = int(os.environ.get("DY_POST_MAX_IMAGE_MB", "50"))
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
POST_MEDIA_ROOT = os.environ.get("DY_POST_MEDIA_ROOT", "").strip()
# 抖音描述框实际字数上限没有对着真实页面验证过，这里给一个保守估计，超了
# 明确报错而不是让页面截断成"看起来发出去了但内容被截断"的静默错误。
MAX_CAPTION_CHARS = 1000


def _now_cn():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def log_action(entry: dict):
    """写动作审计日志——跟 twitter-tool 的 actions.log 同一个约定：每次写
    动作（含参数、结果）落一行 JSON，出了事有账可查。"""
    entry["ts"] = _now_cn()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _persist_state():
    """频率窗口和自有 target 台账一起原子落盘。

    台账必须落盘，理由见 _owned_targets 上面那段：服务重启时留下的孤儿页是最
    常见的一类，只存在内存里的话重启后就永远认不出来了。绝不记录人工标签页。"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(f".{STATE_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(
        json.dumps({"rate_windows": {g: list(w) for g, w in _rate_windows.items()},
                    "owned_targets": _owned_targets},
                    ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, STATE_PATH)


def _load_state():
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except Exception as e:
        log_action({"action": "_state_load_failed", "error": str(e)})
        return
    now = time.time()
    raw_windows = payload.get("rate_windows") or {}
    for group in RATE_LIMITS:
        values = raw_windows.get(group) or []
        _rate_windows[group] = deque(
            float(ts) for ts in values
            if isinstance(ts, (int, float)) and 0 <= now - float(ts) <= 3600
        )
    # 重启前登记过的 target 原样读回来——启动时的那次清扫（见 startup()）就是
    # 靠这份台账认出"上一条命留下的孤儿页"的。
    raw_targets = payload.get("owned_targets") or {}
    if isinstance(raw_targets, dict):
        for target_id, meta in raw_targets.items():
            if isinstance(target_id, str) and isinstance(meta, dict):
                _owned_targets[target_id] = {
                    "created_at": float(meta.get("created_at") or now),
                    "last_active_at": float(meta.get("last_active_at") or now),
                }


def check_rate(action: str):
    group = RATE_GROUP.get(action)
    if not group:
        return None
    win = _rate_windows[group]
    now = time.time()
    changed = False
    while win and now - win[0] > 3600:
        win.popleft()
        changed = True
    if changed:
        _persist_state()
    if len(win) >= RATE_LIMITS[group]:
        wait_min = int((3600 - (now - win[0])) / 60) + 1
        return f"频率闸：{group} 组最近 1 小时已用满 {RATE_LIMITS[group]} 次，约 {wait_min} 分钟后再试"
    return None


def record_rate(action: str):
    group = RATE_GROUP.get(action)
    if group:
        _rate_windows[group].append(time.time())
        _persist_state()

# 抖音页面比小红书更重（视频优先级高），VPS 上同样一次只开一个工作页，
# 避免和人工 relay 页面抢内存——跟 xiaohongshu-tool 的 ACTION_LOCK 同一个考虑。
ACTION_LOCK = asyncio.Lock()
_PAGE_LOCK = asyncio.Lock()
_CONN_LOCK = asyncio.Lock()
_pw = None
_browser = None
_browse_session = None  # 最近一次 search/profile 列表页（feed 不用，见下方 action_feed 注释）
_sweeper_task = None

# ================= 自有标签页台账（孤儿回收）=================
# 2026-09-05 从 twitter-tool 搬过来的，那边叫 _owned_targets/_sweep_owned_orphans。
# 为什么非搬不可，先说清楚这个故障长什么样：
#
# relay 那台 Chrome 是**跟人共用**的（手机接管的那些标签页也在同一个浏览器里），
# 而它的 systemd drop-in 给了 MemoryHigh=1300M。实测一个抖音工作页要 0.55~1.1G，
# 也就是说**一个页勉强够用、两个页必死**：cgroup 一顶到 MemoryHigh，内核就疯狂
# 回收，Chrome 主进程再也调度不动，CDP 变成"TCP 连得上、但任何请求永远不回包"。
# 表现是抖音相关功能全线超时，而且没有任何一条报错指向内存——2026-09-05 一天
# 之内撞了三次，每次都要人工去杀进程才能恢复。
#
# 孤儿页从哪来：调试脚本被 kill -9（Playwright 建的页留在 Chrome 里没人关）、
# 服务重启时正好有请求在飞、动作执行到一半抛异常而 finally 没能关掉。这些页
# 一旦留下就永远不会自己消失，下一次动作再开一个就是两个页。
#
# 关键约束：**绝不能按 URL 猜归属去关页**。这个浏览器里可能有人正开着抖音在
# 手动操作，关掉别人的页比留着孤儿页糟糕得多。所以走台账：每开一个页就把它的
# CDP target id 登记下来，只关登记过的。台账跟频率窗口一起落盘（见
# _persist_state），这样**服务重启也能认出重启前留下的孤儿**——这恰恰是最常见
# 的那一类，只存在内存里等于白做。
_owned_targets = {}  # target_id -> {created_at, last_active_at}，落盘，跨重启有效
_owned_pages = {}    # id(page) -> {page, target_id}，只在内存里，进程内用

# 自有页静默超过这么久就当孤儿收掉。默认 30 分钟：比任何一个正常动作都长得多
# （最慢的 read 也就一两分钟，常驻私信页有自己更短的 TTL，见 IM_PAGE_TTL_SECONDS），
# 所以正常在用的页不可能被误伤；同时又比"人走开一会儿再回来"短，孤儿不会留一
# 整天。跟 twitter-tool 的 TW_ORPHAN_MINUTES 同名同义，最小值也一样卡 30。
ORPHAN_MINUTES = max(30.0, float(os.environ.get("DY_ORPHAN_MINUTES", "30")))

app = FastAPI(title="douyin-tool", version="1.0.0")


class ChallengeDetected(RuntimeError):
    """页面进入验证码/风控拦截；由路由层统一返回结构化状态。"""

    def __init__(self, result):
        super().__init__(result["hint"])
        self.result = result


DY_CHALLENGE_PATH_MARKERS = (
    "/captcha", "/challenge", "/verify", "/punish", "/security",
)
DY_CHALLENGE_SELECTORS = ", ".join((
    'iframe[src*="captcha" i]',
    'iframe[src*="verify" i]',
    '[id*="captcha" i]',
    '[class*="captcha" i]',
    '[class*="verify-wrap" i]',
    '[class*="secsdk-captcha" i]',
    '[class*="yidun" i]',
    '[class*="geetest" i]',
))


def _challenge_result(url):
    return {
        "ok": False,
        "outcome": "challenge",
        "status": "challenge",
        "changed": False,
        "retry_safe": False,
        "platform": "douyin",
        "url": url,
        "hint": "检测到抖音验证码/风控拦截，请用手机接管 browser-relay 完成验证后再试",
    }


async def _raise_if_challenge(page):
    path = urlparse(page.url or "").path.lower()
    if any(marker in path for marker in DY_CHALLENGE_PATH_MARKERS):
        raise ChallengeDetected(_challenge_result(page.url))
    try:
        matches = page.locator(DY_CHALLENGE_SELECTORS)
        for index in range(min(await matches.count(), 5)):
            if await matches.nth(index).is_visible():
                raise ChallengeDetected(_challenge_result(page.url))
    except ChallengeDetected:
        raise
    except Exception:
        return


async def _guarded_click(page, locator, **kwargs):
    await _raise_if_challenge(page)
    try:
        await locator.click(**kwargs)
    except PWTimeout:
        # 点击超时最常见的真实原因是"身份验证/登录"遮罩延迟出现、盖住了目标
        # 元素（见 2026-08-20 实测：登录态好好的，但账号被抖音风控要求二次
        # 验证时会弹出一个 id="login-full-panel-..." 的全屏遮罩，且这个遮罩
        # 经常是在 _login_error() 的前置检查跑完之后才渲染出来的，属于时序
        # 竞争，不是选择器真的失效）——这里补一次检查，命中就换成人能看懂的
        # 提示，而不是把原始的 Playwright 重试日志原样甩给调用方。检查不到
        # 登录问题时说明确实是别的原因（真的选择器失效/元素被其它东西挡住），
        # 照常把原始异常抛出去，不吞掉真实信息。
        login_error = await _login_error(page)
        if login_error:
            raise RuntimeError(login_error) from None
        raise
    await _raise_if_challenge(page)


async def _human_type(page, text):
    for char in text:
        delay = random.randint(20, 80)
        if char.isascii():
            await page.keyboard.type(char, delay=delay)
        else:
            await page.keyboard.insert_text(char)
            await asyncio.sleep(delay / 1000)
        if char in "，。！？,.!?\n" or random.random() < 0.06:
            await asyncio.sleep(random.uniform(0.12, 0.36))


def _bounded_int(value, default, maximum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _safe_dy_url(value, kind="any"):
    """只允许抖音 Web 主站 URL，避免把服务变成任意 URL 抓取器——跟
    xiaohongshu-tool 的 _safe_xhs_url 同一个安全阀。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺少抖音 URL")
    raw = value.strip()
    if raw.startswith("/"):
        raw = BASE + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in {
        "douyin.com",
        "www.douyin.com",
    }:
        raise ValueError("只接受 https://www.douyin.com/ 下的 URL")
    path = parsed.path or "/"
    if kind == "video" and "/video/" not in path:
        raise ValueError("这不是可识别的抖音视频 URL")
    if kind == "profile" and "/user/" not in path:
        raise ValueError("这不是可识别的抖音用户主页 URL")
    return raw.replace("http://", "https://", 1)


# 抖音的 sec_uid 是一串以 MS4wLjAB 开头的 base64-ish 字符串（实测两个真实
# 账号分别是 76 和 55 个字符，长度并不固定，所以只卡一个宽松的上下界，不写
# 死长度）。这里跟 _safe_dy_url() 是同一类安全阀，作用也一样：user_posts 会
# 拿它去拼 https://www.douyin.com/user/<sec_uid>，不校验的话调用方塞一个
# "../../xxx" 或者带 ?# 的字符串进来就能把请求拐到别的路径上去，等于把这个
# 服务变成任意抖音页面抓取器。白名单字符集（字母数字 _ -）本身就排除了 /
# ? # : 这些能改变 URL 结构的字符，比事后 quote 更直接。
_SEC_UID_RE = re.compile(r"^MS4wLjAB[A-Za-z0-9_-]{20,200}$")


def _safe_sec_uid(value):
    """校验并返回规整后的 sec_uid，不合法直接抛 ValueError（跟
    _safe_dy_url() 的错误约定一致，调用方统一转成 {"ok": False, ...}）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sec_uid 必须是非空字符串")
    raw = value.strip()
    if not _SEC_UID_RE.match(raw):
        raise ValueError(
            "sec_uid 格式不对：应该是抖音主页链接 /user/ 后面那一串以 MS4wLjAB "
            "开头的字符串，不是抖音号、也不是 uid 数字"
        )
    return raw


_SHORT_LINK_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_SHORT_LINK_OPENER = urllib.request.build_opener(_NoRedirect())


def _is_short_link(value):
    if not isinstance(value, str):
        return False
    host = (urlparse(value.strip()).hostname or "").lower()
    return host == "v.douyin.com" or host.endswith(".v.douyin.com")


def _expand_short_link_sync(raw):
    """把 App 分享的 v.douyin.com 短链展开成主站链接。手动跟 Location 跳转：
    只向短链域名发请求，见到主站链接立刻返回，绝不抓取最终页面；中途跳到
    白名单外的域名一律拒绝——跟 xiaohongshu-tool 的
    _expand_short_link_sync 完全同一个套路。"""
    url = raw.strip()
    for _ in range(5):
        host = (urlparse(url).hostname or "").lower()
        if host in {"douyin.com", "www.douyin.com"}:
            return url
        if not (host == "v.douyin.com" or host.endswith(".v.douyin.com")):
            raise ValueError(f"短链跳转到了白名单外的域名：{host or '(空)'}")
        request = urllib.request.Request(
            url, headers={"User-Agent": _SHORT_LINK_UA}, method="GET")
        location = None
        try:
            with _SHORT_LINK_OPENER.open(request, timeout=8) as resp:
                location = resp.headers.get("Location")
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400 and exc.headers:
                location = exc.headers.get("Location")
            else:
                raise ValueError(f"展开短链失败：HTTP {exc.code}") from exc
        except Exception as exc:
            raise ValueError(f"展开短链失败：{exc}") from exc
        if not location:
            raise ValueError("短链没有返回跳转目标，可能已失效")
        url = urljoin(url, location)
    raise ValueError("短链跳转次数过多")


async def _resolve_short_link(raw):
    if _is_short_link(raw):
        return await asyncio.to_thread(_expand_short_link_sync, raw)
    return raw


def _video_id_from_url(url):
    path = urlparse(url).path.rstrip("/")
    match = re.search(r"/video/(\d+)", path)
    return match.group(1) if match else None


# ================= 数据抽取：以接口 JSON 为主，DOM 只做粗粒度判断 =================
# 见文件头的架构说明。RENDER_DATA 是首屏 SSR 塞进 HTML 的数据，滚动加载后续
# 内容走的是 XHR/fetch，两边返回的视频对象（aweme）字段形状是一致的（同一套
# 后端数据结构），所以用同一个 _find_aweme_dicts / _normalize_aweme 处理。

RENDER_DATA_EXTRACT_JS = r"""
() => {
  const el = document.querySelector('script#RENDER_DATA, script#RENDER_DATA__');
  if (!el || !el.textContent) return null;
  try {
    return JSON.parse(decodeURIComponent(el.textContent));
  } catch (_) {
    try { return JSON.parse(el.textContent); } catch (_) { return null; }
  }
}
"""


def _find_aweme_dicts(node, found=None, depth=0):
    """RENDER_DATA / 接口响应内部的字段路径经常随抖音改版变化，硬编码一条
    路径（比如 root['xxx']['awemeList']）很容易随时失效。这里改成递归扫描
    整棵 JSON 树，找"看起来像一个视频对象"的 dict（同时具备 id 类字段和
    desc/统计类字段）——只要数据里还塞着视频信息，不管嵌套在哪个 key 下面
    都能找到；真的找不到时，需要回来看是不是抖音把这两类字段都改名了。"""
    if found is None:
        found = []
    if depth > 14:
        return found
    if isinstance(node, dict):
        vid = node.get("aweme_id") or node.get("awemeId") or node.get("id")
        has_signal = (
            "desc" in node or "description" in node
            or "statistics" in node or "stats" in node
        )
        if vid and has_signal and isinstance(vid, (str, int)):
            found.append(node)
        for value in node.values():
            _find_aweme_dicts(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _find_aweme_dicts(item, found, depth + 1)
    return found


def _url_list_first(obj):
    if not isinstance(obj, dict):
        return None
    urls = obj.get("url_list") or obj.get("urlList")
    if isinstance(urls, list) and urls:
        return urls[0]
    return obj.get("url")


def _normalize_aweme(raw):
    author = raw.get("author") or {}
    stats = raw.get("statistics") or raw.get("stats") or {}
    video = raw.get("video") or {}
    aweme_id = str(raw.get("aweme_id") or raw.get("awemeId") or raw.get("id") or "").strip()
    sec_uid = author.get("sec_uid") or author.get("secUid")
    cover = (
        _url_list_first(video.get("cover"))
        or _url_list_first(video.get("originCover"))
        or _url_list_first(video.get("origin_cover"))
        or raw.get("cover")
    )
    return {
        "id": aweme_id or None,
        "url": f"{BASE}/video/{aweme_id}" if aweme_id else None,
        "desc": raw.get("desc") or raw.get("description") or "",
        "cover": cover,
        "type": "video",
        "author": author.get("nickname") or author.get("nickName") or "",
        "author_id": sec_uid,
        "author_url": f"{BASE}/user/{sec_uid}" if sec_uid else None,
        "avatar": _url_list_first(author.get("avatar_thumb") or author.get("avatarThumb")),
        "stats": {
            # play_count 单独说一句：抖音网页接口对"别人的作品"确实会返回
            # 这个字段，但拿真实登录态实测过，返回的每一条都是 0——网页端
            # 根本不下发真实播放量，真实播放数只有创作者本人在
            # creator.douyin.com 后台看得到。这里照实透传（是 0 就是 0，不
            # 改写成 None、也不假装拿不到），由调用方自己判断"全是 0 就别
            # 提播放量"，不要在这一层擅自加工。
            "plays": stats.get("play_count", stats.get("playCount")),
            "likes": stats.get("digg_count", stats.get("diggCount")),
            "comments": stats.get("comment_count", stats.get("commentCount")),
            "shares": stats.get("share_count", stats.get("shareCount")),
            "collects": stats.get("collect_count", stats.get("collectCount")),
        },
        "duration_ms": video.get("duration"),
        "create_time": raw.get("create_time") or raw.get("createTime"),
    }


def _normalize_comment(raw):
    user = raw.get("user") or {}
    cid = str(raw.get("cid") or raw.get("comment_id") or raw.get("id") or "").strip()
    return {
        "id": cid or None,
        "content": raw.get("text") or raw.get("content") or "",
        "created_at": raw.get("create_time") or raw.get("createTime"),
        "ip_location": raw.get("ip_label") or raw.get("ipLabel"),
        "likes": raw.get("digg_count", raw.get("diggCount")),
        "liked_by_me": bool(raw.get("user_digged") or raw.get("userDigged")),
        "user": {
            "nickname": user.get("nickname") or user.get("nickName") or "",
            "user_id": user.get("sec_uid") or user.get("secUid"),
        },
        "sub_comment_count": raw.get("reply_comment_total") or raw.get("replyCommentTotal") or 0,
    }


def _collect_aweme_from_json(payload, out, seen):
    for raw in _find_aweme_dicts(payload):
        item = _normalize_aweme(raw)
        if item["id"] and item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)


def _find_comment_dicts(node, found=None, depth=0):
    if found is None:
        found = []
    if depth > 10:
        return found
    if isinstance(node, dict):
        cid = node.get("cid") or node.get("comment_id") or node.get("id")
        has_signal = "text" in node or "content" in node
        if cid and has_signal:
            found.append(node)
        for value in node.values():
            _find_comment_dicts(value, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _find_comment_dicts(item, found, depth + 1)
    return found


def _collect_comments_from_json(payload, out, seen):
    for raw in _find_comment_dicts(payload):
        item = _normalize_comment(raw)
        if item["id"] and item["id"] not in seen:
            seen.add(item["id"])
            out.append(item)


def _make_json_capture(page, url_markers, on_payload):
    """挂一个响应监听：URL 里含任一关键词、且是 JSON 响应，就把 body 交给
    on_payload 处理。抖音接口路径/版本号（v1/v2 之类）随时可能变，这里用
    "URL 里含有这几个关键词子串"而不是精确路径匹配，尽量对版本号跳动免疫；
    真的抓不到东西时，第一件事是打开浏览器 DevTools 网络面板核对真实关键词。
    """
    async def on_response(response):
        url = response.url
        if not any(marker in url for marker in url_markers):
            return
        content_type = response.headers.get("content-type") or ""
        if "json" not in content_type:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        try:
            on_payload(payload)
        except Exception:
            pass

    page.on("response", on_response)
    return on_response


_EMPTY_TEXT_MARKERS = (
    "没有找到相关内容", "暂无更多", "还没有发布作品", "TA还没有发布", "无搜索结果",
    "抱歉，没有找到", "内容不存在",
)


async def _page_has_empty_marker(page):
    try:
        body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return False
    return any(marker in (body_text or "") for marker in _EMPTY_TEXT_MARKERS)


async def _login_error(page):
    """仅在内容确实加载不出来时判断登录态，避免把页面上出现的"登录"字样
    当成掉线——跟 xiaohongshu-tool 的 _login_error 同一个原则。"""
    if "/login" in page.url:
        return "登录态失效：请在中继浏览器（browser-relay）里重新人工登录抖音"
    # 必须判"可见"，不能只判"存在"：2026-09-05 实测，视频/图文详情页
    # （/video/、/note/）的 DOM 里常驻一个 <div class="disturb-login-panel">，
    # 登录态好好的时候它也在，只是尺寸 0x0、根本没显示出来。只用
    # querySelector 判存在的话，read 动作对**任何**一条视频都会误报"登录态
    # 失效"（feed/profile 不受影响，因为那些页面没有这个元素），等于这个动作
    # 整个不可用。真的掉线时弹出来的登录遮罩是可见的，加上可见性判断既修好
    # 了误报、又不会漏掉真的掉线。判据跟 _raise_if_challenge() 里用 is_visible()
    # 是同一个思路，这里因为要一次判一批元素，直接在页面里算 rect 更省事。
    login_visible = await page.evaluate(
        """() => {
          const nodes = document.querySelectorAll(
            '[class*="login-panel" i], [class*="login-guide" i], [class*="loginDialog" i], [class*="login-mask" i],'
            + '[id*="login-full-panel" i], [id*="login-panel" i]'
          );
          for (const el of nodes) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden') return true;
          }
          return false;
        }"""
    )
    if login_visible:
        return "登录态失效：请在中继浏览器（browser-relay）里重新人工登录抖音"
    return None


async def _wait_for_collected(page, collected_ref, timeout_ms=18000):
    """轮询等待网络抓取拿到至少一条数据；期间持续检查登录态/风控/空态。
    collected_ref 是个 list（就地被 _collect_aweme_from_json 追加），不是
    靠某个 DOM 选择器出现来判断"加载完了"——抖音没有一个稳定 class 能拿来
    等，用"有没有抓到数据"代替。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        await _raise_if_challenge(page)
        if collected_ref:
            return None
        login = await _login_error(page)
        if login:
            return login
        if await _page_has_empty_marker(page):
            return "EMPTY"
        await page.wait_for_timeout(300)
    if await _page_has_empty_marker(page):
        return "EMPTY"
    return (
        "等待抖音数据超时：可能是网络慢，或者接口路径/RENDER_DATA 结构已变化"
        "（这是本文件里最需要对着真实登录态校准的一处，见文件头注释）"
    )


async def _scroll_to_load_more(page, collected_ref, n, rounds=15):
    for _ in range(rounds):
        if len(collected_ref) >= n:
            break
        await _raise_if_challenge(page)
        await page.mouse.wheel(0, random.randint(700, 1150))
        await page.wait_for_timeout(random.randint(500, 950))
        await _raise_if_challenge(page)


def _close_owned_targets_sync(target_ids):
    """通过 CDP 的 HTTP 接口关掉指定 target。**只关传进来的这些 id**，调用方
    保证它们都来自 _owned_targets，这里不做任何"看着像我们的页"式的推断。

    用同步的 urllib 而不是 Playwright：这个函数存在的意义就是"Playwright 那条
    路已经不好使了"（页面卡死、连接断了、进程重启后压根没有对应的 Page 对象），
    走 CDP 的 HTTP 接口是不依赖任何既有连接的最后一条路。调用方负责丢进
    run_in_executor，别在事件循环里同步阻塞。"""
    report = {"closed": [], "missing": [], "errors": []}
    wanted = set(target_ids)
    if not wanted:
        return report
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json", timeout=5) as resp:
            targets = json.load(resp)
        pages = {t.get("id") for t in targets if t.get("type") == "page"}
        for target_id in wanted:
            # 已经不在了（人工关掉了、Chrome 重启过）也算"处理完了"，从台账里
            # 摘掉即可，不算错误。
            if target_id not in pages:
                report["missing"].append(target_id)
                continue
            try:
                with urllib.request.urlopen(
                        f"{CDP_URL}/json/close/{target_id}", timeout=5):
                    pass
                report["closed"].append(target_id)
            except Exception as e:
                report["errors"].append({"target_id": target_id, "error": str(e)})
    except Exception as e:
        report["errors"].append({"error": str(e)})
    return report


async def _sweep_owned_orphans():
    """把静默超过 ORPHAN_MINUTES 的自有页收掉。返回被处理的 target id 列表。

    判据只有一个：这个 target 在台账里，而且 last_active_at 太老了。不看 URL、
    不看标题——那些都是猜，猜错就是关掉别人正在用的页。"""
    cutoff = time.time() - ORPHAN_MINUTES * 60
    stale = [target_id for target_id, meta in list(_owned_targets.items())
             if float(meta.get("last_active_at") or 0) <= cutoff]
    if not stale:
        return []
    result = await asyncio.get_running_loop().run_in_executor(
        None, _close_owned_targets_sync, stale)
    resolved = set(result.get("closed", [])) | set(result.get("missing", []))
    for target_id in resolved:
        _owned_targets.pop(target_id, None)
        for key, rec in list(_owned_pages.items()):
            if rec.get("target_id") == target_id:
                _owned_pages.pop(key, None)
    _persist_state()
    if result.get("closed"):
        print(f"[tabs] 回收孤儿标签页 {len(result['closed'])} 个", flush=True)
    return result


async def _page_target_id(context, page):
    """读这个 Page 自己的 CDP target id。

    必须精确读出来，不能靠"我刚建了一个 target，那么下一个出现的 Page 就是它"
    这种时序推断——并发或者人工同时开页的时候会认错，而认错的后果是把别人的
    标签页登记成自己的，之后被当孤儿关掉。"""
    session = await context.new_cdp_session(page)
    try:
        info = await session.send("Target.getTargetInfo")
        return (info.get("targetInfo") or {}).get("targetId")
    finally:
        with suppress(Exception):
            await session.detach()


async def _find_page_by_target_id(context, target_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for candidate in reversed(context.pages):
            try:
                if await _page_target_id(context, candidate) == target_id:
                    return candidate
            except Exception:
                continue
        await asyncio.sleep(0.05)
    raise RuntimeError(f"创建 target {target_id} 之后找不到对应的 Page，拒绝接管别的标签页")


def _register_owned_page(page, target_id):
    now = time.time()
    _owned_targets[target_id] = {"created_at": now, "last_active_at": now}
    _owned_pages[id(page)] = {"page": page, "target_id": target_id}
    _persist_state()


def _touch_owned_page(page):
    """这个页刚被用过，把静默计时清零。常驻的那两个页（私信页、列表页）靠这个
    避免在还在用的时候被当成孤儿收掉。"""
    rec = _owned_pages.get(id(page))
    if not rec:
        return
    target_id = rec.get("target_id")
    if target_id in _owned_targets:
        _owned_targets[target_id]["last_active_at"] = time.time()
        _persist_state()


async def _close_owned_page(page):
    """关掉一个自有页并从台账里摘掉。

    page.close() 失败（页面卡死、连接断了）时退回 CDP HTTP 直接关 target——
    正是这种"Playwright 已经不好使了"的情况最容易留下孤儿，不能就这么算了。
    两条路都失败就把这个 target 留在台账里，等定期清扫再试一次。"""
    rec = _owned_pages.pop(id(page), None)
    target_id = rec.get("target_id") if rec else None
    resolved = False
    if page is not None and not page.is_closed():
        try:
            await asyncio.wait_for(page.close(), timeout=5)
            resolved = True
        except Exception as e:
            print(f"[tabs] page.close() 失败，改走 CDP：{e!r}", flush=True)
    else:
        resolved = True

    if target_id and not resolved:
        fallback = await asyncio.get_running_loop().run_in_executor(
            None, _close_owned_targets_sync, [target_id])
        resolved = (target_id in fallback.get("closed", [])
                    or target_id in fallback.get("missing", []))

    if target_id and resolved:
        _owned_targets.pop(target_id, None)
    _persist_state()


async def get_context():
    """连接或重连常驻 Chrome；不清理、不关闭任何既有人工标签页——跟
    xiaohongshu-tool 的 get_context 完全一致。"""
    global _pw, _browser
    async with _CONN_LOCK:
        if _browser is not None and _browser.is_connected():
            if not _browser.contexts:
                raise RuntimeError("CDP 已连接但没有浏览器 context")
            return _browser.contexts[0]

        _browser = None
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
            raise RuntimeError(f"连接中继浏览器失败：{exc}") from exc

        if not _browser.contexts:
            raise RuntimeError("CDP 已连接但没有浏览器 context")
        return _browser.contexts[0]


async def new_work_page(context):
    """创建自己的后台页并登记进台账；调用方必须在 finally 中用
    _close_owned_page() 关掉这一页。

    2026-09-05 改用"建 target -> 按 target id 精确找到对应 Page"，替掉原来的
    `context.expect_page()`：那个是靠"我建了一个页，那么接下来出现的第一个新
    Page 就是它"的时序推断，人工同时开页时会认错人，而认错的后果是把别人的
    标签页登记成自己的、稍后被当孤儿关掉。做法跟 twitter-tool 一致。"""
    page = None
    target_id = None
    async with _PAGE_LOCK:
        session = None
        try:
            session = await _browser.new_browser_cdp_session()
            created = await session.send(
                "Target.createTarget", {"url": "about:blank", "background": True}
            )
            target_id = created.get("targetId")
            if not target_id:
                raise RuntimeError("创建工作页成功但拿不到 target id")
            page = await _find_page_by_target_id(context, target_id)
        except Exception:
            # 走到这里说明"建 target 成功但没能找到对应的 Page"（或者建都没建
            # 成）。前一种情况下那个 target 已经真的在浏览器里开着了，而我们
            # 手上没有 Page 对象、也还没登记进台账——不管它就是一个既没人用、
            # 又永远不会被回收的孤儿页，正是这套机制要消灭的东西。所以先按
            # id 把它关掉再走退路。
            if target_id:
                with suppress(Exception):
                    await asyncio.get_running_loop().run_in_executor(
                        None, _close_owned_targets_sync, [target_id])
            # 退路：直接开一页，再把它自己的 target id 读出来登记。读不到就不
            # 登记——宁可这一页进不了台账（最坏是留个孤儿等人工清），也不能
            # 拿一个不确定的 id 去登记，那可能对应着别人的页。
            page = await asyncio.wait_for(context.new_page(), timeout=10)
            target_id = None
            with suppress(Exception):
                target_id = await _page_target_id(context, page)
        finally:
            if session is not None:
                with suppress(Exception):
                    await session.detach()

    if target_id:
        _register_owned_page(page, target_id)
    else:
        print("[tabs] 这一页拿不到 target id，没能登记进台账", flush=True)

    await page.set_viewport_size({"width": 1200, "height": 1360})
    page.set_default_timeout(18000)

    async def block_heavy(route):
        # 卡片/详情元数据（标题、作者、点赞数、封面图 URL）从接口 JSON 和
        # 图片 <img> 属性拿，不依赖视频真的解码播放——把体积最大的视频流
        # 也当 media 一起拦掉，比只拦 font 能多省不少带宽/内存。
        if route.request.resource_type in ("media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", block_heavy)
    return page


# ================= 私信（IM）=================
# 私信动作全部只读 window.conversationStore，**不打开任何会话、不解析 DOM**。
#
# 这条路是 2026-09-05 探出来的，比"进会话解析 DOM"好在两处，值得写清楚免得
# 以后有人"优化"回去：
#   1. 不进会话就不发已读回执。进会话读消息会把消息标成已读，对方那头看得见
#      "已读"——那是个写操作，不该由一个轮询任务默默替人做掉。
#   2. store 里的消息是结构化的：conversation.getMessageList() 同步返回一个
#      数组，每条带 content(JSON 字符串) / sender / secSender / type /
#      serverId / createdAt(Date)，比从聊天气泡里抠文本稳得多，也不会因为
#      抖音改个 class 名就全线失效。
#
# IM 页复用一个常驻标签页，不像别的动作那样每次 new_work_page：私信是轮询
# 型调用（默认十分钟一次），每次开关一个抖音首页标签页对 relay 那台 Chrome
# 是实打实的负担（实测它常年占 3.5G+/7.2G）。这一页闲置超过 IM_PAGE_TTL_SECONDS
# 就由 _session_sweeper() 收掉，不会永久占着内存。
# 默认 300 秒：比轮询间隔（默认 10 分钟）短，也就是说两次轮询之间这一页会被
# 收掉，每次轮询重新建一页。这是拿"每轮多花 10~15 秒"换"平时不常驻约 1GB"——
# 实测这一页即使掐掉图片/媒体/字体，抖音 SPA 自身的 JS 堆加 IM 长连接仍然占
# 0.7~1.1GB，而 relay 那台机器总共 7.2G、基线就已经 3.4G。想换回"常驻换速度"
# 把这个值调到大于轮询间隔即可（比如 900）。
IM_PAGE_TTL_SECONDS = max(60, int(os.environ.get("DY_IM_PAGE_TTL_SECONDS", "300")))
_im_page = None
_im_page_touched_at = 0.0


def _im_page_alive():
    return bool(_im_page and not _im_page.is_closed())


async def _close_im_page(reason):
    global _im_page
    page, _im_page = _im_page, None
    if page is not None:
        with suppress(Exception):
            await _close_owned_page(page)
    if reason:
        print(f"[im] 私信页已关闭：{reason}", flush=True)


async def _get_im_page(context):
    """拿到那个常驻私信页；不存在/已被关掉/被导航走了就重建。"""
    global _im_page, _im_page_touched_at
    if _im_page_alive():
        # 页面还在，但可能被别的东西导航走了（理论上不会，这一页只有这里用）——
        # 不在抖音主站上就重建，免得后面 evaluate 读到的是别的站点的 window。
        current = _im_page.url or ""
        if current.startswith(BASE):
            _im_page_touched_at = time.time()
            return _im_page
        await _close_im_page("页面被导航到了非抖音地址")

    page = await new_work_page(context)

    # 这一页比别的工作页要再省一大截：它是**常驻**的（轮询期间一直开着），
    # 而落地页是抖音首页推荐流——图片和视频会一直加载、一直占内存。实测
    # 只按 new_work_page 默认那样拦 media/font，relay 那台 Chrome 会从
    # 3.7G 涨到 4.7G（总共才 7.2G），这在这台机器上是要命的。
    #
    # 私信数据全部走 IM 的 websocket 和 XHR，页面渲染出什么样我们一个像素
    # 都不看（读的是 window.conversationStore 里的对象），所以图片/媒体/字体
    # 全部掐掉是安全的。先 unroute 掉 new_work_page 装的那个，两个 route
    # 挂同一个 pattern 只有后注册的生效、前一个反而会让请求卡住。
    with suppress(Exception):
        await page.unroute("**/*")

    async def block_for_im(route):
        if route.request.resource_type in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", block_for_im)
    await _goto(page, BASE + "/")
    _im_page = page
    _im_page_touched_at = time.time()
    return page


# 等 IM 数据就绪最多等多久。这个值跟上面的 TTL 是一对：TTL 设成 300 秒
# （小于轮询间隔）意味着**每一轮轮询拿到的都是一张冷页面**，要从零加载抖音
# SPA、建立 IM 长连接、再等会话列表同步回来。2026-09-05 在这台机器上实测，
# 这一整套在 relay 内存宽裕时约 15~25 秒，内存吃紧（cgroup high 1.3G 被顶到）
# 时会拖到 60 秒以上。原来默认的 25 秒正好卡在这条线上，表现是"平时好好的，
# 一忙就整轮读不到消息"，而且报出来的错是"IM 长连接可能没建立起来，或者页面
# 结构已变化"——很容易被误读成抖音改版。给足 90 秒：这是个十分钟一次的后台
# 任务，多等一会儿没有任何代价，误报一次却要人去查半天。
IM_READY_TIMEOUT_MS = max(10, int(os.environ.get("DY_IM_READY_TIMEOUT_SECONDS", "90"))) * 1000


async def _wait_im_ready(page, timeout_ms=None):
    """等 IM SDK 把会话列表灌进 window.conversationStore。

    只等"store 存在且会话数 > 0"——store 本身在页面加载后很快就有，但里面是
    空的（会话列表是 websocket 同步回来的，慢一两秒），只判 store 存在会读到
    空列表，误报成"一个会话都没有"。"""
    deadline = time.time() + (timeout_ms or IM_READY_TIMEOUT_MS) / 1000
    while time.time() < deadline:
        await _raise_if_challenge(page)
        try:
            count = await page.evaluate(
                "() => { const S = window.conversationStore;"
                " return S && S.conversationMap ? [...S.conversationMap.keys()].length : -1; }"
            )
        except Exception:
            count = -1
        if isinstance(count, int) and count > 0:
            return None
        if count == 0:
            # store 已经就绪但确实一个会话都没有（新号/清空过），是正常状态。
            # 再多给一点时间，到点了就按"真的没有会话"处理。
            if time.time() > deadline - 3:
                return None
        login = await _login_error(page)
        if login:
            return login
        await page.wait_for_timeout(400)
    return "等待抖音私信数据超时：IM 长连接可能没建立起来，或者页面结构已变化"


async def _im_settle():
    """每个私信动作前的一点随机停顿。轮询型调用本来节奏就很机械，紧挨着
    连打几次请求是最容易被风控盯上的形态，加一点抖动没有任何代价。"""
    await asyncio.sleep(random.uniform(0.8, 2.2))


# 私信消息类型 -> 好认的名字。抖音的 type 是数字，实测出来这几个（见
# 2026-09-05 的真实会话）：
#   1   系统提示（"你们已互相关注对方"）  content.tips
#   7   纯文本                            content.text
#   15  表情/贴纸                         content.stickers[].display_name
#   77  视频/图文分享                     content.itemId / content_title / content_name
#   150 文件分享                          content.name / content.format
# 没见过的 type 一律归到 unknown，并把 content 原样截一段带出去——宁可让
# 调用方看到"有一条我不认识的消息"，也不要静默丢掉，那会让骁莫名其妙地
# 漏掉对方说的话。
IM_MESSAGE_TYPES = {1: "system", 7: "text", 15: "sticker", 77: "share", 150: "file"}


def _browse_session_alive(session):
    if not session or time.time() - session["last_active_at"] > BROWSE_TTL_SECONDS:
        return False
    page = session.get("page")
    return bool(page and not page.is_closed())


async def _close_browse_session(reason):
    global _browse_session
    session, _browse_session = _browse_session, None
    if not session:
        return
    page = session.get("page")
    if page is not None:
        with suppress(Exception):
            await _close_owned_page(page)


def _install_browse_session(page, items, source):
    global _browse_session
    ids = {item.get("id") for item in items if item.get("id")}
    _browse_session = {
        "page": page,
        "ids": ids,
        "source": source,
        "list_url": page.url,
        "last_active_at": time.time(),
    }


async def _find_video_card(page, video_id):
    """用真实滚轮寻找对应卡片的封面链接。假定搜索结果/主页作品是网格卡片，
    每张卡片的 <a href> 里包含 /video/{id}——这是绝大多数视频平台网格页
    的通用写法，但没有对着真实抖音页面验证过，选择器失效时先来这里改。"""
    for step in range(12):
        link = page.locator(f'a[href*="/video/{video_id}"]').first
        if await link.count() and await link.is_visible():
            await link.scroll_into_view_if_needed()
            return link
        await page.mouse.wheel(0, random.randint(750, 1150))
        await page.wait_for_timeout(random.randint(220, 520))
        await _raise_if_challenge(page)
    return None


async def _restore_dy_list(page, list_url):
    """点进详情后退回列表：按 URL pathname 判断（不假设是弹层还是整页
    路由），退不回去就直接报告，不硬撑。"""
    try:
        for _ in range(6):
            current_path = urlparse(page.url).path
            if "/video/" not in current_path:
                return True
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(250)
            if "/video/" not in urlparse(page.url).path:
                return True
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=8000)
            except PWTimeout:
                pass
            await page.wait_for_timeout(300)
        return "/video/" not in urlparse(page.url).path
    except Exception:
        return False


async def _goto(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout as exc:
        await _raise_if_challenge(page)
        raise RuntimeError("打开抖音页面超时") from exc
    await _raise_if_challenge(page)


# ================= 动作实现 =================

async def action_feed(page, body):
    """首页推荐流。抖音网页版首页是"一次一个视频"的沉浸式竖向 feed，不是
    小红书那种网格——所以这里不复用 browse session 的"点卡片进详情"模式
    （feed 拿到的视频对象里已经带了 desc/stats 这些字段，信息量本身已经
    接近一次 read；真要看某条的完整详情/评论，直接把它的 url 传给 read
    即可，会走 direct_url_fallback）。"""
    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)
    collected = []
    seen = set()
    handler = _make_json_capture(
        page,
        (
            "/aweme/v1/web/tab/feed", "/aweme/v2/web/tab/feed", "/web/feed",
            # 2026-08-20 拿真实登录态跑通后确认：首页 /jingxuan 实际打的是
            # module/feed，不是 tab/feed（见 app.py 头部注释里说的"部署后
            # 第一件事必须冒烟测试"，这条就是冒烟测出来的）。
            "/aweme/v2/web/module/feed",
        ),
        lambda payload: _collect_aweme_from_json(payload, collected, seen),
    )
    try:
        await _goto(page, BASE + "/")
        render_data = await page.evaluate(RENDER_DATA_EXTRACT_JS)
        if render_data:
            _collect_aweme_from_json(render_data, collected, seen)

        error = await _wait_for_collected(page, collected)
        if error == "EMPTY":
            return {"ok": True, "action": "feed", "items": [], "count": 0}
        if error:
            return {"ok": False, "error": error}

        await _scroll_to_load_more(page, collected, n)
    finally:
        with suppress(Exception):
            page.remove_listener("response", handler)

    items = collected[:n]
    return {"ok": True, "action": "feed", "items": items, "count": len(items)}


async def action_search(page, body):
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "error": "search 需要非空 query"}
    query = query.strip()
    if len(query) > 100:
        return {"ok": False, "error": "query 最长 100 个字符"}
    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)

    collected = []
    seen = set()
    handler = _make_json_capture(
        page,
        ("/aweme/v1/web/general/search", "/aweme/v1/web/search", "/web/search"),
        lambda payload: _collect_aweme_from_json(payload, collected, seen),
    )
    try:
        await _goto(page, BASE + "/")
        # 未登录时点搜索框会弹出一个 id="login-full-panel-..." 的全屏遮罩，
        # 直接盖住输入框——如果不提前查，click 会在这个遮罩上反复重试直到
        # 超时，报出的是原始 Playwright 异常而不是"登录态失效"这句人话。
        login_error_before_click = await _login_error(page)
        if login_error_before_click:
            with suppress(Exception):
                page.remove_listener("response", handler)
            return {"ok": False, "error": login_error_before_click}
        search_box = page.locator(
            'input[placeholder*="搜索"]:visible, textarea[placeholder*="搜索"]:visible'
        ).first
        try:
            await search_box.wait_for(timeout=10000)
        except PWTimeout:
            with suppress(Exception):
                page.remove_listener("response", handler)
            return {"ok": False, "error": "找不到搜索输入框，抖音首页结构可能已变化"}

        await _guarded_click(page, search_box)
        await _human_type(page, query)
        await page.keyboard.press("Enter")

        try:
            await page.wait_for_url(re.compile(r"/search/"), timeout=15000)
        except PWTimeout:
            await _raise_if_challenge(page)
            # 有的抖音版本搜索结果是原地渲染、不换 URL，不当硬性失败条件，
            # 继续走下面"等数据抓到"的判断。

        error = await _wait_for_collected(page, collected)
        if error == "EMPTY":
            return {"ok": True, "action": "search", "query": query, "items": [], "count": 0}
        if error:
            return {"ok": False, "error": error}

        await _scroll_to_load_more(page, collected, n)
    finally:
        with suppress(Exception):
            page.remove_listener("response", handler)

    items = collected[:n]
    return {"ok": True, "action": "search", "query": query, "items": items, "count": len(items)}


PROFILE_HEADER_EXTRACT_JS = r"""
() => {
  const text = (selector) => document.querySelector(selector)?.textContent?.trim() || '';
  // 优先找页面里明显是"昵称/签名/抖音号"的文本块；抖音网页版没有稳定的
  // class 名可用，这里退而求其求，靠 header 区域里最靠前的几段文字兜底，
  // 精确度不如 RENDER_DATA，仅在拿不到 RENDER_DATA 时才会被用到。
  const h1 = document.querySelector('h1, h2');
  return {
    nickname: h1 ? h1.textContent.trim() : '',
    signature: text('[class*="signature" i], [class*="desc" i]'),
  };
}
"""


async def action_profile(page, body):
    raw = body.get("url") or body.get("user")
    try:
        raw = await _resolve_short_link(raw)
        url = _safe_dy_url(raw, "profile")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)

    collected = []
    seen = set()
    handler = _make_json_capture(
        page,
        ("/aweme/v1/web/aweme/post", "/aweme/v2/web/aweme/post", "/web/aweme/post"),
        lambda payload: _collect_aweme_from_json(payload, collected, seen),
    )
    try:
        await _goto(page, url)

        # 用户资料头（昵称/签名）目前只走粗粒度 DOM 兜底——RENDER_DATA 里
        # user 对象的字段名同样不确定，没有对着真实页面验证过映射关系，
        # 与其编一套大概率映射错的字段名，不如先如实只给这两个粗粒度值，
        # 需要更多字段（抖音号/粉丝数等）时再回来对着真实页面补。
        profile = await page.evaluate(PROFILE_HEADER_EXTRACT_JS)

        error = await _wait_for_collected(page, collected)
        videos = []
        if error and error != "EMPTY":
            profile["videos_warning"] = error
        else:
            await _scroll_to_load_more(page, collected, n)
            videos = collected[:n]
    finally:
        with suppress(Exception):
            page.remove_listener("response", handler)

    profile["url"] = page.url
    return {
        "ok": True,
        "action": "profile",
        "profile": profile,
        "items": videos,
        "count": len(videos),
    }


async def action_user_posts(page, body):
    """读一个抖音账号的作品列表（只读）。

    跟 action_profile 的区别不在"取的数据"，而在"寻址方式和返回形状"：
    profile 要求调用方给一条完整的主页 URL（人在聊天里甩链接过来那条路径），
    这里接的是 sec_uid，且允许不传——不传就看当前登录账号自己的主页
    (/user/self)。调用方如果是定时/按需地盯着某个固定的号看数据涨没涨，手里
    天然只有一个 sec_uid、没有链接，
    每次自己拼一条 URL 再走 profile 属于绕远路，而且 profile 还会顺带装一个
    browse session（为了"点卡片进详情"那条路径保留页面），对纯取数来说是白
    占一个常驻标签页。

    返回形状也故意跟 profile 不一样：profile 返回 _normalize_aweme() 那份
    嵌套结构（stats 是个子 dict），这里摊平成一层、字段名直接对齐调用方要的
    "aweme_id/发布时间/播放/点赞/评论/分享/链接"，省得每个调用方各写一遍
    嵌套取值 + 空值兜底。

    ⚠️ plays（播放量）：拿真实登录态实测过，抖音网页接口对别人的作品
    **恒返回 0**，真实播放量只有创作者本人在 creator.douyin.com 后台
    看得到。这里照实透传不做修饰，调用方要自己判断"全是 0 就别提播放量"，
    见 _normalize_aweme() 里 plays 那条注释。

    只读：只做导航 + 滚动加载 + 读接口返回，不点赞、不关注、不评论。
    """
    raw_sec_uid = body.get("sec_uid")
    n = _bounded_int(body.get("n"), 10, MAX_ITEMS)

    # 不传 sec_uid = 看自己的主页。/user/self 是抖音自己的固定别名，登录态下
    # 会渲染成当前账号的主页，不需要先查出自己的 sec_uid 再拼一次 URL。
    if raw_sec_uid is None or (isinstance(raw_sec_uid, str) and not raw_sec_uid.strip()):
        sec_uid = None
        url = f"{BASE}/user/self"
    else:
        try:
            sec_uid = _safe_sec_uid(raw_sec_uid)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        url = f"{BASE}/user/{sec_uid}"

    collected = []
    seen = set()
    author = {}

    def _take_user(payload):
        """/aweme/v1/web/user/profile/{self,other} 的响应里带作者信息。
        用接口返回的 nickname/aweme_count，不用 PROFILE_HEADER_EXTRACT_JS 那套
        DOM 兜底——那个是 action_profile 在没有可靠接口字段时的退路，而这个
        接口实测字段名稳定（nickname/sec_uid/aweme_count/
        follower_count 都在 user 下面一层），能拿准的就别去猜 DOM。"""
        if not isinstance(payload, dict):
            return
        user = payload.get("user")
        if not isinstance(user, dict) or not user.get("sec_uid"):
            return
        author.update({
            "nickname": user.get("nickname") or "",
            "sec_uid": user.get("sec_uid"),
            "unique_id": user.get("unique_id") or user.get("short_id") or "",
            "signature": (user.get("signature") or "").strip(),
            "aweme_count": user.get("aweme_count"),
            "follower_count": user.get("follower_count"),
        })

    # answered 是"作品接口回过话了"这个信号，跟 collected（真的解析出作品）
    # 是两回事，必须分开——实测踩到的坑：有的号作品全是"私密作品"，
    # 公开的 aweme_list 是空数组，接口正常 200 回来了，但
    # _collect_aweme_from_json() 一条都收不到。如果拿 collected 当等待条件，
    # 这种"号存在、就是没有公开作品"的正常情况会一路死等到超时，报成"抖音改版
    # 了"这种驴唇不对马嘴的错误。改成等 answered：接口一回话就往下走，到底有
    # 没有作品交给后面看 collected 判断。
    answered = []

    def _on_posts(payload):
        if isinstance(payload, dict) and "aweme_list" in payload:
            answered.append(True)
        _collect_aweme_from_json(payload, collected, seen)

    posts_handler = _make_json_capture(
        page,
        ("/aweme/v1/web/aweme/post", "/aweme/v2/web/aweme/post", "/web/aweme/post"),
        _on_posts,
    )
    user_handler = _make_json_capture(
        page,
        ("/aweme/v1/web/user/profile/",),
        _take_user,
    )
    try:
        # 强制这一页走真实网络：关掉 HTTP 缓存 + 绕过 service worker。
        #
        # 为什么必须加（排查了很久的一个不稳定失败）：这个动作靠
        # page.on("response") 抓 /aweme/v1/web/aweme/post 的响应体拿数据，而
        # 抖音站点注册了 service worker（douyin.com/sw.js）。短时间内重复打开
        # 同一个主页时，作品接口有时会被 SW / HTTP 缓存直接供数、根本不走网络，
        # 于是 Playwright 这边一个 response 事件都收不到——表现就是"第一次好用、
        # 隔一会儿再调就卡满 40 秒超时"，而页面上作品其实好好地渲染出来了，
        # 极具迷惑性（一度以为是选择器失效/抖音改版）。实测确认：只要 response
        # 事件到了就一定成功，失败清一色是事件没到。
        #
        # 这一页是纯取数用的一次性后台页，用完就关，禁掉缓存没有任何副作用，
        # 代价只是每次都真的请求一次（本来也就是我们想要的：要的是"现在的
        # 数据"，读到缓存里半小时前的数字反而是错的）。只作用于这一页，不碰
        # feed/search/read/post 那几条路径。
        with suppress(Exception):
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            await cdp.send("Network.setBypassServiceWorker", {"bypass": True})

        await _goto(page, url)

        # RENDER_DATA 里有时直接就带着首屏那几条作品，先白捡一次，捡到了
        # 下面 _wait_for_collected() 立刻就能返回，省一轮等待——跟
        # action_feed()/action_read() 里那两次 RENDER_DATA_EXTRACT_JS 是
        # 同一个用法。
        render_data = await page.evaluate(RENDER_DATA_EXTRACT_JS)
        if render_data:
            _collect_aweme_from_json(render_data, collected, seen)
            if collected:
                # RENDER_DATA 里就带着作品，不用再等接口了——补一个信号，
                # 免得下面白等 40 秒。
                answered.append(True)

        # 等一轮 20 秒，没等到作品接口回话就 reload 再等一轮。
        #
        # 为什么要重试（跟上面禁缓存那段是同一个毛病的两道防线）：即使禁了缓存
        # 和 SW，实测仍有大约六分之一的加载收不到 /aweme/v1/web/aweme/post 的
        # response 事件（页面本身渲染正常，就是事件没到 Playwright 这边）。这种
        # 失败是"这一次加载"的属性，换一次加载基本就好了——所以与其把单轮等待
        # 拉长到 40 秒干等（等再久也等不来一个不会发生的事件），不如 20 秒判负、
        # reload 换一次加载重试，最坏总耗时跟原来的 40 秒一样，但把绝大部分这类
        # 失败救回来了。冷启动本身实测 5~9 秒，20 秒的单轮预算是够的。
        error = await _wait_for_collected(page, answered, timeout_ms=20000)
        if error and error != "EMPTY" and not answered:
            with suppress(Exception):
                await page.reload(wait_until="domcontentloaded", timeout=30000)
            await _raise_if_challenge(page)
            error = await _wait_for_collected(page, answered, timeout_ms=20000)
        if error == "EMPTY":
            # 这个号一条作品都没发过，是正常状态不是失败——照常返回 ok，
            # items 空列表，调用方自己决定怎么说（"她还没发过作品"）。
            return {
                "ok": True, "action": "user_posts", "sec_uid": sec_uid,
                "author": author, "items": [], "count": 0,
            }
        if error:
            return {"ok": False, "error": error}

        if not collected:
            # 接口回话了但一条公开作品都没有：这个号没发过作品，或者作品
            # 全是私密作品。是正常状态不是失败。
            return {
                "ok": True, "action": "user_posts", "sec_uid": sec_uid,
                "author": author, "items": [], "count": 0,
            }

        await _scroll_to_load_more(page, collected, n)
    finally:
        for handler in (posts_handler, user_handler):
            with suppress(Exception):
                page.remove_listener("response", handler)

    # 抖音主页默认就是按发布时间倒序的，但 _collect_aweme_from_json() 是
    # "扫到哪条算哪条"（RENDER_DATA 和接口响应两个来源混在一起），顺序不保证，
    # 所以这里显式按发布时间重排一次再截断——不然 n=3 有可能截出三条老作品，
    # 把刚发的那条漏掉，而"最近发了什么"恰恰是调用方最在意的。
    collected.sort(key=lambda it: it.get("create_time") or 0, reverse=True)

    items = []
    for it in collected[:n]:
        stats = it.get("stats") or {}
        create_time = it.get("create_time")
        items.append({
            "aweme_id": it.get("id"),
            "desc": it.get("desc") or "",
            "create_time": create_time,
            "created_at": (
                datetime.fromtimestamp(create_time, timezone(timedelta(hours=8)))
                .isoformat(timespec="seconds")
                if isinstance(create_time, (int, float)) and create_time > 0 else None
            ),
            "plays": stats.get("plays"),
            "likes": stats.get("likes"),
            "comments": stats.get("comments"),
            "shares": stats.get("shares"),
            "collects": stats.get("collects"),
            "url": it.get("url"),
            "cover": it.get("cover"),
        })

    return {
        "ok": True,
        "action": "user_posts",
        "sec_uid": sec_uid,
        "author": author,
        "items": items,
        "count": len(items),
    }


# 在页面里跑的那段：把 conversationStore 里的会话列表读成朴素 JSON。
# 注意 sortOrder / unreadCount / badgeCount 这些字段全在原型链第二层的
# getter 上，Object.keys() 是看不见的，只能一个个点名取。
IM_CONVERSATIONS_JS = r"""() => {
  const S = window.conversationStore;
  if (!S || !S.conversationMap) return {err: 'no_store'};
  const pick = (conv) => {
    const get = (k) => { try { return conv[k]; } catch (e) { return null; } };
    return {
      unread: get('unreadCount'),
      badge: get('badgeCount'),
      sort_order: get('sortOrder'),
      to_participant_user_id: String(get('toParticipantUserId') || ''),
      to_participant_sec_uid: get('toParticipantSecUserId') || '',
      is_stranger: !!get('isStrangerConversation'),
      is_muted: !!get('isMuted'),
      conv_type: get('type'),
    };
  };
  const out = [];
  for (const [cid, conv] of S.conversationMap.entries()) {
    const info = pick(conv);
    info.conversation_id = cid;
    // 最后一条消息的时间和类型，用来判断"要不要去读这个会话"，省得每次
    // 都把消息全拉一遍。
    try {
      const last = conv.lastMessage;
      if (last) {
        info.last_message_type = last.type;
        info.last_message_at = (last.createdAt instanceof Date) ? last.createdAt.getTime() : null;
        info.last_message_from = String(last.sender || '');
      }
    } catch (e) {}
    out.push(info);
  }
  return {total_unread: S.totalUnreadCounts, conversations: out};
}"""


# 读某个会话的消息。同样只读 store，不打开会话、不发已读回执。
IM_MESSAGES_JS = r"""(args) => {
  const S = window.conversationStore;
  if (!S || !S.conversationMap) return {err: 'no_store'};
  const conv = S.conversationMap.get(args.conversationId);
  if (!conv) return {err: 'no_conversation'};

  const peerUid = String(conv.toParticipantUserId || '');
  // "这条是不是我发的"：优先用消息对象自己的 isFromMe（2026-09-05 实测确认
  // 存在，是消息原型上的 getter，SDK 自己算好的），拿不到才退回"sender 不等
  // 于对方就是自己"这个推断——1:1 会话里除了对方就只剩自己，这个推断本身也
  // 是对的（实测两种判法在真实会话上结论一致），只是既然 SDK 已经给了答案，
  // 就没必要在这里自己推一遍。会话 id 形如 0:1:<对方uid>:<我的uid>，顺带
  // 解析出来一起带回去，方便调用方核对。
  const idParts = String(args.conversationId).split(':');
  const meUid = idParts.length === 4 && idParts[2] === peerUid ? idParts[3] : '';

  const parse = (raw) => { try { return JSON.parse(raw); } catch (e) { return null; } };

  const msgs = [];
  let list;
  try { list = conv.getMessageList(); } catch (e) { return {err: 'get_list_failed:' + e}; }

  for (const m of list) {
    const c = typeof m.content === 'string' ? parse(m.content) : (m.content || null);
    const item = {
      server_id: String(m.serverId || ''),
      type: m.type,
      kind: args.typeNames[m.type] || 'unknown',
      sender_uid: String(m.sender || ''),
      sender_sec_uid: m.secSender || '',
      is_me: (() => {
        try { if (typeof m.isFromMe === 'boolean') return m.isFromMe; } catch (e) {}
        return String(m.sender || '') !== peerUid;
      })(),
      created_at_ms: (m.createdAt instanceof Date) ? m.createdAt.getTime() : null,
      text: '',
    };
    // ext 里的 s:server_message_create_time 是服务端时间，createdAt 是本地
    // Date；正常情况下差几十毫秒。优先用 createdAt，它拿不到才回退。
    if (item.created_at_ms == null && m.ext && m.ext['s:server_message_create_time']) {
      const t = parseInt(m.ext['s:server_message_create_time'], 10);
      if (!isNaN(t)) item.created_at_ms = t;
    }
    if (c) {
      if (item.kind === 'text') {
        item.text = c.text || '';
      } else if (item.kind === 'system') {
        item.text = c.tips || '';
      } else if (item.kind === 'sticker') {
        const st = (c.stickers && c.stickers[0]) || {};
        item.text = st.display_name || '';
        item.sticker_name = st.display_name || '';
      } else if (item.kind === 'file') {
        item.text = c.name || '';
        item.file_name = c.name || '';
        item.file_format = c.format || '';
      } else if (item.kind === 'share') {
        // 视频/图文分享：itemId 就是 aweme_id，标题和作者昵称消息里本来就
        // 带着（content_title / content_name），不用再去请求一次详情页。
        item.aweme_id = String(c.itemId || '');
        item.title = c.content_title || '';
        item.author = c.content_name || '';
        item.author_sec_uid = c.secUID || '';
        item.aweme_url = item.aweme_id ? ('https://www.douyin.com/video/' + item.aweme_id) : '';
        item.text = item.title;
      } else {
        // 不认识的类型：带一段原文出去，方便以后照着补解析。
        item.raw_preview = (typeof m.content === 'string' ? m.content : JSON.stringify(m.content) || '').slice(0, 300);
      }
    }
    msgs.push(item);
  }
  msgs.sort((a, b) => (a.created_at_ms || 0) - (b.created_at_ms || 0));
  return {
    conversation_id: args.conversationId,
    peer_uid: peerUid,
    peer_sec_uid: conv.toParticipantSecUserId || '',
    me_uid: meUid,
    unread: conv.unreadCount,
    messages: msgs,
  };
}"""


async def action_im_conversations(page, body):
    """读私信会话列表（只读，零 UI 交互）。

    返回每个会话的 conversation_id / 对方 sec_uid / 未读数 / sortOrder
    时间戳，外加最后一条消息的时间和类型（省得调用方为了判断"有没有新消息"
    每次都去拉整个消息列表）。

    ⚠️ unread 和 badge 是两个不一样的数，实测见过 unreadCount=0 而
    badgeCount=4 的会话，两个都原样带出去，由调用方决定信哪个——这里不替
    调用方合并，合错了会导致"明明有新消息却被判成没有"。真正可靠的"有没有
    新消息"判据是 last_message_at 跟自己记的水位线比，见 luxiao_bot2 的
    douyin_im_context.py。
    """
    await _im_settle()
    error = await _wait_im_ready(page)
    if error:
        return {"ok": False, "error": error}
    data = await page.evaluate(IM_CONVERSATIONS_JS)
    if data.get("err"):
        return {"ok": False, "error": f"读会话列表失败：{data['err']}"}
    convs = data.get("conversations") or []
    return {
        "ok": True, "action": "im_conversations",
        "total_unread": data.get("total_unread"),
        "conversations": convs, "count": len(convs),
    }


async def action_im_messages(page, body):
    """读指定会话的消息（只读，零 UI 交互，**不会把消息标成已读**）。

    参数 conversation_id 必填，从 im_conversations 拿。可选 limit：只要最近
    N 条（按时间正序返回，limit 从尾部截）。

    每条消息的形状见 IM_MESSAGES_JS 里的注释：kind 是 text/system/sticker/
    share/file/unknown 之一，share 类型额外带 aweme_id / aweme_url / title /
    author。is_me 表示这条是不是自己发的。
    """
    conversation_id = body.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return {"ok": False, "error": "im_messages 需要 conversation_id（从 im_conversations 拿）"}
    conversation_id = conversation_id.strip()
    limit = _bounded_int(body.get("limit"), 50, 200)

    await _im_settle()
    error = await _wait_im_ready(page)
    if error:
        return {"ok": False, "error": error}

    data = await page.evaluate(
        IM_MESSAGES_JS,
        {"conversationId": conversation_id, "typeNames": {str(k): v for k, v in IM_MESSAGE_TYPES.items()}},
    )
    if data.get("err") == "no_conversation":
        return {"ok": False, "error": f"没有这个会话：{conversation_id}（会话 id 变了？先调 im_conversations）"}
    if data.get("err"):
        return {"ok": False, "error": f"读消息失败：{data['err']}"}

    msgs = data.get("messages") or []
    if limit and len(msgs) > limit:
        msgs = msgs[-limit:]
    for m in msgs:
        ts = m.get("created_at_ms")
        m["created_at"] = (
            datetime.fromtimestamp(ts / 1000, timezone(timedelta(hours=8))).isoformat(timespec="seconds")
            if isinstance(ts, (int, float)) and ts > 0 else None
        )
    return {
        "ok": True, "action": "im_messages",
        "conversation_id": data.get("conversation_id"),
        "peer_sec_uid": data.get("peer_sec_uid"),
        "me_uid": data.get("me_uid"),
        "unread": data.get("unread"),
        "messages": msgs, "count": len(msgs),
    }


# ================= 私信：发送（唯一一个会往对话里写东西的私信动作）=================
# 上面 im_conversations / im_messages 两个动作刻意做到"读多少遍都不留痕迹"
# （不进会话、不发已读回执，见【私信】那一节的注释）。im_send 是另一半：它
# **必须**进会话——网页版没有"不打开会话直接发消息"的入口，openImConversation()
# 之后 IM 面板才会挂上那个输入框。所以调这个动作一定会顺带把这个会话里之前
# 的未读标成已读。这不是副作用失控，正是原来那半边留出来的语义：真正的"已读"
# 只在骁真的回复的时候发生，跟人的行为一致（人也是点进去看了才回的）。
#
# 发完之后整页关掉（_close_im_page），不是省内存，是为了别让"已读"漏出去：
# IM 面板一直开着的话，之后每一条新消息一到就会被自动标成已读，等于回到了
# "轮询任务默默替人已读"那个被否掉的行为，只是换了个路径。反正 TTL 默认
# 300 秒 < 轮询间隔 10 分钟，这一页两次轮询之间本来就会被收掉，提前关掉
# 几乎不多花什么（下一轮重建一页 10~15 秒，后台任务无所谓）。
#
# 三态结果（confirmed/failed/uncertain）跟 post/post_images 用同一套，理由
# 也一样、而且更硬：发私信不是幂等操作，"不确定发没发出去"的时候自动重试
# 一次，对面收到的就是两条一模一样的话。所以只有在 conversationStore 里真的
# 看见自己那条消息才回 confirmed，看不见一律 uncertain + retry_safe=False。

# 两次发送之间至少隔这么久。跟 _im_settle() 那个 1~2 秒的随机停顿不是一回事：
# 那个是"每个私信动作前抖一下"，这个是"别连着突突"——真人打字发消息中间总
# 有几十秒，接连两条毫秒级发出的消息是自动化最明显的特征之一。没到时间不是
# 报错，是等够了再发（调用方是十分钟一次的后台任务，等这一会儿无所谓）。
IM_SEND_MIN_GAP_SECONDS = max(0, int(os.environ.get("DY_IM_SEND_MIN_GAP_SECONDS", "20")))
_im_last_send_at = 0.0

# 一条私信最长多少字。抖音真实上限没有对着页面验证过，这里给一个保守值：
# 超了当场报错，而不是让输入框自己截断、发出去半句话。
MAX_IM_TEXT_CHARS = 500


# 打开会话。openImConversation 是抖音自己挂在 window 上的函数（2026-09-05
# 实测存在），参数就是一个会话 id 字符串——它内部只是 setConfig + 发一个
# openImConversation 事件，由 IM 面板自己去响应，所以**不会触发页面跳转**，
# 调完之后还在原来那一页上，这正是我们要的（这一页是复用的常驻私信页）。
IM_OPEN_CONVERSATION_JS = r"""(cid) => {
  const S = window.conversationStore;
  if (!S || !S.conversationMap) return {err: 'no_store'};
  if (!S.conversationMap.has(cid)) return {err: 'no_conversation'};
  if (typeof window.openImConversation !== 'function') return {err: 'no_open_fn'};
  try { window.openImConversation(cid); } catch (e) { return {err: 'open_failed:' + e}; }
  return {ok: true};
}"""


# 找输入框，找到就地打一个 data-dy-im-editor 标记，后面 Playwright 直接按
# 这个属性定位。绕这一圈而不是写死 CSS 选择器，是因为抖音的 class 名是编译
# 期哈希出来的（见文件头），messageEditorinputArea 这种看着像人话的也随时
# 可能变；反过来"页面上唯一那个可见的 contenteditable"这个判据跟具体 class
# 无关，改版也不容易失效。三级判据从最可靠到最兜底排：占位符文案 → class
# 里带 editor-kit/messageEditor → 位置最靠下的那个（输入框永远在面板底部）。
IM_FIND_EDITOR_JS = r"""() => {
  document.querySelectorAll('[data-dy-im-editor]').forEach(el => el.removeAttribute('data-dy-im-editor'));
  const ph = (el) => (el.getAttribute('data-placeholder') || el.getAttribute('placeholder')
                      || el.getAttribute('aria-placeholder') || '');
  const visible = [...document.querySelectorAll('[contenteditable="true"], [contenteditable=""]')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.width > 40 && r.height > 10; });
  if (!visible.length) return {err: 'no_editor'};
  const target = visible.find(el => ph(el).includes('发送消息'))
    || visible.find(el => /messageEditor|editor-kit/i.test(String(el.className || '')))
    || visible.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top).pop();
  target.setAttribute('data-dy-im-editor', '1');
  return {ok: true, placeholder: ph(target), cls: String(target.className || '').slice(0, 120)};
}"""


# 焦点到底在不在输入框里。这是"能不能开始打字"唯一靠谱的判据——见
# action_im_send() 里那段注释：输入框的 DOM 早在面板挂好之前就存在了，
# 光判"元素在不在""可不可见"都判不出来它能不能收键盘事件。
# 用 contains 而不是 ===：editor-kit 会在里面再套一层可编辑节点，真正拿到
# 焦点的经常是子节点，判严格相等会把已经就绪的情况误判成没就绪。
IM_EDITOR_FOCUSED_JS = r"""() => {
  const el = document.querySelector('[data-dy-im-editor="1"]');
  if (!el) return false;
  const active = document.activeElement;
  return !!active && (active === el || el.contains(active));
}"""


# 输入框里现在是什么。editor-kit 这类富文本编辑器空的时候里面躺着一个零宽
# 空格（实测 innerText 是一个 U+200B），所以判"空不空"必须先把零宽字符去掉，
# 不然永远判成"有内容"。
IM_EDITOR_TEXT_JS = r"""() => {
  const el = document.querySelector('[data-dy-im-editor="1"]');
  if (!el) return null;
  return (el.innerText || '').replace(/[\u200B-\u200D\uFEFF]/g, '').trim();
}"""


# 发出去没有：在 conversationStore 里找自己刚发的那一条。判据是"我发的 +
# 时间在开始发送之后 + 正文一模一样 + **有服务端分配的 serverId**"，四个
# 条件缺一不可。
#
# 最后那条 serverId 是 2026-09-05 用两次真实发送换来的，不能省：SDK 在你按下
# 回车的那一刻就会把消息**乐观地**塞进 getMessageList()，这时候它只有本地的
# clientId（一个 uuid），serverId 是空字符串；服务端真的收下之后才回填一个
# 真正的 serverId（形如 7681992212238026289）。只判"列表里有这条消息"的话，
# 这两种状态长得一模一样——实测第一次发的那条一直停在 serverId 为空的状态、
# 最后压根没送到（在对方那边和重新加载后的页面里都不存在），却被判成了
# confirmed。判 serverId 之后这两种状态就分得开了：有 serverId = 服务端收下
# 了，没有 = 还在本地排队或者已经失败，一律走 uncertain。
#
# 另外 is_me 这里也优先用 SDK 自己的 isFromMe，理由同 IM_MESSAGES_JS。
IM_VERIFY_SENT_JS = r"""(args) => {
  const S = window.conversationStore;
  if (!S || !S.conversationMap) return {err: 'no_store'};
  const conv = S.conversationMap.get(args.conversationId);
  if (!conv) return {err: 'no_conversation'};
  const peerUid = String(conv.toParticipantUserId || '');
  let list;
  try { list = conv.getMessageList(); } catch (e) { return {err: 'get_list_failed:' + e}; }
  let pending = false;
  for (const m of list) {
    let mine;
    try { mine = (typeof m.isFromMe === 'boolean') ? m.isFromMe : (String(m.sender || '') !== peerUid); }
    catch (e) { mine = String(m.sender || '') !== peerUid; }
    if (!mine) continue;
    const t = (m.createdAt instanceof Date) ? m.createdAt.getTime() : 0;
    if (!t || t < args.sinceMs) continue;
    let text = '';
    try {
      const c = typeof m.content === 'string' ? JSON.parse(m.content) : m.content;
      text = (c && c.text) || '';
    } catch (e) {}
    if (text !== args.text) continue;
    const serverId = String(m.serverId || '');
    if (!serverId) { pending = true; continue; }
    return {ok: true, server_id: serverId, created_at_ms: t};
  }
  // pending=true 表示"消息在列表里但还没拿到 serverId"——调用方据此区分
  // "还在发（再等等）"和"连本地记录都没有（回车压根没生效）"。
  return {ok: false, pending: pending};
}"""


def _clean_im_text(raw):
    """把要发的内容规整成"一条消息"。返回 (text, error)。

    换行统一压成空格：输入框里回车 = 发送，多行文本照原样敲进去会被拆成好
    几条消息发出去（对面看到的是连着弹三条），这不是调用方想要的。真要发
    多条就调多次 im_send，语义清清楚楚。"""
    if not isinstance(raw, str):
        return "", "im_send 需要 text（要发的文字）"
    text = " ".join(raw.replace("\r", "\n").split())
    if not text:
        return "", "im_send 的 text 不能是空的"
    if len(text) > MAX_IM_TEXT_CHARS:
        return "", f"im_send 的 text 太长（{len(text)} 字，上限 {MAX_IM_TEXT_CHARS}）"
    return text, ""


async def action_im_send(page, body):
    """往指定会话发一条文字私信。参数：conversation_id（从 im_conversations
    拿）+ text。

    结果是三态的，跟 post 一样，见上面那段注释：
      confirmed  在 conversationStore 里看见自己这条消息了，确实发出去了
      failed     压根没走到"按回车"这一步（会话找不到、输入框没找到、文字
                 没敲进去），什么都没发生，调用方可以安全重试
      uncertain  回车按下去了但没验证到，**禁止自动重试**
    """
    conversation_id = body.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return _failed("im_send 需要 conversation_id（从 im_conversations 拿）", action="im_send")
    conversation_id = conversation_id.strip()

    text, error = _clean_im_text(body.get("text"))
    if error:
        return _failed(error, action="im_send")

    # 距上一条发送不够久就先等够，见 IM_SEND_MIN_GAP_SECONDS。
    global _im_last_send_at
    gap = IM_SEND_MIN_GAP_SECONDS - (time.time() - _im_last_send_at)
    if _im_last_send_at and gap > 0:
        await asyncio.sleep(gap)

    await _im_settle()
    login_error = await _wait_im_ready(page)
    if login_error:
        return _failed(login_error, action="im_send")

    opened = await page.evaluate(IM_OPEN_CONVERSATION_JS, conversation_id)
    if opened.get("err") == "no_conversation":
        return _failed(f"没有这个会话：{conversation_id}（会话 id 变了？先调 im_conversations）",
                       action="im_send")
    if opened.get("err") == "no_open_fn":
        return _failed("页面上没有 window.openImConversation，抖音网页版可能改版了", action="im_send")
    if opened.get("err"):
        return _failed(f"打开会话失败：{opened['err']}", action="im_send")

    # 等 IM 面板真的切到这个会话。curConversationId 是 store 自己维护的，
    # 比等某个 DOM 元素出现可靠——面板是异步渲染的，元素出现的那一刻里面
    # 也未必已经是目标会话了。
    for _ in range(40):
        current = await page.evaluate(
            "() => { try { return window.conversationStore.curConversationId || ''; } catch (e) { return ''; } }"
        )
        if current == conversation_id:
            break
        await page.wait_for_timeout(300)
    else:
        return _failed("打开会话超时：IM 面板没有切到这个会话", action="im_send")

    # curConversationId 置上了 ≠ 这个会话可以打字了。
    #
    # 2026-09-05 用四次真实发送换来的一段：openImConversation() 是同步
    # setConfig + 发事件，curConversationId 立刻就变，但 IM 面板（消息列表 +
    # 输入框）是异步挂载的。在它挂完之前敲字，**字是进得去的**——输入框 DOM
    # 早就在了，innerText 也读得到——回车下去 SDK 也确实建了一条本地消息，
    # 但那条消息永远拿不到服务端 id，对方那边什么都收不到。
    #
    # 所以"输入框存在""输入框可见"这两个条件都不能当就绪判据（它们在没挂好
    # 的时候就已经成立了，这正是那两次假成功的成因）。真正能判出来的是
    # **焦点**：点一下之后 document.activeElement 落没落进输入框里。挂好之前
    # 点了也拿不到焦点，挂好之后一点就有。所以下面是三层递进的判据，每一层
    # 都是"真的能观察到的状态"，不是等一个拍脑袋的秒数：
    #   1. 输入框出现（IM_FIND_EDITOR_JS 能找到且可见）
    #   2. 点得上焦点（IM_EDITOR_FOCUSED_JS）
    #   3. 字确实进去了（IM_EDITOR_TEXT_JS）
    # 三层都过了才按回车。任何一层没过都是 failed（changed=False，什么都没
    # 发生），调用方可以安全重试。
    await _raise_if_challenge(page)

    # 第 1 层：等输入框出现。面板是异步挂的，刚切完会话时它可能还不存在。
    for _ in range(30):
        editor = await page.evaluate(IM_FIND_EDITOR_JS)
        if not editor.get("err"):
            break
        await page.wait_for_timeout(500)
    else:
        return _failed("等私信输入框出现超时（抖音网页版可能改版了）", action="im_send")

    locator = page.locator('[data-dy-im-editor="1"]')
    try:
        await locator.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        return _failed("私信输入框找到了但一直不可见", action="im_send")

    # 第 2 层：点到焦点真的落进去为止。点不上就等一会儿再点——面板挂载和
    # 入场动画期间点击会被吃掉，这时候唯一能做的就是再点一次。
    #
    # 给 40 次（约 40 秒）而不是 20 次：2026-09-05 实测在 relay 内存吃紧的时候
    # （cgroup 顶到 MemoryHigh、页面整体变慢）真的会超过 20 秒才挂好，那次
    # 就因为窗口太短白失败了一轮。这是个后台任务，多等 20 秒没有任何代价。
    for _ in range(40):
        await _guarded_click(page, locator)
        await page.wait_for_timeout(400)
        if await page.evaluate(IM_EDITOR_FOCUSED_JS):
            break
        await page.wait_for_timeout(600)
    else:
        return _failed("私信输入框点不上焦点（面板可能没挂好），这次什么都没发出去",
                       action="im_send")

    # 第 3 层：清掉可能残留的草稿（上一次发失败留下的半句话会跟这次的内容
    # 拼在一起发出去），敲字，再核对一遍字有没有真的进去。
    #
    # 核对这一步不是多余的谨慎：焦点要是中途被别的东西抢走，这些按键就会落到
    # 抖音首页上——那一页的空格/上下键全是播放和翻视频的快捷键，等于在人家的
    # 推荐流里乱按一通，而且回车还什么都没发出去。第一次没敲进去就重来一次
    # （焦点抖动是偶发的，重敲一次通常就好），两次都不行才放弃。
    typed = ""
    for attempt in range(2):
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(150)
        await _human_type(page, text)
        await page.wait_for_timeout(200)
        typed = await page.evaluate(IM_EDITOR_TEXT_JS)
        if typed:
            break
        print(f"[im] 第 {attempt + 1} 次敲字没进输入框，重试", flush=True)
        await _guarded_click(page, locator)
        await page.wait_for_timeout(500)
    if not typed:
        return _failed("文字没能敲进输入框（焦点可能被别的元素抢走了），这次什么都没发出去",
                       action="im_send")

    since_ms = await page.evaluate("() => Date.now()") - 2000
    await page.keyboard.press("Enter")

    # 从这里往后都是 uncertain：回车已经按下去了。等服务端回填 serverId，
    # 实测正常情况下 2 秒内就有了，这里给到 25 秒是留给网络慢的时候。
    verified = None
    pending = False
    for _ in range(80):
        await page.wait_for_timeout(300)
        result = await page.evaluate(
            IM_VERIFY_SENT_JS,
            {"conversationId": conversation_id, "text": text, "sinceMs": since_ms},
        )
        if result.get("ok"):
            verified = result
            break
        pending = bool(result.get("pending"))

    _im_last_send_at = time.time()
    # 不管验没验到都把私信页关掉，见本节开头注释（面板开着 = 之后的新消息
    # 会被自动已读）。
    await _close_im_page("sent" if verified else "send_unverified")

    if verified:
        return _confirmed(
            f"已发送：{text[:30]}",
            action="im_send",
            conversation_id=conversation_id,
            chars=len(text),
            server_id=verified.get("server_id"),
            created_at_ms=verified.get("created_at_ms"),
        )
    return _uncertain(
        ("这条消息已经进了本地发送队列但一直没拿到服务端 id，大概率没发出去"
         if pending else
         "回车已经按下去了，但会话记录里连本地记录都没有，回车可能压根没生效")
        + "。禁止自动重试——下一轮读消息的时候就能看出到底发没发成功。",
        action="im_send",
        conversation_id=conversation_id,
        chars=len(text),
        pending=pending,
    )


async def action_read(page, body, from_list=False):
    try:
        raw = await _resolve_short_link(body.get("url"))
        url = _safe_dy_url(raw, "video")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    video_id = _video_id_from_url(url)
    if not video_id:
        return {"ok": False, "error": "URL 中没有可识别的视频 ID"}

    n = _bounded_int(body.get("n"), 10, MAX_COMMENTS)

    detail_holder = {}
    comments_collected = []
    comments_seen = set()

    def on_detail_payload(payload):
        for raw_item in _find_aweme_dicts(payload):
            item_id = str(raw_item.get("aweme_id") or raw_item.get("awemeId") or raw_item.get("id") or "")
            if item_id == video_id:
                detail_holder["raw"] = raw_item

    detail_handler = _make_json_capture(
        page,
        ("/aweme/v1/web/aweme/detail", "/aweme/v2/web/aweme/detail", "/web/aweme/detail"),
        on_detail_payload,
    )
    comment_handler = _make_json_capture(
        page,
        ("/comment/list",),
        lambda payload: _collect_comments_from_json(payload, comments_collected, comments_seen),
    )

    opened_from_list = False
    list_url = page.url
    try:
        if from_list:
            link = await _find_video_card(page, video_id)
            if link is None:
                await _close_browse_session("card_not_found")
                return {
                    "ok": False,
                    "error": "命中刚才的列表，但在渲染出的网格里找不到对应视频卡片；为避免硬跳 URL，未自动降级",
                    "navigation": "card_click_failed",
                }
            await _guarded_click(page, link)
            opened_from_list = True
        else:
            await _goto(page, url)

        render_data = await page.evaluate(RENDER_DATA_EXTRACT_JS)
        if render_data and "raw" not in detail_holder:
            for raw_item in _find_aweme_dicts(render_data):
                item_id = str(raw_item.get("aweme_id") or raw_item.get("awemeId") or raw_item.get("id") or "")
                if item_id == video_id:
                    detail_holder["raw"] = raw_item
                    break

        deadline = time.time() + 15
        while "raw" not in detail_holder and time.time() < deadline:
            await _raise_if_challenge(page)
            login = await _login_error(page)
            if login:
                result = {"ok": False, "error": login}
                break
            await page.wait_for_timeout(300)
        else:
            if "raw" not in detail_holder:
                result = {
                    "ok": False,
                    "error": "等待视频详情超时：链接可能已失效，或接口路径/RENDER_DATA 结构已变化",
                }
            else:
                detail = _normalize_aweme(detail_holder["raw"])
                await _scroll_to_load_more(page, comments_collected, n, rounds=6)
                detail["comments"] = comments_collected[:n]
                detail["comments_has_more"] = len(comments_collected) > n
                detail["source_url"] = page.url
                detail["action"] = "read"
                detail["ok"] = True
                result = detail

        result["navigation"] = "feed_card_click" if (from_list and opened_from_list) else "direct_url_fallback"
    finally:
        with suppress(Exception):
            page.remove_listener("response", detail_handler)
        with suppress(Exception):
            page.remove_listener("response", comment_handler)

    restored = None
    if opened_from_list:
        restored = await _restore_dy_list(page, list_url)
        if _browse_session and _browse_session.get("page") is page and restored:
            _browse_session["last_active_at"] = time.time()
        elif _browse_session and _browse_session.get("page") is page:
            await _close_browse_session("list_restore_failed")
        if not restored:
            result["browse_session_warning"] = "详情已读取，但返回列表失败；下一次 read 将走外部链接兜底"

    return result


# ================= 写动作：post =================
# 照抄 twitter-tool 写动作的三态结果（confirmed/failed/uncertain）+ 网络响应
# 捕获的模式，不另起一套。跟 read 系动作最大的不同：这里点错/传错都可能是
# "已经上传/已经点了发布，但没验证上"，不能用二态（成功/失败）+ 自动重试，
# 那等于随时可能重复发布。


def _confirmed(verified: str, *, action="post", changed=True, **extra):
    return {"ok": True, "outcome": "confirmed", "action": action,
            "changed": changed, "verified": verified, **extra}


def _failed(error: str, *, action="post", changed=False, **extra):
    return {"ok": False, "outcome": "failed", "action": action,
            "changed": changed, "error": error, **extra}


def _uncertain(error: str, *, action="post", changed=True, **extra):
    return {"ok": None, "outcome": "uncertain", "action": action,
            "changed": changed, "error": error, "retry_safe": False, **extra}


def _network_public(network):
    """写进 actions.log / 返回给调用方的网络信息——完整 payload 不能直接
    透出去（url 里已经带着 msToken/a_bogus 这类签名参数，payload 本身也
    没必要整个暴露），但 2026-08-23 发现 status_code/status_msg/item_id
    这几个字段对排查"到底发没发成功"至关重要（见 _publish_and_verify()
    里怎么用 status_code 判断成功/失败），只挑这几个字段单独摘出来，其余
    payload 内容仍然丢弃。"""
    info = {k: v for k, v in (network or {}).items() if k != "payload"}
    payload = (network or {}).get("payload")
    if isinstance(payload, dict):
        info["payload_status_code"] = payload.get("status_code")
        if payload.get("status_msg"):
            info["payload_status_msg"] = payload.get("status_msg")
        if payload.get("status_message"):
            info["payload_status_message"] = payload.get("status_message")
        if payload.get("item_id"):
            info["payload_item_id"] = payload.get("item_id")
    return info


async def _click_with_response(page, click, url_markers, timeout_ms=15000):
    """先挂网络监听再点击；没抓到响应 ≠ 没发生——跟 twitter-tool 的
    _click_with_x_response 同一个理由：点击超时最常见的原因是响应慢或没被
    这几个关键词捕获到，不是动作没执行。"""
    await _raise_if_challenge(page)
    try:
        async with page.expect_response(
                lambda response: any(marker in response.url for marker in url_markers),
                timeout=timeout_ms) as response_info:
            await click()
        await _raise_if_challenge(page)
        response = await response_info.value
        try:
            payload = await response.json()
        except Exception:
            payload = None
        return {"captured": True, "status": response.status,
                "url": response.url, "payload": payload}
    except PWTimeout:
        await _raise_if_challenge(page)
        return {"captured": False}


def _safe_video_path(value):
    """只接受这台机器上已经存在的视频文件路径；不做 URL 下载、不做任意路径
    穿越保护之外的事——douyin-tool 本身就是给内网可信调用方用的服务（没有
    鉴权，见仓库根 README 的安全模型），这里检查的是"文件必须真的是个视频、
    大小别离谱"，不是防外部攻击者。

    这个路径是相对**这台宿主机**（douyin-tool 是 systemd 直接起在宿主机上
    的，见 douyin-tool.service），不是相对调用方的 docker 容器——
    调用方（比如 douyin_context.py）如果自己也跑在容器里，传进来的必须是
    容器外能看到的宿主机路径，不能拿容器内路径过来（容器只挂了几个 json
    数据文件，没有共享通用文件系统）。

    DY_POST_MEDIA_ROOT 设置时把路径收紧到指定目录下，方便约定一个"往这个
    文件夹扔视频"的固定位置；不设置就只做存在性/格式/大小检查。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("缺参数 video_path")
    raw = value.strip()
    if not os.path.isabs(raw):
        raise ValueError("video_path 必须是这台机器上的绝对路径")
    resolved = os.path.realpath(raw)
    if POST_MEDIA_ROOT:
        root = os.path.realpath(POST_MEDIA_ROOT)
        if resolved != root and not resolved.startswith(root + os.sep):
            raise ValueError(f"video_path 必须在允许的目录 {POST_MEDIA_ROOT} 下")
    if not os.path.isfile(resolved):
        raise ValueError(f"找不到视频文件：{raw}")
    ext = os.path.splitext(resolved)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        raise ValueError(
            f"不支持的视频格式 {ext or '(无扩展名)'}，支持：{', '.join(sorted(VIDEO_EXTENSIONS))}"
        )
    size = os.path.getsize(resolved)
    if size <= 0:
        raise ValueError("视频文件是空的")
    if size > MAX_VIDEO_BYTES:
        raise ValueError(
            f"视频文件过大（{size / 1024 / 1024:.0f}MB），上限 {MAX_VIDEO_MB}MB"
            "（DY_POST_MAX_VIDEO_MB 可调）"
        )
    return resolved


def _safe_image_paths(value):
    """跟 _safe_video_path 同一套校验规则（本机绝对路径、受 DY_POST_MEDIA_ROOT
    约束、白名单扩展名、大小上限），区别只是这里接受一个列表——图文发布
    一次最多 MAX_IMAGES_PER_POST 张，跟视频共用同一个"往固定目录扔文件"
    的约定，不单独再搞一个目录。"""
    if not isinstance(value, list) or not value:
        raise ValueError("缺参数 image_paths：图文发布至少需要一张图片")
    if len(value) > MAX_IMAGES_PER_POST:
        raise ValueError(f"image_paths 最多 {MAX_IMAGES_PER_POST} 张")
    resolved_paths = []
    for raw in value:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("image_paths 里有空路径")
        raw = raw.strip()
        if not os.path.isabs(raw):
            raise ValueError(f"image_paths 必须是这台机器上的绝对路径：{raw}")
        resolved = os.path.realpath(raw)
        if POST_MEDIA_ROOT:
            root = os.path.realpath(POST_MEDIA_ROOT)
            if resolved != root and not resolved.startswith(root + os.sep):
                raise ValueError(f"image_paths 必须在允许的目录 {POST_MEDIA_ROOT} 下：{raw}")
        if not os.path.isfile(resolved):
            raise ValueError(f"找不到图片文件：{raw}")
        ext = os.path.splitext(resolved)[1].lower()
        if ext not in IMAGE_EXTENSIONS:
            raise ValueError(
                f"不支持的图片格式 {ext or '(无扩展名)'}，支持：{', '.join(sorted(IMAGE_EXTENSIONS))}"
            )
        size = os.path.getsize(resolved)
        if size <= 0:
            raise ValueError(f"图片文件是空的：{raw}")
        if size > MAX_IMAGE_BYTES:
            raise ValueError(
                f"图片文件过大（{size / 1024 / 1024:.0f}MB），上限 {MAX_IMAGE_MB}MB"
                "（DY_POST_MAX_IMAGE_MB 可调）"
            )
        resolved_paths.append(resolved)
    return resolved_paths


def _build_caption(raw_caption, tags):
    caption = (raw_caption or "").strip()
    if not caption:
        raise ValueError("缺参数 caption：发布视频需要文案")
    if tags is not None:
        if not isinstance(tags, list):
            raise ValueError("tags 必须是字符串数组")
        extra = []
        for t in tags:
            if not isinstance(t, str) or not t.strip():
                continue
            token = "#" + t.strip().lstrip("#")
            if token not in caption and token not in extra:
                extra.append(token)
        if extra:
            caption = (caption + " " + " ".join(extra)).strip()
    if len(caption) > MAX_CAPTION_CHARS:
        raise ValueError(
            f"caption 过长（含话题标签共 {len(caption)} 字），需 ≤{MAX_CAPTION_CHARS} 字"
            "（这是保守估计，没有对着真实页面验证过抖音实际上限）"
        )
    return caption


async def _wait_for_upload_ready(page, timeout_ms=180000):
    """等视频上传 + 抖音端转码完成。创作者后台同样没有稳定 class 名可等
    （见文件头部关于抖音 CSS 类名的说明），这里退而求其次：轮询"发布"按钮
    从禁用变成可点——不管中间上传进度条/预览长什么样，转码没走完这个按钮
    大概率是不可点的，等它翻转比等某个具体选择器更抗改版。期间照常检查
    验证码/风控和登录态，不做假设。这段没有对着真实登录态跑通过，是
    action_post 里最需要冒烟测试校准的一处。"""
    deadline = time.time() + timeout_ms / 1000
    # 2026-08-23 实测发现：这里原来用的子串匹配 re.compile("发布") 会连带
    # 匹配上左侧导航栏那个常驻的图标按钮（无障碍名称是"作品发布"，页面一
    # 加载就存在、从来不禁用），.first 永远先抓到这个不相关的导航图标，
    # 不是表单最下面真正的提交按钮——导致"等按钮从禁用变可点"形同虚设
    # （导航图标从一开始就是"可点"的），真正点击发布时也可能点错。改成
    # 锚定整个无障碍名称必须精确等于"发布"两个字，排除"作品发布""发布
    # 视频""发布图文"这些包含但不等于"发布"的同名坑。
    publish_btn = page.get_by_role("button", name=re.compile(r"^发布$"))
    while time.time() < deadline:
        await _raise_if_challenge(page)
        login_error = await _login_error(page)
        if login_error:
            return login_error
        try:
            if await publish_btn.count() and await publish_btn.first.is_visible():
                if not await publish_btn.first.is_disabled():
                    return None
        except Exception:
            pass
        await page.wait_for_timeout(800)
    return "等待视频上传/转码超时：文件可能过大/过长，或者创作者后台页面结构已变化"


async def _fill_caption(page, caption):
    box = page.locator(
        '[contenteditable="true"][data-placeholder*="标题" i], '
        '[contenteditable="true"][data-placeholder*="描述" i], '
        'div.zone-container[contenteditable="true"], '
        'textarea[placeholder*="描述" i], textarea[placeholder*="标题" i]'
    ).first
    await box.wait_for(timeout=10000)
    await _guarded_click(page, box)
    # 有些草稿态会带占位/历史内容，先全选清空，避免和已有内容拼在一起。
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await _human_type(page, caption)


async def _wait_for_images_ready(page, timeout_ms=60000):
    """图片不需要转码，上传比视频快得多，原来的思路是用"发布"按钮从禁用变
    可点当信号，比等某个具体缩略图选择器更抗改版。

    2026-08-23 实测推翻了这个假设的前半句：发布按钮实测在文件刚 set_input_
    files 之后大约 1 秒就已经是可点状态，而这时候页面上"取消上传"这个上传
    进度提示还要再等 4~5 秒才消失——也就是说按钮"可点"这件事跟图片是否真的
    传完之间没有可靠的因果关系，按钮几乎从一开始就是可点的。这解释了生产环境
    里出现过的"接口返回了成功状态码，但响应体里没有 status_code 字段"：
    图片没传完就点了发布，create_v2 请求确实发出去了，但服务端拿到的是个
    不完整的状态，回来的响应体是空的。现在改成必须同时满足两个条件：页面上
    已经不再显示"取消上传"（图片真的传完了）+ 发布按钮可点，两个条件都满足
    才返回就绪，不再只看按钮。"""
    deadline = time.time() + timeout_ms / 1000
    # 2026-08-23 实测发现：这里原来用的子串匹配 re.compile("发布") 会连带
    # 匹配上左侧导航栏那个常驻的图标按钮（无障碍名称是"作品发布"，页面一
    # 加载就存在、从来不禁用），.first 永远先抓到这个不相关的导航图标，
    # 不是表单最下面真正的提交按钮——导致"等按钮从禁用变可点"形同虚设
    # （导航图标从一开始就是"可点"的），真正点击发布时也可能点错。改成
    # 锚定整个无障碍名称必须精确等于"发布"两个字，排除"作品发布""发布
    # 视频""发布图文"这些包含但不等于"发布"的同名坑。
    publish_btn = page.get_by_role("button", name=re.compile(r"^发布$"))
    still_uploading = page.get_by_text("取消上传", exact=False)
    while time.time() < deadline:
        await _raise_if_challenge(page)
        login_error = await _login_error(page)
        if login_error:
            return login_error
        try:
            if await still_uploading.count():
                pass  # 还在传，跳过这一轮，不去看按钮状态
            elif await publish_btn.count() and await publish_btn.first.is_visible():
                if not await publish_btn.first.is_disabled():
                    return None
        except Exception:
            pass
        await page.wait_for_timeout(500)
    return "等待图片上传超时：文件可能过多/过大，或者创作者后台页面结构已变化"


async def _switch_to_image_mode(page):
    """上传页默认是视频模式，图文发布要先切到"图文"这个 tab——创作者后台
    没有稳定 class 名可用（见文件头说明），这里用"文本里含'图文'的可点
    元素"这个最粗但最抗改版的信号去定位。这是 post_images 整个实现里最不
    确定的一步，没有对着真实登录态跑通过，找不到就明确报错，绝不静默留在
    视频模式误发成视频。"""
    tab = page.get_by_text(re.compile("图文")).first
    try:
        await tab.wait_for(timeout=8000)
    except PWTimeout:
        return "找不到图文发布入口，创作者后台页面结构可能已变化（也可能这个账号没有图文发布权限）"
    await _guarded_click(page, tab)
    return None


async def _publish_and_verify(page, action="post"):
    """点发布按钮 + 三态验证，post/post_images 共用，不重复写一遍——两者
    发布之后的确认逻辑（网络响应 + 跳转作品管理页）完全一致，差异只在
    上传阶段。action 只是用来在返回结果里如实标注是哪个动作，不影响验证
    逻辑本身。"""
    await _raise_if_challenge(page)
    # 2026-08-23 实测发现：这里原来用的子串匹配 re.compile("发布") 会连带
    # 匹配上左侧导航栏那个常驻的图标按钮（无障碍名称是"作品发布"，页面一
    # 加载就存在、从来不禁用），.first 永远先抓到这个不相关的导航图标，
    # 不是表单最下面真正的提交按钮——导致"等按钮从禁用变可点"形同虚设
    # （导航图标从一开始就是"可点"的），真正点击发布时也可能点错。改成
    # 锚定整个无障碍名称必须精确等于"发布"两个字，排除"作品发布""发布
    # 视频""发布图文"这些包含但不等于"发布"的同名坑。
    publish_btn = page.get_by_role("button", name=re.compile(r"^发布$"))
    try:
        await publish_btn.first.wait_for(timeout=10000)
    except PWTimeout:
        return _failed("找不到发布按钮，创作者后台页面结构可能已变化", action=action, changed=True)
    if await publish_btn.first.is_disabled():
        return _failed("发布按钮是灰的：可能还没上传完，或者文案不合规", action=action, changed=True)

    # 2026-08-23 实测连续踩到两次同一类坑：markers 原来是
    # ("creator", "aweme/v1/creator", "content/post", "/janus/")。裸词
    # "creator" 因为整个创作者中心就跑在 creator.douyin.com 上，几乎匹配
    # 页面发出的所有请求——文案带话题标签时，点发布那一刻会顺带发一个话题
    # 联想请求（url 含 aweme/v1/search/challengesug），比真正的发布请求先
    # 返回。去掉裸 "creator" 后以为 "aweme/v1/creator" 够精确，结果它又碰上
    # 了另一个无关请求：封面图片取 URL 的接口（url 含
    # aweme/v1/creator/get/url/）。这两个无关请求返回的响应体里都带着
    # status_code: 0 这个字节系接口通用的"成功"外壳字段（不是发布专属的），
    # 导致 _publish_and_verify() 连续两次把无关请求的 status_code: 0 误判成
    # 发布成功，实测创作者中心后台根本没有对应的新作品。这里只保留唯一一个
    # 经过真实发布反复验证过的关键词——create_v2 本身（发布接口的真实路径
    # 是 .../web/api/media/aweme/create_v2/...），不再用任何"看起来应该
    # 是它但没验证过"的宽泛关键词去赌。真发布走的是这个接口时能精确抓到；
    # 万一某次发布走了别的路径导致没抓到，也只会落到下面 uncertain 分支
    # 如实说不确定，不会再错误地"确认"一次没发生的发布。
    network = await _click_with_response(
        page, publish_btn.first.click,
        ("aweme/create_v2",),
    )
    if network.get("captured") and int(network.get("status") or 0) >= 400:
        return _failed(f"抖音接口返回 HTTP {network['status']}", action=action, changed=True,
                       network=_network_public(network))

    # 2026-08-23 实测发现：HTTP 状态码在这个接口上不可信——create_v2 即使
    # 发布真的失败，也照样回 HTTP 200，真正的成功/失败信号在响应体的
    # status_code 字段里（0 = 成功，非 0 = 失败，抖音这类字节系接口的通用
    # 约定）。之前只看 HTTP 状态码 + 有没有跳转到作品管理页，等于压根没读
    # 接口已经告诉我们的真实结果，导致一次真实失败的发布被误判成
    # "uncertain"。这里改成优先信 status_code：非 0 直接判定失败，0 则视为
    # 真的发布成功，不用再赌页面会不会跳转——跳转只是锦上添花的视觉确认，
    # 跳转慢或没跳转不该推翻接口已经明确给出的成功信号。
    payload = network.get("payload") if isinstance(network.get("payload"), dict) else None
    status_code = payload.get("status_code") if payload else None
    if status_code not in (None, 0):
        status_msg = (payload.get("status_msg") or payload.get("status_message")
                      or "抖音接口未说明具体原因")
        return _failed(f"抖音接口返回失败（status_code={status_code}：{status_msg}）",
                       action=action, changed=True, network=_network_public(network))

    try:
        # 20s 放宽到 30s：实测发布之后偶尔会有几秒到十几秒的跳转延迟，
        # 20s 在正常路径下也偶尔差一点点等不到（不是前面提到的 fast_detect
        # 预审那种分钟级延迟，那种情况下等多久都等不到，放宽到 30s 只是
        # 让正常路径的偶发慢跳转不至于被误判成 uncertain）。
        await page.wait_for_url(re.compile(r"/creator-micro/content/manage"), timeout=30000)
        return _confirmed("发布后跳转到了作品管理页，视觉确认发布成功",
                           action=action, network=_network_public(network))
    except PWTimeout:
        await _raise_if_challenge(page)
        if status_code == 0:
            return _confirmed(
                "接口返回 status_code=0（发布成功），页面跳转比预期慢没等到，"
                "但接口已经明确确认发布成功",
                action=action, network=_network_public(network))
        if network.get("captured") and 200 <= int(network.get("status") or 0) < 300:
            return _uncertain(
                "接口返回了成功状态码，但响应体里没有 status_code 字段可以确认，"
                "页面也没有跳转到作品管理页，禁止自动重试",
                action=action, network=_network_public(network))
        # 2026-08-23 实测发现：点发布之后，抖音有时不会直接调用 create_v2，
        # 而是先走一条"发文助手/快速检测"的异步内容预审流程
        # （aweme/v1/post_assistant/fast_detect/poll），实测这个检测流程
        # 完整跑完要一分钟以上，跑完之后也不一定马上跳转发布——这条路径这次
        # 没能在合理时间内等到最终结果，具体是不是走了这条预审流程、给用户
        # 更准确的提示，靠页面上"发文助手"这几个字做个弱信号判断，判断不到
        # 就是真的什么信息都没有，如实说不确定就行，不猜。
        extra_hint = ""
        try:
            page_text = await page.evaluate("() => document.body.innerText")
            if "发文助手" in page_text or "检测" in page_text:
                extra_hint = (
                    "（页面上出现了'发文助手/内容检测'相关文字，这次发布可能被抖音"
                    "转去做异步内容预审了，这个流程实测要一分钟以上才有结果，"
                    "不是脚本卡住——建议过一会儿去创作者中心的'内容管理'里人工确认）"
                )
        except Exception:
            pass
        return _uncertain(
            f"已经点击发布，但既没有跳转到成功页也没抓到接口响应，禁止自动重试{extra_hint}",
            action=action, network=_network_public(network))


async def action_post(page, body):
    try:
        caption = _build_caption(body.get("caption"), body.get("tags"))
        video_path = _safe_video_path(body.get("video_path"))
    except ValueError as exc:
        return _failed(str(exc))

    await _goto(page, CREATOR_BASE + "/creator-micro/content/upload")
    login_error = await _login_error(page)
    if login_error:
        return _failed(login_error)

    # 用 accept 属性锁定视频输入框，不能用 .first——见 action_post_images()
    # 里同一处的注释，这两个动作是同一个坑的两半。
    file_input = page.locator('input[type="file"][accept*="video"]').first
    try:
        await file_input.wait_for(state="attached", timeout=10000)
    except PWTimeout:
        return _failed("找不到上传视频的文件选择框，创作者后台页面结构可能已变化")
    await file_input.set_input_files(video_path)

    upload_error = await _wait_for_upload_ready(page)
    if upload_error:
        # 视频文件已经交给页面处理，不能算"什么都没发生"；但也没能确认发布
        # 动作本身发没发生，按失败处理但把 changed 标 true 提醒调用方别裸重传。
        return _failed(upload_error, changed=True)

    try:
        await _fill_caption(page, caption)
    except PWTimeout:
        return _failed("找不到文案输入框，创作者后台页面结构可能已变化", changed=True)

    await _raise_if_challenge(page)
    # 2026-08-23 实测发现：这里原来用的子串匹配 re.compile("发布") 会连带
    # 匹配上左侧导航栏那个常驻的图标按钮（无障碍名称是"作品发布"，页面一
    # 加载就存在、从来不禁用），.first 永远先抓到这个不相关的导航图标，
    # 不是表单最下面真正的提交按钮——导致"等按钮从禁用变可点"形同虚设
    # （导航图标从一开始就是"可点"的），真正点击发布时也可能点错。改成
    # 锚定整个无障碍名称必须精确等于"发布"两个字，排除"作品发布""发布
    # 视频""发布图文"这些包含但不等于"发布"的同名坑。
    publish_btn = page.get_by_role("button", name=re.compile(r"^发布$"))
    try:
        await publish_btn.first.wait_for(timeout=10000)
    except PWTimeout:
        return _failed("找不到发布按钮，创作者后台页面结构可能已变化", changed=True)
    if await publish_btn.first.is_disabled():
        return _failed("发布按钮是灰的：视频可能还没转码完，或者文案不合规", changed=True)

    return await _publish_and_verify(page)


async def action_post_images(page, body):
    """图文发布：跟 action_post 共用登录检查/文案填写/发布验证，差异只在
    "先切图文 tab" + "不需要等转码，等按钮可点就行"这两步——见
    _switch_to_image_mode() / _wait_for_images_ready() 的注释，这两处是
    整个动作里最需要冒烟测试校准的地方。"""
    try:
        caption = _build_caption(body.get("caption"), body.get("tags"))
        image_paths = _safe_image_paths(body.get("image_paths"))
    except ValueError as exc:
        return _failed(str(exc), action="post_images")

    await _goto(page, CREATOR_BASE + "/creator-micro/content/upload")
    login_error = await _login_error(page)
    if login_error:
        return _failed(login_error, action="post_images")

    mode_error = await _switch_to_image_mode(page)
    if mode_error:
        return _failed(mode_error, action="post_images")

    # 2026-08-23 实测发现的真实 bug：切到"图文" tab 之后，页面上同时挂着
    # 两个 <input type="file">——视频 tab 那个（accept 含 video/*，DOM 里排
    # 在前面，因为视频 tab 是默认先挂载的）和图文 tab 自己这个（accept 含
    # image/*）。用 `.first` 永远选中的是前一个视频输入框：把图片文件设置
    # 进一个视频输入框，页面完全没反应，"添加作品描述"这个 contenteditable
    # 框根本不会挂载，导致后面 _fill_caption() 报"找不到文案输入框"——
    # 表面症状是文案填不进去，根子其实是图片压根没传上去。用 accept 属性
    # 精确锁定图片输入框，跳过这个"同一个 DOM 里混了两个同名 input"的陷阱。
    file_input = page.locator('input[type="file"][accept*="image"]').first
    try:
        await file_input.wait_for(state="attached", timeout=10000)
    except PWTimeout:
        return _failed("找不到上传图片的文件选择框，创作者后台页面结构可能已变化", action="post_images")
    await file_input.set_input_files(image_paths)

    upload_error = await _wait_for_images_ready(page)
    if upload_error:
        # 图片已经交给页面处理，不能算"什么都没发生"，理由跟 action_post
        # 里同样的注释一致。
        return _failed(upload_error, action="post_images", changed=True)

    try:
        await _fill_caption(page, caption)
    except PWTimeout:
        return _failed("找不到文案输入框，创作者后台页面结构可能已变化", action="post_images", changed=True)

    return await _publish_and_verify(page, action="post_images")


READ_ACTIONS = {
    "feed": action_feed,
    "search": action_search,
    "read": action_read,
    "profile": action_profile,
    "user_posts": action_user_posts,
    "im_conversations": action_im_conversations,
    "im_messages": action_im_messages,
}

# 私信动作用那个常驻私信页，不像别的动作每次新开一页，见 _get_im_page()。
# im_send 是写动作但也在这里：它同样要用那一页（而且必须是同一页——
# openImConversation() 是在页面里发事件让 IM 面板切会话的，换一页就得
# 重新等 IM 长连接建起来）。写动作的频率闸/审计/三态结果照旧走 WRITE_ACTIONS
# 那条路，两个集合互不冲突，见 douyin() 里的分发。
IM_ACTIONS = {"im_conversations", "im_messages", "im_send"}
WRITE_ACTIONS = {
    "post": action_post,
    "post_images": action_post_images,
    "im_send": action_im_send,
}
ACTIONS = {**READ_ACTIONS, **WRITE_ACTIONS}


@app.get("/health")
async def health():
    try:
        context = await get_context()
        return {
            "ok": True,
            "connected": bool(_browser and _browser.is_connected()),
            "contexts": len(_browser.contexts),
            "existing_tabs": len(context.pages),
            "read_actions": sorted(READ_ACTIONS),
            "write_actions": sorted(WRITE_ACTIONS),
            "publish_rate_limit_per_hour": RATE_LIMITS.get("publish"),
            "im_send_rate_limit_per_hour": RATE_LIMITS.get("im_send"),
            "browse_session": bool(_browse_session_alive(_browse_session)),
            "im_page": _im_page_alive(),
            "browse_ttl_seconds": BROWSE_TTL_SECONDS,
            "im_page_ttl_seconds": IM_PAGE_TTL_SECONDS,
            "owned_tabs": len(_owned_targets),
            "orphan_minutes": ORPHAN_MINUTES,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/douyin")
async def douyin(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "请求体必须是 JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "请求体必须是 JSON object"}

    action = body.get("action")
    is_write = action in WRITE_ACTIONS
    if action not in ACTIONS:
        return {
            "ok": False,
            "error": "不支持的 action",
            "allowed_read_actions": sorted(READ_ACTIONS),
            "allowed_write_actions": sorted(WRITE_ACTIONS),
        }

    def _audit(result):
        # post/post_images 两个写动作共用一条日志格式，两边都记，缺的那个
        # 字段自然是 None——跟 twitter-tool 的 actions.log 同一个约定，出了
        # 事有账可查。
        #
        # im_send 只记会话 id、字数和前 20 个字，不记整条私信正文：审计要
        # 回答的是"什么时候往哪个会话发过东西、发没发成功"，这几个字段已经
        # 够了；而私信正文是两个人之间的私人对话，没有理由为了排查方便就在
        # 磁盘上留一份全文副本（这个日志文件不轮转、不加密）。留前 20 个字
        # 是为了万一发重了/发错了能对得上是哪一条。
        im_text = body.get("text") if action == "im_send" else None
        log_action({"action": action, "video_path": body.get("video_path"),
                    "image_paths": body.get("image_paths"),
                    "caption": body.get("caption"), "tags": body.get("tags"),
                    "conversation_id": body.get("conversation_id"),
                    "text_chars": len(im_text) if isinstance(im_text, str) else None,
                    "text_head": im_text[:20] if isinstance(im_text, str) else None,
                    "result": result})

    async with ACTION_LOCK:
        page = None
        keep_page = False
        if is_write:
            # 频率闸判断 + 记账必须在同一把锁内完成，两个并发请求不能同时
            # 穿过最后一个名额——douyin-tool 本来就用 ACTION_LOCK 把所有
            # 动作串成一条队（见文件头注释：一次只开一个工作页），这里不用
            # 再单独引入一把写锁，天然满足这个要求。
            gate = check_rate(action)
            if gate:
                result = _failed(gate)
                _audit(result)
                return result
        try:
            if _browse_session and not _browse_session_alive(_browse_session):
                await _close_browse_session("expired_before_action")
            if action in {"search", "profile"}:
                await _close_browse_session("replaced_by_list_action")

            context = await get_context()
            raw_read_url = body.get("url")
            video_id = (
                _video_id_from_url(raw_read_url)
                if action == "read" and isinstance(raw_read_url, str)
                else None
            )
            use_cached_list = bool(
                action == "read"
                and _browse_session_alive(_browse_session)
                and video_id in _browse_session["ids"]
            )
            if action in IM_ACTIONS:
                # 常驻私信页由 _get_im_page()/_session_sweeper() 管生命周期，
                # 不能被下面 finally 里那句"用完就关"收掉。
                page = await _get_im_page(context)
                keep_page = True
                result = await ACTIONS[action](page, body)
            elif use_cached_list:
                page = _browse_session["page"]
                keep_page = True
                result = await action_read(page, body, from_list=True)
            else:
                page = await new_work_page(context)
                result = await ACTIONS[action](page, body)

            if action in {"search", "profile"} and result.get("ok"):
                items = result.get("items") or []
                if items:
                    _install_browse_session(page, items, action)
                    keep_page = True
                    result["read_navigation"] = "card_click_available"

            if is_write:
                if result.get("changed") and result.get("outcome") in ("confirmed", "uncertain"):
                    record_rate(action)
                _audit(result)
            return result
        except asyncio.CancelledError:
            raise
        except ChallengeDetected as exc:
            if _browse_session and _browse_session.get("page") is page:
                await _close_browse_session("challenge")
                keep_page = False
            if is_write:
                _audit(exc.result)
            return exc.result
        except Exception as exc:
            if is_write:
                # 写动作异常不能直接报"失败"——点击可能已经生效，二态判断在
                # 这里等于允许调用方自动重试、随时重复发布，跟 twitter-tool
                # 的写路径同一个考虑。
                result = _uncertain(
                    f"post 执行中异常：{type(exc).__name__}: {exc}；是否生效不确定，禁止自动重试"
                )
                _audit(result)
                return result
            return {"ok": False, "error": f"{action} 失败：{exc}"}
        finally:
            if page is not None:
                # 用完就关的页走台账关闭（顺带从 _owned_targets 里摘掉）；
                # 留着复用的页（私信页、列表页）只把静默计时清零，免得它在
                # 还在用的时候被定期清扫当成孤儿收掉。
                if keep_page:
                    _touch_owned_page(page)
                else:
                    with suppress(Exception):
                        await _close_owned_page(page)


async def _session_sweeper():
    while True:
        await asyncio.sleep(30)
        async with ACTION_LOCK:
            if _browse_session and not _browse_session_alive(_browse_session):
                await _close_browse_session("expired")
            # 闲置太久的私信页也收掉：私信是十分钟一次的轮询，闲下来之后没
            # 必要一直占着一个抖音首页标签页（那一页还挂着 IM websocket）。
            if _im_page_alive() and time.time() - _im_page_touched_at > IM_PAGE_TTL_SECONDS:
                await _close_im_page("idle_expired")
            # 最后一道网：上面两条管的是"正常用完该收的页"，这一条管的是
            # "根本没走到收尾那一步"的页（动作抛异常、进程被 kill、重启时
            # 有请求在飞）。见 _owned_targets 上面那段——这台机器上两个抖音
            # 页就能把 relay 顶死，孤儿页不是洁癖问题是可用性问题。
            await _sweep_owned_orphans()


@app.on_event("startup")
async def startup():
    global _sweeper_task
    _load_state()
    # 开机先扫一次：上一条命留下的孤儿页就靠这次清掉（台账是落盘的，重启
    # 之后照样认得出来）。这次清扫在这里做而不是等第一次定期清扫，是因为
    # 服务重启后紧接着就可能来请求，那时候再开一个页就是两个页了。
    with suppress(Exception):
        result = await _sweep_owned_orphans()
        if result and result.get("closed"):
            log_action({"action": "_startup_sweep_orphans", "closed": result["closed"]})
    _sweeper_task = asyncio.create_task(_session_sweeper())


@app.on_event("shutdown")
async def shutdown():
    """只断开 Playwright 控制连接，绝不关闭常驻 Chrome。"""
    global _pw, _browser, _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper_task
        _sweeper_task = None
    await _close_browse_session("service_shutdown")
    await _close_im_page("service_shutdown")
    _browser = None
    if _pw is not None:
        with suppress(Exception):
            await asyncio.wait_for(_pw.stop(), timeout=5)
        _pw = None
