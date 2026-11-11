"""Image work done here on the CPU: preparing edits and outpainting for Centre AI, resizing to store and social sizes,
background removal (IS-Net, the same model rembg uses, run with onnxruntime)."""
import io, os

from PIL import Image, ImageFilter, ImageOps

MAX_EDIT_PIXELS = 1536 * 1536        # Centre AI's limit for edits

# Sizes the store page and marketing need. (label, width, height)
PRESETS = {
    "appstore-iphone": ("App Store iPhone 6.9\"", 1320, 2868),
    "appstore-ipad": ("App Store iPad 13\"", 2064, 2752),
    "og": ("Link preview (Open Graph)", 1200, 630),
    "web-hero": ("Website hero banner", 1920, 800),
    "ig-square": ("Instagram square", 1080, 1080),
    "ig-portrait": ("Instagram portrait", 1080, 1350),
    "story": ("Story / Reel / TikTok", 1080, 1920),
    "x-header": ("X / Twitter header", 1500, 500),
    "fb-cover": ("Facebook cover", 1640, 624),
    "youtube": ("YouTube thumbnail", 1280, 720),
    "icon": ("App icon", 1024, 1024),
}


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def fit_for_edit(im, mask=None):
    """Shrink (never enlarge) so width*height fits Centre AI's edit limit, with sides that are multiples of 8."""
    w, h = im.size
    scale = min(1.0, (MAX_EDIT_PIXELS / (w * h)) ** 0.5)
    nw, nh = max(64, int(w * scale) // 8 * 8), max(64, int(h * scale) // 8 * 8)
    if (nw, nh) != (w, h):
        im = im.resize((nw, nh), Image.LANCZOS)
        if mask is not None:
            mask = mask.resize((nw, nh), Image.LANCZOS)
    return im, mask


def edit_inputs(im, mask_png):
    """The image (RGB) and the painted mask (white = repaint), the same size, ready for /v1/images/edits."""
    mask = Image.open(io.BytesIO(mask_png)).convert("L")
    if mask.size != im.size:
        mask = mask.resize(im.size, Image.NEAREST)
    if im.mode == "RGBA":                       # transparent parts become grey, then may be repainted
        bg = Image.new("RGB", im.size, (128, 128, 128))
        bg.paste(im, mask=im.split()[3])
        im = bg
    im, mask = fit_for_edit(im.convert("RGB"), mask)
    mask = mask.point(lambda v: 255 if v > 127 else 0)
    return png(im), png(mask.convert("RGB")), im.size


def outpaint_inputs(im, left, top, right, bottom, overlap=24):
    """A bigger canvas with the image in it, the new border filled with blurred, stretched edges (a better start than
    flat grey), and a mask that repaints the border plus a little of the image so the seam blends."""
    im = im.convert("RGB")
    w, h = im.size
    W, H = w + left + right, h + top + bottom
    base = im.resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(radius=max(W, H) / 40))
    base.paste(im, (left, top))
    mask = Image.new("L", (W, H), 255)
    inner = (left + (overlap if left else 0), top + (overlap if top else 0),
             left + w - (overlap if right else 0), top + h - (overlap if bottom else 0))
    mask.paste(0, inner)
    base, mask = fit_for_edit(base, mask)
    return png(base), png(mask.convert("RGB")), base.size


def resize(im, w, h, mode="cover", bg="#000000", focus=(0.5, 0.5)):
    """cover: fill the frame and crop (around focus, 0-1 each); contain: fit inside on a background colour."""
    alpha = im.mode == "RGBA"
    if mode == "contain":
        out = Image.new("RGBA" if alpha else "RGB", (w, h), bg if not alpha else (0, 0, 0, 0))
        fit = ImageOps.contain(im, (w, h), Image.LANCZOS)
        out.paste(fit, ((w - fit.width) // 2, (h - fit.height) // 2), fit if alpha else None)
        return out
    return ImageOps.fit(im, (w, h), Image.LANCZOS, centering=focus)


# ---------- background removal ----------

MODEL = os.environ.get("STUDIO_CUTOUT_MODEL", "/var/lib/studio/models/isnet-general-use.onnx")
_session = None


def cutout(im):
    """The image with its background made transparent (IS-Net general use, 1024x1024 input, as rembg does)."""
    global _session
    import numpy as np
    import onnxruntime as ort
    if _session is None:
        _session = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    rgb = im.convert("RGB")
    x = np.asarray(rgb.resize((1024, 1024), Image.LANCZOS), dtype=np.float32)
    x = x / max(float(x.max()), 1e-6)
    x = (x - 0.5) / 1.0
    x = x.transpose(2, 0, 1)[None]
    inp = _session.get_inputs()[0].name
    pred = _session.run(None, {inp: x})[0][:, 0, :, :]
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / max(hi - lo, 1e-6)
    alpha = Image.fromarray((pred[0] * 255).clip(0, 255).astype("uint8"), "L").resize(rgb.size, Image.LANCZOS)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out
