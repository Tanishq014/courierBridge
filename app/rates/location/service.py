import json
import os
from typing import Dict, Optional

class LocationService:
    """
    Service responsible for providing canonical location data (countries, states).
    """
    def __init__(self):
        self._countries_cache = None
        self._states_cache = None
        self.countries_file = os.path.join(os.path.dirname(__file__), "resources", "countries.json")
        self.states_file = os.path.join(os.path.dirname(__file__), "resources", "states.json")

    def get_countries(self) -> Dict[str, str]:
        """
        Returns a dictionary of ISO-2 country codes to country names.
        """
        if os.path.exists(self.countries_file):
            with open(self.countries_file, "r", encoding="utf-8") as f:
                # Sort alphabetically by country name for the dropdown
                raw_countries = json.load(f)
                return dict(sorted(raw_countries.items(), key=lambda item: item[1]))
        return {}

    def get_country(self, code: str) -> Optional[str]:
        return self.get_countries().get(code.upper(), code.upper())

    def _load_states(self) -> Dict[str, Dict[str, str]]:
        if os.path.exists(self.states_file):
            with open(self.states_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def get_states(self, country_code: str) -> Dict[str, str]:
        """
        Returns a dictionary of state codes to state names for a given ISO-2 country code.
        """
        return self._load_states().get(country_code.upper(), {})

    def get_state(self, country_code: str, state_code: str) -> Optional[str]:
        states = self.get_states(country_code)
        return states.get(state_code.upper(), state_code.upper())

    def validate_country(self, code: str) -> bool:
        """Always returns True in Phase 1 (no validation)."""
        return True

    def validate_state(self, country_code: str, state_code: str) -> bool:
        """Always returns True in Phase 1 (no validation)."""
        return True

    def search_country(self, query: str) -> Dict[str, str]:
        return {}

    def search_state(self, country_code: str, query: str) -> Dict[str, str]:
        return {}

# Singleton instance
location_service = LocationService()
