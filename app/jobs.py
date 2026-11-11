"""The job queue: one job at a time, in order. Image jobs go to Centre AI (CT 107), which shares its GPU with Tickdeck's
and Flipdeck's language model, so:
  - batches of more than HOLD_OVER images wait during US market hours (Mon-Fri 9:30-16:00 ET) unless overridden;
  - batches are sent two images per request, so a chat request can take the GPU in between.
Cut-outs, resizes and template renders run here on the CPU. Results are new library items."""
import asyncio, base64, datetime, logging, os, time
from zoneinfo import ZoneInfo

import httpx

from . import imaging, render, store

log = logging.getLogger("studio")
ET = ZoneInfo("America/New_York")
HOLD_OVER = 3
AI_URL = os.environ.get("AI_URL", "http://192.168.1.193:11500")
AI_TOKEN = os.environ.get("AI_TOKEN", "")
STYLES = {
    "none": "",
    "photo": ", professional photograph, natural light, sharp focus, high detail",
    "cinematic": ", cinematic lighting, dramatic, shallow depth of field, rich color, film still",
    "illustration": ", digital illustration, clean lines, vibrant colors, polished",
    "3d": ", 3D render, octane render, soft studio lighting, glossy materials",
    "flat": ", flat vector illustration, simple shapes, bold colors, minimal, clean background",
}


def market_hours(now=None):
    t = (now or datetime.datetime.now(ET)).astimezone(ET)
    return t.weekday() < 5 and datetime.time(9, 30) <= t.time() < datetime.time(16, 0)


def held(job, now=None):
    return job["images"] > HOLD_OVER and not job["override"] and market_hours(now)


class Worker:
    def __init__(self, transport=None):
        self.http = httpx.AsyncClient(base_url=AI_URL, timeout=httpx.Timeout(960, connect=10), transport=transport,
                                      headers={"Authorization": f"Bearer {AI_TOKEN}", "X-Max-Wait": "900"})
        self.wake = asyncio.Event()
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
        await self.http.aclose()

    async def ai(self, path, body):
        r = await self.http.post(path, json=body)
        if r.status_code != 200:
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except ValueError:
                msg = r.text
            raise RuntimeError(f"Centre AI: {str(msg)[:200]} ({r.status_code})")
        return [(base64.b64decode(d["b64_json"]), d.get("seed")) for d in r.json()["data"]]

    async def loop(self):
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job loop")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def step(self):
        """Run every job that may run now, oldest first; held jobs stay held until the close."""
        for j in store.next_jobs():
            if held(j):
                if j["status"] != "held":
                    store.update_job(j["id"], status="held", note="Held until 4:00 PM ET (market hours; Tickdeck uses the GPU's AI)")
                continue
            store.update_job(j["id"], status="running", started=int(time.time()), note=None)
            try:
                await self.run(j)
                store.update_job(j["id"], status="done", finished=int(time.time()))
            except Exception as e:
                log.warning("job %s failed: %s", j["id"], e)
                store.update_job(j["id"], status="error", error=str(e)[:300], finished=int(time.time()))

    def saved(self, j, ids):
        store.update_job(j["id"], results=ids, done=len(ids))

    async def run(self, j):
        p, kind, ids = j["params"], j["kind"], []
        if kind == "generate":
            prompt = p["prompt"] + STYLES.get(p.get("style", "none"), "")
            seed = p.get("seed")
            left = p["n"]
            while left:
                n = min(2, left)
                body = {"prompt": prompt, "negative_prompt": p.get("negative") or None, "size": p["size"], "quality": p["quality"], "n": n}
                if body["negative_prompt"] is None:
                    del body["negative_prompt"]
                if seed is not None:
                    body["seed"] = seed + (p["n"] - left)
                for png, sd in await self.ai("/v1/images/generations", body):
                    ids.append(store.add_image(png, "generate", prompt=p["prompt"], negative=p.get("negative", ""),
                                               params={**{k: p[k] for k in ("size", "quality", "style") if k in p}, "seed": sd})["id"])
                    self.saved(j, ids)
                left -= n
        elif kind in ("edit", "outpaint"):
            it, im = store.open_image(p["item"])
            if kind == "edit":
                img, mask, size = imaging.edit_inputs(im, base64.b64decode(p["mask"]))
            else:
                img, mask, size = imaging.outpaint_inputs(im, p["left"], p["top"], p["right"], p["bottom"])
            body = {"prompt": p["prompt"] or it["prompt"] or "seamless continuation of the scene", "image": base64.b64encode(img).decode(),
                    "mask": base64.b64encode(mask).decode(), "quality": p.get("quality", "final"), "n": p.get("n", 1),
                    "strength": p.get("strength", 1.0)}
            if p.get("negative"):
                body["negative_prompt"] = p["negative"]
            for png, sd in await self.ai("/v1/images/edits", body):
                ids.append(store.add_image(png, kind, parent=it["id"], prompt=body["prompt"],
                                           params={"seed": sd, "strength": body["strength"], **({k: p[k] for k in ("left", "top", "right", "bottom")} if kind == "outpaint" else {})})["id"])
                self.saved(j, ids)
        elif kind == "upscale":
            it, im = store.open_image(p["item"])
            has_alpha = im.mode == "RGBA"
            [(png, _)] = await self.ai("/v1/images/upscale", {"image": base64.b64encode(imaging.png(im.convert("RGB"))).decode(), "scale": p["scale"]})
            out = png
            if has_alpha:                                        # keep transparency: scale the alpha channel here
                from PIL import Image
                import io
                big = Image.open(io.BytesIO(png)).convert("RGBA")
                big.putalpha(im.split()[3].resize(big.size, Image.LANCZOS))
                out = big
            ids.append(store.add_image(out, "upscale", parent=it["id"], prompt=it["prompt"], params={"scale": p["scale"]})["id"])
        elif kind == "cutout":
            it, im = store.open_image(p["item"])
            out = await asyncio.to_thread(imaging.cutout, im)
            ids.append(store.add_image(out, "cutout", parent=it["id"], prompt=it["prompt"])["id"])
        elif kind == "render":
            png = await asyncio.to_thread(render.render, p["layout"], p["w"], p["h"], p["fields"])
            ids.append(store.add_image(png, "template", title=p["fields"].get("headline", ""), params={"layout": p["layout"], "w": p["w"], "h": p["h"],
                                       "fields": p["fields"]})["id"])
        else:
            raise ValueError(f"unknown job kind {kind}")
        self.saved(j, ids)
