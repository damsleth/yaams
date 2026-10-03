"""Cold load per bucket: fresh process, load -> first prediction on CPU_AND_NE.
  .venv/bin/python coldload.py            # driver: runs each case 3x in fresh processes
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
PY = sys.executable


def child(kind, path, bucket):
  t0 = time.perf_counter()
  import coremltools as ct
  import numpy as np
  t_import = time.perf_counter()
  if kind == "mlpackage":
    m = ct.models.MLModel(path, compute_units=ct.ComputeUnit.CPU_AND_NE)
  else:
    m = ct.models.CompiledMLModel(path, compute_units=ct.ComputeUnit.CPU_AND_NE)
  t_load = time.perf_counter()
  ids = np.ones((16, bucket), np.int32)
  m.predict({"input_ids": ids, "attention_mask": ids})
  t_first = time.perf_counter()
  m.predict({"input_ids": ids, "attention_mask": ids})
  t_second = time.perf_counter()
  print(json.dumps({"import_s": t_import - t0, "load_s": t_load - t_import, "first_predict_s": t_first - t_load,
                    "steady_predict_ms": (t_second - t_first) * 1000}))


def main():
  import coremltools as ct
  for bucket in (128, 256):
    pkg = str(HERE / f"junk-mmbert-L{bucket}.mlpackage")
    mlc = str(HERE / f"junk-mmbert-L{bucket}.cached.mlmodelc")
    if not Path(mlc).exists():
      ct.utils.compile_model(pkg, mlc)
    for kind, path in (("mlpackage", pkg), ("mlmodelc", mlc)):
      for run in range(3):
        out = subprocess.run([PY, __file__, "child", kind, path, str(bucket)], capture_output=True, text=True)
        line = [x for x in out.stdout.splitlines() if x.startswith("{")]
        r = json.loads(line[-1]) if line else {"error": out.stderr[-300:]}
        print(f"L{bucket} {kind:9s} run {run + 1}: "
              + (", ".join(f"{k} {v:.2f}" for k, v in r.items()) if "error" not in r else r["error"]), flush=True)


if __name__ == "__main__":
  if len(sys.argv) > 1 and sys.argv[1] == "child":
    child(sys.argv[2], sys.argv[3], int(sys.argv[4]))
  else:
    main()
