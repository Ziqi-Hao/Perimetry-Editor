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
<title>HFA 24-2 Total Deviation Editor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1a1a2e;color:#eee}
.header{background:#16213e;padding:8px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #0f3460;gap:10px;flex-wrap:wrap}
.header h1{font-size:16px;color:#e94560}
.nav-btn{background:#0f3460;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px;margin:0 3px}
.nav-btn:hover{background:#e94560}
.nav-btn:disabled{opacity:.3}
.upload-btn{background:#2d6a4f;color:#fff;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px}
.upload-btn:hover{background:#40916c}
.info{background:#16213e;padding:6px 20px;font-size:13px;display:flex;gap:16px;align-items:center;border-bottom:1px solid #0f3460;flex-wrap:wrap}
.info span{color:#aaa}.info .v{color:#fff;font-weight:bold}
.main{display:flex;height:calc(100vh - 110px)}
.left{flex:1;overflow:hidden;position:relative;background:#111;cursor:grab}
.left:active{cursor:grabbing}
.left img{position:absolute;transform-origin:0 0;max-width:none}
.left .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#666;font-size:14px;text-align:center;padding:20px}
.right{width:420px;padding:12px;overflow-y:auto;background:#1a1a2e;display:flex;flex-direction:column;align-items:center}
.zoom{position:absolute;bottom:8px;right:8px;z-index:10;display:flex;gap:3px}
.zoom button{background:rgba(15,52,96,.9);color:#fff;border:none;padding:5px 10px;border-radius:4px;cursor:pointer;font-size:14px}
.zoom button:hover{background:#e94560}
table.g{border-collapse:separate;border-spacing:2px;margin:8px auto}
table.g td{width:40px;height:30px;text-align:center;font-size:11px;font-weight:bold;border-radius:3px;cursor:pointer;border:2px solid transparent;transition:all .12s}
table.g td:hover{border-color:#e94560;transform:scale(1.1);z-index:2;position:relative}
table.g td.e{background:transparent;cursor:default;border:none}
table.g td.e:hover{transform:none;border:none}
table.g td.editing{border-color:#fff!important;box-shadow:0 0 8px rgba(233,69,96,.8)}
table.g td input{width:100%;height:100%;border:none;background:transparent;text-align:center;font-size:11px;font-weight:bold;color:inherit;outline:none}
.sn{background:#4caf50;color:#000}.sm{background:#ffd54f;color:#000}
.so{background:#ff9800;color:#000}.ss{background:#ef5350;color:#fff}
.sb{background:#555;color:#fff;font-size:9px}.sq{background:#333;color:#888}
.legend{display:flex;gap:6px;margin:6px 0;flex-wrap:wrap;justify-content:center}
.legend span{font-size:10px;padding:2px 6px;border-radius:3px}
.status-bar{background:#16213e;padding:6px 20px;border-top:1px solid #0f3460;font-size:12px;color:#4caf50;text-align:center;position:fixed;bottom:0;left:0;right:0;z-index:100}
.status-bar.saving{color:#ffd54f}
.status-bar.error{color:#ef5350}
select{background:#0f3460;color:#fff;border:1px solid #555;padding:5px 8px;border-radius:4px;font-size:13px;min-width:160px}
.bs-note{font-size:10px;color:#888;margin-top:4px;text-align:center}
.upload-modal{position:fixed;inset:0;background:rgba(0,0,0,.7);display:none;align-items:center;justify-content:center;z-index:200}
.upload-modal.show{display:flex}
.upload-modal .card{background:#16213e;padding:24px;border-radius:8px;width:380px;max-width:90vw;border:1px solid #0f3460}
.upload-modal h3{margin-bottom:12px;color:#e94560}
.upload-modal label{display:block;font-size:12px;color:#aaa;margin-top:10px}
.upload-modal input[type=text]{width:100%;margin-top:4px;padding:6px 8px;border-radius:4px;border:1px solid #555;background:#0f3460;color:#fff;font-size:13px}
.upload-modal select{width:100%;margin-top:4px}
.upload-modal input[type=file]{width:100%;margin-top:4px;color:#aaa}
.upload-modal .row{display:flex;gap:8px;margin-top:16px;justify-content:flex-end}
.upload-modal button{padding:6px 14px;border:none;border-radius:4px;cursor:pointer;font-size:13px}
.upload-modal .ok{background:#2d6a4f;color:#fff}
.upload-modal .cancel{background:#555;color:#fff}
</style>
</head>
<body>
<div class="header">
<h1>HFA 24-2 Total Deviation Editor (auto-save)</h1>
<div>
<button class="nav-btn" onclick="prev()" id="pb">&#8592;</button>
<select id="sel" onchange="load(this.value)"></select>
<button class="nav-btn" onclick="next()" id="nb">&#8594;</button>
<button class="upload-btn" onclick="openUpload()">+ Upload report</button>
</div>
</div>
<div class="info" id="info"></div>
<div class="main">
<div class="left" id="ip">
<img id="img" src="" draggable="false" style="display:none">
<div class="empty" id="emptyMsg">No subjects yet — click "Upload report" to add a perimetry image.</div>
<div class="zoom">
<button onclick="zi()">+</button>
<button onclick="zo()">-</button>
<button onclick="zf()">Fit</button>
<button onclick="rot(-90)" title="Rotate left 90°">↺</button>
<button onclick="rot(90)" title="Rotate right 90°">↻</button>
<button onclick="flipH()" title="Flip horizontal">⇔</button>
</div>
</div>
<div class="right">
<div class="legend">
<span class="sn">TD&ge;0 normal</span>
<span class="sm">-5 to -1 borderline</span>
<span class="so">-15 to -6 moderate</span>
<span class="ss">&lt;-15 severe</span>
<span class="sb">BS</span>
<span class="sq">? missing</span>
</div>
<table class="g" id="gt"></table>
<div class="bs-note">
Click cell to edit &middot; Enter=confirm &middot; Tab=next &middot; BS=blind spot &middot; ?=missing<br>
Values are Total Deviation (dB): negative = worse than age-matched normal
</div>
</div>
</div>
<div class="status-bar" id="sb">Ready</div>

<div class="upload-modal" id="uploadModal">
<div class="card">
<h3>Upload perimetry report</h3>
<label>Subject ID (any string, e.g. "patient_007")</label>
<input type="text" id="upSubj" placeholder="patient_007">
<label>Eye</label>
<select id="upEye"><option>OD</option><option>OS</option></select>
<label>Image file (jpg / jpeg / png)</label>
<input type="file" id="upFile" accept="image/jpeg,image/png,image/jpg">
<div class="row">
<button class="cancel" onclick="closeUpload()">Cancel</button>
<button class="ok"     onclick="doUpload()">Upload</button>
</div>
</div>
</div>

<script>
const RS=[4,6,8,9,9,8,6,4],MC=9,BSR=4,BSC=7;
let D={},ck="",ci=0,ks=[],sc=1,sx=0,sy=0,drag=false,dsx,dsy,dix,diy,imgRot=0,imgFlipH=false;

function init(){refresh();
let p=document.getElementById('ip');
p.onmousedown=e=>{drag=true;dsx=e.clientX;dsy=e.clientY;dix=sx;diy=sy};
p.onmousemove=e=>{if(!drag)return;sx=dix+(e.clientX-dsx);sy=diy+(e.clientY-dsy);ut()};
p.onmouseup=()=>drag=false;p.onmouseleave=()=>drag=false;
p.onwheel=e=>{e.preventDefault();let d=e.deltaY>0?.9:1.1,r=p.getBoundingClientRect(),
mx=e.clientX-r.left,my=e.clientY-r.top;sx=mx-(mx-sx)*d;sy=my-(my-sy)*d;sc*=d;ut()}}

function refresh(){fetch('/api/data').then(r=>r.json()).then(d=>{
D=d;ks=Object.keys(d).sort();
let s=document.getElementById('sel');s.innerHTML='';
ks.forEach(k=>{let o=document.createElement('option');o.value=k;
let m=d[k].rows.flat().filter(v=>v===null).length;
o.textContent=k.replace('_',' ')+(m>0?` (${m}?)`:' ✓');s.appendChild(o)});
if(ks.length===0){document.getElementById('emptyMsg').style.display='flex';
document.getElementById('img').style.display='none';
document.getElementById('info').innerHTML='';
document.getElementById('gt').innerHTML=''}
else{document.getElementById('emptyMsg').style.display='none';
document.getElementById('img').style.display='';
load(ks[Math.min(ci,ks.length-1)]||ks[0])}})}

function ut(){let f=imgFlipH?'scaleX(-1)':'';
document.getElementById('img').style.transform=`translate(${sx}px,${sy}px) scale(${sc}) rotate(${imgRot}deg) ${f}`}
function zi(){sc*=1.3;ut()}function zo(){sc/=1.3;ut()}
function zf(){let p=document.getElementById('ip'),i=document.getElementById('img');
if(!i.naturalWidth)return;
sc=Math.min(p.clientWidth/i.naturalWidth,p.clientHeight/i.naturalHeight)*.95;
sx=(p.clientWidth-i.naturalWidth*sc)/2;sy=(p.clientHeight-i.naturalHeight*sc)/2;ut()}
function rot(deg){imgRot=(imgRot+deg)%360;ut()}
function flipH(){imgFlipH=!imgFlipH;ut()}

function load(k){ck=k;ci=ks.indexOf(k);document.getElementById('sel').value=k;
document.getElementById('pb').disabled=ci===0;
document.getElementById('nb').disabled=ci===ks.length-1;
let d=D[k],info=document.getElementById('info');
info.innerHTML=`<span>Subject: <span class="v">${d.subject}</span></span>
<span>Eye: <span class="v">${d.eye}</span></span>
<span>Type: <span class="v" style="color:#e94560">Total Deviation (dB)</span></span>`;
imgRot=0;imgFlipH=false;
let img=document.getElementById('img');
img.src='/api/image?key='+encodeURIComponent(k)+'&t='+Date.now();
img.onload=()=>setTimeout(zf,50);
img.onerror=()=>{img.src='';};
bg(d.rows)}

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
if(ri===BSR&&di===BSC){td.style.outline='2px dashed #fff';td.title='Standard BS position'}
td.onclick=()=>ed(td,ri,di);tr.appendChild(td)}t.appendChild(tr)}}

function ed(td,row,col){if(td.querySelector('input'))return;
let ov=td.textContent;td.classList.add('editing');
let inp=document.createElement('input');inp.value=ov==='?'?'':ov;inp.maxLength=4;
td.textContent='';td.appendChild(inp);inp.focus();inp.select();
let fin=()=>{let nt=inp.value.trim().toUpperCase();td.classList.remove('editing');
td.removeChild(inp);let nv;
if(nt===''||nt==='?'){nv=null;nt='?'}else if(nt==='BS'){nv='BS'}
else{let n=parseInt(nt);if(!isNaN(n)){nv=n;nt=String(n)}else{nv=null;nt='?'}}
td.textContent=nt==='?'?'?':nt;td.className=sc2(nv);
if(row===BSR&&col===BSC){td.style.outline='2px dashed #fff';td.title='Standard BS position'}
if(D[ck]&&D[ck].rows[row]){let old=D[ck].rows[row][col];
if(old!==nv){D[ck].rows[row][col]=nv;autoSave()}}};
inp.onkeydown=e=>{if(e.key==='Enter')fin();
if(e.key==='Escape'){td.classList.remove('editing');td.removeChild(inp);td.textContent=ov}
if(e.key==='Tab'){e.preventDefault();fin();mn(row,col,e.shiftKey)}};
inp.onblur=fin}

function mn(r,c,bk){let nc=RS[r],nr=r,nx=c+(bk?-1:1);
if(nx>=nc){nr++;nx=0}if(nx<0){nr--;nx=nr>=0?RS[nr]-1:0}
if(nr<0||nr>=8)return;let pl=Math.floor((MC-RS[nr])/2);
let t=document.getElementById('gt'),td=t.rows[nr]?.cells[nx+pl];
if(td&&!td.classList.contains('e'))td.click()}

function autoSave(){
let sb=document.getElementById('sb');sb.textContent='Saving...';sb.className='status-bar saving';
fetch('/api/autosave',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({key:ck,rows:D[ck].rows})}).then(r=>r.json()).then(res=>{
if(res.ok){sb.textContent='Saved ✓ ('+new Date().toLocaleTimeString()+')';sb.className='status-bar';
let sel=document.getElementById('sel'),idx=ks.indexOf(ck);
let m=D[ck].rows.flat().filter(v=>v===null).length;
sel.options[idx].textContent=ck.replace('_',' ')+(m>0?` (${m}?)`:' ✓')}
else{sb.textContent='Error: '+res.error;sb.className='status-bar error'}})}

function prev(){if(ci>0)load(ks[ci-1])}function next(){if(ci<ks.length-1)load(ks[ci+1])}

document.onkeydown=e=>{if(e.target.tagName==='INPUT')return;
if(e.key==='ArrowLeft')prev();if(e.key==='ArrowRight')next()};

function openUpload(){document.getElementById('uploadModal').classList.add('show')}
function closeUpload(){document.getElementById('uploadModal').classList.remove('show')}
function doUpload(){
let subj=document.getElementById('upSubj').value.trim();
let eye=document.getElementById('upEye').value;
let file=document.getElementById('upFile').files[0];
if(!subj||!file){alert('Subject ID and image are required.');return}
let fd=new FormData();fd.append('subject',subj);fd.append('eye',eye);fd.append('file',file);
let sb=document.getElementById('sb');sb.textContent='Uploading...';sb.className='status-bar saving';
fetch('/api/upload',{method:'POST',body:fd}).then(r=>r.json()).then(res=>{
if(res.ok){sb.textContent='Uploaded ✓';sb.className='status-bar';
closeUpload();refresh();
setTimeout(()=>{let key=subj+'_'+eye;if(D[key])load(key)},300)}
else{sb.textContent='Upload error: '+res.error;sb.className='status-bar error'}})}

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
