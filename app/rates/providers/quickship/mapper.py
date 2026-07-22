import os
from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote, Package, Charge
from .dtos import QuickShipQuoteRequest, QuickShipQuoteResponse, QuickShipPackage
from .location import quickship_location_service

class QuickShipMapper:
    """
    Translates between canonical domain models and QuickShip DTOs.
    """
    
    
    @staticmethod
    def to_provider_request(request: ShipmentRequest) -> QuickShipQuoteRequest:
        """
        Maps a canonical ShipmentRequest to a QuickShipQuoteRequest.
        Location normalization happens here.
        """
        qs_packages = [
            QuickShipPackage(
                packageLength=str(p.length),
                packageWidth=str(p.width),
                packageHeight=str(p.height),
                packageWeight=str(p.weight)
            ) for p in request.packages
        ]
        
        # doxSpx mapping (1 = Document, 2 = Parcel/Non-Document based on common conventions)
        dox_spx = 1 if request.shipmentType.lower() == "document" else 2
        
        # Read originCountry from config/settings with IND as default
        origin_country = os.environ.get("QUICKSHIP_ORIGIN_COUNTRY", "IND")
        
        dest_country_iso3 = quickship_location_service.get_country_code(request.destinationCountry)
        
        return QuickShipQuoteRequest(
            originCountry=origin_country,
            country=dest_country_iso3,
            pincode=request.postalCode,
            city=request.destinationCity,
            state=request.destinationState,
            stateCode=quickship_location_service.get_state_code(dest_country_iso3, request.destinationState),
            doxSpx=dox_spx,
            packages=qs_packages
        )

    @staticmethod
    def to_domain_quotes(response: QuickShipQuoteResponse) -> List[ShipmentQuote]:
        """
        Maps a QuickShipQuoteResponse to a list of canonical ShipmentQuotes.
        """
        if not response.success or not response.data:
            return []
            
        quotes = []
        for item in response.data:
            # Map charges
            mapped_charges = [
                Charge(
                    name=c.chargeName,
                    amount=c.amount,
                    gst=c.gstAmount,
                    total=c.totalAmountIncGst
                ) for c in item.charges
            ]
            
            # Map badges
            badges = []
            if item.showExtendedAreaButton:
                badges.append("extended_area")
                
            # Direct mapping of transit string
            transit_estimate = item.transitDay
                
            quotes.append(
                ShipmentQuote(
                    provider="QuickShip",
                    providerCode="quickship",
                    service=item.shippingServiceName,
                    serviceCode=item.serviceCode or str(item.shippingServiceId),
                    providerQuoteId=str(item.shippingServiceId), # Using shippingServiceId as the unique quote ID
                    currency=item.rateCurrency,
                    totalPrice=item.totalAmount,
                    gst=item.totalGstAmount,
                    transitEstimate=transit_estimate,
                    chargeableWeight=item.chargeableWeight,
                    volumetricWeight=item.volumetricWeight,
                    deadWeight=item.deadWeight,
                    zone=item.zone,
                    charges=mapped_charges,
                    badges=badges,
                    logo=item.logo
                )
            )
            
        return quotes
