from pydantic import BaseModel, Field
from typing import List, Optional, Any

# --- Request Models ---

class QuickShipPackage(BaseModel):
    packageLength: str
    packageWidth: str
    packageHeight: str
    packageWeight: str

class QuickShipQuoteRequest(BaseModel):
    originCountry: str
    country: str
    pincode: str
    city: str
    state: str
    stateCode: str
    doxSpx: int
    packages: List[QuickShipPackage]

# --- Response Models ---

class QuickShipCharge(BaseModel):
    chargeName: str
    amount: float
    gstAmount: float
    totalAmountIncGst: float
    chargeCode: str

class QuickShipQuoteData(BaseModel):
    shippingServiceId: int
    carrierId: int
    rateCurrency: str
    shippingServiceName: str
    totalAmount: float
    totalAmountWithGst: float
    totalGstAmount: float
    charges: List[QuickShipCharge]
    basePrice: QuickShipCharge
    transitDay: Optional[str] = None
    chargeableWeight: float
    volumetricWeight: float
    deadWeight: float
    pickupType: int
    logo: Optional[str] = None
    serviceCode: Optional[str] = None
    lowestPriceService: Optional[str] = None
    zone: Optional[str] = None
    showExtendedAreaButton: bool

class QuickShipQuoteResponse(BaseModel):
    success: bool
    data: List[QuickShipQuoteData] = []
