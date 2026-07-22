import httpx
import logging
from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote
from app.rates.providers.skynet.auth import skynet_auth_service
from app.rates.providers.skynet.mapper import skynet_mapper
from app.rates.providers.skynet.parser import SkyNetParser

logger = logging.getLogger(__name__)

class SkyNetClient:
    def __init__(self):
        self.base_url = "https://skylink.skynetww.com/rate_cal/live_customer_rate_api"
        
    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        try:
            params = skynet_mapper.map_request(request)
            
            # Destination ID is strictly required by SkyNet
            dest_id = next((v for k, v in params if k == "destination_id"), "")
            if not dest_id:
                logger.warning(f"SkyNet mapping failed: Unknown country {request.destinationCountry}")
                return []
                
            cookie_header = await skynet_auth_service.get_session_cookie()
            
            async with httpx.AsyncClient(follow_redirects=False) as client:
                headers = {
                    "Accept": "text/html,application/xhtml+xml,application/xml",
                    "User-Agent": "Mozilla/5.0",
                    "Cookie": cookie_header,
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://skylink.skynetww.com/rate_cal/live_customer_rate_api"
                }
                
                resp = await client.get(self.base_url, params=params, headers=headers)
                
                def is_login_page(html_content, path):
                    html_lower = html_content.lower()
                    if 'id="rate_table"' in html_lower:
                        return False
                    if 'type="password"' in html_lower or 'name="password"' in html_lower or 'name="username"' in html_lower:
                        return True
                    if "user_login" in html_lower or "login" in path.lower():
                        return True
                    return False

                needs_refresh = False
                # Check for session expiration (usually 302 redirect to login)
                if resp.status_code in (301, 302, 307):
                    needs_refresh = True
                else:
                    resp.raise_for_status()
                    html = resp.text
                    if is_login_page(html, resp.url.path):
                        needs_refresh = True
                        
                if needs_refresh:
                    logger.info("SkyNet session expired (redirect or login page). Refreshing session...")
                    new_cookie = await skynet_auth_service.force_refresh(cookie_header)
                    headers["Cookie"] = new_cookie
                    resp = await client.get(self.base_url, params=params, headers=headers)
                    resp.raise_for_status()
                    html = resp.text
                    
                    if is_login_page(html, resp.url.path):
                        raise Exception("SkyNet authentication failed even after session refresh. Check credentials.")

                quotes = SkyNetParser.parse_rates(html, request)
                return quotes
                
        except Exception as e:
            logger.error(f"SkyNet API Error: {e}")
            return []
