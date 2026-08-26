import os
import httpx
import logging
import asyncio
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

class SkyNetAuthService:
    def __init__(self):
        self._cached_cookie: str = None
        self._login_lock = asyncio.Lock()

    async def login(self) -> str:
        """
        Automates the web login flow for SkyNet to retrieve session cookies.
        """
        url_login = "https://skylink.skynetww.com/login/user_login/uvf_login"
        
        async with httpx.AsyncClient(follow_redirects=True) as client:
            logger.info("Authenticating SkyNet session...")
            
            # Retrieve secure credentials from environment
            username = os.environ.get("SKYNET_USERNAME")
            password = os.environ.get("SKYNET_PASSWORD")
            company_sef = os.environ.get("SKYNET_COMPANY_SEF", "skynet")
            company_id = os.environ.get("SKYNET_COMPANY_ID", "3")
            
            if not username or not password:
                raise ValueError("SKYNET_USERNAME and SKYNET_PASSWORD must be set in the environment.")
            
            payload = urlencode({
                "company_url": "",
                "company_sef": company_sef,
                "company_id": company_id,
                "username": username,
                "password": password,
                "encode_password": "1"
            })
            
            headers = {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://skylink.skynetww.com/",
                "Origin": "https://skylink.skynetww.com"
            }
            
            resp_login = await client.post(url_login, content=payload, headers=headers)
            resp_login.raise_for_status()
            
            # Additional cookie (the CI session needs a cookie_type=customer to identify type)
            client.cookies.set("cookie_type", "customer", domain="skylink.skynetww.com")
            
            # Format the cookie header string
            cookie_parts = []
            for name, value in client.cookies.items():
                cookie_parts.append(f"{name}={value}")
                
            cookie_header = "; ".join(cookie_parts)
            logger.info("Successfully automated SkyNet session login.")
            return cookie_header

    async def get_session_cookie(self) -> str:
        if self._cached_cookie:
            return self._cached_cookie
            
        async with self._login_lock:
            if self._cached_cookie:
                return self._cached_cookie
                
            self._cached_cookie = await self.login()
            return self._cached_cookie

    async def force_refresh(self, old_cookie: str) -> str:
        """
        Forces a new login and updates the cache, preventing thundering herd if already refreshed.
        """
        async with self._login_lock:
            if self._cached_cookie and self._cached_cookie != old_cookie:
                return self._cached_cookie
                
            self._cached_cookie = await self.login()
            return self._cached_cookie

skynet_auth_service = SkyNetAuthService()
