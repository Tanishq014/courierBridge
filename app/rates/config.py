import os
from pydantic import BaseModel

class RatesConfig(BaseModel):
    # Overall configuration for the rates module could go here
    # e.g., default timeout, retry policies
    default_timeout_seconds: int = 10

config = RatesConfig()
