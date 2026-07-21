from typing import Dict, Optional

class LocationService:
    """
    Service responsible for providing canonical location data (countries, states).
    
    TODO: Integrate a complete ISO-3166 country/subdivision dataset (or provider-supported 
    location dataset) here. Currently, we accept values from the UI without validation
    to avoid issues with incomplete sample data.
    """
    def __init__(self):
        pass

    def get_countries(self) -> Dict[str, str]:
        """
        Returns an empty dictionary for Phase 1.
        The UI will fallback to text inputs for country selection.
        """
        return {}

    def get_country(self, code: str) -> Optional[str]:
        return code.upper()

    def get_states(self, country_code: str) -> Dict[str, str]:
        return {}

    def get_state(self, country_code: str, state_code: str) -> Optional[str]:
        return state_code.upper()

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
