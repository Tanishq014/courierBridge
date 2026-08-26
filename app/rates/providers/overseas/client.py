import httpx
import logging
from .auth import overseas_auth_service
from .dtos import OverseasQuoteRequestDict

logger = logging.getLogger(__name__)

class OverseasClient:
    def __init__(self):
        self.api_base = "https://app.overseaslogistic.com"

    async def get_rates(self, payload: OverseasQuoteRequestDict) -> str:
        url = f"{self.api_base}/rate/calculator"
        
        async with httpx.AsyncClient(follow_redirects=False) as client:
            try:
                cookie_header = await overseas_auth_service.get_session_cookie()
                response = await self._fetch(client, url, payload, cookie_header)
                
                # Detect expired session: 401 Unauthorized or 302 Redirect to login
                is_unauthorized = response.status_code == 401
                is_redirect = response.status_code in (301, 302)
                location = response.headers.get("location", "").lower()
                
                if is_unauthorized or (is_redirect and "login" in location):
                    logger.warning("Overseas cookie expired (401/redirect detected). Forcing refresh...")
                    cookie_header = await overseas_auth_service.force_refresh()
                    response = await self._fetch(client, url, payload, cookie_header)
                
                response.raise_for_status()
                return response.text
                
            except Exception as e:
                logger.error(f"Overseas API failed: {e}")
                raise RuntimeError(f"Failed to fetch rates from Overseas: {str(e)}")

    async def _fetch(self, client: httpx.AsyncClient, url: str, payload: dict, cookie_header: str) -> httpx.Response:
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "cookie": cookie_header,
            "origin": self.api_base,
            "referer": f"{self.api_base}/rate/calculator",
            "x-requested-with": "XMLHttpRequest"
        }
        
        return await client.post(url, json=payload, headers=headers, timeout=15.0)
