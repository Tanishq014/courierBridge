import json
import os
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)

@dataclass
class MawwCountry:
    id: int
    display_name: str

class MawwLocationRepository:
    def __init__(self):
        self._mappings = {}
        self._load_mappings()

    def _load_mappings(self):
        json_path = os.path.join(os.path.dirname(__file__), "data", "countries.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            for iso2, entry in data.items():
                self._mappings[iso2.upper()] = MawwCountry(
                    id=entry["id"],
                    display_name=entry["display_name"]
                )
            logger.info(f"Loaded {len(self._mappings)} MAWW country mappings.")
        except Exception as e:
            logger.error(f"Failed to load MAWW countries.json: {e}")

    def get_country_data(self, iso2: str) -> Optional[MawwCountry]:
        if not iso2:
            return None
        return self._mappings.get(iso2.upper())

maww_location_repository = MawwLocationRepository()
