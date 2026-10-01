# Vasooli - MSME payment-recovery agent (BHARAT AGENTIC 2026 MVP)

An autonomous agent that chases overdue invoices for a small supplier, adapts to buyer replies, escalates with
owner approval, and verifies recovery against the bank statement.

**Understand -> Reason -> Plan -> Use tools -> Act -> Observe -> Re-plan -> Verify**, run as a 12-day simulated loop.

## Run (about 10 seconds)
```bash
pip install -r requirements.txt
python main.py                  # full demo, owner approvals auto-granted
python main.py --approve ask    # you are the owner: approve/reject the legal notice live
python main.py --approve no     # owner refuses escalation -> agent keeps following up
python main.py --serve          # API on :8000 -> POST /run, GET /health  (Method 2 submission)
docker build -t vasooli . && docker run -p 8000:8000 vasooli
```
Works with **no API key** (rule-based fallback). For LLM-powered reply understanding + message drafting:
```bash
export ANTHROPIC_API_KEY=...    # optional: VASOOLI_MODEL=claude-sonnet-5-5 (default)
# or local:  export OLLAMA_MODEL=llama3.1   (Ollama on localhost:11434)
```
Optional real email: set SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS (otherwise messages go to output/outbox.jsonl).
LLM output is guard-railed: a drafted message is discarded for the template if any invoice id or amount is missing.

## Files
| File | Role |
|---|---|
| `agent.py` | the loop: observe, understand (LLM or rules), verify, reason (cash gap, priority), plan (escalation ladder), act (approval gate) |
| `tools.py` | SQLite state, statutory interest, cash projection, PDF notice/POD/Samadhaan packet, outbox/SMTP, bank reconciliation |
| `simulator.py` | SIMULATED buyers + inbox + bank feed (swap for WhatsApp/email/bank adapters in production) |
| `main.py` | CLI + FastAPI |
| `data/` | synthetic ledger (12 invoices, 5 buyers), owner cash obligations, config |

## What the demo shows (5 synthetic buyer personas)
- **Cash-aware prioritisation**: salary (Fri 09-Oct) is Rs 2.4L short -> Apex (Rs 6.9L, 88 days late) ranks first.
- **Re-planning from replies**: promise dates are parsed and counted as expected inflow; once cash is covered the agent relaxes.
- **Dispute resolution**: Bharat Auto says INV-105 has no POD -> agent generates and sends the challan, buyer then commits a date.
- **Broken-promise detection**: Kisan Agro misses 09-Oct -> strike, firm follow-up citing statutory interest.
- **Silent-buyer escalation**: Apex ignores 2 contacts -> MSMED notice drafted -> **owner approval gate** -> sent.
- **Verification**: bank credits matched to invoices (single, multi-invoice subset sums, FIFO partials); ledger and cash updated.
- **Outputs** in `output/`: outbox.jsonl, bank_statement.csv, run_log.txt, report.json, docs/ (PDF notice, POD).

## Honesty notes (read before the demo)
- Buyers, replies and the bank feed are **simulated** (`simulator.py`); impact numbers are on synthetic data.
- Verify with a CA/advocate before real use: MSMED Act ss.15/16/18 (45-day cap, interest at 3x RBI Bank Rate, monthly
  compounding), Income-tax s.43B(h). `bank_rate_pct` in `data/config.json` is a placeholder - set the current RBI Bank Rate.
  Invoice date is used as a proxy for the "appointed day".
- Notices are drafts; nothing escalates without owner approval. The Samadhaan packet is prepared, not filed.
- No aiKart YAML manifest is included (the official guide wasn't available). Point it at `python main.py` or the `/run` API.
