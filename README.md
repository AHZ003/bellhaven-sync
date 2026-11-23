# bellhaven-sync

Keeps the CRM's picture of **Bellhaven Senior Living** in line with what Bellhaven lists on its own website.

Every day it scrapes the site, compares each community against the CRM, and puts proposed fixes in a review queue with the evidence behind them. A person approves or rejects each one. Only approved changes are written to the CRM, and anything already decided never comes back.

```
website ──scrape──┐
                  ├─ match + classify ──> proposals (SQLite) ──> review app ──approve──> CRM API
CRM API ──read────┘                                                  └──reject──> remembered, never re-proposed
```

## Run it

Needs Python 3.9+.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # then put your CRM token in .env

python run_pipeline.py             # scrape + match, fills the review queue (writes nothing to the CRM)
python review_app.py               # open http://127.0.0.1:5050 and approve / reject
python -m pytest -q                # 24 tests, all offline
```

**Rehearsal mode.** Add `--offline` to either command to run against a snapshot of the website and CRM saved in `fixtures/`. It uses a fake CRM stored in a local file, so you can click Approve on everything and nothing reaches the real CRM. Delete `data/offline_*` to reset it.

## How matching works

The website is treated as the source of truth for *which communities Bellhaven operates today*. The CRM is the source of truth for billing history.

**1. Is it the same facility?** An account is the same physical facility as a website location when it is in the same state and the normalised street address matches (`980 West Michigan Avenue` = `980 W Michigan Ave`, `Pk` = `Pike`, `NW` = `Northwest`), plus city or zip. Address is the anchor because names change with every acquisition and addresses don't.

- If the CRM only has a PO Box, the street can't be compared, so it needs the same city/zip **and** the same name **and** a matching phone or administrator contact.
- A different real street address is always a different facility, no matter how similar the name is. That is what stops "Bellhaven at Union Square" from grabbing "Union Square Senior Living" a few blocks away, or "Amberly Manor" in Ohio from grabbing "Amberly Manor" in Colorado.

**2. If several accounts are the same facility, which one survives?** Each gets a strength score: already under the Bellhaven parent (4), name matches the website (3), phone matches (2), the website's administrator is a contact on the account (2), has billing history (1). A clear winner survives; the others are proposed as duplicates (`duplicate_of_account` set, status `Inactive`). **A tie is not guessed.** All tied records get flagged `Needs Review` with a note.

**3. What needs fixing on the survivor?** Name, zip, care type, parent.

**4. What's left over?** Accounts under the Bellhaven parent that no website location claimed.

### The billing SOP

Any time an account needs a different parent, the code checks `lifetime_revenue` and `outstanding_ar` first (`must_preserve_for_billing` in `matcher.py`):

| Revenue history | Outstanding AR | What happens |
|---|---|---|
| yes | > 0 | **Parent is not touched.** A new account is created under the correct parent and `chow_current_account` on the old one points to it. Nothing else on the old account changes. |
| no, or | 0 | Re-parent the existing account directly. |

The check runs twice: when the proposal is built, and again against live numbers at the moment someone clicks Approve. If AR appeared in between, the approval is refused and the next run produces a CHOW proposal instead.

## What it found on the current data

35 website locations (34 in the paginated directory plus one that is only linked from the homepage), 121 CRM accounts. 21 locations matched with nothing material to change. 26 proposals:

| Type | # | Which |
|---|---|---|
| CHOW, new account (SOP) | 2 | Tiffin and Marietta: under Cedar Trail, both have revenue and outstanding AR |
| CHOW, link to successor (SOP) | 1 | Sandusky: gone from the website, Millstone already has an account at that address, and the Bellhaven one has revenue and AR |
| Wrong parent, move directly | 3 | Lima (Harborview, AR is $0), Zanesville (Cedar Trail, no revenue, also renamed), Findlay (had no parent) |
| Field fix | 4 | Outdated names in Chagrin Falls, Portage and Chesterton; a transposed zip in Portsmouth |
| New location | 4 | Batavia, Carlisle PA, Union Square, Amberly Manor (Hudson OH) |
| Duplicate | 5 | Owosso (exact twin), Monroe x2, Port Clinton, Erie (leftover Harborview / Cedar Trail records) |
| Ambiguous | 1 | Kettering: three accounts at the address, none of them Bellhaven, nothing to separate them |
| Not on website | 2 | Coldwater and Alliance |
| Name formatting only | 4 | e.g. "Rehab and Nursing" vs "Rehabilitation & Nursing". Optional. |

### Judgment calls

- **Kettering is flagged, not resolved.** Three records share the address (Harborview, Cedar Trail, no parent). None has the website's name, phone or administrator, and none has billing history. Picking a survivor would be a coin flip that looks like a decision, so all three go to `Needs Review` with a note listing the other two. Once a person marks two of them as duplicates, the next run picks up the remaining one and proposes the re-parent and rename on its own.
- **Coldwater and Alliance get `Needs Review`, not `Inactive`.** They dropped off the website but there is no evidence of who owns them now (no other account at those addresses). The facility probably still exists and could still be a customer, so I flag it for a rep and leave the parent alone instead of guessing.
- **Sandusky links to the existing Millstone account** instead of creating a new one. The SOP says create a new account under the correct parent, but one already exists at that address, and a second one would just be a duplicate.
- **Ashtabula's PO Box is left alone.** The field is a billing address, and a PO Box is a valid one. It still matches confidently on name, zip, phone and administrator.
- **Phone and administrator are evidence, not sync targets.** They differ between the website and CRM on plenty of solid matches (front desk vs billing line, staff turnover), so the pipeline uses them to support a match and never overwrites them.
- **Care type:** the site lists offerings ("Memory Support", "Short-Term Rehabilitation & Nursing"), which map to the CRM vocabulary in `config.CARE_TYPE_MAP`. A CRM value is accepted if it matches any of a location's offerings.
- **Formatting-only name differences** are matched as the same name and offered as a separate, optional group. Turn them off with `PROPOSE_COSMETIC_NAME_SYNC = False`.

## Re-runs are safe

Two things make a second run quiet:

1. **Proposals come from current state.** Once a fix is approved, the CRM matches the website, so there is nothing to propose. Accounts with `duplicate_of_account` or `chow_current_account` set are treated as resolved and left out of matching, and already-flagged accounts are not flagged again.
2. **Decisions are remembered.** Each proposal has a fingerprint (kind + record + exact change, no dates or note text). `store.py` never re-queues a fingerprint that was approved or rejected. If the facts change, the fingerprint changes, so a rejected rename does not block a different fix later.

Other guards:

- Approval re-reads the record first. If it no longer looks like it did when the proposal was made, nothing is written and the proposal is marked stale.
- Every write is read back and checked. A create first looks for the account it is about to create, so retrying after a network error cannot produce two.
- If a scrape returns less than half of the previous run's locations, the run aborts. Otherwise one bad deploy of the website would flag half the portfolio as "left Bellhaven".
- The scraper follows the "Next" link instead of counting pages (asking for a page past the end returns the last page again), and compares its counts with what the site says about itself.

## Schedule

`crontab.example` has the entry I'd use: 06:15 daily, on the same machine as the review app so both share `data/sync.db`. The run exits non-zero on failure so cron mail (or any monitor) picks it up. For a hosted scheduler the only real change is moving the SQLite file to a shared database, because that file is what remembers decisions.

## Code map

| File | What it does |
|---|---|
| `run_pipeline.py` | Daily entry point |
| `review_app.py`, `templates/queue.html` | Review queue (Flask) |
| `bellhaven_sync/scraper.py` | Crawl directory + homepage, parse detail pages |
| `bellhaven_sync/normalize.py` | Street, name, phone, zip normalisation |
| `bellhaven_sync/matcher.py` | Matching, classification, SOP, builds proposals with evidence |
| `bellhaven_sync/store.py` | SQLite: proposals, decisions, run history |
| `bellhaven_sync/apply.py` | Writes an approved proposal (re-validate, write, read back) |
| `bellhaven_sync/crm.py` | CRM API client |
| `bellhaven_sync/sandbox.py`, `fixtures/` | Offline website + fake CRM for tests and rehearsal |
| `bellhaven_sync/config.py` | Settings: target parent, care-type map, thresholds |
| `tests/` | One test per kind of mess in the data, plus approve / reject / re-run |
