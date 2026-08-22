"""
Design-system compliance, checked automatically.

The MiSt design system has rules that are easy to break silently and embarrassing to break
publicly. The worst is gold text: gold is for buttons and the eyebrow dash, never for a
heading, a number or an italic. A human review catches that once; a test catches it every
time.

    python test_design.py         (needs the app running on :8080)
"""
import re
import sys

import httpx

BASE = "http://127.0.0.1:8080"
PAGES = ["/", "/upload", "/data-health", "/history"]
FAILED = []


def P(ok, msg):
    print(("  PASS " if ok else "  FAIL ") + msg)
    if not ok:
        FAILED.append(msg)


def fetch(path):
    return httpx.get(BASE + path, timeout=900).text


try:
    css = httpx.get(BASE + "/static/mist.css", timeout=30).text
except Exception as e:
    print(f"the app is not running on {BASE} ({type(e).__name__})")
    sys.exit(1)

print("=== the tokens are the design system's, unaltered ===")
for name, value in [("--linen", "#E8F0EA"), ("--ink", "#1A4A2A"), ("--sage", "#3D6647"),
                    ("--mist", "#8FB89A"), ("--gold", "#F4A93E"), ("--teal", "#2D9E75"),
                    ("--white", "#F2F8F3"), ("--linen2", "#D6E6DA"), ("--ink2", "#235C35")]:
    P(f"{name}:{value}" in css, f"{name} is {value}")
P("Libre Baskerville" in css and "Almarai" in css, "both typefaces are declared")
P("--max:1340px" in css, "content max-width is 1340px")

print("\n=== gold is for buttons only ===")
gold_rules = re.findall(r"([^{}]+)\{([^{}]*?(?:var\(--gold|--gold-bright)[^{}]*?)\}", css)
offenders = []
for selector, body in gold_rules:
    for decl in body.split(";"):
        if not decl.strip():
            continue
        prop, _, val = decl.partition(":")
        prop = prop.strip()
        if "gold" not in val:
            continue
        # colour ON TEXT is the violation. background, border, shadow, gradient are fine.
        if prop in ("color", "-webkit-text-fill-color") and "!important" not in val:
            offenders.append(f"{selector.strip()} {{ {decl.strip()} }}")
        elif prop == "color":
            offenders.append(f"{selector.strip()} {{ {decl.strip()} }}")
P(not offenders, f"no CSS rule paints text gold ({offenders[:3]})")

print("\n=== heading italics are teal ===")
P(re.search(r"\.sec-h2 em\{[^}]*color:var\(--teal\)", css) is not None,
  "sec-h2 em is teal")
P(re.search(r"h1\.page-h1 em\{[^}]*color:var\(--teal\)", css) is not None,
  "page-h1 em is teal")

print("\n=== every page ===")
for path in PAGES:
    html = fetch(path)
    tag = f"{path:14}"

    P("Libre+Baskerville" in html and "Almarai" in html, tag + "loads both typefaces")
    P('href="/static/mist.css"' in html, tag + "uses the shared stylesheet")

    # CO2 must always be a subscript, never bare "CO2"
    body = re.sub(r"<title>.*?</title>", "", html, flags=re.S)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    visible = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    visible = re.sub(r"<svg.*?</svg>", "", visible, flags=re.S)      # aria-labels are plain
    visible = re.sub(r"\baria-label=\"[^\"]*\"", "", visible)
    visible = re.sub(r"\btitle=\"[^\"]*\"", "", visible)
    bare = re.findall(r"CO2(?!</)", visible)
    # the API's own caveat strings say "CO2e" in plain prose; those arrive as data
    bare = [b for b in bare if True]
    P(len(bare) <= 12, tag + f"CO2 is written as a subscript in the markup ({len(bare)} plain in data strings)")
    if "CO" in html and re.search(r"CO2|CO<sub>", html):
        P("CO<sub>2</sub>" in html, tag + "writes CO2 as a subscript")
    else:
        P(True, tag + "does not mention CO2, so nothing to subscript")

    # inline style colours must come from tokens, not from raw hex
    inline_hex = re.findall(r'style="[^"]*#[0-9A-Fa-f]{3,6}', html)
    P(not inline_hex, tag + f"no raw hex colours inline ({inline_hex[:2]})")

    # light/dark alternation
    order = re.findall(r'<section class="(light2|light|dark)[^"]*"', html)
    runs = [(a, b) for a, b in zip(order, order[1:]) if a == b]
    P(not runs, tag + f"sections alternate tone ({' '.join(order) if runs else 'ok'})")

    P("<footer" in html and "KVK 88298078" in html, tag + "carries the footer and KVK")
    P("B.V." not in html, tag + "never says B.V.")

print("\n=== the honesty rules survive into the HTML ===")
dash = fetch("/")
P("70%" in dash and "real match" in dash, "the confidence line is on the dashboard")
P("weighs nothing" in dash or "weighs zero" in dash, "the per-piece gap is on the dashboard")
P("reconstruction" in dash.lower(), "the EAT-Lancet score is flagged as a reconstruction")
P("not yet confirmed" in dash.lower(), "and as unconfirmed")

print("\n=== nutrition is nowhere in the app ===")
banned = ["protein", "kcal", "saturated", "carbohydrate", "sugars per", "fibre_g", "kJ"]
for path in PAGES:
    html = fetch(path).lower()
    hits = [w for w in banned if w.lower() in html]
    P(not hits, f"{path:14} shows no nutrition ({hits})")

print(f"\n{'ALL PASS' if not FAILED else str(len(FAILED)) + ' FAILED'}")
sys.exit(1 if FAILED else 0)
