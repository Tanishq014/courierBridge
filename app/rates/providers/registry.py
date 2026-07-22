from typing import List, Dict
from app.rates.providers.base import RateProvider
from app.rates.providers.quickship.provider import QuickShipProvider
from app.rates.providers.overseas.provider import OverseasProvider
from app.rates.providers.maww.provider import MawwProvider
from app.rates.providers.atlantic.provider import AtlanticProvider
from app.rates.providers.skynet.provider import SkyNetProvider

class ProviderRegistry:
    # TODO: Provider registration should eventually become configuration-driven.
    # This will allow operators to enable or disable providers without modifying code.
    def __init__(self):
        self._providers: Dict[str, RateProvider] = {}
        # Register available providers
        self.register(QuickShipProvider())
        self.register(OverseasProvider())
        self.register(MawwProvider())
        self.register(AtlanticProvider())
        self.register(SkyNetProvider())

    def register(self, provider: RateProvider):
        """Registers a new provider."""
        self._providers[provider.provider_code] = provider

    def get_provider(self, code: str) -> RateProvider:
        """Gets a provider by code."""
        if code not in self._providers:
            raise ValueError(f"Provider '{code}' not found.")
        return self._providers[code]

    def get_all_providers(self) -> List[RateProvider]:
        """Gets all registered providers."""
        return list(self._providers.values())

# Global registry instance
provider_registry = ProviderRegistry()
