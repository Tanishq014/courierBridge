import httpx
import os
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ShipglobalClient:
    """
    HTTP client abstraction for the Shipglobal API.
    """
    def __init__(self):
        self.api_base = "https://api.app.shipglobal.in/api/v1"
        
    async def get_quotes(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calls the Shipglobal get-shipper-rates API.
        """
        # Fetch dynamically so token updates take effect without a server restart
        raw_token = os.environ.get(
            "SHIPGLOBAL_API_TOKEN",
            "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJlbnRpdHlJZCI6NTkxMjQsImNyZWF0ZWRfYXQiOnsiZGF0ZSI6IjIwMjYtMDgtMTIgMDI6MDI6MzUuNzczMTk0IiwidGltZXpvbmVfdHlwZSI6MywidGltZXpvbmUiOiJBc2lhL0tvbGthdGEifSwiZXhwaXJlc19hdCI6eyJkYXRlIjoiMjAyNi0wOS0xMSAwMjowMjozNS43NzMxOTUiLCJ0aW1lem9uZV90eXBlIjozLCJ0aW1lem9uZSI6IkFzaWEvS29sa2F0YSJ9LCJpZCI6IjZhZDgyZjU4LThiNGUtNDAyOS1iMGRhLTViZTk1NWUyNzAyNCIsInJlbW90ZV9lbnRpdHlfaWQiOjB9.LZodsoF6wXk4lO-xhcR3iL_vznEjSSeay9O_KZ-Pg6Q"
        )
        self.token = raw_token[7:].strip() if raw_token.lower().startswith("bearer ") else raw_token.strip()
        
        endpoint = f"{self.api_base}/orders/get-shipper-rates"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Origin": "https://v2.app.shipglobal.in",
            "Referer": "https://v2.app.shipglobal.in/"
        }
        
        cookies = {
            "vendor_cookie": self.token
        }
        
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(endpoint, json=payload, headers=headers, cookies=cookies, timeout=15.0)
                
                # Shipglobal returns 400 with "No Rates Found" when a route is unsupported
                if response.status_code == 400:
                    try:
                        data = response.json()
                        if data.get("message") == "No Rates Found":
                            return {"data": {"rate": []}}
                    except Exception:
                        pass
                        
                response.raise_for_status()
                data = response.json()
                
                # If API returns an empty list or dict for data, we assume no rates
                if not data.get("data"):
                    return {"data": {"rate": []}}
                    
                if "rate" not in data["data"]:
                    logger.error(f"Shipglobal returned unexpected structure: {data}")
                    raise ValueError(f"Unexpected API response structure from Shipglobal: {data.get('message', 'Unknown error')}")
                    
                return data
            except httpx.HTTPStatusError as e:
                logger.error(f"Shipglobal API error: {e.response.text}")
                raise ValueError(f"Shipglobal API returned {e.response.status_code}")
            except Exception as e:
                logger.error(f"Shipglobal connection error: {e}")
                raise
