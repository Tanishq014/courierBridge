import httpx
import logging
from typing import List

from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.atlantic.auth import atlantic_auth_service
from app.rates.providers.atlantic.mapper import AtlanticMapper
from app.rates.providers.atlantic.parser import AtlanticParser

logger = logging.getLogger(__name__)

class AtlanticClient:
    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        payload_dict = AtlanticMapper.to_provider_request(request)
        url = "https://cloud.atlanticcourier.net/CustomerRateCompare/GetCustomerRate"
        
        cookie_header = await atlantic_auth_service.get_session_cookie()
        
        async with httpx.AsyncClient(follow_redirects=False) as client:
            try:
                response = await self._fetch(client, url, payload_dict, cookie_header)
                
                # Check for redirect to login (session expiration)
                is_redirect = response.status_code in (301, 302)
                location = response.headers.get("location", "").lower()
                
                # If they intercept our JSON request and try to redirect to HTML
                if is_redirect or (response.status_code == 200 and "text/html" in response.headers.get("Content-Type", "").lower()):
                    logger.warning("Atlantic cookie expired. Forcing refresh...")
                    cookie_header = await atlantic_auth_service.force_refresh(old_cookie=cookie_header)
                    response = await self._fetch(client, url, payload_dict, cookie_header)
                    
                response.raise_for_status()
                return AtlanticParser.parse_rates(response.json(), request)
                
            except httpx.HTTPStatusError as e:
                logger.error(f"Atlantic HTTP error: {e.response.status_code}")
                raise RuntimeError(f"Atlantic API Error: {e.response.status_code}")
            except Exception as e:
                logger.error(f"Atlantic Fetch error: {e}")
                raise

    async def _fetch(self, client: httpx.AsyncClient, url: str, payload_dict: dict, cookie_header: str):
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json; charset=UTF-8",
            "Cookie": cookie_header,
            "Referer": "https://cloud.atlanticcourier.net/CustomerRateCompare/CustomerRateCompare",
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
        }
        return await client.post(url, json=payload_dict, headers=headers, timeout=15.0)
