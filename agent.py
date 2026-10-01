"""agent.py - Vasooli's brain.
Daily loop:  OBSERVE (inbox + bank) -> UNDERSTAND (classify replies) -> VERIFY (reconcile credits, lapse promises)
             -> REASON (cash gap, priorities) -> PLAN (per-buyer ladder) -> ACT (tools, owner approval gate) -> repeat.
LLM (Anthropic API or local Ollama) is used for reply understanding + message drafting, behind guardrails.
With no LLM configured, deterministic rules/templates take over so the demo never breaks."""
import json, os, re, datetime as dt
import requests
import tools as T
from simulator import Channel

LOG, LLM_STATE = [], {"fails": 0, "used": 0, "name": "none"}
MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def say(tag, msg=""):
    line = f"  [{tag:<10}] {msg}"; LOG.append(line); print(line)


# ======================= LLM layer (optional, guard-railed) =======================
def llm(system, prompt, max_tokens=500):
    if LLM_STATE["fails"] >= 2: return None            # circuit-breaker: stop wasting demo time
    try:
        if os.getenv("ANTHROPIC_API_KEY"):
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=25,
                              headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                              json={"model": os.getenv("VASOOLI_MODEL", "claude-sonnet-5-5"), "max_tokens": max_tokens,
                                    "system": system, "messages": [{"role": "user", "content": prompt}]})
            r.raise_for_status(); LLM_STATE["used"] += 1
            return r.json()["content"][0]["text"].strip()
        if os.getenv("OLLAMA_MODEL"):
            r = requests.post(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/generate", timeout=90,
                              json={"model": os.environ["OLLAMA_MODEL"], "system": system, "prompt": prompt, "stream": False})
            r.raise_for_status(); LLM_STATE["used"] += 1
            return r.json()["response"].strip()
    except Exception:
        LLM_STATE["fails"] += 1
    return None


def llm_name():
    return ("Anthropic " + os.getenv("VASOOLI_MODEL", "claude-sonnet-5-5")) if os.getenv("ANTHROPIC_API_KEY") else \
           ("Ollama " + os.environ["OLLAMA_MODEL"]) if os.getenv("OLLAMA_MODEL") else "none (rule-based fallback)"


# ======================= UNDERSTAND =======================
INTENTS = {"PROMISE", "DISPUTE", "PAYMENT_CLAIM", "STALL", "OTHER"}


def rule_classify(text, day):
    res = []
    for s in re.split(r"(?<=[.!?])\s+|;\s*", text):
        low = s.lower().strip()
        if not low: continue
        inv = (re.findall(r"INV-\d+", s) or [None])[0]
        date = None
        if m := re.search(r"(\d{1,2})[- ]?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", low):
            date = dt.date(T.d(day).year, MONTHS[m.group(2)], int(m.group(1)))
        if m := re.search(r"within (\d+)", low): date = T.d(day) + dt.timedelta(int(m.group(1)))
        amt = re.search(r"₹\s?([\d,]+)", s); amount = float(amt.group(1).replace(",", "")) if amt else None
        dispute = any(k in low for k in ["nahi hai", "nahi mila", "hold par", "dispute", "wrong", "mismatch", "not received"])
        claim = re.search(r"\b(abhi|aaj|today|now|bhej rahe|kar rahe|paid|sent)\b", low)
        if dispute and not date:
            reason = "missing_pod" if ("pod" in low or "proof" in low) else "amount" if ("wrong" in low or "mismatch" in low) else "other"
            res.append(dict(intent="DISPUTE", invoice=inv, reason=reason))
        else:
            if claim: res.append(dict(intent="PAYMENT_CLAIM", invoice=inv, amount=amount))
            if date: res.append(dict(intent="PROMISE", invoice=inv, date=str(date)))
    return res or [dict(intent="OTHER")]


def understand(buyer, text, day, open_ids):
    raw = llm("You classify replies from a business buyer (English/Hinglish) to a supplier's payment reminder. "
              "Return ONLY a JSON list, no prose.",
              f"Today: {day}. Buyer: {buyer}. Open invoices: {open_ids}.\nReply: \"{text}\"\n"
              'JSON list of objects: {"intent": PROMISE|DISPUTE|PAYMENT_CLAIM|STALL|OTHER, "invoice": id or null (=all), '
              '"date": YYYY-MM-DD or null, "amount": number or null, "reason": missing_pod|amount|quality|other|null}. '
              "Resolve relative dates (e.g. 'Friday', 'within 3 days') using today's date.")
    if raw:
        try:
            items = json.loads(raw[raw.index("["): raw.rindex("]") + 1])
            ok = all(isinstance(i, dict) and i.get("intent") in INTENTS and (not i.get("date") or T.d(i["date"])) for i in items)
            if items and ok: return items, "LLM"
        except Exception:
            pass
    return rule_classify(text, day), "rules"


def apply_understanding(buyer, items, day):
    mine = [i for i in T.overdue(day) if i["buyer"] == buyer and i["days"] > 0]
    T.run("update buyers set unanswered=0 where name=?", buyer)
    for it in items:
        tgt = [it["invoice"]] if it.get("invoice") else [i["id"] for i in mine if not i["dispute"]]
        tgt = [x for x in tgt if x in {i["id"] for i in mine}]
        if it["intent"] == "DISPUTE":
            for x in tgt or [i["id"] for i in mine][:1]: T.run("update invoices set dispute=? where id=?", it.get("reason") or "other", x)
        elif it["intent"] in ("PROMISE", "PAYMENT_CLAIM"):
            due = it.get("date") if it["intent"] == "PROMISE" else (str(T.d(day) + dt.timedelta(1)) if not it.get("amount") else None)
            for x in tgt if due else []:
                T.run("update promises set status='superseded' where invoice=? and status='active'", x)
                T.run("insert into promises(invoice,due) values(?,?)", x, due)
                T.run("update invoices set dispute='' where id=?", x)


# ======================= ACT: drafting with guardrails =======================
def lines(invs):
    return "; ".join(f"{i['id']} ({T.inr(i['out'])}, {i['days']}d overdue)" for i in invs)


def template(kind, b, invs, ctx):
    s, tot, own = T.CFG["supplier"], sum(i["out"] for i in invs), T.CFG["supplier"]["owner"]
    if kind == "nudge" and b["lang"] == "english":
        return (f"Dear Sir/Madam, this is {own} from {s['name']}. A gentle reminder that {lines(invs)} remain outstanding "
                f"(total {T.inr(tot)}). Could you please share the expected payment date? Thank you.")
    if kind == "nudge":
        return (f"Namaste ji, {own} here from {s['name']}. Friendly reminder: {lines(invs)} abhi pending hai "
                f"(total {T.inr(tot)}). Kripya payment ki expected date bata dein. Dhanyavaad!")
    if kind == "followup":
        extra = f" Your promised payment date has passed without receipt." if ctx.get("broken") else " We have not heard back on our earlier reminder."
        return (f"Dear Sir/Madam,{extra} Outstanding: {lines(invs)}; total {T.inr(tot)}. As a registered small enterprise, "
                f"delayed payments attract interest under Section 16 of the MSMED Act (about {T.inr(sum(i['interest'] for i in invs))} "
                f"accrued so far). Please confirm a payment date within 2 days. Regards, {own}, {s['name']}")
    if kind == "doc":
        i = invs[0]
        return (f"Dear team, regarding {i['id']} ({T.inr(i['out'])}): attaching the signed delivery challan {i['pod_ref']} "
                f"as proof of delivery. Please process payment and confirm the date. Regards, {own}, {s['name']}")
    return (f"Dear Sir/Madam, please find attached a formal notice for delayed payment under the MSMED Act covering "
            f"{lines(invs)}, total principal {T.inr(tot)}. Kindly arrange payment within 7 days. Regards, {own}, {s['name']}")


def draft(kind, b, invs, ctx):
    fallback = template(kind, b, invs, ctx)
    tone = {"nudge": f"warm, short, no pressure, in {b['lang']}", "followup": "firm but polite English, cite MSMED Act s.16 interest",
            "doc": "helpful, brief, English", "notice": "formal cover note, English"}[kind]
    raw = llm("You write payment messages for an Indian small-business owner. Be concise (max 90 words). "
              "Never reveal the owner's cash position. Never invent facts, amounts or dates. Output only the message.",
              f"Tone: {tone}. Relationship importance 1-3: {b['importance']}. Sender: {T.CFG['supplier']['owner']}, "
              f"{T.CFG['supplier']['name']}. Facts: {lines(invs)}. Interest accrued: "
              f"{T.inr(sum(i['interest'] for i in invs))}. Context: {json.dumps(ctx)}")
    must = [i["id"] for i in invs] + [T.inr(i["out"]) for i in invs]
    if raw and all(m in raw for m in must): return raw, "LLM"      # guardrail: amounts/IDs must be intact
    return fallback, "template"


def approve(mode, what):
    if mode == "ask":
        return input(f"   >> OWNER APPROVAL needed: {what}  Approve? [y/N] ").strip().lower().startswith("y")
    ok = mode != "no"
    say("APPROVAL", f"{'auto-approved' if ok else 'auto-REJECTED'} for demo: {what}")
    return ok


# ======================= the daily loop =======================
def tick(day, ch, mode, S):
    ds = day.isoformat()
    print(f"\n━━━ DAY {S['n']}  {day.strftime('%a %d-%b-%Y')}  |  cash in bank {T.inr(T.balance())} ━━━"); LOG.append(f"\n--- {ds} ---")
    # 1. OBSERVE
    credits, replies = ch.bank_feed(ds), ch.poll_inbox(ds)
    say("OBSERVE", f"{len(credits)} bank credit(s), {len(replies)} buyer reply(ies)")
    # 2. VERIFY: reconcile credits against the ledger
    for c in credits:
        r = T.reconcile(c); S["recovered"] += c["amount"]
        what = ", ".join(f"{i} {T.inr(x)}" for i, x in r["applied"]) or "nothing"
        say("VERIFY", f"credit {T.inr(c['amount'])} '{c['narration'][-24:]}' -> {r['buyer']} | {r['mode']} | {what}")
        T.log(ds, "reconciled", r["buyer"], amount=c["amount"], applied=r["applied"], mode=r["mode"])
        S["closed"] = [i["id"] for i in T.q("select id from invoices where amount-paid<0.5")]
    # 3. UNDERSTAND replies
    for r in replies:
        open_ids = [i["id"] for i in T.overdue(ds) if i["buyer"] == r["buyer"]]
        items, src = understand(r["buyer"], r["text"], ds, open_ids); S["replies"] += 1
        say("UNDERSTAND", f"{r['buyer']}: \"{r['text']}\"")
        say("", f"   -> [{src}] " + "; ".join(f"{i['intent']}" + (f" {i.get('invoice') or 'all'}" if i['intent'] != 'OTHER' else "")
                                              + (f" by {T.fmt(i['date'])}" if i.get("date") else "")
                                              + (f" {T.inr(i['amount'])}" if i.get("amount") else "")
                                              + (f" ({i['reason']})" if i.get("reason") else "") for i in items))
        apply_understanding(r["buyer"], items, ds); T.log(ds, "reply", r["buyer"], items=items)
    for lab, amt, ok in T.pay_obligations(ds):
        say("OWNER", f"{lab} {T.inr(amt)} paid {'on time' if ok else 'WITH SHORTFALL (cash short)'}"); S["obl"].append((lab, ok))
    lapsed = T.lapse_promises(ds)
    for b, ids in lapsed.items():
        say("VERIFY", f"BROKEN PROMISE by {b}: {', '.join(ids)} still unpaid after promised date -> strike recorded")
    # 4. REASON: cash gap
    proj = T.projection(ds); gap = next((p for p in proj if p["shortfall"] > 0), None)
    if proj:
        p = proj[0]
        say("REASON", f"next cash need: {p['label']} {T.inr(p['amount'])} on {T.fmt(p['due'])}; bank {T.inr(p['balance'])}, "
                      f"promised inflows {T.inr(p['promised'])}" + (f" -> SHORTFALL {T.inr(p['shortfall'])}" if p["shortfall"] else " -> covered"))
    if S["n"] == 1: S["gap0"] = gap["shortfall"] if gap else 0
    # 5. PLAN: per-buyer decision ladder
    od = [i for i in T.overdue(ds) if i["days"] >= T.CFG["grace_days"] or i["dispute"]]
    actions = []
    for b in T.q("select * from buyers"):
        mine = [i for i in od if i["buyer"] == b["name"]]
        if not mine: continue
        pend = {p["invoice"] for p in T.q("select invoice from promises where status='active'")}
        score = sum(i["out"] for i in mine) / 1e5 * (1 + min(max(i["days"] for i in mine) / 30, 3)) * (1.5 if gap else 1)
        for i in mine:                                             # (a) disputes we can fix with a document
            if i["dispute"] == "missing_pod" and not i["doc_sent"]:
                actions.append(dict(type="DOC", b=b, invs=[i], score=score + 5, why=f"{i['id']} disputed: missing POD -> send challan {i['pod_ref']}"))
        act = [i for i in mine if not i["dispute"] and i["id"] not in pend]
        if not act: continue
        strikes, thr = b["unanswered"] + b["broken"], 2 + (b["importance"] >= 3)
        since = (day - T.d(b["last_contact"])).days if b["last_contact"] else 99
        if b["last_contact"] and since < (1 if gap else 2) and b["name"] not in lapsed: continue   # give them time to reply
        hold = b["hold_until"] and b["hold_until"] > ds
        if b["notice_day"] and (day - T.d(b["notice_day"])).days >= 7 and not b["packet"]:
            kind, why = "packet", f"notice unanswered for 7+ days -> prepare Samadhaan (MSEFC) complaint packet"
        elif strikes >= thr and max(i["days"] for i in act) >= 30 and not b["notice_day"] and not hold:
            kind, why = "notice", f"strikes {strikes}/{thr}, {max(i['days'] for i in act)}d overdue -> formal MSMED notice"
        elif strikes >= 1:
            kind, why = "followup", f"strike {strikes}: " + ("promise broken" if b["broken"] else "no reply") + " -> firm follow-up with statutory interest"
        else:
            kind, why = "nudge", "first/clean contact -> friendly reminder"
        actions.append(dict(type=kind.upper(), b=b, invs=act, score=score, why=why))
    actions.sort(key=lambda a: -a["score"])
    for r, a in enumerate(actions, 1):
        say("PLAN", f"#{r} {a['b']['name']:<24} {T.inr(sum(i['out'] for i in a['invs'])):>10} | priority {a['score']:.1f} | {a['type']}: {a['why']}")
    if not actions: say("PLAN", "nothing to do today (waiting on promises / not yet overdue)")
    # 6. ACT
    for a in actions:
        b, invs, t = a["b"], a["invs"], a["type"]; ctx = dict(broken=bool(b["broken"]), strikes=b["unanswered"] + b["broken"])
        attach, kind = [], t.lower()
        if t == "DOC":
            attach = [T.make_pod(invs[0])]; T.run("update invoices set doc_sent=1 where id=?", invs[0]["id"])
        elif t == "NOTICE":
            path, tot, tint = T.make_notice(b["name"], invs, ds)
            if not approve(mode, f"send legal notice to {b['name']} for {T.inr(tot)} (+{T.inr(tint)} interest) [{path}]"):
                T.run("update buyers set hold_until=? where name=?", str(day + dt.timedelta(3)), b["name"])
                say("ACT", f"owner declined -> notice on hold 3 days; will follow up softly instead"); T.log(ds, "notice_rejected", b["name"]); continue
            attach = [path]; T.run("update buyers set notice_day=? where name=?", ds, b["name"]); S["notices"] += 1
        elif t == "PACKET":
            path = T.make_packet(b["name"], invs, ds)
            if approve(mode, f"Samadhaan complaint packet vs {b['name']} [{path}]"):
                T.run("update buyers set packet=1 where name=?", b["name"]); say("ACT", f"packet ready for owner to file: {path}")
            continue
        text, src = draft(kind, b, invs, ctx)
        msg = dict(day=ds, buyer=b["name"], email=b["email"], kind=kind, invoices=[i["id"] for i in invs],
                   subject=f"{kind.title()}: pending invoices - {T.CFG['supplier']['name']}", text=text, attachments=attach)
        T.deliver(msg); ch.send(ds, msg); S["msgs"] += 1; S["docs"] += len(attach)
        T.run("update buyers set last_contact=?, unanswered=unanswered+? where name=?", ds, 0 if t == "DOC" else 1, b["name"])
        T.log(ds, "sent", b["name"], msg_kind=kind, invoices=msg["invoices"])
        say("ACT", f"{kind.upper()} -> {b['name']} [{src}]" + (f" + attachment {attach[0]}" if attach else ""))
        say("", f"   \"{text}\"")


def run(mode="auto", days=None):
    LOG.clear(); LLM_STATE.update(fails=0, used=0)
    T.init(); ch = Channel(); start = T.d(T.CFG["today"])
    S = dict(n=0, recovered=0, replies=0, msgs=0, docs=0, notices=0, closed=[], obl=[], gap0=0)
    print(f"VASOOLI agent | LLM: {llm_name()} | supplier: {T.CFG['supplier']['name']}")
    inv0 = T.overdue(T.CFG["today"]); out0 = sum(i["out"] for i in inv0 if i["days"] > 0)
    print(f"Ledger: {len(inv0)} open invoices, {T.inr(out0)} overdue as of {start}. SIMULATED buyers & bank feed.")
    for k in range(days or T.CFG["horizon_days"]):
        S["n"] = k + 1; tick(start + dt.timedelta(k), ch, mode, S)
    end = start + dt.timedelta((days or T.CFG["horizon_days"]) - 1)
    left = T.overdue(end)
    summ = dict(overdue_at_start=out0, recovered=S["recovered"], invoices_closed=len(S["closed"]), still_open=sum(i["out"] for i in left),
                messages_sent=S["msgs"], replies_understood=S["replies"], documents=S["docs"], notices=S["notices"],
                salary_shortfall_without_agent=S["gap0"], obligations=[f"{l}: {'on time' if ok else 'SHORT'}" for l, ok in S["obl"]],
                llm_calls=LLM_STATE["used"], llm=llm_name())
    print("\n══════════ OUTCOME (simulated) ══════════")
    money = ("overdue_at_start", "recovered", "still_open", "salary_shortfall_without_agent")
    for k, v in summ.items(): print(f"  {k:<32} {T.inr(v) if k in money else v}")
    print(f"  (≈{S['msgs']} chase messages written/sent by the agent instead of the owner; documents in output/docs/)")
    (T.OUT / "report.json").write_text(json.dumps(summ, indent=2, default=str), encoding="utf-8")
    (T.OUT / "run_log.txt").write_text("\n".join(LOG), encoding="utf-8")
    return summ
