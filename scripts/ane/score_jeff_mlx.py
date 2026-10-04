"""Score unlabelled decision rows with a Jeff (Qwen3.5) checkpoint on MLX -> {"id", "p_junk"} jsonl.

  ~/code/jeff/.venv/bin/python scripts/ane/score_jeff_mlx.py --checkpoint CKPT --rows rows.jsonl --out scores.jsonl
"""
import argparse
import json

from jeff.mlx_backend import MlxDecisionModel


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--checkpoint", required=True)
  ap.add_argument("--rows", required=True)
  ap.add_argument("--out", required=True)
  a = ap.parse_args()
  rows = [json.loads(line) for line in open(a.rows)]
  m = MlxDecisionModel(a.checkpoint)
  res = m.decide([{"state": r["state"], "question": r["question"]} for r in rows])
  with open(a.out, "w") as f:
    for r, (probs, _) in zip(rows, res, strict=True):
      f.write(json.dumps({"id": r["id"], "p_junk": probs[1]}) + "\n")  # options [false, true]
  print(f"{len(rows)} scores -> {a.out}")


if __name__ == "__main__":
  main()
