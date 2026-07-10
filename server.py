#!/usr/bin/env python3
"""Minimal stdlib-only HTTP endpoint exposing Pi/printer status as JSON + a dashboard."""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
WIFI_IFACE = "wlan0"
OCTOPRINT_BASE = "http://127.0.0.1:5000"
OCTOPRINT_CONFIG = os.path.expanduser("~/.octoprint/config.yaml")
UPLOADS_DIR = os.path.expanduser("~/.octoprint/uploads")

THROTTLE_BITS = {
    0: "under_voltage_now",
    1: "arm_freq_capped_now",
    2: "currently_throttled",
    3: "soft_temp_limit_now",
    16: "under_voltage_occurred",
    17: "arm_freq_capped_occurred",
    18: "throttling_occurred",
    19: "soft_temp_limit_occurred",
}

_updates_cache = {"count": None, "ts": 0}
UPDATES_CACHE_TTL = 1800  # apt list takes ~2.5s, don't run it every poll

_temp_events = []
_last_soft_temp_state = None


def get_wifi():
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if line.strip().startswith(WIFI_IFACE):
                    fields = line.split()
                    quality = float(fields[2].rstrip("."))
                    level = float(fields[3].rstrip("."))
                    return {"rssi_dbm": level, "link_quality_pct": round(quality / 70 * 100, 1)}
    except Exception as e:
        return {"error": str(e)}
    return {"rssi_dbm": None, "link_quality_pct": None}


def get_ssid():
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    except Exception as e:
        return {"error": str(e)}
    return None


def get_lan_ip():
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", WIFI_IFACE],
            capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception as e:
        return {"error": str(e)}


def run_vcgencmd(*args):
    try:
        out = subprocess.run(
            ["vcgencmd", *args], capture_output=True, text=True, timeout=3
        )
        return out.stdout.strip()
    except Exception as e:
        return f"error: {e}"


def get_voltage():
    raw = run_vcgencmd("measure_volts")
    m = re.search(r"volt=([\d.]+)V", raw)
    return float(m.group(1)) if m else raw


def get_temp():
    raw = run_vcgencmd("measure_temp")
    m = re.search(r"temp=([\d.]+)", raw)
    return float(m.group(1)) if m else raw


def get_arm_clock_mhz():
    raw = run_vcgencmd("measure_clock", "arm")
    m = re.search(r"frequency\(\d+\)=(\d+)", raw)
    return round(int(m.group(1)) / 1_000_000) if m else raw


def get_throttled():
    raw = run_vcgencmd("get_throttled")
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", raw)
    if not m:
        return {"raw": raw}
    val = int(m.group(1), 16)
    flags = {name: bool(val & (1 << bit)) for bit, name in THROTTLE_BITS.items()}
    return {"raw": hex(val), **flags}


def track_temp_events(throttled):
    """dmesg doesn't log soft-temp-limit transitions the way it logs
    under-voltage ones, so we track them ourselves in-process. Resets
    on service restart (not persisted like the PSU log, which reads dmesg)."""
    global _last_soft_temp_state
    current = throttled.get("soft_temp_limit_now")
    if _last_soft_temp_state is not None and current != _last_soft_temp_state:
        _temp_events.append({
            "time": datetime.now().strftime("%a %b %d %H:%M:%S %Y"),
            "event": "Soft temp limit engaged" if current else "Soft temp limit cleared",
        })
    _last_soft_temp_state = current
    return _temp_events[-10:]


def get_disk():
    try:
        total, used, free = shutil.disk_usage("/")
        return {
            "total_gb": round(total / 1024**3, 1),
            "used_gb": round(used / 1024**3, 1),
            "percent_used": round(100 * used / total, 1),
        }
    except Exception as e:
        return {"error": str(e)}


def get_memory():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, val = line.split(":", 1)
                info[key] = int(val.strip().split()[0])
        total_kb = info["MemTotal"]
        avail_kb = info.get("MemAvailable", info["MemFree"])
        used_kb = total_kb - avail_kb
        swap_total_kb = info.get("SwapTotal", 0)
        swap_free_kb = info.get("SwapFree", 0)
        return {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "percent_used": round(100 * used_kb / total_kb, 1),
            "swap_total_mb": round(swap_total_kb / 1024),
            "swap_used_mb": round((swap_total_kb - swap_free_kb) / 1024),
        }
    except Exception as e:
        return {"error": str(e)}


def get_cpu_percent():
    def read_idle_total():
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:8]]
        return fields[3], sum(fields)

    try:
        idle1, total1 = read_idle_total()
        time.sleep(0.2)
        idle2, total2 = read_idle_total()
        total_delta = total2 - total1
        if total_delta <= 0:
            return None
        return round(100 * (1 - (idle2 - idle1) / total_delta), 1)
    except Exception as e:
        return {"error": str(e)}


def get_uptime():
    try:
        with open("/proc/uptime") as f:
            seconds = int(float(f.read().split()[0]))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = ([f"{days}d"] if days else []) + [f"{hours}h", f"{minutes}m"]
        return " ".join(parts)
    except Exception as e:
        return {"error": str(e)}


def get_updates_available():
    now = time.time()
    if now - _updates_cache["ts"] > UPDATES_CACHE_TTL:
        try:
            out = subprocess.run(
                ["apt", "list", "--upgradable"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            lines = [l for l in out.strip().splitlines() if not l.startswith("Listing")]
            _updates_cache["count"] = len(lines)
        except Exception as e:
            _updates_cache["count"] = {"error": str(e)}
        _updates_cache["ts"] = now
    return _updates_cache["count"]


def get_reboot_required():
    return os.path.exists("/var/run/reboot-required")


def get_uploads():
    try:
        entries = []
        for e in os.scandir(UPLOADS_DIR):
            if e.name.startswith("."):
                continue
            st = e.stat()
            entries.append({
                "name": e.name,
                "size_mb": round(st.st_size / 1024**2, 2),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "mtime_sort": st.st_mtime,
            })
        entries.sort(key=lambda x: x["mtime_sort"], reverse=True)
        for e in entries:
            del e["mtime_sort"]
        return entries
    except Exception as e:
        return [{"error": str(e)}]


def get_last_print_outcome():
    try:
        with open(os.path.join(UPLOADS_DIR, ".metadata.json")) as f:
            meta = json.load(f)
        latest = None
        for fname, info in meta.items():
            for h in info.get("history", []):
                ts = h.get("timestamp")
                if ts and (latest is None or ts > latest["timestamp"]):
                    latest = {"timestamp": ts, "success": h.get("success"), "file": fname}
        if not latest:
            return None
        return {
            "file": latest["file"],
            "success": latest["success"],
            "time": datetime.fromtimestamp(latest["timestamp"]).strftime("%Y-%m-%d %H:%M"),
        }
    except Exception as e:
        return {"error": str(e)}


def _octoprint_api_key():
    try:
        with open(OCTOPRINT_CONFIG) as f:
            m = re.search(r"^api:\s*\n\s*key:\s*(\S+)", f.read(), re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None


def _octoprint_get(path):
    api_key = _octoprint_api_key()
    if not api_key:
        raise RuntimeError("octoprint api key not found")
    req = urllib.request.Request(OCTOPRINT_BASE + path, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=3) as resp:
        return json.loads(resp.read())


def _octoprint_post(path, payload):
    api_key = _octoprint_api_key()
    if not api_key:
        raise RuntimeError("octoprint api key not found")
    req = urllib.request.Request(
        OCTOPRINT_BASE + path,
        data=json.dumps(payload).encode(),
        headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def restart_last_print():
    """Re-select and start the most recently printed file. Gated on the
    printer being idle/connected right now -- OctoPrint doesn't record *why*
    a print failed (just success: false), so this can't tell a USB-unplug
    failure apart from a jam or thermal issue. It only guards against firing
    into a printer that's mid-print or disconnected."""
    conn = get_printer_connection()
    if conn.get("state") != "Operational":
        return {"ok": False, "error": f"printer not ready (state={conn.get('state')!r}, must be 'Operational')"}
    last = get_last_print_outcome()
    if not last:
        return {"ok": False, "error": "no print history found"}
    filename = last["file"]
    try:
        _octoprint_post(
            "/api/files/local/" + urllib.parse.quote(filename),
            {"command": "select", "print": True},
        )
        return {"ok": True, "file": filename}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"octoprint http {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_printer_connection():
    """Connection state of OctoPrint's serial link to the 3D printer.
    OctoPrint's API doesn't track raw RX/TX byte counts, only link state/port/baud."""
    try:
        cur = _octoprint_get("/api/connection").get("current", {})
        return {
            "state": cur.get("state"),
            "port": cur.get("port"),
            "baudrate": cur.get("baudrate"),
            "printer_profile": cur.get("printerProfile"),
        }
    except Exception as e:
        return {"error": str(e)}


def _format_seconds(s):
    s = int(s)
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    return (f"{h}h " if h else "") + f"{m}m"


def get_job():
    try:
        data = _octoprint_get("/api/job")
        job = data.get("job") or {}
        progress = data.get("progress") or {}
        file_info = job.get("file") or {}
        left = progress.get("printTimeLeft")
        completion = progress.get("completion")
        return {
            "state": data.get("state"),
            "file": file_info.get("display"),
            "completion_pct": round(completion, 1) if completion is not None else None,
            "time_left": _format_seconds(left) if left is not None else None,
        }
    except Exception as e:
        return {"error": str(e)}


def get_printer_temps():
    try:
        data = _octoprint_get("/api/printer")
        if "error" in data:
            return {"connected": False, "message": data["error"]}
        temps = data.get("temperature", {})
        tool0 = temps.get("tool0", {})
        bed = temps.get("bed", {})
        return {
            "connected": True,
            "nozzle_actual": tool0.get("actual"),
            "nozzle_target": tool0.get("target"),
            "bed_actual": bed.get("actual"),
            "bed_target": bed.get("target"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_power_supply(throttled):
    """Pi 3 has no PSU input ADC (that's Pi 4/5-only via the PMIC) -- the only
    real signal is the under-voltage comparator, surfaced here plus its
    dmesg event history so you can see how often/when the PSU has sagged."""
    events = []
    try:
        out = subprocess.run(
            ["dmesg", "-T"], capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines():
            if "Undervoltage detected" in line or "Voltage normalised" in line:
                m = re.match(r"\[(.*?)\]\s*(.*)", line)
                if m:
                    events.append({"time": m.group(1), "event": m.group(2).strip()})
    except Exception as e:
        events = [{"error": str(e)}]

    stable_for = None
    if events and "time" in events[-1]:
        try:
            last_dt = datetime.strptime(events[-1]["time"], "%a %b %d %H:%M:%S %Y")
            secs = int((datetime.now() - last_dt).total_seconds())
            if secs < 60:
                stable_for = f"{secs}s"
            elif secs < 3600:
                stable_for = f"{secs // 60}m"
            else:
                stable_for = f"{secs // 3600}h {(secs % 3600) // 60}m"
        except Exception:
            pass

    return {
        "under_voltage_now": throttled.get("under_voltage_now"),
        "under_voltage_occurred": throttled.get("under_voltage_occurred"),
        "drop_count": sum(1 for e in events if "Undervoltage detected" in e.get("event", "")),
        "recent_events": events[-10:],
        "stable_for": stable_for,
    }


DASHBOARD_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Status</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #fafafa; --fg: #1a1a1a;
    --card-bg: #fff; --card-border: #e5e5e5;
    --label: rgba(0,0,0,.55); --updated: rgba(0,0,0,.4);
    --pill-ok-bg: #e6f7ea; --pill-ok-fg: #1a7a34;
    --pill-bad-bg: #fbe6e6; --pill-bad-fg: #b31f1f;
    --warn-fg: #9a6b00;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #121212; --fg: #e8e8e8;
      --card-bg: #1e1e1e; --card-border: #333;
      --label: rgba(255,255,255,.55); --updated: rgba(255,255,255,.4);
      --pill-ok-bg: #123a1f; --pill-ok-fg: #7ee08a;
      --pill-bad-bg: #3a1212; --pill-bad-fg: #ff8a8a;
      --warn-fg: #e0b34d;
    }
  }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 1100px; margin: 0 auto; padding: 1rem;
    color: var(--fg); background: var(--bg);
    box-sizing: border-box; height: 100vh;
    display: grid; grid-template-columns: repeat(3, 1fr);
    grid-template-rows: auto 1fr 1fr 8.5rem auto;
    gap: .6rem; overflow-y: auto;
  }
  h1 { grid-column: 1 / -1; font-size: 1.05rem; font-weight: 600; margin: 0; color: var(--label); }
  .card {
    background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px;
    padding: .75rem 1rem; box-sizing: border-box;
    display: flex; flex-direction: column; min-height: 0;
  }
  .card-title { font-size: .78rem; color: var(--label); margin-bottom: .25rem; font-weight: 600; }
  .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 .6rem; }
  .metric { display: flex; justify-content: space-between; align-items: baseline; padding: .18rem 0; gap: .4rem; }
  .metric .label { color: var(--label); font-size: .74rem; white-space: nowrap; }
  .metric .value { color: var(--fg); font-size: .85rem; font-weight: 600; font-variant-numeric: tabular-nums; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .metric .value.ok { color: var(--pill-ok-fg); }
  .metric .value.warn { color: var(--warn-fg); }
  .metric .value.bad { color: var(--pill-bad-fg); }
  .flags { display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .25rem; }
  .flags-label { font-size: .74rem; color: var(--label); margin-top: .3rem; }
  .pill { font-size: .68rem; padding: .18rem .5rem; border-radius: 999px; white-space: nowrap; }
  .pill.ok { background: var(--pill-ok-bg); color: var(--pill-ok-fg); }
  .pill.bad { background: var(--pill-bad-bg); color: var(--pill-bad-fg); }
  .updated { grid-column: 1 / -1; font-size: .7rem; color: var(--updated); text-align: center; }
  .scroll-list { list-style: none; margin: .25rem 0 0; padding: 0; font-size: .7rem; overflow-y: auto; flex: 1; min-height: 0; }
  .scroll-list li { display: flex; justify-content: space-between; gap: .5rem; padding: .18rem 0; border-top: 1px solid var(--card-border); color: var(--label); }
  .scroll-list li:first-child { border-top: none; }
  .scroll-list .item-name { color: var(--fg); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .no-events { font-size: .72rem; color: var(--label); margin-top: .2rem; }
  .bar { height: 5px; border-radius: 3px; background: var(--card-border); overflow: hidden; margin-top: .35rem; }
  .bar-fill { height: 100%; background: var(--pill-ok-fg); width: 0%; }
  .span2 { grid-column: span 2; }
  .span3 { grid-column: 1 / -1; }
  .psu-card { flex-direction: row; align-items: stretch; gap: 1.5rem; }
  .psu-left { flex: 0 0 auto; min-width: 12rem; }
  .psu-right { flex: 1; display: flex; flex-direction: column; min-height: 0; }
  .restart-btn {
    margin-top: .5rem; font-size: .78rem; font-weight: 600; padding: .45rem .8rem;
    border-radius: 8px; border: 1px solid var(--pill-bad-fg); background: var(--pill-bad-bg);
    color: var(--pill-bad-fg); cursor: pointer;
  }
  .restart-btn:disabled {
    border-color: var(--card-border); background: transparent; color: var(--label); cursor: not-allowed;
  }
</style>
</head>
<body>
<h1>skypi3-octopi</h1>

<div class="card">
  <div class="card-title">Vitals</div>
  <div class="metrics-grid">
    <div class="metric"><span class="label">SSID</span><span class="value" id="ssid">&mdash;</span></div>
    <div class="metric"><span class="label">LAN IP</span><span class="value" id="lan-ip">&mdash;</span></div>
    <div class="metric"><span class="label">RSSI</span><span class="value" id="rssi">&mdash;</span></div>
    <div class="metric"><span class="label">Quality</span><span class="value" id="quality">&mdash;</span></div>
    <div class="metric"><span class="label">Voltage</span><span class="value" id="voltage">&mdash;</span></div>
    <div class="metric"><span class="label">CPU Temp</span><span class="value" id="temp">&mdash;</span></div>
    <div class="metric"><span class="label">ARM Clock</span><span class="value" id="arm-clock">&mdash;</span></div>
  </div>
</div>

<div class="card">
  <div class="card-title">Throttle State</div>
  <div class="flags" id="flags"></div>
  <div class="card-title" style="margin-top:.5rem;">Temp Limit Events</div>
  <ul class="scroll-list" id="temp-events"></ul>
</div>

<div class="card">
  <div class="card-title">System</div>
  <div class="metrics-grid">
    <div class="metric"><span class="label">Disk</span><span class="value" id="disk">&mdash;</span></div>
    <div class="metric"><span class="label">Memory</span><span class="value" id="mem">&mdash;</span></div>
    <div class="metric"><span class="label">Swap</span><span class="value" id="swap">&mdash;</span></div>
    <div class="metric"><span class="label">CPU</span><span class="value" id="cpu">&mdash;</span></div>
    <div class="metric"><span class="label">Uptime</span><span class="value" id="uptime">&mdash;</span></div>
    <div class="metric"><span class="label">Clock</span><span class="value" id="sys-clock">&mdash;</span></div>
    <div class="metric"><span class="label">Updates</span><span class="value" id="updates">&mdash;</span></div>
    <div class="metric"><span class="label">Reboot</span><span class="value" id="reboot-required">&mdash;</span></div>
  </div>
</div>

<div class="card span2">
  <div class="card-title">Printer</div>
  <div class="metrics-grid">
    <div class="metric"><span class="label">Serial Link</span><span class="value" id="printer-state">&mdash;</span></div>
    <div class="metric"><span class="label">Port</span><span class="value" id="printer-port">&mdash;</span></div>
    <div class="metric"><span class="label">Baud</span><span class="value" id="printer-baud">&mdash;</span></div>
    <div class="metric"><span class="label">Job</span><span class="value" id="job-state">&mdash;</span></div>
    <div class="metric"><span class="label">Nozzle</span><span class="value" id="nozzle-temp">&mdash;</span></div>
    <div class="metric"><span class="label">Bed</span><span class="value" id="bed-temp">&mdash;</span></div>
  </div>
  <div class="metric"><span class="label" id="job-file">&mdash;</span><span class="value" id="job-time-left">&mdash;</span></div>
  <div class="bar"><div class="bar-fill" id="job-progress-bar"></div></div>
  <div class="flags-label">Last print: <span id="last-print">&mdash;</span></div>
  <button id="restart-btn" class="restart-btn" disabled>Restart Last Print</button>
</div>

<div class="card">
  <div class="card-title">Uploads</div>
  <ul class="scroll-list" id="uploads-list"></ul>
</div>

<div class="card psu-card span3">
  <div class="psu-left">
    <div class="card-title">Power Supply (5V rail)</div>
    <div class="flags" id="psu-flags"></div>
    <div class="flags-label">Drop events since boot: <span id="psu-count">&mdash;</span></div>
    <div class="flags-label">Stable for: <span id="psu-stable">&mdash;</span></div>
  </div>
  <div class="psu-right">
    <div class="card-title">Recent events</div>
    <ul class="scroll-list" id="psu-events"></ul>
  </div>
</div>

<div class="updated" id="updated">loading&hellip;</div>
<script>
let lastData = null;
let restartInFlight = false;

function classifyPrinterTemp(actual, target) {
  if (actual == null) return '';
  if (target && target > 0) return Math.abs(actual - target) <= 3 ? 'ok' : 'warn';
  return actual >= 50 ? 'warn' : 'ok';
}

async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    lastData = d;

    document.getElementById('ssid').textContent = d.ssid || 'unknown';
    document.getElementById('lan-ip').textContent = d.lan_ip || '—';

    const rssiEl = document.getElementById('rssi');
    rssiEl.textContent = d.rssi_dbm + ' dBm';
    rssiEl.className = 'value ' + (d.rssi_dbm >= -60 ? 'ok' : d.rssi_dbm >= -70 ? 'warn' : 'bad');

    const qualityEl = document.getElementById('quality');
    const q = d.link_quality_pct;
    qualityEl.textContent = (q != null ? q + '%' : '—');
    qualityEl.className = 'value ' + (q == null ? '' : q >= 70 ? 'ok' : q >= 40 ? 'warn' : 'bad');
    document.getElementById('voltage').textContent = d.voltage + ' V';

    const tempEl = document.getElementById('temp');
    tempEl.textContent = d.temp_c + ' °C';
    tempEl.className = 'value ' + (d.temp_c < 60 ? 'ok' : d.temp_c < 70 ? 'warn' : 'bad');

    document.getElementById('arm-clock').textContent = (d.arm_clock_mhz != null ? d.arm_clock_mhz + ' MHz' : '—');

    const flagsEl = document.getElementById('flags');
    flagsEl.innerHTML = '';
    for (const [key, val] of Object.entries(d.throttled)) {
      if (key === 'raw') continue;
      const pill = document.createElement('span');
      pill.className = 'pill ' + (val ? 'bad' : 'ok');
      pill.textContent = key.replaceAll('_', ' ');
      flagsEl.appendChild(pill);
    }

    const teEl = document.getElementById('temp-events');
    teEl.innerHTML = '';
    const tevents = d.temp_events || [];
    if (tevents.length === 0) {
      teEl.innerHTML = '<div class="no-events">No soft temp-limit transitions since service start.</div>';
    } else {
      for (const ev of tevents.slice().reverse()) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="item-name">' + ev.event + '</span><span>' + ev.time + '</span>';
        teEl.appendChild(li);
      }
    }

    const sys = d.system || {};
    const disk = sys.disk || {};
    document.getElementById('disk').textContent = disk.used_gb + '/' + disk.total_gb + ' GB';
    const mem = sys.memory || {};
    document.getElementById('mem').textContent = mem.used_mb + '/' + mem.total_mb + ' MB';
    document.getElementById('swap').textContent = mem.swap_used_mb + '/' + mem.swap_total_mb + ' MB';
    document.getElementById('cpu').textContent = sys.cpu_percent + ' %';
    document.getElementById('uptime').textContent = sys.uptime;
    document.getElementById('sys-clock').textContent = sys.clock;
    document.getElementById('updates').textContent = (sys.updates_available != null ? sys.updates_available : '—');
    const rebootEl = document.getElementById('reboot-required');
    rebootEl.textContent = sys.reboot_required ? 'yes' : 'no';
    rebootEl.className = 'value ' + (sys.reboot_required ? 'bad' : 'ok');

    const pc = d.printer_connection || {};
    const connected = ['Operational', 'Printing', 'Paused'].includes(pc.state);
    const stateEl = document.getElementById('printer-state');
    stateEl.textContent = pc.state || pc.error || 'unknown';
    stateEl.className = 'value ' + (pc.error ? '' : (connected ? 'ok' : 'bad'));
    document.getElementById('printer-port').textContent = pc.port || 'none';
    document.getElementById('printer-baud').textContent = pc.baudrate || '—';

    const job = d.printer_job || {};
    document.getElementById('job-state').textContent = job.state || job.error || 'unknown';
    document.getElementById('job-file').textContent = job.file || 'No file loaded';
    document.getElementById('job-time-left').textContent = job.time_left ? (job.time_left + ' left') : '—';
    document.getElementById('job-progress-bar').style.width = (job.completion_pct || 0) + '%';

    const temps = d.printer_temps || {};
    const nozzleEl = document.getElementById('nozzle-temp');
    const bedEl = document.getElementById('bed-temp');
    if (temps.connected) {
      nozzleEl.textContent = temps.nozzle_actual + '/' + temps.nozzle_target + ' °C';
      nozzleEl.className = 'value ' + classifyPrinterTemp(temps.nozzle_actual, temps.nozzle_target);
      bedEl.textContent = temps.bed_actual + '/' + temps.bed_target + ' °C';
      bedEl.className = 'value ' + classifyPrinterTemp(temps.bed_actual, temps.bed_target);
    } else {
      nozzleEl.textContent = 'n/a';
      nozzleEl.className = 'value';
      bedEl.textContent = 'n/a';
      bedEl.className = 'value';
    }

    const lp = d.last_print;
    const lpEl = document.getElementById('last-print');
    if (lp && !lp.error) {
      lpEl.textContent = (lp.success ? 'success' : 'failed') + ' — ' + lp.file + ' (' + lp.time + ')';
      lpEl.style.color = lp.success ? 'var(--pill-ok-fg)' : 'var(--pill-bad-fg)';
    } else {
      lpEl.textContent = 'no history yet';
      lpEl.style.color = 'var(--label)';
    }

    const restartBtn = document.getElementById('restart-btn');
    if (!restartInFlight) {
      const canRestart = lp && !lp.error && lp.success === false && pc.state === 'Operational';
      restartBtn.disabled = !canRestart;
      restartBtn.textContent = 'Restart Last Print';
      restartBtn.title = canRestart
        ? 'Reprint "' + lp.file + '"'
        : 'Only enabled when the last print failed and the printer is idle/connected';
    }

    const uploadsEl = document.getElementById('uploads-list');
    uploadsEl.innerHTML = '';
    const uploads = d.uploads || [];
    if (uploads.length === 0) {
      uploadsEl.innerHTML = '<div class="no-events">No files uploaded.</div>';
    } else {
      for (const f of uploads) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="item-name" title="' + f.name + '">' + f.name + '</span><span>' + f.size_mb + ' MB</span>';
        uploadsEl.appendChild(li);
      }
    }

    const psuFlagsEl = document.getElementById('psu-flags');
    psuFlagsEl.innerHTML = '';
    for (const key of ['under_voltage_now', 'under_voltage_occurred']) {
      const pill = document.createElement('span');
      pill.className = 'pill ' + (d.power_supply[key] ? 'bad' : 'ok');
      pill.textContent = key.replaceAll('_', ' ');
      psuFlagsEl.appendChild(pill);
    }
    document.getElementById('psu-count').textContent = d.power_supply.drop_count;
    document.getElementById('psu-stable').textContent = d.power_supply.stable_for || '—';
    const eventsEl = document.getElementById('psu-events');
    eventsEl.innerHTML = '';
    const events = d.power_supply.recent_events || [];
    if (events.length === 0) {
      eventsEl.innerHTML = '<div class="no-events">No under-voltage events logged since last boot.</div>';
    } else {
      for (const ev of events.slice().reverse()) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="item-name">' + ev.event + '</span><span>' + ev.time + '</span>';
        eventsEl.appendChild(li);
      }
    }

    document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('updated').textContent = 'fetch failed: ' + e;
  }
}
document.getElementById('restart-btn').addEventListener('click', async () => {
  if (!lastData || !lastData.last_print) return;
  const lp = lastData.last_print;
  if (!confirm('Restart print of "' + lp.file + '"?\\n\\nThis will immediately start heating and extruding on the printer.')) return;

  restartInFlight = true;
  const btn = document.getElementById('restart-btn');
  btn.disabled = true;
  btn.textContent = 'Starting…';
  try {
    const res = await fetch('/restart-print', { method: 'POST' });
    const result = await res.json();
    if (result.ok) {
      btn.textContent = 'Started ✓';
    } else {
      alert('Failed to restart: ' + result.error);
      btn.textContent = 'Restart Last Print';
    }
  } catch (e) {
    alert('Request failed: ' + e);
    btn.textContent = 'Restart Last Print';
  }
  restartInFlight = false;
  refresh();
});

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep it quiet, no disk writes

    def do_GET(self):
        if self.path == "/":
            body = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path != "/status":
            self.send_response(404)
            self.end_headers()
            return
        throttled = get_throttled()
        wifi = get_wifi()
        body = json.dumps(
            {
                "ssid": get_ssid(),
                "lan_ip": get_lan_ip(),
                "rssi_dbm": wifi.get("rssi_dbm"),
                "link_quality_pct": wifi.get("link_quality_pct"),
                "voltage": get_voltage(),
                "temp_c": get_temp(),
                "arm_clock_mhz": get_arm_clock_mhz(),
                "throttled": throttled,
                "temp_events": track_temp_events(throttled),
                "power_supply": get_power_supply(throttled),
                "printer_connection": get_printer_connection(),
                "printer_job": get_job(),
                "printer_temps": get_printer_temps(),
                "last_print": get_last_print_outcome(),
                "system": {
                    "disk": get_disk(),
                    "memory": get_memory(),
                    "cpu_percent": get_cpu_percent(),
                    "uptime": get_uptime(),
                    "clock": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "updates_available": get_updates_available(),
                    "reboot_required": get_reboot_required(),
                },
                "uploads": get_uploads(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/restart-print":
            self.send_response(404)
            self.end_headers()
            return
        result = restart_last_print()
        body = json.dumps(result).encode()
        self.send_response(200 if result.get("ok") else 409)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
