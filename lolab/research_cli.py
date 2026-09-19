"""Research CLI; kept separate from legacy commands."""
import json
import time
from pathlib import Path
from . import discovery, paths, research
from .archive import Warehouse
from .transport import DeferredRequest


def run(args):
    wh = Warehouse(paths.find_data_dir(args.data))
    action = args.research_action
    if action == "import":
        result = research.import_manifest(wh, args.manifest)
    elif action == "migrate":
        result = research.migrate_legacy(wh, paths.find_data_dir(args.data))
    elif action == "rebuild":
        result = {"rebuilt": [g["gameId"] for g in research.rebuild(wh, args.game)]}
    elif action == "audit":
        attestation = json.loads(Path(args.attestation).read_text()) if args.attestation else {}
        result = research.validate_lpl(wh, attestation)
    elif action == "stats":
        result = research.analytics(wh)
    elif action == "discover":
        config = json.loads(Path(args.config).read_text())
        if args.watch and args.watch < 60:
            raise ValueError("重复发现间隔至少为 60 秒")
        while True:
            try:
                result = discovery.discover(wh, config, args.pages)
            except DeferredRequest as err:
                if not args.watch:
                    raise
                result = {"deferred": str(err)}
            if not args.watch:
                break
            print(json.dumps(result, ensure_ascii=False), flush=True)
            time.sleep(args.watch)
    elif action == "features":
        games = [g for g in wh.games() if g["quality"]["ok"] and not g["context"].get("synthetic")]
        if not games:
            raise ValueError("没有通过质量检查的真实比赛，不能导出训练样本")
        splits = research.temporal_split(games, args.cutoff)
        rows = []
        for g in games:
            feature = research.features_at(g, args.minute * 60000)
            if feature:
                rows.append({**feature, "label": g["labels"]["blueWin"],
                             "split": "train" if g["gameId"] in splits["train"] else "test",
                             "revision": g["revision"]})
        result = {"featureVersion": "features-v1", "cutoff": args.cutoff, "minute": args.minute,
                  "splits": splits, "rows": rows, "models": []}
    else:
        result = wh.overview()
    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "blocked" or result.get("errors") else 0


def register(sub):
    parser = sub.add_parser("research", help="研究仓库：导入、离线重建、LPL 验证及统计")
    actions = parser.add_subparsers(dest="research_action", required=True)
    for name, help_text in (("import", "从清单导入真实数据"), ("migrate", "导入现有普通对局"),
                            ("rebuild", "仅用存档离线重建"), ("audit", "检查十局 LPL 验收关卡"),
                            ("stats", "描述统计"), ("discover", "发现 LPL 赛程，需独立来源凭证"),
                            ("features", "按整场/系列赛和日期划分特征"), ("status", "仓库状态")):
        p = actions.add_parser(name, help=help_text)
        p.add_argument("--out", help="将结果保存为 JSON")
        p.set_defaults(func=run)
        if name == "import":
            p.add_argument("manifest")
        if name == "rebuild":
            p.add_argument("--game")
        if name == "audit":
            p.add_argument("--attestation", help="具名人工来源/权限核验 JSON")
        if name == "discover":
            p.add_argument("config")
            p.add_argument("--pages", type=int, default=20)
            p.add_argument("--watch", type=int, default=0, help="每隔指定秒数发现，0 为单次")
        if name == "features":
            p.add_argument("--cutoff", required=True, help="含时区的测试集起始时间")
            p.add_argument("--minute", type=int, default=15)
