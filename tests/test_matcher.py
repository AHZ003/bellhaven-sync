"""The matcher against the real snapshot: one test per kind of mess in the data."""
from collections import Counter

import pytest

from conftest import BELLHAVEN


@pytest.fixture
def proposals(run):
    return run()["proposals"]


def find(proposals, kind, text):
    hits = [p for p in proposals if p["kind"] == kind and text in p["title"]]
    assert len(hits) == 1, "expected one {} proposal mentioning {!r}, got {}".format(kind, text, len(hits))
    return hits[0]


def test_overall_shape(proposals):
    assert Counter(p["kind"] for p in proposals) == {
        "chow_new_account": 2, "chow_link_existing": 1, "reparent": 3, "update_fields": 4,
        "create_account": 4, "mark_duplicate": 5, "flag_ambiguous": 1, "flag_not_on_website": 2,
        "cosmetic_name": 4}
    assert len({p["fingerprint"] for p in proposals}) == len(proposals)


def test_sop_revenue_and_ar_means_new_account_not_reparent(proposals):
    for town, old_id in (("Tiffin", "001U6RW32TY0WSXZZB"), ("Marietta", "001A34WFSUYHCRBLFT")):
        p = find(proposals, "chow_new_account", town)
        create, link = p["actions"]
        assert create["op"] == "create" and create["fields"]["parent_id"] == BELLHAVEN
        assert link["account_id"] == old_id and link["changes"] == {"chow_current_account": "$new"}
        assert p["sop"]["preserve"] is True


def test_sop_no_outstanding_ar_means_direct_reparent(proposals):
    lima = find(proposals, "reparent", "Lima")          # revenue $47k, AR $0
    assert lima["actions"][0]["changes"] == {"parent_id": BELLHAVEN}
    findlay = find(proposals, "reparent", "Findlay")    # had no parent at all
    assert findlay["actions"][0]["expect"] == {"parent_id": ""}
    zanesville = find(proposals, "reparent", "Zanesville")
    assert zanesville["actions"][0]["changes"] == {"name": "Bellhaven of Zanesville", "parent_id": BELLHAVEN}


def test_sold_facility_links_to_existing_successor_instead_of_creating_one(proposals):
    p = find(proposals, "chow_link_existing", "Sandusky")
    assert p["actions"] == [{"op": "update", "account_id": "001SXSF4ELF0Z2LGDM", "account_name": "Bellhaven of Sandusky",
                             "changes": {"chow_current_account": "0017JP8Z1UQ763BVK3"},
                             "expect": {"chow_current_account": ""}, "guard": "sop_preserve"}]


def test_outdated_names_and_zip_typo(proposals):
    assert find(proposals, "update_fields", "Sunny Acres")["actions"][0]["changes"] == {"name": "Bellhaven Willow Creek"}
    assert find(proposals, "update_fields", "Riverbend")["actions"][0]["changes"] == {"name": "Bellhaven of Chagrin Falls"}
    assert find(proposals, "update_fields", "Chesterton")["actions"][0]["changes"] == {"name": "Bellhaven of Chesterton"}
    assert find(proposals, "update_fields", "Portsmouth")["actions"][0]["changes"] == {"billing_zip": "45662"}


def test_lookalikes_do_not_match(proposals):
    # Same name in another state, similar name at a different street, "Carlisle" vs "New Carlisle".
    created = {p["actions"][0]["fields"]["name"] for p in proposals if p["kind"] == "create_account"}
    assert created == {"Amberly Manor", "Bellhaven at Union Square", "Bellhaven of Carlisle", "Bellhaven of Batavia"}
    touched = {a.get("account_id") for p in proposals for a in p["actions"]}
    assert not touched & {"0015D74ZLRY810RGY5", "001GNU41AVXZRLLJ9P", "0019M1Z45PGV2DJ971"}


def test_duplicates_point_at_the_right_survivor(proposals):
    dups = {p["actions"][0]["account_id"]: p["actions"][0]["changes"] for p in proposals if p["kind"] == "mark_duplicate"}
    assert {k: v["duplicate_of_account"] for k, v in dups.items()} == {
        "001QU150PM4Z15UA71": "001EGU7BMJ942ZTRE6",   # Owosso: survivor has the website phone + administrator
        "00159PL81N38KM4FHM": "001U1750VLVJAGG1S5",   # Monroe (Harborview copy)
        "0011AB44D05WLA9HTX": "001U1750VLVJAGG1S5",   # Monroe (Cedar Trail copy)
        "001JD2MWRA74LTSN24": "001UELXDAKFRKB8932",   # Port Clinton
        "001BLYF02K97SZLZHH": "001CVBBCSDM7YHN220",   # Erie
    }
    assert all(v["status"] == "Inactive" for v in dups.values())


def test_three_way_tie_is_flagged_not_guessed(proposals):
    p = find(proposals, "flag_ambiguous", "Kettering")
    assert {a["account_id"] for a in p["actions"]} == {"001WR41PYNWXCAE2X4", "0016KTS1UAWBRXS09J", "001B7XZAA3AFALS9GP"}
    assert all(set(a["changes"]) == {"status", "note"} and a["changes"]["status"] == "Needs Review" for a in p["actions"])


def test_bellhaven_accounts_missing_from_the_website_are_flagged(proposals):
    flagged = {p["actions"][0]["account_id"] for p in proposals if p["kind"] == "flag_not_on_website"}
    assert flagged == {"0016PVXH4B25HWR7QE", "00116ETS45BL7DTQP7"}   # Coldwater, Alliance


def test_confident_matches_need_no_material_change(run, store, proposals):
    import json
    records = json.loads(store.runs(1)[0]["report_json"])["records"]
    confident = {r["account_id"] for r in records if r["outcome"].startswith("Confident")}
    assert len(confident) == 21
    # PO Box billing address and abbreviation-only address differences are matched and left alone.
    assert {"001NXP9X46CWEPSLSV", "0014DHACE6WQU3RMM5", "001MVVAPY6U5TVYRC4"} <= confident
    material = {a.get("account_id") for p in proposals if p["kind"] != "cosmetic_name" for a in p["actions"]}
    assert not material & confident


def test_formatting_only_name_differences_are_offered_separately_and_can_be_switched_off(run, proposals, monkeypatch, store):
    cosmetic = {p["actions"][0]["account_id"]: p["actions"][0]["changes"] for p in proposals if p["kind"] == "cosmetic_name"}
    assert cosmetic == {
        "001TZCSTBZFM6K5BGN": {"name": "Bellhaven Rehabilitation & Nursing of Grove City"},
        "00191H7JR16471SZ34": {"name": "Bellhaven at Sycamore Ridge"},
        "0010KJFP601YNTZVDA": {"name": "The Arbors at Bellhaven - Dayton"},
        "001RJ0D7Z5MAZN5HX5": {"name": "Bellhaven Healthcare Centre of Ashland"},
    }
    from bellhaven_sync import config
    monkeypatch.setattr(config, "PROPOSE_COSMETIC_NAME_SYNC", False)
    assert not [p for p in run()["proposals"] if p["kind"] == "cosmetic_name"]
