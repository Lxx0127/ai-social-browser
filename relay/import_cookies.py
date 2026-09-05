#!/usr/bin/env python3
"""把本地电脑 Chrome 导出的 cookie 灌进服务器这只常驻 Chrome 的 cookie jar。

为什么要这么做：抖音的滑块验证图（错误码 5202）在美国 IP 上加载不出来，
服务器上根本走不完登录流程。于是改成"在国内自己电脑上登录 → 导出 cookie →
灌进服务器"。

为什么走 CDP 而不是直接改 profile 里的 Cookies 这个 sqlite 文件：
1. Chrome 正在跑，sqlite 被独占且内存里有一份缓存，外面写了会被覆盖；
2. cookie 的 value 在 Linux 上是加密存的（v10/v11，密钥来自 keyring 或
   写死的 "peanuts"），手写要自己做 AES，容易写出脏数据把整个 jar 搞坏。
CDP 的 Network.setCookies 是 Chrome 自己写自己的 jar，编码/加密/过期全由它
处理，写完立刻生效，也会正常落盘到 profile，重启 relay 不丢。

支持两种输入：
- JSON 数组（Cookie-Editor / EditThisCookie 的 Export 格式，也接受
  {"cookies": [...]} 这种包一层的）
- Netscape cookies.txt（# 开头是注释，7 列 tab 分隔）

用法: venv/bin/python import_cookies.py douyin_cookies.json [--domain-filter douyin]
"""

import asyncio
import json
import sys
import urllib.request

CDP_PORT = 9333

# Cookie-Editor 用的是 Chrome 扩展 API 的写法，CDP 要的是首字母大写那套。
_SAMESITE_MAP = {
    "no_restriction": "None",
    "none": "None",
    "lax": "Lax",
    "strict": "Strict",
}


def _load(path):
    raw = open(path, "r", encoding="utf-8-sig").read().strip()
    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("cookies") or data.get("Cookies") or []
        return [_norm_json(c) for c in data]
    return _parse_netscape(raw)


def _norm_json(c):
    out = {
        "name": c.get("name"),
        "value": c.get("value", ""),
        "domain": c.get("domain", ""),
        "path": c.get("path", "/"),
        "secure": bool(c.get("secure", False)),
        "httpOnly": bool(c.get("httpOnly", c.get("httponly", False))),
    }
    exp = c.get("expirationDate") or c.get("expires") or c.get("expiry")
    # session cookie（浏览器关掉就没了）不带 expires 字段，带了反而会被当成
    # 已过期直接丢掉。
    if exp and not c.get("session"):
        out["expires"] = float(exp)
    ss = _SAMESITE_MAP.get(str(c.get("sameSite", "")).lower())
    # SameSite=None 必须配 Secure，否则 Chrome 直接拒收整条 cookie；宁可
    # 不带这个属性（默认 Lax）也不要让整条丢掉。
    if ss and (ss != "None" or out["secure"]):
        out["sameSite"] = ss
    return out


def _parse_netscape(raw):
    cookies = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        c = {
            "name": name, "value": value, "domain": domain, "path": path,
            "secure": secure.upper() == "TRUE", "httpOnly": False,
        }
        if expires and expires != "0":
            c["expires"] = float(expires)
        cookies.append(c)
    return cookies


def _page_ws():
    """挑一个 page target 连上去。Network.setCookies 虽然是 page 域的命令，
    写进去的却是整个 profile 共享的那份 cookie jar，所以随便哪个标签页都行。"""
    pages = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json").read())
    for p in pages:
        if p.get("type") == "page":
            return p["webSocketDebuggerUrl"]
    raise SystemExit("CDP 里没有可用的 page target，relay 的 Chrome 是不是没起？")


async def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    path = sys.argv[1]
    dfilter = None
    if "--domain-filter" in sys.argv:
        dfilter = sys.argv[sys.argv.index("--domain-filter") + 1]

    cookies = [c for c in _load(path) if c.get("name") and c.get("domain")]
    if dfilter:
        cookies = [c for c in cookies if dfilter in c["domain"]]
    if not cookies:
        raise SystemExit("没解析出任何 cookie，检查导出文件格式")

    from websockets.asyncio.client import connect

    async with connect(_page_ws(), max_size=None) as ws:
        i = [0]

        async def cmd(method, params=None):
            i[0] += 1
            await ws.send(json.dumps({"id": i[0], "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == i[0]:
                    return msg

        await cmd("Network.enable")
        r = await cmd("Network.setCookies", {"cookies": cookies})
        if "error" in r:
            raise SystemExit(f"写入失败: {r['error']}")

        print(f"已写入 {len(cookies)} 条 cookie")
        got = await cmd("Network.getCookies", {"urls": ["https://www.douyin.com/"]})
        names = sorted(c["name"] for c in got["result"]["cookies"])
        print(f"douyin.com 现在有 {len(names)} 条: {', '.join(names)}")
        for key in ("sessionid", "sessionid_ss", "sid_tt"):
            print(f"  {key}: {'✓ 在' if key in names else '✗ 缺'}")


asyncio.run(main())
