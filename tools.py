"""tools.py - Vasooli's toolbox.
SQLite state, MSMED-Act interest math, cash projection, PDF documents,
outbound delivery, and bank-statement reconciliation."""
import csv, itertools, json, os, re, smtplib, sqlite3, textwrap, datetime as dt
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
DATA, OUT = ROOT / "data", ROOT / "output"
CFG = json.load(open(DATA / "config.json", encoding="utf-8"))
db = None
PAID_OBL = set()


# ---------- helpers ----------
def d(x):
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(x)


def inr(x, sym="₹"):
    """Indian digit grouping: 1234567 -> ₹12,34,567"""
    x = int(round(x)); s = str(abs(x))
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        s = ",".join(parts + [tail])
    return f"{'-' if x < 0 else ''}{sym}{s}" if sym == "₹" else f"{'-' if x < 0 else ''}{sym} {s}"


def fmt(x):
    return d(x).strftime("%d-%b")


def q(sql, *a):
    return [dict(r) for r in db.execute(sql, a)]


def one(sql, *a):
    r = q(sql, *a)
    return r[0] if r else None


def run(sql, *a):
    db.execute(sql, a); db.commit()


def log(day, kind, buyer="", **detail):
    run("insert into events(day,kind,buyer,detail) values(?,?,?,?)", str(day), kind, buyer, json.dumps(detail, default=str))


# ---------- state ----------
def init():
    global db
    OUT.mkdir(exist_ok=True); (OUT / "docs").mkdir(exist_ok=True)
    for f in list(OUT.glob("*.jsonl")) + list(OUT.glob("*.csv")) + list((OUT / "docs").glob("*")):
        f.unlink()
    PAID_OBL.clear()
    db = sqlite3.connect(":memory:", check_same_thread=False); db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE invoices(id TEXT PRIMARY KEY, buyer TEXT, amount REAL, paid REAL DEFAULT 0, inv_date TEXT,
        credit_days INT, stat_due TEXT, pod_ref TEXT, dispute TEXT DEFAULT '', doc_sent INT DEFAULT 0);
    CREATE TABLE buyers(name TEXT PRIMARY KEY, email TEXT, importance INT, lang TEXT, unanswered INT DEFAULT 0, broken INT DEFAULT 0,
        last_contact TEXT, notice_day TEXT, hold_until TEXT, packet INT DEFAULT 0);
    CREATE TABLE promises(id INTEGER PRIMARY KEY AUTOINCREMENT, invoice TEXT, due TEXT, status TEXT DEFAULT 'active');
    CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, kind TEXT, buyer TEXT, detail TEXT);
    CREATE TABLE kv(k TEXT PRIMARY KEY, v REAL);""")
    for b in csv.DictReader(open(DATA / "buyers.csv", encoding="utf-8")):
        run("insert into buyers(name,email,importance,lang) values(?,?,?,?)", b["name"], b["email"], int(b["importance"]), b["language"])
    for i in csv.DictReader(open(DATA / "invoices.csv", encoding="utf-8")):
        cd = int(i["credit_days"] or 15)
        stat = d(i["inv_date"]) + dt.timedelta(min(cd, 45))  # MSMED Act s.15: agreed credit can't exceed 45 days
        run("insert into invoices(id,buyer,amount,inv_date,credit_days,stat_due,pod_ref) values(?,?,?,?,?,?,?)",
            i["id"], i["buyer"], float(i["amount"]), i["inv_date"], cd, stat.isoformat(), i["pod_ref"])
    run("insert into kv values('bal',?)", CFG["bank_balance"])


def balance():
    return one("select v from kv where k='bal'")["v"]


def add_balance(x):
    run("update kv set v=v+? where k='bal'", x)


# ---------- statutory maths ----------
def interest(principal, days_overdue):
    """MSMED Act s.16: compound interest, monthly rests, at 3x RBI Bank Rate. (Rate is configurable - verify.)"""
    if days_overdue <= 0: return 0.0
    r = 3 * CFG["bank_rate_pct"] / 100
    return principal * ((1 + r / 12) ** (days_overdue / 30) - 1)


def overdue(today):
    """All invoices with outstanding balance, enriched with days past statutory due date and interest."""
    out = []
    for i in q("select * from invoices where amount-paid>0.5 order by stat_due"):
        i["out"] = i["amount"] - i["paid"]
        i["days"] = (d(today) - d(i["stat_due"])).days
        i["interest"] = interest(i["out"], i["days"])
        out.append(i)
    return out


# ---------- cash planning ----------
def projection(today):
    """For each upcoming obligation: cumulative need vs. balance + promised inflows -> shortfall."""
    bal, rows, cum = balance(), [], 0
    for o in sorted(CFG["obligations"], key=lambda o: o["due"]):
        if o["label"] in PAID_OBL: continue
        cum += o["amount"]
        prom = one("""select coalesce(sum(i.amount-i.paid),0) s from promises p join invoices i on i.id=p.invoice
                      where p.status='active' and p.due<=? and i.amount-i.paid>0.5""", o["due"])["s"]
        rows.append(dict(o, need=cum, balance=bal, promised=prom, shortfall=max(0, cum - bal - prom)))
    return rows


def pay_obligations(today):
    """Owner's outgoing payments falling due today. Returns [(label, amount, paid_in_full)]."""
    res = []
    for o in CFG["obligations"]:
        if o["label"] not in PAID_OBL and o["due"] <= str(today):
            PAID_OBL.add(o["label"]); ok = balance() >= o["amount"]
            add_balance(-o["amount"]); res.append((o["label"], o["amount"], ok))
    return res


def lapse_promises(today):
    """Promises whose date has passed with the invoice still unpaid -> broken. Returns {buyer: [invoice ids]}."""
    broken = {}
    for p in q("""select p.id, p.invoice, i.buyer from promises p join invoices i on i.id=p.invoice
                  where p.status='active' and p.due<? and i.amount-i.paid>0.5""", str(today)):
        run("update promises set status='broken' where id=?", p["id"])
        broken.setdefault(p["buyer"], []).append(p["invoice"])
    for b in broken:
        run("update buyers set broken=broken+1 where name=?", b)
    return broken


# ---------- documents ----------
def write_doc(name, title, lines):
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        p = OUT / "docs" / f"{name}.pdf"
        c = canvas.Canvas(str(p), pagesize=A4); y = 800
        c.setFont("Helvetica-Bold", 13); c.drawString(40, y, title); y -= 28; c.setFont("Helvetica", 9.5)
        for ln in lines:
            for w in (textwrap.wrap(ln, 105) or [""]):
                if y < 50: c.showPage(); c.setFont("Helvetica", 9.5); y = 800
                c.drawString(40, y, w); y -= 14
        c.save()
    except ImportError:  # graceful fallback: plain text
        p = OUT / "docs" / f"{name}.txt"
        p.write_text(title + "\n\n" + "\n".join(lines), encoding="utf-8")
    return str(p.relative_to(ROOT))


def make_pod(inv):
    s = CFG["supplier"]
    return write_doc(f"POD_{inv['id']}", "DELIVERY CHALLAN / PROOF OF DELIVERY (copy)", [
        f"Supplier : {s['name']}, {s['city']}   |   {s['udyam']}",
        f"Challan  : {inv['pod_ref']}   |   Invoice: {inv['id']}   |   Invoice date: {inv['inv_date']}",
        f"Buyer    : {inv['buyer']}", f"Value    : {inr(inv['amount'], 'Rs.')}", "",
        "Goods received in good condition. Receiver signature & stamp: [signed copy on file]",
        "(Synthetic demo document generated by Vasooli.)"])


def make_notice(buyer, invs, today):
    s, rows, tot, tint = CFG["supplier"], [], 0, 0
    for i in invs:
        rows.append(f"{i['id']} | dated {i['inv_date']} | statutory due {i['stat_due']} | {i['days']} days late | "
                    f"principal {inr(i['out'], 'Rs.')} | interest to date {inr(i['interest'], 'Rs.')}")
        tot += i["out"]; tint += i["interest"]
    return write_doc(f"NOTICE_{buyer.replace(' ', '_')}_{today}", "NOTICE FOR DELAYED PAYMENT - MSMED ACT, 2006", [
        "[DRAFT - generated by Vasooli; review with your CA / advocate before dispatch]", "",
        f"Date: {today}", f"From: {s['name']} ({s['category']}), {s['city']} - {s['udyam']}", f"To:   {buyer}", "",
        "Subject: Demand for payment of overdue invoices along with statutory interest", "", *rows, "",
        f"TOTAL principal: {inr(tot, 'Rs.')}   |   interest accrued to date: {inr(tint, 'Rs.')}   |   "
        f"claim: {inr(tot + tint, 'Rs.')}", "",
        "Under Sections 15 and 16 of the MSMED Act, 2006, payment to a micro/small enterprise is due within the agreed "
        "period (not exceeding 45 days), and delayed amounts carry compound interest with monthly rests at three times "
        "the Bank Rate notified by the RBI.", "",
        "You are requested to clear the principal together with interest within 7 days of this notice. Failing this, "
        "we will refer the matter to the MSE Facilitation Council through the Samadhaan portal (Section 18).", "",
        "Please also note Section 43B(h) of the Income-tax Act, under which amounts owed to micro/small enterprises "
        "beyond the statutory period are deductible for the buyer only on actual payment.", "",
        f"Authorised signatory: {s['owner']}"]), tot, tint


def make_packet(buyer, invs, today):
    s = CFG["supplier"]
    lines = [f"Complainant: {s['name']} - {s['udyam']}", f"Respondent : {buyer}", f"Filed on   : {today}", "",
             "Outstanding invoices:"] + [f" - {i['id']} ({i['inv_date']}): {inr(i['out'], 'Rs.')}, {i['days']} days late, "
                                          f"interest {inr(i['interest'], 'Rs.')}, challan {i['pod_ref']}" for i in invs] + [
             "", "Enclosures: invoices, challans, notice copy, reminder trail (see events log)."]
    return write_doc(f"SAMADHAAN_PACKET_{buyer.replace(' ', '_')}", "SAMADHAAN COMPLAINT PACKET (prepared for owner filing)", lines)


# ---------- outbound ----------
def deliver(msg):
    """Log every outbound message; send real email only if SMTP_HOST is configured."""
    with open(OUT / "outbox.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    if os.getenv("SMTP_HOST") and msg.get("email"):
        try:
            m = EmailMessage(); m["From"] = os.getenv("SMTP_FROM", "vasooli@demo.local"); m["To"] = msg["email"]
            m["Subject"] = msg["subject"]; m.set_content(msg["text"])
            for a in msg.get("attachments", []):
                m.add_attachment((ROOT / a).read_bytes(), maintype="application", subtype="octet-stream", filename=Path(a).name)
            with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", 587))) as s:
                s.starttls(); s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"]); s.send_message(m)
        except Exception as e:
            print("   (SMTP failed, message kept in outbox.jsonl):", e)


# ---------- reconciliation ----------
STOP = {"ltd", "pvt", "private", "limited", "and", "co"}


def match_buyer(narration):
    words = re.sub(r"[^a-z ]", " ", narration.lower()).split(); best = None
    for b in q("select name from buyers"):
        toks = [t for t in re.sub(r"[^a-z ]", " ", b["name"].lower()).split() if t not in STOP]
        score = sum(t in words for t in toks) / len(toks)
        if score >= 0.5 and (not best or score > best[0]): best = (score, b["name"])
    return best[1] if best else None


def reconcile(credit):
    """Match a bank credit to a buyer, then to the invoice combination it settles (exact subset match first,
    otherwise FIFO partial). Updates the ledger and cash balance."""
    buyer = match_buyer(credit["narration"]); amt = credit["amount"]
    add_balance(amt)
    if not buyer: return dict(buyer=None, mode="UNMATCHED", applied=[])
    opn = q("select * from invoices where buyer=? and amount-paid>0.5 order by stat_due", buyer)
    applied, mode = [], "exact"
    for n in range(1, min(len(opn), 4) + 1):
        for combo in itertools.combinations(opn, n):
            if abs(sum(i["amount"] - i["paid"] for i in combo) - amt) < 1:
                applied = [(i["id"], i["amount"] - i["paid"]) for i in combo]; break
        if applied: break
    if not applied:
        mode, left = "partial (FIFO)", amt
        for i in opn:
            if left <= 0.5: break
            x = min(left, i["amount"] - i["paid"]); applied.append((i["id"], x)); left -= x
    for iid, x in applied:
        run("update invoices set paid=paid+? where id=?", x, iid)
        if one("select amount-paid r from invoices where id=?", iid)["r"] < 0.5:
            run("update promises set status='kept' where invoice=? and status='active'", iid)
    return dict(buyer=buyer, mode=mode, applied=applied)
