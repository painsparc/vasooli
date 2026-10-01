"""main.py - entry point.
  python main.py                 -> run the 12-day demo (owner approvals auto-granted)
  python main.py --approve ask   -> you are the owner: approve/reject the legal notice live
  python main.py --approve no    -> owner rejects escalation (shows the human-in-the-loop gate)
  python main.py --serve         -> start the API (POST /run, GET /health) for API-endpoint submission"""
import argparse, sys, threading
import agent

try:
    from fastapi import FastAPI
    app = FastAPI(title="Vasooli - MSME payment recovery agent")
    _lock = threading.Lock()

    @app.get("/health")
    def health(): return {"status": "ok", "llm": agent.llm_name()}

    @app.post("/run")
    def run_agent(approve: str = "auto", days: int = 12):
        """Runs the full agent loop on the bundled synthetic ledger. approve: auto | no"""
        with _lock:
            summary = agent.run("no" if approve == "no" else "auto", days)
            return {"summary": summary, "log": agent.LOG}
except ImportError:
    app = None

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(); ap.add_argument("--approve", default="auto", choices=["auto", "ask", "no"])
    ap.add_argument("--days", type=int, default=None); ap.add_argument("--serve", action="store_true")
    a = ap.parse_args()
    if a.serve:
        import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8000)
    else:
        agent.run(a.approve, a.days)
