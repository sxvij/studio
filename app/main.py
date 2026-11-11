"""Studio API (127.0.0.1:8122, behind nginx and the Cloudflare Access gate). Long work runs as jobs (Cloudflare cuts
requests at 100 s): POST returns the job, the page polls /api/jobs. Writes need JSON and the X-Studio: 1 header, so
other sites can't trigger them with a form or a plain fetch."""
import base64, binascii, io, logging, re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from . import imaging, jobs, render, store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
SIZES = {"1024x1024": "Square", "1216x832": "Landscape", "1344x768": "Wide", "1536x640": "Banner", "832x1216": "Portrait",
         "768x1344": "Tall", "1152x896": "Photo landscape", "896x1152": "Photo portrait", "640x1536": "Skyscraper"}
ID = re.compile(r"^[0-9]{8}-[0-9a-f]{8}$")


def create_app(transport=None):
    worker = jobs.Worker(transport)

    @asynccontextmanager
    async def lifespan(app):
        store.init()
        worker.start()
        yield
        await worker.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.worker = worker

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            if request.headers.get("x-studio") != "1" or not request.headers.get("content-type", "").startswith("application/json"):
                return JSONResponse({"detail": "Requests must be JSON with the X-Studio header"}, status_code=400)
        return await call_next(request)

    async def body(request):
        try:
            b = await request.json()
        except ValueError:
            raise HTTPException(400, "Body must be JSON")
        if not isinstance(b, dict):
            raise HTTPException(400, "Body must be a JSON object")
        return b

    def need_item(iid):
        if not isinstance(iid, str) or not ID.match(iid) or not store.item(iid):
            raise HTTPException(404, "No such image")
        return iid

    def queue(kind, params, images, override=False):
        j = store.add_job(kind, params, images, override)
        worker.wake.set()
        return j

    def num(b, k, lo, hi, default, typ=int):
        v = b.get(k, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise HTTPException(400, f"{k} must be between {lo} and {hi}")
        return typ(v)

    def text(b, k, n, required=False):
        v = b.get(k) or ""
        if not isinstance(v, str) or len(v) > n:
            raise HTTPException(400, f"{k} must be text up to {n} characters")
        if required and not v.strip():
            raise HTTPException(400, f"{k} is required")
        return v.strip()

    @app.get("/api/config")
    def config():
        return {"sizes": SIZES, "styles": list(jobs.STYLES), "presets": {k: {"label": v[0], "w": v[1], "h": v[2]} for k, v in imaging.PRESETS.items()},
                "layouts": render.LAYOUTS, "hold_over": jobs.HOLD_OVER, "market_hours": jobs.market_hours()}

    # ---------- library ----------
    @app.get("/api/items")
    def items(q: str = "", starred: bool = False, kind: str = "", before: int = 0, limit: int = 120):
        return {"items": store.items(q[:100], starred, kind[:20], limit, before or None)}

    @app.get("/api/items/{iid}")
    def item(iid: str):
        return store.item(need_item(iid))

    @app.get("/api/items/{iid}/image")
    def image(iid: str, download: bool = False):
        need_item(iid)
        return FileResponse(store.path(iid), media_type="image/png", headers={"Cache-Control": "private, max-age=31536000, immutable"},
                            filename=f"studio-{iid}.png" if download else None)

    @app.get("/api/items/{iid}/thumb")
    def thumb(iid: str):
        need_item(iid)
        return FileResponse(store.path(iid, thumb=True), media_type="image/webp", headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @app.post("/api/items/{iid}")
    async def update(iid: str, request: Request):
        b = await body(request)
        need_item(iid)
        f = {}
        if "starred" in b:
            f["starred"] = int(bool(b["starred"]))
        if "title" in b:
            f["title"] = text(b, "title", 200)
        if b.get("deleted") is True:
            f["deleted"] = 1
        return store.update_item(iid, **f) or {"deleted": True}

    @app.post("/api/upload")
    async def upload(request: Request):
        b = await body(request)
        data = b.get("data", "")
        try:
            raw = base64.b64decode(data.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError, AttributeError):
            raise HTTPException(400, "data must be a base64 image")
        if len(raw) > 25 * 2**20:
            raise HTTPException(413, "Images up to 25 MB")
        from PIL import Image, UnidentifiedImageError
        try:
            im = Image.open(io.BytesIO(raw))
            if im.width * im.height > 40_000_000:
                raise HTTPException(413, "Images up to 40 megapixels")
            im.load()
        except (UnidentifiedImageError, OSError):
            raise HTTPException(400, "Not an image Pillow can read (PNG, JPEG, WebP)")
        from PIL import ImageOps
        im = ImageOps.exif_transpose(im)
        return store.add_image(im, "upload", title=text(b, "name", 200))

    # ---------- jobs ----------
    @app.get("/api/jobs")
    def list_jobs(active: bool = False):
        return {"jobs": store.jobs(active), "market_hours": jobs.market_hours()}

    @app.post("/api/jobs/{jid}")
    async def job_action(jid: str, request: Request):
        b = await body(request)
        j = store.job(jid) if ID.match(jid) else None
        if not j:
            raise HTTPException(404, "No such job")
        if b.get("action") == "run-now" and j["status"] == "held":
            j = store.update_job(jid, override=1, status="queued", note=None)
            worker.wake.set()
        elif b.get("action") == "cancel" and j["status"] in ("queued", "held"):
            j = store.update_job(jid, status="cancelled")
        else:
            raise HTTPException(409, "That job can't do that now")
        return j

    @app.post("/api/generate")
    async def generate(request: Request):
        b = await body(request)
        size = b.get("size", "1024x1024")
        if size not in SIZES:
            raise HTTPException(400, "Unknown size")
        style = b.get("style", "none")
        if style not in jobs.STYLES:
            raise HTTPException(400, "Unknown style")
        quality = b.get("quality", "draft")
        if quality not in ("draft", "final"):
            raise HTTPException(400, "quality must be draft or final")
        seed = b.get("seed")
        if seed not in (None, "") and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32):
            raise HTTPException(400, "seed must be a whole number")
        n = num(b, "n", 1, 8, 1)
        p = {"prompt": text(b, "prompt", 1500, True), "negative": text(b, "negative", 600), "size": size, "style": style,
             "quality": quality, "n": n, "seed": seed if seed not in ("", None) else None}
        return queue("generate", p, n, bool(b.get("override")))

    @app.post("/api/edit")
    async def edit(request: Request):
        b = await body(request)
        iid = need_item(b.get("item"))
        mask = b.get("mask", "")
        try:
            mb = base64.b64decode(mask.split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError, AttributeError):
            raise HTTPException(400, "mask must be a base64 PNG")
        if not mb.startswith(b"\x89PNG"):
            raise HTTPException(400, "mask must be a PNG")
        n = num(b, "n", 1, 4, 1)
        p = {"item": iid, "mask": base64.b64encode(mb).decode(), "prompt": text(b, "prompt", 1500), "negative": text(b, "negative", 600),
             "strength": num(b, "strength", 0.2, 1.0, 1.0, float), "quality": "draft" if b.get("quality") == "draft" else "final", "n": n}
        return queue("edit", p, n)

    @app.post("/api/outpaint")
    async def outpaint(request: Request):
        b = await body(request)
        iid = need_item(b.get("item"))
        p = {"item": iid, "prompt": text(b, "prompt", 1500), "negative": "", "quality": "final", "n": 1}
        for k in ("left", "top", "right", "bottom"):
            p[k] = num(b, k, 0, 1024, 0)
        if not any(p[k] for k in ("left", "top", "right", "bottom")):
            raise HTTPException(400, "Extend at least one side")
        return queue("outpaint", p, 1)

    @app.post("/api/upscale")
    async def upscale(request: Request):
        b = await body(request)
        iid = need_item(b.get("item"))
        scale = b.get("scale", 2)
        if scale not in (2, 4):
            raise HTTPException(400, "scale must be 2 or 4")
        it = store.item(iid)
        if it["w"] * it["h"] * scale * scale > 4096 * 4096:
            raise HTTPException(400, "Too big to upscale that much: try 2x, or resize first")
        return queue("upscale", {"item": iid, "scale": scale}, 1)

    @app.post("/api/cutout")
    async def cutout(request: Request):
        b = await body(request)
        return queue("cutout", {"item": need_item(b.get("item"))}, 0)

    @app.post("/api/render")
    async def do_render(request: Request):
        b = await body(request)
        layout = b.get("layout")
        if layout not in render.LAYOUTS:
            raise HTTPException(400, "Unknown layout")
        if b.get("preset"):
            if b["preset"] not in imaging.PRESETS:
                raise HTTPException(400, "Unknown size preset")
            _, w, h = imaging.PRESETS[b["preset"]]
        else:
            w, h = num(b, "w", 200, 4096, 1200), num(b, "h", 200, 4096, 630)
        f = b.get("fields") or {}
        if not isinstance(f, dict):
            raise HTTPException(400, "fields must be an object")
        fields = {k: str(f.get(k) or "")[:300] for k in ("headline", "subline", "button", "accent", "ink", "bg", "font", "align", "position")}
        for k in ("background", "foreground", "logo"):
            if f.get(k):
                fields[k] = need_item(f[k])
        return queue("render", {"layout": layout, "w": w, "h": h, "fields": fields}, 0)

    @app.post("/api/resize")
    async def do_resize(request: Request):
        b = await body(request)
        iid = need_item(b.get("item"))
        if b.get("preset"):
            if b["preset"] not in imaging.PRESETS:
                raise HTTPException(400, "Unknown size preset")
            label, w, h = imaging.PRESETS[b["preset"]]
        else:
            label, w, h = "custom", num(b, "w", 16, 8192, 1024), num(b, "h", 16, 8192, 1024)
        mode = "contain" if b.get("mode") == "contain" else "cover"
        fx, fy = num(b, "fx", 0, 1, 0.5, float), num(b, "fy", 0, 1, 0.5, float)
        it, im = store.open_image(iid)
        out = imaging.resize(im, w, h, mode, render.color(b.get("bg"), "#000000"), (fx, fy))
        return store.add_image(out, "resize", parent=iid, title=f"{label} {w}x{h}", prompt=it["prompt"], params={"w": w, "h": h, "mode": mode})

    return app


app = create_app()
