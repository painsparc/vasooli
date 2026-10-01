"""simulator.py - SIMULATED world for the demo (clearly separate from the agent).
Channel = stand-in for WhatsApp/email inbox + bank-statement feed.  In production, swap this class for real
adapters (WhatsApp Business API / IMAP / Account Aggregator or bank CSV).  Buyers react to what the agent
actually sends, so the agent's choices change the outcome.  Fully deterministic (no randomness)."""
import csv, datetime as dt
from pathlib import Path

DATA = Path(__file__).parent / "data"
OUT = Path(__file__).parent / "output"
D = dt.date.fromisoformat
f = lambda x: x.strftime("%d-%b")


class Channel:
    def __init__(self):
        self.inv = {r["id"]: float(r["amount"]) for r in csv.DictReader(open(DATA / "invoices.csv", encoding="utf-8"))}
        self.scheduled, self.queue, self.seen, self.utr = set(), [], {}, 0

    # --- world events ---
    def _reply(self, day, buyer, text):
        self.queue.append(dict(t="reply", day=day, buyer=buyer, text=text, done=False))

    def _pay(self, day, buyer, ids=(), amount=None):
        ids = [i for i in ids if i not in self.scheduled]
        if amount is None:
            amount = sum(self.inv[i] for i in ids); self.scheduled.update(ids)
        if amount > 0:
            self.utr += 1
            self.queue.append(dict(t="bank", day=day, done=False, amount=amount,
                                   narration=f"NEFT/AXISN2610{self.utr:04d}/{buyer.upper()}"))

    # --- agent-facing tools ---
    def send(self, today, m):
        """Agent sends a message; simulated buyer decides how to react."""
        b, t, kind = m["buyer"], D(today), m["kind"]
        n = self.seen[b] = self.seen.get(b, 0) + 1
        ids = [i for i in m["invoices"] if i not in self.scheduled]
        if b == "Mehta Retail Pvt Ltd" and ids:            # prompt payer: forgot, pays next day
            self._reply(t + dt.timedelta(1), b, "Sorry sir, reminder miss ho gaya tha. Aaj hi NEFT kar rahe hain.")
            self._pay(t + dt.timedelta(1), b, ids)
        elif b == "Sharma Steel Traders" and ids:           # reliable, gives a date, keeps it
            p = t + dt.timedelta(3)
            self._reply(t + dt.timedelta(1), b, f"Namaste ji. Accounts ko bol diya hai, payment {f(p)} tak release ho jayega.")
            self._pay(p, b, ids)
        elif b == "Bharat Auto Components":                 # disputes one invoice for missing POD
            if kind == "doc":
                p = t + dt.timedelta(8)
                self._reply(t + dt.timedelta(1), b, f"POD mil gaya, thanks. INV-105 {f(p)} tak clear kar denge.")
                self._pay(p, b, ["INV-105"])
            elif n == 1:
                p = t + dt.timedelta(7)
                self._reply(t + dt.timedelta(1), b, "INV-105 ka delivery proof/POD hamare paas nahi hai, isliye hold par hai. "
                                                    f"INV-106 hum {f(p)} tak clear kar denge.")
                self._pay(p, b, ["INV-106"])
        elif b == "Kisan Agro Mart":                        # promises, breaks promise, then part-pays
            if n == 1:
                self._reply(t + dt.timedelta(1), b, f"Boss, Friday tak kar denge ({f(t + dt.timedelta(4))}). Thoda wait kijiye.")
            elif n == 2:
                self._reply(t + dt.timedelta(1), b, "Sorry, market mein paisa phasa hai. Abhi ₹30,000 bhej rahe hain, "
                                                    f"baaki {f(t + dt.timedelta(8))} tak.")
                self._pay(t + dt.timedelta(1), b, amount=30000)
        elif b == "Apex Infra Projects Ltd" and kind == "notice":   # silent until a formal notice arrives
            self._reply(t + dt.timedelta(1), b, "Notice received and forwarded to accounts. Principal payment will be "
                                                "released within 3 days; interest claim is under review.")
            self._pay(t + dt.timedelta(4), b, ids)

    def poll_inbox(self, today):
        out = [e for e in self.queue if e["t"] == "reply" and not e["done"] and e["day"] <= D(today)]
        for e in out: e["done"] = True
        return [dict(buyer=e["buyer"], text=e["text"], day=str(e["day"])) for e in out]

    def bank_feed(self, today):
        out = [e for e in self.queue if e["t"] == "bank" and not e["done"] and e["day"] <= D(today)]
        new = not (OUT / "bank_statement.csv").exists()
        with open(OUT / "bank_statement.csv", "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new: w.writerow(["date", "narration", "credit"])
            for e in out:
                e["done"] = True; w.writerow([e["day"], e["narration"], e["amount"]])
        return [dict(date=str(e["day"]), narration=e["narration"], amount=e["amount"]) for e in out]
