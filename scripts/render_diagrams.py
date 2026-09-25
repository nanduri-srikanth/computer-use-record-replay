"""Extract Mermaid blocks from docs/WORKFLOW.md and render each to docs/diagrams/NN-name.svg."""
import html, re, subprocess, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "WORKFLOW.md"
OUT = ROOT / "docs" / "diagrams"
SRC = OUT / "src"
MMDC = ROOT / "node_modules" / ".bin" / "mmdc"

INDEX = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workflow Diagrams</title>
<style>
:root {{ --bg:#F6F7F9; --card:#FFFFFF; --ink:#1B1F24; --muted:#5B6470; --line:#DDE1E6; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#14171B; --card:#1D2126; --ink:#E8EAED; --muted:#9AA3AE; --line:#2E343B; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system, system-ui, sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:24px; margin:0 0 4px; }}
p.sub {{ color:var(--muted); margin:0 0 20px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:8px 16px; margin:0 0 20px; font-size:13px; color:var(--muted); }}
.legend i {{ display:inline-block; width:14px; height:14px; border-radius:3px; border:1.5px solid; vertical-align:-2px; margin-right:6px; }}
ol.toc {{ columns:2; padding-left:20px; margin:0 0 28px; }}
ol.toc a {{ color:inherit; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin:0 0 24px; }}
h2 {{ font-size:17px; margin:0 0 12px; }}
h2 span {{ color:var(--muted); font-variant-numeric:tabular-nums; margin-right:10px; }}
img {{ display:block; max-width:100%; height:auto; margin:0 auto; background:#FFFFFF; border-radius:6px; }}
@media (max-width:640px) {{ ol.toc {{ columns:1; }} }}
</style></head>
<body><main>
<h1>System Workflow Diagrams</h1>
<p class="sub">Rendered from docs/WORKFLOW.md. Click a diagram to open it full size.</p>
<div class="legend">
<span><i style="background:#EDE4FF;border-color:#6B3FD4"></i>LLM involved</span>
<span><i style="background:#E3F0FF;border-color:#2F6FD1"></i>Deterministic</span>
<span><i style="background:#FFF0DC;border-color:#D9822B"></i>Human</span>
<span><i style="background:#37474F;border-color:#1C262B"></i>Result bucket / end state</span>
</div>
<ol class="toc">{toc}</ol>
{cards}
</main></body></html>
"""

text = DOC.read_text()
sections = re.findall(r"^## (\d+)\. (.+?)$(.*?)(?=^## |\Z)", text, re.S | re.M)
failed = []
rendered = []
for num, title, body in sections:
    m = re.search(r"```mermaid\n(.*?)```", body, re.S)
    if not m:
        continue
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    name = f"{int(num):02d}-{slug}"
    src = SRC / f"{name}.mmd"
    src.write_text(m.group(1))
    r = subprocess.run([str(MMDC), "-i", str(src), "-o", str(OUT / f"{name}.svg"),
                        "-b", "white", "-p", str(ROOT / "scripts" / "puppeteer.json")],
                       capture_output=True, text=True)
    ok = r.returncode == 0
    print(("OK   " if ok else "FAIL ") + name)
    rendered.append((num, title, f"{name}.svg"))
    if not ok:
        failed.append(name)
        print(r.stderr[-1500:])

cards = "\n".join(
    f'<section id="d{n}"><h2><span>{int(n):02d}</span>{html.escape(t)}</h2>'
    f'<a href="{f}" target="_blank"><img src="{f}" alt="{html.escape(t)}" loading="lazy"></a></section>'
    for n, t, f in rendered)
toc = "".join(f'<li><a href="#d{n}">{html.escape(t)}</a></li>' for n, t, f in rendered)
(OUT / "index.html").write_text(INDEX.format(toc=toc, cards=cards))
sys.exit(1 if failed else 0)
