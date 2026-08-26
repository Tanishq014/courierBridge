# ADR 0001: Rate Compare Module Architecture

## Status
Accepted

## Context
CourierBridge is expanding to offer a Rate Compare feature that allows courier operators to instantly quote shipments across multiple providers (e.g., QuickShip, FedEx, DHL, etc.). A core challenge is that every provider has different API contracts, pricing rules, authentication mechanisms, and expected formats for geographical locations (countries, states, zip codes). 

If the application business logic or UI depends directly on a specific provider's data structures, adding new providers in the future will require extensive refactoring and lead to a fragile, tightly coupled system.

## Decision
We have decided to adopt a **Clean Architecture** approach with strict **Provider Isolation**. 

### Canonical Domain Models
Instead of using provider-specific DTOs throughout the application, we introduce **Canonical Domain Models** (`ShipmentRequest`, `ShipmentQuote`, `Package`, `Charge`). 
- **Why?** Provider APIs are highly volatile and diverse. If business logic or the UI depended on QuickShip's payload structure, switching to FedEx would break the app. 
- The UI, core services, and routing layer *only* understand these canonical models. 
- Using canonical models guarantees that the system is completely agnostic to how providers implement their APIs. It enforces a standard interface for all operations, ensuring that the addition of a new provider has zero impact on the core domain logic or frontend implementation.

### Provider Adapters & Provider Isolation
Each external integration (e.g., QuickShip) will be encapsulated in its own dedicated package (adapter) under `app/rates/providers/`.
- **Why Isolation?** This strict separation prevents provider-specific "hacks" or authentication nuances from leaking into the shared codebase. If a provider changes their API version, only their specific adapter needs to be updated.
- **Responsibilities:** Each provider package handles its own HTTP client, authentication, specific DTOs, and mapping logic.
- **Mapping:** Provider-specific mappers are responsible for translating the canonical `ShipmentRequest` into the provider's specific API request, and translating the provider's API response into the canonical `ShipmentQuote`.

### Centralized Location Normalization
We bundle a lightweight, localized ISO country/state dataset within a centralized `app/rates/location` module.
- **Why Centralized?** The application must own the canonical location model (using standard ISO codes like `CA` for Canada). We cannot rely on one provider's specific geographic naming convention as the system standard.
- The UI and domain models will exclusively use these standard ISO codes.
- The provider adapters will consume the centralized module and perform any necessary translation (e.g., mapping `CA` to `CAN` or `Canada` or `124`) based on their specific API requirements.

### Future Provider Integration
- How will future providers (like FedEx, DHL, or Excel rate cards) integrate?
- Future providers will simply be added as a new package under `app/rates/providers/`.
- The developer will implement the `RateProvider` interface, map the inputs/outputs, and register the new provider in `app/rates/providers/registry.py`.
- No changes to `routes.py`, `rate_service.py`, or the frontend will be required.

## Consequences
**Positive:**
- **Extensibility:** Adding a new provider (e.g., FedEx) simply requires creating a new provider package without touching existing business logic.
- **Maintainability:** Provider-specific hacks and data structures will not leak into the UI or core services.
- **Testability:** Providers can be mocked easily by implementing the `RateProvider` interface.

**Negative:**
- **Initial Overhead:** Requires more upfront design and boilerplate (interfaces, mappers, DTOs) compared to a tightly coupled implementation.
- **Data Maintenance:** The lightweight ISO dataset will need to be maintained if new geographical mappings are required by future providers.
