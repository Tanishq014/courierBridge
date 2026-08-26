import os
import time
import json
import base64
import httpx
import logging

logger = logging.getLogger(__name__)

class QuickShipAuthService:
    def __init__(self):
        self.api_base = os.environ.get("QUICKSHIP_API_BASE", "https://qsapi.quickshipnow.com").rstrip("/")
        self.email = os.environ.get("QUICKSHIP_EMAIL", "")
        self.password = os.environ.get("QUICKSHIP_PASSWORD", "")
        
        self._cached_token = None
        self._token_expiry = 0
        
        # We want to refresh the token if it expires within 5 minutes (300 seconds)
        self.EXPIRY_BUFFER_SECONDS = 300

    async def get_token(self, force_refresh: bool = False) -> str:
        """
        Returns a valid JWT token.
        If the token is missing, expired, or about to expire, it logs in again.
        """
        current_time = time.time()
        
        if force_refresh or not self._cached_token or (self._token_expiry - current_time < self.EXPIRY_BUFFER_SECONDS):
            await self._login()
            
        return self._cached_token

    async def _login(self):
        if not self.email or not self.password:
            raise ValueError("QuickShip credentials (email/password) are not configured.")
            
        login_url = f"{self.api_base}/customer/login"
        payload = {
            "email": self.email,
            "password": self.password
        }
        
        # Using a fresh client for the login request
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(login_url, json=payload, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                
                if data.get("success") is not True:
                    raise ValueError("Authentication unsuccessful (success flag is false).")
                
                token = None
                resp_data = data.get("data")
                if isinstance(resp_data, dict):
                    token = resp_data.get("token")
                
                if not token:
                    raise ValueError("No token found in QuickShip login response.")
                    
                self._cached_token = token
                self._token_expiry = self._decode_jwt_expiry(token)
                
            except Exception:
                # Do not log the password, JWT or response body
                logger.error("QuickShip authentication failed.")
                raise RuntimeError("QuickShip authentication failed.")

    def _decode_jwt_expiry(self, token: str) -> float:
        """
        Decodes the JWT payload to extract the 'exp' claim.
        Does not verify the signature.
        """
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return 0
                
            # Pad base64 if necessary
            payload_b64 = parts[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
            payload = json.loads(payload_json)
            
            return float(payload.get("exp", 0))
        except Exception:
            # If we can't parse it, assume it's expired so it gets refreshed sooner rather than later
            return 0

# Singleton instance
auth_service = QuickShipAuthService()
