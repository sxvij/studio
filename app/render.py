"""Templates: finished store and marketing images with real text. AI pictures supply the background or the product;
headlines, buttons and logos are laid out in HTML and rendered by headless Chromium at the exact size, so text is sharp
and on-brand. Every text field is HTML-escaped; images are local files from the library."""
import html, os, subprocess, tempfile

from . import store

LAYOUTS = {
    "hero": "Headline over a picture (banners, link previews, headers)",
    "social": "Social post: picture with a headline band",
    "appshot": "App Store screenshot: headline above an app screen",
    "product": "Product: a cut-out object on a gradient with a caption",
}
FONTS = {"inter": "'Inter', 'DejaVu Sans', sans-serif", "serif": "'DejaVu Serif', Georgia, serif"}


def esc(s, n=300):
    return html.escape(str(s or "")[:n]).replace("\n", "<br>")


def color(c, default):
    c = str(c or "")
    return c if len(c) in (4, 7) and c.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in c[1:]) else default


def img_url(iid):
    if not iid:
        return ""
    it = store.item(iid)
    return "file://" + store.path(iid) if it else ""


def page(layout, w, h, f):
    accent, ink, bg = color(f.get("accent"), "#f4c64e"), color(f.get("ink"), "#ffffff"), color(f.get("bg"), "#0d241f")
    font = FONTS.get(f.get("font"), FONTS["inter"])
    head, sub, button = esc(f.get("headline"), 140), esc(f.get("subline"), 300), esc(f.get("button"), 40)
    back, front, logo = img_url(f.get("background")), img_url(f.get("foreground")), img_url(f.get("logo"))
    align = "center" if f.get("align") == "center" else "left"
    u = min(w, h) / 100                                   # 1 unit = 1% of the short side
    css = f"""*{{box-sizing:border-box;margin:0}} html,body{{width:{w}px;height:{h}px;overflow:hidden}}
body{{font-family:{font};color:{ink};background:{bg};position:relative;-webkit-font-smoothing:antialiased}}
.bg{{position:absolute;inset:0;background:{'url("' + back + '") center/cover no-repeat' if back else f'radial-gradient(120% 90% at 30% 20%, {accent}33, transparent 60%), {bg}'}}}
h1{{font-weight:800;letter-spacing:-.02em;line-height:1.02}} p{{line-height:1.25;opacity:.92}}
.btn{{display:inline-block;margin-top:{3*u:.0f}px;padding:{1.6*u:.0f}px {3.6*u:.0f}px;border-radius:999px;background:{accent};color:#111;font-weight:700;font-size:{3.2*u:.0f}px}}
.logo{{position:absolute;height:{9*u:.0f}px;{'right' if align == 'left' else 'left'}:{5*u:.0f}px;top:{5*u:.0f}px}}"""
    if layout == "hero":
        css += f""".shade{{position:absolute;inset:0;background:linear-gradient({'90deg' if align == 'left' else '0deg'},rgba(0,0,0,.72),rgba(0,0,0,.15) 70%)}}
.text{{position:absolute;{'left' if align == 'left' else 'left:0;right'}:{6*u:.0f}px;{'' if align == 'left' else f'left:{6*u:.0f}px;'}bottom:{9*u:.0f}px;max-width:{w*0.62 if align == 'left' else w-12*u:.0f}px;text-align:{align}}}
h1{{font-size:{11*u:.0f}px}} p{{font-size:{4.4*u:.0f}px;margin-top:{2*u:.0f}px}}"""
        body = f'<div class="bg"></div><div class="shade"></div><div class="text"><h1>{head}</h1>{f"<p>{sub}</p>" if sub else ""}{f"<span class=btn>{button}</span>" if button else ""}</div>'
    elif layout == "social":
        top = f.get("position") == "top"
        css += f""".band{{position:absolute;left:0;right:0;{'top' if top else 'bottom'}:0;padding:{7*u:.0f}px {6*u:.0f}px;text-align:{align};
background:linear-gradient({'180deg' if top else '0deg'},rgba(0,0,0,.82),rgba(0,0,0,0))}} h1{{font-size:{9.5*u:.0f}px}} p{{font-size:{4.2*u:.0f}px;margin-top:{1.6*u:.0f}px}}"""
        body = f'<div class="bg"></div><div class="band"><h1>{head}</h1>{f"<p>{sub}</p>" if sub else ""}{f"<span class=btn>{button}</span>" if button else ""}</div>'
    elif layout == "appshot":
        css += f""".text{{position:absolute;left:{7*u:.0f}px;right:{7*u:.0f}px;top:{9*u:.0f}px;text-align:center}}
h1{{font-size:{10*u:.0f}px}} p{{font-size:{4.6*u:.0f}px;margin-top:{2.2*u:.0f}px}}
.shot{{position:absolute;left:50%;transform:translateX(-50%);top:{h*0.27:.0f}px;width:{w*0.78:.0f}px;height:{h*0.80:.0f}px;border-radius:{7*u:.0f}px;
background:{'url("' + front + '") top center/cover no-repeat' if front else '#000'};box-shadow:0 {3*u:.0f}px {9*u:.0f}px rgba(0,0,0,.55);border:{1.1*u:.0f}px solid #0b0b0c}}"""
        body = f'<div class="bg"></div><div class="text"><h1>{head}</h1>{f"<p>{sub}</p>" if sub else ""}</div><div class="shot"></div>'
    else:  # product
        css += f""".bg{{background:radial-gradient(80% 70% at 50% 42%, {accent}55, transparent 70%), {bg}}}
.obj{{position:absolute;left:50%;top:44%;transform:translate(-50%,-50%);max-width:{w*0.72:.0f}px;max-height:{h*0.62:.0f}px;filter:drop-shadow(0 {2.5*u:.0f}px {5*u:.0f}px rgba(0,0,0,.5))}}
.text{{position:absolute;left:{6*u:.0f}px;right:{6*u:.0f}px;bottom:{7*u:.0f}px;text-align:center}} h1{{font-size:{8*u:.0f}px}} p{{font-size:{4*u:.0f}px;margin-top:{1.4*u:.0f}px}}"""
        body = f'<div class="bg"></div>{f"<img class=obj src=\"{front}\">" if front else ""}<div class="text"><h1>{head}</h1>{f"<p>{sub}</p>" if sub else ""}</div>'
    if logo:
        body += f'<img class="logo" src="{logo}">'
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{body}</body></html>'


def render(layout, w, h, fields, timeout=90):
    """PNG bytes of the template at exactly w x h."""
    if layout not in LAYOUTS:
        raise ValueError("unknown layout")
    if not (200 <= w <= 4096 and 200 <= h <= 4096):
        raise ValueError("size must be 200-4096 px per side")
    tmp = tempfile.mkdtemp(dir=os.path.join(store.DATA, "tmp"))
    try:
        src, out = os.path.join(tmp, "page.html"), os.path.join(tmp, "out.png")
        with open(src, "w") as fh:
            fh.write(page(layout, w, h, fields))
        # --no-sandbox: Chromium's sandbox needs user namespaces this unprivileged container doesn't allow; the page is
        # our own template with escaped text and local images only, and the network is off for it.
        cmd = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
               "--allow-file-access-from-files", "--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
               f"--user-data-dir={tmp}/profile", f"--window-size={w},{h}", "--virtual-time-budget=4000",
               f"--screenshot={out}", "file://" + src]
        subprocess.run(cmd, timeout=timeout, capture_output=True, check=False)
        if not os.path.exists(out):
            raise RuntimeError("the renderer produced no image")
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        subprocess.run(["rm", "-rf", tmp], check=False)
