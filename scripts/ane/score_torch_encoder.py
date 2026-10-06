"""Reference scores for a jeff encoder checkpoint in PyTorch (CPU) -> {"id", "p_junk"} jsonl,
for the ANE parity gate.

  ~/code/jeff/.venv/bin/python scripts/ane/score_torch_encoder.py --checkpoint CKPT --rows rows.jsonl --out ref.jsonl
"""
import argparse
import json

from jeff.encoder import EncoderDecisionModel


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--checkpoint", required=True)
  ap.add_argument("--rows", required=True)
  ap.add_argument("--out", required=True)
  a = ap.parse_args()
  rows = [json.loads(line) for line in open(a.rows)]
  m = EncoderDecisionModel(a.checkpoint, device="cpu")
  probs = m.predict([{"state": r["state"], "question": r["question"]} for r in rows], batch_size=16)
  with open(a.out, "w") as f:
    for r, p in zip(rows, probs, strict=True):
      f.write(json.dumps({"id": r["id"], "p_junk": p[1]}) + "\n")
  print(f"{len(rows)} reference scores -> {a.out}")


if __name__ == "__main__":
  main()
