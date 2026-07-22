import httpx
import os
import logging
from .dtos import QuickShipQuoteRequest, QuickShipQuoteResponse
from .auth import auth_service

logger = logging.getLogger(__name__)

class QuickShipClient:
    """
    HTTP client abstraction for the QuickShip API.
    Handles network calls and automatic token refresh upon authentication failures.
    """
    def __init__(self):
        self.api_base = os.environ.get("QUICKSHIP_API_BASE", "https://qsapi.quickshipnow.com").rstrip("/")
        
    async def get_quotes(self, request: QuickShipQuoteRequest) -> QuickShipQuoteResponse:
        """
        Calls the QuickShip Quote API.
        Handles token fetching and 401 retries.
        """
        if not self.api_base:
            raise ValueError("QUICKSHIP_API_BASE is not configured.")
            
        endpoint = f"{self.api_base}/rate/calculate"
        
        # Initial attempt
        token = await auth_service.get_token()
        response = await self._post_request(endpoint, request, token)
        
        # If we get a 401 Unauthorized, the token might have been invalidated server-side
        if response.status_code == 401:
            logger.info("QuickShip API returned 401. Refreshing token and retrying once.")
            token = await auth_service.get_token(force_refresh=True)
            response = await self._post_request(endpoint, request, token)
            
        # Raise exception for 4xx and 5xx
        response.raise_for_status()
        
        # Validate and return response
        try:
            return QuickShipQuoteResponse.model_validate(response.json())
        except Exception as e:
            raise ValueError(f"Failed to parse QuickShip response: {str(e)}")

    async def _post_request(self, endpoint: str, request: QuickShipQuoteRequest, token: str) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "token": token
        }
        
        # We do NOT log the JWT token or headers.
        async with httpx.AsyncClient() as client:
            try:
                # Assuming quote API might take some time, setting a 15s timeout
                return await client.post(
                    endpoint, 
                    json=request.model_dump(), 
                    headers=headers,
                    timeout=15.0
                )
            except httpx.TimeoutException:
                raise TimeoutError("QuickShip Quote API timed out.")
            except httpx.RequestError as exc:
                raise RuntimeError(f"Network error while calling QuickShip: {str(exc)}")

    async def get_city_state(self, pincode: str, country_iso3: str) -> dict:
        """
        Resolves the exact city and state names expected by QuickShip from a pincode.
        """
        if not self.api_base:
            return {}
            
        endpoint = f"{self.api_base}/pincode-city-master"
        params = {"pincode": pincode, "country": country_iso3}
        
        try:
            token = await auth_service.get_token()
            headers = {"token": token}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(endpoint, params=params, headers=headers, timeout=5.0)
                
                if response.status_code == 401:
                    token = await auth_service.get_token(force_refresh=True)
                    headers["token"] = token
                    response = await client.get(endpoint, params=params, headers=headers, timeout=5.0)
                    
                response.raise_for_status()
                data = response.json()
                
                if data.get("success") and data.get("data"):
                    return data["data"]
        except Exception as e:
            logger.warning(f"Failed to fetch QuickShip city/state for pincode {pincode}: {e}")
            
        return {}
