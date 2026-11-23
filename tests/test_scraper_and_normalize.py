from bellhaven_sync.normalize import same_name, street_key, street_relation, zip_is_typo_of
from bellhaven_sync.sandbox import fixture_fetch
from bellhaven_sync.scraper import scrape


def test_scrapes_every_location_including_the_one_missing_from_the_directory():
    result = scrape(fetch=fixture_fetch)
    assert len(result.locations) == 35
    assert result.warnings == []
    assert result.stats["directory_pages"] == 3          # did not loop on the repeating last page
    assert result.stats["in_directory"] == 34 and result.stats["homepage_claim"] == 35
    findlay = next(loc for loc in result.locations if loc.slug == "bellhaven-meadows-of-findlay")
    assert findlay.found_on == ["homepage"]


def test_detail_page_fields():
    by_slug = {loc.slug: loc for loc in scrape(fetch=fixture_fetch).locations}
    erie = by_slug["bellhaven-shores-of-erie"]
    assert (erie.street, erie.city, erie.state, erie.zip) == ("4930 W Lake Rd", "Erie", "PA", "16505")
    assert erie.care_offerings == ["Assisted Living", "Memory Support"]
    assert erie.administrator == "Angela Quinn" and erie.phone == "(614) 792-9343"
    assert by_slug["bellhaven-rehabilitation-nursing-of-grove-city"].name == "Bellhaven Rehabilitation & Nursing of Grove City"


def test_street_normalisation():
    assert street_key("980 West Michigan Avenue") == street_key("980 W Michigan Ave")
    assert street_key("4850 NW Sylvania Ave") == street_key("4850 Northwest Sylvania Avenue")
    assert street_key("3313 Wilmington Pk") == street_key("3313 Wilmington Pike")
    assert street_relation("118 Union Square Dr", "240 Market St") == "mismatch"
    assert street_relation("3156 W Prospect Rd", "PO Box 517") == "unknown"


def test_cosmetic_name_differences_are_the_same_name():
    assert same_name("The Arbors at Bellhaven - Dayton", "Arbors at Bellhaven Dayton")
    assert same_name("Bellhaven Rehabilitation & Nursing of Grove City", "Bellhaven Rehab and Nursing of Grove City")
    assert same_name("Bellhaven Healthcare Centre of Ashland", "Bellhaven Health Care Center of Ashland")
    assert not same_name("Bellhaven of Carlisle", "Bellhaven of New Carlisle")
    assert not same_name("Bellhaven Willow Creek", "Sunny Acres Retirement Home")


def test_zip_typo():
    assert zip_is_typo_of("45626", "45662")
    assert not zip_is_typo_of("44280", "45344")
