"""All settings in one place. Values come from the environment (or a local .env file)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env reader so the project has no extra dependency. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

# --- where things live -------------------------------------------------------
BASE_URL = os.getenv("BELLHAVEN_BASE_URL", "https://analyst-assessment-production.up.railway.app").rstrip("/")
API_BASE = BASE_URL + "/api/v1"
API_TOKEN = os.getenv("CRM_API_TOKEN", "")

# Offline rehearsal mode: fixture website + a fake CRM saved to a local JSON file.
# Nothing touches the real CRM, so it is safe to practise the review flow.
OFFLINE = os.getenv("BELLHAVEN_OFFLINE", "") == "1"

DATA_DIR = Path(os.getenv("BELLHAVEN_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / ("offline_sync.db" if OFFLINE else "sync.db")
OFFLINE_CRM_PATH = DATA_DIR / "offline_crm.json"
FIXTURES_DIR = ROOT / "fixtures"

# --- business rules ----------------------------------------------------------
# The operator we keep in sync. The parent account id is looked up by name at run
# time, so nothing here is tied to one copy of the CRM.
TARGET_PARENT_NAME = "Bellhaven Senior Living"

# Website care offering -> CRM care_type vocabulary.
CARE_TYPE_MAP = {
    "Assisted Living": "Assisted Living",
    "Memory Support": "Memory Care",
    "Short-Term Rehabilitation & Nursing": "Skilled Nursing",
    "Independent Living": "Independent Living",
}

# When a CRM name differs from the website only in formatting ("Rehab and" vs "Rehabilitation &"),
# offer a low-priority rename so the CRM can mirror the operator's own spelling. The match itself
# is already confident either way. Set to False to keep these out of the review queue.
PROPOSE_COSMETIC_NAME_SYNC = True

STATUS_ACTIVE = "Active"
STATUS_INACTIVE = "Inactive"
STATUS_NEEDS_REVIEW = "Needs Review"

# Safety net for the daily run: if the scrape suddenly returns fewer than this share
# of the previous run's locations, assume the site is broken and stop before
# proposing that half the portfolio "left Bellhaven".
MIN_LOCATION_RATIO = 0.5

HTTP_TIMEOUT = 20
USER_AGENT = "bellhaven-sync/1.0 (CRM ownership hygiene)"
