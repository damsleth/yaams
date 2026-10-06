"""SoC power sampling via `mactop --headless` (no sudo): CPU / GPU / ANE / DRAM watts at 2 Hz."""
import json
import subprocess
import tempfile
import time


class Power:
  def __enter__(self):
    self.out = tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False)
    self.proc = subprocess.Popen(["mactop", "--headless", "--count", "0", "--interval", "500"],
                                 stdout=self.out, stderr=subprocess.DEVNULL)
    time.sleep(1.2)  # first sample
    self.t0 = time.time()
    return self

  def __exit__(self, *exc):
    self.t1 = time.time()
    time.sleep(0.6)
    self.proc.terminate()
    self.proc.wait()
    self.out.flush()
    text = open(self.out.name).read()
    dec, i, self.samples = json.JSONDecoder(), 0, []
    while i < len(text):
      while i < len(text) and text[i] in " \n\r\t,[]":
        i += 1
      if i >= len(text):
        break
      obj, i = dec.raw_decode(text, i)
      for o in obj if isinstance(obj, list) else [obj]:
        self.samples.append(o["soc_metrics"])
    return False

  def mean(self, skip=1):
    s = self.samples[skip:] or self.samples
    keys = ("cpu_power", "gpu_power", "ane_power", "dram_power")
    m = {k: sum(x.get(k, 0) for x in s) / len(s) for k in keys}
    m["soc"] = sum(m[k] for k in keys)
    m["ane_active"] = sum(x.get("ane_active", 0) for x in s) / len(s)
    m["gpu_active"] = sum(x.get("gpu_active", 0) for x in s) / len(s)
    m["n"] = len(s)
    return m


def idle(seconds=10):
  with Power() as p:
    time.sleep(seconds)
  return p.mean()


def report(name, rows, seconds, p, base):
  m = p.mean()
  extra = m["soc"] - base["soc"]
  print(f"{name:28s} {rows / seconds:7.1f} rows/s  {seconds / rows * 1000:6.2f} ms/row | "
        f"SoC {m['soc']:5.1f} W (+{extra:4.1f} over idle; cpu {m['cpu_power']:.1f} gpu {m['gpu_power']:.1f} "
        f"ane {m['ane_power']:.1f} dram {m['dram_power']:.1f}) | {extra * seconds / rows * 1000:6.1f} mJ/row "
        f"| gpu {m['gpu_active']:.0f}% ane {m['ane_active']:.0f}%", flush=True)
  return {"name": name, "rows_per_s": rows / seconds, "ms_per_row": seconds / rows * 1000,
          "soc_w": m["soc"], "extra_w": extra, "mj_per_row": extra * seconds / rows * 1000, **m}
