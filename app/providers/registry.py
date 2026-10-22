from typing import Dict, Any, Optional, List, Tuple
from app.providers.base import MessagingProvider
from app.providers.twilio_provider import TwilioProvider
from app.providers.infobip_provider import InfobipProvider
from app.providers.mock_provider import MockAggregatorProvider

class ProviderRegistry:
    def __init__(self):
        self._providers: Dict[str, MessagingProvider] = {}
        # Pre-register built-in providers
        self.register(MockAggregatorProvider())
        self.register(TwilioProvider())
        self.register(InfobipProvider())

    def register(self, provider: MessagingProvider) -> None:
        self._providers[provider.provider_id.lower()] = provider

    def get_provider(self, provider_id: str) -> MessagingProvider:
        pid = (provider_id or "").lower().strip()
        if pid not in self._providers:
            available = ", ".join(sorted(self._providers.keys()))
            raise ValueError(f"Unknown messaging provider '{provider_id}'. Available providers: {available}")
        return self._providers[pid]

    def list_providers(self) -> List[Dict[str, str]]:
        return [
            {"id": p.provider_id, "name": p.display_name}
            for p in self._providers.values()
        ]

    def get_provider_for_phone(self, phone: str, storage=None) -> Tuple[MessagingProvider, Dict[str, Any]]:
        """
        Determines the appropriate provider and configuration for a given phone number.
        Reads phone configuration from storage.
        """
        if storage is None:
            from app.core.storage import get_storage
            storage = get_storage()

        phone_config = storage.get_phone_configuration(phone)
        if phone_config and phone_config.get("provider_id"):
            provider_id = phone_config["provider_id"]
            provider = self.get_provider(provider_id)
            config_data = phone_config.get("config", {})
            if phone_config.get("endpoint_url"):
                config_data["endpoint_url"] = phone_config["endpoint_url"]
            return provider, config_data

        # Fallback to Mock provider for unconfigured numbers or local development
        default_provider = self.get_provider("mock")
        return default_provider, {}

_registry_instance: Optional[ProviderRegistry] = None

def get_provider_registry() -> ProviderRegistry:
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = ProviderRegistry()
    return _registry_instance
