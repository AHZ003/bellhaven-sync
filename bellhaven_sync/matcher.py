"""Match website locations to CRM accounts and turn the differences into proposals.

How a location is matched (see README for the long version):

  1. Same facility?  An account is the same physical facility when it is in the same
     state and its normalised street address matches (plus city or zip). If the CRM
     only has a PO Box, we instead require the same city/zip AND the same name AND a
     matching phone or administrator contact. A different real street address is
     always treated as a different facility, however similar the name.
  2. Which record survives?  If several accounts are the same facility, each one gets
     a strength score (already under Bellhaven, name matches, phone matches, the
     website's administrator is a contact, has billing history). A clear winner
     survives and the rest become duplicates. A tie is NOT guessed: it is flagged
     for a human.
  3. What needs fixing on the survivor?  Name, zip, care type, parent. A parent move
     goes through the billing SOP (revenue history AND outstanding AR -> CHOW).
  4. Anything left under the Bellhaven parent that no location claimed is an orphan.

Nothing here writes to the CRM. The output is a list of proposals for the review app.
"""
from __future__ import annotations

import hashlib
import json

from . import config
from .normalize import (is_po_box, money, name_similarity, norm_text, same_name, same_phone,
                        street_key, street_relation, zip_is_typo_of)

KIND_ORDER = ["chow_new_account", "chow_link_existing", "reparent", "update_fields", "create_account",
              "mark_duplicate", "flag_ambiguous", "flag_not_on_website", "cosmetic_name"]
KIND_LABELS = {
    "chow_new_account": "CHOW - new account (billing SOP)",
    "chow_link_existing": "CHOW - link to successor (billing SOP)",
    "reparent": "Wrong parent - move",
    "update_fields": "Field fix",
    "create_account": "New location - create",
    "mark_duplicate": "Duplicate",
    "flag_ambiguous": "Ambiguous - needs review",
    "flag_not_on_website": "Not on website - needs review",
    "cosmetic_name": "Name formatting only (optional)",
}


class MatchError(RuntimeError):
    pass


# --- CRM helpers -----------------------------------------------------------------
class CRMIndex:
    def __init__(self, accounts: list, contacts: list):
        self.accounts = accounts
        self.by_id = {a["account_id"]: a for a in accounts}
        parent_ids = {a["parent_id"] for a in accounts if a.get("parent_id")}
        self.parents = [a for a in accounts if a["account_id"] in parent_ids
                        or "(parent account)" in a["name"].lower()]
        parent_set = {a["account_id"] for a in self.parents}
        self.facilities = [a for a in accounts if a["account_id"] not in parent_set]
        self.contacts = {}
        for c in contacts:
            if c.get("is_active", True):
                self.contacts.setdefault(c["account_id"], []).append(c)

    def find_parent(self, name: str) -> dict:
        hits = [p for p in self.parents if norm_text(name) in norm_text(p["name"])]
        if len(hits) != 1:
            raise MatchError("Expected exactly one parent account matching {!r}, found {}.".format(name, len(hits)))
        return hits[0]

    def parent_name(self, account: dict) -> str:
        parent = self.by_id.get(account.get("parent_id") or "")
        return parent["name"] if parent else (account.get("parent_name") or "(no parent)")

    def snapshot(self, account: dict) -> dict:
        snap = dict(account)
        snap["parent_name"] = self.parent_name(account)
        snap["contacts"] = [{"name": c["name"], "title": c.get("title", "")}
                            for c in self.contacts.get(account["account_id"], [])]
        return snap


def is_resolved(account: dict) -> bool:
    """Already handled by an earlier decision: a marked duplicate or a CHOW'd old account."""
    return bool(account.get("duplicate_of_account") or account.get("chow_current_account"))


def must_preserve_for_billing(account: dict) -> bool:
    """The SOP: revenue history AND outstanding AR > 0 -> the account must not be re-parented."""
    return float(account.get("lifetime_revenue") or 0) > 0 and float(account.get("outstanding_ar") or 0) > 0


def sop_summary(account: dict) -> dict:
    preserve = must_preserve_for_billing(account)
    rev, ar = account.get("lifetime_revenue") or 0, account.get("outstanding_ar") or 0
    if preserve:
        why = ("Has revenue history ({}) AND outstanding AR ({}): billing needs this account preserved, "
               "so its parent must not change.".format(money(rev), money(ar)))
    elif float(rev) > 0:
        why = "Has revenue history ({}) but no outstanding AR: safe to re-parent directly.".format(money(rev))
    else:
        why = "No revenue history (AR {}): safe to re-parent directly.".format(money(ar))
    return {"lifetime_revenue": rev, "outstanding_ar": ar, "preserve": preserve, "explanation": why}


# --- comparing one location with one account --------------------------------------
def compare(loc, account: dict, index: CRMIndex, target_id: str) -> dict:
    street = street_relation(loc.street, account["billing_street"])
    same_state = loc.state == account["billing_state"]
    city = norm_text(loc.city) == norm_text(account["billing_city"])
    zip_exact = loc.zip == account["billing_zip"]
    zip_typo = zip_is_typo_of(loc.zip, account["billing_zip"])
    name_same = same_name(loc.name, account["name"])
    similarity = name_similarity(loc.name, account["name"], config.TARGET_PARENT_NAME)
    phone = same_phone(loc.phone, account.get("phone", ""))
    contact_names = {norm_text(c["name"]) for c in index.contacts.get(account["account_id"], [])}
    admin = bool(loc.administrator) and norm_text(loc.administrator) in contact_names
    under_target = account.get("parent_id") == target_id

    same_facility, basis = False, ""
    if same_state and street == "match" and (city or zip_exact):
        same_facility, basis = True, "street address"
    elif (same_state and street == "unknown" and city and (zip_exact or zip_typo)
          and name_same and (phone or admin)):
        same_facility, basis = True, "name + city/zip + phone/contact (CRM has no physical street)"

    lookalike = not same_facility and (
        name_same or similarity >= 0.8 or phone or (same_state and city and similarity >= 0.6))

    confidence = 60 if street == "match" else 35
    confidence += 10 if (city and zip_exact) else (5 if (city or zip_exact) else 0)
    confidence += (20 if name_same else 0) + (5 if phone else 0) + (5 if admin else 0) + (5 if under_target else 0)

    strength = (4 * under_target + 3 * name_same + 2 * phone + 2 * admin
                + (1 if float(account.get("lifetime_revenue") or 0) > 0 else 0))

    return {
        "account": account, "same_facility": same_facility, "basis": basis, "lookalike": lookalike,
        "street": street, "same_state": same_state, "city": city, "zip_exact": zip_exact, "zip_typo": zip_typo,
        "name_same": name_same, "similarity": similarity, "phone": phone, "admin": admin,
        "under_target": under_target, "confidence": min(confidence, 99), "strength": strength,
    }


def match_evidence(loc, c: dict, index: CRMIndex) -> list:
    a = c["account"]
    lines = []
    if c["street"] == "match":
        lines.append("Street address matches: website '{}' = CRM '{}'{}.".format(
            loc.street, a["billing_street"],
            "" if loc.street == a["billing_street"] else " (after normalising abbreviations)"))
    elif c["street"] == "unknown":
        lines.append("CRM billing street is '{}', so the street cannot be compared; matched on {}.".format(
            a["billing_street"] or "(blank)", c["basis"] or "other signals"))
    else:
        lines.append("Street address differs: website '{}' vs CRM '{}'.".format(loc.street, a["billing_street"]))
    if c["city"] and c["zip_exact"]:
        lines.append("City, state and zip match ({}, {} {}).".format(loc.city, loc.state, loc.zip))
    elif c["city"]:
        lines.append("City and state match, but zip differs: website {} vs CRM {}{}.".format(
            loc.zip, a["billing_zip"], " (looks like a typo)" if c["zip_typo"] else ""))
    if c["name_same"]:
        lines.append("Name matches{}.".format(
            "" if loc.name == a["name"] else " apart from formatting (website '{}', CRM '{}')".format(loc.name, a["name"])))
    else:
        lines.append("Name differs: website '{}' vs CRM '{}'.".format(loc.name, a["name"]))
    if c["phone"]:
        lines.append("Phone matches ({}).".format(loc.phone))
    if c["admin"]:
        lines.append("Website administrator {} is a contact on this CRM account.".format(loc.administrator))
    lines.append("CRM parent: {}.".format(index.parent_name(a)))
    return lines


# --- proposal plumbing --------------------------------------------------------------
def short(parent_name: str) -> str:
    return parent_name.replace(" (Parent Account)", "")


def lookalike_reason(c: dict) -> str:
    if not c["same_state"]:
        return "different state"
    reasons = ["different street address" if c["street"] == "mismatch" else "no comparable street address"]
    if not c["phone"]:
        reasons.append("different phone")
    if not c["admin"]:
        reasons.append("website administrator is not a contact there")
    return ", ".join(reasons)


def confidence_label(score: int) -> str:
    return "High" if score >= 85 else ("Medium" if score >= 65 else "Low")


def fingerprint(kind: str, slug: str, actions: list) -> str:
    """Stable id for 'this exact change to this exact record'. Notes and dates are left out
    so the same finding gets the same fingerprint on every run."""
    core = []
    for act in actions:
        if act["op"] == "update":
            core.append(["update", act["account_id"],
                         sorted((k, v) for k, v in act["changes"].items() if k != "note")])
        else:
            f = act["fields"]
            core.append(["create", norm_text(f["name"]), street_key(f["billing_street"]),
                         f["billing_zip"], f["parent_id"]])
    raw = json.dumps([kind, slug or "", core], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def update_action(account: dict, changes: dict, guard: str = "") -> dict:
    return {"op": "update", "account_id": account["account_id"], "account_name": account["name"],
            "changes": changes, "expect": {k: account.get(k, "") for k in changes}, "guard": guard}


def with_note(account: dict, text: str) -> str:
    existing = (account.get("note") or "").strip()
    return "{} | {}".format(existing, text) if existing else text


def make_proposal(kind, title, summary, actions, evidence, confidence, loc=None, accounts=(), sop=None,
                  index=None) -> dict:
    return {
        "fingerprint": fingerprint(kind, loc.slug if loc else "", actions),
        "kind": kind, "title": title, "summary": summary, "actions": actions, "evidence": evidence,
        "confidence": confidence,
        "confidence_label": "Human call" if kind.startswith("flag_") else confidence_label(confidence),
        "location_slug": loc.slug if loc else "", "website": loc.to_dict() if loc else None,
        "accounts": [index.snapshot(a) for a in accounts], "sop": sop,
        "account_ids": [a["account_id"] for a in accounts],
    }


def site_context(site_notes: list, *phrases) -> list:
    """Sentences from the homepage/About page that mention any of the given names."""
    out = []
    for phrase in phrases:
        words = (phrase or "").replace("(Parent Account)", "").split()
        needle = " ".join(words[:2]).lower() if len(words) > 2 else " ".join(words).lower()
        if not needle:
            continue
        for sentence in site_notes:
            line = 'Website says: "{}"'.format(sentence)
            if needle in sentence.lower() and line not in out:
                out.append(line)
    return out


def primary_care_type(loc, warnings: list) -> tuple:
    mapped = []
    for offering in loc.care_offerings:
        if offering in config.CARE_TYPE_MAP:
            mapped.append(config.CARE_TYPE_MAP[offering])
        else:
            warnings.append("Unmapped care offering {!r} on {} (add it to CARE_TYPE_MAP).".format(offering, loc.name))
    return (mapped[0] if mapped else ""), mapped


def new_account_fields(loc, target: dict, care_type: str, note: str) -> dict:
    return {
        "name": loc.name, "parent_id": target["account_id"], "billing_street": loc.street,
        "billing_city": loc.city, "billing_state": loc.state, "billing_zip": loc.zip,
        "care_type": care_type, "phone": loc.phone, "status": config.STATUS_ACTIVE, "note": note,
    }


# --- the main analysis -----------------------------------------------------------------
def analyze(locations: list, accounts: list, contacts: list, site_notes: list, today: str) -> dict:
    index = CRMIndex(accounts, contacts)
    target = index.find_parent(config.TARGET_PARENT_NAME)
    target_id = target["account_id"]
    tag = "[bellhaven-sync {}]".format(today)

    open_accounts = [a for a in index.facilities if not is_resolved(a)]
    proposals, records, warnings = [], [], []
    claimed = set()

    for loc in locations:
        comparisons = [compare(loc, a, index, target_id) for a in open_accounts]
        same = sorted((c for c in comparisons if c["same_facility"]), key=lambda c: -c["strength"])
        lookalikes = [c for c in comparisons if c["lookalike"]]
        claimed.update(c["account"]["account_id"] for c in same)
        primary_care, mapped_care = primary_care_type(loc, warnings)
        lookalike_lines = [
            "Look-alike rejected: '{}' ({}, {}, {} - parent {}): {}.".format(
                c["account"]["name"], c["account"]["billing_street"], c["account"]["billing_city"],
                c["account"]["billing_state"], short(index.parent_name(c["account"])), lookalike_reason(c))
            for c in lookalikes]

        # -- no CRM account for this facility -> create one ------------------------------
        if not same:
            note = "{} Created from {} (listed on the Bellhaven website; no existing CRM account for this address).".format(tag, loc.url)
            evidence = ["Listed on the Bellhaven website ({}).".format(", ".join(loc.found_on) or "site"),
                        "No CRM account in {} shares this street address ({}, {} {}).".format(
                            loc.state, loc.street, loc.city, loc.zip)]
            evidence += lookalike_lines + site_context(site_notes, loc.name)
            proposals.append(make_proposal(
                "create_account", "Create account: {}".format(loc.name),
                "On the website but not in the CRM. Create it under {}.".format(short(target["name"])),
                [{"op": "create", "ref": "new", "fields": new_account_fields(loc, target, primary_care, note), "guard": ""}],
                evidence, 80 if lookalikes else 90, loc=loc,
                accounts=[c["account"] for c in lookalikes], index=index))
            records.append({"slug": loc.slug, "name": loc.name, "outcome": "No CRM account - create proposed",
                            "account_id": "", "account_name": "", "confidence": None, "evidence": evidence})
            continue

        # -- several accounts, no clear winner -> do not guess -----------------------------
        if len(same) > 1 and same[0]["strength"] == same[1]["strength"]:
            ids = ", ".join(c["account"]["account_id"] for c in same)
            note = ("{} Website lists '{}' at this address, but {} CRM accounts share it ({}) and the "
                    "tie-break signals (Bellhaven parent, name, phone, contact, billing history) do not separate "
                    "them. Needs a person to confirm which record is current before any re-parent or merge.").format(
                        tag, loc.name, len(same), ids)
            actions = [update_action(c["account"], {"status": config.STATUS_NEEDS_REVIEW,
                                                    "note": with_note(c["account"], note)})
                       for c in same if c["account"]["status"] != config.STATUS_NEEDS_REVIEW]
            evidence = ["{} CRM accounts are at the website address {}, {} {}:".format(
                len(same), loc.street, loc.city, loc.state)]
            evidence += ["  - '{}' ({}) parent {}, phone {}, revenue {}, AR {}, {} contact(s)".format(
                c["account"]["name"], c["account"]["account_id"], index.parent_name(c["account"]),
                c["account"].get("phone") or "-", money(c["account"]["lifetime_revenue"]),
                money(c["account"]["outstanding_ar"]), len(index.contacts.get(c["account"]["account_id"], [])))
                for c in same]
            evidence.append("Tie-break signals do not separate them (checked: under the Bellhaven parent, website "
                            "name, website phone {}, website administrator {}, billing history).".format(
                                loc.phone, loc.administrator))
            evidence.append("Picking one would be a guess, so the proposal is to flag all of them rather than "
                            "re-parent, merge or create a fourth record.")
            evidence += site_context(site_notes, *[index.parent_name(c["account"]) for c in same])
            if actions:
                proposals.append(make_proposal(
                    "flag_ambiguous", "Needs review: {} CRM accounts could be {}".format(len(same), loc.name),
                    "Same address, no clear survivor. Flag all as Needs Review with an explanatory note.",
                    actions, evidence, 50, loc=loc, accounts=[c["account"] for c in same], index=index))
            records.append({"slug": loc.slug, "name": loc.name,
                            "outcome": "Ambiguous - flagged for human review" if actions else "Ambiguous - already flagged, waiting on a human",
                            "account_id": ids, "account_name": "", "confidence": 50, "evidence": evidence})
            continue

        # -- one clear survivor ------------------------------------------------------------------
        best, losers = same[0], same[1:]
        acct = best["account"]
        evidence = match_evidence(loc, best, index)
        changes = {}
        if not best["name_same"]:
            changes["name"] = loc.name
        if best["street"] == "match":
            if not best["city"]:
                changes["billing_city"] = loc.city
            if not best["zip_exact"]:
                changes["billing_zip"] = loc.zip
        elif is_po_box(acct["billing_street"]):
            evidence.append("CRM billing street is a PO Box. That is a valid billing address, so it is left alone "
                            "(website street: {}).".format(loc.street))
        if mapped_care and acct.get("care_type") not in mapped_care:
            changes["care_type"] = primary_care
            evidence.append("Care type differs: website offers {} but CRM says '{}'.".format(
                ", ".join(loc.care_offerings), acct.get("care_type")))

        outcome = "Confident match - no change needed"
        if acct.get("parent_id") != target_id:
            sop = sop_summary(acct)
            old_parent = index.parent_name(acct)
            context = site_context(site_notes, old_parent, loc.name)
            if sop["preserve"]:
                note = ("{} Created for change of ownership: {} is now a Bellhaven community ({}). Previous account "
                        "{} under {} is preserved for billing (revenue {}, outstanding AR {}).").format(
                            tag, loc.name, loc.url, acct["account_id"], old_parent,
                            money(sop["lifetime_revenue"]), money(sop["outstanding_ar"]))
                actions = [
                    {"op": "create", "ref": "new", "guard": "sop_preserve", "source_account_id": acct["account_id"],
                     "fields": new_account_fields(loc, target, primary_care, note)},
                    update_action(acct, {"chow_current_account": "$new"}),
                ]
                proposals.append(make_proposal(
                    "chow_new_account",
                    "CHOW: new Bellhaven account for {} (old account preserved)".format(loc.name),
                    "Belongs under {} but is under {}. SOP blocks a re-parent, so create a new account and "
                    "point the old one at it via chow_current_account. The old account is otherwise untouched.".format(
                        short(target["name"]), short(old_parent)),
                    actions, evidence + [sop["explanation"]] + context, best["confidence"],
                    loc=loc, accounts=[acct], sop=sop, index=index))
                outcome = "Wrong parent - CHOW proposed (billing SOP)"
            else:
                changes["parent_id"] = target_id
                extra = [k for k in changes if k != "parent_id"]
                proposals.append(make_proposal(
                    "reparent",
                    "Move '{}' from {} to {}{}".format(acct["name"], short(old_parent),
                                                      short(target["name"]),
                                                      " and rename" if "name" in changes else ""),
                    "Listed on the Bellhaven website but parented to {} in the CRM. {}{}".format(
                        short(old_parent), "Re-parent directly (SOP check passed).",
                        " Also fixes: {}.".format(", ".join(extra)) if extra else ""),
                    [update_action(acct, changes, guard="sop_direct")],
                    evidence + [sop["explanation"]] + context, best["confidence"],
                    loc=loc, accounts=[acct], sop=sop, index=index))
                outcome = "Wrong parent - re-parent proposed"
        elif changes:
            if "name" in changes:
                title = "Rename '{}' to '{}'".format(acct["name"], loc.name)
                summary = "Same facility (address{}), but the CRM still has an old name.".format(
                    ", phone" if best["phone"] else "")
            else:
                title = "Fix {} on {}".format(", ".join(k.replace("billing_", "") for k in changes), acct["name"])
                summary = "Confident match with a data error: " + ", ".join(
                    "{} '{}' -> '{}'".format(k, acct.get(k), v) for k, v in changes.items()) + "."
            proposals.append(make_proposal("update_fields", title, summary, [update_action(acct, changes)],
                                           evidence, best["confidence"], loc=loc, accounts=[acct], index=index))
            outcome = "Match - fix proposed ({})".format(", ".join(changes))
        elif config.PROPOSE_COSMETIC_NAME_SYNC and acct["name"] != loc.name:
            proposals.append(make_proposal(
                "cosmetic_name", "Name formatting: '{}' -> '{}'".format(acct["name"], loc.name),
                "Confident match; the names differ only in formatting. Optional: copy the website's exact "
                "spelling into the CRM. Rejecting this is fine and it will not be offered again.",
                [update_action(acct, {"name": loc.name})], evidence, best["confidence"],
                loc=loc, accounts=[acct], index=index))
            outcome = "Confident match - only a formatting-level name difference"

        records.append({"slug": loc.slug, "name": loc.name, "outcome": outcome, "account_id": acct["account_id"],
                        "account_name": acct["name"], "confidence": best["confidence"], "evidence": evidence})

        # -- the other records for the same facility are duplicates ---------------------------------
        for dup in losers:
            d = dup["account"]
            note = "{} Duplicate of {} ('{}'): same facility at {}, {} {}. Surviving record matches the Bellhaven website.".format(
                tag, acct["account_id"], acct["name"], loc.street, loc.city, loc.state)
            dup_evidence = [
                "Same street address as the surviving account '{}' ({}): '{}' vs '{}'.".format(
                    acct["name"], acct["account_id"], d["billing_street"], acct["billing_street"]),
                "Survivor chosen because it scores higher on: {}.".format(", ".join(
                    label for label, key in (("already under the Bellhaven parent", "under_target"),
                                             ("name matches the website", "name_same"),
                                             ("phone matches the website", "phone"),
                                             ("website administrator is a contact", "admin"))
                    if best[key] and not dup[key]) or "billing history"),
                "This record: parent {}, revenue {}, outstanding AR {}, {} contact(s).".format(
                    index.parent_name(d), money(d["lifetime_revenue"]), money(d["outstanding_ar"]),
                    len(index.contacts.get(d["account_id"], []))),
            ] + (site_context(site_notes, index.parent_name(d)) if d.get("parent_id") not in ("", target_id) else [])
            proposals.append(make_proposal(
                "mark_duplicate", "Duplicate: '{}' [{}] is a second record for '{}'".format(
                    d["name"], short(index.parent_name(d)), acct["name"]),
                "Second CRM record for the same facility. Mark it Inactive and point duplicate_of_account at the survivor.",
                [update_action(d, {"duplicate_of_account": acct["account_id"], "status": config.STATUS_INACTIVE,
                                   "note": with_note(d, note)})],
                dup_evidence, 90 if best["strength"] - dup["strength"] >= 3 else 75,
                loc=loc, accounts=[d, acct], index=index))

    # -- accounts under the Bellhaven parent that no website location claimed ------------------------
    for acct in open_accounts:
        if acct.get("parent_id") != target_id or acct["account_id"] in claimed:
            continue
        if acct["status"] != config.STATUS_ACTIVE:
            continue  # already Inactive or flagged Needs Review - nothing new to say
        successors = [a for a in open_accounts
                      if a["account_id"] != acct["account_id"] and a.get("parent_id") != target_id
                      and a["billing_state"] == acct["billing_state"]
                      and street_relation(a["billing_street"], acct["billing_street"]) == "match"
                      and (a["billing_zip"] == acct["billing_zip"]
                           or norm_text(a["billing_city"]) == norm_text(acct["billing_city"]))]
        base = ["Parent in CRM is {}, but no community on the Bellhaven website is at {}, {} {} {} "
                "({} locations scraped).".format(index.parent_name(acct), acct["billing_street"], acct["billing_city"],
                                                 acct["billing_state"], acct["billing_zip"], len(locations))]
        if len(successors) == 1:
            succ = successors[0]
            sop = sop_summary(acct)
            base.append("Another CRM account is at the same address under a different parent: '{}' ({}), parent {}. "
                        "That points to a change of ownership.".format(succ["name"], succ["account_id"], index.parent_name(succ)))
            base.append(sop["explanation"])
            if sop["preserve"]:
                base.append("The correct-parent account already exists, so no new account is created; the old "
                            "account is linked to it and otherwise left exactly as it is.")
                proposals.append(make_proposal(
                    "chow_link_existing", "CHOW: link '{}' to successor '{}'".format(acct["name"], succ["name"]),
                    "No longer a Bellhaven community; now under {}. SOP blocks a re-parent, so set "
                    "chow_current_account to the existing successor account.".format(short(index.parent_name(succ))),
                    [update_action(acct, {"chow_current_account": succ["account_id"]}, guard="sop_preserve")],
                    base, 85, accounts=[acct, succ], sop=sop, index=index))
            else:
                note = "{} No longer on the Bellhaven website; facility is now '{}' ({}) under {}.".format(
                    tag, succ["name"], succ["account_id"], index.parent_name(succ))
                proposals.append(make_proposal(
                    "mark_duplicate", "Duplicate: '{}' -> '{}' (ownership moved)".format(acct["name"], succ["name"]),
                    "No longer a Bellhaven community and the new owner's account already exists. Mark this one "
                    "Inactive as a duplicate of it.",
                    [update_action(acct, {"duplicate_of_account": succ["account_id"], "status": config.STATUS_INACTIVE,
                                          "note": with_note(acct, note)})],
                    base, 80, accounts=[acct, succ], sop=sop, index=index))
        else:
            note = ("{} Under the Bellhaven parent but not on the Bellhaven website ({} communities checked). "
                    "Possibly sold or closed; new owner unknown, so parent left unchanged. Verify ownership.").format(
                        tag, len(locations))
            base.append("No other CRM account shares this address, so there is no evidence of who owns it now. "
                        "Parent is left unchanged; the account is flagged so a rep verifies before selling into it.")
            base.append("Revenue {}, outstanding AR {}.".format(money(acct["lifetime_revenue"]), money(acct["outstanding_ar"])))
            proposals.append(make_proposal(
                "flag_not_on_website", "Needs review: '{}' is not on the Bellhaven website".format(acct["name"]),
                "Under the Bellhaven parent in the CRM but no longer listed by Bellhaven. Flag as Needs Review.",
                [update_action(acct, {"status": config.STATUS_NEEDS_REVIEW, "note": with_note(acct, note)})],
                base, 70, accounts=[acct], index=index))

    proposals.sort(key=lambda p: (KIND_ORDER.index(p["kind"]), p["title"]))
    return {"proposals": proposals, "records": records, "warnings": warnings,
            "target_parent": {"account_id": target_id, "name": target["name"]}}
