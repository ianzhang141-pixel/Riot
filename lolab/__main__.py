"""命令行入口：python3 -m lolab <子命令>

设计上只依赖 Mac 自带的 python3，不需要 pip 安装任何东西。
所有子命令都可以加 --data 指定项目的 data 目录，不加就自动往上找。

    python3 -m lolab where                      看看它认的是哪个 data 目录
    python3 -m lolab check KR_8368663855        检查这场的 Match / Timeline 是否完整
    python3 -m lolab fetch KR_8368663855        从 Riot API 补下载 Match + Timeline
    python3 -m lolab state KR_8368663855        把 Timeline 转成分钟级 State
    python3 -m lolab serve                      打开 Timeline / State 调试页面
    python3 -m lolab items                      更新装备价格表（算装备价值用）
    python3 -m lolab huya <录像地址>             下载虎牙录像
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import check, ddragon, huya, paths, riot, state, store


# ---------------------------------------------------------------- 子命令实现

def cmd_where(args: argparse.Namespace) -> int:
    data_dir = paths.find_data_dir(args.data)
    print(f"data 目录：{data_dir}")
    print(f"存在：{'是' if data_dir.is_dir() else '否（还没创建）'}")
    if data_dir.is_dir():
        ids = store.list_match_ids(data_dir)
        print(f"扫描到 {len(ids)} 场比赛。")
        for match_id in ids[:20]:
            print(f"  · {match_id}")
        if len(ids) > 20:
            print(f"  …… 还有 {len(ids) - 20} 场")
    else:
        print("如果这不是你项目的 data 目录，请用 --data 指定，例如：")
        print("  python3 -m lolab where --data ~/你的项目/data")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    data_dir = paths.find_data_dir(args.data)
    report = check.run(data_dir, args.match_id)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(check.format_text(report))
    return 0 if report["verdict"]["ok"] else 1


def cmd_fetch(args: argparse.Namespace) -> int:
    data_dir = paths.ensure(paths.find_data_dir(args.data))
    region = args.region or riot.guess_region(args.match_id)
    try:
        api_key = riot.load_api_key(data_dir)
    except riot.RiotError as err:
        print(f"❌ {err}")
        return 2

    print(f"大区路由：{region}    比赛：{args.match_id}")
    for kind, fetch in (("match", riot.fetch_match), ("timeline", riot.fetch_timeline)):
        existing = store.locate(data_dir, args.match_id)[kind]
        if existing and not args.overwrite:
            print(f"  ⏭  已存在 {kind}，跳过（加 --overwrite 可重新下载）：{existing[0].source}")
            continue
        try:
            obj = fetch(region, args.match_id, api_key)
        except riot.RiotError as err:
            print(f"  ❌ 下载 {kind} 失败：{err}")
            return 2
        path = store.save(data_dir, kind, args.match_id, obj)
        print(f"  ✅ {kind} 已保存：{path}")

    print()
    print(check.format_text(check.run(data_dir, args.match_id)))
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    data_dir = paths.find_data_dir(args.data)
    found = store.locate(data_dir, args.match_id)
    if not found["timeline"] or not found["match"]:
        print(f"❌ 本机没有找到 {args.match_id} 的 Match 或 Timeline。")
        print(f"   先执行：python3 -m lolab fetch {args.match_id}")
        return 2

    built = state.build(
        found["match"][0].data, found["timeline"][0].data, ddragon.load(data_dir)
    )

    if args.out:
        out_path = Path(args.out).expanduser()
    else:
        out_path = data_dir / "states" / f"{args.match_id}.state.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(built, handle, ensure_ascii=False)
    print(f"✅ 已生成 {built['meta']['frameCount']} 帧 State：{out_path}")

    if args.minute is not None:
        _print_frame(built, args.minute)
    return 0


def _print_frame(built: dict, minute: int) -> None:
    frames = built["frames"]
    frame = min(frames, key=lambda f: abs(f["minute"] - minute))
    print()
    print("=" * 78)
    print(f"{frame['clock']}   （第 {frame['frameIndex']} 帧）")
    print("=" * 78)
    blue, red = frame["teams"]["100"], frame["teams"]["200"]
    print(
        f"蓝方  经济 {blue['totalGold']:>6}  击杀 {blue['kills']:>2}  塔 {blue['towers']}  "
        f"龙 {blue['dragons']}  先锋 {blue['heralds']}  大龙 {blue['barons']}"
    )
    print(
        f"红方  经济 {red['totalGold']:>6}  击杀 {red['kills']:>2}  塔 {red['towers']}  "
        f"龙 {red['dragons']}  先锋 {red['heralds']}  大龙 {red['barons']}"
    )
    print(f"经济差（蓝-红）：{frame['diff']['totalGold']:+}")
    print("-" * 78)
    header = f"{'':<3}{'英雄':<14}{'位置':<8}{'Lv':>3}{'总金币':>8}{'现金':>7}{'经验':>7}{'CS':>5}{'野怪':>5}   坐标"
    print(header)
    for player in frame["players"]:
        side = "蓝" if player["teamId"] == 100 else "红"
        print(
            f"{side:<3}{(player['championName'] or '?'):<14}{(player['role'] or '?'):<8}"
            f"{player['level']:>3}{player['totalGold']:>8}{player['currentGold']:>7}"
            f"{player['xp']:>7}{player['laneCs']:>5}{player['jungleCs']:>5}   "
            f"({player['x']}, {player['y']})"
        )
    print("-" * 78)
    print(f"这一分钟内发生的事件（{len(frame['recentEvents'])} 条）：")
    for event in frame["recentEvents"][:30]:
        print(f"  {event['clock']}  {event['type']}")


def cmd_doctor(args: argparse.Namespace) -> int:
    data_dir = paths.find_data_dir(args.data)
    report = riot.diagnose(data_dir)
    if report.get("error"):
        print(f"❌ {report['error']}")
        return 2
    print(f"Key：{report['masked']}（{report['length']} 个字符，来自 {report['source']}）")
    print(f"格式：{report['shapeMessage']}")
    for key in ("header", "query"):
        item = report[key]
        state = f"HTTP {item['status']} 通过" if item["ok"] else (
            f"HTTP {item['status']}" if item["status"] else item.get("error", "失败")
        )
        print(f"{item['method']}：{state}")
    print()
    print(report["verdict"])
    return 0 if (report["header"]["ok"] or report["query"]["ok"]) else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server

    data_dir = paths.find_data_dir(args.data)
    server.serve(data_dir, port=args.port)
    return 0


def cmd_items(args: argparse.Namespace) -> int:
    data_dir = paths.ensure(paths.find_data_dir(args.data))
    version, count = ddragon.update(data_dir)
    print(f"✅ 已缓存 {count} 件装备的价格（版本 {version}）：{ddragon.cache_path(data_dir)}")
    return 0


def cmd_huya(args: argparse.Namespace) -> int:
    data_dir = paths.ensure(paths.find_data_dir(args.data))
    video_dir = paths.ensure(data_dir / "videos")

    urls = list(args.urls)
    if args.m3u8:
        urls.append(args.m3u8)
    if args.file:
        text = Path(args.file).expanduser().read_text(encoding="utf-8")
        urls.extend(line.strip() for line in text.splitlines() if line.strip())
    if not urls:
        print("❌ 至少要给一个虎牙录像地址。例如：")
        print('   python3 -m lolab huya "https://v.huya.com/play/123456.html"')
        return 2

    if args.list:
        for url in urls:
            print(f"\n{url}")
            try:
                for stream in huya.resolve(url):
                    print(f"  · {stream.label()}")
            except huya.HuyaError as err:
                print(f"  ❌ {err}")
        return 0

    print(f"录像库：{video_dir}")
    if len(urls) == 1:
        try:
            record = huya.download_one(
                urls[0], video_dir, args.name, args.quality, args.overwrite
            )
        except huya.HuyaError as err:
            print(f"❌ {err}")
            return 2
        print(f"✅ 完成：{record['file']}  ({record['quality']}, {record['seconds']}秒)")
        return 0

    print(f"共 {len(urls)} 个任务，同时下载 {args.workers} 个。")
    results = huya.download_many(urls, video_dir, args.workers, args.quality, args.overwrite)
    failed = [r for r in results if r.get("error")]
    print(f"\n完成 {len(results) - len(failed)} / {len(results)}")
    return 1 if failed else 0


# ---------------------------------------------------------------- 参数解析

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m lolab",
        description="LOL PC 决策实验室 —— 数据验证与状态抽取工具箱",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--data", help="项目的 data 目录（不写就自动往上找）")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("where", help="显示它认的是哪个 data 目录，以及找到了哪些比赛")
    p.set_defaults(func=cmd_where)

    p = sub.add_parser("check", help="检查某场比赛的 Match / Timeline 是否完整")
    p.add_argument("match_id", help="例如 KR_8368663855")
    p.add_argument("--json", action="store_true", help="输出 JSON 而不是中文报告")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("fetch", help="从 Riot API 下载 Match + Timeline")
    p.add_argument("match_id")
    p.add_argument("--region", choices=riot.REGIONAL_ROUTES, help="不写就按 matchId 前缀猜")
    p.add_argument("--overwrite", action="store_true", help="已存在也重新下载")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("state", help="Timeline → 分钟级 State JSON")
    p.add_argument("match_id")
    p.add_argument("--minute", type=int, help="顺便在 Terminal 里打印这一分钟的完整局势")
    p.add_argument("--out", help="输出文件路径（默认 data/states/<matchId>.state.json）")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("doctor", help="自检：分清「Key 无效」和「请求被拦截」两种 403")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="打开 Timeline / State 调试页面")
    p.add_argument("--port", type=int, default=8010, help="默认 8010，不占用现有的 8000")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("items", help="更新 Data Dragon 装备价格表")
    p.set_defaults(func=cmd_items)

    p = sub.add_parser("huya", help="下载虎牙录像（URL → m3u8 → 最高画质 → MP4）")
    p.add_argument("urls", nargs="*", help="虎牙录像页地址，可以写多个")
    p.add_argument("--m3u8", help="退路：直接给从 DevTools 复制的 m3u8 地址")
    p.add_argument("--name", help="保存的文件名（不用写 .mp4）")
    p.add_argument("--file", help="从一个文本文件里读地址，一行一个")
    p.add_argument("--quality", default="best", help="best（默认）/ worst / 或画质名如 原画")
    p.add_argument("--workers", type=int, default=3, help="同时下载几个，默认 3")
    p.add_argument("--overwrite", action="store_true", help="文件已存在也覆盖")
    p.add_argument("--list", action="store_true", help="只列出可用画质，不下载")
    p.set_defaults(func=cmd_huya)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
