import os
import httpx
import logging
import asyncio

logger = logging.getLogger(__name__)

class MawwAuthService:
    def __init__(self):
        self._cached_cookie: str = None
        self._login_lock = asyncio.Lock()

    async def login(self) -> str:
        """
        Automates the web login flow for MAWW Logistics to retrieve session cookies.
        """
        username = os.environ.get("MAWW_USERNAME")
        password = os.environ.get("MAWW_PASSWORD")
        company_sef = os.environ.get("MAWW_COMPANY_SEF", "ma-logsitics-delhi")
        company_id = os.environ.get("MAWW_COMPANY_ID", "109")
        
        if not username or not password:
            raise ValueError("MAWW_USERNAME and MAWW_PASSWORD must be set in .env")

        logger.info("Attempting automated login for MAWW Logistics...")
        
        async with httpx.AsyncClient() as client:
            try:
                # 1. Post to login
                payload = {
                    'company_url': '',
                    'company_sef': company_sef,
                    'company_id': company_id,
                    'username': username,
                    'password': password
                }
                
                res = await client.post(
                    'https://online.mawwl.in/login/user_login/uvf_login',
                    data=payload,
                    follow_redirects=False,
                    timeout=10.0
                )
                
                # Check cookies
                cookies = client.cookies
                if 'ci_sessions' not in cookies:
                    raise RuntimeError("Login failed: No ci_sessions cookie returned from uvf_login")
                
                # 2. GET show_form
                await client.get('https://online.mawwl.in/adminx/show_form', timeout=10.0)
                
                # 3. GET login_redirect
                await client.get('https://online.mawwl.in/adminx/login_redirect', timeout=10.0)
                
                # Build cookie header
                cookie_str = "; ".join([f"{k}={v}" for k, v in client.cookies.items()])
                logger.info("Successfully retrieved MAWW session cookies.")
                return cookie_str
                
            except Exception as e:
                logger.error(f"MAWW automated login failed: {e}")
                raise RuntimeError(f"Failed to authenticate with MAWW Logistics: {e}")

    async def get_session_cookie(self) -> str:
        if self._cached_cookie:
            return self._cached_cookie
            
        async with self._login_lock:
            if self._cached_cookie:
                return self._cached_cookie
                
            self._cached_cookie = await self.login()
            return self._cached_cookie

    async def force_refresh(self) -> str:
        """
        Forces a new login and updates the cache.
        """
        async with self._login_lock:
            self._cached_cookie = await self.login()
            return self._cached_cookie

maww_auth_service = MawwAuthService()
