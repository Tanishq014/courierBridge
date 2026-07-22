import httpx
import logging
import json
from typing import List
from urllib.parse import urlencode

from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.maww.auth import maww_auth_service
from app.rates.providers.maww.mapper import MawwMapper
from app.rates.providers.maww.parser import MawwParser

logger = logging.getLogger(__name__)

class MawwClient:
    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        payload_dict = MawwMapper.to_provider_request(request)
        payload_str = urlencode(payload_dict)
        url = f"https://online.mawwl.in/rate_cal/live_customer_rate?{payload_str}"
        
        cookie_header = await maww_auth_service.get_session_cookie()
        
        async with httpx.AsyncClient(follow_redirects=False) as client:
            try:
                response = await self._fetch(client, url, cookie_header)
                
                # Check for redirect to login (session expiration)
                is_redirect = response.status_code in (301, 302)
                location = response.headers.get("location", "").lower()
                
                if is_redirect and "login" in location:
                    logger.warning("MAWW cookie expired (redirect detected). Forcing refresh...")
                    cookie_header = await maww_auth_service.force_refresh()
                    response = await self._fetch(client, url, cookie_header)
                    
                response.raise_for_status()
                return MawwParser.parse_rates(response.text)
                
            except httpx.HTTPStatusError as e:
                logger.error(f"MAWW HTTP error: {e.response.status_code}")
                raise RuntimeError(f"MAWW API Error: {e.response.status_code}")
            except Exception as e:
                logger.error(f"MAWW Fetch error: {e}")
                raise

    async def _fetch(self, client: httpx.AsyncClient, url: str, cookie_header: str):
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Cookie": cookie_header,
            "Referer": url, # Setting referer to same URL as requested
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
        }
        return await client.get(url, headers=headers, timeout=15.0)
