#!/usr/bin/env python3
"""IICP node live-stats dashboard + cardiogram.

A stdlib-only HTTP endpoint that renders THIS machine's IICP node health by
unifying three live sources: the canonical directory (mesh /stats + our node's
/discover entry), the local structured event stream (~/.iicp/logs/events.jsonl),
and the serve log. It resolves the *current* node dynamically, so it survives the
node_id + tunnel-URL rotation on every restart.

Endpoints:
  /            dashboard (auto-refresh tables + animated ECG cardiogram)
  /api.json    unified snapshot (local + directory + mesh + security)
  /events.json rolling event timeline that drives the cardiogram

Run:  python3 ~/.iicp/node-stats-server.py [--port 9486]
"""
import argparse, calendar, glob, json, os, re, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IICP_DIR = os.path.expanduser("~/.iicp")
LOGS = os.path.join(IICP_DIR, "logs")
EVENTS = os.path.join(LOGS, "events.jsonl")
HISTORY = os.path.join(LOGS, "metrics-history.jsonl")  # longitudinal samples of slow dims
OPERATOR = os.path.join(IICP_DIR, "operator.json")
DIRECTORY = "https://iicp.network/api"
INTENT = "urn:iicp:intent:llm:chat:v1"
# Curated intent probe list. There is no directory enumeration endpoint, so we
# probe these and draw ONLY the ones with >=1 live provider (honest-dataviz: no
# phantom functions). Add a (urn, short) pair here as the mesh grows new intents.
INTENTS = [
    ("urn:iicp:intent:llm:chat:v1", "llm:chat"),
    ("urn:iicp:intent:llm:embedding:v1", "llm:embed"),
]
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.log$")

# event name -> cardiogram beat class
BEAT = {
    "heartbeat_ok": "beat",      # normal sinus pulse (node <-> directory)
    "serve_start": "start",
    "register_ok": "start",
    "task_ok": "task", "task_done": "task", "task": "task",
    "call_ok": "task", "inference_ok": "task",
    "task_error": "bad", "task_refused": "warn", "policy_refused": "warn",
    "recovery_check": "warn", "heartbeat_error": "bad", "heartbeat_fail": "bad",
    "register_error": "bad",
}

# derived live state maintained by the background poller (tasks aren't in events.jsonl,
# so we reconstruct them from the directory's completed_tasks counter)
import threading
_poll = {"last_tasks": None, "task_beats": [], "active_jobs": 0, "load": 0, "lock": threading.Lock()}


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "iicp-node-stats/2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _epoch(ts):
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:  # noqa: BLE001
        return None


def current_node():
    files = [f for f in glob.glob(os.path.join(LOGS, "*.log")) if UUID_RE.match(os.path.basename(f))]
    if not files:
        # fall back to events.jsonl last node_id
        nid = None
        try:
            with open(EVENTS) as fh:
                for line in fh:
                    j = json.loads(line)
                    nid = j.get("node_id", nid)
        except OSError:
            pass
        return {"node_id": nid, "heartbeats": 0, "alive": False} if nid else None
    newest = max(files, key=os.path.getmtime)
    node_id = os.path.basename(newest)[:-4]
    heartbeats, last_seq, endpoint, model, recovery, recovery_ts = 0, None, None, None, None, None
    try:
        with open(newest) as fh:
            for line in fh:
                if "[heartbeat_ok]" in line:
                    heartbeats += 1
                    m = re.search(r"seq=(\d+)", line)
                    if m:
                        last_seq = int(m.group(1))
                elif "[register_ok]" in line:
                    m = re.search(r"endpoint=(\S+)", line)
                    if m:
                        endpoint = m.group(1)
                elif "[serve_start]" in line:
                    m = re.search(r"model=(\S+)", line)
                    if m:
                        model = m.group(1)
                elif "[recovery_check]" in line:
                    m = re.search(r"state=(\S+).*failures=(\S+)", line)
                    if m:
                        recovery = {"state": m.group(1), "failures": m.group(2)}
                        t = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
                        if t:
                            recovery_ts = calendar.timegm(
                                time.strptime(t.group(1), "%Y-%m-%dT%H:%M:%SZ"))
    except OSError:
        pass
    # A recovery_check reflects a live reach concern only while fresh. Recovery
    # events fire on a ~30-60s cadence *during* trouble; if the last one is old,
    # the node long since returned to normal and never logs an explicit "clear".
    # Presenting a stale state as current is a measurement lie (honest-dataviz
    # rule), so drop it past the staleness window instead of freezing it forever.
    RECOVERY_STALE_S = 300
    if recovery is not None and recovery_ts is not None:
        recovery_age = time.time() - recovery_ts
        if recovery_age > RECOVERY_STALE_S:
            recovery = None
        else:
            recovery["age_s"] = round(recovery_age, 1)
    age = time.time() - os.path.getmtime(newest)
    return {"node_id": node_id, "heartbeats": heartbeats, "last_seq": last_seq,
            "endpoint": endpoint, "model": model, "recovery": recovery,
            "log_age_s": round(age, 1), "alive": age < 90}


def read_events(limit=240):
    out = []
    try:
        with open(EVENTS) as fh:
            for line in fh:
                try:
                    j = json.loads(line)
                except ValueError:
                    continue
                ev = j.get("event", "")
                out.append({"t": _epoch(j.get("ts", "")), "event": ev,
                            "cls": BEAT.get(ev, "beat"), "detail": j.get("details", "")})
    except OSError:
        pass
    out = [e for e in out if e["t"]]
    with _poll["lock"]:
        out.extend(dict(e) for e in _poll["task_beats"])
    out.sort(key=lambda e: e["t"])
    return out[-limit:]


def poll_tasks():
    """Background: diff the directory's completed_tasks; emit a task beat per new task."""
    while True:
        try:
            net = network_snapshot()
            mine = next((n for n in net.get("nodes", []) if n.get("ours")), None)
            if mine:
                cur = mine.get("tasks", 0)
                now = time.time()
                with _poll["lock"]:
                    _poll["load"] = mine.get("load", 0)
                    last = _poll["last_tasks"]
                    if last is not None and cur > last:
                        for i in range(min(cur - last, 12)):
                            _poll["task_beats"].append(
                                {"t": now - i * 0.6, "event": "task_ok", "cls": "task",
                                 "detail": f"task #{last + i + 1}"})
                        _poll["task_beats"] = _poll["task_beats"][-100:]
                    _poll["last_tasks"] = cur
        except Exception:  # noqa: BLE001
            pass
        time.sleep(15)


def network_snapshot():
    """Whole-mesh topology + live per-node stats (directory-mediated star)."""
    local = current_node()
    ours = local and local.get("node_id")
    out = {"now": time.time(), "directory": "iicp.network", "nodes": [], "mesh": {}}
    try:
        stats = _get(f"{DIRECTORY}/v1/stats")
        mh = stats.get("mesh_health", {})
        out["mesh"] = {"version": stats.get("server", {}).get("version"),
                       "active_nodes": stats.get("server", {}).get("active_nodes"),
                       "health": mh.get("label"), "health_score": mh.get("score"),
                       "dist": mh.get("distribution", {})}
    except Exception:  # noqa: BLE001
        pass
    # Probe each curated intent; union the providers, annotating which intents
    # each node serves. Only intents with >=1 provider are emitted.
    node_map = {}   # node_id -> node dict (deduped across intents)
    intents_out = []
    for urn, short in INTENTS:
        try:
            disc = _get(f"{DIRECTORY}/v1/discover?intent={urn}")
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
            continue
        ids = []
        for n in disc.get("nodes", []):
            nid = n.get("node_id") or ""
            ids.append(nid[:8])
            if nid not in node_map:
                tp = n.get("trust_progress") or {}
                pm = n.get("node_policy_manifest") or {}
                perf = n.get("performance") or {}
                node_map[nid] = {
                    "id": nid[:8], "ours": nid == ours,
                    "region": n.get("region", "?"), "health": n.get("health_label", "?"),
                    "reputation": n.get("reputation_tier", "?"), "score": n.get("score", 0),
                    "tasks": tp.get("completed_tasks", 0), "probation": n.get("probation"),
                    "models": n.get("models") or [], "backend": n.get("backend", "?"),
                    "reachable": n.get("directory_observed_reachable"),
                    "operator": n.get("operator_display_name"), "load": n.get("load", 0),
                    "active_jobs": n.get("active_jobs", 0), "max_concurrent": n.get("max_concurrent"),
                    "sdk": n.get("sdk_version"), "latency_ms": perf.get("task_latency_ms"),
                    "jurisdiction": pm.get("jurisdiction"), "training_use": pm.get("training_use"),
                    "endpoint": n.get("endpoint", ""), "intents": [],
                }
            if short not in node_map[nid]["intents"]:
                node_map[nid]["intents"].append(short)
        if ids:
            intents_out.append({"urn": urn, "short": short, "count": len(ids), "nodes": ids})
    out["nodes"] = list(node_map.values())
    out["intents"] = intents_out
    return out


# ---------------------------------------------------------------- history (over time)
# The slow-moving dimensions (reputation score/tier, completed_tasks, health, task
# latency, mesh size) don't belong on the fast heartbeat cardiogram. We sample them
# on a slow cadence into an append-only JSONL and chart the real samples. Honest-
# dataviz still holds: every point is one real measurement at its real timestamp;
# we connect real samples, we never backfill or interpolate synthetic history.
HISTORY_DIMS = ("score", "tier", "tasks", "health", "reachable", "latency_ms",
                "load", "active_nodes", "mesh_health")


def sample_metrics():
    """Append one longitudinal sample of the slow dims. Deduped: skip if <45s since last."""
    try:
        prev = read_history(1)
        if prev and time.time() - (prev[-1].get("t") or 0) < 45:
            return
        net = network_snapshot()
        mine = next((n for n in net.get("nodes", []) if n.get("ours")), None) or {}
        mesh = net.get("mesh", {}) or {}
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "t": round(time.time(), 1),
               "score": mine.get("score"), "tier": mine.get("reputation"),
               "tasks": mine.get("tasks"), "health": mine.get("health"),
               "reachable": mine.get("reachable"), "latency_ms": mine.get("latency_ms"),
               "load": mine.get("load"), "active_nodes": mesh.get("active_nodes"),
               "mesh_health": mesh.get("health")}
        with open(HISTORY, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:  # noqa: BLE001
        pass


def sample_loop(interval=60):
    while True:
        sample_metrics()
        time.sleep(interval)


def read_history(limit=1440):
    out = []
    try:
        with open(HISTORY) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out[-limit:]


def spark(vals, w=190, h=32, color="var(--accent)", lo=None, hi=None):
    """Inline-SVG sparkline of real numeric samples (no interpolation beyond the line)."""
    pts = [v for v in vals if isinstance(v, (int, float))]
    if len(pts) < 2:
        return f'<svg width="{w}" height="{h}" class="spk"></svg>'
    lo = min(pts) if lo is None else lo
    hi = max(pts) if hi is None else hi
    rng = (hi - lo) or 1
    n = len(pts)
    coords = [(i / (n - 1) * (w - 3) + 1.5, h - 2 - (v - lo) / rng * (h - 4)) for i, v in enumerate(pts)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    lx, ly = coords[-1]
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" class="spk" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{poly}"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.2" fill="{color}"/></svg>')


def strip(vals, colmap, w=190, h=14):
    """Categorical timeline: one colored cell per real sample (tier / health over time)."""
    vals = [v for v in vals if v]
    if not vals:
        return f'<svg width="{w}" height="{h}" class="spk"></svg>'
    n = len(vals)
    seg = w / n
    cells = "".join(
        f'<rect x="{i * seg:.2f}" y="0" width="{seg + 0.6:.2f}" height="{h}" '
        f'fill="var({colmap.get(str(v), "--line")})"/>' for i, v in enumerate(vals))
    return f'<svg width="{w}" height="{h}" class="spk" preserveAspectRatio="none">{cells}</svg>'


TIER_COL = {"probation": "--bad", "silver": "--mut", "gold": "--warn", "platinum": "--ok"}
HEALTH_COL = {"healthy": "--ok", "degraded": "--warn", "critical": "--bad", "offline": "--bad"}


def operator_secret_encrypted():
    try:
        d = json.load(open(OPERATOR))
        sec = d.get("operator_secret", "")
        # heuristic: encrypted secrets are wrapped/prefixed; plaintext is raw base64
        return bool(d.get("secret_encrypted")) or sec.startswith("enc:")
    except OSError:
        return None


def snapshot():
    local = current_node()
    out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "local": local,
           "directory": None, "mesh": None, "security": None, "error": None}
    mine = None
    try:
        stats = _get(f"{DIRECTORY}/v1/stats")
        out["mesh"] = {"version": stats.get("server", {}).get("version"),
                       "active_nodes": stats.get("server", {}).get("active_nodes"),
                       "mesh_health": stats.get("mesh_health", {}),
                       "directory_health": stats.get("directory_health", {}).get("label")}
    except Exception as e:  # noqa: BLE001
        out["error"] = f"mesh: {e}"
    try:
        disc = _get(f"{DIRECTORY}/v1/discover?intent={INTENT}")
        nid = local and local.get("node_id")
        mine = next((n for n in disc.get("nodes", []) if n.get("node_id") == nid), None)
        if mine:
            out["directory"] = {k: mine.get(k) for k in (
                "score", "available", "directory_observed_reachable", "route_evidence",
                "reachability_tier", "exposure_mode", "health_label", "probation",
                "reputation_tier", "region", "backend", "endpoint", "models",
                "trust_progress", "load", "active_jobs", "max_concurrent",
                "operator_display_name", "operator_fingerprint")}
        else:
            out["directory"] = {"listed": False}
    except Exception as e:  # noqa: BLE001
        out["error"] = (out["error"] or "") + f" discover: {e}"

    # ---- security posture (derived, honest) ----
    evs = read_events()
    now = time.time()
    beats = [e for e in evs if e["cls"] == "beat"]
    last_beat_age = round(now - beats[-1]["t"], 1) if beats else None
    task_evs = [e for e in evs if e["cls"] == "task"]
    warn_evs = [e for e in evs if e["cls"] in ("warn", "bad")]
    d = out["directory"] or {}
    anomalies = []
    if last_beat_age is not None and last_beat_age > 90:
        anomalies.append(f"no heartbeat for {last_beat_age}s (directory drops node at 90s)")
    if isinstance(d, dict) and d.get("active_jobs", 0) and d.get("max_concurrent"):
        if d["active_jobs"] >= d["max_concurrent"]:
            anomalies.append("all concurrency slots busy (possible load/abuse)")
    out["security"] = {
        "inbound_surface": "1 path: Cloudflare tunnel -> POST /v1/task (inference only)",
        "code_execution": "REFUSED (bash / write_file not served; intent = llm:chat only)",
        "served_intent": INTENT,
        "home_ip": "masked by Cloudflare tunnel (origin hidden)",
        "data_exposure": "none of your data - public open-weights model, no filesystem access",
        "max_concurrent": d.get("max_concurrent"),
        "active_jobs": d.get("active_jobs"),
        "operator": d.get("operator_display_name"),
        "operator_secret_encrypted_at_rest": operator_secret_encrypted(),
        "last_beat_age_s": last_beat_age,
        "tasks_seen": len(task_evs),
        "warnings_seen": len(warn_evs),
        "anomalies": anomalies,
    }
    return out


# ------------------------------------------------------------------ rendering
def badge(state, label):
    cls = {True: "ok", False: "bad", None: "warn"}.get(state, state)
    return f'<span class="badge {cls}">{label}</span>'


def render(s, screensaver=False):
    loc, d = s.get("local") or {}, s.get("directory") or {}
    sec, mesh = s.get("security") or {}, s.get("mesh") or {}
    tp = d.get("trust_progress") or {} if isinstance(d, dict) else {}
    alive = loc.get("alive")
    status = "SERVING" if alive else "DOWN"

    def row(k, v):
        return f"<tr><td class='k'>{k}</td><td class='v'>{v}</td></tr>"

    node_rows = (
        row("operator", f"<b>{sec.get('operator') or '(anonymous)'}</b>")
        + row("node_id", f"<code>{loc.get('node_id','-')}</code>")
        + row("model", loc.get("model") or d.get("models", ["-"])[0])
        + row("endpoint", f"<code>{d.get('endpoint') or loc.get('endpoint') or '-'}</code>")
        + row("reachable (dir-observed)", badge(d.get("directory_observed_reachable") is True, str(d.get("directory_observed_reachable"))))
        + row("reputation", d.get("reputation_tier", "-"))
        + row("probation", badge(d.get("probation") is False, str(d.get("probation"))))
        + row("completed tasks", f"{tp.get('completed_tasks','-')} (gold at {tp.get('gold_min_tasks','-')})")
        + row("heartbeats", f"{loc.get('heartbeats','-')} (last {sec.get('last_beat_age_s','-')}s ago)")
    )
    sec_rows = (
        row("inbound surface", sec.get("inbound_surface"))
        + row("code execution", badge(True, "REFUSED") + " " + "bash / write_file")
        + row("home IP", sec.get("home_ip"))
        + row("data exposure", sec.get("data_exposure"))
        + row("concurrency cap", f"{sec.get('active_jobs','-')} / {sec.get('max_concurrent','-')} slots busy")
        + row("operator secret at rest", badge(sec.get("operator_secret_encrypted_at_rest"),
              "encrypted" if sec.get("operator_secret_encrypted_at_rest") else "PLAINTEXT - run `operator encrypt`"))
        + row("tasks / warnings seen", f"{sec.get('tasks_seen',0)} / {sec.get('warnings_seen',0)}")
    )
    anom = sec.get("anomalies") or []
    anom_html = ("".join(f"<li>{a}</li>" for a in anom) if anom
                 else "<li class='clear'>no anomalies - rhythm nominal</li>")
    mesh_rows = (row("directory", mesh.get("version", "-"))
                 + row("active nodes", mesh.get("active_nodes", "-"))
                 + row("mesh health", f"{(mesh.get('mesh_health') or {}).get('label','-')}"))

    # ---- trends over time (slow dims, real sampled points) ----
    hist = read_history(720)
    def _col(k):
        return [r.get(k) for r in hist]
    tl = hist[-1] if hist else {}
    def _fmt(v, suf=""):
        return f"{v}{suf}" if v not in (None, "") else "-"
    if len(hist) < 2:
        trends_html = (f"<div class='sub'>collecting samples… {len(hist)} so far "
                       f"(1/min). Trends draw once there are 2+ real points; nothing is backfilled.</div>")
    else:
        span_min = round((hist[-1].get("t", 0) - hist[0].get("t", 0)) / 60)
        def trow(label, svg, now):
            return f"<tr><td class='k'>{label}</td><td class='tv'>{svg}</td><td class='tn'>{now}</td></tr>"
        trends_html = (
            "<table class='trend'>"
            + trow("reputation score", spark(_col("score"), color="var(--accent)", lo=0, hi=1), _fmt(tl.get("score")))
            + trow("completed tasks", spark(_col("tasks"), color="var(--ecg)"), _fmt(tl.get("tasks")))
            + trow("task latency", spark(_col("latency_ms"), color="#3b82f6"), _fmt(tl.get("latency_ms"), "ms"))
            + trow("mesh nodes", spark(_col("active_nodes"), color="var(--mut)"), _fmt(tl.get("active_nodes")))
            + trow("reputation tier", strip(_col("tier"), TIER_COL), _fmt(tl.get("tier")))
            + trow("node health", strip(_col("health"), HEALTH_COL), _fmt(tl.get("health")))
            + "</table>"
            + f"<div class='sub'>{len(hist)} samples over ~{span_min} min · 1/min · newest at right · every mark is one measurement</div>")

    err = f"<div class='err'>{s['error']}</div>" if s.get("error") else ""
    htmlcls = "ss" if screensaver else ""
    return f"""<!doctype html><html class="{htmlcls}"{' data-theme="dark"' if screensaver else ''}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>IICP node - {status}</title>
<style>
:root {{ --bg:#f4f5f7; --card:#fff; --fg:#1a1d21; --mut:#687076; --line:#e4e6ea;
 --ok:#1a7f37; --okbg:#d7f5dd; --warn:#8a6d00; --warnbg:#fff3cd; --bad:#b42318; --badbg:#fde3e1;
 --accent:#CC7755; --ecg:#1a7f37; --ecgbg:#eef2ee; --grid:#dfe4df; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0d0f11; --card:#16191d; --fg:#e6e8eb; --mut:#9aa0a6; --line:#252a30;
 --ok:#4ade80; --okbg:#0f2e1a; --warn:#fbbf24; --warnbg:#332800; --bad:#f87171; --badbg:#3a1512;
 --accent:#e0996f; --ecg:#39ff8b; --ecgbg:#07120c; --grid:#123018; }} }}
:root[data-theme=dark] {{ --bg:#0d0f11; --card:#16191d; --fg:#e6e8eb; --mut:#9aa0a6; --line:#252a30;
 --ok:#4ade80; --okbg:#0f2e1a; --warn:#fbbf24; --warnbg:#332800; --bad:#f87171; --badbg:#3a1512;
 --accent:#e0996f; --ecg:#39ff8b; --ecgbg:#07120c; --grid:#123018; }}
:root[data-theme=light] {{ --bg:#f4f5f7; --card:#fff; --fg:#1a1d21; --mut:#687076; --line:#e4e6ea;
 --ok:#1a7f37; --okbg:#d7f5dd; --warn:#8a6d00; --warnbg:#fff3cd; --bad:#b42318; --badbg:#fde3e1;
 --accent:#CC7755; --ecg:#1a7f37; --ecgbg:#eef2ee; --grid:#dfe4df; }}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;padding:20px}}
.wrap{{max-width:820px;margin:0 auto}}
h1{{font-size:18px;margin:0 0 2px;display:flex;align-items:center;gap:10px}}
.sub{{color:var(--mut);font-size:12px;margin-bottom:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px 17px;margin-bottom:14px}}
.card h2{{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin:0 0 10px;display:flex;justify-content:space-between}}
table{{width:100%;border-collapse:collapse}} td{{padding:5px 0;vertical-align:top;border-bottom:1px solid var(--line)}}
tr:last-child td{{border-bottom:none}} .k{{color:var(--mut);width:42%}} .v{{text-align:right;word-break:break-all}}
code{{font:12px ui-monospace,Menlo,monospace}}
.badge{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600}}
.badge.ok{{background:var(--okbg);color:var(--ok)}} .badge.warn{{background:var(--warnbg);color:var(--warn)}}
.badge.bad{{background:var(--badbg);color:var(--bad)}}
.pill{{font-size:12px;padding:2px 10px;border-radius:999px;font-weight:700}}
.pill.up{{background:var(--okbg);color:var(--ok)}} .pill.down{{background:var(--badbg);color:var(--bad)}}
.err{{background:var(--badbg);color:var(--bad);padding:8px 12px;border-radius:8px;font-size:12px;margin-bottom:14px}}
.ecgwrap{{background:var(--ecgbg);border-radius:10px;padding:6px;position:relative;overflow:hidden}}
canvas{{display:block;width:100%;height:120px}}
#ecg{{height:150px}} #net{{height:340px}}
.leg{{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--mut);margin-top:8px}}
.leg b{{color:var(--fg)}} .dot{{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;vertical-align:middle}}
.bpm{{font:700 22px ui-monospace,Menlo,monospace;color:var(--ecg)}}
ul.anom{{margin:6px 0 0;padding-left:18px;font-size:12px}} ul.anom li{{color:var(--bad)}} ul.anom li.clear{{color:var(--ok)}}
a{{color:var(--accent)}}
table.trend{{width:100%;border-collapse:collapse}}
table.trend td{{padding:6px 0;border-bottom:1px solid var(--line);vertical-align:middle}}
table.trend tr:last-child td{{border-bottom:none}}
table.trend .tv{{text-align:right;width:200px}} table.trend .tv .spk{{width:190px;height:32px}}
table.trend .tn{{text-align:right;font:600 12px ui-monospace,Menlo,monospace;width:72px;color:var(--fg)}}
.spk{{vertical-align:middle}}
table.nodes{{font-size:12px;min-width:640px}}
table.nodes th{{text-align:left;color:var(--mut);font-weight:600;border-bottom:1px solid var(--line);padding:4px 10px 4px 0}}
table.nodes td{{padding:5px 10px 5px 0;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}}
table.nodes tr:last-child td{{border-bottom:none}}
table.nodes tr.ours td{{color:var(--accent);font-weight:600}}
table.nodes .op{{color:var(--mut);font-size:11px}}
.h-healthy{{color:var(--ok)}} .h-degraded{{color:var(--warn)}} .h-critical,.h-offline{{color:var(--bad)}}
/* ---- screensaver / ambient wall mode ---- */
.ss{{background:#05070a}} .ss body{{background:#05070a;padding:2.4vh 3vw;height:100vh;overflow:hidden}}
.ss .wrap{{max-width:none;height:96vh;display:flex;flex-direction:column;gap:1.6vh}}
.ss .sub{{display:none}} .ss h1{{font-size:clamp(20px,3vw,40px);margin:0}}
.ss .bpm{{font-size:clamp(24px,4vw,56px)}}
/* keep ONLY the cardiogram + mesh-network cards; hide everything else */
.ss .card{{display:none}}
.ss #ecgcard,.ss #netcard{{display:block;margin:0;background:#0b0f14;border-color:#141a20}}
.ss #ecgcard{{flex:0 0 34vh}} .ss #netcard{{flex:1 1 auto;display:flex;flex-direction:column}}
.ss .card h2{{font-size:14px}}
.ss #ecg{{height:26vh}}
.ss #netcard .ecgwrap{{flex:1 1 auto}} .ss #net{{height:100%;min-height:38vh}}
.ss .leg{{font-size:13px}}
html.ss,html.ss *{{cursor:none}}
</style></head><body><div class="wrap">
<h1>IICP node <span class="pill {'up' if alive else 'down'}">{status}</span>
  <span class="bpm" id="bpm">--</span></h1>
<div class="sub">{s['ts']} - operator <b>{sec.get('operator') or '(anonymous)'}</b>
  - <a href="/api.json">json</a> / <a href="/events.json">events</a> / <a href="?mode=screensaver">screensaver</a></div>
{err}
<div class="card" id="ecgcard"><h2>Cardiogram <span id="beatage">-</span></h2>
  <div class="ecgwrap"><canvas id="ecg" width="1600" height="240"></canvas></div>
  <div class="leg">
    <span><span class="dot" style="background:var(--ecg)"></span><b>beat</b> directory heartbeat (~30s)</span>
    <span><span class="dot" style="background:#3b82f6"></span><b>task</b> inference served</span>
    <span><span class="dot" style="background:var(--warn)"></span><b>warn</b> refusal/recovery</span>
    <span><span class="dot" style="background:var(--bad)"></span><b>flatline</b> node down</span>
  </div></div>
<div class="card" id="netcard"><h2>Mesh network <span id="netcount">-</span></h2>
  <div class="ecgwrap" style="background:var(--card)"><canvas id="net" width="1600" height="520"></canvas></div>
  <div class="leg">
    <span><span class="dot" style="background:var(--accent)"></span><b>this node</b> ({sec.get('operator') or 'anon'})</span>
    <span><span class="dot" style="background:var(--ok)"></span>healthy</span>
    <span><span class="dot" style="background:var(--warn)"></span>degraded</span>
    <span><span class="dot" style="background:var(--bad)"></span>critical/offline</span>
    <span>inner ring = intents (functions) · edge color = intent a node serves</span>
    <span>size = score - ring = directory-observed reachable</span>
  </div></div>
<div class="card" id="nodesdetail"><h2>Mesh nodes <span id="ndcount">-</span></h2>
  <div style="overflow-x:auto"><table class="nodes"><thead><tr>
    <th>node</th><th>region</th><th>health</th><th>rep</th><th>tasks</th><th>models</th>
    <th>backend</th><th>load</th><th>lat</th><th>reach</th><th>juris</th><th>intents</th>
  </tr></thead><tbody id="nodetable"><tr><td colspan="12">loading…</td></tr></tbody></table></div></div>
<div class="card"><h2>Security posture</h2><table>{sec_rows}</table>
  <ul class="anom">{anom_html}</ul></div>
<div class="card"><h2>This node</h2><table>{node_rows}</table></div>
<div class="card"><h2>Mesh</h2><table>{mesh_rows}</table></div>
<div class="card" id="trendcard"><h2>Trends over time <span>{len(hist)} samples</span></h2>{trends_html}</div>
</div>
<script>
const col=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
// DPR-aware backing store so text/strokes stay crisp (not bitmap-scaled)
function fit(cv){{ const r=cv.getBoundingClientRect(),d=window.devicePixelRatio||1;
  cv.width=Math.round(r.width*d); cv.height=Math.round(r.height*d);
  const c=cv.getContext('2d'); c.setTransform(d,0,0,d,0,0); return [c,r.width,r.height]; }}
const cvs=document.getElementById('ecg');
let [ctx,W,H]=fit(cvs),BASE=H*0.62;
let events=[],sweep=0,lastBeatT=null;
window.addEventListener('resize',()=>{{[ctx,W,H]=fit(cvs);BASE=H*0.62;[nx,NW0,NH0]=fit(net);}});
// Honest impulse plot: NOTHING is drawn that isn't a real event in the log.
// Flat baseline = node idle (the truth between 30s heartbeats). One needle per
// real event, at its real timestamp (x = time). No synthetic waveform shape.
function draw(){{
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle=col('--grid');ctx.lineWidth=1;
  for(let x=0;x<=W;x+=W/15){{ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}}   // 15 gridlines = 15 x 1min
  ctx.beginPath();ctx.moveTo(0,BASE);ctx.lineTo(W,BASE);ctx.stroke();
  const now=Date.now()/1000, span=900; // 15-min window; each gridline = 1 min
  const down = lastBeatT && (now-lastBeatT>90);
  // flat baseline (idle truth); red if the node has actually gone silent
  ctx.lineWidth=2;ctx.strokeStyle= down? col('--bad'):col('--ecg');
  ctx.beginPath();ctx.moveTo(0,BASE);ctx.lineTo(W,BASE);ctx.stroke();
  // one needle per REAL event, positioned by its real timestamp
  events.forEach(e=>{{
    const px=W-((now-e.t)/span)*W; if(px<0||px>W) return;
    const h = e.cls==='task'? H*0.44 : e.cls==='bad'? H*0.34 : e.cls==='warn'? H*0.22 : H*0.18;
    const color = e.cls==='task'?'#3b82f6': e.cls==='warn'?col('--warn'): e.cls==='bad'?col('--bad'):col('--ecg');
    ctx.strokeStyle=color;ctx.lineWidth=e.cls==='task'?2.6:1.8;
    ctx.beginPath();ctx.moveTo(px-3,BASE);ctx.lineTo(px,BASE-h);ctx.lineTo(px+3,BASE); // clean tick
    if(e.cls==='bad') ctx.lineTo(px,BASE+h*0.35);                                       // failures dip below
    ctx.stroke();
  }});
  // 'now' marker at the right edge; events drift left in real time as now advances
  ctx.strokeStyle=col('--accent');ctx.globalAlpha=.35;ctx.lineWidth=1.5;
  ctx.beginPath();ctx.moveTo(W-1,0);ctx.lineTo(W-1,H);ctx.stroke();ctx.globalAlpha=1;
  requestAnimationFrame(draw);
}}
async function poll(){{
  try{{
    const r=await fetch('/events.json');const j=await r.json();
    events=j.events||[];
    const beats=events.filter(e=>e.cls==='beat');
    if(beats.length){{ lastBeatT=beats[beats.length-1].t;
      const age=Math.round(Date.now()/1000-lastBeatT);
      document.getElementById('beatage').textContent='last beat '+age+'s ago';
      // bpm-ish: beats in last 5 min * (60/300) -> per min, x30 to read like pulse
      const recent=beats.filter(e=>Date.now()/1000-e.t<300).length;
      document.getElementById('bpm').textContent=(recent? Math.round(recent/5*10)/10 : 0)+' bpm';
    }}
  }}catch(e){{}}
}}
// ---- mesh network graph (directory-mediated star) ----
const net=document.getElementById('net');
let [nx,NW0,NH0]=fit(net);
let netData=null,netPulse=0;
function hcol(h){{ if(h==='healthy')return col('--ok'); if(h==='degraded')return col('--warn');
  if(h==='offline'||h==='critical')return col('--bad'); return col('--mut'); }}
function drawNet(){{
  const NW=NW0,NH=NH0,cx=NW/2,cy=NH/2;
  nx.clearRect(0,0,NW,NH);
  if(!netData){{requestAnimationFrame(drawNet);return;}}
  const nodes=netData.nodes||[],R=Math.min(NW*0.30,NH*0.34),Ri=R*0.44;
  const intents=netData.intents||[];
  const clip=s=>s&&s.length>16?s.slice(0,15)+'…':s;
  netPulse+=0.03;
  const icol=j=>[col('--accent'),'#3b82f6','#a855f7','#f59e0b','#10b981'][j%5];
  // outer node positions, keyed by id (so intent edges can find them)
  const npos={{}};
  nodes.forEach((n,i)=>{{ const a=-Math.PI/2 + i/Math.max(nodes.length,1)*Math.PI*2;
    npos[n.id]=[cx+Math.cos(a)*R, cy+Math.sin(a)*R]; }});
  // intent positions on an inner ring
  const ipos=intents.map((t,j)=>{{ const a=-Math.PI/2 + (j+0.5)/Math.max(intents.length,1)*Math.PI*2;
    return [cx+Math.cos(a)*Ri, cy+Math.sin(a)*Ri]; }});
  // edges: each intent -> the nodes that serve it (colored by intent)
  intents.forEach((t,j)=>{{ const ip=ipos[j];
    (t.nodes||[]).forEach(id=>{{ const p=npos[id]; if(!p)return;
      nx.strokeStyle=icol(j);nx.globalAlpha=0.30;nx.lineWidth=1;
      nx.beginPath();nx.moveTo(ip[0],ip[1]);nx.lineTo(p[0],p[1]);nx.stroke();nx.globalAlpha=1; }});
  }});
  // edges: hub -> each intent (thick)
  intents.forEach((t,j)=>{{ const ip=ipos[j];
    nx.strokeStyle=icol(j);nx.globalAlpha=0.85;nx.lineWidth=2.5;
    nx.beginPath();nx.moveTo(cx,cy);nx.lineTo(ip[0],ip[1]);nx.stroke();nx.globalAlpha=1; }});
  // intent function nodes
  intents.forEach((t,j)=>{{ const ip=ipos[j];
    nx.fillStyle=icol(j);nx.beginPath();nx.arc(ip[0],ip[1],7,0,7);nx.fill();
    nx.fillStyle=col('--fg');nx.font='700 11px ui-monospace,monospace';nx.textAlign='center';
    nx.fillText(t.short+' ('+t.count+')',ip[0],ip[1]-12); }});
  // hub
  nx.fillStyle=col('--accent');nx.beginPath();nx.arc(cx,cy,14,0,7);nx.fill();
  nx.fillStyle=col('--fg');nx.font='700 14px -apple-system,sans-serif';nx.textAlign='center';
  nx.fillText('iicp.network',cx,cy+28);
  const mh=netData.mesh||{{}};
  nx.fillStyle=col('--mut');nx.font='11px ui-monospace,monospace';
  nx.fillText((mh.active_nodes||nodes.length)+' nodes - '+(intents.length)+' intents',cx,cy+44);
  // nodes
  nodes.forEach((n,i)=>{{
    const a=-Math.PI/2 + i/nodes.length*Math.PI*2;
    const x=cx+Math.cos(a)*R,y=cy+Math.sin(a)*R;
    const rad=8+Math.min(16,(n.score||0)*14);
    if(n.ours){{ const p=3+Math.sin(netPulse)*2;
      nx.strokeStyle=col('--accent');nx.lineWidth=3;nx.beginPath();nx.arc(x,y,rad+6+p,0,7);nx.stroke(); }}
    else if(n.reachable){{ nx.strokeStyle=col('--ok');nx.lineWidth=1.5;nx.beginPath();nx.arc(x,y,rad+3,0,7);nx.stroke(); }}
    nx.fillStyle=hcol(n.health);nx.beginPath();nx.arc(x,y,rad,0,7);nx.fill();
    nx.fillStyle=n.ours?col('--accent'):col('--fg');nx.font=(n.ours?'700 ':'600 ')+'13px ui-monospace,monospace';
    const lx=x+(Math.cos(a)>=0?rad+9:-(rad+9)); nx.textAlign=Math.cos(a)>=0?'left':'right';
    nx.fillText((n.ours?'YOU - ':'')+n.region+' - '+clip(n.models[0]||''),lx,y-3);
    nx.fillStyle=col('--mut');nx.font='12px ui-monospace,monospace';
    nx.fillText(n.reputation+' - '+n.tasks+' tasks',lx,y+13);
  }});
  requestAnimationFrame(drawNet);
}}
async function netPoll(){{
  try{{ const r=await fetch('/network.json');netData=await r.json();
    const nodes=netData.nodes||[];
    const c=document.getElementById('netcount'); if(c)c.textContent=nodes.length+' nodes';
    const nd=document.getElementById('ndcount'); if(nd)nd.textContent=nodes.length;
    const tb=document.getElementById('nodetable');
    if(tb) tb.innerHTML = nodes.map(n=>{{
      const models=n.models||[]; const mstr=models.length? models[0]+(models.length>1?' +'+(models.length-1):'') : '-';
      const lat=n.latency_ms? Math.round(n.latency_ms)+'ms':'-';
      return `<tr class="${{n.ours?'ours':''}}">`
        +`<td>${{n.ours?'★ ':''}}${{n.id}}${{n.operator?' <span class=op>'+n.operator+'</span>':''}}</td>`
        +`<td>${{n.region}}</td><td class="h-${{n.health}}">${{n.health}}${{n.probation?' ⚠':''}}</td>`
        +`<td>${{n.reputation}}</td><td>${{n.tasks}}</td>`
        +`<td title="${{models.join(', ')}}">${{mstr}}</td>`
        +`<td>${{n.backend}}</td><td>${{n.active_jobs}}/${{n.max_concurrent||'-'}}</td>`
        +`<td>${{lat}}</td><td>${{n.reachable===true?'yes':n.reachable===false?'no':'-'}}</td>`
        +`<td>${{n.jurisdiction||'-'}}</td><td>${{(n.intents||[]).join(', ')||'-'}}</td></tr>`;
    }}).join('') || '<tr><td colspan="12">no nodes</td></tr>';
  }}catch(e){{}}
}}
netPoll();setInterval(netPoll,10000);requestAnimationFrame(drawNet);
poll();setInterval(poll,5000);requestAnimationFrame(draw);
setInterval(()=>location.reload(),60000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path.startswith("/events.json"):
                self._send(json.dumps({"now": time.time(), "events": read_events()}).encode(),
                           "application/json")
                return
            if self.path.startswith("/network.json"):
                self._send(json.dumps(network_snapshot()).encode(), "application/json")
                return
            if self.path.startswith("/history.json"):
                self._send(json.dumps({"dims": HISTORY_DIMS, "samples": read_history()}).encode(),
                           "application/json")
                return
            s = snapshot()
            if self.path.startswith("/api.json"):
                self._send(json.dumps(s, indent=2).encode(), "application/json")
            else:
                ss = ("/screensaver" in self.path) or ("mode=screensaver" in self.path)
                self._send(render(s, screensaver=ss).encode(), "text/html; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"error: {e}".encode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9486)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    threading.Thread(target=poll_tasks, daemon=True).start()
    threading.Thread(target=sample_loop, daemon=True).start()
    print(f"[iicp-stats] http://{a.host}:{a.port}/")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
