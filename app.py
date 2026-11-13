"""Restaurant POS server (dine-in / takeaway / delivery, KOT, GST billing, daily reports).
Run: python app.py      Self-check: python app.py test      Windows control window: python gui.py
Only needs Python 3 - no packages. All data lives in pos.db: back it up."""
import csv, hmac, json, logging, os, socket, sqlite3, sys, time, webbrowser
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).parent
FROZEN = getattr(sys, "frozen", False)           # running inside CafePOS.exe
RES = Path(getattr(sys, "_MEIPASS", HERE))       # index.html + menu.csv (unpacked from the exe)
# exe keeps data in the user's profile so moving/updating the exe never loses bills
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or HERE) / "CafePOS" if FROZEN else HERE
# Cloud hosting sets these: POS_DB must point at a persistent disk, or data is lost on every redeploy.
DB = Path(os.environ.get("POS_DB") or DATA_DIR / "pos.db")
PORT = int(os.environ.get("PORT") or 8000)
log = logging.getLogger("pos")
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
    bill_no INTEGER UNIQUE, day_no INTEGER, billed TEXT, day TEXT, payment TEXT, subtotal INTEGER, discount_pct TEXT,
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
        if "day_no" not in {r[1] for r in con.execute("PRAGMA table_info(orders)")}:
            con.execute("ALTER TABLE orders ADD COLUMN day_no INTEGER")  # databases from before daily bill numbers
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS bill_no_per_day ON orders(day, day_no)")
        con.executemany("INSERT OR IGNORE INTO settings VALUES(?, ?)", DEFAULT_SETTINGS.items())
        # Business day: bills go to this date until staff press "Close day", so reopening the app or
        # billing after midnight never splits a day. Existing databases continue their latest billed day.
        con.execute("INSERT OR IGNORE INTO settings VALUES('business_day', "
                    "COALESCE((SELECT MAX(day) FROM orders WHERE status = 'billed'), ?))", (date.today().isoformat(),))
        if not con.execute("SELECT 1 FROM items").fetchone():  # first run: load the starter menu
            with open(RES / "menu.csv", encoding="utf-8") as f:
                con.executemany("INSERT INTO items(category, name, price) VALUES(?, ?, ?)",
                                [(r["category"], r["name"], paise(r["price"])) for r in csv.DictReader(f)])


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---- money: integer paise everywhere, half-up rounding ----
def paise(x):
    return int((Decimal(str(x)) * 100).quantize(Decimal(1), ROUND_HALF_UP))


def rs(p):
    return f"₹{p / 100:,.2f}"


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


def business_day(con):
    return con.execute("SELECT value FROM settings WHERE key = 'business_day'").fetchone()[0]


def bill_label(day, n):
    """Printed invoice number: restarts at 1 each business day, prefixed DDMMYY so it stays unique for the
    financial year as GST requires (e.g. 131126-1)."""
    return f"{day[8:10]}{day[5:7]}{day[2:4]}-{n}"


def order_out(con, r):
    o = dict(r)
    if o.get("bill_no"):
        o["bill_label"] = bill_label(o["day"], o["day_no"]) if o.get("day_no") else str(o["bill_no"])  # old bills
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
        "SELECT * FROM orders WHERE status = 'billed' AND day = ? ORDER BY COALESCE(day_no, bill_no) DESC", (day,))]
    pay, typ, items = {}, {}, {}
    for b in bills:
        pay[b["payment"]] = pay.get(b["payment"], 0) + b["total"]
        typ[b["type"]] = typ.get(b["type"], 0) + b["total"]
        for l in b["lines"]:
            qty, amt = items.get(l["name"], (0, 0))
            items[l["name"]] = (qty + l["qty"], amt + l["qty"] * l["price"])
    return {"day": day, "open": day == business_day(con), "count": len(bills),
            "sums": {k: sum(b[k] for b in bills) for k in ("subtotal", "discount", "cgst", "sgst", "round_off", "total")},
            "by_payment": pay, "by_type": typ,
            "items": sorted(([n, q, a] for n, (q, a) in items.items()), key=lambda x: -x[2]),
            "cancelled": con.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled' "
                                     "AND COALESCE(day, substr(created, 1, 10)) = ?", (day,)).fetchone()[0],
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
    log.info("Menu: saved %s (%s) at %s", name, cat, rs(price))
    return {"ok": True}


def delete_item(con, d):
    # ponytail: soft delete; past bills keep their own name/price snapshot anyway
    con.execute("UPDATE items SET active = 0 WHERE id = ?", (int(d["id"]),))
    log.info("Menu: deleted item #%s", d["id"])
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
    log.info("Settings saved%s", " (PIN changed)" if d.get("pin") else "")
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
    log.info("Order #%s opened: %s", cur.lastrowid, f"Table {table}" if table else typ.title())
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
    log.info("KOT %s -> kitchen for order #%s: %s", kid, oid, ", ".join(f"{q}x {n}" for _, n, _, q in lines))
    return {"id": kid}


def remove_line(con, d):
    # ponytail: no audit trail for removed items; add a voids table if staff misuse becomes a worry
    if not con.execute("DELETE FROM lines WHERE id = ? AND order_id IN (SELECT id FROM orders WHERE status = 'open')",
                       (int(d["line_id"]),)).rowcount:
        raise ValueError("item not found or order already billed")
    log.info("Item removed from an open order (line #%s)", d["line_id"])
    return {"ok": True}


def cancel_order(con, d):
    if not con.execute("UPDATE orders SET status = 'cancelled', day = ? WHERE id = ? AND status = 'open'",
                       (business_day(con), int(d["order_id"]))).rowcount:
        raise ValueError("order is not open")
    log.warning("Order #%s CANCELLED", d["order_id"])
    return {"ok": True}


def kot_ready(con, d):
    con.execute("UPDATE kots SET status = 'ready' WHERE id = ?", (int(d["id"]),))
    log.info("KOT %s ready", d["id"])
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
    t, day = now(), business_day(con)
    # numbers assigned inside the UPDATE so two tablets billing at once can't get the same one;
    # bill_no = lifetime sequence (internal), day_no = 1, 2, 3... restarting every business day
    if not con.execute(
            "UPDATE orders SET status = 'billed', bill_no = (SELECT COALESCE(MAX(bill_no), 0) + 1 FROM orders), "
            "day_no = (SELECT COALESCE(MAX(day_no), 0) + 1 FROM orders WHERE day = ?), "
            "billed = ?, day = ?, payment = ?, subtotal = ?, discount_pct = ?, discount = ?, tax_pct = ?, "
            "cgst = ?, sgst = ?, round_off = ?, total = ? WHERE id = ? AND status = 'open'",
            (day, t, day, payment, sub, disc, dis, tax_pct, cgst, sgst, ro, total, oid)).rowcount:
        raise ValueError("order is not open")
    b = get_order(con, oid)
    log.info("BILL %s: %s paid by %s (order #%s)", b["bill_label"], rs(total), payment, oid)
    return b


def close_day(con, d):
    """End-of-day: lock in the daily report and move billing to the next business day."""
    day = business_day(con)
    if d.get("day") != day:
        raise ValueError("this day is already closed - refresh the page")
    still_open = con.execute("SELECT COUNT(*) FROM orders WHERE status = 'open'").fetchone()[0]
    if still_open:
        raise ValueError(f"{still_open} order(s) still open - bill or cancel them before closing the day")
    nxt = max(date.fromisoformat(day) + timedelta(days=1), date.today()).isoformat()  # forgot to close? skip to today
    # conditional UPDATE: if two tablets press Close at once, only one wins
    if not con.execute("UPDATE settings SET value = ? WHERE key = 'business_day' AND value = ?", (nxt, day)).rowcount:
        raise ValueError("this day is already closed - refresh the page")
    r = report(con, {"day": [day]})
    log.info("DAY CLOSED %s: %d bills, net %s. Next business day: %s", day, r["count"], rs(r["sums"]["total"]), nxt)
    return {"report": r, "next": nxt}


def backup(con, q):
    """Consistent snapshot of the whole database, safe to take while the café is billing."""
    snap = sqlite3.connect(":memory:")
    con.backup(snap)
    data = snap.serialize()
    snap.close()
    log.info("Backup downloaded (%d KB)", len(data) // 1024)
    return data


GET = {"/api/state": state, "/api/kitchen": kitchen, "/api/report": report, "/api/backup": backup}
POST = {"/api/items": save_item, "/api/items/delete": delete_item, "/api/settings": save_settings,
        "/api/orders/new": new_order, "/api/orders/kot": send_kot, "/api/orders/remove_line": remove_line,
        "/api/orders/cancel": cancel_order, "/api/orders/bill": bill_order, "/api/kots/ready": kot_ready,
        "/api/day/close": close_day}


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
            return self.send((RES / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
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
                if self.headers.get("X-Pin"):  # a blank PIN is just a tablet's first visit
                    # behind Tailscale Funnel every visitor is 127.0.0.1; the proxy passes the real one along
                    log.warning("Wrong PIN from %s", self.headers.get("X-Forwarded-For") or self.client_address[0])
                if LOCK["fails"] >= 5:  # 5 wrong in a row -> everyone locked out for 60 s
                    LOCK.update(fails=0, until=time.time() + 60)
                    log.warning("5 wrong PINs in a row - POS locked for 60 seconds")
                time.sleep(1)
                return self.send({"error": "Wrong PIN"}, 401)
            LOCK["fails"] = 0
            with con:  # one transaction per request, rolled back on any error
                out = route(con, arg)
            self.send(out, ctype="application/octet-stream" if isinstance(out, bytes) else "application/json")
        except sqlite3.IntegrityError:
            self.send({"error": "That table already has an open order. Refresh and try again."}, 409)
        except (ValueError, KeyError, TypeError, InvalidOperation) as e:
            log.warning("Rejected %s: %s", self.path, e)
            self.send({"error": f"Invalid input: {e}"}, 400)
        except Exception:  # never let one bad request kill the server silently (exe has no console)
            log.exception("Error handling %s", self.path)
            self.send({"error": "Server error - see the POS log window."}, 500)
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
        assert b["day_no"] == 1 and b["bill_label"] == bill_label(b["day"], 1)
        try:
            bill_order(con, {"order_id": oid, "payment": "UPI"})
            raise AssertionError("billed twice")
        except ValueError:
            pass
        assert b["day"] == business_day(con) == date.today().isoformat()
        o2 = new_order(con, {"type": "dine_in", "table_no": 1})["id"]  # table is free again after billing
        r = report(con, {"day": [b["day"]]})
        assert r["count"] == 1 and r["by_payment"] == {"UPI": 52400} and r["items"] == [["Paneer Tikka", 2, 49900]]
        assert r["open"]
        try:
            close_day(con, {"day": b["day"]})
            raise AssertionError("closed a day with an open order")
        except ValueError:
            pass
        send_kot(con, {"order_id": o2, "lines": [{"id": pid, "qty": 1}]})
        assert bill_order(con, {"order_id": o2, "payment": "Cash"})["day_no"] == 2  # same day -> 2
        o2 = new_order(con, {"type": "takeaway"})["id"]
        cancel_order(con, {"order_id": o2})
        closed = close_day(con, {"day": b["day"]})
        assert closed["report"]["count"] == 2 and closed["report"]["cancelled"] == 1
        assert business_day(con) == closed["next"] == (date.today() + timedelta(days=1)).isoformat()
        try:
            close_day(con, {"day": b["day"]})  # second tablet pressing Close on the same day
            raise AssertionError("closed the same day twice")
        except ValueError:
            pass
        assert not report(con, {"day": [b["day"]]})["open"]
        o3 = new_order(con, {"type": "takeaway"})["id"]  # next business day: numbering starts again at 1
        send_kot(con, {"order_id": o3, "lines": [{"id": pid, "qty": 1}]})
        b3 = bill_order(con, {"order_id": o3, "payment": "Cash"})
        assert (b3["day_no"], b3["bill_no"]) == (1, 3) and b3["bill_label"] != b["bill_label"]
    snap = sqlite3.connect(":memory:")
    snap.deserialize(backup(con, {}))
    assert snap.execute("SELECT total FROM orders WHERE status = 'billed'").fetchone()[0] == 52400  # backup holds the bill
    print("ok")


def make_server(port=PORT):
    """Open (or create) the database and return a ready, not-yet-serving HTTP server."""
    if FROZEN:
        DB.parent.mkdir(parents=True, exist_ok=True)
    if not DB.parent.is_dir():  # e.g. POS_DB=/data/pos.db but no volume mounted at /data
        raise SystemExit(f"Data folder {DB.parent} does not exist. On Railway: add a Volume with mount path {DB.parent}.")
    con = db()
    init(con)
    con.close()
    ThreadingHTTPServer.allow_reuse_address = False  # on Windows, True lets a 2nd copy silently share the port
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


if __name__ == "__main__":
    if sys.argv[1:] == ["test"]:
        sys.exit(selftest())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    server = make_server()
    print(f"Data file: {DB}", flush=True)
    print(f"POS running.\n  This PC:  http://localhost:{PORT}\n  Tablets (same Wi-Fi) - try these:")
    for ip in lan_ips():
        print(f"            http://{ip}:{PORT}")
    print("  If a VPN is on, its address won't work from tablets - use the other one.\n"
          "  Default staff PIN is 1234 - change it in Settings.  Close this window to stop.")
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://localhost:{PORT}")
    server.serve_forever()
