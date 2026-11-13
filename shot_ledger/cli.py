"""shot-ledger CLI: lint / plan / preflight / gap.  python3 -m shot_ledger <cmd> -h"""
import argparse
import json
import shlex
import sys
from pathlib import Path

from .core import load_doc
from .manifest import build_plan, gap_card, preflight, render_gap, render_table, write_plan


def _gpu_overrides(args):
    g = {}
    if args.sec_per_frame is not None:
        g["sec_per_frame"] = args.sec_per_frame
    if args.usd_per_hour is not None:
        g["usd_per_hour"] = args.usd_per_hour
    return g


def cmd_lint(args):
    plan = build_plan(args.source, _gpu_overrides(args))
    print(render_table(plan))
    return 1 if plan["ledger"]["errors"] else 0


def cmd_plan(args):
    plan = build_plan(args.source, _gpu_overrides(args))
    print(render_table(plan))
    if plan["ledger"]["errors"] and not args.force:
        print("\n有 ERROR，未写出清单（确认要写可加 --force）", file=sys.stderr)
        return 1
    root = write_plan(plan, args.out)
    print(f"\n已写出：{root}/manifest/*.json、ledger.json、target.lock.json")
    return 0


def _project_from_cmd(cmd):
    argv = shlex.split(cmd)
    for i, tok in enumerate(argv):
        if tok == "--drama" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--drama="):
            return tok.split("=", 1)[1]
    return None


def cmd_preflight(args):
    lock = json.loads(Path(args.lock).read_text("utf-8"))
    if args.job:
        job = json.loads(Path(args.job).read_text("utf-8"))
    elif args.source:
        plan = build_plan(args.source)
        job = {"project": plan["ledger"]["project"], "plan_hash": plan["ledger"]["plan_hash"],
               "shots": {m["shot"]: m["content_hash"] for m in plan["manifests"]}}
        if args.shots:
            keep = set(args.shots.split(","))
            job["shots"] = {k: v for k, v in job["shots"].items() if k in keep}
            job.pop("plan_hash")
    elif args.project or args.cmd:
        job = {"project": args.project or _project_from_cmd(args.cmd)}
    else:
        print("preflight 需要 --source / --job / --project / --cmd 之一", file=sys.stderr)
        return 2
    problems = preflight(lock, job)
    if args.cmd:
        cmd_proj = _project_from_cmd(args.cmd)
        if cmd_proj is None:
            problems.append("渲染命令里没有 --drama <slug>，无法确认项目")
        elif cmd_proj != lock["project"] and not any(cmd_proj in p for p in problems):
            problems.append(f"渲染命令的项目是 {cmd_proj}，锁定的是 {lock['project']}")
    if problems:
        print("PREFLIGHT 拒绝：")
        for p in problems:
            print(f"  - {p}")
        return 2
    n = len(job.get("shots") or lock["shots"])
    print(f"PREFLIGHT 通过：项目 {lock['project']}，{n} 镜，plan_hash {lock['plan_hash'][:12]}")
    return 0


def cmd_gap(args):
    card = gap_card(load_doc(args.mine), load_doc(args.ref))
    print(json.dumps(card, ensure_ascii=False, indent=2) if args.json else render_gap(card))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="shot-ledger", description="CPU 端分镜账本：拆镜、帧数吸附、GPU 时长、审计清单、开跑前核对项目")
    sub = ap.add_subparsers(dest="cmd_name", required=True)
    for name, fn, hlp in (("lint", cmd_lint, "只检查并打印分镜表"), ("plan", cmd_plan, "检查并写出审计清单和项目锁")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("source", help="剧本 (scenes/beats) 或 storyboard.json，支持 .yaml/.json")
        p.add_argument("--sec-per-frame", type=float, help="每帧 GPU 秒数（按你的卡实测改）")
        p.add_argument("--usd-per-hour", type=float, help="GPU 每小时价格，用于估算花费")
        if name == "plan":
            p.add_argument("--out", default="out", help="输出目录，默认 ./out")
            p.add_argument("--force", action="store_true", help="有 ERROR 也写出")
        p.set_defaults(func=fn)
    p = sub.add_parser("preflight", help="开跑前核对项目和内容哈希，不一致返回退出码 2")
    p.add_argument("--lock", required=True, help="plan 生成的 target.lock.json")
    p.add_argument("--source", help="即将提交渲染的 storyboard/剧本（会重新计算哈希）")
    p.add_argument("--shots", help="只核对这些镜头，逗号分隔，如 S02a,S02b")
    p.add_argument("--job", help="待提交/正在跑的任务 JSON：{project, shots:{id:hash}}")
    p.add_argument("--project", help="只核对项目名")
    p.add_argument("--cmd", help="将要执行的渲染命令，从 --drama 里读出项目名核对")
    p.set_defaults(func=cmd_preflight)
    p = sub.add_parser("gap", help="我的样本 vs 参考样本：提示词/参考图/模型/工作流 四项对比")
    p.add_argument("mine")
    p.add_argument("ref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_gap)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
