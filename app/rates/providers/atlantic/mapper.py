import pycountry
from typing import Dict, Any
from app.rates.domain.models import ShipmentRequest

class AtlanticMapper:
    @staticmethod
    def to_provider_request(request: ShipmentRequest) -> Dict[str, Any]:
        """
        Maps a canonical ShipmentRequest to Atlantic's JSON payload format.
        """
        # Resolve country using the custom atlantic mapping
        dest_iso2 = request.destinationCountry.upper()
        
        import os, json
        mapping_path = os.path.join(os.path.dirname(__file__), 'data', 'atlantic_countries.json')
        with open(mapping_path, 'r') as f:
            country_mapping = json.load(f)
            
        dest_code = country_mapping.get(dest_iso2)
        
        if not dest_code:
            raise ValueError(f"Unsupported destination country for Atlantic: {dest_iso2}")
            
        # Atlantic ignores Destination name if DestinationCode is correct, but we'll try to pass the name.
        import pycountry
        country_obj = pycountry.countries.get(alpha_2=dest_iso2)
        dest_name = country_obj.name.upper() if country_obj else dest_iso2
        
        # Origin is fixed to BOM (Mumbai)
        origin_code = "BOM"
        
        # Calculate weights
        total_actual_wt = 0.0
        total_vol_wt = 0.0
        for p in request.packages:
            total_actual_wt += p.weight
            vol_wt = round((p.length * p.width * p.height) / 5000, 2)
            total_vol_wt += vol_wt
            
        # Read CustomerCode from environment (it is the username)
        import os
        customer_code = os.environ.get("ATLANTIC_USERNAME")
        if not customer_code:
            raise ValueError("ATLANTIC_USERNAME must be set in the environment.")
            
        # Map shipment type (document vs parcel)
        product_code = "DOX" if request.shipmentType.lower() == "document" else "SPX"
            
        return {
            "searchdata": {
                "CustomerCode": customer_code,
                "VendorCode": "",
                "ProductCode": product_code,
                "DestinationCode": dest_code,
                "Destination": dest_name,
                "ServiceType": "",
                "Weight": str(round(total_actual_wt, 2)),
                "VolWeight": str(round(total_vol_wt, 2)),
                "OriginCode": origin_code,
                "ToPinCode": request.postalCode or ""
            }
        }
