#!/usr/bin/env python3
"""
Listen through a vocabulary in the browser before committing it.

  python vocab_player.py Vocab_Master.cpp            # new = not in the committed (git HEAD) version
  python vocab_player.py Vocab_Master.cpp --base old.cpp

Words not present in the baseline are marked NEW. Play one word, or auto-play
down the list; press f to flag a word that sounds wrong. Flags are saved to
listen_flags.json (next to the vocabulary) and survive restarts.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import tempfile
import threading
import wave
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np

from lpc_audio import DEFAULT_DC_HZ, DEFAULT_LPF_HZ, DEFAULT_LPF_POLES, analog_output, normalize_peak
from lpc_vocab import parse_cpp_file
from tms5220 import synthesize


def baseline_names(path: str, base: str | None) -> set[str]:
    """Names in the baseline vocabulary: --base, else the git HEAD copy of the file."""
    if base:
        return {e.name for e in parse_cpp_file(base)}
    try:
        blob = subprocess.run(["git", "show", f"HEAD:{Path(path).name}"], capture_output=True,
                              check=True, cwd=Path(path).resolve().parent).stdout
    except subprocess.CalledProcessError:
        return set()
    with tempfile.NamedTemporaryFile(suffix=".cpp", delete=False) as fh:
        fh.write(blob)
    try:
        return {e.name for e in parse_cpp_file(fh.name)}
    finally:
        Path(fh.name).unlink()


def make_handler(words: dict[str, bytes], items: list[dict], flags_path: Path):
    flags = set(json.loads(flags_path.read_text())) if flags_path.exists() else set()
    cache: dict[str, bytes] = {}
    lock = threading.Lock()

    def wav(name: str) -> bytes:
        if name not in cache:
            audio, rate = analog_output(synthesize(words[name]), 8000,
                                        DEFAULT_LPF_HZ, DEFAULT_LPF_POLES, DEFAULT_DC_HZ)
            pcm = np.clip(normalize_peak(audio) * 32767.0, -32768, 32767).astype(np.int16)
            buf = io.BytesIO()
            with wave.open(buf, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(round(rate)))
                wf.writeframes(pcm.tobytes())
            cache[name] = buf.getvalue()
        return cache[name]

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
                self.send(200, json.dumps({"items": items, "flags": sorted(flags)}).encode(), "application/json")
            elif m := re.fullmatch(r"/audio/([A-Za-z0-9_]+)\.wav", path):
                if m.group(1) not in words:
                    self.send(404, b"unknown word", "text/plain")
                else:
                    with lock:
                        self.send(200, wav(m.group(1)), "audio/wav")
            else:
                self.send(404, b"not found", "text/plain")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.path == "/api/flag" and body.get("name") in words:
                with lock:
                    (flags.add if body.get("on") else flags.discard)(body["name"])
                    flags_path.write_text(json.dumps(sorted(flags), indent=1))
                self.send(200, b"{}", "application/json")
            else:
                self.send(404, b"not found", "text/plain")

    return Handler


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Vocabulary Player</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f6f3;--panel:#fff;--ink:#1d1d1b;--mute:#6c6c66;--line:#ddddd6;--acc:#2b5fd9;--new:#1f8a4c;--flag:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#161615;--panel:#212120;--ink:#ececE6;--mute:#9a9a92;--line:#37372f;--acc:#7da2ff;--new:#4cc783;--flag:#ff7a70}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;gap:10px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header h1{font-size:16px;margin:0 8px 0 0}select,input,button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:5px 10px}
button{cursor:pointer}button:hover{border-color:var(--acc)}button.primary{background:var(--acc);color:#fff;border-color:var(--acc)}
#now{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;flex-wrap:wrap}#now b{font-size:26px}
#list{flex:1;overflow:auto;background:var(--panel)}.row{display:flex;gap:10px;align-items:center;padding:6px 20px;border-bottom:1px solid var(--line);cursor:pointer}
.row:hover{background:var(--bg)}.row.sel{background:var(--bg);box-shadow:inset 3px 0 var(--acc)}.row .n{flex:1}.badge{font-size:11px;font-weight:700;color:var(--new)}
.flag{color:var(--flag);font-weight:700;visibility:hidden}.row.flagged .flag{visibility:visible}.mute{color:var(--mute);font-size:13px}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:12px;color:var(--mute)}
</style></head><body>
<header><h1>Vocabulary Player</h1>
<select id="fShow"><option value="all">All words</option><option value="new">New words only</option><option value="old">Original words only</option><option value="flag">Flagged only</option></select>
<input id="fText" placeholder="Search…" style="width:150px">
<label class="mute">Gap <input id="gap" type="number" value="500" min="0" step="100" style="width:70px"> ms</label>
<span class="mute" id="count"></span></header>
<div id="now"><b id="nm">—</b><button class="primary" id="playBtn">▶ Play all from here <kbd>p</kbd></button><button id="one">Play this word <kbd>space</kbd></button>
<button id="flagBtn">Flag <kbd>f</kbd></button><span class="mute"><kbd>j</kbd>/<kbd>k</kbd> next/prev · <kbd>p</kbd> auto-play on/off</span></div>
<div id="list"></div>
<script>
let S,view=[],idx=-1,auto=false,audio=null,timer=null;const $=s=>document.querySelector(s);
async function load(){S=await (await fetch('/api/state')).json();S.flags=new Set(S.flags);refilter()}
function refilter(){const m=$('#fShow').value,t=$('#fText').value.toUpperCase();
 view=S.items.filter(i=>(m=='all'||(m=='new'&&i.new)||(m=='old'&&!i.new)||(m=='flag'&&S.flags.has(i.name)))&&(!t||i.name.includes(t)));
 const n=S.items.filter(i=>i.new).length;$('#count').textContent=`${view.length} shown · ${S.items.length} total · ${n} new · ${S.flags.size} flagged`;
 $('#list').innerHTML=view.map((i,k)=>`<div class="row${S.flags.has(i.name)?' flagged':''}" data-k="${k}"><span class="n">${i.name}</span>${i.new?'<span class="badge">NEW</span>':''}<span class="flag">⚑</span></div>`).join('');
 document.querySelectorAll('.row').forEach(r=>r.onclick=()=>{stop();go(+r.dataset.k,true)});idx=-1;$('#nm').textContent='—'}
function mark(){document.querySelectorAll('.row').forEach(r=>r.classList.toggle('sel',+r.dataset.k==idx));
 const r=document.querySelector('.row.sel');if(r)r.scrollIntoView({block:'center'});
 const i=view[idx];$('#nm').textContent=i?i.name:'—';$('#flagBtn').textContent=i&&S.flags.has(i.name)?'Unflag':'Flag'}
function stop(){auto=false;clearTimeout(timer);if(audio)audio.pause();$('#playBtn').innerHTML='▶ Play all from here <kbd>p</kbd>'}
function go(k,playIt){if(k<0||k>=view.length)return stop();idx=k;mark();if(playIt)playCur()}
function playCur(){if(audio)audio.pause();clearTimeout(timer);const a=audio=new Audio(`/audio/${view[idx].name}.wav`);
 if(view[idx+1])fetch(`/audio/${view[idx+1].name}.wav`);
 a.onended=()=>{if(auto)timer=setTimeout(()=>{if(auto)go(idx+1,true)},+$('#gap').value)};a.play().catch(()=>{})}
function toggleAuto(){if(auto)return stop();auto=true;$('#playBtn').innerHTML='■ Stop <kbd>p</kbd>';go(idx<0?0:idx,true)}
async function flag(){const i=view[idx];if(!i)return;const on=!S.flags.has(i.name);on?S.flags.add(i.name):S.flags.delete(i.name);
 await fetch('/api/flag',{method:'POST',body:JSON.stringify({name:i.name,on})});
 const r=document.querySelector(`.row[data-k="${idx}"]`);if(r)r.classList.toggle('flagged',on);mark();
 $('#count').textContent=$('#count').textContent.replace(/\d+ flagged/,S.flags.size+' flagged')}
$('#playBtn').onclick=toggleAuto;$('#one').onclick=()=>{stop();if(idx<0)idx=0;go(idx,true)};$('#flagBtn').onclick=flag;
$('#fShow').onchange=refilter;$('#fText').oninput=refilter;
document.addEventListener('keydown',e=>{if(/INPUT|SELECT/.test(document.activeElement.tagName)||e.metaKey||e.ctrlKey)return;
 if(document.activeElement.tagName=='BUTTON')document.activeElement.blur();const k=e.key;
 if(k==' '){e.preventDefault();stop();if(idx<0)idx=0;go(idx,true)}else if(k=='p')toggleAuto();else if(k=='f')flag();
 else if(k=='j'){const was=auto;go(idx+1,true);auto=was}else if(k=='k'){const was=auto;go(idx-1,true);auto=was}});
load();
</script></body></html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("vocab", help="vocabulary .cpp to listen through")
    ap.add_argument("--base", help="baseline .cpp; words not in it are marked NEW (default: git HEAD copy)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    entries = parse_cpp_file(args.vocab)
    words = {e.name: e.data for e in entries}
    base = baseline_names(args.vocab, args.base)
    items = [{"name": e.name, "new": e.name not in base} for e in entries]
    flags_path = Path(args.vocab).with_name("listen_flags.json")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(words, items, flags_path))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{len(items)} words, {sum(i['new'] for i in items)} new. Player at {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
