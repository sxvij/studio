"""Studio tests: python3 tests/test_studio.py  (in CT 114: needs fastapi, httpx, Pillow, chromium; no Centre AI)."""
import base64, datetime, io, json, os, sys, tempfile, time, unittest
tmp = tempfile.mkdtemp()
os.environ.update(STUDIO_DATA=tmp, AI_TOKEN="test", AI_URL="http://ai")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import httpx
from PIL import Image
from fastapi.testclient import TestClient
from app import imaging, jobs, main, render, store

H = {"X-Studio": "1"}


def png(w, h, color=(200, 30, 30), mode="RGB"):
    b = io.BytesIO()
    Image.new(mode, (w, h), color).save(b, "PNG")
    return b.getvalue()


class FakeAI:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append((request.url.path, body))
        if request.url.path == "/v1/images/generations":
            w, h = map(int, body["size"].split("x"))
            data = [{"b64_json": base64.b64encode(png(w, h)).decode(), "seed": body.get("seed", 100) + i} for i in range(body["n"])]
        elif request.url.path == "/v1/images/edits":
            im = Image.open(io.BytesIO(base64.b64decode(body["image"])))
            data = [{"b64_json": base64.b64encode(png(*im.size, (0, 90, 0))).decode(), "seed": 5} for _ in range(body["n"])]
        else:
            im = Image.open(io.BytesIO(base64.b64decode(body["image"])))
            data = [{"b64_json": base64.b64encode(png(im.width * body["scale"], im.height * body["scale"])).decode()}]
        return httpx.Response(200, json={"created": 1, "data": data})


class Units(unittest.TestCase):
    def test_market_hours_hold(self):
        et = jobs.ET
        wed_noon = datetime.datetime(2026, 11, 11, 12, 0, tzinfo=et)
        wed_eve = datetime.datetime(2026, 11, 11, 16, 0, tzinfo=et)
        sat = datetime.datetime(2026, 11, 14, 12, 0, tzinfo=et)
        big, small = {"images": 4, "override": 0}, {"images": 3, "override": 0}
        self.assertEqual([jobs.held(big, wed_noon), jobs.held(small, wed_noon), jobs.held(big, wed_eve), jobs.held(big, sat)], [True, False, False, False])
        self.assertFalse(jobs.held({"images": 8, "override": 1}, wed_noon))

    def test_edit_and_outpaint_inputs(self):
        im = Image.new("RGB", (2000, 1500), "red")
        mask = png(500, 375, (255, 255, 255))
        img, m, (w, h) = imaging.edit_inputs(im, mask)
        self.assertTrue(w * h <= imaging.MAX_EDIT_PIXELS and w % 8 == 0 and h % 8 == 0)
        self.assertEqual(Image.open(io.BytesIO(m)).size, (w, h))
        img, m, (w, h) = imaging.outpaint_inputs(Image.new("RGB", (512, 512), "blue"), 256, 0, 256, 0)
        self.assertEqual((w, h), (1024, 512))
        mk = Image.open(io.BytesIO(m)).convert("L")
        self.assertEqual((mk.getpixel((10, 256)), mk.getpixel((512, 256)), mk.getpixel((1010, 256))), (255, 0, 255))

    def test_resize(self):
        im = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
        self.assertEqual(imaging.resize(im, 1320, 2868).size, (1320, 2868))
        c = imaging.resize(Image.new("RGB", (800, 600), "red"), 1000, 1000, "contain", "#00ff00")
        self.assertEqual((c.size, c.getpixel((500, 5)), c.getpixel((500, 500))), ((1000, 1000), (0, 255, 0), (255, 0, 0)))

    def test_template_escapes_and_renders_exact_size(self):
        store.init()
        page = render.page("hero", 1200, 630, {"headline": "<script>alert(1)</script>", "accent": "red; x"})
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("#f4c64e", page)                      # a bad colour falls back to the default
        out = render.render("appshot", 660, 1434, {"headline": "Master the table", "subline": "Strategy lab"})
        self.assertEqual(Image.open(io.BytesIO(out)).size, (660, 1434))


class Api(unittest.TestCase):
    def setUp(self):
        self.ai = FakeAI()
        self.app = main.create_app(transport=httpx.MockTransport(self.ai))
        self.c = TestClient(self.app).__enter__()

    def tearDown(self):
        self.c.__exit__(None, None, None)

    def post(self, path, body, headers=H):
        return self.c.post(path, json=body, headers=headers)

    def wait(self, jid, t=60):
        end = time.time() + t
        while time.time() < end:
            j = self.c.get("/api/jobs").json()["jobs"]
            j = next(x for x in j if x["id"] == jid)
            if j["status"] in ("done", "error", "held", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("job did not finish")

    def test_guard(self):
        self.assertEqual(self.c.post("/api/generate", json={"prompt": "x"}).status_code, 400)
        self.assertEqual(self.c.post("/api/generate", content="prompt=x", headers={**H, "content-type": "application/x-www-form-urlencoded"}).status_code, 400)

    def test_generate_split_and_library(self):
        r = self.post("/api/generate", {"prompt": "dice on felt", "n": 3, "size": "1216x832", "style": "photo", "seed": 7, "override": True})
        self.assertEqual(r.status_code, 200, r.text)
        j = self.wait(r.json()["id"])
        self.assertEqual((j["status"], j["done"], len(j["results"])), ("done", 3, 3), j)
        sent = [b for p, b in self.ai.calls if p == "/v1/images/generations"]
        self.assertEqual([(b["n"], b["seed"]) for b in sent], [(2, 7), (1, 9)])
        self.assertTrue(sent[0]["prompt"].startswith("dice on felt, professional photograph"))
        items = self.c.get("/api/items").json()["items"]
        self.assertEqual(items[0]["prompt"], "dice on felt")
        self.assertEqual(self.c.get(f"/api/items/{items[0]['id']}/image").content[:4], b"\x89PNG")
        self.assertEqual(self.c.get(f"/api/items/{items[0]['id']}/thumb").headers["content-type"], "image/webp")
        star = self.post(f"/api/items/{items[0]['id']}", {"starred": True}).json()
        self.assertEqual(star["starred"], 1)
        self.assertEqual(len(self.c.get("/api/items?starred=true").json()["items"]), 1)
        self.post(f"/api/items/{items[0]['id']}", {"deleted": True})
        self.assertEqual(self.c.get(f"/api/items/{items[0]['id']}").status_code, 404)

    def test_edit_outpaint_upscale_resize_render(self):
        up = self.post("/api/upload", {"data": base64.b64encode(png(640, 480)).decode(), "name": "shot.png"}).json()
        mask = base64.b64encode(png(640, 480, (255, 255, 255))).decode()
        j = self.wait(self.post("/api/edit", {"item": up["id"], "mask": mask, "prompt": "gold chips", "n": 2, "strength": 0.8}).json()["id"])
        self.assertEqual((j["status"], len(j["results"])), ("done", 2), j)
        self.assertEqual(self.c.get(f"/api/items/{j['results'][0]}").json()["parent"], up["id"])
        j = self.wait(self.post("/api/outpaint", {"item": up["id"], "left": 128, "right": 128}).json()["id"])
        self.assertEqual(self.c.get(f"/api/items/{j['results'][0]}").json()["w"], 896)
        rgba = self.post("/api/upload", {"data": base64.b64encode(png(100, 80, (1, 2, 3, 0), "RGBA")).decode()}).json()
        j = self.wait(self.post("/api/upscale", {"item": rgba["id"], "scale": 2}).json()["id"])
        out = self.c.get(f"/api/items/{j['results'][0]}").json()
        self.assertEqual((out["w"], out["h"], out["alpha"]), (200, 160, 1))
        r = self.post("/api/resize", {"item": up["id"], "preset": "og"}).json()
        self.assertEqual((r["w"], r["h"], r["kind"]), (1200, 630, "resize"))
        j = self.wait(self.post("/api/render", {"layout": "hero", "preset": "og", "fields": {"headline": "Roll with it", "background": up["id"]}}).json()["id"])
        self.assertEqual(j["status"], "done", j)
        self.assertEqual(self.c.get(f"/api/items/{j['results'][0]}").json()["w"], 1200)
        self.assertEqual(self.post("/api/render", {"layout": "hero", "fields": {"background": "20260101-deadbeef"}}).status_code, 404)

    def test_held_run_now_and_cancel(self):
        orig = jobs.market_hours
        jobs.market_hours = lambda now=None: True
        try:
            jid = self.post("/api/generate", {"prompt": "x", "n": 6}).json()["id"]
            self.assertEqual(self.wait(jid)["status"], "held")
            j2 = self.post("/api/generate", {"prompt": "y", "n": 4}).json()["id"]
            self.assertEqual(self.wait(j2)["status"], "held")
            self.assertEqual(self.post(f"/api/jobs/{j2}", {"action": "cancel"}).json()["status"], "cancelled")
            self.post(f"/api/jobs/{jid}", {"action": "run-now"})
            self.assertEqual(self.wait(jid)["status"], "done")
        finally:
            jobs.market_hours = orig

    def test_validation(self):
        self.assertEqual(self.post("/api/generate", {"prompt": ""}).status_code, 400)
        self.assertEqual(self.post("/api/generate", {"prompt": "x", "size": "999x999"}).status_code, 400)
        self.assertEqual(self.post("/api/generate", {"prompt": "x", "n": 9}).status_code, 400)
        self.assertEqual(self.post("/api/upscale", {"item": "../etc/passwd", "scale": 2}).status_code, 404)
        self.assertEqual(self.post("/api/upload", {"data": base64.b64encode(b"not an image").decode()}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=1)
