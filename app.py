"""Restaurant POS server (dine-in / takeaway / delivery, KOT, GST billing, daily reports).
Run: python app.py      Self-check: python app.py test
Only needs Python 3 - no packages. All data lives in pos.db next to this file: back it up."""
import csv, hmac, json, os, socket, sqlite3, sys, time, webbrowser
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
# Cloud hosting sets these: POS_DB must point at a persistent disk, or data is lost on every redeploy.
DB = Path(os.environ.get("POS_DB") or HERE / "pos.db")
PORT = int(os.environ.get("PORT") or 8000)
TYPES = ("dine_in", "takeaway", "delivery")
PAYMENTS = ("Cash", "UPI", "Card")
DEFAULT_SETTINGS = {"name": "Guns & Roses Café", "address": "Kondapur, Hyderabad", "phone": "", "gstin": "", "fssai": "",
                    "tax_pct": "5", "tables": "8", "footer": "Thank you! Visit again.",
                    "pin": os.environ.get("POS_PIN") or "1234"}  # set POS_PIN when hosting so a fresh site never has 1234

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
    price INTEGER NOT NULL CHECK(price >= 0), active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, type TEXT NOT NULL, table_no INTEGER,
    customer TEXT, phone TEXT, address TEXT, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'billed', 'cancelled')),
    bill_no INTEGER UNIQUE, billed TEXT, day TEXT, payment TEXT, subtotal INTEGER, discount_pct TEXT,
    discount INTEGER, tax_pct TEXT, cgst INTEGER, sgst INTEGER, round_off INTEGER, total INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_order_per_table ON orders(table_no) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS orders_day ON orders(day);
CREATE TABLE IF NOT EXISTS kots(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
    created TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new', 'ready')));
CREATE TABLE IF NOT EXISTS lines(id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders,
    kot_id INTEGER NOT NULL REFERENCES kots, item_id INTEGER, name TEXT NOT NULL, price INTEGER NOT NULL,
    qty INTEGER NOT NULL CHECK(qty > 0));
CREATE INDEX IF NOT EXISTS lines_order ON lines(order_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def db(path=DB):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init(con):
    con.execute("PRAGMA journal_mode=WAL")  # kitchen/floor polling reads don't block billing writes
    with con:
        con.executescript(SCHEMA)
        con.executemany("INSERT OR IGNORE INTO settings VALUES(?, ?)", DEFAULT_SETTINGS.items())
        if not con.execute("SELECT 1 FROM items").fetchone():  # first run: load the starter menu
            with open(HERE / "menu.csv", encoding="utf-8") as f:
                con.executemany("INSERT INTO items(category, name, price) VALUES(?, ?, ?)",
                                [(r["category"], r["name"], paise(r["price"])) for r in csv.DictReader(f)])


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---- money: integer paise everywhere, half-up rounding ----
def paise(x):
    return int((Decimal(str(x)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def pct(amount, p):
    return int((Decimal(amount) * Decimal(str(p)) / 100).quantize(Decimal(1), ROUND_HALF_UP))


def totals(lines, discount_pct, tax_pct):
    """lines: [(price, qty)]. GST on the discounted amount, split equally into CGST + SGST,
    grand total rounded to the nearest rupee."""
    subtotal = sum(p * q for p, q in lines)
    discount = pct(subtotal, discount_pct)
    cgst = sgst = pct(subtotal - discount, Decimal(str(tax_pct)) / 2)
    raw = subtotal - discount + cgst + sgst
    total = (raw + 50) // 100 * 100
    return subtotal, discount, cgst, sgst, total - raw, total


def check_pct(v):
    v = str(v or 0)
    if not 0 <= Decimal(v) <= 100:
        raise ValueError("percentage must be 0-100")
    return v


def settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT * FROM settings")}


def order_out(con, r):
    o = dict(r)
    o["lines"] = [dict(l) for l in con.execute(
        "SELECT l.id, l.name, l.price, l.qty, l.kot_id, k.status AS kot_status FROM lines l "
        "JOIN kots k ON k.id = l.kot_id WHERE l.order_id = ? ORDER BY l.id", (r["id"],))]
    o["amount"] = sum(l["price"] * l["qty"] for l in o["lines"])
    o["pending"] = len({l["kot_id"] for l in o["lines"] if l["kot_status"] == "new"})
    return o


def get_order(con, oid):
    return order_out(con, con.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone())


# ---- GET routes (con, query) ----
def state(con, q):
    return {"settings": {k: v for k, v in settings(con).items() if k != "pin"},
            "items": [dict(r) for r in con.execute(
                "SELECT id, name, category, price FROM items WHERE active = 1 ORDER BY id")],  # menu.csv order
            "orders": [order_out(con, r) for r in con.execute("SELECT * FROM orders WHERE status = 'open' ORDER BY id")]}


def kitchen(con, q):
    kots = []
    for k in con.execute(
            "SELECT k.id, k.created, k.note, k.order_id, o.type, o.table_no, o.customer FROM kots k "
            "JOIN orders o ON o.id = k.order_id WHERE k.status = 'new' AND o.status != 'cancelled' ORDER BY k.id"):
        lines = [dict(l) for l in con.execute("SELECT name, qty FROM lines WHERE kot_id = ? ORDER BY id", (k["id"],))]
        if lines:  # every line removed -> nothing left to cook
            kots.append({**dict(k), "lines": lines})
    return kots


def report(con, q):
    day = q.get("day", [date.today().isoformat()])[0]
    bills = [order_out(con, r) for r in con.execute(
        "SELECT * FROM orders WHERE status = 'billed' AND day = ? ORDER BY bill_no DESC", (day,))]
    pay, typ, items = {}, {}, {}
    for b in bills:
        pay[b["payment"]] = pay.get(b["payment"], 0) + b["total"]
        typ[b["type"]] = typ.get(b["type"], 0) + b["total"]
        for l in b["lines"]:
            qty, amt = items.get(l["name"], (0, 0))
            items[l["name"]] = (qty + l["qty"], amt + l["qty"] * l["price"])
    return {"day": day, "count": len(bills),
            "sums": {k: sum(b[k] for b in bills) for k in ("subtotal", "discount", "cgst", "sgst", "round_off", "total")},
            "by_payment": pay, "by_type": typ,
            "items": sorted(([n, q, a] for n, (q, a) in items.items()), key=lambda x: -x[2]),
            "cancelled": con.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled' AND substr(created, 1, 10) = ?",
                                     (day,)).fetchone()[0],
            "bills": bills}


# ---- POST routes (con, body) ----
def save_item(con, d):
    name, cat, price = str(d["iname"]).strip(), str(d.get("icat") or "").strip() or "Other", paise(d["iprice"])
    if not name or price < 0:
        raise ValueError("name required, price must be >= 0")
    if d.get("iid"):
        con.execute("UPDATE items SET name = ?, category = ?, price = ? WHERE id = ?", (name, cat, price, int(d["iid"])))
    else:
        con.execute("INSERT INTO items(name, category, price) VALUES(?, ?, ?)", (name, cat, price))
    return {"ok": True}


def delete_item(con, d):
    # ponytail: soft delete; past bills keep their own name/price snapshot anyway
    con.execute("UPDATE items SET active = 0 WHERE id = ?", (int(d["id"]),))
    return {"ok": True}


def save_settings(con, d):
    if "tax_pct" in d:
        check_pct(d["tax_pct"])
    if "tables" in d and not 1 <= int(d["tables"]) <= 50:
        raise ValueError("tables must be 1-50")
    if d.get("pin") and not (str(d["pin"]).isdigit() and len(str(d["pin"])) >= 6):
        raise ValueError("PIN must be 6+ digits")
    for k in DEFAULT_SETTINGS:
        if k in d and not (k == "pin" and not d[k]):  # blank PIN field = keep current
            con.execute("UPDATE settings SET value = ? WHERE key = ?", (str(d[k]).strip(), k))
    return {"ok": True}


def new_order(con, d):
    typ, table = d.get("type"), None
    if typ not in TYPES:
        raise ValueError("bad order type")
    if typ == "dine_in":
        table = int(d["table_no"])
        if not 1 <= table <= int(settings(con)["tables"]):
            raise ValueError("bad table number")
    cust = [str(d.get(k) or "").strip()[:300] for k in ("customer", "phone", "address")]
    if typ == "delivery" and not (cust[1] and cust[2]):
        raise ValueError("delivery needs phone and address")
    cur = con.execute("INSERT INTO orders(type, table_no, customer, phone, address, created) VALUES(?, ?, ?, ?, ?, ?)",
                      (typ, table, *cust, now()))
    return {"id": cur.lastrowid}


def send_kot(con, d):
    oid = int(d["order_id"])
    if not con.execute("SELECT 1 FROM orders WHERE id = ? AND status = 'open'", (oid,)).fetchone():
        raise ValueError("order is not open")
    lines = []
    for l in d.get("lines") or []:
        qty = int(l["qty"])
        r = con.execute("SELECT id, name, price FROM items WHERE id = ? AND active = 1", (int(l["id"]),)).fetchone()
        if not r or not 1 <= qty <= 999:
            raise ValueError("invalid item or quantity")
        lines.append((r["id"], r["name"], r["price"], qty))
    if not lines:
        raise ValueError("no items")
    kid = con.execute("INSERT INTO kots(order_id, created, note) VALUES(?, ?, ?)",
                      (oid, now(), str(d.get("note") or "").strip()[:200])).lastrowid
    con.executemany("INSERT INTO lines(order_id, kot_id, item_id, name, price, qty) VALUES(?, ?, ?, ?, ?, ?)",
                    [(oid, kid, *l) for l in lines])
    return {"id": kid}


def remove_line(con, d):
    # ponytail: no audit trail for removed items; add a voids table if staff misuse becomes a worry
    if not con.execute("DELETE FROM lines WHERE id = ? AND order_id IN (SELECT id FROM orders WHERE status = 'open')",
                       (int(d["line_id"]),)).rowcount:
        raise ValueError("item not found or order already billed")
    return {"ok": True}


def cancel_order(con, d):
    if not con.execute("UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'open'",
                       (int(d["order_id"]),)).rowcount:
        raise ValueError("order is not open")
    return {"ok": True}


def kot_ready(con, d):
    con.execute("UPDATE kots SET status = 'ready' WHERE id = ?", (int(d["id"]),))
    return {"ok": True}


def bill_order(con, d):
    oid, payment = int(d["order_id"]), d.get("payment")
    if payment not in PAYMENTS:
        raise ValueError("pick a payment mode")
    lines = [(r["price"], r["qty"]) for r in con.execute("SELECT price, qty FROM lines WHERE order_id = ?", (oid,))]
    if not lines:
        raise ValueError("order has no items")
    tax_pct, disc = settings(con)["tax_pct"], check_pct(d.get("discount_pct"))
    sub, dis, cgst, sgst, ro, total = totals(lines, disc, tax_pct)
    t = now()
    # bill_no assigned inside the UPDATE so two tablets billing at once can't get the same number
    if not con.execute(
            "UPDATE orders SET status = 'billed', bill_no = (SELECT COALESCE(MAX(bill_no), 0) + 1 FROM orders), "
            "billed = ?, day = ?, payment = ?, subtotal = ?, discount_pct = ?, discount = ?, tax_pct = ?, "
            "cgst = ?, sgst = ?, round_off = ?, total = ? WHERE id = ? AND status = 'open'",
            (t, t[:10], payment, sub, disc, dis, tax_pct, cgst, sgst, ro, total, oid)).rowcount:
        raise ValueError("order is not open")
    return get_order(con, oid)


def backup(con, q):
    """Consistent snapshot of the whole database, safe to take while the café is billing."""
    snap = sqlite3.connect(":memory:")
    con.backup(snap)
    data = snap.serialize()
    snap.close()
    return data


GET = {"/api/state": state, "/api/kitchen": kitchen, "/api/report": report, "/api/backup": backup}
POST = {"/api/items": save_item, "/api/items/delete": delete_item, "/api/settings": save_settings,
        "/api/orders/new": new_order, "/api/orders/kot": send_kot, "/api/orders/remove_line": remove_line,
        "/api/orders/cancel": cancel_order, "/api/orders/bill": bill_order, "/api/kots/ready": kot_ready}


# ponytail: one global lockout, so guessing a 6-digit PIN online takes months; an attacker can also keep
# staff locked out - switch to per-IP lockout if that ever happens.
LOCK = {"fails": 0, "until": 0.0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # tablets poll every few seconds; keep the console readable
        pass

    def send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if ctype == "application/octet-stream":
            self.send_header("Content-Disposition", f'attachment; filename="pos-backup-{date.today()}.db"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self.send((HERE / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        self.api(GET.get(u.path), parse_qs(u.query))

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.send({"error": "bad request"}, 400)
        self.api(POST.get(self.path), data)

    def api(self, route, arg):
        if not route:
            return self.send({"error": "not found"}, 404)
        if time.time() < LOCK["until"]:
            return self.send({"error": "Too many wrong PINs. Wait a minute and try again."}, 429)
        con = db()
        try:
            if not hmac.compare_digest(self.headers.get("X-Pin", "").encode(), settings(con)["pin"].encode()):
                LOCK["fails"] += 1
                if LOCK["fails"] >= 5:  # 5 wrong in a row -> everyone locked out for 60 s
                    LOCK.update(fails=0, until=time.time() + 60)
                time.sleep(1)
                return self.send({"error": "Wrong PIN"}, 401)
            LOCK["fails"] = 0
            with con:  # one transaction per request, rolled back on any error
                out = route(con, arg)
            self.send(out, ctype="application/octet-stream" if isinstance(out, bytes) else "application/json")
        except sqlite3.IntegrityError:
            self.send({"error": "That table already has an open order. Refresh and try again."}, 409)
        except (ValueError, KeyError, TypeError, InvalidOperation) as e:
            self.send({"error": f"Invalid input: {e}"}, 400)
        finally:
            con.close()


def lan_ips():
    # ponytail: lists every address (Wi-Fi, Ethernet, VPN...) since stdlib can't tell which adapter is Wi-Fi
    ips = {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254."))) or ["THIS-PC-IP"]


def selftest():
    two = [(10000, 2), (4550, 1)]
    assert totals(two, 0, 5) == (24550, 0, 614, 614, 22, 25800)       # 613.75 -> 614 each, round to ₹258
    assert totals(two, 10, 5) == (24550, 2455, 552, 552, 1, 23200)    # GST on discounted amount
    assert totals(two, 0, 18) == (24550, 0, 2210, 2210, 30, 29000)
    assert totals([(4949, 1)], 0, 0) == (4949, 0, 0, 0, -49, 4900)    # rounds down too
    assert paise("249.50") == 24950 and paise("0.005") == 1

    con = db(":memory:")
    init(con)
    assert con.execute("SELECT COUNT(*) FROM items").fetchone()[0] > 100  # menu.csv loaded
    with con:
        save_item(con, {"iname": "Paneer Tikka", "icat": "Starters", "iprice": "249.50"})
        pid = con.execute("SELECT id FROM items WHERE name = 'Paneer Tikka'").fetchone()[0]
        oid = new_order(con, {"type": "dine_in", "table_no": 1})["id"]
        try:
            new_order(con, {"type": "dine_in", "table_no": 1})
            raise AssertionError("table double-booked")
        except sqlite3.IntegrityError:
            pass
        send_kot(con, {"order_id": oid, "lines": [{"id": pid, "qty": 2}], "note": "less spicy"})
        k = kitchen(con, {})[0]
        assert k["lines"] == [{"name": "Paneer Tikka", "qty": 2}] and k["note"] == "less spicy"
        b = bill_order(con, {"order_id": oid, "payment": "UPI", "discount_pct": 0})
        assert (b["bill_no"], b["subtotal"], b["cgst"], b["round_off"], b["total"]) == (1, 49900, 1248, 4, 52400)
        try:
            bill_order(con, {"order_id": oid, "payment": "UPI"})
            raise AssertionError("billed twice")
        except ValueError:
            pass
        new_order(con, {"type": "dine_in", "table_no": 1})  # table is free again after billing
        r = report(con, {"day": [b["day"]]})
        assert r["count"] == 1 and r["by_payment"] == {"UPI": 52400} and r["items"] == [["Paneer Tikka", 2, 49900]]
    snap = sqlite3.connect(":memory:")
    snap.deserialize(backup(con, {}))
    assert snap.execute("SELECT total FROM orders WHERE status = 'billed'").fetchone()[0] == 52400  # backup holds the bill
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        sys.exit(selftest())
    con = db()
    init(con)
    con.close()
    print(f"POS running.\n  This PC:  http://localhost:{PORT}\n  Tablets (same Wi-Fi) - try these:")
    for ip in lan_ips():
        print(f"            http://{ip}:{PORT}")
    print("  If a VPN is on, its address won't work from tablets - use the other one.\n"
          "  Default staff PIN is 1234 - change it in Settings.  Close this window to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
