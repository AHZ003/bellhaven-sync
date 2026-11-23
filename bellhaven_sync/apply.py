"""Write one approved proposal to the CRM.

Rules this module enforces at the moment of approval (not just when the proposal was made):
  * the record must still look the way it did when the proposal was created, otherwise the
    proposal is stale and nothing is written;
  * the billing SOP is re-checked against live revenue / AR numbers;
  * every step is safe to repeat (fields already at the target value are skipped, and a
    "create" first looks for the account it would create), so a retry after a network error
    cannot double-write;
  * every write is read back and verified.
"""
from __future__ import annotations

from .matcher import must_preserve_for_billing
from .normalize import norm_text, street_key


class StaleProposal(RuntimeError):
    """The CRM changed since this proposal was generated. Re-run the pipeline."""


def _check_guard(guard: str, account: dict):
    preserve = must_preserve_for_billing(account)
    if guard == "sop_direct" and preserve:
        raise StaleProposal("{} now has revenue history and outstanding AR, so the SOP no longer allows a direct "
                            "re-parent. Re-run the pipeline to get a CHOW proposal.".format(account["name"]))
    if guard == "sop_preserve" and not preserve:
        raise StaleProposal("{} no longer has both revenue history and outstanding AR, so the CHOW path no longer "
                            "applies. Re-run the pipeline.".format(account["name"]))


def _validate_update(crm, action: dict) -> dict:
    account = crm.get_account(action["account_id"])
    for field, before in action["expect"].items():
        target = action["changes"][field]
        current = account.get(field) or ""
        if current != (before or "") and current != target and not str(target).startswith("$"):
            raise StaleProposal("{}.{} was '{}' when proposed but is now '{}'.".format(
                account["name"], field, before, current))
    if action.get("guard"):
        _check_guard(action["guard"], account)
    return account


def _find_existing(crm, fields: dict):
    """Has this exact account already been created (e.g. by an earlier attempt that half-failed)?"""
    for a in crm.list_accounts(parent_id=fields["parent_id"]):
        if (a.get("parent_id") == fields["parent_id"] and a.get("billing_zip") == fields["billing_zip"]
                and street_key(a.get("billing_street", "")) == street_key(fields["billing_street"])
                and norm_text(a.get("name", "")) == norm_text(fields["name"])
                and not a.get("duplicate_of_account")):
            return a
    return None


def apply_proposal(crm, proposal: dict) -> dict:
    """Returns {"log": [...], "created": [...ids], "updated": [...ids]}. Raises on failure."""
    log, created, updated, refs = [], [], [], {}
    actions = proposal["actions"]

    # Phase 1: validate everything before writing anything.
    for action in actions:
        if action["op"] == "update":
            _validate_update(crm, action)
        elif action.get("guard"):
            _check_guard(action["guard"], crm.get_account(action["source_account_id"]))

    # Phase 2: write, in order.
    for action in actions:
        if action["op"] == "create":
            fields = action["fields"]
            existing = _find_existing(crm, fields)
            if existing:
                account = existing
                log.append("Account '{}' already exists as {} - reused, not created again.".format(
                    account["name"], account["account_id"]))
            else:
                account = crm.create_account(fields)
                if "account_id" not in account:      # unexpected response shape: look the new record up instead
                    account = account.get("data") or _find_existing(crm, fields) or {}
                if "account_id" not in account:
                    raise RuntimeError("Create returned no account id for '{}'.".format(fields["name"]))
                created.append(account["account_id"])
                log.append("Created account '{}' -> {}.".format(fields["name"], account["account_id"]))
            saved = crm.get_account(account["account_id"])
            wrong = [k for k in ("name", "parent_id", "billing_street", "billing_zip") if saved.get(k) != fields[k]]
            if wrong:
                raise RuntimeError("Read-back mismatch on new account {}: {}".format(account["account_id"], wrong))
            refs[action.get("ref", "new")] = account["account_id"]
        else:
            changes = {k: refs.get(v[1:], v) if isinstance(v, str) and v.startswith("$") else v
                       for k, v in action["changes"].items()}
            unresolved = [k for k, v in changes.items() if isinstance(v, str) and v.startswith("$")]
            if unresolved:
                raise RuntimeError("Unresolved reference for {}".format(unresolved))
            current = crm.get_account(action["account_id"])
            todo = {k: v for k, v in changes.items() if (current.get(k) or "") != v}
            if not todo:
                log.append("'{}' ({}) already has these values - nothing to write.".format(
                    current["name"], action["account_id"]))
                continue
            crm.update_account(action["account_id"], todo)
            saved = crm.get_account(action["account_id"])
            wrong = [k for k, v in todo.items() if saved.get(k) != v]
            if wrong:
                raise RuntimeError("Read-back mismatch on {}: {}".format(action["account_id"], wrong))
            updated.append(action["account_id"])
            log.append("Updated '{}' ({}): {}.".format(
                action["account_name"], action["account_id"],
                "; ".join("{} -> '{}'".format(k, v if k != "note" else "(note added)") for k, v in todo.items())))
    return {"log": log, "created": created, "updated": updated}
