"""Builds docs/walkthrough/walkthrough.mp4 from the clips recorded by scripts/record_walkthrough.py.

Every segment is an animated HTML scene plus optional browser clips, timed captions and narration:

- Narration is spoken by macOS `say`, one sentence at a time, so each sentence's start time is known.
  Those times ("beats") drive the scene's animations, so motion lands on the words.
- Scenes animate with CSS animations and small JS hooks that are pure functions of time. The builder
  seeks every animation to each frame's timestamp and screenshots it, so rendering is frame-exact and
  repeatable (no screen recording of a live animation).
- ffmpeg lays the recorded clips and captions over the scene and joins the segments.

Needs ffmpeg on PATH, or FFMPEG=/path/to/ffmpeg (for example from `pip install imageio-ffmpeg`).

    PYTHONPATH=src:. .venv/bin/python scripts/make_walkthrough.py [--only SEGMENT ...]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "walkthrough"
BUILD = OUT / "build"
FF = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
VOICE, RATE = os.environ.get("VOICE", "Samantha"), os.environ.get("RATE", "186")  # macOS `say` fallback
# Narration engine: OpenAI TTS when a key is present (scripts/with_openai_key.sh), else macOS `say`.
TTS = os.environ.get("TTS") or ("openai" if os.environ.get("OPENAI_API_KEY") else "say")
OPENAI_MODEL, OPENAI_VOICE = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts"), os.environ.get("OPENAI_VOICE", "marin")
OPENAI_STYLE = ("Voice: a confident, warm product-demo narrator. Tone: upbeat, clear and friendly, never salesy. "
                "Pace: brisk but easy to follow. Pronounce identifiers naturally: API as A-P-I, SSNs as S-S-Ns, "
                "CI as C-I, M1001 as M ten oh one.")
TEMPO = float(os.environ.get("TEMPO", "1.12" if TTS == "openai" else "1.0"))  # >1 speeds narration up without changing pitch
SAY_FIXES = {"API": "A.P.I.", "M1001": "M 1001", "SSNs": "S.S.N.s", " CI.": " C.I."}
MUSIC_GAIN = float(os.environ.get("MUSIC_GAIN", "0.34"))  # background track level before ducking; 0 disables it
W, H, FPS = 1920, 1080, 30
STAGE = (40, 128, 1360, 918)  # x, y, w, h of the main video area (800x540 clips scaled 1.7x)
LEAD, GAP = 0.4, 0.09  # silence before the first sentence; pause between sentences
CLIP_IN = 0.5  # clips fade in this long after the scene starts, once the stage has zoomed in


# ---------------------------------------------------------------- styling and the animation runtime

CSS = """
:root { --ink:#0f172a; --muted:#475569; --line:#e2e8f0; --bg:#f8fafc; --blue:#2563eb; --green:#16a34a;
        --orange:#ea580c; --purple:#7c3aed; --red:#dc2626; --navy:#0b1f3a; --magenta:#ff00ff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { width:1920px; height:1080px; background:var(--bg); color:var(--ink); overflow:hidden; position:relative;
       font-family:-apple-system, "SF Pro Display", "Helvetica Neue", Arial, sans-serif; }
body::before { content:""; position:absolute; inset:0; opacity:.5; pointer-events:none;
       background-image:radial-gradient(#cbd5e1 1.2px, transparent 1.2px); background-size:32px 32px;
       animation:drift 24s linear both; }
@keyframes drift { to { background-position:192px 96px; } }
header { position:relative; height:104px; background:var(--navy); color:#fff; display:flex; align-items:center;
         padding:0 40px; gap:22px; overflow:hidden; }
header::after { content:""; position:absolute; left:0; bottom:0; height:4px; width:100%;
         background:linear-gradient(90deg,var(--blue),var(--purple),var(--orange)); transform-origin:left;
         animation:grow .9s cubic-bezier(.2,.8,.2,1) .15s both; }
header .chip { font-size:20px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; padding:8px 16px;
               border-radius:999px; background:rgba(255,255,255,.14); animation:pop .5s cubic-bezier(.3,1.6,.5,1) both; }
header h1 { font-size:40px; font-weight:700; animation:wipe .8s cubic-bezier(.2,.8,.2,1) .1s both; }
header .step { margin-left:auto; font-size:20px; opacity:.7; }
.stage { position:absolute; left:40px; top:128px; width:1360px; height:918px; background:#1e293b; border-radius:10px;
         box-shadow:0 20px 50px rgba(15,23,42,.25); animation:zoomin .5s cubic-bezier(.2,.8,.2,1) both; }
.side { position:absolute; left:1432px; top:128px; width:448px; bottom:34px; display:flex; flex-direction:column; gap:18px; }
.card { background:#fff; border:1px solid var(--line); border-radius:14px; padding:22px 24px;
        box-shadow:0 4px 14px rgba(15,23,42,.06); }
.card h3 { font-size:18px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); margin-bottom:12px; }
.card p, .card li { font-size:24px; line-height:1.4; }
.card ul { padding-left:24px; } .card li { margin:6px 0; }
pre, code { font-family:"SF Mono", Menlo, monospace; }
pre { font-size:19px; line-height:1.45; white-space:pre-wrap; }
.term { background:#0b1220; color:#e2e8f0; border-radius:14px; padding:22px 26px; box-shadow:0 20px 50px rgba(15,23,42,.25); }
.term .bar { display:flex; gap:8px; margin-bottom:14px; } .term .bar i { width:13px; height:13px; border-radius:50%; }
.term .p { color:#7dd3fc; } .term .c { color:#64748b; } .term .ok { color:#4ade80; } .term .k { color:#fbbf24; }
.badge { display:inline-block; font-weight:700; font-size:22px; padding:6px 14px; border-radius:8px; color:#fff; }
.b-SUCCESS { background:var(--green); } .b-BUSINESS_OUTCOME { background:var(--blue); }
.b-ESCALATED { background:var(--orange); } .b-FAILURE { background:var(--red); }
.legend span { display:inline-block; width:18px; height:18px; border-radius:4px; vertical-align:-3px; margin-right:8px; }
.abs { position:absolute; }

/* entrance animations: every element gets animation-delay from the narration beats */
.rise { animation:rise .7s cubic-bezier(.2,.8,.2,1) both; }
.slide { animation:slide .65s cubic-bezier(.2,.8,.2,1) both; }
.pop { animation:pop .55s cubic-bezier(.3,1.6,.5,1) both; }
.fade { animation:fade .6s ease both; }
.stamp { animation:stamp .45s cubic-bezier(.3,1.4,.5,1) both; }
.deal { animation:deal 1s cubic-bezier(.2,.8,.2,1) both; }
.shake { animation:shake .5s ease both; }
@keyframes rise { from { opacity:0; transform:translateY(34px); } to { opacity:1; transform:none; } }
@keyframes slide { from { opacity:0; transform:translateX(70px); } to { opacity:1; transform:none; } }
@keyframes pop { from { opacity:0; transform:scale(.3); } to { opacity:1; transform:scale(1); } }
@keyframes fade { from { opacity:0; } to { opacity:1; } }
@keyframes wipe { from { clip-path:inset(0 100% 0 0); } to { clip-path:inset(0 0 0 0); } }
@keyframes grow { from { transform:scaleX(0); } to { transform:scaleX(1); } }
@keyframes zoomin { from { opacity:0; transform:scale(.94); } to { opacity:1; transform:none; } }
@keyframes stamp { from { opacity:0; transform:scale(2.6) rotate(-14deg); } to { opacity:1; transform:scale(1) rotate(-8deg); } }
@keyframes deal { from { opacity:0; transform:translate(var(--dx),var(--dy)) rotate(var(--r)) scale(.55); }
                  35% { opacity:1; } to { opacity:1; transform:none; } }
@keyframes shake { 0%,100% { transform:none; } 20% { transform:translateX(-10px); } 40% { transform:translateX(9px); }
                   60% { transform:translateX(-6px); } 80% { transform:translateX(4px); } }
#fx { position:absolute; inset:0; pointer-events:none; z-index:50; }
"""

# Everything the hooks draw is a pure function of t, so any frame can be rendered in any order.
RUNTIME = r"""
const HOOKS = [], ENDS = [0];
const clamp = x => Math.max(0, Math.min(1, x));
const ease = x => { x = clamp(x); return 1 - Math.pow(1 - x, 3); };
function rng(seed) { return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
  t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
function hook(fn, end) { HOOKS.push(fn); if (end !== undefined) ENDS.push(end); }
window.__seek = t => { document.getAnimations().forEach(a => { a.pause(); a.currentTime = t * 1000; }); HOOKS.forEach(h => h(t)); };
window.__end = () => { let m = Math.max(...ENDS); for (const a of document.getAnimations()) {
  if (a.animationName === 'drift') continue; const e = a.effect.getComputedTiming().endTime; m = Math.max(m, isFinite(e) ? e / 1000 : 1e9); } return m; };

// counters: data-count="from,to,start,duration,decimals"
document.querySelectorAll('[data-count]').forEach(el => { const [a, b, s, d, k] = el.dataset.count.split(',').map(Number);
  hook(t => { el.textContent = (a + (b - a) * ease((t - s) / d)).toFixed(k || 0); }, s + d); });
// typing: data-type="start,chars per second"; the text is the element's own content
document.querySelectorAll('[data-type]').forEach(el => { const text = el.textContent; const [s, cps] = el.dataset.type.split(',').map(Number);
  const r = cps || 30; hook(t => { const n = Math.floor((t - s) * r); el.textContent = n <= 0 ? '' : text.slice(0, n); }, s + text.length / r); });
// bars: data-bar="start,duration" grows the element's width from 0 to its CSS --w
document.querySelectorAll('[data-bar]').forEach(el => { const [s, d] = el.dataset.bar.split(',').map(Number);
  hook(t => { el.style.width = `calc(${ease((t - s) / d)} * var(--w))`; }, s + d); });

// confetti bursts: window.CONFETTI = [[t, x, y], ...]
const fx = document.getElementById('fx'), g = fx && fx.getContext('2d');
const COLORS = ['#2563eb', '#16a34a', '#ea580c', '#7c3aed', '#facc15', '#ec4899', '#06b6d4'];
const bursts = (window.CONFETTI || []).map(([t0, x, y], i) => { const r = rng(1000 + i); return { t0, x, y,
  parts: Array.from({ length: 170 }, () => ({ a: -Math.PI / 2 + (r() - .5) * 2.4, v: 520 + r() * 900, s: 8 + r() * 10,
    c: COLORS[Math.floor(r() * COLORS.length)], w: (r() - .5) * 14, ph: r() * 6 })) }; });
if (bursts.length) hook(t => { if (!g) return; g.clearRect(0, 0, 1920, 1080);
  for (const b of bursts) { const dt = t - b.t0; if (dt < 0 || dt > 3.2) continue;
    for (const p of b.parts) { const x = b.x + Math.cos(p.a) * p.v * dt * .9, y = b.y + Math.sin(p.a) * p.v * dt + 900 * dt * dt;
      g.save(); g.globalAlpha = clamp(1.4 - dt / 2.4); g.translate(x, y); g.rotate(p.ph + p.w * dt); g.fillStyle = p.c;
      g.fillRect(-p.s / 2, -p.s / 4, p.s, p.s / 2); g.restore(); } } }, Math.max(...bursts.map(b => b.t0 + 3.2)));
"""


def doc(body: str, script: str = "", confetti: list[tuple[float, int, int]] | None = None) -> str:
    return (f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}"
            f"<canvas id='fx' width='1920' height='1080'></canvas>"
            f"<script>window.CONFETTI = {json.dumps(confetti or [])};</script><script>{RUNTIME}</script>"
            f"<script>{script}</script></body></html>")


def page(chip: str, title: str, body: str, step: str = "", script: str = "",
         confetti: list[tuple[float, int, int]] | None = None) -> str:
    return doc(f"<header><span class='chip'>{chip}</span><h1>{title}</h1><span class='step'>{step}</span></header>"
               f"{body}", script, confetti)


def at(t: float) -> str:
    return f"animation-delay:{t:.2f}s;"


def side(*cards: tuple[str, str, float]) -> str:
    return "<div class='side'>" + "".join(f"<div class='card slide' style='{at(t)}'><h3>{h}</h3>{b}</div>"
                                          for h, b, t in cards) + "</div>"


def ul(*items: str) -> str:
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>"


def term(lines: list[tuple[str, str, float]], size: int = 19, cps: int = 34) -> str:
    """lines of (css class, text, start). Prompt lines (class p) type out; the rest fade in."""
    out = []
    for cls, text, t in lines:
        t_ = html.escape(text)
        if cls == "p":
            out.append(f"<div class='fade' style='{at(t)}'><span class='p'>$</span> "
                       f"<span data-type='{t:.2f},{cps}'>{t_}</span></div>")
        else:
            out.append(f"<div class='{cls} fade' style='{at(t)}'>{t_ or '&nbsp;'}</div>")
    return (f"<div class='term'><div class='bar'><i style='background:#f87171'></i><i style='background:#fbbf24'></i>"
            f"<i style='background:#4ade80'></i></div><pre style='font-size:{size}px'>" + "".join(out) + "</pre></div>")


def result_json(r: dict) -> str:
    return html.escape(json.dumps({k: v for k, v in r.items() if k != "recoveries" or v}, indent=2))


# ---------------------------------------------------------------- segments


@dataclass
class Clip:
    name: str
    x: int = STAGE[0]
    y: int = STAGE[1]
    w: int = STAGE[2]
    h: int = STAGE[3]
    trim: float = 0.3  # skip the blank first frames
    captions: list[dict] = field(default_factory=list)  # discovery tool calls: {t, tool, reason}


@dataclass
class Segment:
    id: str
    sentences: list[str]
    scene: Callable[[list[float]], str]  # narration beats (sentence start times) -> HTML
    clips: list[Clip] = field(default_factory=list)
    max_speed: float = 3.0
    hold: float = 0.55  # silence after the narration

    @property
    def narration(self) -> str:
        return " ".join(self.sentences)


def segments(m: dict) -> list[Segment]:
    rep = {r["clip"]: r for r in m["replays"]}
    disc = {d["clip"]: d for d in m["discoveries"]}
    ds, dx = disc["discovery-savings"], disc["discovery-expired"]
    art = json.loads((BUILD / "artifacts" / "get_savings_balance" / "v1.json").read_text())
    sys.path[:0] = [str(ROOT / "src")]
    from cua.contracts import Capability
    approved_hash = Capability.model_validate(art).content_hash()
    stress = json.loads((ROOT / "evidence" / "stress" / "summary.json").read_text())
    uri = lambda p: Path(p).as_uri()  # noqa: E731
    shot = lambda name: (BUILD / "frames" / f"{name}.png").as_uri()  # noqa: E731
    n = 15
    S = lambda i: f"{i} / {n}"  # noqa: E731

    # ---- 1 title: drifting fragments of legacy UI, a cursor that clicks, kinetic type
    def title(b):
        words = "Computer-use automation|for legacy bank back-office apps".split("|")
        lines = "".join(f"<div>{''.join(f'<span class=rise style=display:inline-block;{at(0.25 + 0.09 * (i * 4 + j))}>{w}&nbsp;</span>' for j, w in enumerate(line.split()))}</div>"
                        for i, line in enumerate(words))
        pills = ('Setup', 'Discovery', 'Replay', 'Handoff', 'Safety', 'Evidence', 'Evals')
        body = f"""<div class=abs style="inset:0;background:linear-gradient(135deg,#0b1f3a,#1e3a8a 60%,#312e81)"></div>
          <canvas id=shards class=abs width=1920 height=1080 style="inset:0"></canvas>
          <div class=abs style="inset:0;color:#fff;display:flex;flex-direction:column;justify-content:center;padding:0 140px;gap:34px">
            <div class=fade style="font-size:26px;letter-spacing:.14em;text-transform:uppercase;opacity:.75;{at(0.1)}">Video walkthrough</div>
            <div style="font-size:88px;font-weight:800;line-height:1.05">{lines}</div>
            <div style="font-size:38px;display:flex;gap:14px">
              <span class=rise style="{at(b[1])}">Discover once with Claude.</span>
              <span class=rise style="{at(b[2])}">Replay deterministically.</span>
              <span class=rise style="{at(b[2] + 1.3)}">Hand off to a human safely.</span></div>
            <div style="display:flex;gap:18px;margin-top:20px;font-size:26px">
              {''.join(f'<span class="pop pill" style="padding:10px 20px;border:2px solid rgba(255,255,255,.45);border-radius:999px;{at(b[2] + 2.2 + 0.14 * i)}">{p}</span>' for i, p in enumerate(pills))}</div></div>
          <div id=ripple class=abs style="left:0;top:0;width:20px;height:20px;border-radius:50%;border:3px solid #facc15;opacity:0"></div>
          <svg id=cursor class=abs width=44 height=44 viewBox="0 0 24 24" style="left:0;top:0;filter:drop-shadow(0 4px 8px rgba(0,0,0,.5))">
            <path d="M4 2 L4 20 L9 15 L12.5 22 L15.5 20.5 L12 13.5 L19 13.5 Z" fill="#fff" stroke="#0f172a" stroke-width="1.3"/></svg>"""
        script = f"""
        const cv = document.getElementById('shards'), c2 = cv.getContext('2d'), R = rng(7);
        const shards = Array.from({{length: 34}}, () => ({{x: R() * 1920, y: R() * 1080, w: 90 + R() * 260, h: 26 + R() * 90,
          vx: (R() - .5) * 26, vy: -8 - R() * 18, r: (R() - .5) * .5, kind: Math.floor(R() * 3)}}));
        hook(t => {{ c2.clearRect(0, 0, 1920, 1080); for (const s of shards) {{
          const x = (s.x + s.vx * t + 2400) % 2400 - 240, y = (s.y + s.vy * t + 1400) % 1400 - 160;
          c2.save(); c2.translate(x, y); c2.rotate(s.r + t * .01); c2.globalAlpha = .13; c2.strokeStyle = '#fff'; c2.lineWidth = 2;
          c2.strokeRect(0, 0, s.w, s.h); if (s.kind === 0) {{ c2.fillStyle = '#fff'; c2.fillRect(10, s.h / 2 - 4, s.w * .5, 8); }}
          if (s.kind === 1) for (let k = 1; k < 3; k++) {{ c2.beginPath(); c2.moveTo(0, s.h * k / 3); c2.lineTo(s.w, s.h * k / 3); c2.stroke(); }}
          if (s.kind === 2) {{ c2.beginPath(); c2.arc(s.w - 18, s.h / 2, 7, 0, 7); c2.stroke(); }} c2.restore(); }} }});
        const cur = document.getElementById('cursor'), rip = document.getElementById('ripple');
        const pts = [[1760, 1010, 0], [1760, 1010, {b[1] - 0.2}], [470, 590, {b[1] + 0.9}], [470, 590, {b[2] + 2.9}],
                     [560, 745, {b[2] + 3.7}], [560, 745, 99]];
        const clicks = [{b[1] + 1.0}, {b[2] + 3.8}];
        hook(t => {{ let i = 0; while (i < pts.length - 2 && t > pts[i + 1][2]) i++;
          const [x0, y0, t0] = pts[i], [x1, y1, t1] = pts[i + 1], k = ease((t - t0) / Math.max(.01, t1 - t0));
          const x = x0 + (x1 - x0) * k, y = y0 + (y1 - y0) * k - Math.sin(Math.PI * k) * 120;
          cur.style.transform = `translate(${{x}}px, ${{y}}px)`;
          const c = clicks.find(c => t >= c && t < c + .7);
          if (c !== undefined) {{ const d = (t - c) / .7; rip.style.opacity = 1 - d;
            rip.style.transform = `translate(${{x - 10}}px, ${{y - 10}}px) scale(${{1 + d * 5}})`; }} else rip.style.opacity = 0; }},
          {b[2] + 4.5});"""
        return doc(body, script)

    # ---- 2 pipeline: five nodes light up on their sentence; a packet carries the work between them
    def pipeline(b):
        xs = [230, 590, 950, 1310, 1670]
        nodes = [("🔍", "Discover", "Claude drives a real browser toward a goal"),
                 ("📄", "Artifact", "Typed, versioned JSON a person approves"),
                 ("⚙️", "Replay", "Deterministic. No model in the loop"),
                 ("🙋", "Handoff", "Same live session, passed to an operator"),
                 ("🗂️", "Evidence", "Every run, sensitive data masked")]
        html_nodes = "".join(f"""
          <div class=abs style="left:{x - 150}px;top:300px;width:300px;text-align:center">
            <div class=pop style="{at(0.3 + 0.12 * i)}"><div id=n{i} style="margin:0 auto;width:180px;height:180px;border-radius:50%;
              background:#fff;border:4px solid #cbd5e1;display:flex;align-items:center;justify-content:center;font-size:84px;
              filter:grayscale(1);opacity:.55">{e}</div></div>
            <div class=rise style="font-size:34px;font-weight:800;margin-top:22px;{at(b[i + 1])}">{i + 1}. {t}</div>
            <div class=rise style="font-size:23px;color:var(--muted);margin-top:8px;line-height:1.35;{at(b[i + 1] + 0.25)}">{d}</div>
          </div>""" for i, (x, (e, t, d)) in enumerate(zip(xs, nodes)))
        lines = "".join(f'<line x1="{xs[i] + 96}" y1="390" x2="{xs[i + 1] - 96}" y2="390" stroke="#94a3b8" stroke-width="5" '
                        f'stroke-dasharray="8 10" class=fade style="{at(0.6 + 0.1 * i)}"/>' for i in range(4))
        buckets = "".join(f'<div class="badge b-{k} pop" style="position:absolute;left:{770 + (i % 2) * 185}px;top:{730 + (i // 2) * 66}px;'
                          f'width:175px;text-align:center;font-size:19px;{at(b[3] + 1.0 + 0.3 * i)}">{k.replace("_", " ")}</div>'
                          for i, k in enumerate(["SUCCESS", "BUSINESS_OUTCOME", "ESCALATED", "FAILURE"]))
        files = "".join(f'<div class="pop" style="position:absolute;left:{1575 + i * 22}px;top:{730 + i * 54}px;background:#fff;border:2px solid #cbd5e1;'
                        f'border-radius:8px;padding:8px 14px;font:600 18px SF Mono,Menlo,monospace;{at(b[5] + 0.8 + 0.3 * i)}">{f}</div>'
                        for i, f in enumerate(["events.jsonl", "result.json", "masked.png"]))
        swap = ('<div class=abs style="left:1250px;top:735px;width:120px;height:90px;font-size:76px;text-align:center">'
                '<span id=hA class=abs style="left:0;right:0;opacity:0">🤖</span><span id=hB class=abs style="left:0;right:0;opacity:0">🧑‍💼</span></div>')
        body = f"""<svg class=abs width=1920 height=1080 style="left:0;top:0">{lines}</svg>{html_nodes}
          <div id=packet class=abs style="left:0;top:0;width:34px;height:34px;border-radius:50%;background:radial-gradient(#fde047,#f59e0b);
            box-shadow:0 0 26px 10px rgba(250,204,21,.55);opacity:0"></div>{buckets}{files}{swap}"""
        colors = ["#2563eb", "#7c3aed", "#16a34a", "#ea580c", "#dc2626"]
        script = f"""
        const B = {json.dumps(b)}, XS = {xs}, COL = {json.dumps(colors)}, pk = document.getElementById('packet');
        hook(t => {{ for (let i = 0; i < 5; i++) {{ const on = ease((t - B[i + 1]) / .5), el = document.getElementById('n' + i);
            el.style.filter = `grayscale(${{1 - on}})`; el.style.opacity = .55 + .45 * on;
            el.style.borderColor = on > .5 ? COL[i] : '#cbd5e1';
            el.style.transform = `scale(${{1 + .12 * Math.sin(Math.PI * clamp((t - B[i + 1]) / .6))}})`;
            el.style.boxShadow = on > .5 ? `0 0 0 ${{8 + 6 * Math.sin(t * 4 + i)}}px ${{COL[i]}}22` : 'none'; }}
          let shown = false; for (let i = 0; i < 4; i++) {{ const s = B[i + 2] - .75, k = (t - s) / .75;
            if (k >= 0 && k <= 1) {{ const x = XS[i] + (XS[i + 1] - XS[i]) * ease(k), y = 390 - Math.sin(Math.PI * k) * 70;
              pk.style.transform = `translate(${{x - 17}}px, ${{y - 17}}px)`; shown = true; }} }}
          pk.style.opacity = shown ? 1 : 0;
          const vis = t >= B[4] + .5, h = Math.floor((t - B[4]) / 1.2) % 2;
          document.getElementById('hA').style.opacity = vis ? 1 - h : 0; document.getElementById('hB').style.opacity = vis ? h : 0; }});"""
        return page("Overview", "Five parts, one contract", body, S(2), script)

    # ---- 3 setup: commands type out; the key flies into the Keychain; tests count up
    def setup(b):
        dots = "".join(f"<i class=pop style='display:inline-block;width:11px;height:11px;border-radius:50%;background:#4ade80;"
                       f"margin:0 3px;{at(b[3] + 0.9 + 0.035 * k)}'></i>" for k in range(54))
        mock_t = b[3] + 3.9
        body = f"""<div class=abs style="left:40px;top:150px;width:1340px">
          <div class='term'><div class='bar'><i style='background:#f87171'></i><i style='background:#fbbf24'></i><i style='background:#4ade80'></i></div>
          <pre style="font-size:24px"><div class=fade style="{at(b[1] - 0.2)}"><span class=p>$</span> <span data-type="{b[1]:.2f},30">make setup</span></div><div class="c fade" style="{at(b[1] + 0.5)}"># uv venv -p 3.12, pip install -e .[dev], playwright install chromium</div><div class=fade style="{at(b[1] + 0.9)}"><span style="display:inline-block;width:700px;height:16px;background:#1e293b;border-radius:8px;vertical-align:middle;overflow:hidden"><span data-bar="{b[1] + 0.9:.2f},2.6" style="--w:700px;width:0;display:block;height:100%;background:linear-gradient(90deg,#38bdf8,#4ade80)"></span></span></div><div>&nbsp;</div><div class=fade style="{at(b[2] - 0.2)}"><span class=p>$</span> <span data-type="{b[2]:.2f},30">scripts/store_api_key.sh</span></div><div class=fade style="{at(b[2] + 0.9)}">Paste your Anthropic API key at the prompt (input is hidden), then paste it again to confirm.</div><div class="ok fade" style="{at(b[2] + 2.6)}">Stored in Keychain as 'cua-anthropic-api-key'.</div><div>&nbsp;</div><div class=fade style="{at(b[3] - 0.2)}"><span class=p>$</span> <span data-type="{b[3]:.2f},30">make test</span></div><div class=fade style="{at(b[3] + 0.8)}">{dots}</div><div class="ok fade" style="{at(b[3] + 0.8)}"><span data-count="0,162,{b[3] + 0.9:.2f},2.2">0</span> passed, 1 deselected<span class=fade style="{at(b[3] + 3.2)}"> in 151.13s</span></div><div>&nbsp;</div><div class=fade style="{at(mock_t - 0.2)}"><span class=p>$</span> <span data-type="{mock_t:.2f},30">make mock</span></div><div class="c fade" style="{at(mock_t + 0.5)}"># mock CoreOne Banking app on http://127.0.0.1:5055</div></pre></div></div>
          <div class=side>
            <div class="card slide" style="{at(b[0])}"><h3>Stack</h3>{ul('Python 3.12 + uv', 'Playwright (Chromium)', 'Pydantic v2 contracts', 'Flask mock bank', 'Anthropic SDK, claude-opus-5')}</div>
            <div class="card slide" style="{at(b[2])};text-align:center"><h3>macOS Keychain</h3>
              <div style="position:relative;height:120px;font-size:84px">
                <span class=abs style="left:30px;top:10px;animation:keyfly 1.1s cubic-bezier(.5,0,.3,1) both;{at(b[2] + 1.4)}">🔑</span>
                <span class=abs style="right:40px;top:10px;animation:unfade .3s both;{at(b[2] + 2.4)}">🔓</span>
                <span class="abs pop" style="right:40px;top:10px;{at(b[2] + 2.5)}">🔒</span></div>
              <p style="font-size:20px;color:var(--muted)">Read per process by <code>scripts/with_api_key.sh</code>. Never on disk or in logs.</p></div>
          </div>
          <style>@keyframes keyfly {{ from {{ transform:none; opacity:1; }} 90% {{ opacity:1; }} to {{ transform:translateX(250px) scale(.4); opacity:0; }} }}
                 @keyframes unfade {{ from {{ opacity:1; }} to {{ opacity:0; }} }}</style>"""
        return page("Setup", "Three commands, no key needed to start", body, S(3))

    # ---- 4 the target app: screenshots deal out like cards; fault stickers slap on
    def app(b):
        cards = [("app-search", "Framesets: navigation and content in separate frames", 40, 140, "380px,280px", "-9deg"),
                 ("app-summary", "Nested tables, no element ids, every link reads \"View\"", 720, 140, "-300px,280px", "7deg"),
                 ("app-dialog", "Injected fault: an unknown security dialog", 40, 590, "380px,-170px", "6deg"),
                 ("app-login", "Injected fault: the session expires mid-flow", 720, 590, "-300px,-170px", "-7deg")]
        deck = "".join(f"""<div class="abs deal" style="left:{x}px;top:{y}px;width:660px;--dx:{d.split(',')[0]};--dy:{d.split(',')[1]};--r:{r};{at(b[0] + 0.25 + 0.3 * i)}">
            <img src="{shot(s)}" style="width:100%;height:372px;object-fit:cover;object-position:left top;border-radius:10px;border:1px solid var(--line);box-shadow:0 14px 34px rgba(15,23,42,.18)">
            <div style="font-size:21px;color:var(--muted);margin-top:6px">{c}</div></div>""" for i, (s, c, x, y, d, r) in enumerate(cards))
        boxes = (f'<div class="abs pop" style="left:40px;top:140px;width:141px;height:372px;border:4px dashed var(--orange);border-radius:10px;{at(b[1] + 0.1)}"></div>'
                 f'<div class="abs pop badge" style="left:52px;top:470px;background:var(--orange);font-size:16px;{at(b[1] + 0.2)}">nav frame</div>'
                 f'<div class="abs pop" style="left:181px;top:140px;width:519px;height:372px;border:4px dashed var(--blue);border-radius:10px;{at(b[1] + 0.4)}"></div>'
                 f'<div class="abs pop badge" style="left:560px;top:470px;background:var(--blue);font-size:16px;{at(b[1] + 0.5)}">main frame</div>'
                 f'<div class="abs pop" style="left:866px;top:273px;width:298px;height:74px;border:4px solid var(--purple);border-radius:6px;{at(b[1] + 1.1)}"></div>'
                 f'<div class="abs pop badge" style="left:1172px;top:292px;background:var(--purple);font-size:18px;{at(b[1] + 1.3)}">no ids</div>')
        faults = ["⚡ session expiry", "⚡ surprise dialog", "⚡ slow page", "⚡ HTTP 500", "⚡ off-allowlist redirect",
                  "⚡ columns reordered", "⚡ native alert", "⚡ truncated input"]
        spots = [(480, 540), (1080, 560), (170, 990), (890, 1000), (420, 790), (1110, 820), (620, 420), (250, 680)]
        stickers = "".join(f'<div class="abs pop" style="left:{x}px;top:{y}px;"><div style="background:#fef08a;color:#713f12;font-weight:800;font-size:22px;'
                           f'padding:8px 14px;border-radius:10px;box-shadow:0 6px 16px rgba(0,0,0,.2);transform:rotate({(-1) ** i * (4 + i % 3 * 3)}deg)">{f}</div></div>'
                           .replace('style="left', f'style="{at(b[2] + 1.2 + 0.32 * i)}left') for i, (f, (x, y)) in enumerate(zip(faults, spots)))
        stamp = (f'<div class="abs stamp" style="left:400px;top:470px;padding:14px 34px;border:8px solid var(--red);color:var(--red);font-size:64px;'
                 f'font-weight:900;letter-spacing:.08em;background:rgba(255,255,255,.88);border-radius:14px;{at(b[3] + 0.1)}">SYNTHETIC DATA</div>')
        body = deck + boxes + stickers + stamp + side(
            ("Mock CoreOne Banking", ul("3 tenants, one relabelled, one on a newer version", "20+ injectable faults",
                                        "Irreversible sub-account commit", "All data is synthetic"), b[0] + 0.5),
            ("Why hostile", "<p>Replay has to survive what real legacy apps do: frames, look-alike links, "
                            "dialogs, expired sessions, slow and broken pages.</p>", b[2]))
        return page("The target", "A deliberately hostile legacy app", body, S(4))

    # ---- clip scenes share a layout: stage on the left, cards sliding in on the beats
    spec = (ROOT / "specs" / "get_savings_balance.yaml").read_text().strip()

    def discovery(b):
        chips = "".join(f'<span class="badge pop" style="background:{c};font-family:SF Mono,Menlo,monospace;font-size:19px;margin:4px;{at(b[1] + 0.7 + 0.18 * i)}">{t}</span>'
                        for i, (t, c) in enumerate([("fill", "#2563eb"), ("click", "#2563eb"), ("select", "#2563eb"), ("extract", "#16a34a"),
                                                    ("wait", "#64748b"), ("request_human", "#ea580c"), ("done", "#16a34a")]))
        return page("Discovery", "Claude explores the app once", "<div class=stage></div>" + side(
            ("Spec: get_savings_balance", f'<pre style="font-size:16px">{html.escape(spec)}</pre>', b[0]),
            ("Tools, one per turn", f"<div>{chips}</div><p style='font-size:20px;color:var(--muted);margin-top:10px'>Every call "
                                    "states a reason. A gateway checks it against the allowlist first.</p>", b[1]),
            ("Result", f'<p><b>{ds["status"]}</b> in <span data-count="0,{ds["turns"]},{b[3]:.2f},1.2">0</span> turns<br>'
                       f'served by {", ".join(ds["models"])}</p>', b[3])), S(5))

    def escalate(b):
        return page("Discovery", "It asks a human when it should", "<div class=stage></div>" + side(
            ("What happens", ul("The session expires after the search", "Claude has no credentials, by design",
                                "<b>request_human</b>: the operator signs in on the same browser",
                                "Discovery continues from the restored page"), b[1]),
            ("Result", f'<p><b>{dx["status"]}</b> in {dx["turns"]} turns</p><p style="font-size:20px;color:var(--muted);'
                       'margin-top:8px">The login form is never recorded as a step.</p>', b[-1])), S(6))

    # ---- 7 artifact: JSON slides up, the locator ladder builds rung by rung and a probe falls through it, APPROVED stamp
    def artifact(b):
        s2, s3 = art["steps"][1], art["steps"][2]
        fmt = lambda s: html.escape(json.dumps({"id": s["id"], "action": s["action"], "risk": s["risk"],  # noqa: E731
                                                "target": {"frame": s["target"]["frame"], "candidates": s["target"]["candidates"]},
                                                "post": s.get("post")}, indent=1))
        rungs = [("ROLE_NAME", 'button "Search"'), ("LABEL_PROXIMITY", "input next to a label"),
                 ("TABLE_ANCHOR", 'row "Savings", column 4'), ("COORDINATES", "last resort")]
        span = b[2] - b[1]
        rung_t = [b[1] + 1.2 + k * span * 0.17 for k in range(4)]
        probe = b[1] + span * 0.8
        marks = [("✗ 0 matches", "var(--red)"), ("✗ 0 matches", "var(--red)"), ("✓ 1 match", "var(--green)")]
        ladder = "".join(f"""<div class=pop style="position:relative;margin:10px 0;padding:12px 16px;border:3px solid #cbd5e1;border-radius:12px;background:#fff;{at(rung_t[k])}">
              <b style="font-size:21px">{k + 1}. {r}</b><div style="font-size:18px;color:var(--muted)">{d}</div>
              {f'<span class="abs pop" style="right:12px;top:14px;font-weight:800;font-size:19px;color:{marks[k][1]};{at(probe + 0.5 * k)}">{marks[k][0]}</span>' if k < 3 else ''}
              {f'<div class="abs fade" style="inset:-3px;border:4px solid var(--green);border-radius:12px;box-shadow:0 0 22px rgba(22,163,74,.5);{at(probe + 1.0)}"></div>' if k == 2 else ''}
            </div>""" for k, (r, d) in enumerate(rungs))
        stamp_t = b[2] + 1.3
        body = f"""<div class=abs style="left:40px;top:140px;width:1360px;display:grid;grid-template-columns:1fr 1fr;gap:20px">
            <div class="term rise" style="{at(b[0] + 0.2)}"><pre style="font-size:15px">{fmt(s2)}</pre></div>
            <div class="term rise" style="{at(b[0] + 0.5)}"><pre style="font-size:15px">{fmt(s3)}</pre></div>
            <div style="grid-column:span 2">{term([
                ('p', 'cua discover specs/get_savings_balance.yaml', b[0] + 0.9),
                ('ok', f'{ds["status"]}: {ds["reason"]} ({ds["turns"]} turns, log runs/{ds["run"]}/)', b[0] + 2.2),
                ('', 'saved get_savings_balance v1 as DRAFT. Review artifacts/get_savings_balance/v1.json, then:', b[0] + 2.4),
                ('p', 'cua approve get_savings_balance 1', b[2] - 0.2),
                ('ok', f'get_savings_balance v1 APPROVED ({approved_hash})', stamp_t - 0.3)], 20)}</div></div>
          <div class="abs stamp" style="left:380px;top:420px;padding:14px 40px;border:9px solid var(--green);color:var(--green);font-size:84px;
            font-weight:900;letter-spacing:.1em;background:rgba(255,255,255,.9);border-radius:16px;{at(stamp_t)}">APPROVED</div>
          <div class=side>
            <div class="card slide" style="{at(b[1])};position:relative"><h3>Locator ladder, tried in order</h3>
              <div id=probe class="abs" style="left:6px;top:0;width:22px;height:22px;border-radius:50%;background:#facc15;box-shadow:0 0 16px #f59e0b;opacity:0;z-index:2"></div>
              {ladder}<p style="font-size:18px;color:var(--muted);margin-top:6px">More than one match stops the run: AMBIGUOUS_TARGET.</p></div>
            <div class="card slide" style="{at(b[2])}"><h3>Lifecycle</h3><div style="font-size:21px;font-weight:700;display:flex;gap:10px;align-items:center;white-space:nowrap">
              <span style="position:relative">DRAFT<span class="abs" style="left:-4px;right:-4px;top:50%;height:4px;background:var(--red);transform-origin:left;animation:grow .4s both;{at(stamp_t)}"></span></span>
              → <span class="badge pop" style="background:var(--green);{at(stamp_t + 0.1)}">APPROVED</span> → DEPRECATED</div></div>
          </div>"""
        script = f"""const pr = document.getElementById('probe'); const P = {probe};
          hook(t => {{ const k = (t - P) / 1.0; pr.style.opacity = k < 0 || k > 1.5 ? 0 : 1;
            pr.style.transform = `translateY(${{80 + 87 * Math.min(2, Math.floor(clamp(k) * 2.99) + ease((k * 2.99) % 1) * (k < .67 ? 1 : 0))}}px)`; }}, P + 1.6);"""
        return page("Artifact", "The trace becomes a reviewable artifact", body, S(7), script)

    ok = rep["replay-success"]

    def replay(b):
        return page("Replay", "Deterministic replay, no model in the loop", "<div class=stage></div>" + side(
            ("Command", term([('p', 'make replay-demo', b[0] + 0.3)], 18), b[0]),
            ("Caller receives", f'<span class="badge b-SUCCESS pop" style="{at(b[2] + 0.3)}">SUCCESS</span><pre style="margin-top:12px">'
                                f'{result_json(ok["result"])}</pre>', b[2]),
            ("On disk", f'<pre style="font-size:17px">{html.escape(json.dumps(ok["on_disk"]["outputs"]))}</pre>', b[-1]),
            ("Legend", '<p class="legend"><span style="background:var(--blue)"></span>action &nbsp; '
                       '<span style="background:var(--green)"></span>read</p>', b[1])), S(8))

    nf, amb = rep["replay-not-found"], rep["replay-ambiguous"]

    def buckets(b):
        cards = "".join(f'<div class="card rise" style="position:absolute;left:{x}px;top:730px;width:900px;{at(t)}">'
                        f'<span class="badge b-{r["result"]["bucket"]}">{r["result"]["bucket"]}</span> '
                        f'<b style="font-size:26px;margin-left:10px">{r["result"].get("code") or r["result"].get("reason")}</b>'
                        f'<p style="margin-top:10px;color:var(--muted)">{c}</p></div>'
                        for x, r, c, t in ((40, nf, 'member_id M9999: an answer, not a crash', b[1]),
                                           (980, amb, 'member_id M1002 has two Savings rows: replay stops instead of guessing', b[2])))
        row = "".join(f'<span class="badge b-{k} pop" style="{at(b[0] + 0.3 + 0.25 * i)}">{k}</span>'
                      for i, k in enumerate(["SUCCESS", "BUSINESS_OUTCOME", "ESCALATED", "FAILURE"]))
        return page("Outcomes", "Every run ends in exactly one of four buckets", f"""
          <div class=stage style="width:900px;height:580px"></div><div class=stage style="left:980px;width:900px;height:580px"></div>{cards}
          <div style="position:absolute;left:40px;top:915px;right:40px;display:flex;gap:16px;font-size:24px;align-items:center">{row}
            <span class=fade style="color:var(--muted);margin-left:10px;{at(b[0] + 1.4)}">each with a typed reason code, never a free-text error</span></div>""",
            S(9))

    ho = rep["replay-handoff"]

    def handoff(b):
        states = ["AUTOMATION", "PAUSE_REQUESTED", "HUMAN_IN_CONTROL", "VERIFYING_CHECKPOINT", "AUTOMATION"]
        icons = ["🤖", "⏸️", "🧑‍💼", "🔎", "🤖"]
        times = [0, b[1] + 0.6, b[2] + 1.0, b[4] + 1.6, b[4] + 3.6]
        rows = "".join(f'<div style="display:flex;align-items:center;gap:12px;height:62px;padding:0 14px;font:700 19px SF Mono,Menlo,monospace">'
                       f'<span style="font-size:30px">{i}</span>{s}</div>' for s, i in zip(states, icons))
        body = f"""<div class=stage></div><div class=side>
          <div class="card slide" style="{at(b[0])};position:relative"><h3>Control token</h3>
            <div style="position:relative"><div id=tok class=abs style="left:0;right:0;top:0;height:62px;border-radius:12px;background:#ffedd5;
              border:3px solid var(--orange);box-shadow:0 0 18px rgba(234,88,12,.35)"></div><div style="position:relative">{rows}</div></div>
            <div class="abs pop" style="left:24px;bottom:-26px;{at(b[3] + 0.2)}"><span class="badge shake" style="display:inline-block;background:var(--red);font-size:18px;{at(b[3] + 0.5)}">✗ automation act rejected</span></div></div>
          <div class="card slide" style="{at(b[4])};margin-top:20px"><h3>Result</h3><span class="badge b-ESCALATED">ESCALATED</span>
            <p style="margin-top:10px;font-size:20px">{html.escape(ho["result"].get("reason") or "")}</p></div></div>"""
        script = f"""const T = {json.dumps(times)}, tok = document.getElementById('tok');
          hook(t => {{ let i = 0; while (i < T.length - 1 && t >= T[i + 1]) i++;
            const k = i < T.length - 1 ? ease((t - T[i + 1] + .45) / .45) : 0;
            tok.style.transform = `translateY(${{(i + k) * 62}}px)`; }}, T[T.length - 1] + .5);"""
        return page("Handoff", "Same live session, handed to a human and back", body, S(10), script)

    def approval(b):
        return page("Safety", "Irreversible steps need a human approval", "<div class=stage></div>" + side(
            ("Risk tiers", ul("READ", "REVERSIBLE_WRITE", "<b>IRREVERSIBLE</b>: needs approval", "Unknown controls fail closed"), b[0]),
            ("Approval token", "<p>HMAC-signed, single use, bound to the run and step. No token, no commit.</p>", b[0] + 1.5),
            ("Result", f'<span class="badge b-SUCCESS pop" style="{at(b[-1] + 0.3)}">SUCCESS</span><p style="margin-top:10px;font-size:20px">'
                       "Exactly one sub-account created</p>", b[-1])), S(11))

    po = rep["replay-policy"]

    def policy(b):
        return page("Safety", "Allowlist and redaction", "<div class=stage></div>" + side(
            ("Result", f'<span class="badge b-FAILURE pop" style="{at(b[1] + 0.3)}">FAILURE</span> <b style="font-size:24px">'
                       f'{po["result"].get("reason")}</b>', b[1]),
            ("Allowlist", ul("Hosts, routes and actions from config/policy.yaml", "App redirects caught at every checkpoint wait; "
                             "no action taken on the page", "Clicks checked against their destination <b>before</b> they happen",
                             "POLICY_VIOLATION is ours; PERMISSION_DENIED is the app"), b[2]),
            ("Redaction", "<p>SSNs, dates of birth, names, account numbers and balances are masked in logs, screenshots "
                          "and the text Claude sees.</p>", b[-1])), S(12))

    # ---- 13 evidence: a scanner sweeps the screenshot and masks what it passes; 38 tiles light up
    def evidence(b):
        cat_col = {"baseline": "#16a34a", "business": "#2563eb", "recoverable": "#06b6d4", "escalation": "#ea580c",
                   "hard_failure": "#dc2626", "drift": "#7c3aed", "safety": "#0f172a"}
        span = max(2.5, b[2] - b[1] - 0.6)
        tiles = "".join(f'<div class=pop style="height:50px;border-radius:8px;background:{cat_col[r["category"]]};color:#fff;'
                        f'font:700 15px SF Mono,Menlo,monospace;display:flex;align-items:center;justify-content:center;'
                        f'{at(b[1] + 0.4 + span * i / 38)}">{r["id"]}{" ✓" if r["pass"] else ""}</div>'
                        for i, r in enumerate(stress["matrix"]))
        legend = "".join(f'<span style="display:inline-flex;align-items:center;gap:6px;margin-right:14px"><i style="width:14px;height:14px;border-radius:3px;'
                         f'background:{c}"></i>{k.replace("_", " ")}</span>' for k, c in cat_col.items())
        stats = "".join(f'<div class=pop style="flex:1;background:#fff;border:3px solid var(--green);border-radius:14px;padding:12px;text-align:center;{at(b[2] + 0.2 + 0.4 * i)}">'
                        f'<div style="font-size:42px;font-weight:900;color:var(--green)">{v}</div><div style="font-size:19px;color:var(--muted)">{k}</div></div>'
                        for i, (v, k) in enumerate([("38/38", "classified correctly"), ("0", "false SUCCESS"),
                                                    (str(len(stress.get("pii_hits") or [])), "PII leaks")]))
        scan0, scan_d = b[0] + 1.0, 3.0
        body = f"""<div class="abs rise" style="left:40px;top:140px;width:1000px;{at(b[0])}">
            <div style="position:relative;width:1000px;height:675px;border-radius:10px;overflow:hidden;border:1px solid var(--line);box-shadow:0 14px 34px rgba(15,23,42,.18)">
              <img src="{uri(BUILD / 'stills/redact-raw.png')}" class=abs style="left:0;top:0;width:100%">
              <img id=masked src="{uri(BUILD / 'stills/redact-masked.png')}" class=abs style="left:0;top:0;width:100%;clip-path:inset(0 0 100% 0)">
              <div id=scan class=abs style="left:0;right:0;top:0;height:6px;background:#22d3ee;box-shadow:0 0 24px 8px rgba(34,211,238,.7);opacity:0"></div></div>
            <div style="font-size:22px;color:var(--muted);margin-top:10px">The same page as the operator sees it, then as the Redactor saves it.</div></div>
          <div class=abs style="left:1080px;top:140px;width:800px;display:flex;flex-direction:column;gap:16px">
            {term([('c', 'evidence/replay/<run>/', b[0] + 0.3), ('', '  events.jsonl       every step, recovery, handoff', b[0] + 0.8),
                   ('', '  result.json        the typed result, outputs masked', b[0] + 1.3), ('', '  evidence-001.png   masked screenshot', b[0] + 1.8),
                   ('', '  evidence-001.txt   redacted page text', b[0] + 2.3)], 18)}
            <div class="card slide" style="{at(b[1])}"><h3>Stress matrix: <span data-count="0,38,{b[1] + 0.4:.2f},{span:.2f}">0</span> injected conditions</h3>
              <div style="display:grid;grid-template-columns:repeat(8,1fr);gap:6px">{tiles}</div>
              <div style="font-size:16px;color:var(--muted);margin-top:10px">{legend}</div></div>
            <div style="display:flex;gap:14px">{stats}</div></div>"""
        script = f"""const S0 = {scan0}, SD = {scan_d}, mk = document.getElementById('masked'), sc = document.getElementById('scan');
          hook(t => {{ const k = clamp((t - S0) / SD); mk.style.clipPath = `inset(0 0 ${{100 - k * 100}}% 0)`;
            sc.style.transform = `translateY(${{k * 675 - 3}}px)`; sc.style.opacity = k > 0 && k < 1 ? 1 : 0; }}, S0 + SD);"""
        return page("Evidence", "Every run leaves an audit trail", body, S(13), script)

    # ---- 14 evals: layers slide in, bars race, confetti when v2 lands on 1.00
    def evals(b):
        layers = [("Runtime telemetry", "every run: outcomes, allowlist refusals by stage, locator fallbacks, cost", b[1]),
                  ("Replay eval", "38 scenarios in CI: 38/38 correct, 0 false SUCCESS", b[2]),
                  ("Discovery eval", "11 cases x 2 reps: replay what Claude found on probe inputs", b[3]),
                  ("Judge calibration", "Sonnet 5 judge, 35 labelled items, 100% agreement", b[3] + 2.6)]
        rows = "".join(f'<tr class=slide style="border-top:1px solid var(--line);{at(t)}"><td style="padding:10px 0;width:250px"><b>{a}</b></td>'
                       f'<td style="color:var(--muted)">{d}</td></tr>' for a, d, t in layers)
        bars_t = b[4] + 0.6
        bars = "".join(f'<div style="display:flex;align-items:center;gap:16px;margin:14px 0;font-size:26px"><b style="width:120px">{v}</b>'
                       f'<div style="width:760px"><div data-bar="{bars_t + 0.5 * i:.2f},1.6" style="--w:{int(p * 760)}px;width:0;height:40px;'
                       f'background:{c};border-radius:8px"></div></div><b data-count="0,{p},{bars_t + 0.5 * i:.2f},1.6,2">0.00</b></div>'
                       for i, (v, p, c) in enumerate((('baseline', 0.818, '#94a3b8'), ('v1', 0.909, '#60a5fa'), ('v2', 1.0, '#16a34a'))))
        boom = bars_t + 1.0 + 1.6
        body = f"""<div class=abs style="left:40px;top:140px;width:1100px;display:flex;flex-direction:column;gap:18px">
            <div class="card rise" style="{at(b[0])}"><h3>Four layers</h3><table style="width:100%;font-size:22px;border-collapse:collapse">{rows}</table></div>
            <div class="card rise" style="{at(b[4])}"><h3>Discovery task success across prompt versions</h3>{bars}
              <p class=fade style="font-size:20px;color:var(--muted);{at(boom)}">v1 added a wait tool and human-only attestations; v2 fixed a restart loop the v1 fix exposed.</p></div></div>
          <div class="abs pop" style="left:1030px;top:780px;font-size:64px;{at(boom)}">🎉</div>""" + side(
            ("Judge rubric", ul("J1 reasoning faithfulness", "J2 artifact reviewability", "J3 escalation judgement", "J4 goal fidelity"), b[3] + 1),
            ("Commands", term([('p', 'make eval', b[1] + 0.5), ('p', 'make eval-live', b[2] + 0.5), ('p', 'make scorecard', b[3] + 0.5)], 18), b[1]))
        return page("Evals", "Measured continuously, not just once", body, S(14), confetti=[(boom, 1000, 820)])

    # ---- 15 recap: the commands type themselves, then a thank-you with confetti
    def recap(b):
        lines = [('c', '# no key needed', b[0]), ('p', 'make setup', b[0] + 0.7), ('p', 'make test', b[0] + 1.5),
                 ('p', 'make mock', b[0] + 2.3), ('p', 'make replay-demo', b[0] + 3.0), ('', '', b[1] - 0.3),
                 ('c', '# with the key in the Keychain (scripts/store_api_key.sh)', b[1] - 0.2),
                 ('p', 'cua discover specs/get_savings_balance.yaml', b[1] + 0.8), ('p', 'make evidence-live', b[1] + 2.4),
                 ('p', 'make eval-live YES=1', b[1] + 3.4)]
        body = f"""<div class=abs style="left:40px;top:150px;width:1200px">{term(lines, 27, 40)}</div>""" + side(
            ("Read next", ul("README.md", "REPORT.md", "docs/WORKFLOW.md (diagrams)", "docs/EVALS.md", "evidence/README.md",
                             "docs/walkthrough/playground.html"), b[2])) + f"""
          <div class="abs fade" style="inset:0;background:rgba(11,31,58,.84);{at(b[3] - 0.1)}"></div>
          <div class="abs pop" style="left:0;right:0;top:390px;text-align:center;color:#fff;{at(b[3])}">
            <div style="font-size:110px;font-weight:900">Thanks for watching</div>
            <div style="font-size:34px;opacity:.85;margin-top:14px">Discover once. Replay deterministically. Hand off safely.</div></div>"""
        return page("Try it", "From clone to evidence", body, S(15), confetti=[(b[3] + 0.2, 560, 1000), (b[3] + 0.45, 1360, 1000)])

    return [
        Segment("01-title", ["Here's a computer-use automation system for legacy bank back-office apps.",
                             "Claude explores the app once.",
                             "What it learns becomes a versioned artifact that replays deterministically, with no model in the loop."],
                title),
        Segment("02-pipeline", ["Five parts.", "Discovery: Claude drives a real browser toward a goal.",
                                "An artifact, approved by a person.",
                                "Deterministic replay, ending in one of four outcomes.",
                                "Human handoff on the same live session.",
                                "And evidence for every run, with sensitive data masked."], pipeline),
        Segment("03-setup", ["Setup is three commands.", "Make setup builds the Python environment and installs Chromium.",
                             "The API key script stores your Anthropic key in the macOS Keychain, never on disk.",
                             "And make test runs 162 offline tests, no key needed."], setup),
        Segment("04-app", ["The target is a mock legacy banking app.", "Framesets, nested tables, no element ids.",
                           "Three tenants and over twenty injectable faults, like expired sessions, surprise dialogs, and slow pages.",
                           "All the data is synthetic."], app),
        Segment("05-discovery", ["Discovery starts from a short spec: a goal, with typed inputs and outputs.",
                                 "Claude reads the page as text and calls one tool per turn, each with a stated reason.",
                                 "A gateway checks every action against the allowlist first.",
                                 "Six turns later, a draft is saved."], discovery,
                [Clip("discovery-savings", captions=ds["captions"])], max_speed=2.2),
        Segment("06-escalate", ["When Claude hits something it shouldn't handle, it asks for help.", "Here the session expires.",
                                "Claude has no credentials, so it calls request human.",
                                "An operator signs in on the same browser, and discovery carries on."], escalate,
                [Clip("discovery-expired", captions=dx["captions"])], max_speed=4.0),
        Segment("07-artifact", ["The recorder turns that trace into an artifact.",
                                "Each step has a ranked ladder of locators: role and name, then label, then table anchor, and coordinates only as a last resort.",
                                "It stays a draft until an operator approves it."], artifact, hold=1.3),
        Segment("08-replay", ["Replay runs the approved artifact with no model at all.", "Blue outlines are actions; green are reads.",
                              "Member M1001 returns success, with balance and currency.",
                              "The caller gets the balance; on disk, it's masked."], replay, [Clip("replay-success")]),
        Segment("09-buckets", ["Every run ends in exactly one of four buckets.",
                               "An unknown member is a business outcome: an answer, not a crash.",
                               "Two matching savings rows is a failure, because replay won't guess."],
                buckets, [Clip("replay-not-found", 40, 128, 900, 580), Clip("replay-ambiguous", 980, 128, 900, 580)]),
        Segment("10-handoff", ["Anything that needs a person escalates.", "Here the session expires mid-run.",
                               "Automation never types credentials, so it hands a control token to an operator on the same live session.",
                               "While the human holds it, automation is locked out.",
                               "The operator signs in and resumes, and replay verifies the page before continuing."],
                handoff, [Clip("replay-handoff")]),
        Segment("11-approval", ["Irreversible steps, like opening a sub-account, need a single-use approval from an operator.",
                                "Without one, nothing is committed."], approval, [Clip("replay-approval")], max_speed=1.6),
        Segment("12-policy", ["Here the app redirects off the allowlist.",
                              "Replay catches it at the next checkpoint and stops with a policy violation.",
                              "Its own clicks are checked before they happen.",
                              "And a redactor masks names, SSNs, and balances in every log and screenshot."], policy,
                [Clip("replay-policy")]),
        Segment("13-evidence", ["Every run leaves evidence: an event log, masked screenshots, and a result.",
                                "The stress matrix injects 38 fault conditions, no key needed.",
                                "All 38 are classified correctly, with zero false successes and zero leaks."], evidence),
        Segment("14-evals", ["Evals keep measuring it.", "Telemetry on every run.", "A replay eval in CI.",
                             "And a live discovery eval that replays what Claude found, graded by a calibrated judge.",
                             "Across three prompt versions, task success rose from 82 to 100 percent."], evals),
        Segment("15-recap", ["To try it: make setup, make test, make mock, and make replay demo.",
                             "With a key: discover, evidence live, and eval live.",
                             "The README, the report, and an interactive playground cover the rest.", "Thanks for watching!"],
                recap, hold=1.9),
    ]


# ---------------------------------------------------------------- media helpers


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(path: Path) -> float:
    err = subprocess.run([FF, "-i", str(path)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def grab(clip: str, t: float, name: str) -> None:
    run([FF, "-y", "-loglevel", "error", "-ss", str(t), "-i", str(BUILD / "clips" / f"{clip}.webm"),
         "-frames:v", "1", str(BUILD / "frames" / f"{name}.png")])


def speak(text: str, d: Path, i: int) -> Path:
    """One sentence of narration as an audio file. OpenAI results are cached by content, so rebuilds cost nothing."""
    if TTS == "say":
        for a, b in SAY_FIXES.items():
            text = text.replace(a, b)
        p = d / f"s{i}.aiff"
        subprocess.run(["say", "-v", VOICE, "-r", RATE, "-o", str(p), text], check=True)
        return p
    import hashlib
    import urllib.error
    import urllib.request
    key = hashlib.sha256(json.dumps([OPENAI_MODEL, OPENAI_VOICE, OPENAI_STYLE, text]).encode()).hexdigest()[:20]
    cached = BUILD / "tts" / f"{key}.wav"
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"model": OPENAI_MODEL, "voice": OPENAI_VOICE, "input": text, "instructions": OPENAI_STYLE,
                           "response_format": "wav"}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/audio/speech", body, {
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}", "Content-Type": "application/json"})
        for attempt in range(4):
            try:
                cached.write_bytes(urllib.request.urlopen(req, timeout=120).read())
                break
            except urllib.error.HTTPError as e:  # never echo the response body: it can quote part of the key
                if e.code == 401:
                    raise SystemExit("OpenAI rejected the key (401). Re-run scripts/store_openai_key.sh.") from None
                if e.code not in (429, 500, 502, 503) or attempt == 3:
                    raise SystemExit(f"OpenAI TTS failed with HTTP {e.code}.") from None
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
                if attempt == 3:
                    raise SystemExit("OpenAI TTS kept timing out. Check the network and re-run.") from None
            import time
            time.sleep(2 ** attempt)
    if TEMPO == 1.0:
        return cached
    p = d / f"s{i}.wav"
    run([FF, "-y", "-loglevel", "error", "-i", str(cached), "-af", f"atempo={TEMPO}", str(p)])
    return p


def narrate(seg: Segment, d: Path) -> tuple[Path, list[float], float]:
    """One TTS call per sentence, joined with short gaps. Returns (audio, beats, spoken length)."""
    parts, beats, t = [], [], LEAD
    for i, s in enumerate(seg.sentences):
        p = speak(s, d, i)
        beats.append(round(t, 2))
        t += duration(p) + GAP
        parts.append(p)
    ins = sum((["-i", str(p)] for p in parts), [])
    chain = "".join(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,apad=pad_dur={GAP}[a{i}];"
                    for i in range(len(parts)))
    chain += "".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
    voice = d / "voice.wav"
    run([FF, "-y", "-loglevel", "error", *ins, "-filter_complex", chain, "-map", "[out]", str(voice)])
    return voice, beats, t - GAP - LEAD


def render_scene(pw_page, html_text: str, out: Path, total: float) -> None:
    """Seeks the scene to every frame time and pipes screenshots to ffmpeg. Stops once nothing moves any more."""
    tmp = out.with_suffix(".html")
    tmp.write_text(html_text)
    pw_page.set_viewport_size({"width": W, "height": H})
    pw_page.goto(tmp.as_uri())
    pw_page.wait_for_load_state("networkidle")
    pw_page.evaluate("document.fonts.ready")
    length = min(total, pw_page.evaluate("__end()") + 0.2)
    frames = max(1, int(length * FPS))
    ff = subprocess.Popen([FF, "-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg",
                           "-i", "-", "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-color_range", "tv",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "14",
                           str(out)], stdin=subprocess.PIPE)
    for i in range(frames):
        pw_page.evaluate(f"__seek({i / FPS})")
        ff.stdin.write(pw_page.screenshot(type="jpeg", quality=93))
    ff.stdin.close()
    if ff.wait():
        raise RuntimeError(f"ffmpeg failed rendering {out}")


def render_png(pw_page, html_text: str, out: Path, size: tuple[int, int]) -> None:
    tmp = out.with_suffix(".html")
    tmp.write_text(html_text)
    pw_page.set_viewport_size({"width": size[0], "height": size[1]})
    pw_page.goto(tmp.as_uri())
    pw_page.screenshot(path=str(out), omit_background=True)


def caption_html(c: dict, i: int, n: int) -> str:
    color = {"request_human": "#ea580c", "done": "#16a34a", "extract": "#16a34a"}.get(c["tool"], "#2563eb")
    reason = html.escape(c["reason"] if len(c["reason"]) < 120 else c["reason"][:117] + "...")
    return (f"<html><head><meta charset='utf-8'><style>{CSS} body{{background:transparent;width:1360px;height:112px}}"
            f"body::before{{display:none}}</style></head><body><div style='margin:0 24px;background:rgba(15,23,42,.9);color:#fff;"
            f"border-radius:12px;padding:14px 20px;display:flex;gap:16px;align-items:center;height:96px'>"
            f"<span style='font-size:18px;opacity:.7;white-space:nowrap'>Claude turn {i}/{n}</span>"
            f"<span class='badge' style='background:{color};font-family:SF Mono,Menlo,monospace'>{c['tool']}</span>"
            f"<span style='font-size:24px;line-height:1.25'>{reason}</span></div></body></html>")


def build_segment(seg: Segment, pw_page) -> Path:
    d = BUILD / "seg" / seg.id
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    voice, beats, spoken = narrate(seg, d)
    total = LEAD + spoken + seg.hold
    speeds = []
    for c in seg.clips:  # speed a long clip up to fit the narration; freeze the last frame of a short one
        raw = duration(BUILD / "clips" / f"{c.name}.webm") - c.trim
        speeds.append(min(seg.max_speed, max(1.0, raw / (total - CLIP_IN - 0.3))))
        total = max(total, CLIP_IN + raw / speeds[-1] + 0.6)
    render_scene(pw_page, seg.scene(beats), d / "scene.mp4", total)

    inputs = ["-i", str(d / "scene.mp4")]
    filters = [f"[0:v]tpad=stop_mode=clone:stop_duration={total:.2f},trim=0:{total:.2f}[v0]"]
    last, idx = "v0", 1
    for c, sp in zip(seg.clips, speeds):
        inputs += ["-ss", str(c.trim), "-i", str(BUILD / "clips" / f"{c.name}.webm")]
        filters.append(f"[{idx}:v]setpts=PTS/{sp:.3f}+{CLIP_IN}/TB,fps={FPS},scale={c.w}:{c.h}:flags=lanczos,format=yuva420p,"
                       f"fade=t=in:st={CLIP_IN}:d=0.35:alpha=1,tpad=stop_mode=clone:stop_duration={total:.2f}[c{idx}]")
        filters.append(f"[{last}][c{idx}]overlay={c.x}:{c.y}:eof_action=repeat[v{idx}]")
        last, idx = f"v{idx}", idx + 1
        for i, cap in enumerate(c.captions):
            png = d / f"cap{i}.png"
            render_png(pw_page, caption_html(cap, i + 1, len(c.captions)), png, (1360, 112))
            start = CLIP_IN + max(0.0, (cap["t"] - c.trim) / sp)
            end = CLIP_IN + (c.captions[i + 1]["t"] - c.trim) / sp if i + 1 < len(c.captions) else total
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{total:.2f}", "-i", str(png)]
            filters.append(f"[{last}][{idx}:v]overlay={c.x}:{c.y + c.h - 130}:enable='between(t,{start:.2f},{end:.2f})'[v{idx}]")
            last, idx = f"v{idx}", idx + 1
    if seg.clips and any(sp > 1.05 for sp in speeds):
        filters.append(f"[{last}]drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:text='{max(speeds):.1f}x speed':"
                       f"x={seg.clips[0].x + seg.clips[0].w - 170}:y={seg.clips[0].y + 14}:fontsize=22:fontcolor=white:"
                       f"box=1:boxcolor=black@0.55:boxborderw=8:enable='gte(t,{CLIP_IN})'[vs]")
        last = "vs"
    filters.append(f"[{last}]format=yuv420p,fade=t=in:st=0:d=0.25,fade=t=out:st={total - 0.25:.2f}:d=0.25[vout]")
    inputs += ["-i", str(voice)]
    filters.append(f"[{idx}:a]adelay={int(LEAD * 1000)}|{int(LEAD * 1000)},apad,atrim=0:{total:.2f}[aout]")
    out = d / "segment.mp4"
    run([FF, "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[vout]", "-map",
         "[aout]", "-t", f"{total:.2f}", "-r", str(FPS), "-color_range", "tv", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", str(out)])
    print(f"  {seg.id}: {total:5.1f}s (voice {spoken:.1f}s{', speed ' + ', '.join(f'{s:.1f}x' for s in speeds) if speeds else ''})",
          flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    args = ap.parse_args()
    m = json.loads((BUILD / "manifest.json").read_text())
    (BUILD / "frames").mkdir(parents=True, exist_ok=True)
    for clip, t, name in (("replay-success", 1.2, "app-search"), ("replay-success", 3.4, "app-summary"),
                          ("replay-dialog", 4.2, "app-dialog"), ("replay-handoff", 6.2, "app-login")):
        grab(clip, t, name)
    segs = segments(m)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        files = [build_segment(s, pg) if (not args.only or s.id in args.only) else BUILD / "seg" / s.id / "segment.mp4"
                 for s in segs]
        browser.close()
    (BUILD / "concat.txt").write_text("".join(f"file '{f}'\n" for f in files))
    out = OUT / "walkthrough.mp4"
    length = sum(duration(f) for f in files)
    # one continuous re-encode with standard BT.709 tags plays everywhere (QuickTime rejects mixed segment headers)
    graph, extra = "[0:v]scale=out_color_matrix=bt709:out_range=tv,format=yuv420p[v]", []
    if MUSIC_GAIN > 0:  # original track (scripts/make_music.py), ducked under the voice by a sidechain compressor
        music = BUILD / "music.wav"
        run(["uv", "run", "-q", "--no-project", "--with", "numpy", "python", str(ROOT / "scripts" / "make_music.py"),
             "--seconds", f"{length + 1:.1f}", "--out", str(music)])
        extra = ["-i", str(music)]
        graph += (f";[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,asplit=2[vo][key];[1:a]lowpass=f=9000,volume={MUSIC_GAIN}[bed];"
                  "[bed][key]sidechaincompress=threshold=0.02:ratio=4:attack=25:release=500[duck];"
                  "[vo][duck]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]")
    else:
        graph += ";[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[a]"
    run([FF, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(BUILD / "concat.txt"), *extra,
         "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-profile:v", "high",
         "-level", "4.1", "-preset", "medium", "-crf", "20", "-color_primaries", "bt709", "-color_trc", "bt709",
         "-colorspace", "bt709", "-color_range", "tv", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out)])
    (OUT / "narration.md").write_text("# Walkthrough narration\n\nSpoken track of `walkthrough.mp4`, segment by "
                                      "segment.\n\n" + "".join(f"**{s.id}**: {s.narration}\n\n" for s in segs))
    print(f"{out.relative_to(ROOT)}: {duration(out):.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
