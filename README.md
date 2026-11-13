# shot-ledger

CPU-first storyboard splitter & GPU render preflight: frame buckets, one-action shots, target lock, cost ledger.

在本地 CPU 上把分镜做完，再上 GPU 渲染。分镜规则对齐 H3 图生视频引擎（`17n+5` 帧，24 fps，一镜一个主要动作、一种运镜）。

## 能做什么

| 命令 | 作用 |
|---|---|
| `lint` | 检查分镜：帧数吸附到 124/141/158/175/192/209，按台词长度自动加帧，动作或运镜超过 1 个就提示拆镜，超过 209 帧报 ERROR |
| `plan` | 在 lint 的基础上写出审计清单：每镜一个 `manifest/<shot>.json`（提示词、参考图及其 sha256、模型、工作流参数、帧数、预估 GPU 秒数、内容哈希），另外写出 `ledger.json`（总时长、总 GPU、花费）和 `target.lock.json` |
| `preflight` | 开跑前核对：项目名和内容哈希必须和 `target.lock.json` 一致，否则退出码为 2，不会开跑 |
| `gap` | 拿我的样本和参考样本，按提示词、参考图、模型、工作流四项逐一对比，先找出差距在哪一层 |

## 安装

只依赖 Python 3.8+ 标准库。要读 `.yaml` 文件需另装 PyYAML；只用 `.json` 的话不用装。

```bash
pip install pyyaml   # 可选
python3 -m shot_ledger -h
```

## 用法

### 1. 从剧本拆镜（scenes / beats）

一个 beat 拆成一镜，见 `examples/demo_project.yaml`。每个场景的第一镜用 `keyframe`，后面默认用 `continue`；连续接力到第 3 次后，下一镜自动改回 `keyframe`。

```bash
python3 -m shot_ledger plan examples/demo_project.yaml --out out
```

### 2. 直接检查现有的 storyboard.json

```bash
python3 -m shot_ledger lint path/to/drama/<slug>/storyboard.json --sec-per-frame 1.5 --usd-per-hour 0.6
```

项目名取 `project` 字段；没有这个字段时，用 storyboard.json 所在目录的名字（也就是 slug）。

### 3. 开跑前锁定目标

```bash
python3 -m shot_ledger plan drama/<slug>/storyboard.json --out out
python3 -m shot_ledger preflight --lock out/<slug>/target.lock.json \
    --source drama/<slug>/storyboard.json \
    --cmd "python3 run_drama.py --drama <slug> --shots S02a,S02b" \
  && python3 run_drama.py --drama <slug> --shots S02a,S02b
```

出现下面任一情况都会被拒绝：

- 渲染命令或任务里的项目名和锁定的项目名不一样（例如锁的是 `demo-a`，结果要跑 `demo-b`）
- 锁定之后，分镜、提示词、参考图或工作流参数被改过（`plan_hash` 或单镜哈希变了）
- 要跑的镜头不在锁定清单里

也可以用 `--job job.json` 传入正在跑或待提交的任务，格式为 `{"project": "...", "shots": {"S01a": "<hash>"}}`。

### 4. 差距对比卡

```bash
python3 -m shot_ledger gap mine.json ref.json
```

两个文件的格式都是 `{prompt, refs:[...], model, workflow:{steps, cfg, ...}}`。输出一张四行对比表，哪一项不一致就先改哪一项。

## 参数说明

- `rules`：`max_actions`、`max_camera_moves`、`max_sequence`、`max_continue_chain`，默认值依次为 1 / 1 / 1 / 3。动作上限比引擎自带 lint 的 3 更严，按「一镜一个主要动作」来查。
- `gpu`：`sec_per_frame`、`shot_overhead_sec`、`keyframe_sec`、`usd_per_hour`。默认值只是占位，请用你自己 GPU 上实测的数字覆盖。
- 动作和运镜是用关键词近似计数的；表情、呼吸、声音不算动作。

## 测试

```bash
python3 -m pytest -q
```
