import json
from pathlib import Path

import pytest

from shot_ledger import FRAME_BUCKETS, busyness, lint_shot, snap_frames, split_script
from shot_ledger.cli import main
from shot_ledger.core import estimate_gpu_sec, vo_need_sec
from shot_ledger.manifest import build_plan, gap_card, preflight, write_plan

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo_project.yaml"


def test_buckets_are_17n_plus_5():
    assert all((b - 5) % 17 == 0 for b in FRAME_BUCKETS)
    assert FRAME_BUCKETS[0] == 124 and FRAME_BUCKETS[-1] == 209


@pytest.mark.parametrize("raw,want", [(1, 124), (124, 124), (125, 141), (193, 209), (209, 209), (210, None)])
def test_snap_frames(raw, want):
    assert snap_frames(raw) == want


def test_busyness_counts_actions_and_camera():
    b = busyness("He pushes open the door, then walks in and locks it. Camera: slow push in, then tilt down")
    assert {"push", "walk", "lock"} <= set(b["actions"])
    assert b["sequence"] == 1
    assert b["camera_moves"] == 2


def test_expressions_are_not_actions():
    assert busyness("She smiles, breathes and glances at him. Camera: static")["actions"] == []


def test_lint_flags_multi_action_and_long_dialogue():
    _, errs, warns = lint_shot({"id": "X", "frames": 124,
                                "visual_prompt": "He pushes the door and walks in. Camera: pan and zoom"})
    assert not errs and "一镜内容过多" in warns[0]
    _, errs, _ = lint_shot({"id": "Y", "frames": 124, "visual_prompt": "",
                            "vo": {"line": "这" * 40}})
    assert errs and "超过上限" in errs[0]


def test_dialogue_stretches_frames():
    need = vo_need_sec("你终于来了，我等了很久。")  # 10 chars -> 0.6 + 2.5 + 0.6
    assert need == pytest.approx(3.7)
    snapped, _, _ = lint_shot({"id": "Z", "frames": 124, "vo": {"line": "这" * 24}})  # 7.2s -> 173 -> 175
    assert snapped == 175


def test_split_script_one_shot_per_beat_and_continue_chain_cap():
    script = {"rules": {"max_continue_chain": 2}, "scenes": [
        {"id": "S01", "setting": "bar", "beats": [{"action": f"step {i}"} for i in range(4)]}]}
    shots = split_script(script)
    assert [s["id"] for s in shots] == ["S01a", "S01b", "S01c", "S01d"]
    assert [s["shot_mode"] for s in shots] == ["keyframe", "continue", "continue", "keyframe"]
    assert shots[1]["visual_prompt"].count("Continuing seamlessly") == 1


def test_gpu_estimate_keyframe_costs_more():
    assert estimate_gpu_sec(124, "keyframe") > estimate_gpu_sec(124, "continue")


def test_plan_hash_is_stable_and_content_sensitive(tmp_path):
    p1 = build_plan(DEMO)
    assert p1["ledger"]["plan_hash"] == build_plan(DEMO)["ledger"]["plan_hash"]
    assert p1["ledger"]["project"] == "demo-rooftop"
    assert all(m["frames"] in FRAME_BUCKETS for m in p1["manifests"])
    edited = tmp_path / "demo.yaml"
    edited.write_text(DEMO.read_text("utf-8").replace("close-up on their hands", "slow zoom on hands"), "utf-8")
    p2 = build_plan(edited)
    assert p2["ledger"]["plan_hash"] != p1["ledger"]["plan_hash"]
    changed = [a["shot"] for a, b in zip(p1["manifests"], p2["manifests"]) if a["content_hash"] != b["content_hash"]]
    assert changed == ["S02"]


def test_preflight_rejects_wrong_project_and_drift(tmp_path):
    root = write_plan(build_plan(DEMO), tmp_path)
    lock = json.loads((root / "target.lock.json").read_text("utf-8"))
    assert preflight(lock, {"project": "demo-rooftop", "plan_hash": lock["plan_hash"]}) == []
    assert "项目不一致" in preflight(lock, {"project": "other-project"})[0]
    bad = preflight(lock, {"project": "demo-rooftop", "shots": {"S02": "0" * 64}})
    assert "内容哈希变了" in bad[0]


def test_storyboard_json_with_face_lock_refs(tmp_path):
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "female.png").write_bytes(b"fake-png")
    sb = {"engine": {"base_model": "h3", "steps": 9}, "director_config": {
        "character_refs": [{"role": "the woman", "image": "input/female.png"}]},
        "shots": [{"id": "S01", "frames": 158, "shot_mode": "keyframe", "ref_source": "face_lock",
                   "visual_prompt": "She leans on the wall. Camera: static"}]}
    proj = tmp_path / "bar-demo"
    proj.mkdir()
    (proj / "input").symlink_to(tmp_path / "input")
    (proj / "storyboard.json").write_text(json.dumps(sb), "utf-8")
    plan = build_plan(proj / "storyboard.json")
    m = plan["manifests"][0]
    assert plan["ledger"]["project"] == "bar-demo"
    assert m["refs"][0]["sha256"] and m["workflow"] == {"steps": 9} and m["model"] == "h3"


def test_cli_plan_and_preflight_exit_codes(tmp_path, capsys):
    assert main(["plan", str(DEMO), "--out", str(tmp_path)]) == 0
    lock = str(tmp_path / "demo-rooftop" / "target.lock.json")
    assert main(["preflight", "--lock", lock, "--source", str(DEMO)]) == 0
    assert main(["preflight", "--lock", lock, "--cmd", "python3 run_drama.py --drama other-project"]) == 2
    assert main(["preflight", "--lock", lock, "--source", str(DEMO),
                 "--cmd", "python3 run_drama.py --drama other-project --shots S02"]) == 2
    assert "other-project" in capsys.readouterr().out


def test_gap_card_pinpoints_layer():
    mine = {"prompt": "woman at bar", "refs": ["a.png"], "model": "h3", "workflow": {"steps": 9}}
    ref = {"prompt": "woman at bar, rim light", "refs": ["a.png", "b.png"], "model": "h3", "workflow": {"steps": 20}}
    card = gap_card(mine, ref)
    assert card["model"]["same"] is True
    assert card["workflow"]["diff"] == {"steps": [9, 20]}
    assert card["refs"]["only_ref"] == ["b.png"] and "rim" in card["prompt"]["only_ref"]
