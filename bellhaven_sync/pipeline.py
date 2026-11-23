"""One pipeline run: scrape -> read CRM -> match -> queue proposals. Never writes to the CRM."""
from __future__ import annotations

from datetime import date

from . import config
from .crm import CRMClient
from .matcher import analyze
from .scraper import http_fetch, scrape
from .store import Store


def get_clients():
    """(crm, fetch) for the configured mode. Offline mode never touches the network."""
    if config.OFFLINE:
        from .sandbox import FakeCRM, fixture_fetch
        return FakeCRM(config.OFFLINE_CRM_PATH), fixture_fetch
    return CRMClient(), http_fetch


def run_pipeline(crm=None, fetch=None, store: Store = None, today: str = None) -> dict:
    if crm is None or fetch is None:
        default_crm, default_fetch = get_clients()
        crm, fetch = crm or default_crm, fetch or default_fetch
    store = store or Store()
    today = today or date.today().isoformat()
    run_id = store.start_run()
    try:
        scraped = scrape(fetch=fetch)
        previous = store.last_ok_run()
        floor = (previous["locations"] or 0) * config.MIN_LOCATION_RATIO if previous else 1
        if len(scraped.locations) < max(floor, 1):
            raise RuntimeError(
                "Scrape returned {} locations (previous good run: {}). Assuming the site is broken; "
                "no proposals were generated.".format(len(scraped.locations), previous["locations"] if previous else "n/a"))

        accounts, contacts = crm.list_accounts(), crm.list_contacts()
        result = analyze(scraped.locations, accounts, contacts, scraped.site_notes, today)
        counts = store.sync_proposals(run_id, result["proposals"])
        report = {
            "scrape": scraped.stats, "warnings": scraped.warnings + result["warnings"],
            "records": result["records"], "target_parent": result["target_parent"],
            "locations": [loc.to_dict() for loc in scraped.locations],
        }
        store.finish_run(run_id, "ok", report=report, locations=len(scraped.locations), accounts=len(accounts), **counts)
        return dict(counts, run_id=run_id, status="ok", locations=len(scraped.locations), accounts=len(accounts),
                    warnings=report["warnings"], proposals=result["proposals"])
    except Exception as exc:  # record the failure so the review app can show it, then re-raise
        store.finish_run(run_id, "error", message=str(exc))
        raise
