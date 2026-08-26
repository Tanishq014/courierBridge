import os
import httpx
import logging
import asyncio

logger = logging.getLogger(__name__)

class AtlanticAuthService:
    def __init__(self):
        self._cached_cookie: str = None
        self._login_lock = asyncio.Lock()

    async def login(self) -> str:
        """
        Automates the web login flow for Atlantic Logistics to retrieve session cookies.
        """
        url_home = "https://cloud.atlanticcourier.net/"
        url_login = "https://cloud.atlanticcourier.net/Login/LoginValidate"
        
        # We need both an ASP.NET_SessionId and __RequestVerificationToken.
        # These are usually issued simply by visiting the homepage.
        async with httpx.AsyncClient(follow_redirects=True) as client:
            logger.info("Fetching Atlantic initial cookies...")
            resp_home = await client.get(url_home, headers={"User-Agent": "Mozilla/5.0"})
            resp_home.raise_for_status()
            
            # Extract cookies
            cookies = client.cookies
            
            # Retrieve secure credentials from environment
            username = os.environ.get("ATLANTIC_USERNAME")
            password = os.environ.get("ATLANTIC_PASSWORD")
            
            if not username or not password:
                raise ValueError("ATLANTIC_USERNAME and ATLANTIC_PASSWORD must be set in the environment.")
            
            # Now POST the login payload using those exact cookies
            payload = f"UserType=&UserName={username}&Password={password}&mobileNumber=&OTP=remail&code=&code=&code=&code=&submit=Login"
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "Mozilla/5.0",
                "Referer": url_home,
                "X-Requested-With": "XMLHttpRequest"
            }
            
            logger.info("Authenticating Atlantic session...")
            resp_login = await client.post(url_login, content=payload, headers=headers)
            resp_login.raise_for_status()
            
            # Format the cookie header string
            cookie_parts = []
            for name, value in client.cookies.items():
                cookie_parts.append(f"{name}={value}")
                
            cookie_header = "; ".join(cookie_parts)
            logger.info("Successfully automated Atlantic session login.")
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

atlantic_auth_service = AtlanticAuthService()
