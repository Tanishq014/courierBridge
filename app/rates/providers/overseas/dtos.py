from typing import List, Optional, TypedDict

class OverseasPackageDict(TypedDict):
    PcsBoxNo: int
    ActualWt: float
    Length: float
    Width: float
    Height: float
    VolumetricWt: float
    ChargeableWt: float

class OverseasQuoteRequestDict(TypedDict):
    ShpCode: Optional[str]
    ISOCode: str
    DestinationName: str
    Pincode: Optional[str]
    City: Optional[str]
    State: Optional[str]
    ShipmentMode: str
    Pcs: int
    ChgWeight: float
    Date: str
    PiecesDetailsTable: List[OverseasPackageDict]
