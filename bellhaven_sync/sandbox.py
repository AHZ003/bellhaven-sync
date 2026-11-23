"""Offline stand-ins for the website and the CRM, built from a snapshot in fixtures/.

Used by the tests, and by BELLHAVEN_OFFLINE=1 so the whole review flow can be rehearsed
without writing anything to the real CRM. FakeCRM follows the real API's rules (same
mutable fields, same status values) so behaviour matches.
"""
from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .crm import MUTABLE_FIELDS, CRMError
from .scraper import ScrapeError

VALID_STATUS = (config.STATUS_ACTIVE, config.STATUS_INACTIVE, config.STATUS_NEEDS_REVIEW)


def fixture_fetch(url: str) -> str:
    """Serve fixture pages the way the real site does (page numbers past the end repeat the last page)."""
    site = config.FIXTURES_DIR / "site"
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    if path == "/":
        target = site / "index.html"
    elif path == "/about":
        target = site / "about.html"
    elif path == "/communities":
        pages = sorted(site.glob("communities_page_*.html"))
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        target = pages[min(max(page, 1), len(pages)) - 1]
    else:
        target = site / (path.lstrip("/") + ".html")
    if not target.exists():
        raise ScrapeError("Could not fetch {}: HTTP 404".format(url))
    return target.read_text(encoding="utf-8")


class FakeCRM:
    def __init__(self, path: Path = None):
        self.path = Path(path) if path else None
        if self.path and self.path.exists():
            state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            state = {"accounts": json.loads((config.FIXTURES_DIR / "crm_accounts.json").read_text(encoding="utf-8")),
                     "contacts": json.loads((config.FIXTURES_DIR / "crm_contacts.json").read_text(encoding="utf-8"))}
        self.accounts, self.contacts = state["accounts"], state["contacts"]
        self.writes = []  # (method, account_id, payload) - handy for assertions

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"accounts": self.accounts, "contacts": self.contacts}, indent=1), encoding="utf-8")

    def _find(self, account_id):
        for a in self.accounts:
            if a["account_id"] == account_id:
                return a
        raise CRMError("GET /accounts/{} failed: HTTP 404: Account not found".format(account_id))

    def _stamp(self, account):
        parent = next((a for a in self.accounts if a["account_id"] == account.get("parent_id")), None)
        account["parent_name"] = parent["name"] if parent else ""
        account["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    def list_accounts(self, **filters):
        out = [dict(a) for a in self.accounts]
        if filters.get("parent_id"):
            out = [a for a in out if a["parent_id"] == filters["parent_id"]]
        if filters.get("zip"):
            out = [a for a in out if a["billing_zip"] == filters["zip"]]
        return out

    def list_contacts(self, **filters):
        return [dict(c) for c in self.contacts]

    def get_account(self, account_id):
        return dict(self._find(account_id))

    def update_account(self, account_id, changes):
        account = self._find(account_id)
        if not changes or set(changes) - set(MUTABLE_FIELDS):
            raise CRMError("PATCH failed: HTTP 422: No mutable fields provided.")
        if "status" in changes and changes["status"] not in VALID_STATUS:
            raise CRMError("PATCH failed: HTTP 422: status must be one of {}".format(list(VALID_STATUS)))
        account.update(changes)
        self._stamp(account)
        self.writes.append(("PATCH", account_id, dict(changes)))
        self._save()
        return dict(account)

    def create_account(self, fields):
        if not fields.get("name"):
            raise CRMError("POST failed: HTTP 422: name is required")
        account = {
            "account_id": "001" + "".join(random.choices(string.ascii_uppercase + string.digits, k=15)),
            "name": "", "parent_id": "", "parent_name": "", "billing_street": "", "billing_city": "",
            "billing_state": "", "billing_zip": "", "care_type": "", "status": config.STATUS_ACTIVE, "phone": "",
            "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "",
            "note": "", "created_by_candidate": True, "updated_at": "",
        }
        account.update({k: v for k, v in fields.items() if k in MUTABLE_FIELDS})
        self._stamp(account)
        self.accounts.append(account)
        self.writes.append(("POST", account["account_id"], dict(fields)))
        self._save()
        return dict(account)
