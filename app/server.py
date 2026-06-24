#!/usr/bin/env python3
"""Browser-based HFA 24-2 Total Deviation grid editor.

Left pane:  zoomable / pannable original perimetry report image
Right pane: editable, colour-coded 24-2 TD grid (keyboard-first data entry)
Auto-save:  every edit immediately writes to CSV + JSON (atomic, thread-safe)

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

HTTP API:
  GET  /                 the single-page editor
  GET  /api/data         all subjects as JSON
  GET  /api/image?key=…  the report image for a subject/eye
  POST /api/autosave     persist one grid (rows) and optional age/sex
  POST /api/upload       multipart upload of a report image
  POST /api/delete       remove a subject (its edits and image)
  GET  /health           liveness probe
"""
import argparse
import csv
import http.server
import io
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import default as email_default_policy

sys.path.insert(0, os.path.dirname(__file__))
from hvf_24_2 import (
    ROWS_24_2,
    get_vf_x, eccentricity, quadrant_anatomical,
)

VERSION     = "1.0.0"
DATA_DIR    = os.environ.get("DATA_DIR", os.path.join(os.getcwd(), "data"))
IMAGES_DIR  = os.path.join(DATA_DIR, "images")
OUT_DIR     = os.path.join(DATA_DIR, "extracted")
OUT_CSV     = os.path.join(OUT_DIR, "td_54point.csv")
OUT_JSON    = os.path.join(OUT_DIR, "td_grids.json")

ALLOWED_EXT = (".jpg", ".jpeg", ".png")
NAME_RE     = re.compile(r"^(?P<sid>[A-Za-z0-9._\-]+)_(?P<eye>OD|OS)\.(?P<ext>jpg|jpeg|png)$",
                         re.IGNORECASE)
SUBJECT_RE  = re.compile(r"^[A-Za-z0-9._\-]+$")
MAX_UPLOAD  = 25 * 1024 * 1024   # 25 MB ceiling for an uploaded report image

LIVE_DATA = {}        # {f"{sid}_{eye}": {"subject":..., "eye":..., "rows":[[...]]}}
IMAGE_PATHS = {}      # {f"{sid}_{eye}": "/abs/path/to/img"}
_LOCK = threading.RLock()   # guards LIVE_DATA / IMAGE_PATHS mutation + disk writes


# ───────────────────────── data store ───────────────────────────
def _ensure_dirs():
    os.makedirs(IMAGES_DIR, exist_ok=True)
    os.makedirs(OUT_DIR,    exist_ok=True)


def _empty_rows():
    return [[None] * len(xs) for _, xs in ROWS_24_2]


def discover_subjects():
    """Scan IMAGES_DIR and refresh IMAGE_PATHS + LIVE_DATA shells."""
    with _LOCK:
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
            with _LOCK:
                LIVE_DATA.clear()
                LIVE_DATA.update(data)
            print(f"  Loaded {len(LIVE_DATA)} previously-edited entries from {OUT_JSON}")
    except Exception as e:
        print(f"  Warning: could not load {OUT_JSON} ({e})")


def _atomic_write_text(path, text):
    """Write ``text`` to ``path`` atomically (temp file + os.replace)."""
    dirn = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_all():
    """Serialise LIVE_DATA to JSON + the flat 54-point CSV (atomic, locked)."""
    fieldnames = [
        "subject", "eye", "age", "sex",
        "row", "col", "x_vf_deg", "y_vf_deg",
        "eccentricity_deg", "quadrant", "td_dB",
    ]
    with _LOCK:
        _ensure_dirs()
        json_text = json.dumps(LIVE_DATA, indent=2)

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames)
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

        _atomic_write_text(OUT_JSON, json_text)
        _atomic_write_text(OUT_CSV, buf.getvalue())


# ───────────────────────── HTML page ───────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Perimetry Editor — HFA 24-2 Total Deviation</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  /* clean clinical light palette */
  --bg:#eef1f6;
  --panel:#ffffff;
  --panel-2:#f8fafc;
  --ink:#0f172a;
  --ink-2:#475569;
  --ink-3:#94a3b8;
  --line:#e6eaf0;
  --line-2:#d6dde7;
  --brand:#1d6fe0;
  --brand-ink:#1557b0;
  --brand-soft:#eaf2ff;
  --focus:rgba(29,111,224,.30);
  /* severity bands (light, dark-text, calm clinical heatmap) */
  --n-bg:#dcf5e7;  --n-bd:#86e3b4;  --n-ink:#10663d;
  --m-bg:#fdf3cf;  --m-bd:#f2d574;  --m-ink:#8a5b09;
  --o-bg:#ffe2cc;  --o-bd:#fbb277;  --o-ink:#9a3d10;
  --s-bg:#fcdcdc;  --s-bd:#f3a3a3;  --s-ink:#9b1c1c;
  --bs-bg:#eef1f5; --bs-ink:#64748b;
  --shadow:0 1px 2px rgba(15,23,42,.04),0 8px 24px -12px rgba(15,23,42,.18);
  --shadow-sm:0 1px 2px rgba(15,23,42,.06);
  --ease:cubic-bezier(.22,.61,.36,1);
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{
  font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI','Roboto',sans-serif;
  background:var(--bg);color:var(--ink);
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale;
  letter-spacing:-.005em;
}
button,input,select{font-family:inherit}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-feature-settings:'tnum'}

/* ─── top bar ─── */
.topbar{
  height:58px;display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;background:var(--panel);
  border-bottom:1px solid var(--line);position:relative;z-index:30;
}
.brand{display:flex;align-items:center;gap:11px}
.brand .logo{
  width:34px;height:34px;border-radius:10px;color:#fff;
  background:linear-gradient(135deg,#2e82ff 0%,#1456c7 100%);
  display:grid;place-items:center;box-shadow:0 4px 12px -3px rgba(29,111,224,.5);
}
.brand .title{font-weight:700;font-size:15px;letter-spacing:-.02em;line-height:1.1}
.brand .sub{font-size:11px;color:var(--ink-3);margin-top:2px}
.topbar .actions{display:flex;gap:8px;align-items:center}

.btn{
  height:36px;padding:0 14px;border-radius:9px;border:1px solid var(--line-2);
  background:var(--panel);color:var(--ink);font-size:13px;font-weight:500;
  cursor:pointer;display:inline-flex;align-items:center;gap:6px;
  transition:all .14s var(--ease);box-shadow:var(--shadow-sm);
}
.btn:hover:not(:disabled){border-color:#b9c4d2;background:var(--panel-2)}
.btn:disabled{opacity:.4;cursor:not-allowed;box-shadow:none}
.btn.icon{width:36px;padding:0;justify-content:center;font-size:17px;color:var(--ink-2)}
.btn.primary{
  background:linear-gradient(135deg,#2e82ff,#1d6fe0);border-color:transparent;
  color:#fff;font-weight:600;box-shadow:0 4px 12px -3px rgba(29,111,224,.45);
}
.btn.primary:hover:not(:disabled){filter:brightness(1.05);transform:translateY(-1px)}

/* ─── shell ─── */
.shell{display:grid;grid-template-columns:268px 1fr;height:calc(100vh - 58px)}

/* ─── sidebar ─── */
.sidebar{background:var(--panel);border-right:1px solid var(--line);
  display:flex;flex-direction:column;overflow:hidden}
.sidebar .head{
  padding:15px 16px 11px;display:flex;justify-content:space-between;align-items:center;
}
.sidebar .head .label{font-size:11px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:.09em}
.sidebar .head .count{font-size:11px;font-weight:600;color:var(--ink-2);background:var(--panel-2);border:1px solid var(--line);padding:2px 9px;border-radius:20px}
.subjects{list-style:none;padding:4px 10px 10px;overflow-y:auto;flex:1}
.subjects::-webkit-scrollbar{width:8px}
.subjects::-webkit-scrollbar-thumb{background:#d5dce6;border-radius:8px;border:2px solid var(--panel)}
.subjects li{
  position:relative;padding:9px 10px 9px 12px;border-radius:10px;cursor:pointer;
  display:flex;align-items:center;gap:10px;font-size:13px;margin-bottom:2px;
  border:1px solid transparent;transition:background .12s var(--ease);
}
.subjects li:hover{background:var(--panel-2)}
.subjects li.active{background:var(--brand-soft);border-color:#c9defc}
.subjects li.active::before{
  content:"";position:absolute;left:-1px;top:9px;bottom:9px;width:3px;
  background:var(--brand);border-radius:0 3px 3px 0;
}
.subjects .name{flex:1;font-weight:600;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.subjects li.active .name{color:var(--brand-ink)}
.subjects .eye{font-size:10px;font-weight:600;color:var(--ink-2);background:var(--panel-2);border:1px solid var(--line-2);padding:2px 6px;border-radius:6px;letter-spacing:.04em}
.ring{width:24px;height:24px;flex-shrink:0;position:relative}
.ring svg{transform:rotate(-90deg);display:block}
.ring .pct{position:absolute;inset:0;display:grid;place-items:center;font-size:8.5px;font-weight:700;color:var(--ink-3)}
.ring.done .pct{color:#10663d}
.del{
  width:22px;height:22px;flex-shrink:0;border:none;background:transparent;cursor:pointer;
  border-radius:6px;color:var(--ink-3);font-size:14px;display:none;place-items:center;
}
.subjects li:hover .del{display:grid}
.del:hover{background:#fdeaea;color:#dc2626}
.sidebar .foot{padding:11px 14px;border-top:1px solid var(--line);font-size:11px;color:var(--ink-3);display:flex;justify-content:space-between}
.kbd{display:inline-flex;align-items:center;font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;padding:2px 6px;border-radius:5px;border:1px solid var(--line-2);background:var(--panel-2);color:var(--ink-2);margin:0 1px}

/* ─── main area ─── */
.main{display:grid;grid-template-columns:1fr 470px;height:100%;overflow:hidden;gap:16px;padding:16px}

/* left image card */
.imgcard{
  position:relative;border-radius:16px;overflow:hidden;background:#11161f;
  border:1px solid var(--line);box-shadow:var(--shadow);cursor:grab;
}
.imgcard:active{cursor:grabbing}
.imgcard img{position:absolute;transform-origin:0 0;max-width:none;user-select:none;-webkit-user-drag:none}
.empty-state{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#9aa6b6;padding:40px;text-align:center;gap:14px;background:var(--panel-2)}
.empty-state .glyph{width:66px;height:66px;border-radius:18px;background:var(--brand-soft);color:var(--brand);display:grid;place-items:center;border:1px solid #cfe0fb}
.empty-state h3{font-size:16px;font-weight:700;color:var(--ink)}
.empty-state p{font-size:13px;max-width:330px;line-height:1.55;color:var(--ink-2)}

.zoom-pill{
  position:absolute;bottom:14px;right:14px;z-index:10;display:flex;gap:1px;padding:4px;
  background:rgba(255,255,255,.94);backdrop-filter:blur(8px);
  border-radius:12px;border:1px solid var(--line-2);box-shadow:var(--shadow);
}
.zoom-pill button{
  border:none;background:transparent;color:var(--ink-2);width:34px;height:32px;border-radius:8px;
  cursor:pointer;font-size:15px;display:grid;place-items:center;transition:background .12s;
}
.zoom-pill button:hover{background:var(--panel-2);color:var(--ink)}
.zoom-pill .sep{width:1px;background:var(--line-2);margin:5px 3px}

/* right grid card */
.gridcard{
  background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:18px 18px 14px;display:flex;flex-direction:column;gap:14px;
  box-shadow:var(--shadow);overflow:hidden;
}
.header-row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.who{display:flex;flex-direction:column;gap:7px;min-width:0}
.who .name{font-size:19px;font-weight:700;letter-spacing:-.02em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.who .meta{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.who .meta .eye-pill{padding:3px 9px;border-radius:7px;background:var(--brand-soft);color:var(--brand-ink);border:1px solid #cfe0fb;font-weight:700;font-size:11px;letter-spacing:.04em}
.who .meta .tag{padding:3px 9px;border-radius:7px;background:var(--panel-2);border:1px solid var(--line);color:var(--ink-2);font-size:11px;font-weight:600}
.meta-field{display:inline-flex;align-items:center;gap:4px;background:var(--panel-2);border:1px solid var(--line-2);border-radius:7px;padding:1px 4px 1px 8px}
.meta-field span{font-size:10px;font-weight:700;color:var(--ink-3);text-transform:uppercase;letter-spacing:.05em}
.meta-field input{width:42px;border:none;background:transparent;color:var(--ink);font-size:12px;font-weight:600;outline:none;padding:4px 2px}
.meta-field input::placeholder{color:var(--ink-3);font-weight:500}

.progress-wrap{flex-shrink:0;text-align:right;min-width:78px}
.progress-wrap .pct{font-size:23px;font-weight:700;color:var(--brand);font-feature-settings:'tnum';line-height:1}
.progress-wrap .pct.done{color:#10663d}
.progress-wrap .plabel{font-size:10px;color:var(--ink-3);text-transform:uppercase;letter-spacing:.08em;margin-top:3px}
.progress-bar{height:5px;border-radius:4px;background:var(--line);overflow:hidden;margin-top:7px}
.progress-bar .fill{height:100%;background:linear-gradient(90deg,#2e82ff,#1d6fe0);border-radius:4px;transition:width .3s var(--ease)}
.progress-bar .fill.done{background:linear-gradient(90deg,#34d399,#10b981)}

/* legend strip */
.legend{display:flex;flex-wrap:wrap;gap:6px;font-size:10.5px}
.legend .chip{padding:3px 9px;border-radius:7px;font-weight:600;display:inline-flex;align-items:center;gap:6px;border:1px solid transparent}
.legend .chip .dot{width:9px;height:9px;border-radius:3px;border:1px solid rgba(0,0,0,.08)}
.legend .chip.normal{background:var(--n-bg);color:var(--n-ink);border-color:var(--n-bd)}
.legend .chip.normal .dot{background:#34c97f}
.legend .chip.borderline{background:var(--m-bg);color:var(--m-ink);border-color:var(--m-bd)}
.legend .chip.borderline .dot{background:#eab308}
.legend .chip.moderate{background:var(--o-bg);color:var(--o-ink);border-color:var(--o-bd)}
.legend .chip.moderate .dot{background:#f97316}
.legend .chip.severe{background:var(--s-bg);color:var(--s-ink);border-color:var(--s-bd)}
.legend .chip.severe .dot{background:#dc2626}
.legend .chip.bs{background:var(--bs-bg);color:var(--bs-ink);border-color:var(--line-2)}
.legend .chip.bs .dot{background:#94a3b8}
.legend .chip.missing{background:var(--panel-2);color:var(--ink-3);border:1px dashed var(--line-2)}

/* the actual grid */
.gridwrap{display:flex;justify-content:center;align-items:center;flex:1;padding:6px 0}
table.g{border-collapse:separate;border-spacing:5px}
table.g td{
  width:44px;height:38px;text-align:center;border-radius:10px;cursor:pointer;
  font-family:'JetBrains Mono',ui-monospace,monospace;font-size:14px;font-weight:700;
  font-feature-settings:'tnum';border:1px solid transparent;
  transition:transform .1s var(--ease),box-shadow .1s var(--ease),filter .1s;position:relative;
}
table.g td:not(.e):hover{transform:translateY(-1px);filter:brightness(.97);z-index:5;box-shadow:var(--shadow-sm)}
table.g td.e{background:transparent !important;border-color:transparent !important;cursor:default}
table.g td.e:hover{transform:none;filter:none;box-shadow:none}
table.g td.focus{outline:2.5px solid var(--brand);outline-offset:1px;box-shadow:0 0 0 4px var(--focus);z-index:6}
table.g td input{
  width:100%;height:100%;border:none;background:transparent;text-align:center;
  font:inherit;color:inherit;outline:none;padding:0;
}
/* severity cell variants */
.sn{background:var(--n-bg);border-color:var(--n-bd);color:var(--n-ink)}
.sm{background:var(--m-bg);border-color:var(--m-bd);color:var(--m-ink)}
.so{background:var(--o-bg);border-color:var(--o-bd);color:var(--o-ink)}
.ss{background:var(--s-bg);border-color:var(--s-bd);color:var(--s-ink)}
.sb{background:var(--bs-bg);border-color:var(--line-2);color:var(--bs-ink);font-size:11px;
  background-image:repeating-linear-gradient(45deg,transparent,transparent 5px,rgba(100,116,139,.10) 5px,rgba(100,116,139,.10) 10px)}
.sq{background:var(--panel-2);color:var(--ink-3);border:1px dashed var(--line-2)}
.sq:hover{border-color:var(--brand);color:var(--brand)}

/* tip footer */
.tip{font-size:11px;color:var(--ink-3);text-align:center;line-height:1.7;border-top:1px solid var(--line);padding-top:11px}
.tip .row{display:flex;justify-content:center;gap:13px;flex-wrap:wrap}

/* ─── status toast ─── */
.status{
  position:fixed;left:50%;transform:translateX(-50%);bottom:16px;z-index:50;
  padding:8px 16px;border-radius:11px;font-size:12px;font-weight:600;
  background:var(--panel);border:1px solid var(--line-2);box-shadow:var(--shadow);
  display:inline-flex;align-items:center;gap:8px;color:var(--ink-2);
  transition:all .2s var(--ease);
}
.status .dot{width:8px;height:8px;border-radius:50%;background:#10b981}
.status.saving{color:#8a5b09;border-color:var(--m-bd)}
.status.saving .dot{background:#eab308;animation:pulse 1.1s infinite}
.status.error{color:#9b1c1c;border-color:var(--s-bd)}
.status.error .dot{background:#dc2626}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}

/* ─── upload modal ─── */
.modal-bg{position:fixed;inset:0;background:rgba(15,23,42,.34);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:100;animation:fadein .18s ease}
.modal-bg.show{display:flex}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.modal{width:430px;max-width:92vw;background:var(--panel);border:1px solid var(--line-2);
  border-radius:18px;padding:26px;box-shadow:0 24px 60px -16px rgba(15,23,42,.35);animation:slideup .22s var(--ease)}
@keyframes slideup{from{transform:translateY(10px);opacity:0}to{transform:none;opacity:1}}
.modal h3{font-size:18px;font-weight:700;letter-spacing:-.02em;margin-bottom:5px}
.modal .desc{font-size:12.5px;color:var(--ink-2);margin-bottom:16px;line-height:1.5}
.modal .desc code{background:var(--panel-2);border:1px solid var(--line);padding:1px 5px;border-radius:5px;font-size:11.5px}
.modal label{display:block;font-size:11px;font-weight:700;color:var(--ink-2);text-transform:uppercase;letter-spacing:.05em;margin:15px 0 6px}
.modal input[type=text],.modal select{width:100%;padding:10px 12px;border-radius:9px;border:1px solid var(--line-2);
  background:var(--panel);color:var(--ink);font-size:13.5px;transition:border-color .12s,box-shadow .12s}
.modal input[type=text]:focus,.modal select:focus{outline:none;border-color:var(--brand);box-shadow:0 0 0 3px var(--focus)}
.modal .filebox{border:1.5px dashed var(--line-2);border-radius:11px;padding:18px;text-align:center;cursor:pointer;
  color:var(--ink-2);font-size:12.5px;background:var(--panel-2);transition:all .14s var(--ease)}
.modal .filebox:hover{border-color:var(--brand);background:var(--brand-soft);color:var(--brand-ink)}
.modal .filebox.has-file{border-color:#10b981;background:var(--n-bg);color:var(--n-ink)}
.modal .filebox input{display:none}
.modal .actions{display:flex;justify-content:flex-end;gap:8px;margin-top:22px}
.modal .actions .btn.primary{padding:0 22px}

/* responsive */
@media(max-width:1080px){
  .main{grid-template-columns:1fr;grid-template-rows:1fr auto}
  .gridcard{max-height:none}
}
@media(max-width:720px){
  .shell{grid-template-columns:1fr}
  .sidebar{display:none}
}
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <div class="logo">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round">
        <circle cx="12" cy="12" r="9.2"/><circle cx="12" cy="12" r="4.4"/>
        <line x1="12" y1="1.4" x2="12" y2="5.6"/><line x1="12" y1="18.4" x2="12" y2="22.6"/>
        <line x1="1.4" y1="12" x2="5.6" y2="12"/><line x1="18.4" y1="12" x2="22.6" y2="12"/>
      </svg>
    </div>
    <div>
      <div class="title">Perimetry Editor</div>
      <div class="sub">HFA 24-2 Total Deviation · auto-save</div>
    </div>
  </div>
  <div class="actions">
    <button class="btn icon" onclick="prev()" id="pb" title="Previous subject (←)">‹</button>
    <button class="btn icon" onclick="next()" id="nb" title="Next subject (→)">›</button>
    <button class="btn" onclick="reveal()" id="folderBtn" title="Open the folder where your data is saved">📁 Data folder</button>
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
      <span><span class="kbd">←</span><span class="kbd">→</span> subject</span>
      <span><span class="kbd">Tab</span> next cell</span>
    </div>
  </aside>

  <!-- main -->
  <div class="main">
    <!-- image -->
    <div class="imgcard" id="ip">
      <img id="img" src="" draggable="false" style="display:none">
      <div class="empty-state" id="emptyMsg">
        <div class="glyph">
          <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 5a2 2 0 0 1 2-2h8l6 6v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Z"/><path d="M14 3v6h6"/>
          </svg>
        </div>
        <h3>No subjects yet</h3>
        <p>Click <strong>＋ Upload report</strong> to add a Humphrey 24-2 single-field analysis image and start correcting OCR errors.</p>
      </div>
      <div class="zoom-pill" id="zoomPill" style="display:none">
        <button onclick="zi()" title="Zoom in">+</button>
        <button onclick="zo()" title="Zoom out">−</button>
        <button onclick="zf()" title="Fit to view">⤢</button>
        <div class="sep"></div>
        <button onclick="rot(-90)" title="Rotate left 90°">↺</button>
        <button onclick="rot(90)" title="Rotate right 90°">↻</button>
        <button onclick="flipH()" title="Flip horizontal">⇆</button>
      </div>
    </div>

    <!-- grid -->
    <div class="gridcard">
      <div class="header-row" id="hdrRow" style="display:none">
        <div class="who">
          <div class="name" id="whoName"></div>
          <div class="meta">
            <span class="eye-pill" id="whoEye"></span>
            <span class="tag">Total Deviation · dB</span>
            <label class="meta-field"><span>Age</span><input id="ageIn" inputmode="numeric" maxlength="3" placeholder="—"></label>
            <label class="meta-field"><span>Sex</span><input id="sexIn" maxlength="6" placeholder="—"></label>
          </div>
        </div>
        <div class="progress-wrap">
          <div class="pct" id="progPct">—</div>
          <div class="plabel">filled</div>
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
          <span><span class="kbd">click</span> / <span class="kbd">type</span> edit</span>
          <span><span class="kbd">Enter</span> save + next</span>
          <span><span class="kbd">↑</span><span class="kbd">↓</span> move</span>
          <span><span class="kbd">Esc</span> cancel</span>
        </div>
        <div style="margin-top:5px;color:var(--ink-3)">Type <strong>BS</strong> (or <strong>B</strong>) for blind spot · <strong>?</strong> or empty for missing</div>
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
"use strict";
// 24-2 row widths and the shared padding used for BOTH rendering and navigation
// (OD padding aligns each point to its true x-position; OS is centred).
const RS=[4,6,8,9,9,8,6,4], MC=9, BSR=4, BSC=7;
const OD_PAD=[3,2,1,0,0,1,2,3];
const OS_PAD=[2,1,0,0,0,0,1,2];
const TOTAL=RS.reduce((a,b)=>a+b,0);   // 54

let D={}, ck="", ci=0, ks=[];
let sc=1, sx=0, sy=0, drag=false, dsx, dsy, dix, diy, imgRot=0, imgFlipH=false;
let focusCell={r:0,c:0};

const $=id=>document.getElementById(id);
function padFor(eye){return eye==='OS'?OS_PAD:OD_PAD;}

function init(){
  const p=$('ip');
  p.onmousedown=e=>{drag=true;dsx=e.clientX;dsy=e.clientY;dix=sx;diy=sy};
  p.onmousemove=e=>{if(!drag)return;sx=dix+(e.clientX-dsx);sy=diy+(e.clientY-dsy);ut()};
  p.onmouseup=()=>drag=false;p.onmouseleave=()=>drag=false;
  p.onwheel=e=>{e.preventDefault();const d=e.deltaY>0?.9:1.1,r=p.getBoundingClientRect(),
    mx=e.clientX-r.left,my=e.clientY-r.top;sx=mx-(mx-sx)*d;sy=my-(my-sy)*d;sc*=d;ut()};
  const f=$('upFile'),fl=$('fileLabel'),flt=$('fileLabelText');
  f.onchange=()=>{if(f.files[0]){flt.textContent=f.files[0].name;fl.classList.add('has-file')}
    else{flt.textContent='Click to choose a file…';fl.classList.remove('has-file')}};
  $('ageIn').onchange=()=>{if(D[ck]){D[ck].age=$('ageIn').value.trim();saveDoc()}};
  $('sexIn').onchange=()=>{if(D[ck]){D[ck].sex=$('sexIn').value.trim();saveDoc()}};
  $('uploadModal').onclick=e=>{if(e.target.id==='uploadModal')closeUpload()};
  fetchInfo();
  refresh();
}

function fetchInfo(){
  fetch('/api/info').then(r=>r.json()).then(i=>{
    const b=$('folderBtn');
    if(b&&i.data_dir)b.title='Open the folder where your data is saved:\n'+i.data_dir;
  }).catch(()=>{});
}
function reveal(){
  fetch('/api/reveal',{method:'POST'}).then(r=>r.json()).then(res=>{
    if(res.ok)setStatus('Opened data folder');
    else setStatus('Could not open folder: '+res.error,'error');
  }).catch(e=>setStatus('Could not open folder: '+e,'error'));
}

function filledCount(rows){let c=0;for(const r of rows)for(const v of r)if(v!==null&&v!==undefined)c++;return c}

function buildSidebar(){
  const ul=$('subjects');ul.innerHTML='';
  $('subCount').textContent=ks.length;
  ks.forEach(k=>{
    const d=D[k],n=filledCount(d.rows),pct=Math.round(100*n/TOTAL),done=n>=TOTAL;
    const C=2*Math.PI*9;
    const li=document.createElement('li');
    if(k===ck)li.classList.add('active');
    li.onclick=()=>load(k);
    li.innerHTML=`<span class="name">${esc(d.subject)}</span>
      <span class="eye">${d.eye}</span>
      <span class="ring ${done?'done':''}">
        <svg width="24" height="24" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" fill="none" stroke="#e6eaf0" stroke-width="2.4"/>
          <circle cx="12" cy="12" r="9" fill="none" stroke="${done?'#10b981':'#1d6fe0'}"
            stroke-width="2.4" stroke-linecap="round"
            stroke-dasharray="${C.toFixed(2)}" stroke-dashoffset="${(C*(1-pct/100)).toFixed(2)}"/>
        </svg>
        <span class="pct">${done?'✓':pct}</span>
      </span>
      <button class="del" title="Delete subject">✕</button>`;
    li.querySelector('.del').onclick=e=>{e.stopPropagation();delSubject(k)};
    ul.appendChild(li);
  });
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

function refresh(){return fetch('/api/data').then(r=>r.json()).then(d=>{
  D=d;ks=Object.keys(d).sort();
  if(ks.length===0){
    $('emptyMsg').style.display='flex';$('img').style.display='none';
    $('zoomPill').style.display='none';$('hdrRow').style.display='none';
    $('gt').innerHTML='';buildSidebar();return;
  }
  $('emptyMsg').style.display='none';$('img').style.display='';
  $('zoomPill').style.display='flex';$('hdrRow').style.display='flex';
  load(ks[Math.min(Math.max(ci,0),ks.length-1)]);
})}

function ut(){const f=imgFlipH?'scaleX(-1)':'';
  $('img').style.transform=`translate(${sx}px,${sy}px) scale(${sc}) rotate(${imgRot}deg) ${f}`}
function zi(){sc*=1.3;ut()} function zo(){sc/=1.3;ut()}
function zf(){const p=$('ip'),i=$('img');if(!i.naturalWidth)return;
  sc=Math.min(p.clientWidth/i.naturalWidth,p.clientHeight/i.naturalHeight)*.95;
  sx=(p.clientWidth-i.naturalWidth*sc)/2;sy=(p.clientHeight-i.naturalHeight*sc)/2;ut()}
function rot(deg){imgRot=(imgRot+deg+360)%360;ut()}
function flipH(){imgFlipH=!imgFlipH;ut()}

function load(k){
  ck=k;ci=ks.indexOf(k);focusCell={r:0,c:0};
  $('pb').disabled=ci<=0;$('nb').disabled=ci>=ks.length-1;
  const d=D[k];
  $('whoName').textContent=d.subject;
  $('whoEye').textContent=d.eye;
  $('ageIn').value=d.age||'';
  $('sexIn').value=d.sex||'';
  updateProgress();
  imgRot=0;imgFlipH=false;
  const img=$('img');
  img.src='/api/image?key='+encodeURIComponent(k)+'&t='+Date.now();
  img.onload=()=>setTimeout(zf,40);
  img.onerror=()=>{img.src=''};
  buildGrid();buildSidebar();
}

function updateProgress(){
  if(!D[ck])return;
  const n=filledCount(D[ck].rows),pct=Math.round(100*n/TOTAL),done=n>=TOTAL;
  const el=$('progPct'),fill=$('progFill');
  el.textContent=pct+'%';el.classList.toggle('done',done);
  fill.style.width=pct+'%';fill.classList.toggle('done',done);
}

function sev(v){if(v===null||v===undefined)return'sq';if(v==='BS')return'sb';
  const n=parseInt(v,10);if(isNaN(n))return'sq';if(n>=0)return'sn';if(n>=-5)return'sm';if(n>=-15)return'so';return'ss'}
function disp(v){if(v===null||v===undefined)return'?';if(v==='BS')return'BS';return String(v)}

function buildGrid(){
  const t=$('gt');t.innerHTML='';
  const eye=D[ck]?.eye||'OD',pad=padFor(eye);
  for(let r=0;r<8;r++){
    const tr=document.createElement('tr'),nc=RS[r],pl=pad[r];
    for(let gc=0;gc<MC;gc++){
      const td=document.createElement('td'),di=gc-pl;
      if(di<0||di>=nc){td.className='e';tr.appendChild(td);continue}
      const v=D[ck].rows[r]&&D[ck].rows[r][di]!==undefined?D[ck].rows[r][di]:null;
      td.className=sev(v);td.textContent=disp(v);
      td.dataset.r=r;td.dataset.c=di;
      if(r===BSR&&di===BSC)td.title='Standard blind-spot position';
      td.onclick=()=>openCell(r,di);
      tr.appendChild(td);
    }
    t.appendChild(tr);
  }
  paintFocus();
}

function cellAt(r,di){
  const eye=D[ck]?.eye||'OD',pad=padFor(eye),t=$('gt');
  if(r<0||r>=8||di<0||di>=RS[r])return null;
  return t.rows[r]?.cells[di+pad[r]]||null;
}
function paintFocus(){
  document.querySelectorAll('table.g td.focus').forEach(td=>td.classList.remove('focus'));
  const td=cellAt(focusCell.r,focusCell.c);
  if(td&&!td.classList.contains('e')&&!td.querySelector('input'))td.classList.add('focus');
}

// next data cell in a direction; returns {r,c} or null
function moveFrom(r,di,dir){
  if(dir==='next'){di++;if(di>=RS[r]){r++;di=0}if(r>=8)return null;return{r,c:di}}
  if(dir==='prev'){di--;if(di<0){r--;if(r<0)return null;di=RS[r]-1}return{r,c:di}}
  if(dir==='down'||dir==='up'){
    const eye=D[ck]?.eye||'OD',pad=padFor(eye),gc=pad[r]+di,nr=r+(dir==='down'?1:-1);
    if(nr<0||nr>=8)return null;
    const ndi=Math.max(0,Math.min(RS[nr]-1,gc-pad[nr]));
    return{r:nr,c:ndi};
  }
  return null;
}

function openCell(r,di,prefill){
  const td=cellAt(r,di);
  if(!td||td.classList.contains('e')||td.querySelector('input'))return;
  focusCell={r,c:di};paintFocus();
  const oldDisp=td.textContent;
  let finished=false;
  const inp=document.createElement('input');
  inp.maxLength=4;
  inp.value=(prefill!==undefined)?prefill:(oldDisp==='?'?'':oldDisp);
  td.classList.remove('focus');td.textContent='';td.appendChild(inp);
  inp.focus();
  if(prefill!==undefined){const n=inp.value.length;inp.setSelectionRange(n,n)}else{inp.select()}

  function done(advance){
    if(finished)return;finished=true;
    const nt=inp.value.trim().toUpperCase();
    let nv;
    if(nt===''||nt==='?')nv=null;
    else if(nt==='BS'||nt==='B')nv='BS';
    else{const n=parseInt(nt,10);nv=isNaN(n)?null:n}
    if(td.contains(inp))td.removeChild(inp);
    td.textContent=disp(nv);td.className=sev(nv);
    if(r===BSR&&di===BSC)td.title='Standard blind-spot position';
    if(D[ck]&&D[ck].rows[r]){
      const old=D[ck].rows[r][di];
      if(old!==nv){D[ck].rows[r][di]=nv;saveDoc()}
    }
    if(advance){const m=moveFrom(r,di,advance);if(m){openCell(m.r,m.c);return}}
    focusCell={r,c:di};paintFocus();
  }
  function cancel(){if(finished)return;finished=true;
    if(td.contains(inp))td.removeChild(inp);td.textContent=oldDisp;focusCell={r,c:di};paintFocus()}

  inp.onkeydown=e=>{
    if(e.key==='Enter'){e.preventDefault();done('next')}
    else if(e.key==='Tab'){e.preventDefault();done(e.shiftKey?'prev':'next')}
    else if(e.key==='Escape'){e.preventDefault();cancel()}
    else if(e.key==='ArrowDown'){e.preventDefault();done('down')}
    else if(e.key==='ArrowUp'){e.preventDefault();done('up')}
  };
  inp.onblur=()=>done(null);
}

function saveDoc(){
  setStatus('Saving…','saving');
  fetch('/api/autosave',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({key:ck,rows:D[ck].rows,age:D[ck].age||'',sex:D[ck].sex||''})})
    .then(r=>r.json()).then(res=>{
      if(res.ok){setStatus('Saved '+new Date().toLocaleTimeString());updateProgress();buildSidebar()}
      else setStatus('Error: '+res.error,'error');
    }).catch(e=>setStatus('Error: '+e,'error'));
}

function delSubject(k){
  const d=D[k];if(!confirm(`Delete ${d.subject} (${d.eye})?\n\nThis removes its edits and the uploaded image. This cannot be undone.`))return;
  setStatus('Deleting…','saving');
  fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:k})})
    .then(r=>r.json()).then(res=>{
      if(res.ok){setStatus('Deleted');if(ci>0)ci--;refresh()}
      else setStatus('Error: '+res.error,'error');
    }).catch(e=>setStatus('Error: '+e,'error'));
}

function prev(){if(ci>0)load(ks[ci-1])}
function next(){if(ci<ks.length-1)load(ks[ci+1])}

function setStatus(txt,cls){$('sb').className='status'+(cls?(' '+cls):'');$('sbtxt').textContent=txt}

// global keys (when not typing in a field)
document.onkeydown=e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')return;
  if(e.key==='ArrowLeft'){prev();return}
  if(e.key==='ArrowRight'){next();return}
  if(!ks.length)return;
  if(e.key==='Enter'){e.preventDefault();openCell(focusCell.r,focusCell.c);return}
  if(/^[0-9]$/.test(e.key)||e.key==='-'||e.key==='b'||e.key==='B'||e.key==='?'){
    e.preventDefault();openCell(focusCell.r,focusCell.c,e.key);
  }
};

function openUpload(){$('uploadModal').classList.add('show');setTimeout(()=>$('upSubj').focus(),80)}
function closeUpload(){$('uploadModal').classList.remove('show')}

function doUpload(){
  const subj=$('upSubj').value.trim(),eye=$('upEye').value,file=$('upFile').files[0];
  if(!subj||!file){alert('Subject ID and image are required.');return}
  const fd=new FormData();fd.append('subject',subj);fd.append('eye',eye);fd.append('file',file);
  setStatus('Uploading…','saving');
  fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(res=>{
    if(res.ok){
      setStatus('Uploaded');closeUpload();
      $('upSubj').value='';$('upFile').value='';
      $('fileLabelText').textContent='Click to choose a file…';$('fileLabel').classList.remove('has-file');
      refresh().then(()=>{const key=subj+'_'+eye;if(D[key])load(key)});
    }else setStatus('Upload error: '+res.error,'error');
  }).catch(e=>setStatus('Upload error: '+e,'error'));
}

init();
</script>
</body></html>"""


# ───────────────────────── desktop helpers ───────────────────────────
def open_in_file_manager(path):
    """Reveal ``path`` in the OS file manager (used by the desktop build)."""
    system = platform.system()
    if system == "Windows":
        os.startfile(path)            # type: ignore[attr-defined]
    elif system == "Darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def build_server(host, port):
    """Construct the threaded HTTP server (shared by CLI + desktop launcher)."""
    return http.server.ThreadingHTTPServer((host, port), Handler)


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
            with _LOCK:
                payload = json.dumps(LIVE_DATA).encode()
            self._respond(200, "application/json", payload)
        elif parsed.path == "/api/image":
            params = urllib.parse.parse_qs(parsed.query)
            key = params.get("key", [""])[0]
            with _LOCK:
                path = IMAGE_PATHS.get(key)
            if path and os.path.exists(path):
                ext = os.path.splitext(path)[1].lower()
                ct = "image/png" if ext == ".png" else "image/jpeg"
                with open(path, "rb") as f:
                    self._respond(200, ct, f.read())
                return
            self._respond(404, "text/plain", b"image not found")
        elif parsed.path == "/api/info":
            info = {
                "data_dir": DATA_DIR,
                "version": VERSION,
                "frozen": bool(getattr(sys, "frozen", False)),
            }
            self._respond(200, "application/json", json.dumps(info).encode())
        elif parsed.path == "/health":
            self._respond(200, "text/plain", b"ok")
        else:
            self._respond(404, "text/plain", b"not found")

    # ----- POST -----
    def do_POST(self):
        if self.path == "/api/autosave":
            self._handle_autosave()
        elif self.path == "/api/delete":
            self._handle_delete()
        elif self.path == "/api/upload":
            self._handle_upload()
        elif self.path == "/api/reveal":
            self._handle_reveal()
        else:
            self._respond(404, "text/plain", b"not found")

    def _handle_reveal(self):
        try:
            _ensure_dirs()
            open_in_file_manager(DATA_DIR)
            self._respond(200, "application/json", json.dumps({"ok": True}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _handle_autosave(self):
        try:
            body = self._read_json_body()
            key = body["key"]
            with _LOCK:
                if key not in LIVE_DATA:
                    sid, _, eye = key.rpartition("_")
                    LIVE_DATA[key] = {
                        "subject": sid or key, "eye": eye or "OD",
                        "age": "", "sex": "", "rows": _empty_rows(),
                    }
                if "rows" in body:
                    LIVE_DATA[key]["rows"] = body["rows"]
                if "age" in body:
                    LIVE_DATA[key]["age"] = str(body["age"])
                if "sex" in body:
                    LIVE_DATA[key]["sex"] = str(body["sex"])
                save_all()
            self._respond(200, "application/json", json.dumps({"ok": True}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _handle_delete(self):
        try:
            body = self._read_json_body()
            key = body["key"]
            with _LOCK:
                LIVE_DATA.pop(key, None)
                path = IMAGE_PATHS.pop(key, None)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                save_all()
            self._respond(200, "application/json", json.dumps({"ok": True}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _handle_upload(self):
        try:
            ct = self.headers.get("Content-Type", "")
            if not ct.startswith("multipart/form-data"):
                raise ValueError("expected multipart/form-data")
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_UPLOAD:
                raise ValueError(f"upload too large (> {MAX_UPLOAD // (1024*1024)} MB)")
            raw = self.rfile.read(length)
            # Hand off to the stdlib email parser to walk the multipart payload.
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
            eye = fields.get("eye", "OD").upper()
            if not subject or not SUBJECT_RE.match(subject):
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
            # Remove any stale variant saved under a different extension.
            for other in ALLOWED_EXT:
                alt = os.path.join(IMAGES_DIR, f"{subject}_{eye}{other}")
                if other != ext and os.path.exists(alt):
                    try:
                        os.remove(alt)
                    except OSError:
                        pass
            with open(dest, "wb") as f:
                f.write(file_part)

            discover_subjects()
            self._respond(200, "application/json",
                          json.dumps({"ok": True, "subject": subject, "eye": eye}).encode())
        except Exception as e:
            self._respond(500, "application/json",
                          json.dumps({"ok": False, "error": str(e)}).encode())

    def _respond(self, code, content_type, data):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


# ───────────────────────────── entry point ─────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="HFA 24-2 Total Deviation grid editor")
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

    server = build_server(args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
