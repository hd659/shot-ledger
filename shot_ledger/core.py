"""CPU-side storyboard rules: frame buckets, one-action/one-camera lint, beat splitting, GPU time estimate.

Detection regexes and thresholds mirror drama_engine/run_drama.py (H3 accepts 17n+5 frames at 24 fps).
"""
import json
import math
import re
from pathlib import Path

FPS = 24
FRAME_BUCKETS = (124, 141, 158, 175, 192, 209)  # 17n+5, 5.2s .. 8.7s
VO_OFFSET_SEC = 0.6
VO_TAIL_SEC = 0.6
CHARS_PER_SEC = 4.0
DEFAULT_RULES = {"max_actions": 1, "max_camera_moves": 1, "max_sequence": 1, "max_continue_chain": 3}
DEFAULT_GPU = {"sec_per_frame": 1.5, "shot_overhead_sec": 40.0, "keyframe_sec": 25.0, "usd_per_hour": 0.0}

_ACTION_VERBS = (r"push|open|step|lean|slam|shut|turn|pin|kiss|slide|walk|sit|stand|lie|lay|pull|grab|grip|lift|raise|"
                 r"lower|kneel|climb|crawl|run|rush|enter|exit|leave|reach|unzip|unbutton|remove|strip|drop|throw|press|"
                 r"wrap|straddle|ride|spread|bend|arch|roll|hug|embrace|undress|tug|rip|tear|spin|dance|carry|close|lock|knock")
_ACTION_RE = re.compile(rf"\b({_ACTION_VERBS})(?:es|s)\b", re.I)
_ACTION_ZH_RE = re.compile(r"推开|走进|走向|走出|坐下|站起|起身|躺下|跪下|转身|关门|锁门|抱起|抬起|拉起|扯下|脱下|脱掉|解开|按住|压住|抵住|靠在|亲吻|扑倒|翻身|爬上|跨坐")
_SEQUENCE_RE = re.compile(r"\bthen\b|\bafter that\b|\bafterwards\b|\bnext\b|\bfinally\b|然后|接着|随后|之后|最后|紧接着", re.I)
_CAMERA_RE = re.compile(r"push(?:es|ing)? in|pull(?:s|ing)? (?:back|out)|dolly|tilt|\bpan(?:s|ning)?\b|orbit|zoom|"
                        r"\btrack(?:s|ing)?\b|crane|rack focus|推近|拉远|摇镜|俯拍转|环绕|跟拍|变焦", re.I)
_CAMERA_SPLIT_RE = re.compile(r"Camera\s*[:：]", re.I)
_SPEECH_CHAR_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def load_doc(path):
    """Load YAML (if PyYAML is installed) or JSON."""
    text = Path(path).read_text(encoding="utf-8")
    if str(path).endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on env
            raise SystemExit("PyYAML 未安装：pip install pyyaml，或改用 .json") from exc
        return yaml.safe_load(text) or {}
    return json.loads(text)


def snap_frames(frames):
    """Round up to the nearest allowed bucket; None means it cannot fit in one shot."""
    n = int(frames)
    for b in FRAME_BUCKETS:
        if n <= b:
            return b
    return None


def frames_to_sec(frames, fps=FPS):
    return round(frames / fps, 2)


def vo_need_sec(line, offset=VO_OFFSET_SEC):
    if not line:
        return 0.0
    chars = len(_SPEECH_CHAR_RE.findall(line))
    return round(offset + chars / CHARS_PER_SEC + VO_TAIL_SEC, 2)


def frames_for_seconds(sec, fps=FPS):
    return math.ceil(sec * fps - 1e-9)


def busyness(prompt, camera=None):
    """Count gross actions, then/然后 chaining and distinct camera moves (same heuristic as run_drama lint)."""
    prompt = prompt or ""
    parts = _CAMERA_SPLIT_RE.split(prompt, maxsplit=1)
    body = parts[0]
    cam_text = camera if camera is not None else (parts[1] if len(parts) > 1 else prompt)
    actions = {m.group(1).lower() for m in _ACTION_RE.finditer(body)} | set(_ACTION_ZH_RE.findall(body))
    cams = {m.group(0).lower()[:4] for m in _CAMERA_RE.finditer(cam_text or "")}
    return {"actions": sorted(actions), "sequence": len(_SEQUENCE_RE.findall(body)), "camera_moves": len(cams)}


def lint_shot(shot, rules=None, fps=FPS):
    """Return (snapped_frames, errors, warnings) for one shot dict (storyboard.json shape)."""
    rules = {**DEFAULT_RULES, **(rules or {})}
    errors, warns = [], []
    sid = shot.get("id", "?")
    vo = shot.get("vo") or {}
    need = vo_need_sec(vo.get("line"), float(vo.get("offset_sec", VO_OFFSET_SEC)))
    want = max(int(shot.get("frames") or FRAME_BUCKETS[0]), frames_for_seconds(need, fps))
    snapped = snap_frames(want)
    if snapped is None:
        errors.append(f"{sid}: 需要 {want} 帧（{frames_to_sec(want, fps)}s）超过上限 {FRAME_BUCKETS[-1]}，拆镜或缩短台词")
    elif shot.get("frames") and int(shot["frames"]) != snapped:
        warns.append(f"{sid}: 帧数 {shot['frames']} 不在档位里，吸附为 {snapped}")
    # script-mode shots carry the raw beat action; static state/setting text must not count as actions
    b = busyness(shot.get("action") or shot.get("visual_prompt"), shot.get("camera"))
    busy = []
    if len(b["actions"]) > rules["max_actions"]:
        busy.append(f"{len(b['actions'])} 个动作（{', '.join(b['actions'])}）")
    if b["sequence"] > rules["max_sequence"]:
        busy.append(f"{b['sequence']} 处 then/然后 串联")
    if b["camera_moves"] > rules["max_camera_moves"]:
        busy.append(f"{b['camera_moves']} 种运镜")
    if busy:
        warns.append(f"{sid}: 一镜内容过多：{'；'.join(busy)}，建议拆镜")
    return snapped, errors, warns


def _visual_prompt(style, setting, state, action, camera, continuing):
    lead = "Continuing seamlessly from the previous frame, " if continuing else ""
    parts = [style, setting, state, lead + action, f"Camera: {camera}" if camera else "",
             "realistic skin texture, shallow depth of field"]
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip()) + "."


def split_script(script):
    """Expand scenes -> one shot per beat (one action, one camera). Returns list of storyboard-shaped shots."""
    style = script.get("style", "East Asian photorealistic cinematic style")
    max_chain = {**DEFAULT_RULES, **(script.get("rules") or {})}["max_continue_chain"]
    shots, chain = [], 0
    for scene in script.get("scenes", []):
        beats = scene.get("beats") or []
        for i, beat in enumerate(beats):
            sid = scene["id"] if len(beats) == 1 else f"{scene['id']}{chr(ord('a') + i)}"
            mode = beat.get("shot_mode") or ("keyframe" if i == 0 else "continue")
            if mode == "continue" and chain >= max_chain:
                mode = "keyframe"
            chain = chain + 1 if mode == "continue" else 0
            vo = {"speaker": beat.get("speaker", ""), "line": beat["line"],
                  "offset_sec": beat.get("offset_sec", VO_OFFSET_SEC)} if beat.get("line") else None
            shots.append({
                "id": sid, "scene": scene["id"], "shot_mode": mode,
                "frames": beat.get("frames") or FRAME_BUCKETS[0],
                "keyframe_prompt": (beat.get("keyframe_prompt") or scene.get("keyframe_prompt", "")) if mode == "keyframe" else "",
                "visual_prompt": _visual_prompt(style, scene.get("setting", ""), beat.get("state", scene.get("state", "")),
                                                beat["action"], beat.get("camera", ""), mode == "continue"),
                "action": beat["action"], "camera": beat.get("camera", ""),
                "sound_prompt": beat.get("sound", scene.get("sound", "")),
                "refs": beat.get("refs", scene.get("refs", [])),
                "vo": vo,
            })
    return shots


def estimate_gpu_sec(frames, shot_mode, gpu=None):
    g = {**DEFAULT_GPU, **(gpu or {})}
    sec = g["shot_overhead_sec"] + frames * g["sec_per_frame"]
    if shot_mode == "keyframe":
        sec += g["keyframe_sec"]
    return round(sec, 1)


def fmt_dur(sec):
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"
