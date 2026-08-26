from typing import Dict, Any, List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote, Charge

class ShipglobalMapper:
    """
    Transforms canonical domain models to and from Shipglobal specific formats.
    """
    
    def to_provider_request(self, request: ShipmentRequest) -> Dict[str, Any]:
        """
        Maps a CourierBridge ShipmentRequest to Shipglobal's get-shipper-rates payload.
        """
        # Calculate total weight and dimensions. 
        # Shipglobal payload takes a single package's details or aggregate.
        total_weight = sum(p.weight for p in request.packages) if request.packages else 1.0
        
        # We'll take the dimensions of the first package or max dimensions. 
        # Usually APIs expect aggregate or single package. We'll send first package or default 10.
        if request.packages:
            length = request.packages[0].length
            breadth = request.packages[0].width
            height = request.packages[0].height
        else:
            length, breadth, height = 10, 10, 10
            
        return {
            "customer_shipping_postcode": request.postalCode or "",
            "customer_shipping_country_code": request.destinationCountry.upper()[:2],
            "package_weight": total_weight,
            "package_length": length,
            "package_breadth": breadth,
            "package_height": height,
            "customer_shipping_address": "",
            "customer_shipping_address_2": "",
            "customer_shipping_address_3": ""
        }

    def to_domain_quotes(self, provider_response: Dict[str, Any]) -> List[ShipmentQuote]:
        """
        Maps Shipglobal's response to CourierBridge canonical ShipmentQuote models.
        """
        quotes = []
        data = provider_response.get("data") or {}
        rates = data.get("rate") or []
        
        chargeable_weight = float(data.get("bill_weight", 0)) / 1000.0 if data.get("bill_weight") else 0.0 # It might be in grams, wait. 
        # In the user's example: package_weight=10 (in request), API returns "bill_weight": 10000 (which is 10kg in grams).
        # We will assume if bill_weight is large, it's grams. Let's just use the bill_weight_kg from the rate itself.
        
        for r in rates:
            # Example: "rate": 4906, "LOGISTIC_FEE": 4906, "SUBTOTAL_FEE": 5906
            total_price = float(r.get("SUBTOTAL_FEE") or r.get("rate", 0))
            
            # The bill_weight_kg is explicitly provided in the rate object
            cw_kg = float(r.get("bill_weight_kg", chargeable_weight))
            
            service_name = r.get("display_name", "ShipGlobal")
            service_code = r.get("provider_code", "sg")
            transit = r.get("transit_time", "")
            logo = r.get("image", None)
            
            charges = []
            if r.get("LOGISTIC_FEE"):
                charges.append(Charge(name="Logistic Fee", amount=float(r["LOGISTIC_FEE"]), total=float(r["LOGISTIC_FEE"])))
            
            other_fees = r.get("OTHER_FEE_DETAIL") or {}
            for info in other_fees.get("INFO") or []:
                charges.append(Charge(name=info.get("name") or info.get("key", "Surcharge"), amount=float(info.get("value", 0)), total=float(info.get("value", 0))))
            
            quote = ShipmentQuote(
                provider="Shipglobal",
                providerCode="shipglobal",
                service=service_name,
                serviceCode=service_code,
                currency="INR", # Assuming INR based on typical Shipglobal setup in India
                totalPrice=total_price,
                transitEstimate=transit,
                chargeableWeight=cw_kg,
                volumetricWeight=cw_kg, # fallback
                deadWeight=cw_kg, # fallback
                charges=charges,
                logo=logo
            )
            quotes.append(quote)
            
        return quotes
