from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class Package(BaseModel):
    length: float = Field(..., gt=0, description="Length of the package in cm")
    width: float = Field(..., gt=0, description="Width of the package in cm")
    height: float = Field(..., gt=0, description="Height of the package in cm")
    weight: float = Field(..., gt=0, description="Weight of the package in kg")

class ShipmentRequest(BaseModel):
    destinationCountry: str = Field(..., description="ISO 3166-1 alpha-2 country code")
    destinationState: str = Field(..., description="ISO 3166-2 state code")
    destinationCity: str = Field(..., description="City name")
    postalCode: str = Field(..., description="Postal or zip code")
    shipmentType: str = Field(..., description="Type of shipment, e.g., 'document', 'parcel'")
    packages: List[Package]
    providers: Optional[List[str]] = Field(None, description="List of provider codes to query. If None or empty, query all providers.")

class Charge(BaseModel):
    name: str
    amount: float
    gst: float = 0.0
    total: float

class ShipmentQuote(BaseModel):
    provider: str = Field(..., description="Display name of the provider")
    providerCode: str = Field(..., description="Internal code of the provider")
    service: str = Field(..., description="Display name of the service")
    serviceCode: str = Field(..., description="Internal code of the service")
    providerQuoteId: Optional[str] = Field(None, description="Quote ID returned by the provider")
    
    currency: str
    totalPrice: float
    gst: float = 0.0
    
    transitEstimate: Optional[str] = None
    chargeableWeight: float
    volumetricWeight: float
    deadWeight: float
    zone: Optional[str] = None
    
    charges: List[Charge] = Field(default_factory=list)
    badges: List[str] = Field(default_factory=list)
    logo: Optional[str] = None
    
    providerMetadata: dict = Field(default_factory=dict, description="Arbitrary provider data needed for booking")
    metadata: dict = Field(default_factory=dict, description="Internal CourierBridge metadata")

class ProviderStatus(BaseModel):
    provider: str
    status: str
    message: Optional[str] = None

class RateCompareResponse(BaseModel):
    quotes: List[ShipmentQuote]
    providers: List[ProviderStatus]
