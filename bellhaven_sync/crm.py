"""Thin client for the CRM sandbox API. Reads paginate; writes return the saved record."""
from __future__ import annotations

import time

import requests

from . import config

# The only account fields the API lets us write (from its own 422 message).
MUTABLE_FIELDS = (
    "name", "parent_id", "status", "note", "care_type", "phone", "billing_street",
    "billing_city", "billing_state", "billing_zip", "chow_current_account", "duplicate_of_account",
)


class CRMError(RuntimeError):
    pass


class CRMClient:
    def __init__(self, base: str = None, token: str = None):
        self.base = (base or config.API_BASE).rstrip("/")
        token = token or config.API_TOKEN
        if not token:
            raise CRMError("CRM_API_TOKEN is not set. Copy .env.example to .env and add your token.")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token, "User-Agent": config.USER_AGENT})

    def _request(self, method: str, path: str, retries: int = 2, **kwargs):
        last = None
        for attempt in range(retries + 1):
            try:
                resp = self.session.request(method, self.base + path, timeout=config.HTTP_TIMEOUT, **kwargs)
            except requests.RequestException as exc:
                last = str(exc)
            else:
                if resp.status_code < 400:
                    return resp.json()
                last = "HTTP {}: {}".format(resp.status_code, resp.text[:300])
                if resp.status_code not in (429, 500, 502, 503, 504):
                    break
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        raise CRMError("{} {} failed: {}".format(method, path, last))

    def _list(self, path: str, **filters) -> list:
        items, page = [], 1
        while True:
            body = self._request("GET", path, params=dict(filters, page=page, page_size=200))
            data = body.get("data", [])
            items.extend(data)
            if not data or len(items) >= body.get("total", 0):
                return items
            page += 1

    # --- reads ---------------------------------------------------------------
    def list_accounts(self, **filters) -> list:
        return self._list("/accounts", **filters)

    def list_contacts(self, **filters) -> list:
        return self._list("/contacts", **filters)

    def get_account(self, account_id: str) -> dict:
        return self._request("GET", "/accounts/" + account_id)

    # --- writes --------------------------------------------------------------
    def update_account(self, account_id: str, changes: dict) -> dict:
        bad = set(changes) - set(MUTABLE_FIELDS)
        if bad:
            raise CRMError("Refusing to write non-mutable fields: {}".format(sorted(bad)))
        return self._request("PATCH", "/accounts/" + account_id, json=changes)

    def create_account(self, fields: dict) -> dict:
        # No automatic retry on POST: a retried create could leave two accounts behind.
        return self._request("POST", "/accounts", retries=0, json=fields)
