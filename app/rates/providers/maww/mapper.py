from datetime import datetime
from app.rates.domain.models import ShipmentRequest
from app.rates.providers.maww.location import maww_location_repository

class MawwMapper:
    @staticmethod
    def to_provider_request(request: ShipmentRequest) -> dict:
        """
        Maps a canonical ShipmentRequest to MAWW's payload format.
        Example: origin_id=242&destination_id=229&pdt_format_id=2&booking_date=2026-07-22&pieces=1&ch_weight=10...
        """
        dest_country = maww_location_repository.get_country_data(request.destinationCountry)
        
        if not dest_country:
            raise ValueError(f"Unsupported destination country for MAWW: {request.destinationCountry}")
            
        dest_id = str(dest_country.id)
        
        # Origin is usually India (242) for CourierBridge
        origin_id = "242"
        
        # Calculate total chargeable weight
        total_chg_wt = 0.0
        for p in request.packages:
            vol_wt = round((p.length * p.width * p.height) / 5000, 2)
            chg_wt = max(p.weight, vol_wt)
            total_chg_wt += chg_wt
            
        pdt_format_id = "1" if request.shipmentType.lower() == "document" else "2"
        
        return {
            "origin_id": origin_id,
            "destination_id": dest_id,
            "pdt_format_id": pdt_format_id,
            "booking_date": datetime.now().strftime("%Y-%m-%d"),
            "pieces": str(len(request.packages)),
            "ch_weight": str(round(total_chg_wt, 2)),
            "ori_city": "",
            "ori_state": "",
            "ori_pincode": "",
            "dest_city": request.destinationCity or "",
            "dest_state": request.destinationState or "",
            "dest_pincode": request.postalCode or ""
        }
