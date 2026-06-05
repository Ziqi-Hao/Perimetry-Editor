#!/usr/bin/env python3
"""Generic browser-based HFA 24-2 Total Deviation grid editor.

Left pane:  zoomable / pannable original perimetry report image
Right pane: editable, colour-coded 24-2 TD grid
Auto-save:  every edit immediately writes to CSV + JSON

This is the project-agnostic version of the MS4 ``interactive_editor_td.py``.

Configuration (environment variables):
  PORT              HTTP port to bind (default 8766; most PaaS set this)
  HOST              Bind host (default 0.0.0.0)
  DATA_DIR          Root data dir. Defaults to ./data
                    Expected layout (auto-created):
                      $DATA_DIR/images/{subject_id}_{OD|OS}.jpg
                      $DATA_DIR/extracted/td_54point.csv      <- output
                      $DATA_DIR/extracted/td_grids.json       <- output

Discovery:
  At startup we list ``$DATA_DIR/images/*`` and build the subject list from
  filenames matching the pattern ``{stem}_OD.{ext}`` / ``{stem}_OS.{ext}``.
  Accepted extensions: jpg, jpeg, png. Existing rows in the saved JSON are
  loaded back; previously-saved subjects that no longer have an image still
  appear (so re-uploading a fresh report later doesn't lose your edits).

Image upload:
  POST /api/upload  (multipart/form-data with fields ``subject`` and ``eye``)
  saves the file under ``$DATA_DIR/images/`` and triggers a re-scan.
"""
import argparse
import csv
import http.server
import json
import os
import re
import sys
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_default_policy

sys.path.insert(0, os.path.dirname(__file__))
from hvf_24_2 import (
    ROWS_24_2, MAX_COLS, BS_ROW, BS_COL,
    get_vf_x, eccentricity, quadrant_anatomical,
)

DATA_DIR    = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
IMAGES_DIR  = os.path.join(DATA_DIR, "images")
OUT_DIR     = os.path.join(DATA_DIR, "extracted")
OUT_CSV     = os.path.join(OUT_DIR, "td_54point.csv")
OUT_JSON    = os.path.join(OUT_DIR, "td_grids.json")

ALLOWED_EXT = (".jpg", ".jpeg", ".png")
NAME_RE     = re.compile(r"^(?P<sid>[A-Za-z0-9._\-]+)_(?P<eye>OD|OS)\.(?P<ext>jpg|jpeg|png)$",
                         re.IGNORECASE)

LIVE_DATA = {}        # {f"{sid}_{eye}": {"subject":..., "eye":..., "rows":[[...]]}}
IMAGE_PATHS = {}      # {f"{sid}_{eye}": "/abs/path/to/img"}


# ───────────────────────── data store ───────────────────────────
def _ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(OUT_DIR,    exist_ok=True)


def _empty_rows():
    return [[None] * len(xs) for _, xs in ROWS_24_2]


def discover_subjects():
    """Scan IMAGES_DIR and refresh IMAGE_PATHS + LIVE_DATA shells."""
    IMAGE_PATHS.clear()
    if not os.path.isdir(IMAGES_DIR):
        return
    for fname in sorted(os.listdir(IMAGES_DIR)):
        m = NAME_RE.match(fname)
        if not m:
            continue
        sid = m.group("sid")
        eye = m.group("eye").upper()
        key = f"{sid}_{eye}"
        IMAGE_PATHS[key] = os.path.join(IMAGES_DIR, fname)
        if key not in LIVE_DATA:
            LIVE_DATA[key] = {
                "subject": sid,
                "eye": eye,
                "age": "",
                "sex": "",
                "rows": _empty_rows(),
            }


def load_persisted():
    """Replace LIVE_DATA with whatever's already in OUT_JSON, if anything."""
    if not os.path.exists(OUT_JSON):
        return
    try:
        with open(OUT_JSON) as f:
            data = json.load(f)
        if isinstance(data, dict):
            LIVE_DATA.clear()
            LIVE_DATA.update(data)
            print(f"  Loaded {len(LIVE_DATA)} previously-edited entries from {OUT_JSON}")
    except Exception as e:
        print(f"  Warning: could not load {OUT_JSON} ({e})")


def save_all():
    _ensure_dirs()
    with open(OUT_JSON, "w") as f:
        json.dump(LIVE_DATA, f, indent=2)

    fieldnames = [
        "subject", "eye", "age", "sex",
        "row", "col", "x_vf_deg", "y_vf_deg",
        "eccentricity_deg", "quadrant", "td_dB",
    ]
    with open(OUT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for key in sorted(LIVE_DATA.keys()):
            d = LIVE_DATA[key]
            subj, eye = d["subject"], d["eye"]
            for row_idx, (y, xs_od) in enumerate(ROWS_24_2):
                row_data = d["rows"][row_idx] if row_idx < len(d["rows"]) else []
                for col_idx, x_od in enumerate(xs_od):
                    val = row_data[col_idx] if col_idx < len(row_data) else None
                    x_vf = get_vf_x(x_od, eye)
                    if val == "BS":
                        td_str = "BS"
                    elif val is None:
                        td_str = ""
                    else:
                        td_str = str(val)
                    writer.writerow({
                        "subject": subj, "eye": eye,
                        "age": d.get("age", ""), "sex": d.get("sex", ""),
                        "row": row_idx, "col": col_idx,
                        "x_vf_deg": x_vf, "y_vf_deg": y,
                        "eccentricity_deg": round(eccentricity(x_vf, y), 2),
                        "quadrant": quadrant_anatomical(x_vf, y, eye),
                        "td_dB": td_str,
                    })


# ───────────────────────── HTML page ───────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Perimetry Editor — HFA 24-2 Total Deviation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0a0e1a;
  --bg-1:#0f1424;
  --bg-2:#141a2e;
  --surface:rgba(20,26,46,.7);
  --surface-solid:#1a2138;
  --border:rgba(148,163,184,.12);
  --border-strong:rgba(148,163,184,.22);
  --text:#e6e9f2;
  --text-mute:#94a3b8;
  --text-dim:#64748b;
  --violet:#8b5cf6;
  --violet-hi:#a78bfa;
  --violet-glow:rgba(139,92,246,.35);
  --cyan:#06b6d4;
  --emerald:#10b981;
  --amber:#fbbf24;
  --orange:#f97316;
  --rose:#ef4444;
  --shadow-lg:0 20px 50px -10px rgba(0,0,0,.5),0 8px 16px -4px rgba(0,0,0,.3);
  --shadow-md:0 6px 18px -4px rgba(0,0,0,.4);
  --ease:cubic-bezier(.22,.61,.36,1);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI','SF Pro Display','Roboto',sans-serif;
  background:
    radial-gradient(1100px 600px at 80% -10%,rgba(139,92,246,.18),transparent 60%),
    radial-gradient(900px 500px at -10% 110%,rgba(6,182,212,.10),transparent 65%),
    var(--bg);
  color:var(--text);
  font-feature-settings:'ss01','cv11';
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  letter-spacing:-.01em;
}
button{font-family:inherit}

/* ─── top bar ─── */
.topbar{
  height:56px;display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;
  background:rgba(15,20,36,.7);backdrop-filter:saturate(140%) blur(14px);
  -webkit-backdrop-filter:saturate(140%) blur(14px);
  border-bottom:1px solid var(--border);
  position:relative;z-index:30;
}
.brand{display:flex;align-items:center;gap:12px}
.brand .logo{
  width:30px;height:30px;border-radius:8px;
  background:linear-gradient(135deg,var(--violet) 0%,var(--cyan) 100%);
  display:grid;place-items:center;
  box-shadow:0 6px 18px -2px var(--violet-glow);
  font-size:15px;
}
.brand .title{font-weight:700;font-size:15px;letter-spacing:-.02em}
.brand .sub{font-size:11px;color:var(--text-mute);letter-spacing:0;margin-top:1px}
.topbar .actions{display:flex;gap:8px;align-items:center}

.btn{
  height:34px;padding:0 14px;border-radius:8px;border:1px solid var(--border-strong);
  background:rgba(255,255,255,.02);color:var(--text);font-size:13px;font-weight:500;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
  transition:all .15s var(--ease);
}
.btn:hover{background:rgba(255,255,255,.06);border-color:rgba(148,163,184,.35)}
.btn:disabled{opacity:.3;cursor:not-allowed}
.btn.icon{width:34px;padding:0;justify-content:center;font-size:15px}
.btn.primary{
  background:linear-gradient(135deg,var(--violet) 0%,#7c3aed 100%);
  border-color:transparent;color:#fff;font-weight:600;
  box-shadow:0 4px 14px -2px var(--violet-glow);
}
.btn.primary:hover{filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 6px 20px -2px var(--violet-glow)}

/* ─── shell ─── */
.shell{display:grid;grid-template-columns:260px 1fr;height:calc(100vh - 56px)}

/* ─── sidebar ─── */
.sidebar{
  border-right:1px solid var(--border);background:rgba(10,14,26,.4);
  display:flex;flex-direction:column;overflow:hidden;
}
.sidebar .head{
  padding:14px 16px 10px;display:flex;justify-content:space-between;align-items:center;
  border-bottom:1px solid var(--border);
}
.sidebar .head .label{font-size:11px;font-weight:600;color:var(--text-mute);text-transform:uppercase;letter-spacing:.08em}
.sidebar .head .count{font-size:11px;color:var(--text-dim);background:rgba(255,255,255,.04);padding:2px 8px;border-radius:10px}
.subjects{list-style:none;padding:8px;overflow-y:auto;flex:1}
.subjects::-webkit-scrollbar{width:6px}
.subjects::-webkit-scrollbar-thumb{background:rgba(148,163,184,.18);border-radius:4px}
.subjects li{
  position:relative;
  padding:10px 12px 10px 14px;border-radius:9px;cursor:pointer;
  display:flex;align-items:center;gap:10px;
  transition:all .15s var(--ease);font-size:13px;
}
.subjects li:hover{background:rgba(255,255,255,.04)}
.subjects li.active{
  background:linear-gradient(90deg,rgba(139,92,246,.15) 0%,rgba(139,92,246,.04) 100%);
  border:1px solid rgba(139,92,246,.25);
}
.subjects li.active::before{
  content:"";position:absolute;left:-1px;top:8px;bottom:8px;width:3px;
  background:var(--violet);border-radius:3px;box-shadow:0 0 8px var(--violet-glow);
}
.subjects .name{flex:1;font-weight:500;letter-spacing:-.01em}
.subjects .eye{font-size:10px;color:var(--text-mute);background:rgba(255,255,255,.05);padding:2px 6px;border-radius:6px;font-family:'JetBrains Mono',ui-monospace,monospace;letter-spacing:.05em}
.ring{width:22px;height:22px;flex-shrink:0;position:relative}
.ring svg{transform:rotate(-90deg)}
.ring .pct{position:absolute;inset:0;display:grid;place-items:center;font-size:9px;font-weight:600;color:var(--text-mute)}
.ring.done .pct{color:var(--emerald)}

.sidebar .foot{padding:12px 14px;border-top:1px solid var(--border);font-size:11px;color:var(--text-dim);display:flex;justify-content:space-between}
.kbd{display:inline-flex;align-items:center;font-family:'JetBrains Mono',monospace;font-size:10px;padding:2px 6px;border-radius:5px;border:1px solid var(--border-strong);background:rgba(255,255,255,.03);margin:0 2px}

/* ─── main area ─── */
.main{display:grid;grid-template-columns:1fr 480px;height:100%;overflow:hidden;gap:16px;padding:16px}

/* left image card */
.imgcard{
  position:relative;border-radius:14px;overflow:hidden;
  background:#08090f;
  border:1px solid var(--border);
  box-shadow:var(--shadow-lg);
  cursor:grab;
}
.imgcard:active{cursor:grabbing}
.imgcard img{position:absolute;transform-origin:0 0;max-width:none;user-select:none;-webkit-user-drag:none}
.empty-state{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;color:var(--text-mute);padding:40px;text-align:center;gap:12px}
.empty-state .glyph{width:64px;height:64px;border-radius:18px;background:linear-gradient(135deg,rgba(139,92,246,.18),rgba(6,182,212,.12));display:grid;place-items:center;font-size:28px;border:1px solid var(--border-strong)}
.empty-state h3{font-size:16px;font-weight:600;color:var(--text);letter-spacing:-.01em}
.empty-state p{font-size:13px;max-width:340px;line-height:1.5}

.zoom-pill{
  position:absolute;bottom:14px;right:14px;z-index:10;
  display:flex;gap:2px;padding:4px;
  background:rgba(15,20,36,.85);backdrop-filter:blur(10px);
  border-radius:11px;border:1px solid var(--border-strong);
  box-shadow:var(--shadow-md);
}
.zoom-pill button{
  border:none;background:transparent;color:var(--text);width:32px;height:30px;border-radius:7px;
  cursor:pointer;font-size:14px;display:grid;place-items:center;
  transition:background .12s;
}
.zoom-pill button:hover{background:rgba(255,255,255,.08)}
.zoom-pill .sep{width:1px;background:var(--border-strong);margin:4px 2px}

/* right grid card */
.gridcard{
  background:var(--surface);backdrop-filter:blur(14px) saturate(140%);
  -webkit-backdrop-filter:blur(14px) saturate(140%);
  border:1px solid var(--border);border-radius:14px;
  padding:18px;display:flex;flex-direction:column;gap:16px;
  box-shadow:var(--shadow-lg);overflow:hidden;
}
.gridcard .header-row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.gridcard .who{display:flex;flex-direction:column;gap:3px}
.gridcard .who .name{font-size:18px;font-weight:700;letter-spacing:-.02em}
.gridcard .who .meta{font-size:12px;color:var(--text-mute);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.gridcard .who .meta .pill{padding:2px 8px;border-radius:6px;background:rgba(139,92,246,.14);color:var(--violet-hi);font-weight:600;font-size:11px;letter-spacing:.02em}
.gridcard .who .meta .eye-pill{padding:2px 8px;border-radius:6px;background:rgba(255,255,255,.04);border:1px solid var(--border-strong);font-family:'JetBrains Mono',monospace;font-size:11px}

.progress-wrap{flex-shrink:0;text-align:right}
.progress-wrap .pct{font-size:22px;font-weight:700;color:var(--emerald);font-feature-settings:'tnum'}
.progress-wrap .label{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.08em}
.progress-bar{height:4px;border-radius:3px;background:rgba(255,255,255,.05);overflow:hidden;margin-top:6px}
.progress-bar .fill{height:100%;background:linear-gradient(90deg,var(--emerald),var(--cyan));border-radius:3px;transition:width .3s var(--ease)}

/* legend strip */
.legend{display:flex;flex-wrap:wrap;gap:6px;font-size:10px}
.legend .chip{padding:3px 8px;border-radius:6px;font-weight:600;letter-spacing:.02em;display:inline-flex;align-items:center;gap:5px}
.legend .chip .dot{width:8px;height:8px;border-radius:50%}
.legend .chip.normal{background:rgba(16,185,129,.14);color:var(--emerald)}
.legend .chip.normal .dot{background:var(--emerald)}
.legend .chip.borderline{background:rgba(251,191,36,.14);color:var(--amber)}
.legend .chip.borderline .dot{background:var(--amber)}
.legend .chip.moderate{background:rgba(249,115,22,.14);color:var(--orange)}
.legend .chip.moderate .dot{background:var(--orange)}
.legend .chip.severe{background:rgba(239,68,68,.14);color:var(--rose)}
.legend .chip.severe .dot{background:var(--rose)}
.legend .chip.bs{background:rgba(148,163,184,.10);color:var(--text-mute)}
.legend .chip.bs .dot{background:var(--text-mute)}
.legend .chip.missing{background:rgba(255,255,255,.04);color:var(--text-dim);border:1px dashed var(--border-strong)}

/* the actual grid */
.gridwrap{display:flex;justify-content:center;align-items:center;flex:1;padding:8px}
table.g{border-collapse:separate;border-spacing:5px}
table.g td{
  width:42px;height:36px;text-align:center;border-radius:9px;cursor:pointer;
  font-family:'JetBrains Mono',ui-monospace,monospace;font-size:13px;font-weight:700;
  font-feature-settings:'tnum';letter-spacing:0;
  transition:transform .12s var(--ease),box-shadow .12s var(--ease),filter .12s;
  position:relative;
}
table.g td:hover{transform:scale(1.08);z-index:5;filter:brightness(1.12)}
table.g td.e{background:transparent !important;cursor:default}
table.g td.e:hover{transform:none;filter:none}
table.g td.editing{
  outline:2px solid var(--violet-hi);outline-offset:1px;
  box-shadow:0 0 0 4px var(--violet-glow),inset 0 0 0 1px rgba(255,255,255,.2);
  z-index:6;
}
table.g td input{
  width:100%;height:100%;border:none;background:transparent;text-align:center;
  font:inherit;color:inherit;outline:none;
}

/* severity cell variants */
.sn{background:linear-gradient(155deg,#34d399,#059669);color:#06311f;box-shadow:inset 0 -1px 0 rgba(0,0,0,.15)}
.sm{background:linear-gradient(155deg,#fde68a,#f59e0b);color:#3b2400;box-shadow:inset 0 -1px 0 rgba(0,0,0,.15)}
.so{background:linear-gradient(155deg,#fdba74,#ea580c);color:#311500;box-shadow:inset 0 -1px 0 rgba(0,0,0,.15)}
.ss{background:linear-gradient(155deg,#fca5a5,#dc2626);color:#fff;box-shadow:inset 0 -1px 0 rgba(0,0,0,.2)}
.sb{background:repeating-linear-gradient(45deg,#475569,#475569 4px,#334155 4px,#334155 8px);color:#cbd5e1;font-size:10px}
.sq{background:rgba(255,255,255,.025);color:var(--text-dim);border:1px dashed rgba(148,163,184,.18);box-shadow:none}
.sq:hover{border-color:var(--violet)}

/* tip footer */
.tip{font-size:10.5px;color:var(--text-dim);text-align:center;line-height:1.7}
.tip .row{display:flex;justify-content:center;gap:14px;flex-wrap:wrap}

/* ─── status ─── */
.status{
  position:fixed;left:50%;transform:translateX(-50%);bottom:14px;z-index:50;
  padding:8px 16px;border-radius:10px;font-size:12px;font-weight:500;
  background:rgba(15,20,36,.9);backdrop-filter:blur(10px);
  border:1px solid var(--border-strong);box-shadow:var(--shadow-md);
  display:inline-flex;align-items:center;gap:8px;
  transition:all .2s var(--ease);
}
.status .dot{width:8px;height:8px;border-radius:50%;background:var(--emerald)}
.status.saving .dot{background:var(--amber);animation:pulse 1.2s infinite}
.status.error .dot{background:var(--rose)}
.status.saving{color:var(--amber);border-color:rgba(251,191,36,.3)}
.status.error{color:var(--rose);border-color:rgba(239,68,68,.35)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}

/* ─── upload modal ─── */
.modal-bg{
  position:fixed;inset:0;background:rgba(5,8,17,.7);backdrop-filter:blur(8px);
  display:none;align-items:center;justify-content:center;z-index:100;
  animation:fadein .2s ease;
}
.modal-bg.show{display:flex}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.modal{
  width:420px;max-width:90vw;background:var(--surface-solid);
  border:1px solid var(--border-strong);border-radius:16px;padding:24px;
  box-shadow:var(--shadow-lg);animation:slideup .25s var(--ease);
}
@keyframes slideup{from{transform:translateY(12px);opacity:0}to{transform:none;opacity:1}}
.modal h3{font-size:17px;font-weight:700;letter-spacing:-.02em;margin-bottom:4px}
.modal .desc{font-size:12.5px;color:var(--text-mute);margin-bottom:18px}
.modal label{display:block;font-size:11px;font-weight:600;color:var(--text-mute);text-transform:uppercase;letter-spacing:.06em;margin:14px 0 6px}
.modal input[type=text],.modal select{
  width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--border-strong);
  background:rgba(255,255,255,.03);color:var(--text);font-size:13px;font-family:inherit;
  transition:border-color .12s,background .12s;
}
.modal input[type=text]:focus,.modal select:focus{outline:none;border-color:var(--violet);background:rgba(139,92,246,.06)}
.modal .filebox{
  border:1.5px dashed var(--border-strong);border-radius:10px;padding:18px;
  text-align:center;cursor:pointer;color:var(--text-mute);font-size:12.5px;
  background:rgba(255,255,255,.02);transition:all .15s var(--ease);
}
.modal .filebox:hover{border-color:var(--violet);background:rgba(139,92,246,.06);color:var(--text)}
.modal .filebox.has-file{border-color:var(--emerald);background:rgba(16,185,129,.08);color:var(--emerald)}
.modal .filebox input{display:none}
.modal .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}
.modal .actions .btn.primary{padding:0 22px}

</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <div class="logo">🩺</div>
    <div>
      <div class="title">Perimetry Editor</div>
      <div class="sub">HFA 24-2 Total Deviation · auto-save</div>
    </div>
  </div>
  <div class="actions">
    <button class="btn icon" onclick="prev()" id="pb" title="Previous (←)">‹</button>
    <button class="btn icon" onclick="next()" id="nb" title="Next (→)">›</button>
    <button class="btn primary" onclick="openUpload()">＋ Upload report</button>
  </div>
</div>

<div class="shell">
  <!-- sidebar -->
  <aside class="sidebar">
    <div class="head">
      <span class="label">Subjects</span>
      <span class="count" id="subCount">0</span>
    </div>
    <ul class="subjects" id="subjects"></ul>
    <div class="foot">
      <span><span class="kbd">←</span><span class="kbd">→</span> nav</span>
      <span><span class="kbd">Tab</span> next cell</span>
    </div>
  </aside>

  <!-- main -->
  <div class="main">
    <!-- image -->
    <div class="imgcard" id="ip">
      <img id="img" src="" draggable="false" style="display:none">
      <div class="empty-state" id="emptyMsg">
        <div class="glyph">📄</div>
        <h3>No subjects yet</h3>
        <p>Click <strong>＋ Upload report</strong> to add a Humphrey 24-2 single-field analysis image and start correcting OCR errors.</p>
      </div>
      <div class="zoom-pill" id="zoomPill" style="display:none">
        <button onclick="zi()" title="Zoom in">+</button>
        <button onclick="zo()" title="Zoom out">−</button>
        <button onclick="zf()" title="Fit">⤢</button>
        <div class="sep"></div>
        <button onclick="rot(-90)" title="Rotate left 90°">↺</button>
        <button onclick="rot(90)" title="Rotate right 90°">↻</button>
        <button onclick="flipH()" title="Flip horizontal">⇔</button>
      </div>
    </div>

    <!-- grid -->
    <div class="gridcard">
      <div class="header-row" id="hdrRow" style="display:none">
        <div class="who">
          <div class="name" id="whoName"></div>
          <div class="meta">
            <span class="eye-pill" id="whoEye"></span>
            <span class="pill">Total Deviation · dB</span>
          </div>
        </div>
        <div class="progress-wrap" style="min-width:90px">
          <div class="pct" id="progPct">—</div>
          <div class="label">filled</div>
          <div class="progress-bar"><div class="fill" id="progFill" style="width:0"></div></div>
        </div>
      </div>

      <div class="legend">
        <span class="chip normal"><span class="dot"></span>≥ 0</span>
        <span class="chip borderline"><span class="dot"></span>−5 to −1</span>
        <span class="chip moderate"><span class="dot"></span>−15 to −6</span>
        <span class="chip severe"><span class="dot"></span>&lt; −15</span>
        <span class="chip bs"><span class="dot"></span>BS</span>
        <span class="chip missing">?</span>
      </div>

      <div class="gridwrap"><table class="g" id="gt"></table></div>

      <div class="tip">
        <div class="row">
          <span><span class="kbd">click</span> edit</span>
          <span><span class="kbd">Enter</span> confirm</span>
          <span><span class="kbd">Tab</span> next</span>
          <span><span class="kbd">Esc</span> cancel</span>
        </div>
        <div style="margin-top:4px;color:var(--text-dim)">Type <strong>BS</strong> for blind spot · <strong>?</strong> or empty for missing</div>
      </div>
    </div>
  </div>
</div>

<div class="status" id="sb"><span class="dot"></span><span id="sbtxt">Ready</span></div>

<!-- upload modal -->
<div class="modal-bg" id="uploadModal">
  <div class="modal">
    <h3>Upload perimetry report</h3>
    <div class="desc">Drop in any HFA 24-2 single-field analysis image. Saved under <code>data/images/</code> on the server.</div>

    <label>Subject ID</label>
    <input type="text" id="upSubj" placeholder="patient_007">

    <label>Eye</label>
    <select id="upEye"><option>OD</option><option>OS</option></select>

    <label>Image file (jpg / jpeg / png)</label>
    <label class="filebox" id="fileLabel">
      <input type="file" id="upFile" accept="image/jpeg,image/png,image/jpg">
      <span id="fileLabelText">Click to choose a file…</span>
    </label>

    <div class="actions">
      <button class="btn" onclick="closeUpload()">Cancel</button>
      <button class="btn primary" onclick="doUpload()">Upload</button>
    </div>
  </div>
</div>

<script>
const RS=[4,6,8,9,9,8,6,4],MC=9,BSR=4,BSC=7;
const TOTAL=RS.reduce((a,b)=>a+b,0);  // 54
let D={},ck="",ci=0,ks=[],sc=1,sx=0,sy=0,drag=false,dsx,dsy,dix,diy,imgRot=0,imgFlipH=false;

function init(){refresh();
let p=document.getElementById('ip');
p.onmousedown=e=>{drag=true;dsx=e.clientX;dsy=e.clientY;dix=sx;diy=sy};
p.onmousemove=e=>{if(!drag)return;sx=dix+(e.clientX-dsx);sy=diy+(e.clientY-dsy);ut()};
p.onmouseup=()=>drag=false;p.onmouseleave=()=>drag=false;
p.onwheel=e=>{e.preventDefault();let d=e.deltaY>0?.9:1.1,r=p.getBoundingClientRect(),
mx=e.clientX-r.left,my=e.clientY-r.top;sx=mx-(mx-sx)*d;sy=my-(my-sy)*d;sc*=d;ut()}
// file label
let f=document.getElementById('upFile'),fl=document.getElementById('fileLabel'),flt=document.getElementById('fileLabelText');
f.onchange=()=>{if(f.files[0]){flt.textContent=f.files[0].name;fl.classList.add('has-file')}
else{flt.textContent='Click to choose a file…';fl.classList.remove('has-file')}}
}

function filledCount(rows){
  let c=0;for(let r of rows)for(let v of r)if(v!==null&&v!==undefined)c++;
  return c;
}

function buildSidebar(){
  let ul=document.getElementById('subjects');ul.innerHTML='';
  document.getElementById('subCount').textContent=ks.length;
  ks.forEach(k=>{
    let d=D[k],n=filledCount(d.rows),pct=Math.round(100*n/TOTAL);
    let done=(n>=TOTAL);
    let li=document.createElement('li');
    if(k===ck)li.classList.add('active');
    li.onclick=()=>load(k);
    li.innerHTML=`<span class="name">${d.subject}</span>
      <span class="eye">${d.eye}</span>
      <span class="ring ${done?'done':''}">
        <svg width="22" height="22" viewBox="0 0 22 22">
          <circle cx="11" cy="11" r="9" fill="none" stroke="rgba(148,163,184,.18)" stroke-width="2.2"/>
          <circle cx="11" cy="11" r="9" fill="none" stroke="${done?'#10b981':'#a78bfa'}"
            stroke-width="2.2" stroke-linecap="round"
            stroke-dasharray="${(2*Math.PI*9).toFixed(2)}"
            stroke-dashoffset="${(2*Math.PI*9*(1-pct/100)).toFixed(2)}"/>
        </svg>
        <span class="pct">${done?'✓':pct}</span>
      </span>`;
    ul.appendChild(li);
  });
}

function refresh(){fetch('/api/data').then(r=>r.json()).then(d=>{
D=d;ks=Object.keys(d).sort();
if(ks.length===0){
  document.getElementById('emptyMsg').style.display='flex';
  document.getElementById('img').style.display='none';
  document.getElementById('zoomPill').style.display='none';
  document.getElementById('hdrRow').style.display='none';
  document.getElementById('gt').innerHTML='';
  buildSidebar();
  return;
}
document.getElementById('emptyMsg').style.display='none';
document.getElementById('img').style.display='';
document.getElementById('zoomPill').style.display='flex';
document.getElementById('hdrRow').style.display='flex';
load(ks[Math.min(Math.max(ci,0),ks.length-1)]);
})}

function ut(){let f=imgFlipH?'scaleX(-1)':'';
document.getElementById('img').style.transform=`translate(${sx}px,${sy}px) scale(${sc}) rotate(${imgRot}deg) ${f}`}
function zi(){sc*=1.3;ut()}function zo(){sc/=1.3;ut()}
function zf(){let p=document.getElementById('ip'),i=document.getElementById('img');
if(!i.naturalWidth)return;
sc=Math.min(p.clientWidth/i.naturalWidth,p.clientHeight/i.naturalHeight)*.95;
sx=(p.clientWidth-i.naturalWidth*sc)/2;sy=(p.clientHeight-i.naturalHeight*sc)/2;ut()}
function rot(deg){imgRot=(imgRot+deg)%360;ut()}
function flipH(){imgFlipH=!imgFlipH;ut()}

function load(k){
ck=k;ci=ks.indexOf(k);
document.getElementById('pb').disabled=ci<=0;
document.getElementById('nb').disabled=ci>=ks.length-1;
let d=D[k];
document.getElementById('whoName').textContent=d.subject;
document.getElementById('whoEye').textContent=d.eye;
let n=filledCount(d.rows),pct=Math.round(100*n/TOTAL);
document.getElementById('progPct').textContent=pct+'%';
document.getElementById('progFill').style.width=pct+'%';
imgRot=0;imgFlipH=false;
let img=document.getElementById('img');
img.src='/api/image?key='+encodeURIComponent(k)+'&t='+Date.now();
img.onload=()=>setTimeout(zf,50);
img.onerror=()=>{img.src='';};
bg(d.rows);
buildSidebar();
}

function sc2(v){if(v===null||v===undefined)return'sq';if(v==='BS')return'sb';
let n=parseInt(v);if(isNaN(n))return'sq';if(n>=0)return'sn';if(n>=-5)return'sm';if(n>=-15)return'so';return'ss'}
function dv(v){if(v===null||v===undefined)return'?';if(v==='BS')return'BS';return String(v)}

function bg(rows){let t=document.getElementById('gt');t.innerHTML='';
let eye=D[ck]?.eye||'OD';
let odPad=[3,2,1,0,0,1,2,3];
let osPad=[2,1,0,0,0,0,1,2];
for(let ri=0;ri<8;ri++){let tr=document.createElement('tr'),nc=RS[ri],pl=eye==='OS'?osPad[ri]:odPad[ri];
for(let ci=0;ci<MC;ci++){let td=document.createElement('td'),di=ci-pl;
if(di<0||di>=nc){td.className='e';tr.appendChild(td);continue}
let v=rows[ri]&&rows[ri][di]!==undefined?rows[ri][di]:null;
td.className=sc2(v);td.textContent=dv(v);td.dataset.row=ri;td.dataset.col=di;
if(ri===BSR&&di===BSC){td.title='Standard blind-spot position'}
td.onclick=()=>ed(td,ri,di);tr.appendChild(td)}t.appendChild(tr)}}

function ed(td,row,col){if(td.querySelector('input'))return;
let ov=td.textContent;td.classList.add('editing');
let inp=document.createElement('input');inp.value=ov==='?'?'':ov;inp.maxLength=4;
td.textContent='';td.appendChild(inp);inp.focus();inp.select();
let fin=()=>{let nt=inp.value.trim().toUpperCase();td.classList.remove('editing');
if(td.contains(inp))td.removeChild(inp);let nv;
if(nt===''||nt==='?'){nv=null;nt='?'}else if(nt==='BS'){nv='BS'}
else{let n=parseInt(nt);if(!isNaN(n)){nv=n;nt=String(n)}else{nv=null;nt='?'}}
td.textContent=nt==='?'?'?':nt;td.className=sc2(nv);
if(row===BSR&&col===BSC){td.title='Standard blind-spot position'}
if(D[ck]&&D[ck].rows[row]){let old=D[ck].rows[row][col];
if(old!==nv){D[ck].rows[row][col]=nv;autoSave()}}};
inp.onkeydown=e=>{if(e.key==='Enter')fin();
if(e.key==='Escape'){td.classList.remove('editing');if(td.contains(inp))td.removeChild(inp);td.textContent=ov}
if(e.key==='Tab'){e.preventDefault();fin();mn(row,col,e.shiftKey)}};
inp.onblur=fin}

function mn(r,c,bk){let nr=r,nx=c+(bk?-1:1);
if(nx>=RS[nr]){nr++;nx=0}if(nx<0){nr--;nx=nr>=0?RS[nr]-1:0}
if(nr<0||nr>=8)return;let pl=Math.floor((MC-RS[nr])/2);
let t=document.getElementById('gt'),td=t.rows[nr]?.cells[nx+pl];
if(td&&!td.classList.contains('e'))td.click()}

function setStatus(txt,cls){
  let s=document.getElementById('sb');
  s.className='status'+(cls?(' '+cls):'');
  document.getElementById('sbtxt').textContent=txt;
}

function autoSave(){
setStatus('Saving…','saving');
fetch('/api/autosave',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({key:ck,rows:D[ck].rows})}).then(r=>r.json()).then(res=>{
if(res.ok){
  setStatus('Saved '+new Date().toLocaleTimeString());
  // update progress + sidebar
  let n=filledCount(D[ck].rows),pct=Math.round(100*n/TOTAL);
  document.getElementById('progPct').textContent=pct+'%';
  document.getElementById('progFill').style.width=pct+'%';
  buildSidebar();
}
else{setStatus('Error: '+res.error,'error')}})}

function prev(){if(ci>0)load(ks[ci-1])}
function next(){if(ci<ks.length-1)load(ks[ci+1])}

document.onkeydown=e=>{if(e.target.tagName==='INPUT')return;
if(e.key==='ArrowLeft')prev();if(e.key==='ArrowRight')next()};

function openUpload(){document.getElementById('uploadModal').classList.add('show');setTimeout(()=>document.getElementById('upSubj').focus(),100)}
function closeUpload(){document.getElementById('uploadModal').classList.remove('show')}

function doUpload(){
let subj=document.getElementById('upSubj').value.trim();
let eye=document.getElementById('upEye').value;
let file=document.getElementById('upFile').files[0];
if(!subj||!file){alert('Subject ID and image are required.');return}
let fd=new FormData();fd.append('subject',subj);fd.append('eye',eye);fd.append('file',file);
setStatus('Uploading…','saving');
fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(res=>{
if(res.ok){
  setStatus('Uploaded ✓');
  closeUpload();
  // reset form
  document.getElementById('upSubj').value='';
  document.getElementById('upFile').value='';
  document.getElementById('fileLabelText').textContent='Click to choose a file…';
  document.getElementById('fileLabel').classList.remove('has-file');
  refresh();
  setTimeout(()=>{let key=subj+'_'+eye;if(D[key])load(key)},300)
}
else{setStatus('Upload error: '+res.error,'error')}})}

// close modal on outside click
document.getElementById('uploadModal').onclick=e=>{
  if(e.target.id==='uploadModal')closeUpload();
};

init();
</script>
</body></html>"""


# ───────────────────────── HTTP server ───────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # quiet by default; flip to ``super().log_message`` for verbose
        pass

    # ----- GET -----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._respond(200, "text/html; charset=utf-8", HTML_PAGE.encode())
        elif parsed.path == "/api/data":
            self._respond(200, "application/json",
                          json.dumps(LIVE_DATA).encode())
        elif parsed.path == "/api/image":
            params = urllib.parse.parse_qs(parsed.query)
            key = params.get("key", [""])[0]
            path = IMAGE_PATHS.get(key)
            if path and os.path.exists(path):
                ext = os.path.splitext(path)[1].lower()
                ct = "image/png" if ext == ".png" else "image/jpeg"
                with open(path, "rb") as f:
                    self._respond(200, ct, f.read())
                return
            self._respond(404, "text/plain", b"image not found")
        elif parsed.path == "/health":
            self._respond(200, "text/plain", b"ok")
        else:
            self._respond(404, "text/plain", b"not found")

    # ----- POST -----
    def do_POST(self):
        if self.path == "/api/autosave":
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(length))
                key  = body["key"]
                if key not in LIVE_DATA:
                    LIVE_DATA[key] = {
                        "subject": key.rsplit("_", 1)[0],
                        "eye":     key.rsplit("_", 1)[1],
                        "age": "", "sex": "",
                        "rows": _empty_rows(),
                    }
                LIVE_DATA[key]["rows"] = body["rows"]
                save_all()
                self._respond(200, "application/json",
                              json.dumps({"ok": True}).encode())
            except Exception as e:
                self._respond(500, "application/json",
                              json.dumps({"ok": False, "error": str(e)}).encode())
            return

        if self.path == "/api/upload":
            try:
                ct = self.headers.get("Content-Type", "")
                if not ct.startswith("multipart/form-data"):
                    raise ValueError("expected multipart/form-data")
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length)
                # Hand off to stdlib email parser to walk the multipart payload.
                msg = BytesParser(policy=email_default_policy).parsebytes(
                    b"Content-Type: " + ct.encode() + b"\r\n\r\n" + raw
                )
                fields = {}
                file_part = None
                file_name = None
                for part in msg.iter_parts():
                    disp = part.get("Content-Disposition", "")
                    m = re.search(r'name="([^"]+)"', disp)
                    if not m:
                        continue
                    name = m.group(1)
                    fn = re.search(r'filename="([^"]+)"', disp)
                    if fn:
                        file_part = part.get_payload(decode=True)
                        file_name = fn.group(1)
                    else:
                        val = part.get_payload(decode=True)
                        if isinstance(val, bytes):
                            val = val.decode("utf-8", errors="replace")
                        fields[name] = (val or "").strip()
                subject = fields.get("subject", "")
                eye     = fields.get("eye", "OD").upper()
                if not subject or not re.match(r"^[A-Za-z0-9._\-]+$", subject):
                    raise ValueError("subject must be non-empty and contain only A-Z, 0-9, dot, dash or underscore")
                if eye not in ("OD", "OS"):
                    raise ValueError("eye must be OD or OS")
                if not file_part or not file_name:
                    raise ValueError("file missing")
                ext = os.path.splitext(file_name)[1].lower()
                if ext not in ALLOWED_EXT:
                    raise ValueError(f"unsupported file extension: {ext}")

                _ensure_dirs()
                dest = os.path.join(IMAGES_DIR, f"{subject}_{eye}{ext}")
                # Remove any stale variants under a different extension.
                for other in ALLOWED_EXT:
                    alt = os.path.join(IMAGES_DIR, f"{subject}_{eye}{other}")
                    if other != ext and os.path.exists(alt):
                        try: os.remove(alt)
                        except OSError: pass
                with open(dest, "wb") as f:
                    f.write(file_part)

                discover_subjects()
                self._respond(200, "application/json",
                              json.dumps({"ok": True, "subject": subject, "eye": eye}).encode())
            except Exception as e:
                self._respond(500, "application/json",
                              json.dumps({"ok": False, "error": str(e)}).encode())
            return

        self._respond(404, "text/plain", b"not found")

    def _respond(self, code, content_type, data):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ───────────────────────────── entry point ─────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8766)))
    args = parser.parse_args()

    _ensure_dirs()
    load_persisted()
    discover_subjects()

    print(f"  HFA 24-2 TD Editor")
    print(f"  DATA_DIR : {DATA_DIR}")
    print(f"  Bind     : http://{args.host}:{args.port}")
    print(f"  Subjects : {len(LIVE_DATA)} (auto-discovered + persisted)")
    print(f"  Saves to : {OUT_CSV}")
    print(f"             {OUT_JSON}")
    print(f"  Ctrl+C to stop.")

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
