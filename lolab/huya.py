"""虎牙录像下载：URL → m3u8 → 最高画质 → FFmpeg → 命名好的 MP4。

把原来「DevTools 手工找 m3u8 + 手工敲 ffmpeg」的流程自动化，
并支持批量输入和多任务并发，不用开一堆 Terminal。

如果自动解析失败（虎牙随时可能改页面结构），
永远还有一条已经验证可用的退路：
    python3 -m lolab huya --m3u8 "你从 DevTools 复制的完整 m3u8 地址" --name 文件名
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MOMENT_API = "https://liveapi.huya.com/moment/getMomentContent?videoId={video_id}"

# 虎牙画质名 → 排序权重（拿不到分辨率时的兜底）
_DEF_RANK = {
    "原画": 100, "蓝光": 90, "蓝光8M": 92, "蓝光4M": 91,
    "超清": 80, "高清": 60, "流畅": 40, "标清": 50,
}


@dataclass
class Stream:
    url: str
    name: str = ""
    width: int = 0
    height: int = 0
    size: int = 0

    @property
    def score(self) -> tuple[int, int, int]:
        return (self.width * self.height, _DEF_RANK.get(self.name, 0), self.size)

    def label(self) -> str:
        parts = [self.name or "未知画质"]
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.size:
            parts.append(f"{self.size / 1024 / 1024:.0f}MB")
        return " · ".join(parts)


class HuyaError(RuntimeError):
    pass


def _fetch(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Referer": "https://v.huya.com/"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _walk(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _streams_from_json(obj: Any) -> list[Stream]:
    """在任意 JSON 结构里找出所有带 m3u8 的画质条目。"""
    out: list[Stream] = []
    for node in _walk(obj):
        url = node.get("m3u8") or node.get("url") or node.get("playUrl")
        if not isinstance(url, str) or ".m3u8" not in url:
            continue
        out.append(
            Stream(
                url=url.replace("\\/", "/"),
                name=str(node.get("defName") or node.get("definition") or ""),
                width=int(node.get("width") or 0),
                height=int(node.get("height") or 0),
                size=int(node.get("size") or 0),
            )
        )
    return out


def _streams_from_html(html: str) -> list[Stream]:
    """页面里没有干净 JSON 时，直接从 HTML 里正则捞 m3u8 地址。"""
    out: list[Stream] = []
    seen: set[str] = set()

    # 页面内嵌的 JSON 变量，例如 window.HNF_GLOBAL_INIT = {...};
    for blob in re.findall(r"=\s*(\{.{200,}?\})\s*;\s*(?:</script>|\n)", html, re.S):
        try:
            out.extend(_streams_from_json(json.loads(blob)))
        except ValueError:
            continue

    for raw in re.findall(r"https?:[\\/\w\.\-%]+?\.m3u8[^\"'\s\\]*", html):
        url = raw.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
        if url not in seen:
            seen.add(url)
            out.append(Stream(url=url))

    return out


def resolve(url: str) -> list[Stream]:
    """输入虎牙录像页地址，返回所有可用画质（已按清晰度从高到低排序）。"""
    if ".m3u8" in url:
        return [Stream(url=url, name="手动指定")]

    streams: list[Stream] = []
    video_id = None
    hit = re.search(r"/play/(\d+)", url) or re.search(r"[?&]vid=(\d+)", url)
    if hit:
        video_id = hit.group(1)

    if video_id:
        try:
            streams.extend(_streams_from_json(json.loads(_fetch(_MOMENT_API.format(video_id=video_id)))))
        except (urllib.error.URLError, ValueError, OSError):
            pass

    if not streams:
        try:
            html = _fetch(url)
        except (urllib.error.URLError, OSError) as err:
            raise HuyaError(f"打不开这个虎牙页面：{err}") from err
        streams.extend(_streams_from_html(html))
        if not video_id:
            hit = re.search(r'"videoId"\s*:\s*"?(\d+)', html)
            if hit:
                try:
                    streams.extend(
                        _streams_from_json(json.loads(_fetch(_MOMENT_API.format(video_id=hit.group(1)))))
                    )
                except (urllib.error.URLError, ValueError, OSError):
                    pass

    deduped: dict[str, Stream] = {}
    for stream in streams:
        existing = deduped.get(stream.url)
        if existing is None or stream.score > existing.score:
            deduped[stream.url] = stream

    if not deduped:
        raise HuyaError(
            "没能从这个页面里解析出 m3u8。\n"
            "退路（已经验证可用）：在 Chrome 里按 Option+Command+I 打开 DevTools，\n"
            "进 Network，Filter 里输入 m3u8，复制完整地址后执行：\n"
            '  python3 -m lolab huya --m3u8 "完整m3u8地址" --name 你想要的文件名'
        )

    return sorted(deduped.values(), key=lambda s: s.score, reverse=True)


_SAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]+')


def safe_name(text: str, fallback: str = "huya") -> str:
    cleaned = _SAFE.sub("_", text).strip().strip(".")
    return (cleaned or fallback)[:120]


def download(
    stream: Stream,
    out_path: Path,
    overwrite: bool = False,
    log_dir: Path | None = None,
) -> Path:
    """用 FFmpeg 把 m3u8 流拷成 MP4（-c copy，不转码，速度快且无损）。"""
    if shutil.which("ffmpeg") is None:
        raise HuyaError(
            "没有找到 ffmpeg。请在 Terminal 里执行：brew install ffmpeg"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and not overwrite:
        raise HuyaError(f"文件已存在（加 --overwrite 可覆盖）：{out_path}")

    # 先下到 .part，成功后再改名，避免半截文件被误认为下载完成
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    command = [
        "ffmpeg", "-y",
        "-loglevel", "warning",
        "-user_agent", _UA,
        "-headers", "Referer: https://v.huya.com/\r\n",
        "-i", stream.url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(tmp_path),
    ]

    log_path = None
    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (out_path.stem + ".ffmpeg.log")

    with (log_path.open("w", encoding="utf-8") if log_path else subprocess.DEVNULL) as sink:
        stderr = sink if log_path else subprocess.DEVNULL
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr, check=False)

    if result.returncode != 0 or not tmp_path.exists() or tmp_path.stat().st_size == 0:
        tmp_path.unlink(missing_ok=True)
        detail = f"，详细日志：{log_path}" if log_path else ""
        raise HuyaError(f"FFmpeg 下载失败（退出码 {result.returncode}）{detail}")

    tmp_path.replace(out_path)
    return out_path


def download_one(
    url: str,
    video_dir: Path,
    name: str | None = None,
    quality: str = "best",
    overwrite: bool = False,
) -> dict[str, Any]:
    """解析 + 下载一个录像，返回结果字典（供批量任务汇总）。"""
    started = time.time()
    streams = resolve(url)
    if quality == "best":
        chosen = streams[0]
    elif quality == "worst":
        chosen = streams[-1]
    else:
        chosen = next((s for s in streams if quality in s.name), streams[0])

    stem = safe_name(name or _default_name(url, chosen))
    out_path = video_dir / f"{stem}.mp4"
    download(chosen, out_path, overwrite=overwrite, log_dir=video_dir / "logs")

    record = {
        "sourceUrl": url,
        "quality": chosen.label(),
        "file": str(out_path),
        "bytes": out_path.stat().st_size,
        "seconds": round(time.time() - started, 1),
        "downloadedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _append_index(video_dir, record)
    return record


def _default_name(url: str, stream: Stream) -> str:
    hit = re.search(r"/play/(\d+)", url)
    base = f"huya_{hit.group(1)}" if hit else "huya_" + str(int(time.time()))
    stamp = time.strftime("%Y%m%d")
    quality = stream.name or (f"{stream.height}p" if stream.height else "src")
    return f"{stamp}_{base}_{quality}"


def _append_index(video_dir: Path, record: dict[str, Any]) -> None:
    """维护 data/videos/index.json，作为「录像库」的目录。"""
    index_path = video_dir / "index.json"
    records: list[dict[str, Any]] = []
    if index_path.is_file():
        try:
            with index_path.open("r", encoding="utf-8") as handle:
                records = json.load(handle)
        except (OSError, ValueError):
            records = []
    records.append(record)
    with index_path.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, ensure_ascii=False, indent=2)


def download_many(
    urls: list[str],
    video_dir: Path,
    workers: int = 3,
    quality: str = "best",
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    """批量并发下载。workers 就是「同时开几个下载」。"""
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(download_one, url, video_dir, None, quality, overwrite): url
            for url in urls
        }
        for future in as_completed(futures):
            url = futures[future]
            try:
                record = future.result()
                print(f"  ✅ 完成：{record['file']}  ({record['quality']})")
                results.append(record)
            except Exception as err:  # noqa: BLE001 - 单个失败不能拖垮整批
                print(f"  ❌ 失败：{url}\n     原因：{err}")
                results.append({"sourceUrl": url, "error": str(err)})
    return results
