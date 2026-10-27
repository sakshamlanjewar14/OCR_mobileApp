"""Print a version-specific PaddleOCR repository command; execute only with --execute.

The caller supplies an official training config and matching pretrained weights.
This avoids guessing architecture, dictionary, or changing a production runtime.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def command(args):
    repo = args.repo.resolve()
    tool = repo / "tools" / {"train": "train.py", "eval": "eval.py", "export": "export_model.py"}[args.mode]
    for path in (tool, args.config, args.weights):
        if not path.exists(): raise ValueError(f"Missing required file: {path}")
    checkpoint = str(args.weights.resolve())
    if checkpoint.endswith(".pdparams"): checkpoint = checkpoint[:-9]
    overrides = [f"Global.save_model_dir={args.output.resolve()}"]
    if args.mode == "train":
        for split in ("train", "val"):
            if not (args.data / f"{split}.txt").read_text(encoding="utf-8").strip(): raise ValueError(f"No verified {split} labels")
        overrides += [f"Global.pretrained_model={checkpoint}",
                      f"Train.dataset.data_dir={args.data.resolve()}",
                      f"Train.dataset.label_file_list={json.dumps([str((args.data / 'train.txt').resolve())])}",
                      f"Eval.dataset.data_dir={args.data.resolve()}",
                      f"Eval.dataset.label_file_list={json.dumps([str((args.data / 'val.txt').resolve())])}"]
    elif args.mode == "eval":
        if not (args.data / "test.txt").read_text(encoding="utf-8").strip(): raise ValueError("No held-out test labels")
        overrides += [f"Global.checkpoints={checkpoint}", f"Eval.dataset.data_dir={args.data.resolve()}",
                      f"Eval.dataset.label_file_list={json.dumps([str((args.data / 'test.txt').resolve())])}"]
    else:
        overrides += [f"Global.pretrained_model={checkpoint}", f"Global.save_inference_dir={args.output.resolve()}"]
    return [sys.executable, str(tool), "-c", str(args.config.resolve()), "-o", *overrides]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["train", "eval", "export"])
    for name in ("repo", "config", "weights", "data", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    invocation = command(args)
    print(json.dumps(invocation, indent=2))
    if args.execute: subprocess.run(invocation, cwd=args.repo, check=True)
