"""Riot API 最小客户端（只用标准库 urllib）。

只做本项目当前真正需要的事：把某一场的 Match JSON 和 Timeline JSON 抓下来。
API Key 永远只从本机文件 / 环境变量读取，绝不会被打印或写进日志。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import net

# Riot ID 的大区路由（Regional Route），KR 属于 asia
REGIONAL_ROUTES = ("americas", "asia", "europe", "sea")

_SECRET_FILES = ("riot_secret.json", "secret.json", "riot.json", "config.json")
_KEY_FIELDS = ("key", "api_key", "apiKey", "riot_api_key", "riotApiKey", "RIOT_API_KEY")


# Riot 的接入层会拦掉「看起来像脚本」的请求（默认 UA 是 Python-urllib/3.x），
# 表现为 403 —— 和 Key 是否有效无关，极易误判。这组请求头是官方文档示例里的写法。
_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Charset": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://developer.riotgames.com",
}


def _headers(api_key: str) -> dict[str, str]:
    return {**_BASE_HEADERS, "X-Riot-Token": api_key}


class RiotError(RuntimeError):
    """带 HTTP 状态码的 Riot API 错误，方便上层给出人话解释。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def _key_from_file(data_dir: Path) -> str:
    for name in _SECRET_FILES:
        path = data_dir / name
        if not path.is_file():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                obj = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(obj, str) and obj.startswith("RGAPI"):
            return obj.strip()
        if isinstance(obj, dict):
            for field in _KEY_FIELDS:
                value = obj.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _key_from_env() -> str:
    import os

    return (os.environ.get("RIOT_API_KEY") or "").strip()


def resolve_api_key(data_dir: Path) -> tuple[str, str]:
    """返回 (Key, 来源说明)。

    文件优先于环境变量：用户在网页上按了「保存」，那就该用这一个。
    早期版本反过来，导致网页显示「已保存」但实际仍在用旧的环境变量，
    这是一个会让人查半天的坑。
    """
    from_file = _key_from_file(data_dir)
    if from_file:
        return from_file, "riot_secret.json"

    from_env = _key_from_env()
    if from_env:
        return from_env, "环境变量 RIOT_API_KEY"

    raise RiotError(
        0,
        "没有找到 Riot API Key。请在控制台第 2 步粘贴 Key 后点「保存」，"
        f"它会写进 {data_dir / 'riot_secret.json'}",
    )


def load_api_key(data_dir: Path) -> str:
    return resolve_api_key(data_dir)[0]


# 接入层拦掉「请求头」方式时，自动改用网址参数。判定一次后记住，避免每次都白试一遍。
_use_query_param = False


def _build(url: str, api_key: str, as_query: bool) -> urllib.request.Request:
    if not as_query:
        return urllib.request.Request(url, headers=_headers(api_key))
    joiner = "&" if "?" in url else "?"
    full = f"{url}{joiner}{urllib.parse.urlencode({'api_key': api_key})}"
    return urllib.request.Request(full, headers=_BASE_HEADERS)


def get(url: str, api_key: str, retries: int = 4) -> Any:
    """GET 一个 Riot API 地址。

    429 限流会按 Retry-After 自动等待重试；
    403 且是「请求头」方式时，会改用网址参数再试一次 —— 这两种 403 长得一样，
    但一个是 Key 无效、另一个是请求被拦截，必须区分开。
    """
    global _use_query_param
    last: Exception | None = None

    for attempt in range(retries):
        try:
            with net.urlopen(_build(url, api_key, _use_query_param), timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 403 and not _use_query_param:
                # 换一种方式再试一次；成功就说明刚才那个 403 与 Key 无关
                try:
                    with net.urlopen(_build(url, api_key, True), timeout=30) as response:
                        _use_query_param = True
                        return json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as retry_err:
                    raise RiotError(retry_err.code, _explain(retry_err.code, url)) from retry_err
            if err.code == 429:
                wait = int(err.headers.get("Retry-After", "10") or 10)
                time.sleep(min(wait, 120))
                last = err
                continue
            if err.code in (500, 502, 503, 504):
                time.sleep(2 ** attempt)
                last = err
                continue
            raise RiotError(err.code, _explain(err.code, url)) from err
        except urllib.error.URLError as err:
            if net.is_cert_error(err):
                raise RiotError(0, net.CERT_HELP) from err
            time.sleep(2 ** attempt)
            last = err

    if last is not None and net.is_cert_error(last):
        raise RiotError(0, net.CERT_HELP)
    raise RiotError(0, f"请求失败（已重试 {retries} 次）：{last}")


def _explain(status: int, url: str) -> str:
    hints = {
        400: "请求格式不对（通常是 Riot ID 或 matchId 拼错了）",
        401: "没有带 API Key",
        403: "被 Riot 拒绝。可能是 Key 过期（Development Key 24 小时失效），也可能是请求被接入层拦截。请在控制台点「自检」分清是哪一种",
        404: "Riot 说这个资源不存在（检查 matchId / Riot ID / 大区是否正确）",
        429: "触发限流，稍后再试",
    }
    return f"HTTP {status}：{hints.get(status, '未知错误')}  ({url.split('?')[0]})"


def match_url(region: str, match_id: str) -> str:
    return f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}"


def timeline_url(region: str, match_id: str) -> str:
    return f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"


def fetch_match(region: str, match_id: str, api_key: str) -> dict[str, Any]:
    return get(match_url(region, match_id), api_key)


def fetch_timeline(region: str, match_id: str, api_key: str) -> dict[str, Any]:
    return get(timeline_url(region, match_id), api_key)


def guess_region(match_id: str) -> str:
    """从 matchId 前缀猜大区路由，例如 KR_8368663855 -> asia。"""
    platform = match_id.split("_", 1)[0].upper() if "_" in match_id else ""
    mapping = {
        "NA1": "americas", "BR1": "americas", "LA1": "americas", "LA2": "americas",
        "KR": "asia", "JP1": "asia", "OC1": "sea", "PH2": "sea", "SG2": "sea",
        "TH2": "sea", "TW2": "sea", "VN2": "sea",
        "EUW1": "europe", "EUN1": "europe", "TR1": "europe", "RU": "europe",
        "ME1": "europe",
    }
    return mapping.get(platform, "asia")


# ---------------------------------------------------------------- 更多接口

# Riot ID 大区路由 → 平台主机（Summoner/Status 这类接口要用平台主机）
PLATFORM_HOSTS = {
    "KR": "kr", "JP1": "jp1", "NA1": "na1", "BR1": "br1", "LA1": "la1",
    "LA2": "la2", "OC1": "oc1", "EUW1": "euw1", "EUN1": "eun1",
    "TR1": "tr1", "RU": "ru", "PH2": "ph2", "SG2": "sg2", "TH2": "th2",
    "TW2": "tw2", "VN2": "vn2", "ME1": "me1",
}


def verify_key(api_key: str, platform: str = "KR") -> dict[str, Any]:
    """用一个最轻量的接口验证 Key 是否有效。成功返回大区状态。"""
    host = PLATFORM_HOSTS.get(platform.upper(), "kr")
    url = f"https://{host}.api.riotgames.com/lol/status/v4/platform-data"
    return get(url, api_key, retries=1)


def fetch_account(region: str, game_name: str, tag_line: str, api_key: str) -> dict[str, Any]:
    """Riot ID（名字#标签）→ 账号信息（含 PUUID）。"""
    name = urllib.parse.quote(game_name, safe="")
    tag = urllib.parse.quote(tag_line, safe="")
    url = (
        f"https://{region}.api.riotgames.com"
        f"/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
    )
    return get(url, api_key, retries=2)


def fetch_match_ids(
    region: str,
    puuid: str,
    api_key: str,
    count: int = 20,
    queue: int | None = 420,
    start: int = 0,
) -> list[str]:
    """PUUID → 最近 N 场的 matchId 列表。queue=420 是排位 Solo/Duo。"""
    params = {"start": start, "count": max(1, min(100, count))}
    if queue:
        params["queue"] = queue
    url = (
        f"https://{region}.api.riotgames.com"
        f"/lol/match/v5/matches/by-puuid/{puuid}/ids?"
        + urllib.parse.urlencode(params)
    )
    return get(url, api_key, retries=3)


def save_api_key(data_dir: Path, key: str) -> Path:
    """把 Key 写进 data/riot_secret.json（和现有网站用的是同一个文件）。"""
    key = key.strip()
    if not key:
        raise RiotError(0, "Key 是空的。")
    path = data_dir / "riot_secret.json"
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}
    existing["key"] = key
    existing["savedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data_dir.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(existing, handle, ensure_ascii=False, indent=2)
    try:
        path.chmod(0o600)  # 只有你自己能读
    except OSError:
        pass
    return path


def mask(key: str) -> str:
    """给人看的遮蔽形式，永远不暴露完整 Key。"""
    if not key:
        return ""
    return f"{key[:9]}…{key[-4:]}" if len(key) > 16 else "已保存"


def key_info(data_dir: Path) -> dict[str, Any]:
    """Key 的遮蔽形式与保存时间。Development Key 24 小时过期，需要提前提醒。"""
    info: dict[str, Any] = {
        "masked": "", "savedAt": None, "ageHours": None,
        "expired": None, "source": None, "envConflict": False,
    }
    try:
        key, source = resolve_api_key(data_dir)
    except RiotError:
        return info
    info["masked"] = mask(key)
    info["source"] = source

    # 环境变量存在但和实际使用的不是同一个，必须说出来
    env = _key_from_env()
    info["envConflict"] = bool(env and env != key)

    path = data_dir / "riot_secret.json"
    if not path.is_file():
        return info
    try:
        with path.open("r", encoding="utf-8") as handle:
            saved = json.load(handle).get("savedAt")
    except (OSError, ValueError, AttributeError):
        return info
    if not isinstance(saved, str):
        return info

    info["savedAt"] = saved
    try:
        stamp = time.mktime(time.strptime(saved, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return info
    hours = (time.time() - stamp) / 3600
    info["ageHours"] = round(hours, 1)
    info["expired"] = hours >= 24
    return info


# Riot 的 Development Key 形如 RGAPI-8位-4位-4位-4位-12位（十六进制），总长 42
_KEY_PATTERN = __import__("re").compile(
    r"^RGAPI-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    __import__("re").IGNORECASE,
)
KEY_LENGTH = 42


def check_key_format(key: str) -> tuple[bool, str]:
    """检查 Key 的形状是否正常。粘贴时少复制几个字符是很常见的失误，
    而遮蔽形式只显示头尾，中间坏了看不出来，所以这里按长度和格式明确报出。"""
    key = key.strip()
    if not key:
        return False, "Key 是空的。"
    if not key.startswith("RGAPI-"):
        return False, "不像 Riot 的 Key —— 正常的 Key 以 RGAPI- 开头。"
    if len(key) != KEY_LENGTH:
        return False, (
            f"长度不对：这串是 {len(key)} 个字符，正常的 Development Key 是 "
            f"{KEY_LENGTH} 个。多半是复制时漏了尾巴，或者混进了空格/换行。"
            "回 Riot 页面用复制按钮重新复制一次，不要手动拖选。"
        )
    if not _KEY_PATTERN.match(key):
        return False, (
            f"长度对（{len(key)} 个字符）但格式不对 —— 正常形如 "
            "RGAPI-8位-4位-4位-4位-12位 的十六进制。可能混进了看不见的字符。"
        )
    return True, f"格式正常（{len(key)} 个字符）"


def diagnose(data_dir: Path, platform: str = "KR") -> dict[str, Any]:
    """自检：用同一个 Key，分别以「请求头」和「网址参数」两种方式访问同一个接口。

    这能分清两件长得一样、原因完全不同的 403：
      · 两种都失败  → Key 本身的问题
      · 只有请求头失败 → 请求被接入层拦截，与 Key 无关
    """
    out: dict[str, Any] = {"platform": platform}
    try:
        key, source = resolve_api_key(data_dir)
    except RiotError as err:
        return {**out, "error": str(err)}

    shape_ok, shape_msg = check_key_format(key)
    out.update(
        {
            "masked": mask(key),
            "length": len(key),
            "source": source,
            "shapeOk": shape_ok,
            "shapeMessage": shape_msg,
        }
    )

    host = PLATFORM_HOSTS.get(platform.upper(), "kr")
    url = f"https://{host}.api.riotgames.com/lol/status/v4/platform-data"

    def attempt(label: str, request: urllib.request.Request) -> dict[str, Any]:
        try:
            with net.urlopen(request, timeout=20) as response:
                return {"method": label, "ok": True, "status": response.status}
        except urllib.error.HTTPError as err:
            return {"method": label, "ok": False, "status": err.code}
        except Exception as err:  # noqa: BLE001 - 自检要把任何失败都显示出来
            return {"method": label, "ok": False, "status": 0, "error": str(err)}

    out["header"] = attempt(
        "请求头 X-Riot-Token", urllib.request.Request(url, headers=_headers(key))
    )
    out["query"] = attempt(
        "网址参数 api_key",
        urllib.request.Request(
            f"{url}?{urllib.parse.urlencode({'api_key': key})}", headers=_BASE_HEADERS
        ),
    )

    out["usingQueryParam"] = _use_query_param
    header_ok, query_ok = out["header"]["ok"], out["query"]["ok"]
    if header_ok:
        out["verdict"] = "✅ 一切正常，可以开始同步。"
    elif query_ok:
        out["verdict"] = (
            "⚠️ Key 是好的，但「请求头」方式被拦截了 —— 工具会自动改用网址参数方式。"
        )
    elif out["header"]["status"] == 0 and out["query"]["status"] == 0:
        # 压根没连上 Riot（网络 / 证书 / 代理），这时候怪 Key 是错的
        detail = out["header"].get("error") or out["query"].get("error") or ""
        out["verdict"] = (
            "❌ 根本没连上 Riot，两次都没拿到回应 —— 这是网络或证书问题，"
            f"和 Key 无关。\n{detail}"
        )
    elif not shape_ok:
        out["verdict"] = f"❌ Key 的格式就不对：{shape_msg}"
    else:
        codes = f"请求头 HTTP {out['header']['status']}、网址参数 HTTP {out['query']['status']}"
        out["verdict"] = (
            f"❌ 两种方式都被 Riot 拒绝（{codes}）。请求本身没问题（两种发法都试过了），"
            "是 Riot 不认这个 Key。\n\n"
            "最常见的原因：存在这里的不是 Riot 页面上当前显示的那一串。"
            "**每点一次 REGENERATE，上一个 Key 会立刻作废** —— 所以不要再重新生成了，"
            "直接打开 Riot Developer Portal，把页面上**现在显示的**那串复制过来保存，"
            f"然后看上面的遮蔽形式有没有从 {out['masked']} 变成别的。没变就是没粘进去。"
        )
    return out


# 肉眼极易混淆的字符。职业选手 ID 里 I/l、0/O 抄错是最常见的失败原因。
_LOOKALIKES = {"I": "l", "l": "I", "0": "O", "O": "0", "1": "l"}
_MAX_VARIANTS = 24


def name_variants(name: str, cap: int = _MAX_VARIANTS) -> list[str]:
    """原样优先，然后逐个替换易混淆字符，最后是全部替换的版本。"""
    out = [name]
    spots = [i for i, ch in enumerate(name) if ch in _LOOKALIKES]

    for i in spots:  # 一次只换一个位置
        swapped = name[:i] + _LOOKALIKES[name[i]] + name[i + 1 :]
        if swapped not in out:
            out.append(swapped)

    if len(spots) > 1:  # 全部一起换
        chars = list(name)
        for i in spots:
            chars[i] = _LOOKALIKES[chars[i]]
        swapped = "".join(chars)
        if swapped not in out:
            out.append(swapped)

    return out[:cap]


def tag_variants(tag: str) -> list[str]:
    out: list[str] = []
    for candidate in (tag, tag.upper(), tag.lower(), tag.capitalize()):
        if candidate not in out:
            out.append(candidate)
    return out


def find_account(
    region: str,
    game_name: str,
    tag_line: str,
    api_key: str,
    log: Any = None,
) -> tuple[dict[str, Any], str]:
    """查账号；原样查不到时，自动试易混淆的写法。

    返回 (账号信息, 实际生效的 Riot ID)。全都查不到才抛错。
    """
    tried: list[str] = []
    for name in name_variants(game_name):
        for tag in tag_variants(tag_line):
            riot_id = f"{name}#{tag}"
            if riot_id in tried:
                continue
            tried.append(riot_id)
            try:
                account = fetch_account(region, name, tag, api_key)
            except RiotError as err:
                if err.status == 404:
                    continue
                raise  # 401/403/429 这些不是「拼错了」，直接往上抛
            if len(tried) > 1 and log:
                log(f"  ℹ️ 原样查不到，实际匹配到的是：{riot_id}")
            return account, riot_id

    raise RiotError(
        404,
        f"查不到这个账号。已经试过 {len(tried)} 种写法（含 I/l、0/O 等易混淆字符和"
        f"标签大小写），都不存在。\n"
        f"试过的前几种：{'、'.join(tried[:6])}\n"
        f"可能是这名选手改名了 —— 职业选手改名很常见，这也是为什么应该用 PUUID "
        f"而不是 Riot ID 长期追踪身份。请到 op.gg 或 deeplol.gg 查一下当前的 Riot ID。",
    )
