#!/usr/bin/env python3
"""Local review app: a person approves or rejects each proposal. Nothing is written to the
CRM unless someone clicks Approve here.

    python review_app.py             # http://127.0.0.1:5050 against the live CRM
    python review_app.py --offline   # rehearsal against fixture data, no real writes
"""
import json
import os
import sys

if "--offline" in sys.argv:          # must be set before config is imported
    os.environ["BELLHAVEN_OFFLINE"] = "1"

from flask import Flask, flash, redirect, render_template, request, url_for

from bellhaven_sync import config
from bellhaven_sync.apply import StaleProposal, apply_proposal
from bellhaven_sync.matcher import KIND_LABELS, KIND_ORDER
from bellhaven_sync.normalize import money
from bellhaven_sync.pipeline import get_clients, run_pipeline
from bellhaven_sync.store import APPLIED, FAILED, PENDING, REJECTED, STALE, Store

app = Flask(__name__)
app.secret_key = os.urandom(16)
app.jinja_env.filters["money"] = money


def crm_link(account_id: str) -> str:
    """Link into the read-only CRM browser so a change can be seen landing."""
    if config.OFFLINE or not account_id or not config.API_TOKEN:
        return ""
    return "{}/crm/{}/accounts/{}".format(config.BASE_URL, config.API_TOKEN, account_id)


app.jinja_env.globals.update(crm_link=crm_link, KIND_LABELS=KIND_LABELS, offline=config.OFFLINE)


@app.route("/")
def queue():
    store = Store()
    tab = request.args.get("tab", "pending")
    counts = store.counts()
    last_run = store.runs(1)
    report = json.loads(last_run[0]["report_json"]) if last_run and last_run[0]["report_json"] else None
    pending = store.list((PENDING, FAILED))
    pending.sort(key=lambda p: (KIND_ORDER.index(p["kind"]), p["title"]))
    groups = []
    for kind in KIND_ORDER:
        items = [p for p in pending if p["kind"] == kind]
        if items:
            groups.append((kind, items))
    return render_template(
        "queue.html", tab=tab, groups=groups, n_pending=len(pending),
        decided=store.list((APPLIED, REJECTED, STALE)) if tab == "decided" else [],
        n_decided=sum(counts.get(s, 0) for s in (APPLIED, REJECTED, STALE)),
        records=[r for r in (report or {}).get("records", []) if r["outcome"].startswith("Confident")],
        report=report, runs=store.runs(), last_run=last_run[0] if last_run else None)


@app.post("/proposal/<fingerprint>/approve")
def approve(fingerprint):
    store = Store()
    item = store.get(fingerprint)
    if not item or item["status"] not in (PENDING, FAILED):
        flash("That proposal is not open for a decision (status: {}).".format(item["status"] if item else "missing"), "warn")
        return redirect(url_for("queue"))
    crm, _ = get_clients()
    try:
        result = apply_proposal(crm, item["payload"])
    except StaleProposal as exc:
        store.decide(fingerprint, STALE, note=str(exc))
        flash("Not applied - the CRM changed since this was proposed: {} Re-run the pipeline.".format(exc), "warn")
    except Exception as exc:  # leave it in the queue as 'failed' so it can be retried safely
        store.decide(fingerprint, FAILED, note=str(exc))
        flash("Write failed, nothing was marked as done: {}".format(exc), "error")
    else:
        store.decide(fingerprint, APPLIED, note=request.form.get("note", ""), result=result)
        flash("Applied: {} - {}".format(item["title"], " ".join(result["log"])), "ok")
    return redirect(url_for("queue"))


@app.post("/proposal/<fingerprint>/reject")
def reject(fingerprint):
    store = Store()
    item = store.get(fingerprint)
    if not item or item["status"] not in (PENDING, FAILED):
        flash("That proposal is not open for a decision.", "warn")
        return redirect(url_for("queue"))
    store.decide(fingerprint, REJECTED, note=request.form.get("note", "").strip())
    flash("Rejected: {}. It will not be proposed again.".format(item["title"]), "ok")
    return redirect(url_for("queue"))


@app.post("/run")
def run_now():
    try:
        s = run_pipeline()
        flash("Pipeline run #{}: {} new, {} still pending, {} already decided and not re-proposed, {} went stale.".format(
            s["run_id"], s["new_proposals"], s["still_pending"], s["skipped_decided"], s["went_stale"]), "ok")
    except Exception as exc:
        flash("Pipeline failed: {}".format(exc), "error")
    return redirect(url_for("queue"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5050"))
    print("Review queue: http://127.0.0.1:{}  ({})".format(port, "OFFLINE rehearsal" if config.OFFLINE else "LIVE CRM"))
    app.run(host="127.0.0.1", port=port, debug=False)
