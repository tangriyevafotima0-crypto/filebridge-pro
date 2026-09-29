#!/usr/bin/env python3
"""
FileBridge Pro - Fayl menejeri + Kompyuter monitoring
Ishga tushirish: python filebridge_pro.py
Ikkinchi kompyuterdan: http://[IP]:8888
"""

import os, sys, json, shutil, threading, time, sqlite3, socket, re
import mimetypes, hashlib, gzip, io
from pathlib import Path
from datetime import datetime, date, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from collections import defaultdict

# ==================== SOZLAMALAR ====================
PORT       = 8888
PASSWORD   = "1234"
ROOT_DIR   = os.path.expanduser("~")
DB_PATH    = os.path.join(os.path.expanduser("~"), ".filebridge_stats.db")
# ====================================================

IS_WINDOWS = sys.platform == "win32"
IS_MAC     = sys.platform == "darwin"

# ──────────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS app_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, app TEXT, title TEXT, duration INTEGER
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS daily_total (
            day TEXT PRIMARY KEY, seconds INTEGER DEFAULT 0
        )""")
    con.commit(); con.close()

def db_log_app(app, title, duration):
    try:
        con = sqlite3.connect(DB_PATH)
        ts = datetime.now().isoformat()
        con.execute("INSERT INTO app_usage (ts,app,title,duration) VALUES (?,?,?,?)",
                    (ts, app, title, duration))
        day = date.today().isoformat()
        con.execute("INSERT INTO daily_total(day,seconds) VALUES(?,?) ON CONFLICT(day) DO UPDATE SET seconds=seconds+?",
                    (day, duration, duration))
        con.commit(); con.close()
    except: pass

def db_query(sql, params=()):
    try:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        con.close()
        return [dict(r) for r in rows]
    except: return []

# ──────────────────────────────────────────────────────────────
# ACTIVE WINDOW TRACKER
# ──────────────────────────────────────────────────────────────
_last_app   = ""
_last_title = ""
_last_ts    = time.time()
_tracking   = True

def get_active_window():
    try:
        if IS_WINDOWS:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            # Exe nomi
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            h = ctypes.windll.kernel32.OpenProcess(0x0400|0x0010, False, pid.value)
            buf2 = ctypes.create_unicode_buffer(512)
            ctypes.windll.psapi.GetModuleFileNameExW(h, 0, buf2, 512)
            ctypes.windll.kernel32.CloseHandle(h)
            exe = os.path.basename(buf2.value) if buf2.value else "Unknown"
            return exe, title

        elif IS_MAC:
            from subprocess import check_output
            script = 'tell application "System Events" to get name of first application process whose frontmost is true'
            app = check_output(['osascript','-e',script]).decode().strip()
            return app, app

        else:  # Linux
            from subprocess import check_output
            wid = check_output(['xdotool','getactivewindow']).decode().strip()
            title = check_output(['xdotool','getwindowname',wid]).decode().strip()
            pid = check_output(['xdotool','getwindowpid',wid]).decode().strip()
            with open(f'/proc/{pid}/comm') as f: app = f.read().strip()
            return app, title
    except:
        return "Unknown", ""

def tracker_loop():
    global _last_app, _last_title, _last_ts
    while _tracking:
        app, title = get_active_window()
        now = time.time()
        dur = int(now - _last_ts)
        if dur >= 2 and _last_app:
            db_log_app(_last_app, _last_title, dur)
        _last_app, _last_title, _last_ts = app, title, now
        time.sleep(3)

# ──────────────────────────────────────────────────────────────
# BROWSER HISTORY
# ──────────────────────────────────────────────────────────────
def get_browser_history(days=7, limit=200):
    results = []
    cutoff = datetime.now() - timedelta(days=days)

    # Chrome / Edge paths
    chrome_paths = []
    if IS_WINDOWS:
        base = os.path.expandvars("%LOCALAPPDATA%")
        chrome_paths += [
            os.path.join(base, "Google","Chrome","User Data","Default","History"),
            os.path.join(base, "Microsoft","Edge","User Data","Default","History"),
            os.path.join(base, "BraveSoftware","Brave-Browser","User Data","Default","History"),
        ]
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
        chrome_paths += [
            os.path.join(base, "Google","Chrome","Default","History"),
            os.path.join(base, "Microsoft Edge","Default","History"),
        ]
    else:
        chrome_paths += [
            os.path.expanduser("~/.config/google-chrome/Default/History"),
            os.path.expanduser("~/.config/chromium/Default/History"),
            os.path.expanduser("~/.config/microsoft-edge/Default/History"),
        ]

    for p in chrome_paths:
        if not os.path.exists(p): continue
        tmp = p + ".tmp_fb"
        try:
            shutil.copy2(p, tmp)
            con = sqlite3.connect(tmp)
            # Chrome epoch: microseconds from 1601-01-01
            epoch_diff = 11644473600
            rows = con.execute("""
                SELECT url, title,
                  datetime(last_visit_time/1000000 - ?, 'unixepoch') as visit_time,
                  visit_count
                FROM urls
                WHERE last_visit_time > (? + ?) * 1000000
                ORDER BY last_visit_time DESC LIMIT ?
            """, (epoch_diff, int(cutoff.timestamp()), epoch_diff, limit)).fetchall()
            for r in rows:
                results.append({
                    'url': r[0], 'title': r[1] or r[0],
                    'time': r[2] or '', 'count': r[3],
                    'browser': os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(p))))
                })
            con.close()
        except: pass
        finally:
            try: os.remove(tmp)
            except: pass

    # Firefox
    ff_base = ""
    if IS_WINDOWS: ff_base = os.path.join(os.path.expandvars("%APPDATA%"), "Mozilla","Firefox","Profiles")
    elif IS_MAC:   ff_base = os.path.expanduser("~/Library/Application Support/Firefox/Profiles")
    else:          ff_base = os.path.expanduser("~/.mozilla/firefox")

    if os.path.exists(ff_base):
        for prof in os.listdir(ff_base):
            db = os.path.join(ff_base, prof, "places.sqlite")
            if not os.path.exists(db): continue
            tmp = db + ".tmp_fb"
            try:
                shutil.copy2(db, tmp)
                con = sqlite3.connect(tmp)
                rows = con.execute("""
                    SELECT p.url, p.title,
                      datetime(h.visit_date/1000000,'unixepoch') as vt,
                      p.visit_count
                    FROM moz_historyvisits h JOIN moz_places p ON h.place_id=p.id
                    WHERE h.visit_date > ?
                    ORDER BY h.visit_date DESC LIMIT ?
                """, (int(cutoff.timestamp())*1000000, limit)).fetchall()
                for r in rows:
                    results.append({'url': r[0], 'title': r[1] or r[0],
                                    'time': r[2] or '', 'count': r[3], 'browser': 'Firefox'})
                con.close()
            except: pass
            finally:
                try: os.remove(tmp)
                except: pass

    # Domain va vaqt bo'yicha sort
    results.sort(key=lambda x: x.get('time',''), reverse=True)
    return results[:limit]

def extract_domain(url):
    try:
        m = re.match(r'https?://([^/]+)', url)
        if m:
            d = m.group(1).lower()
            d = re.sub(r'^www\.', '', d)
            return d
    except: pass
    return url[:40]

# ──────────────────────────────────────────────────────────────
# STATS AGGREGATION
# ──────────────────────────────────────────────────────────────
def get_app_stats(days=7):
    rows = db_query("""
        SELECT app, SUM(duration) as total
        FROM app_usage
        WHERE ts >= datetime('now', ?)
        GROUP BY app ORDER BY total DESC LIMIT 15
    """, (f'-{days} days',))
    return rows

def get_daily_stats(days=14):
    rows = db_query("""
        SELECT day, seconds FROM daily_total
        WHERE day >= date('now', ?)
        ORDER BY day
    """, (f'-{days} days',))
    # Fill missing days
    result = {}
    for i in range(days):
        d = (date.today() - timedelta(days=days-1-i)).isoformat()
        result[d] = 0
    for r in rows:
        result[r['day']] = r['seconds']
    return [{'day': k, 'seconds': v} for k, v in sorted(result.items())]

def get_timeline_today():
    rows = db_query("""
        SELECT strftime('%H', ts) as hour, app, SUM(duration) as total
        FROM app_usage
        WHERE date(ts) = date('now')
        GROUP BY hour, app ORDER BY hour, total DESC
    """)
    hourly = defaultdict(list)
    for r in rows:
        hourly[r['hour']].append({'app': r['app'], 'seconds': r['total']})
    return dict(hourly)

def fmt_time(seconds):
    if seconds < 60: return f"{seconds}s"
    if seconds < 3600: return f"{seconds//60}m {seconds%60}s"
    return f"{seconds//3600}s {(seconds%3600)//60}m"

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80)); return s.getsockname()[0]
    except: return '127.0.0.1'
    finally: s.close()

# ──────────────────────────────────────────────────────────────
# HTML
# ──────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="uz">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FileBridge Pro</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{
  --bg:#08090d;--s1:#0f1117;--s2:#161b26;--s3:#1e2535;
  --bd:#252d40;--bd2:#2e3a55;
  --acc:#3b82f6;--acc2:#8b5cf6;--gr:#10b981;--rd:#ef4444;--yw:#f59e0b;--cy:#06b6d4;
  --tx:#e2e8f0;--m:#64748b;--m2:#8b9ab0;
  --ff:'Space Grotesk',sans-serif;--fm:'JetBrains Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:var(--ff);min-height:100vh;overflow-x:hidden}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:var(--s1)}
::-webkit-scrollbar-thumb{background:var(--bd2);border-radius:2px}

/* ── LOGIN ── */
#login{min-height:100vh;display:flex;align-items:center;justify-content:center;
  background:radial-gradient(ellipse 80% 60% at 50% 0%,#1a1f3a,var(--bg))}
.lbox{background:var(--s1);border:1px solid var(--bd);border-radius:20px;padding:48px 40px;width:380px;text-align:center}
.llogo{font-size:28px;font-weight:700;background:linear-gradient(135deg,var(--acc),var(--acc2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:4px}
.lsub{color:var(--m);font-size:13px;margin-bottom:32px}
.linput{width:100%;background:var(--s2);border:1px solid var(--bd);border-radius:10px;
  padding:13px 16px;color:var(--tx);font-family:var(--fm);font-size:18px;letter-spacing:6px;
  outline:none;margin-bottom:14px;transition:border .2s}
.linput:focus{border-color:var(--acc)}
.lbtn{width:100%;background:linear-gradient(135deg,var(--acc),var(--acc2));border:none;
  border-radius:10px;padding:13px;color:#fff;font-family:var(--ff);font-size:15px;
  font-weight:600;cursor:pointer;transition:opacity .2s}
.lbtn:hover{opacity:.85}
.lerr{color:var(--rd);font-size:13px;margin-top:10px;display:none}

/* ── LAYOUT ── */
#app{display:none;height:100vh;flex-direction:column}
header{display:flex;align-items:center;gap:12px;padding:0 20px;height:54px;
  background:var(--s1);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:100;flex-shrink:0}
.logo{font-size:18px;font-weight:700;background:linear-gradient(135deg,var(--acc),var(--acc2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;white-space:nowrap}
.nav-tabs{display:flex;gap:4px;flex:1}
.ntab{padding:6px 16px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;
  color:var(--m);transition:all .15s;display:flex;align-items:center;gap:6px}
.ntab:hover{color:var(--tx);background:var(--s2)}
.ntab.active{color:var(--acc);background:rgba(59,130,246,.12)}
.hbtns{display:flex;gap:8px}
.hbtn{background:var(--s2);border:1px solid var(--bd);border-radius:8px;padding:6px 14px;
  color:var(--tx);font-family:var(--ff);font-size:12px;cursor:pointer;display:flex;
  align-items:center;gap:5px;transition:all .15s;white-space:nowrap}
.hbtn:hover{border-color:var(--acc);color:var(--acc)}
.hbtn.primary{background:var(--acc);border-color:var(--acc);color:#fff}
.hbtn.primary:hover{background:#2563eb;color:#fff}

.main-body{display:flex;flex:1;overflow:hidden}

/* ── SIDEBAR ── */
aside{width:200px;background:var(--s1);border-right:1px solid var(--bd);
  padding:14px 10px;overflow-y:auto;flex-shrink:0}
.sec-title{font-size:10px;font-weight:600;color:var(--m);text-transform:uppercase;
  letter-spacing:1px;margin:8px 8px 6px;padding:0}
.ai{display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:8px;
  cursor:pointer;font-size:12px;color:var(--m);transition:all .15s;margin-bottom:1px}
.ai:hover,.ai.active{background:var(--s2);color:var(--tx)}
.ai .i{font-size:14px;width:18px;text-align:center}

/* ── PANELS ── */
.panel{display:none;flex:1;overflow-y:auto;padding:20px}
.panel.show{display:block}

/* ── CARDS ── */
.card{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:16px}
.card-title{font-size:12px;font-weight:600;color:var(--m);text-transform:uppercase;
  letter-spacing:.8px;margin-bottom:12px}
.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
@media(max-width:900px){.grid-4{grid-template-columns:repeat(2,1fr)}}

.stat-card{background:var(--s1);border:1px solid var(--bd);border-radius:12px;padding:16px}
.stat-card .label{font-size:11px;color:var(--m);margin-bottom:6px}
.stat-card .value{font-size:22px;font-weight:700;font-family:var(--fm)}
.stat-card .sub{font-size:11px;color:var(--m2);margin-top:4px}
.stat-card.acc{border-color:rgba(59,130,246,.4);background:rgba(59,130,246,.06)}
.stat-card.gr{border-color:rgba(16,185,129,.4);background:rgba(16,185,129,.06)}
.stat-card.yw{border-color:rgba(245,158,11,.4);background:rgba(245,158,11,.06)}
.stat-card.cy{border-color:rgba(6,182,212,.4);background:rgba(6,182,212,.06)}

/* ── APP BARS ── */
.app-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.app-row .name{font-size:12px;width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.app-row .bar-wrap{flex:1;height:8px;background:var(--s3);border-radius:4px;overflow:hidden}
.app-row .bar-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .6s}
.app-row .dur{font-size:11px;color:var(--m);width:60px;text-align:right;font-family:var(--fm)}

/* ── HISTORY TABLE ── */
.htable{width:100%;border-collapse:collapse;font-size:12px}
.htable th{text-align:left;padding:8px 12px;font-size:10px;font-weight:600;
  color:var(--m);text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid var(--bd)}
.htable td{padding:8px 12px;border-bottom:1px solid rgba(37,45,64,.5);max-width:0}
.htable tr:hover td{background:rgba(59,130,246,.04)}
.htable .url{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--acc);font-family:var(--fm);font-size:11px}
.htable .title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.htable .time{white-space:nowrap;color:var(--m);font-family:var(--fm);font-size:11px}
.htable .cnt{text-align:right;color:var(--yw);font-family:var(--fm)}
.domain-pill{display:inline-flex;align-items:center;gap:5px;background:var(--s3);
  border:1px solid var(--bd);border-radius:20px;padding:3px 10px;font-size:11px;
  margin:2px;cursor:pointer;transition:all .15s}
.domain-pill:hover{border-color:var(--acc);color:var(--acc)}
.domain-pill.active{background:rgba(59,130,246,.15);border-color:var(--acc);color:var(--acc)}

/* ── FILES ── */
.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:14px}
.search{flex:1;background:var(--s1);border:1px solid var(--bd);border-radius:8px;
  padding:8px 13px;color:var(--tx);font-family:var(--fm);font-size:12px;outline:none;transition:border .2s}
.search:focus{border-color:var(--acc)}
.upload-zone{border:2px dashed var(--bd);border-radius:12px;padding:24px;
  text-align:center;color:var(--m);font-size:13px;margin-bottom:14px;
  cursor:pointer;transition:all .2s}
.upload-zone:hover,.upload-zone.drag{border-color:var(--acc);color:var(--acc);background:rgba(59,130,246,.04)}
.file-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:10px}
.fcard{background:var(--s1);border:1px solid var(--bd);border-radius:10px;
  padding:12px 10px;cursor:pointer;transition:all .15s;text-align:center;position:relative}
.fcard:hover{border-color:var(--acc);transform:translateY(-1px)}
.fcard.sel{border-color:var(--acc);background:rgba(59,130,246,.08)}
.fcard .chk{position:absolute;top:7px;right:7px;width:15px;height:15px;
  border-radius:4px;border:1.5px solid var(--bd);background:var(--s2);
  display:flex;align-items:center;justify-content:center;font-size:9px}
.fcard.sel .chk{background:var(--acc);border-color:var(--acc);color:#fff}
.fcard .fi{font-size:28px;margin-bottom:7px}
.fcard .fn{font-size:10px;word-break:break-all;line-height:1.4}
.fcard .fm{font-size:10px;color:var(--m);margin-top:3px}
.breadcrumb{display:flex;align-items:center;gap:3px;font-size:12px;color:var(--m);
  background:var(--s1);border:1px solid var(--bd);border-radius:8px;padding:6px 12px;
  flex:1;overflow:hidden}
.breadcrumb span{cursor:pointer;padding:1px 4px;border-radius:4px;white-space:nowrap}
.breadcrumb span:hover{color:var(--acc)}
.breadcrumb .sep{color:var(--bd2)}

/* ── PROGRESS ── */
.prog-wrap{margin:12px 0}
.prog-bar{height:3px;background:var(--bd);border-radius:2px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .3s;width:0}
.prog-lbl{font-size:11px;color:var(--m);margin-top:4px}

/* ── CTX ── */
.ctx{position:fixed;background:var(--s2);border:1px solid var(--bd);border-radius:8px;
  padding:4px;z-index:999;min-width:160px;box-shadow:0 8px 32px rgba(0,0,0,.5);display:none}
.cxi{padding:7px 12px;border-radius:5px;cursor:pointer;font-size:12px;
  display:flex;gap:8px;align-items:center}
.cxi:hover{background:var(--s3)}
.cxi.d{color:var(--rd)}
.cxs{height:1px;background:var(--bd);margin:3px 0}

/* ── MODAL ── */
.mbg{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;
  display:none;align-items:center;justify-content:center}
.modal{background:var(--s1);border:1px solid var(--bd);border-radius:14px;
  padding:26px;width:380px;max-width:92vw}
.modal h3{font-size:16px;margin-bottom:16px}
.modal input{width:100%;background:var(--s2);border:1px solid var(--bd);
  border-radius:7px;padding:10px 12px;color:var(--tx);font-family:var(--fm);
  font-size:14px;outline:none;margin-bottom:14px}
.mbtns{display:flex;gap:8px;justify-content:flex-end}

/* ── TOAST ── */
.toasts{position:fixed;bottom:20px;right:20px;z-index:500;display:flex;flex-direction:column;gap:7px}
.toast{background:var(--s2);border:1px solid var(--bd);border-radius:8px;
  padding:10px 14px;font-size:12px;min-width:180px;animation:tin .2s ease;display:flex;align-items:center;gap:8px}
.toast.ok{border-color:var(--gr)}
.toast.err{border-color:var(--rd)}
@keyframes tin{from{transform:translateX(16px);opacity:0}to{transform:translateX(0);opacity:1}}
.statusbar{background:var(--s1);border-top:1px solid var(--bd);padding:5px 20px;
  font-size:11px;color:var(--m);display:flex;gap:20px;flex-shrink:0}
.empty{text-align:center;padding:50px;color:var(--m)}
.empty .ei{font-size:40px;margin-bottom:10px}
input[type=file]{display:none}
.chart-wrap{position:relative;height:180px}
.chart-wrap canvas{max-height:180px}

/* Timeline */
.tl-hour{display:flex;align-items:center;gap:8px;margin-bottom:5px;font-size:11px}
.tl-h{width:40px;color:var(--m);font-family:var(--fm)}
.tl-apps{display:flex;gap:4px;flex-wrap:wrap}
.tl-app{padding:2px 8px;border-radius:12px;font-size:10px;background:var(--s3);border:1px solid var(--bd)}

.tag{display:inline-block;padding:2px 8px;border-radius:12px;font-size:10px;background:var(--s3);border:1px solid var(--bd);color:var(--m2)}
.loading{text-align:center;padding:30px;color:var(--m)}
.tabs-row{display:flex;gap:6px;margin-bottom:14px}
.stab{padding:5px 14px;border-radius:7px;cursor:pointer;font-size:12px;
  color:var(--m);border:1px solid var(--bd);background:var(--s2);transition:all .15s}
.stab:hover{color:var(--tx)}
.stab.active{color:var(--acc);border-color:var(--acc);background:rgba(59,130,246,.1)}
</style>
</head>
<body>

<!-- LOGIN -->
<div id="login">
  <div class="lbox">
    <div class="llogo">🌉 FileBridge Pro</div>
    <div class="lsub">Fayl menejeri + Monitoring</div>
    <input class="linput" type="password" id="pw" placeholder="••••" onkeydown="if(event.key==='Enter')auth()">
    <button class="lbtn" onclick="auth()">Kirish →</button>
    <div class="lerr" id="lerr">Parol noto'g'ri!</div>
  </div>
</div>

<!-- APP -->
<div id="app">
  <header>
    <div class="logo">FileBridge Pro</div>
    <div class="nav-tabs">
      <div class="ntab active" onclick="gotoTab('monitor')" id="tab-monitor">📊 Monitoring</div>
      <div class="ntab" onclick="gotoTab('files')" id="tab-files">📁 Fayllar</div>
      <div class="ntab" onclick="gotoTab('history')" id="tab-history">🌐 Brauzer tarixi</div>
    </div>
    <div class="hbtns">
      <button class="hbtn" onclick="refreshAll()" title="Yangilash">🔄</button>
      <button class="hbtn primary" id="upload-btn" style="display:none" onclick="document.getElementById('fi').click()">⬆ Yuklash</button>
      <input type="file" id="fi" multiple onchange="uploadFiles(this.files)">
    </div>
  </header>

  <div class="main-body">
    <aside>
      <div class="sec-title">Monitoring</div>
      <div class="ai active" onclick="gotoTab('monitor')"><span class="i">📊</span> Dashboard</div>
      <div class="ai" onclick="gotoTab('history')"><span class="i">🌐</span> Saytlar</div>
      <div class="ai" onclick="gotoTab('apps')"><span class="i">🖥</span> Ilovalar</div>
      <div class="sec-title" style="margin-top:12px">Fayllar</div>
      <div class="ai" onclick="gotoTab('files');navigate('')"><span class="i">🏠</span> Uy papkasi</div>
      <div class="ai" onclick="gotoTab('files');navigate('Desktop')"><span class="i">🖥</span> Desktop</div>
      <div class="ai" onclick="gotoTab('files');navigate('Documents')"><span class="i">📄</span> Documents</div>
      <div class="ai" onclick="gotoTab('files');navigate('Downloads')"><span class="i">⬇</span> Downloads</div>
      <div class="ai" onclick="gotoTab('files');navigate('Pictures')"><span class="i">🖼</span> Pictures</div>
      <div class="ai" onclick="gotoTab('files');navigate('Videos')"><span class="i">🎬</span> Videos</div>
    </aside>

    <!-- MONITOR PANEL -->
    <div class="panel show" id="panel-monitor">
      <div id="stat-cards" class="grid-4"></div>
      <div class="grid-2">
        <div class="card">
          <div class="card-title">📅 Kunlik foydalanish (2 hafta)</div>
          <div class="chart-wrap"><canvas id="daily-chart"></canvas></div>
        </div>
        <div class="card">
          <div class="card-title">🖥 Ilovalar bo'yicha vaqt (7 kun)</div>
          <div id="app-bars"></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">⏱ Bugungi ish jarayoni (soatlar)</div>
        <div id="timeline"></div>
      </div>
    </div>

    <!-- HISTORY PANEL -->
    <div class="panel" id="panel-history">
      <div style="margin-bottom:14px">
        <div class="tabs-row">
          <div class="stab active" onclick="loadHistory(1,this)">Bugun</div>
          <div class="stab" onclick="loadHistory(3,this)">3 kun</div>
          <div class="stab" onclick="loadHistory(7,this)">7 kun</div>
          <div class="stab" onclick="loadHistory(30,this)">30 kun</div>
        </div>
        <input class="search" type="text" id="hist-search" placeholder="Sayt yoki nom qidirish..." oninput="filterHistory()">
      </div>
      <div class="grid-2" style="margin-bottom:14px">
        <div class="card">
          <div class="card-title">🔝 Eng ko'p kirilgan saytlar</div>
          <div id="top-domains"></div>
        </div>
        <div class="card">
          <div class="card-title">🌐 Brauzerlar</div>
          <div class="chart-wrap"><canvas id="browser-chart"></canvas></div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">📋 Barcha tashrif tarixi</div>
        <div id="hist-table"></div>
      </div>
    </div>

    <!-- APPS PANEL -->
    <div class="panel" id="panel-apps">
      <div class="tabs-row">
        <div class="stab active" onclick="loadApps(1,this)">Bugun</div>
        <div class="stab" onclick="loadApps(7,this)">7 kun</div>
        <div class="stab" onclick="loadApps(30,this)">30 kun</div>
      </div>
      <div class="grid-2">
        <div class="card">
          <div class="card-title">📊 Ilova vaqti grafigi</div>
          <div class="chart-wrap"><canvas id="app-pie-chart"></canvas></div>
        </div>
        <div class="card">
          <div class="card-title">📋 Batafsil ro'yxat</div>
          <div id="app-detail-list"></div>
        </div>
      </div>
    </div>

    <!-- FILES PANEL -->
    <div class="panel" id="panel-files">
      <div class="toolbar">
        <div class="breadcrumb" id="bc">🏠</div>
        <button class="hbtn" onclick="showNewFolder()">📁 Yangi</button>
        <button class="hbtn primary" onclick="document.getElementById('fi').click()">⬆ Yuklash</button>
      </div>
      <input class="search" type="text" id="fsearch" placeholder="Fayl qidirish..." oninput="filterFiles()" style="margin-bottom:12px;width:100%">
      <div class="upload-zone" id="uzone" onclick="document.getElementById('fi').click()">
        📂 Fayllarni bu yerga tashlang yoki bosing
      </div>
      <div id="prog-area" style="display:none">
        <div class="prog-wrap"><div class="prog-bar"><div class="prog-fill" id="pf"></div></div>
        <div class="prog-lbl" id="pl"></div></div>
      </div>
      <div id="flist"></div>
    </div>
  </div>
  <div class="statusbar">
    <span id="sb-info">FileBridge Pro</span>
    <span id="sb-extra"></span>
  </div>
</div>

<!-- CTX MENU -->
<div class="ctx" id="ctx">
  <div class="cxi" id="cx-open">📂 Ochish</div>
  <div class="cxi" id="cx-dl">⬇ Yuklab olish</div>
  <div class="cxs"></div>
  <div class="cxi" id="cx-ren">✏️ Nom o'zgartirish</div>
  <div class="cxs"></div>
  <div class="cxi d" id="cx-del">🗑 O'chirish</div>
</div>

<!-- MODAL -->
<div class="mbg" id="mbg">
  <div class="modal">
    <h3 id="mt">Yangi papka</h3>
    <input type="text" id="mi" placeholder="">
    <div class="mbtns">
      <button class="hbtn" onclick="closeMod()">Bekor</button>
      <button class="hbtn primary" id="mok">OK</button>
    </div>
  </div>
</div>

<div class="toasts" id="toasts"></div>

<script>
let pwd='', currentPath='', allFiles=[], selFiles=new Set();
let ctxTarget=null;
let dailyChart=null, browserChart=null, appPieChart=null;
let histData=[], histDays=1;

// ── AUTH ──
function auth(){
  pwd=document.getElementById('pw').value;
  fetch('/api/auth',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:pwd})}).then(r=>r.json()).then(d=>{
    if(d.ok){
      document.getElementById('login').style.display='none';
      document.getElementById('app').style.display='flex';
      gotoTab('monitor');
      loadMonitor();
    } else {
      document.getElementById('lerr').style.display='block';
    }
  });
}
document.getElementById('pw').onkeydown=e=>{if(e.key==='Enter')auth()};

// ── TABS ──
function gotoTab(t){
  ['monitor','history','apps','files'].forEach(id=>{
    document.getElementById('panel-'+id).classList.toggle('show', id===t);
    const tb=document.getElementById('tab-'+id);
    if(tb) tb.classList.toggle('active', id===t);
  });
  document.querySelectorAll('aside .ai').forEach(a=>a.classList.remove('active'));
  document.getElementById('upload-btn').style.display = t==='files'?'':'none';
  if(t==='history') loadHistory(1);
  if(t==='apps') loadApps(7);
  if(t==='files'&&!allFiles.length) navigate('');
}

function refreshAll(){
  const active=['monitor','history','apps','files'].find(id=>
    document.getElementById('panel-'+id).classList.contains('show'));
  if(active==='monitor') loadMonitor();
  else if(active==='history') loadHistory(histDays);
  else if(active==='apps') loadApps(7);
  else if(active==='files') navigate(currentPath);
  toast('🔄 Yangilandi','ok');
}

// ── MONITOR ──
async function loadMonitor(){
  const [daily, apps, today] = await Promise.all([
    apiFetch('/api/stats/daily'),
    apiFetch('/api/stats/apps?days=7'),
    apiFetch('/api/stats/today')
  ]);

  // Stat cards
  const todayRow = daily.find(d=>d.day===new Date().toISOString().slice(0,10)) || {seconds:0};
  const todaySec = todayRow.seconds;
  const weekSec = daily.reduce((a,b)=>a+b.seconds,0);
  const topApp = apps[0] ? apps[0].app : '—';
  const todayApps = today ? Object.keys(today).length : 0;

  document.getElementById('stat-cards').innerHTML=`
    <div class="stat-card acc"><div class="label">⏱ Bugun jami</div>
      <div class="value">${fmtTime(todaySec)}</div></div>
    <div class="stat-card gr"><div class="label">📅 Haftalik jami</div>
      <div class="value">${fmtTime(weekSec)}</div></div>
    <div class="stat-card yw"><div class="label">🏆 Eng ko'p ilova</div>
      <div class="value" style="font-size:14px">${topApp.replace('.exe','')}</div></div>
    <div class="stat-card cy"><div class="label">🖥 Bugungi ilovalar</div>
      <div class="value">${todayApps}</div></div>
  `;

  // Daily chart
  renderDailyChart(daily);

  // App bars
  renderAppBars(apps.slice(0,8));

  // Timeline
  renderTimeline(today);
}

function renderDailyChart(data){
  if(dailyChart) dailyChart.destroy();
  const ctx=document.getElementById('daily-chart').getContext('2d');
  dailyChart=new Chart(ctx,{
    type:'bar',
    data:{
      labels:data.map(d=>d.day.slice(5)),
      datasets:[{
        data:data.map(d=>Math.round(d.seconds/60)),
        backgroundColor:'rgba(59,130,246,.6)',
        borderColor:'rgba(59,130,246,1)',
        borderWidth:1,borderRadius:4
      }]
    },
    options:{
      responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},
        tooltip:{callbacks:{label:v=>`${v.raw} daqiqa`}}},
      scales:{
        x:{ticks:{color:'#64748b',font:{size:10}},grid:{color:'rgba(37,45,64,.5)'}},
        y:{ticks:{color:'#64748b',font:{size:10}},grid:{color:'rgba(37,45,64,.5)'},
          title:{display:true,text:'daqiqa',color:'#64748b',font:{size:10}}}
      }
    }
  });
}

function renderAppBars(apps){
  if(!apps.length){document.getElementById('app-bars').innerHTML='<div class="empty"><div class="ei">📭</div>Ma\'lumot yo\'q</div>';return;}
  const max=apps[0].total;
  document.getElementById('app-bars').innerHTML=apps.map(a=>`
    <div class="app-row">
      <div class="name">${a.app.replace('.exe','')}</div>
      <div class="bar-wrap"><div class="bar-fill" style="width:${Math.round(a.total/max*100)}%"></div></div>
      <div class="dur">${fmtTime(a.total)}</div>
    </div>`).join('');
}

function renderTimeline(today){
  const el=document.getElementById('timeline');
  if(!today||!Object.keys(today).length){
    el.innerHTML='<div class="empty"><div class="ei">📭</div>Bugun ma\'lumot yo\'q</div>';return;
  }
  el.innerHTML=Object.entries(today).map(([h,apps])=>`
    <div class="tl-hour">
      <div class="tl-h">${h}:00</div>
      <div class="tl-apps">${apps.slice(0,5).map(a=>
        `<div class="tl-app">${a.app.replace('.exe','')} <span style="color:var(--acc)">${fmtTime(a.seconds)}</span></div>`
      ).join('')}</div>
    </div>`).join('');
}

// ── HISTORY ──
async function loadHistory(days,el){
  histDays=days;
  if(el){document.querySelectorAll('#panel-history .stab').forEach(s=>s.classList.remove('active'));el.classList.add('active');}
  document.getElementById('hist-table').innerHTML='<div class="loading">⏳ Yuklanmoqda...</div>';
  histData=await apiFetch(`/api/history?days=${days}`);
  renderHistory(histData);
  renderTopDomains(histData);
  renderBrowserChart(histData);
}

function filterHistory(){
  const q=document.getElementById('hist-search').value.toLowerCase();
  renderHistory(q?histData.filter(h=>h.url.toLowerCase().includes(q)||h.title.toLowerCase().includes(q)):histData);
}

function renderHistory(data){
  const el=document.getElementById('hist-table');
  if(!data.length){el.innerHTML='<div class="empty"><div class="ei">📭</div>Tarix topilmadi</div>';return;}
  el.innerHTML=`<table class="htable">
    <thead><tr><th style="width:40%">Sarlavha</th><th style="width:35%">URL</th>
    <th>Vaqt</th><th>Brauzer</th><th style="text-align:right">Son</th></tr></thead>
    <tbody>${data.slice(0,300).map(h=>`<tr>
      <td class="title" title="${h.title}">${h.title||'—'}</td>
      <td class="url" title="${h.url}"><a href="${h.url}" target="_blank" style="color:var(--acc);text-decoration:none">${h.url}</a></td>
      <td class="time">${h.time||'—'}</td>
      <td><span class="tag">${h.browser||'?'}</span></td>
      <td class="cnt">${h.count||1}</td>
    </tr>`).join('')}</tbody>
  </table>`;
}

function renderTopDomains(data){
  const counts={};
  data.forEach(h=>{const d=extractDomain(h.url);counts[d]=(counts[d]||0)+1;});
  const sorted=Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,10);
  const max=sorted[0]?sorted[0][1]:1;
  document.getElementById('top-domains').innerHTML=sorted.map(([d,c])=>`
    <div class="app-row">
      <div class="name">${d}</div>
      <div class="bar-wrap"><div class="bar-fill" style="width:${Math.round(c/max*100)}%;background:linear-gradient(90deg,var(--cy),var(--acc))"></div></div>
      <div class="dur">${c} ta</div>
    </div>`).join('');
}

function renderBrowserChart(data){
  const counts={};
  data.forEach(h=>{const b=h.browser||'?';counts[b]=(counts[b]||0)+1;});
  if(browserChart) browserChart.destroy();
  const ctx=document.getElementById('browser-chart').getContext('2d');
  browserChart=new Chart(ctx,{
    type:'doughnut',
    data:{
      labels:Object.keys(counts),
      datasets:[{data:Object.values(counts),
        backgroundColor:['rgba(59,130,246,.8)','rgba(139,92,246,.8)','rgba(6,182,212,.8)','rgba(245,158,11,.8)'],
        borderWidth:0}]
    },
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:'bottom',labels:{color:'#e2e8f0',font:{size:11}}}}}
  });
}

// ── APPS ──
async function loadApps(days,el){
  if(el){document.querySelectorAll('#panel-apps .stab').forEach(s=>s.classList.remove('active'));el.classList.add('active');}
  const apps=await apiFetch(`/api/stats/apps?days=${days}`);
  renderAppPie(apps.slice(0,8));
  renderAppDetail(apps);
}

function renderAppPie(apps){
  if(appPieChart) appPieChart.destroy();
  const ctx=document.getElementById('app-pie-chart').getContext('2d');
  const colors=['#3b82f6','#8b5cf6','#10b981','#f59e0b','#ef4444','#06b6d4','#ec4899','#84cc16'];
  appPieChart=new Chart(ctx,{
    type:'doughnut',
    data:{
      labels:apps.map(a=>a.app.replace('.exe','')),
      datasets:[{data:apps.map(a=>a.total),backgroundColor:colors,borderWidth:0}]
    },
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{position:'right',labels:{color:'#e2e8f0',font:{size:11}}},
        tooltip:{callbacks:{label:v=>`${v.label}: ${fmtTime(v.raw)}`}}}}
  });
}

function renderAppDetail(apps){
  if(!apps.length){document.getElementById('app-detail-list').innerHTML='<div class="empty"><div class="ei">📭</div>Ma\'lumot yo\'q</div>';return;}
  const max=apps[0].total;
  document.getElementById('app-detail-list').innerHTML=apps.map(a=>`
    <div class="app-row">
      <div class="name">${a.app.replace('.exe','')}</div>
      <div class="bar-wrap"><div class="bar-fill" style="width:${Math.round(a.total/max*100)}%"></div></div>
      <div class="dur">${fmtTime(a.total)}</div>
    </div>`).join('');
}

// ── FILES ──
function navigate(path){
  currentPath=path;selFiles.clear();
  document.getElementById('fsearch').value='';
  loadFiles();
}

function loadFiles(){
  document.getElementById('flist').innerHTML='<div class="loading">⏳...</div>';
  updateBC();
  apiFetch(`/api/list?path=${encodeURIComponent(currentPath)}`).then(d=>{
    if(d.error){document.getElementById('flist').innerHTML=`<div class="empty"><div class="ei">⚠️</div>${d.error}</div>`;return;}
    allFiles=d.files;renderFiles(allFiles);
    document.getElementById('sb-info').textContent=`📁 ${currentPath||'~'} — ${allFiles.length} element`;
  });
}

function renderFiles(files){
  const el=document.getElementById('flist');
  if(!files.length){el.innerHTML='<div class="empty"><div class="ei">📭</div>Bo\'sh</div>';return;}
  el.innerHTML='<div class="file-grid">'+files.map(f=>`
    <div class="fcard${selFiles.has(f.name)?' sel':''}" data-name="${f.name}" data-dir="${f.is_dir}"
      onclick="fclick(event,'${f.name}',${f.is_dir})"
      ondblclick="fdbl('${f.name}',${f.is_dir})"
      oncontextmenu="showCtx(event,'${f.name}',${f.is_dir})">
      <div class="chk">${selFiles.has(f.name)?'✓':''}</div>
      <div class="fi">${f.is_dir?'📁':getIcon(f.name)}</div>
      <div class="fn">${f.name}</div>
      <div class="fm">${f.is_dir?'Papka':fmtSize(f.size)}</div>
    </div>`).join('')+'</div>';
}

function fclick(e,name,isdir){
  if(e.shiftKey||e.ctrlKey||e.metaKey){
    if(selFiles.has(name)) selFiles.delete(name); else selFiles.add(name);
    renderFiles(allFiles);
    document.getElementById('sb-extra').textContent=selFiles.size?`${selFiles.size} ta tanlangan`:'';
  }
}
function fdbl(name,isdir){
  if(isdir) navigate(currentPath?currentPath+'/'+name:name);
  else dlFile(name);
}
function filterFiles(){
  const q=document.getElementById('fsearch').value.toLowerCase();
  renderFiles(q?allFiles.filter(f=>f.name.toLowerCase().includes(q)):allFiles);
}
function updateBC(){
  const bc=document.getElementById('bc');
  const parts=currentPath?currentPath.split('/'):[]; let html='<span onclick="navigate(\'\')">🏠</span>';
  let built='';
  parts.forEach(p=>{built=built?built+'/'+p:p;const bp=built;html+=`<span class="sep"> / </span><span onclick="navigate('${bp}')">${p}</span>`;});
  bc.innerHTML=html;
}

// ── UPLOAD ──
function uploadFiles(files){
  if(!files.length)return;
  const pa=document.getElementById('prog-area'),pf=document.getElementById('pf'),pl=document.getElementById('pl');
  pa.style.display='block';
  let done=0;const total=files.length;
  const next=i=>{
    if(i>=total){pa.style.display='none';toast(`✅ ${total} ta yuklandi`,'ok');loadFiles();return;}
    const fd=new FormData();
    fd.append('file',files[i]);fd.append('path',currentPath);fd.append('pwd',pwd);
    pl.textContent=`Yuklanmoqda: ${files[i].name} (${i+1}/${total})`;
    pf.style.width=(i/total*100)+'%';
    fetch('/api/upload',{method:'POST',body:fd}).then(()=>{done++;pf.style.width=(done/total*100)+'%';next(i+1);})
      .catch(()=>{toast('❌ Xato: '+files[i].name,'err');next(i+1);});
  };next(0);
}
const uz=document.getElementById('uzone');
uz.ondragover=e=>{e.preventDefault();uz.classList.add('drag')};
uz.ondragleave=()=>uz.classList.remove('drag');
uz.ondrop=e=>{e.preventDefault();uz.classList.remove('drag');uploadFiles(e.dataTransfer.files)};

function dlFile(name){
  const p=currentPath?currentPath+'/'+name:name;
  window.location.href=`/api/download?path=${encodeURIComponent(p)}&pwd=${encodeURIComponent(pwd)}`;
}

// ── CTX ──
function showCtx(e,name,isdir){
  e.preventDefault(); ctxTarget={name,isdir};
  const m=document.getElementById('ctx');
  m.style.display='block';m.style.left=e.clientX+'px';m.style.top=e.clientY+'px';
  document.getElementById('cx-open').style.display=isdir?'':'none';
  document.getElementById('cx-dl').style.display=isdir?'none':'';
  document.getElementById('cx-open').onclick=()=>{navigate(currentPath?currentPath+'/'+name:name);closeCtx()};
  document.getElementById('cx-dl').onclick=()=>{dlFile(name);closeCtx()};
  document.getElementById('cx-ren').onclick=()=>{showRename(name);closeCtx()};
  document.getElementById('cx-del').onclick=()=>{ctxDel(name);closeCtx()};
}
function closeCtx(){document.getElementById('ctx').style.display='none'}
document.addEventListener('click',closeCtx);

function ctxDel(name){
  if(!confirm(`"${name}" o'chirilsinmi?`))return;
  const p=currentPath?currentPath+'/'+name:name;
  apiPost('/api/delete',{path:p}).then(()=>{toast('🗑 O\'chirildi','ok');loadFiles()});
}

function showRename(name){
  document.getElementById('mt').textContent="Nomni o'zgartirish";
  document.getElementById('mi').value=name;
  document.getElementById('mbg').style.display='flex';
  document.getElementById('mi').focus();
  document.getElementById('mok').onclick=()=>{
    const nn=document.getElementById('mi').value.trim(); if(!nn)return;
    const p=currentPath?currentPath+'/'+name:name;
    apiPost('/api/rename',{path:p,new_name:nn}).then(()=>{closeMod();toast('✏️ Nom o\'zgartirildi','ok');loadFiles()});
  };
}
function showNewFolder(){
  document.getElementById('mt').textContent='Yangi papka';
  document.getElementById('mi').value='';
  document.getElementById('mbg').style.display='flex';
  document.getElementById('mi').focus();
  document.getElementById('mok').onclick=()=>{
    const n=document.getElementById('mi').value.trim();if(!n)return;
    const p=currentPath?currentPath+'/'+n:n;
    apiPost('/api/mkdir',{path:p}).then(()=>{closeMod();toast('📁 Papka yaratildi','ok');loadFiles()});
  };
}
function closeMod(){document.getElementById('mbg').style.display='none'}
document.getElementById('mbg').onclick=e=>{if(e.target===e.currentTarget)closeMod()};

// ── HELPERS ──
async function apiFetch(url){
  const sep=url.includes('?')?'&':'?';
  const r=await fetch(url+sep+'pwd='+encodeURIComponent(pwd));
  return r.json();
}
async function apiPost(url,data){
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({...data,pwd})}).then(r=>r.json());
}

function fmtTime(s){
  s=parseInt(s)||0;
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m '+Math.floor(s%60)+'s';
  return Math.floor(s/3600)+'s '+Math.floor((s%3600)/60)+'m';
}
function fmtSize(b){
  if(!b)return '0 B';
  if(b<1024)return b+' B';
  if(b<1048576)return (b/1024).toFixed(1)+' KB';
  if(b<1073741824)return (b/1048576).toFixed(1)+' MB';
  return (b/1073741824).toFixed(2)+' GB';
}
function extractDomain(url){
  try{const m=url.match(/https?:\/\/([^\/]+)/);if(m)return m[1].replace(/^www\./,'');}catch(e){}return url.slice(0,30);
}
function getIcon(n){
  const e=(n.split('.').pop()||'').toLowerCase();
  const m={pdf:'📄',doc:'📝',docx:'📝',xls:'📊',xlsx:'📊',ppt:'📋',pptx:'📋',
    jpg:'🖼',jpeg:'🖼',png:'🖼',gif:'🖼',webp:'🖼',svg:'🖼',
    mp4:'🎬',mov:'🎬',avi:'🎬',mkv:'🎬',mp3:'🎵',wav:'🎵',flac:'🎵',
    zip:'📦',rar:'📦','7z':'📦',tar:'📦',gz:'📦',
    py:'🐍',js:'📜',html:'🌐',css:'🎨',json:'⚙',txt:'📃',
    exe:'⚙',msi:'⚙'};
  return m[e]||'📄';
}
function toast(msg,type='ok'){
  const w=document.getElementById('toasts'),t=document.createElement('div');
  t.className='toast '+type;t.textContent=msg;w.appendChild(t);
  setTimeout(()=>t.remove(),3000);
}
</script>
</body>
</html>"""

# ──────────────────────────────────────────────────────────────
# HTTP SERVER
# ──────────────────────────────────────────────────────────────
def check_pwd(p): return p == PASSWORD

def safe_path(rel):
    if not rel: return ROOT_DIR
    full = os.path.normpath(os.path.join(ROOT_DIR, rel))
    if not full.startswith(os.path.normpath(ROOT_DIR)): return None
    return full

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json;charset=utf-8')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        qs = parse_qs(p.query)
        path = p.path
        pw = qs.get('pwd',[''])[0]

        if path == '/':
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html;charset=utf-8')
            self.send_header('Content-Length',len(body))
            self.end_headers(); self.wfile.write(body); return

        if path == '/api/list':
            if not check_pwd(pw): self.send_json({'error':'Ruxsat yo\'q'},403); return
            rel = unquote(qs.get('path',[''])[0])
            fp = safe_path(rel)
            if not fp or not os.path.isdir(fp): self.send_json({'error':'Papka topilmadi'},404); return
            items = []
            for name in sorted(os.listdir(fp)):
                try:
                    full = os.path.join(fp, name)
                    st = os.stat(full)
                    items.append({'name':name,'is_dir':os.path.isdir(full),'size':st.st_size})
                except: pass
            items.sort(key=lambda x:(not x['is_dir'],x['name'].lower()))
            self.send_json({'files':items}); return

        if path == '/api/download':
            if not check_pwd(pw): self.send_json({'error':'Ruxsat yo\'q'},403); return
            rel = unquote(qs.get('path',[''])[0])
            fp = safe_path(rel)
            if not fp or not os.path.isfile(fp): self.send_json({'error':'Topilmadi'},404); return
            mime = mimetypes.guess_type(fp)[0] or 'application/octet-stream'
            fname = os.path.basename(fp)
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', os.path.getsize(fp))
            self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
            self.end_headers()
            with open(fp,'rb') as f: shutil.copyfileobj(f, self.wfile)
            return

        if path == '/api/history':
            if not check_pwd(pw): self.send_json({'error':'Ruxsat yo\'q'},403); return
            days = int(qs.get('days',['7'])[0])
            data = get_browser_history(days)
            self.send_json(data); return

        if path == '/api/stats/daily':
            if not check_pwd(pw): self.send_json([],403); return
            self.send_json(get_daily_stats(14)); return

        if path == '/api/stats/apps':
            if not check_pwd(pw): self.send_json([],403); return
            days = int(qs.get('days',['7'])[0])
            self.send_json(get_app_stats(days)); return

        if path == '/api/stats/today':
            if not check_pwd(pw): self.send_json({},403); return
            self.send_json(get_timeline_today()); return

        self.send_response(404); self.end_headers()

    def do_POST(self):
        p = urlparse(self.path)
        path = p.path
        length = int(self.headers.get('Content-Length', 0))

        if path == '/api/auth':
            body = json.loads(self.rfile.read(length))
            self.send_json({'ok': check_pwd(body.get('password',''))}); return

        if path == '/api/upload':
            import cgi
            ct = self.headers.get('Content-Type','')
            fs = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                  environ={'REQUEST_METHOD':'POST','CONTENT_TYPE':ct})
            if not check_pwd(fs.getvalue('pwd','')): self.send_json({'error':'Ruxsat yo\'q'},403); return
            rel = fs.getvalue('path','')
            fp = safe_path(rel)
            if not fp: self.send_json({'error':'Xato yo\'l'},400); return
            os.makedirs(fp, exist_ok=True)
            if 'file' in fs:
                item = fs['file']
                if isinstance(item, list): item = item[0]
                with open(os.path.join(fp, item.filename),'wb') as f: f.write(item.file.read())
            self.send_json({'ok':True}); return

        body = json.loads(self.rfile.read(length)) if length else {}
        if not check_pwd(body.get('pwd','')): self.send_json({'error':'Ruxsat yo\'q'},403); return

        if path == '/api/delete':
            fp = safe_path(body.get('path',''))
            if not fp: self.send_json({'error':'Xato'}); return
            try:
                if os.path.isdir(fp): shutil.rmtree(fp)
                elif os.path.isfile(fp): os.remove(fp)
                self.send_json({'ok':True})
            except Exception as e: self.send_json({'error':str(e)},500)
            return

        if path == '/api/rename':
            fp = safe_path(body.get('path',''))
            nn = body.get('new_name','')
            if not fp or not nn: self.send_json({'error':'Xato'}); return
            try: os.rename(fp, os.path.join(os.path.dirname(fp), nn)); self.send_json({'ok':True})
            except Exception as e: self.send_json({'error':str(e)},500)
            return

        if path == '/api/mkdir':
            fp = safe_path(body.get('path',''))
            if not fp: self.send_json({'error':'Xato'}); return
            try: os.makedirs(fp, exist_ok=True); self.send_json({'ok':True})
            except Exception as e: self.send_json({'error':str(e)},500)
            return

        self.send_response(404); self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.end_headers()


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()

    # Tracker thread
    t = threading.Thread(target=tracker_loop, daemon=True)
    t.start()

    ip = get_ip()
    server = HTTPServer(('0.0.0.0', PORT), Handler)

    print("=" * 58)
    print("   🌉  FileBridge Pro — Monitoring + Fayl menejeri")
    print("=" * 58)
    print(f"   📡  Manzil   :  http://{ip}:{PORT}")
    print(f"   🔑  Parol    :  {PASSWORD}")
    print(f"   📁  Papka    :  {ROOT_DIR}")
    print(f"   💾  Ma'lumotlar: {DB_PATH}")
    print("=" * 58)
    print("   Birinchi kompyuterdan brauzerda yuqoridagi")
    print("   manzilni oching!")
    print("   Ctrl+C — to'xtatish")
    print("=" * 58)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _tracking = False
        print("\n   Server to'xtatildi.")
