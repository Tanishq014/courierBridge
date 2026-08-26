from app.rates.providers.registry import provider_registry
from app.rates.providers.quickship.provider import QuickShipProvider

def register_all_providers():
    """
    Registers all available rate providers.
    In the future, this could be data-driven or based on environment variables.
    """
    provider_registry.register(QuickShipProvider())

# Initialize the registry with the active providers
register_all_providers()
