"""The library: every image with how it was made, and the job list. SQLite in DATA/studio.db, PNGs in DATA/items,
thumbnails (WebP, 384 px) in DATA/thumbs. Nothing is ever overwritten: edits, upscales and renders are new items
that point at their parent."""
import io, json, os, secrets, sqlite3, threading, time

from PIL import Image

DATA = os.environ.get("STUDIO_DATA", "/var/lib/studio")
THUMB = 384
_lock = threading.Lock()


def _db():
    con = sqlite3.connect(os.path.join(DATA, "studio.db"), timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init():
    for d in ("items", "thumbs", "tmp"):
        os.makedirs(os.path.join(DATA, d), exist_ok=True)
    with _lock, _db() as con:
        con.executescript("""
        create table if not exists items (id text primary key, created integer, kind text, parent text, title text,
            prompt text, negative text, params text, w integer, h integer, alpha integer default 0, starred integer default 0,
            deleted integer default 0);
        create index if not exists items_created on items(created);
        create table if not exists jobs (id text primary key, created integer, kind text, status text, params text,
            images integer, done integer default 0, results text default '[]', error text, override integer default 0,
            started integer, finished integer, note text);
        create index if not exists jobs_created on jobs(created);
        """)
        con.execute("update jobs set status='queued', started=null where status='running'")   # interrupted by a restart


def new_id():
    return time.strftime("%Y%m%d") + "-" + secrets.token_hex(4)


def path(item_id, thumb=False):
    if not all(c.isalnum() or c == "-" for c in item_id):
        raise ValueError("bad id")
    return os.path.join(DATA, "thumbs" if thumb else "items", item_id + (".webp" if thumb else ".png"))


def add_image(png_or_img, kind, parent=None, title="", prompt="", negative="", params=None):
    """Save a PNG (bytes) or PIL image as a new item; returns its row."""
    im = Image.open(io.BytesIO(png_or_img)) if isinstance(png_or_img, (bytes, bytearray)) else png_or_img
    im.load()
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    im = im.convert("RGBA" if alpha else "RGB")
    iid = new_id()
    im.save(path(iid), "PNG", optimize=False, compress_level=6)
    t = im.copy()
    t.thumbnail((THUMB, THUMB))
    t.save(path(iid, thumb=True), "WEBP", quality=80)
    row = dict(id=iid, created=int(time.time()), kind=kind, parent=parent, title=title[:200], prompt=prompt, negative=negative,
               params=json.dumps(params or {}), w=im.width, h=im.height, alpha=int(alpha), starred=0, deleted=0)
    with _lock, _db() as con:
        con.execute("insert into items values (:id,:created,:kind,:parent,:title,:prompt,:negative,:params,:w,:h,:alpha,:starred,:deleted)", row)
    return item(iid)


def _item(r):
    d = dict(r)
    d["params"] = json.loads(d["params"] or "{}")
    return d


def item(iid):
    with _db() as con:
        r = con.execute("select * from items where id=? and deleted=0", (iid,)).fetchone()
    return _item(r) if r else None


def items(q="", starred=False, kind="", limit=200, before=None):
    sql, args = "select * from items where deleted=0", []
    if q:
        sql += " and (prompt like ? or title like ?)"; args += [f"%{q}%", f"%{q}%"]
    if starred:
        sql += " and starred=1"
    if kind:
        sql += " and kind=?"; args.append(kind)
    if before:
        sql += " and created < ?"; args.append(int(before))
    sql += " order by created desc, id desc limit ?"; args.append(min(int(limit), 500))
    with _db() as con:
        return [_item(r) for r in con.execute(sql, args)]


def update_item(iid, **fields):
    allowed = {k: v for k, v in fields.items() if k in ("starred", "title", "deleted")}
    if not allowed:
        return item(iid)
    with _lock, _db() as con:
        con.execute(f"update items set {', '.join(f'{k}=?' for k in allowed)} where id=?", [*allowed.values(), iid])
    if allowed.get("deleted"):
        for t in (False, True):
            try:
                os.remove(path(iid, thumb=t))
            except OSError:
                pass
    return item(iid)


def open_image(iid):
    it = item(iid)
    if not it:
        raise KeyError(iid)
    im = Image.open(path(iid))
    im.load()
    return it, im


# ---------- jobs ----------

def _job(r):
    d = dict(r)
    d["params"] = json.loads(d["params"] or "{}")
    d["results"] = json.loads(d["results"] or "[]")
    return d


def add_job(kind, params, images, override=False):
    jid = new_id()
    with _lock, _db() as con:
        con.execute("insert into jobs (id, created, kind, status, params, images, override) values (?,?,?,?,?,?,?)",
                    (jid, int(time.time()), kind, "queued", json.dumps(params), images, int(override)))
    return job(jid)


def job(jid):
    with _db() as con:
        r = con.execute("select * from jobs where id=?", (jid,)).fetchone()
    return _job(r) if r else None


def jobs(active_only=False, limit=50):
    sql = "select * from jobs"
    if active_only:
        sql += " where status in ('queued','held','running')"
    sql += " order by created desc limit ?"
    with _db() as con:
        return [_job(r) for r in con.execute(sql, (limit,))]


def update_job(jid, **f):
    if "results" in f:
        f["results"] = json.dumps(f["results"])
    with _lock, _db() as con:
        con.execute(f"update jobs set {', '.join(f'{k}=?' for k in f)} where id=?", [*f.values(), jid])
    return job(jid)


def next_jobs():
    with _db() as con:
        return [_job(r) for r in con.execute("select * from jobs where status in ('queued','held') order by created, id")]
