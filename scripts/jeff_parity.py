"""Parity: score jeff-train JSONL rows on a served Jeff (e.g. MLX on the Mac) and
compare with jeff-evaluate predictions from another backend (e.g. PyTorch on CUDA).

  .venv/bin/python scripts/jeff_parity.py --url http://127.0.0.1:8766/v1/systemone \
      --rows a.jsonl b.jsonl --predictions a.predictions.jsonl b.predictions.jsonl
"""
import argparse
import json
import time
import urllib.request


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--url", required=True)
  ap.add_argument("--rows", nargs="+", required=True)
  ap.add_argument("--predictions", nargs="+", required=True)
  ap.add_argument("--batch", type=int, default=50)
  a = ap.parse_args()
  rows = [json.loads(line) for p in a.rows for line in open(p)]
  ref = {p["id"]: p["probabilities"][1] for f in a.predictions for p in map(json.loads, open(f))}
  got, ms = {}, []
  for s in range(0, len(rows), a.batch):
    batch = rows[s:s + a.batch]
    assert all(r["state"] == batch[0]["state"] for r in batch)
    body = {"model": "jeff-latest", "state": batch[0]["state"],
            "questions": {f"q{i}": r["question"] for i, r in enumerate(batch)}}
    req = urllib.request.Request(a.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    resp = json.loads(urllib.request.urlopen(req, timeout=600).read())
    ms.append((time.perf_counter() - t0) * 1000 / len(batch))
    for i, r in enumerate(batch):
      got[r["id"]] = resp["answers"][f"q{i}"]["noul"]
  diffs = sorted(abs(got[i] - ref[i]) for i in got if i in ref)
  flips = {t: sum((got[i] >= t) != (ref[i] >= t) for i in got if i in ref) for t in (0.5, 0.73)}
  print(f"rows {len(diffs)}; |mlx - ref| mean {sum(diffs) / len(diffs):.4f}, median {diffs[len(diffs) // 2]:.4f}, "
        f"max {diffs[-1]:.4f}; decision flips {flips}; {sum(ms) / len(ms):.1f} ms/decision")


if __name__ == "__main__":
  main()
