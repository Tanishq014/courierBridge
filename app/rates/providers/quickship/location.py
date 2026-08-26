import os
import json
from typing import Dict, List, Optional

class QuickShipLocationService:
    def __init__(self):
        self._states_cache = None
        self._countries_cache = None
        
        self.resources_dir = os.path.join(os.path.dirname(__file__), "resources")
        self.states_file = os.path.join(self.resources_dir, "states.json")
        self.countries_file = os.path.join(self.resources_dir, "countries.json")

    def _load_states(self) -> Dict[str, List[Dict[str, str]]]:
        if self._states_cache is None:
            if os.path.exists(self.states_file):
                with open(self.states_file, "r") as f:
                    self._states_cache = json.load(f)
            else:
                self._states_cache = {}
        return self._states_cache
        
    def _load_countries(self) -> Dict[str, str]:
        if self._countries_cache is None:
            self._countries_cache = {}
            if os.path.exists(self.countries_file):
                with open(self.countries_file, "r", encoding="utf-8") as f:
                    try:
                        raw_data = json.load(f)
                    except json.JSONDecodeError:
                        raw_data = {}
                
                # Support simple key-value dictionary
                if isinstance(raw_data, dict) and "data" not in raw_data and not any(isinstance(v, dict) for v in raw_data.values()):
                    self._countries_cache = raw_data
                else:
                    # Support QuickShip API payload format {"success": true, "data": [...]} or [...]
                    items = raw_data.get("data", []) if isinstance(raw_data, dict) else raw_data
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict) and "iso2Code" in item and "iso3Code" in item:
                                self._countries_cache[item["iso2Code"].upper()] = item["iso3Code"].upper()
        return self._countries_cache

    def get_countries(self) -> Dict[str, str]:
        return self._load_countries()

    def get_country_code(self, country_iso2: str) -> str:
        """Map generic country code to QuickShip country code."""
        countries = self._load_countries()
        return countries.get(country_iso2.upper(), country_iso2.upper())

    def get_states(self, country_code: str) -> List[Dict[str, str]]:
        """Returns the QuickShip states list for a given QuickShip country code."""
        states = self._load_states()
        return states.get(country_code.upper(), [])

    def get_state_code(self, country_code: str, state_name: str) -> str:
        """Finds the provider-specific state abbreviation for the given state name."""
        if not state_name:
            return ""
            
        states_list = self.get_states(country_code)
        
        # Case insensitive match on name
        target = state_name.strip().lower()
        for state in states_list:
            if state["name"].lower() == target:
                return state["abbreviation"]
                
        # Fallback to the provided name if not found
        return state_name

    def get_state_name(self, country_code: str, state_code: str) -> str:
        """Finds the provider-specific state name for a given abbreviation."""
        if not state_code:
            return ""
            
        states_list = self.get_states(country_code)
        
        target = state_code.strip().lower()
        for state in states_list:
            if state["abbreviation"].lower() == target:
                return state["name"]
                
        return state_code

# Singleton instance for the provider
quickship_location_service = QuickShipLocationService()
