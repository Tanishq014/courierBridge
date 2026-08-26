import os
import httpx
import logging
from bs4 import BeautifulSoup
import asyncio

logger = logging.getLogger(__name__)

class OverseasAuthService:
    def __init__(self):
        self._cached_cookie: str = None
        self._login_lock = asyncio.Lock()

    async def login(self) -> str:
        """
        Automates the web login flow for app.overseaslogistic.com to retrieve session cookies.
        """
        username = os.environ.get("OVERSEAS_USERNAME")
        password = os.environ.get("OVERSEAS_PASSWORD")
        
        if not username or not password:
            raise ValueError("OVERSEAS_USERNAME and OVERSEAS_PASSWORD must be set in .env")

        logger.info("Attempting automated login for Overseas Logistics...")
        
        async with httpx.AsyncClient() as client:
            try:
                # 1. Fetch the login page to get the Antiforgery Token
                res = await client.get('https://app.overseaslogistic.com/account/login', timeout=10.0)
                res.raise_for_status()
                
                soup = BeautifulSoup(res.text, 'html.parser')
                token_input = soup.find('input', {'name': '__RequestVerificationToken'})
                
                if not token_input:
                    raise RuntimeError("Failed to find CSRF token on login page")
                    
                csrf_token = token_input.get('value')
                
                # 2. Submit the login form
                payload = {
                    'Userid': username,
                    'Password': password,
                    '__RequestVerificationToken': csrf_token,
                    'KeepLoggedIn': 'false'
                }
                
                res2 = await client.post(
                    'https://app.overseaslogistic.com/account/login',
                    data=payload,
                    follow_redirects=False,
                    timeout=10.0
                )
                
                # A successful login returns 302 Found. 
                # If it returns 200 OK, it means the login failed and we got the form back.
                if res2.status_code == 200:
                    raise RuntimeError("Invalid credentials for Overseas Logistics Web Login")
                    
                # 3. Extract and cache all cookies
                cookie_parts = []
                for name, value in client.cookies.items():
                    cookie_parts.append(f"{name}={value}")
                    
                self._cached_cookie = "; ".join(cookie_parts)
                logger.info("Successfully refreshed Overseas session cookies!")
                return self._cached_cookie
                
            except Exception as e:
                logger.error(f"Overseas automated login failed: {e}")
                raise

    async def get_session_cookie(self) -> str:
        """
        Returns the cached Cookie header. If none exists, performs login.
        Thread-safe to prevent multiple concurrent logins.
        """
        if self._cached_cookie:
            return self._cached_cookie
            
        async with self._login_lock:
            if not self._cached_cookie:
                await self.login()
            return self._cached_cookie
            
    async def force_refresh(self) -> str:
        """
        Forces a new login to bypass an expired cached cookie.
        """
        async with self._login_lock:
            return await self.login()

overseas_auth_service = OverseasAuthService()
