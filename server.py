#!/usr/bin/env python3
"""Minimal stdlib-only HTTP endpoint exposing Pi wifi RSSI, core voltage, and throttle state."""
import json
import os
import re
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
WIFI_IFACE = "wlan0"
OCTOPRINT_URL = "http://127.0.0.1:5000/api/connection"
OCTOPRINT_CONFIG = os.path.expanduser("~/.octoprint/config.yaml")

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


def get_rssi():
    try:
        with open("/proc/net/wireless") as f:
            for line in f:
                if line.strip().startswith(WIFI_IFACE):
                    fields = line.split()
                    return float(fields[3].rstrip("."))
    except Exception as e:
        return {"error": str(e)}
    return None


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


def get_printer_connection():
    """Connection state of OctoPrint's serial link to the 3D printer.
    OctoPrint's API doesn't track raw RX/TX byte counts, only link state/port/baud."""
    try:
        with open(OCTOPRINT_CONFIG) as f:
            m = re.search(r"^api:\s*\n\s*key:\s*(\S+)", f.read(), re.MULTILINE)
        api_key = m.group(1) if m else None
        if not api_key:
            return {"error": "octoprint api key not found"}
        req = urllib.request.Request(OCTOPRINT_URL, headers={"X-Api-Key": api_key})
        with urllib.request.urlopen(req, timeout=3) as resp:
            cur = json.loads(resp.read()).get("current", {})
        return {
            "state": cur.get("state"),
            "port": cur.get("port"),
            "baudrate": cur.get("baudrate"),
            "printer_profile": cur.get("printerProfile"),
        }
    except Exception as e:
        return {"error": str(e)}


def get_throttled():
    raw = run_vcgencmd("get_throttled")
    m = re.search(r"throttled=0x([0-9a-fA-F]+)", raw)
    if not m:
        return {"raw": raw}
    val = int(m.group(1), 16)
    flags = {name: bool(val & (1 << bit)) for bit, name in THROTTLE_BITS.items()}
    return {"raw": hex(val), **flags}


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
    return {
        "under_voltage_now": throttled.get("under_voltage_now"),
        "under_voltage_occurred": throttled.get("under_voltage_occurred"),
        "drop_count": sum(1 for e in events if "Undervoltage detected" in e.get("event", "")),
        "recent_events": events[-10:],
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
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #121212; --fg: #e8e8e8;
      --card-bg: #1e1e1e; --card-border: #333;
      --label: rgba(255,255,255,.55); --updated: rgba(255,255,255,.4);
      --pill-ok-bg: #123a1f; --pill-ok-fg: #7ee08a;
      --pill-bad-bg: #3a1212; --pill-bad-fg: #ff8a8a;
    }
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 420px; margin: 3rem auto; padding: 0 1.25rem;
    color: var(--fg); background: var(--bg);
  }
  h1 { font-size: 1.1rem; font-weight: 600; margin: 0 0 1rem; color: var(--label); }
  .card {
    background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px;
    padding: 1.25rem 1.5rem; margin-bottom: 1rem;
  }
  .metric { display: flex; justify-content: space-between; align-items: baseline; padding: .4rem 0; }
  .metric .label { color: var(--label); font-size: .85rem; }
  .metric .value { color: var(--fg); font-size: 1.4rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .flags { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .5rem; }
  .flags-label { font-size: .85rem; color: var(--label); margin-bottom: .5rem; }
  .pill { font-size: .75rem; padding: .25rem .6rem; border-radius: 999px; }
  .pill.ok { background: var(--pill-ok-bg); color: var(--pill-ok-fg); }
  .pill.bad { background: var(--pill-bad-bg); color: var(--pill-bad-fg); }
  .updated { font-size: .75rem; color: var(--updated); text-align: center; margin-top: .5rem; }
  .event-log { list-style: none; margin: .6rem 0 0; padding: 0; font-size: .78rem; }
  .event-log li { display: flex; justify-content: space-between; gap: .5rem; padding: .3rem 0; border-top: 1px solid var(--card-border); color: var(--label); }
  .event-log li:first-child { border-top: none; }
  .event-log .event-name { color: var(--fg); }
  .no-events { font-size: .8rem; color: var(--label); margin-top: .4rem; }
</style>
</head>
<body>
<h1>skypi3-octopi</h1>
<div class="card">
  <div class="metric"><span class="label">Wi-Fi RSSI</span><span class="value" id="rssi">&mdash;</span></div>
  <div class="metric"><span class="label">Core Voltage</span><span class="value" id="voltage">&mdash;</span></div>
  <div class="metric"><span class="label">CPU Temp</span><span class="value" id="temp">&mdash;</span></div>
</div>
<div class="card">
  <div class="flags-label">Throttle state</div>
  <div class="flags" id="flags"></div>
</div>
<div class="card">
  <div class="metric"><span class="label">Power Supply (5V rail)</span></div>
  <div class="flags" id="psu-flags"></div>
  <div class="flags-label" style="margin-top:.8rem;">Drop events since boot: <span id="psu-count">&mdash;</span></div>
  <ul class="event-log" id="psu-events"></ul>
</div>
<div class="card">
  <div class="metric"><span class="label">Printer Serial Link</span><span class="value" id="printer-state">&mdash;</span></div>
  <div class="metric"><span class="label">Port</span><span class="value" id="printer-port" style="font-size:.95rem;">&mdash;</span></div>
  <div class="metric"><span class="label">Baud</span><span class="value" id="printer-baud" style="font-size:.95rem;">&mdash;</span></div>
</div>
<div class="updated" id="updated">loading&hellip;</div>
<script>
async function refresh() {
  try {
    const r = await fetch('/status');
    const d = await r.json();
    document.getElementById('rssi').textContent = d.rssi_dbm + ' dBm';
    document.getElementById('voltage').textContent = d.voltage + ' V';
    document.getElementById('temp').textContent = d.temp_c + ' °C';
    const flagsEl = document.getElementById('flags');
    flagsEl.innerHTML = '';
    for (const [key, val] of Object.entries(d.throttled)) {
      if (key === 'raw') continue;
      const pill = document.createElement('span');
      pill.className = 'pill ' + (val ? 'bad' : 'ok');
      pill.textContent = key.replaceAll('_', ' ');
      flagsEl.appendChild(pill);
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
    const eventsEl = document.getElementById('psu-events');
    eventsEl.innerHTML = '';
    const events = d.power_supply.recent_events || [];
    if (events.length === 0) {
      eventsEl.innerHTML = '<div class="no-events">No under-voltage events logged since last boot.</div>';
    } else {
      for (const ev of events.slice().reverse()) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="event-name">' + ev.event + '</span><span>' + ev.time + '</span>';
        eventsEl.appendChild(li);
      }
    }
    const pc = d.printer_connection || {};
    const connected = ['Operational', 'Printing', 'Paused'].includes(pc.state);
    const stateEl = document.getElementById('printer-state');
    stateEl.textContent = pc.state || pc.error || 'unknown';
    stateEl.style.color = pc.error ? 'var(--label)' : (connected ? 'var(--pill-ok-fg)' : 'var(--pill-bad-fg)');
    document.getElementById('printer-port').textContent = pc.port || 'none';
    document.getElementById('printer-baud').textContent = pc.baudrate || '—';
    document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById('updated').textContent = 'fetch failed: ' + e;
  }
}
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
        body = json.dumps(
            {
                "rssi_dbm": get_rssi(),
                "voltage": get_voltage(),
                "temp_c": get_temp(),
                "throttled": throttled,
                "power_supply": get_power_supply(throttled),
                "printer_connection": get_printer_connection(),
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
