import json
import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class OverseasCountry:
    canonical_iso2: str
    iso2: str
    display_name: str

class OverseasCountryRepository:
    def __init__(self):
        self._mappings = {}
        self._load_mappings()

    def _load_mappings(self):
        json_path = os.path.join(os.path.dirname(__file__), "countries.json")
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            for iso2, entry in data.items():
                self._mappings[iso2] = OverseasCountry(
                    canonical_iso2=entry["canonical_iso2"],
                    iso2=entry["iso2"],
                    display_name=entry["display_name"]
                )
        except Exception as e:
            print(f"Failed to load Overseas countries.json: {e}")

    def get_country_data(self, iso2_code: str) -> Optional[OverseasCountry]:
        return self._mappings.get(iso2_code.upper())

overseas_country_repository = OverseasCountryRepository()
