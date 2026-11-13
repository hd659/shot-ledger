"""Auditable per-shot manifests, content hashes, GPU ledger, target-lock preflight and gap card."""
import hashlib
import json
import re
from pathlib import Path

from .core import (FPS, estimate_gpu_sec, fmt_dur, frames_to_sec, lint_shot, load_doc, split_script)

WORKFLOW_KEYS = ("steps", "res_mode", "negative", "lora", "cfg", "sampler", "seed")


def canon_hash(obj):
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def file_sha(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def project_slug(doc, src):
    return str(doc.get("project") or doc.get("slug") or Path(src).resolve().parent.name)


def _refs(shot, doc, base):
    refs = list(shot.get("refs") or [])
    cfg = doc.get("director_config") or {}
    if shot.get("ref_source") == "face_lock":
        refs += [r["image"] for r in cfg.get("character_refs", []) if r.get("image")]
    return [{"path": r, "sha256": file_sha(base / r)} for r in refs]


def build_plan(src, gpu=None):
    """Load a script (scenes/beats) or storyboard (shots) and return the full audited plan."""
    src = Path(src)
    doc = load_doc(src)
    base = src.resolve().parent
    shots = split_script(doc) if "scenes" in doc else list(doc.get("shots", []))
    engine = doc.get("engine") or {}
    fps = int(doc.get("fps", FPS))
    gpu = {**(doc.get("gpu") or {}), **(gpu or {})}
    project = project_slug(doc, src)
    manifests, errors, warns = [], [], []
    for shot in shots:
        snapped, e, w = lint_shot(shot, doc.get("rules"), fps)
        errors += e
        warns += w
        frames = snapped or int(shot.get("frames") or 0)
        workflow = {k: shot.get(k, engine.get(k)) for k in WORKFLOW_KEYS if shot.get(k, engine.get(k)) not in (None, "")}
        m = {
            "project": project, "shot": shot["id"], "shot_mode": shot.get("shot_mode", "keyframe"),
            "frames": frames, "seconds": frames_to_sec(frames, fps), "fps": fps,
            "model": shot.get("base_model") or engine.get("base_model", ""),
            "workflow": workflow,
            "prompts": {k: shot.get(k, "") for k in ("keyframe_prompt", "visual_prompt", "sound_prompt")},
            "vo": shot.get("vo"),
            "refs": _refs(shot, doc, base),
            "gpu_sec": estimate_gpu_sec(frames, shot.get("shot_mode"), gpu),
        }
        m["content_hash"] = canon_hash({k: v for k, v in m.items() if k != "gpu_sec"})
        manifests.append(m)
    total_gpu = sum(m["gpu_sec"] for m in manifests)
    rate = float(gpu.get("usd_per_hour", 0) or 0)
    ledger = {
        "project": project, "source": src.name, "shots": len(manifests),
        "runtime_sec": round(sum(m["seconds"] for m in manifests), 2),
        "gpu_sec": round(total_gpu, 1), "gpu_human": fmt_dur(total_gpu),
        "usd_estimate": round(total_gpu / 3600 * rate, 2) if rate else None,
        "plan_hash": canon_hash([[m["shot"], m["content_hash"]] for m in manifests]),
        "errors": errors, "warnings": warns,
    }
    return {"ledger": ledger, "manifests": manifests}


def write_plan(plan, out_dir):
    """Write manifest/<shot>.json, ledger.json and target.lock.json under out_dir/<project>/."""
    led = plan["ledger"]
    root = Path(out_dir) / led["project"]
    (root / "manifest").mkdir(parents=True, exist_ok=True)
    for m in plan["manifests"]:
        (root / "manifest" / f"{m['shot']}.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")
    (root / "ledger.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), "utf-8")
    lock = {"project": led["project"], "plan_hash": led["plan_hash"],
            "shots": {m["shot"]: m["content_hash"] for m in plan["manifests"]}}
    (root / "target.lock.json").write_text(json.dumps(lock, ensure_ascii=False, indent=2), "utf-8")
    return root


def render_table(plan):
    rows = ["| 镜头 | 模式 | 帧数 | 秒 | 预估 GPU | 哈希 |", "|---|---|---|---|---|---|"]
    for m in plan["manifests"]:
        rows.append(f"| {m['shot']} | {m['shot_mode']} | {m['frames']} | {m['seconds']} | "
                    f"{fmt_dur(m['gpu_sec'])} | {m['content_hash'][:10]} |")
    led = plan["ledger"]
    cost = f"，约 ${led['usd_estimate']}" if led["usd_estimate"] is not None else ""
    rows.append(f"\n项目 {led['project']}：{led['shots']} 镜，成片 {led['runtime_sec']}s，"
                f"预估 GPU {led['gpu_human']}{cost}，plan_hash {led['plan_hash'][:12]}")
    rows += [f"[ERROR] {e}" for e in led["errors"]] + [f"[warn] {w}" for w in led["warnings"]]
    return "\n".join(rows)


def preflight(lock, job):
    """Compare a job ({project, shots:{id:hash}} or {project, plan_hash}) with the lock. Returns list of problems."""
    problems = []
    if job.get("project") != lock["project"]:
        problems.append(f"项目不一致：锁定的是 {lock['project']}，待跑的是 {job.get('project')}")
        return problems
    if job.get("plan_hash") and job["plan_hash"] != lock["plan_hash"]:
        problems.append("plan_hash 不一致：分镜在锁定之后被改过，先重新 plan")
    for sid, h in (job.get("shots") or {}).items():
        if sid not in lock["shots"]:
            problems.append(f"{sid}: 不在锁定清单里")
        elif h != lock["shots"][sid]:
            problems.append(f"{sid}: 内容哈希变了（锁定 {lock['shots'][sid][:10]}，现在 {h[:10]}）")
    return problems


def _words(text):
    return set(re.findall(r"[a-z]+|[\u4e00-\u9fff]", (text or "").lower()))


def gap_card(mine, ref):
    """Diff two samples along prompt / refs / model / workflow, so you know which layer to fix first."""
    a, b = _words(mine.get("prompt")), _words(ref.get("prompt"))
    sim = round(len(a & b) / len(a | b), 2) if a | b else 1.0
    wa, wb = mine.get("workflow") or {}, ref.get("workflow") or {}
    wf_diff = {k: [wa.get(k), wb.get(k)] for k in sorted(set(wa) | set(wb)) if wa.get(k) != wb.get(k)}
    ra, rb = set(mine.get("refs") or []), set(ref.get("refs") or [])
    return {
        "prompt": {"same": a == b, "similarity": sim,
                   "only_ref": sorted(b - a)[:20], "only_mine": sorted(a - b)[:20]},
        "refs": {"same": ra == rb, "count": [len(ra), len(rb)], "only_ref": sorted(rb - ra)},
        "model": {"same": mine.get("model") == ref.get("model"), "values": [mine.get("model"), ref.get("model")]},
        "workflow": {"same": not wf_diff, "diff": wf_diff},
    }


def render_gap(card):
    lines = ["| 维度 | 一致 | 差异 |", "|---|---|---|"]
    p = card["prompt"]
    lines.append(f"| 提示词 | {'是' if p['same'] else '否'} | 相似度 {p['similarity']}；参考独有：{' '.join(p['only_ref'][:8]) or '-'} |")
    r = card["refs"]
    lines.append(f"| 参考图 | {'是' if r['same'] else '否'} | 我 {r['count'][0]} 张 / 参考 {r['count'][1]} 张 |")
    m = card["model"]
    lines.append(f"| 模型 | {'是' if m['same'] else '否'} | {m['values'][0]} vs {m['values'][1]} |")
    w = card["workflow"]
    diff = "；".join(f"{k}: {v[0]} vs {v[1]}" for k, v in w["diff"].items()) or "-"
    lines.append(f"| 工作流 | {'是' if w['same'] else '否'} | {diff} |")
    return "\n".join(lines)
