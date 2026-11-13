#!/usr/bin/env python3
"""
Interactive vocabulary audit: listen to candidate words next to the closest
master word, decide what to do with each, and export the approved words.

  python compare_vocab.py sources/cpp/other/Vocab_HP27201A.cpp --out-dir audit_hp     # once
  python audit_app.py sources/cpp/other/Vocab_HP27201A.cpp --audit-dir audit_hp       # then browse

Decisions are saved to <audit-dir>/decisions.json after every click, so you can
stop and resume. Export writes the approved words to a .cpp vocabulary.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import threading
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

from compare_vocab import base_name
from lpc_audio import DEFAULT_DC_HZ, DEFAULT_LPF_HZ, DEFAULT_LPF_POLES, analog_output, normalize_peak
from lpc_vocab import VocabEntry, c_identifier_fragment, parse_cpp_file, write_cpp_file
from tms5220 import synthesize

DECISIONS = ("new", "alternate", "reject", "later")


class State:
    def __init__(self, cand_path: str, ref_path: str, audit_dir: str, suffix: str):
        self.audit_dir = Path(audit_dir)
        self.cand = {e.name: e for e in parse_cpp_file(cand_path)}
        self.ref = {e.name: e for e in parse_cpp_file(ref_path)}
        self.cand_path = cand_path
        self.suffix = suffix
        self.lock = threading.Lock()
        self.wav_cache: dict[tuple[str, str], bytes] = {}
        self.dec_path = self.audit_dir / "decisions.json"
        self.decisions: dict[str, dict] = (
            json.loads(self.dec_path.read_text()) if self.dec_path.exists() else {}
        )
        csv_path = self.audit_dir / "comparison_all.csv"
        if not csv_path.exists():
            raise SystemExit(f"missing {csv_path}; run compare_vocab.py first")
        groups: dict[str, list[str]] = {}
        for name in self.cand:
            groups.setdefault(base_name(name), []).append(name)
        self.items = []
        for row in csv.DictReader(csv_path.open()):
            if row["name"] not in self.cand:
                continue
            row["siblings"] = [n for n in groups[base_name(row["name"])] if n != row["name"]]
            row["ref_siblings"] = [n for n in self.ref if base_name(n) == base_name(row["ref_name"] or row["name"])
                                   and n != row["ref_name"]][:6]
            row["suggested_name"] = self.suggest(row)
            row["bytes"] = len(self.cand[row["name"]].data)
            self.items.append(row)

    def suggest(self, row: dict) -> dict:
        """Default final names: new words keep their name; alternates hang off the master word."""
        taken = set(self.ref) | {d.get("final_name") for d in self.decisions.values()}

        def free(base: str) -> str:
            base = c_identifier_fragment(base)
            if base not in taken:
                return base
            n = 2
            while f"{base}_{n}" in taken:
                n += 1
            return f"{base}_{n}"

        return {
            "new": c_identifier_fragment(row["name"]) if c_identifier_fragment(row["name"]) not in self.ref
            else free(f"{row['name']}_{self.suffix}"),
            "alternate": free(f"{row['ref_name'] or row['name']}_{self.suffix}"),
        }

    def save(self) -> None:
        self.dec_path.write_text(json.dumps(self.decisions, indent=1, sort_keys=True))

    def wav(self, which: str, name: str) -> bytes | None:
        table = self.cand if which == "cand" else self.ref
        if name not in table:
            return None
        key = (which, name)
        if key not in self.wav_cache:
            audio, rate = analog_output(synthesize(table[name].data), 8000,
                                        DEFAULT_LPF_HZ, DEFAULT_LPF_POLES, DEFAULT_DC_HZ)
            pcm = np.clip(normalize_peak(audio) * 32767.0, -32768, 32767).astype(np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(round(rate)))
                wf.writeframes(pcm.tobytes())
            self.wav_cache[key] = buf.getvalue()
        return self.wav_cache[key]

    def export(self, out_name: str) -> dict:
        """Write approved words; alternates and new words alike, under their final names."""
        entries, problems, used = [], [], set(self.ref)
        for name, d in sorted(self.decisions.items()):
            if d.get("decision") not in ("new", "alternate") or name not in self.cand:
                continue
            final = c_identifier_fragment(d.get("final_name") or name)
            if final in used:
                problems.append(f"{name}: final name {final} already exists - skipped")
                continue
            used.add(final)
            entries.append(VocabEntry(final, self.cand[name].data, Path(self.cand_path).name, name))
        out = Path(out_name)
        write_cpp_file(entries, out, f"Approved words from {Path(self.cand_path).name} (audit export)")
        return {"written": len(entries), "path": str(out), "problems": problems}


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlparse(self.path).path)
            if path == "/":
                self.send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                body = json.dumps({"items": state.items, "decisions": state.decisions,
                                   "ref_names": sorted(state.ref), "candidate": Path(state.cand_path).name})
                self.send(200, body.encode(), "application/json")
            elif m := re.fullmatch(r"/audio/(cand|ref)/([A-Za-z0-9_]+)\.wav", path):
                with state.lock:
                    wav = state.wav(m.group(1), m.group(2))
                if wav is None:
                    self.send(404, b"unknown word", "text/plain")
                else:
                    self.send(200, wav, "audio/wav")
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/decide":
                name = body["name"]
                with state.lock:
                    if body.get("decision") in (None, ""):
                        state.decisions.pop(name, None)
                    elif body["decision"] in DECISIONS and name in state.cand:
                        state.decisions[name] = {k: body.get(k, "") for k in
                                                 ("decision", "final_name", "ref_name", "note")}
                    state.save()
                self.send(200, b"{}", "application/json")
            elif self.path == "/api/export":
                with state.lock:
                    result = state.export(body.get("out") or "Vocab_HP_Approved.cpp")
                self.send(200, json.dumps(result).encode(), "application/json")
            else:
                self.send(404, b"not found", "text/plain")

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LPC Vocabulary Audit</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f6f3;--panel:#fff;--ink:#1d1d1b;--mute:#6c6c66;--line:#ddddd6;--acc:#2b5fd9;--new:#1f8a4c;--alt:#b36b00;--rej:#b3261e;--later:#6c6c66}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--panel:#212120;--ink:#ececE6;--mute:#9a9a92;--line:#37372f;--acc:#7da2ff;--new:#4cc783;--alt:#f0a840;--rej:#ff7a70;--later:#9a9a92}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header h1{font-size:16px;margin:0 8px 0 0}select,input,button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
button{cursor:pointer}button:hover{border-color:var(--acc)}button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
main{flex:1;display:flex;min-height:0}#list{width:300px;overflow:auto;border-right:1px solid var(--line);background:var(--panel)}
.row{display:flex;justify-content:space-between;padding:6px 14px;cursor:pointer;border-bottom:1px solid var(--line)}.row:hover{background:var(--bg)}.row.sel{background:var(--bg);box-shadow:inset 3px 0 var(--acc)}
.tag{font-size:12px;font-weight:600}.d-new{color:var(--new)}.d-alternate{color:var(--alt)}.d-reject{color:var(--rej)}.d-later{color:var(--later)}
#detail{flex:1;overflow:auto;padding:22px 28px;max-width:820px}h2{margin:0;font-size:28px}.sub{color:var(--mute);margin:2px 0 18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.card h3{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
.words{display:flex;gap:8px;flex-wrap:wrap}.words button.playing{background:var(--acc);color:#fff}
.decide{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.decide button{padding:8px 14px;font-weight:600}
.decide button.on{color:#fff}.decide .b-new.on{background:var(--new)}.decide .b-alternate.on{background:var(--alt)}.decide .b-reject.on{background:var(--rej)}.decide .b-later.on{background:var(--later)}
label{display:block;font-size:12px;color:var(--mute);margin:8px 0 3px}input[type=text]{width:100%}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:12px;color:var(--mute)}
.help{color:var(--mute);font-size:13px;margin-top:6px}#bar{height:4px;background:var(--line);flex:1;min-width:80px;border-radius:2px;overflow:hidden}#bar i{display:block;height:100%;background:var(--new)}
</style></head><body>
<header><h1>LPC Audit</h1>
<select id="fBucket"></select><select id="fStatus"><option value="open">Undecided</option><option value="all">All</option><option value="done">Decided</option></select>
<input id="fText" placeholder="Search name…" style="width:160px">
<div id="bar"><i></i></div><span id="prog" style="color:var(--mute);font-size:13px"></span>
<button class="primary" id="export">Export approved…</button></header>
<main><div id="list"></div><div id="detail"></div></main>
<datalist id="refs"></datalist>
<script>
let S, view=[], cur=null, audioEl=null;
const $=s=>document.querySelector(s);
const BUCKETS={"AUDIT-ALTERNATE":"Alternates","AUDIT-NEW":"New words","AUDIT-RENAME":"Rename (data match)","MATCH":"Matched (spot check)"};
async function load(){S=await (await fetch('/api/state')).json();
 $('#fBucket').innerHTML='<option value="">All audits</option>'+Object.entries(BUCKETS).map(([k,v])=>`<option value="${k}">${v} (${S.items.filter(i=>i.bucket==k).length})</option>`).join('');
 $('#refs').innerHTML=S.ref_names.map(n=>`<option value="${n}">`).join('');
 refilter(true)}
function refilter(keep){const b=$('#fBucket').value,st=$('#fStatus').value,t=$('#fText').value.toUpperCase();
 view=S.items.filter(i=>(b?i.bucket==b:i.bucket!='MATCH')&&(!t||i.name.includes(t))&&(st=='all'||(st=='open')==!S.decisions[i.name]));
 const prev=cur&&view.find(i=>i.name==cur.name);
 renderList();if(prev)select(prev,false);else if(view.length)select(view[0],false);else{cur=null;$('#detail').innerHTML='<p class="sub">Nothing here.</p>'}
 const audits=S.items.filter(i=>i.bucket!='MATCH'),done=audits.filter(i=>S.decisions[i.name]).length;
 $('#prog').textContent=`${done} / ${audits.length} decided`;$('#bar i').style.width=(100*done/audits.length)+'%'}
function renderList(){$('#list').innerHTML=view.map(i=>{const d=S.decisions[i.name];return `<div class="row${cur&&cur.name==i.name?' sel':''}" data-n="${i.name}"><span>${i.name}</span><span class="tag ${d?'d-'+d.decision:''}">${d?d.decision:''}</span></div>`}).join('');
 document.querySelectorAll('.row').forEach(r=>r.onclick=()=>select(view.find(i=>i.name==r.dataset.n)))}
function wordBtns(kind,names){return names.map(n=>`<button data-play="${kind}/${n}">▶ ${n}</button>`).join('')}
function select(item,scroll=true){cur=item;const d=S.decisions[item.name]||{};
 document.querySelectorAll('.row').forEach(r=>{r.classList.toggle('sel',r.dataset.n==item.name);if(r.dataset.n==item.name&&scroll!==false)r.scrollIntoView({block:'nearest'})});
 const alt=d.decision=='alternate',fin=d.final_name||(alt?item.suggested_name.alternate:item.suggested_name.new),refn=d.ref_name||item.ref_name;
 $('#detail').innerHTML=`<h2>${item.name}</h2><div class="sub">${BUCKETS[item.bucket]} — ${item.reason} · ${item.frames} frames · ${item.bytes} bytes</div>
 <div class="card"><h3>Listen</h3><div class="words"><button class="primary" data-play="cand/${item.name}">▶ ${item.name} <kbd>space</kbd></button>
 ${item.ref_name?`<button data-play="ref/${item.ref_name}">▶ master ${item.ref_name} <kbd>r</kbd></button><button id="both">▶ both <kbd>b</kbd></button>`:''}</div>
 ${item.ref_name?`<div class="help">Distance to ${item.ref_name}: ${item.ref_dist} (0 = identical; under 0.06 counts as the same)</div>`:''}
 ${item.closest_data_name!=item.ref_name?`<div class="words" style="margin-top:8px"><span class="help">Closest by sound:</span>${wordBtns('ref',[item.closest_data_name])}<span class="help">(${item.closest_data_dist})</span></div>`:''}
 ${item.siblings.length?`<div class="words" style="margin-top:8px"><span class="help">Other HP variants:</span>${wordBtns('cand',item.siblings)}</div>`:''}
 ${item.ref_siblings.length?`<div class="words" style="margin-top:8px"><span class="help">Other master variants:</span>${wordBtns('ref',item.ref_siblings)}</div>`:''}</div>
 <div class="card"><h3>Decide</h3><div class="decide">
 <button class="b-new${d.decision=='new'?' on':''}" data-d="new">Add as new word <kbd>1</kbd></button>
 <button class="b-alternate${alt?' on':''}" data-d="alternate">Add as alternate <kbd>2</kbd></button>
 <button class="b-reject${d.decision=='reject'?' on':''}" data-d="reject">Reject <kbd>3</kbd></button>
 <button class="b-later${d.decision=='later'?' on':''}" data-d="later">Decide later <kbd>4</kbd></button>
 ${d.decision?'<button data-d="">Clear <kbd>0</kbd></button>':''}</div>
 <label>Final name in vocabulary</label><input type="text" id="final" value="${fin}">
 <label>Alternate of (master word)</label><input type="text" id="refn" list="refs" value="${refn}">
 <label>Note</label><input type="text" id="note" value="${(d.note||'').replace(/"/g,'&quot;')}">
 <div class="help"><kbd>j</kbd>/<kbd>k</kbd> next / previous · decisions advance automatically · saved on every click</div></div>`;
 document.querySelectorAll('[data-play]').forEach(b=>b.onclick=()=>play(b.dataset.play,b));
 document.querySelectorAll('[data-d]').forEach(b=>b.onclick=()=>decide(b.dataset.d));
 if($('#both'))$('#both').onclick=playBoth}
function play(path,btn){return new Promise(res=>{if(audioEl)audioEl.pause();const a=audioEl=new Audio(`/audio/${path}.wav`);
 btn&&btn.classList.add('playing');const end=()=>{btn&&btn.classList.remove('playing');res()};a.onended=end;a.onerror=end;a.play().catch(end)})}
async function playBoth(){await play('cand/'+cur.name);await new Promise(r=>setTimeout(r,500));await play('ref/'+cur.ref_name)}
async function decide(dec){if(!cur)return;const name=cur.name;
 const body={name,decision:dec,final_name:$('#final').value.trim(),ref_name:$('#refn').value.trim(),note:$('#note').value};
 if(dec=='new'||dec=='alternate'){if(dec=='new'&&!body.final_name)body.final_name=cur.suggested_name.new;
  if(dec=='alternate'&&(!body.final_name||body.final_name==cur.suggested_name.new))body.final_name=cur.suggested_name.alternate}
 await fetch('/api/decide',{method:'POST',body:JSON.stringify(body)});
 if(dec)S.decisions[name]=body;else delete S.decisions[name];
 const i=view.findIndex(v=>v.name==name);refilter(true);
 if(dec&&$('#fStatus').value=='open'){const nxt=view[Math.min(i,view.length-1)];if(nxt)select(nxt)}else if(dec){const nxt=view[i+1];if(nxt)select(nxt)}}
['fBucket','fStatus'].forEach(id=>$('#'+id).onchange=()=>refilter());$('#fText').oninput=()=>refilter();
$('#export').onclick=async()=>{const out=prompt('Write approved words to which file?','Vocab_HP_Approved.cpp');if(!out)return;
 const r=await (await fetch('/api/export',{method:'POST',body:JSON.stringify({out})})).json();
 alert(`Wrote ${r.written} words to ${r.path}`+(r.problems.length?'\n\nProblems:\n'+r.problems.join('\n'):''))};
document.addEventListener('keydown',e=>{if(/INPUT|SELECT/.test(document.activeElement.tagName)||e.metaKey||e.ctrlKey||!cur)return;
 if(document.activeElement.tagName=='BUTTON')document.activeElement.blur();
 const i=view.findIndex(v=>v.name==cur.name),k=e.key;
 if(k==' '){e.preventDefault();play('cand/'+cur.name)}else if(k=='r'&&cur.ref_name)play('ref/'+cur.ref_name);else if(k=='b'&&cur.ref_name)playBoth();
 else if(k=='j'&&view[i+1])select(view[i+1]);else if(k=='k'&&view[i-1])select(view[i-1]);
 else if(k=='1')decide('new');else if(k=='2')decide('alternate');else if(k=='3')decide('reject');else if(k=='4')decide('later');else if(k=='0')decide('')});
load();
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("candidate", help="candidate vocabulary .cpp")
    ap.add_argument("--ref", default="Vocab_Master.cpp", help="master vocabulary .cpp")
    ap.add_argument("--audit-dir", default="audit_hp", help="directory holding comparison_all.csv")
    ap.add_argument("--suffix", default="HP", help="suffix for alternate names (default HP)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    state = State(args.candidate, args.ref, args.audit_dir, args.suffix)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Audit UI at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
