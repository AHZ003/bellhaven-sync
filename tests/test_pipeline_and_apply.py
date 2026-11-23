"""End to end: approve, reject, re-run. Plus the safety checks at approval time."""
import pytest

from bellhaven_sync.apply import StaleProposal, apply_proposal
from bellhaven_sync.crm import CRMError
from bellhaven_sync.sandbox import fixture_fetch
from bellhaven_sync.scraper import ScrapeError
from bellhaven_sync.store import APPLIED, PENDING, REJECTED
from conftest import BELLHAVEN


def approve_all(crm, store):
    for item in store.list((PENDING,)):
        store.decide(item["fingerprint"], APPLIED, result=apply_proposal(crm, item["payload"]))


def test_approving_everything_gives_a_clean_crm_and_a_quiet_second_run(run, crm, store):
    assert run()["new_proposals"] == 26
    approve_all(crm, store)
    acct = {a["account_id"]: a for a in crm.list_accounts()}

    # SOP: old accounts keep their parent and only gain the CHOW pointer.
    for old_id, name in (("001U6RW32TY0WSXZZB", "Bellhaven of Tiffin"), ("001A34WFSUYHCRBLFT", "Bellhaven of Marietta")):
        old = acct[old_id]
        new = acct[old["chow_current_account"]]
        assert old["parent_id"] == "001FWSQ30SFW6S7604" and old["status"] == "Active" and old["note"] == ""
        assert new["name"] == name and new["parent_id"] == BELLHAVEN and new["created_by_candidate"]
    sandusky = acct["001SXSF4ELF0Z2LGDM"]
    assert sandusky["chow_current_account"] == "0017JP8Z1UQ763BVK3" and sandusky["parent_id"] == BELLHAVEN
    assert sandusky["status"] == "Active" and sandusky["note"] == ""

    assert acct["001LGFPBJY4N9MB6KL"]["parent_id"] == BELLHAVEN                     # Lima
    assert acct["001UKEFGADQ8YCZ4YM"]["parent_id"] == BELLHAVEN                     # Findlay
    assert acct["001H1JMVZWP46D5VUF"]["name"] == "Bellhaven of Zanesville"
    assert acct["001CF3LDWVRGL09P4F"]["billing_zip"] == "45662"
    assert acct["001QU150PM4Z15UA71"]["status"] == "Inactive"
    assert acct["0016PVXH4B25HWR7QE"]["status"] == "Needs Review"
    assert len(crm.list_accounts()) == 121 + 4 + 2                                  # 4 new locations + 2 CHOW accounts
    assert sum(1 for m, _, _ in crm.writes if m == "POST") == 6

    second = run()
    assert second["proposals"] == [] and second["new_proposals"] == 0


def test_rejected_proposals_are_never_proposed_again(run, store):
    first = run()
    victim = next(p for p in first["proposals"] if p["kind"] == "update_fields" and "Sunny Acres" in p["title"])
    store.decide(victim["fingerprint"], REJECTED, note="keeping the legal name")
    second = run()
    assert second["new_proposals"] == 0 and second["skipped_decided"] == 1 and second["still_pending"] == 25
    assert store.get(victim["fingerprint"])["status"] == REJECTED
    assert victim["fingerprint"] not in {i["fingerprint"] for i in store.list((PENDING,))}


def test_nothing_is_written_without_approval(run, crm):
    run()
    run()
    assert crm.writes == []


def test_sop_is_rechecked_at_approval_time(run, crm):
    lima = next(p for p in run()["proposals"] if p["kind"] == "reparent" and "Lima" in p["title"])
    next(a for a in crm.accounts if a["account_id"] == "001LGFPBJY4N9MB6KL")["outstanding_ar"] = 2500  # AR appears later
    with pytest.raises(StaleProposal):
        apply_proposal(crm, lima)
    assert crm.writes == []


def test_stale_proposal_is_refused_if_the_record_changed(run, crm):
    rename = next(p for p in run()["proposals"] if "Riverbend" in p["title"])
    next(a for a in crm.accounts if a["account_id"] == "001RJU1X4NBWC1Q0G7")["name"] = "Someone Else Renamed It"
    with pytest.raises(StaleProposal):
        apply_proposal(crm, rename)
    assert crm.writes == []


def test_retry_after_a_half_finished_chow_does_not_create_a_second_account(run, crm):
    tiffin = next(p for p in run()["proposals"] if p["kind"] == "chow_new_account" and "Tiffin" in p["title"])
    real_update = crm.update_account
    crm.update_account = lambda *a, **k: (_ for _ in ()).throw(CRMError("network blip"))
    with pytest.raises(CRMError):
        apply_proposal(crm, tiffin)                     # account created, link step failed
    crm.update_account = real_update
    result = apply_proposal(crm, tiffin)                # retry
    assert sum(1 for m, _, _ in crm.writes if m == "POST") == 1
    assert "reused" in " ".join(result["log"])
    old = crm.get_account("001U6RW32TY0WSXZZB")
    assert crm.get_account(old["chow_current_account"])["name"] == "Bellhaven of Tiffin"


def test_broken_scrape_aborts_instead_of_flagging_the_whole_portfolio(run, store):
    run()

    def half_site(url):
        if "page=" in url or url.rstrip("/").endswith(("-of-tiffin", "-of-marietta")):
            raise ScrapeError("Could not fetch {}: HTTP 500".format(url))
        return fixture_fetch(url).replace("Next &rarr;", "")   # pagination link vanished

    with pytest.raises(Exception):
        run(fetch=half_site)
    assert store.runs(1)[0]["status"] == "error"
    assert len(store.list((PENDING,))) == 26            # queue untouched


def test_review_app_approve_and_reject(monkeypatch):
    import review_app
    client = review_app.app.test_client()
    assert client.post("/run", follow_redirects=True).status_code == 200
    store = review_app.Store()
    items = store.list((PENDING,))
    assert len(items) == 26
    zip_fix = next(i for i in items if "Portsmouth" in i["title"])
    owosso = next(i for i in items if "Owosso" in i["title"])
    assert client.post("/proposal/{}/approve".format(zip_fix["fingerprint"])).status_code == 302
    assert client.post("/proposal/{}/reject".format(owosso["fingerprint"]), data={"note": "test"}).status_code == 302
    store = review_app.Store()
    assert store.get(zip_fix["fingerprint"])["status"] == APPLIED
    assert store.get(owosso["fingerprint"])["status"] == REJECTED
    client.post("/run")
    assert len(review_app.Store().list((PENDING,))) == 24
    assert b"Decided (2)" in client.get("/?tab=decided").data
