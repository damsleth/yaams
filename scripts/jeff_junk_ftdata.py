"""Build a Jeff fine-tune set for the A1 junk decision (jeff-train JSONL rows).

  .venv/bin/python scripts/jeff_junk_ftdata.py --db <live-db copy> --out ~/brain/feed/eval/jeff/ft/junk-v1

Rows: the A1 scope (both Sonnet runs judged the row). Hard label matches the
owner's bar, not Sonnet's: JUNK if Sonnet run1 OR run2 OR Jev (nb rubric) says
JUNK -- the owner sided with a JUNK call on 49/50 A1 disagreements.
# ponytail: hard labels only; soft targets (Jev noul, vote share) are the next ablation

Question shape is the serving shape (jev.noul, F1): a short shared `state`,
the prev/TARGET/next block as `instructions`, the claim as `criteria.true`.
The rubric is left out: the fine-tune learns it, and that cuts ~2/3 of the tokens.

Folds by thread (`family` = thread_id): 80 train / 10 dev / 10 calibration by
stable hash. Held out of every fold: the 50 owner-labelled A1 sheet rows
(written as owner50.jsonl, the eval set), their prev/next neighbours (whose
context shows the owner row's text), and the owner junk golds. owner50 shares
threads with train by design; its rows' own text never appears as a TARGET there.
"""
import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from jev_junk_pass import CRITERION, block  # noqa: E402
from junk_sonnet_pass import context, load_ckpt  # noqa: E402

from yaams.db import open_db  # noqa: E402
from yaams.quality import MODEL_STATE as STATE  # noqa: E402

JEV = Path.home() / "brain/feed/eval/jev"


def fold(thread):
  h = int(hashlib.sha1((thread or "").encode()).hexdigest(), 16) % 100
  return "train" if h < 80 else "dev" if h < 90 else "calibration"


def main():
  ap = argparse.ArgumentParser()
  ap.add_argument("--db", required=True)
  ap.add_argument("--out", required=True)
  ap.add_argument("--sample", type=int, default=200, help="random rows held out for blind owner labels")
  ap.add_argument("--owner-sheet", default="~/brain/feed/eval/jeff/ft/junk-v1/sample_sheet.tsv",
                  help="labelled blind sheet; if it exists its rows are the held-out sample, never rewritten")
  ap.add_argument("--owner-train", nargs="*", help="v3: owner-labelled TSV sheets used as training rows")
  ap.add_argument("--owner-repeat", type=int, default=5, help="repeat owner training rows in the train fold")
  ap.add_argument("--labels", help="v2: junk_relabel.py checkpoint jsonl ({id: JUNK|KEEP}) as training labels")
  a = ap.parse_args()
  out = Path(a.out).expanduser()
  out.mkdir(parents=True, exist_ok=True)

  r1, r2 = load_ckpt(1), load_ckpt(2)
  jev = {r["item_id"]: r for r in map(json.loads, open(JEV / "junk-nb.jsonl"))}
  owner = {r["item_id"]: r["owner_verdict (JUNK/KEEP)"] == "JUNK"
           for r in csv.DictReader(open(JEV / "a1_disagreements.tsv"), delimiter="\t")}
  ids = [i for i in r1 if i in r2 and i in jev and not jev[i]["owner_gold"]]
  conn = open_db(a.db, readonly=True)
  rows = {}
  for s in range(0, len(ids), 500):
    ch = ids[s:s + 500]
    q = f"SELECT id, source, thread_id, timestamp, content FROM items WHERE id IN ({','.join('?' * len(ch))})"
    rows.update({r["id"]: dict(r) for r in conn.execute(q, ch)})
  # hold out the owner rows' neighbours too: their prev/next context would contain the owner row's text
  # (whole threads cost 2/3 of the data, a few huge chats hold most rows)
  # random eval rows for the owner to label blind (owner50 is 98% JUNK, useless as a test)
  sheet_path = Path(a.owner_sheet).expanduser()
  labelled = {}
  if sheet_path.exists():  # reuse the labelled sheet: same 200 rows, owner verdicts, never rewritten
    for r in csv.DictReader(open(sheet_path), delimiter="\t"):
      labelled[r["item_id"]] = (int(r["n"]), r["owner_verdict (J/K)"])
    sample = set(labelled)
  else:
    sample = set(random.Random(29).sample([i for i in ids if i in rows and i not in owner], a.sample))
  relabel = {}
  if a.labels:  # v2: one judge pass at the owner's bar (junk_relabel.py checkpoint)
    for line in open(Path(a.labels).expanduser()):
      relabel.update(json.loads(line))
  owner_train = {}  # v3: owner-labelled training rows (context keeps, A1 rows); they override the relabel
  for path in a.owner_train or []:
    rs = list(csv.DictReader(open(Path(path).expanduser()), delimiter="\t"))
    col = next(c for c in rs[0] if c.startswith("owner_verdict"))
    owner_train.update({r["item_id"]: r[col] in ("J", "JUNK") for r in rs if r[col]})
  held = set()
  for i in [*(i for i in owner if i not in owner_train), *sample]:
    r = rows.get(i)
    if r is None:
      continue
    for op, order in (("<", "DESC"), (">", "ASC")):
      n = conn.execute(f"SELECT id FROM items WHERE thread_id=? AND timestamp{op}? ORDER BY timestamp {order} LIMIT 1",
                       (r["thread_id"], r["timestamp"])).fetchone()
      if n:
        held.add(n[0])

  def train_label(i):
    if relabel:
      return relabel.get(i) == "JUNK" if i in relabel else None
    return "JUNK" in (r1[i], r2[i]) or jev[i]["noul"] >= 0.5

  names = ["train", "dev", "calibration", "owner50"]
  names += ["sample_tune", "sample_test"] if labelled else ["sample_unlabeled"]
  files = {n: open(out / f"{n}.jsonl", "w") for n in names}
  counts = {n: [0, 0] for n in files}
  for i in ids:
    r = rows.get(i)
    if r is None:
      continue
    repeat = 1
    if i in owner_train:  # owner truth, placed by thread like any row; upweighted by repetition in train
      name, label = fold(r["thread_id"]), owner_train[i]
      repeat = a.owner_repeat if name == "train" else 1
    elif i in owner:
      name, label = "owner50", owner[i]
    elif i in sample:
      if labelled:  # owner labels; n 1-100 tune the threshold, 101-200 are the test
        n, v = labelled[i]
        name, label = ("sample_tune" if n <= 100 else "sample_test"), v == "J"
      else:
        name, label = "sample_unlabeled", train_label(i)
    elif i in held:
      continue
    else:
      name = fold(r["thread_id"])
      label = train_label(i)
      if label is None:  # the relabel pass skipped or failed this row
        continue
    dataset = "yaams-junk-v3" if owner_train else "yaams-junk-v2" if relabel else "yaams-junk-v1"
    for k in range(repeat):
      ex = {"id": f"yaams-junk:{i}" + (f":r{k}" if k else ""), "suite": "yaams_junk",
            "family": f"thread:{r['thread_id']}", "state": STATE,
            "question": {"type": "noul", "instructions": block(conn, r), "criteria": {"true": CRITERION}},
            "label": label, "target": label,
            "source": {"dataset": dataset, "lang": jev[i]["lang"], "sonnet": [r1[i], r2[i]],
                       "jev_nb": jev[i]["noul"], "owner": i in owner_train}}
      files[name].write(json.dumps(ex, ensure_ascii=False) + "\n")
      counts[name][label] += 1
  for f in files.values():
    f.close()
  for n, (keep, junk) in counts.items():
    print(f"{n:12s} {keep + junk:6d} rows  JUNK {junk:5d} ({junk / max(1, keep + junk):.0%})")
  print(f"held-out neighbours of owner/sample rows: {len(held)} -> {out}")
  if labelled:
    return
  # blind owner sheet for the random sample: shuffled, no model verdicts shown
  with open(out / "sample_sheet.tsv", "w", newline="") as f:
    w = csv.writer(f, delimiter="\t")
    w.writerow(["n", "source", "prev", "target", "next", "owner_verdict (J/K)", "item_id"])
    order = sorted(sample, key=lambda i: hashlib.sha1(i.encode()).hexdigest())
    for n, i in enumerate(order, 1):
      p, nx = context(conn, rows[i])
      flat = lambda s: " / ".join(ln.strip() for ln in s.strip().splitlines() if ln.strip())  # noqa: E731
      w.writerow([n, rows[i]["source"], flat(p), flat(rows[i]["content"]), flat(nx), "", i])


if __name__ == "__main__":
  main()
