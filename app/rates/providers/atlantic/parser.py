import logging
from typing import List
from app.rates.domain.models import ShipmentQuote, Charge, ShipmentRequest

logger = logging.getLogger(__name__)

class AtlanticParser:
    @staticmethod
    def parse_rates(json_data: dict, request: ShipmentRequest = None) -> List[ShipmentQuote]:
        quotes = []
        seen_quotes = {}
        
        # Calculate weights if request is provided
        actual_wt = sum(p.weight for p in request.packages) if request else 0.0
        vol_wt = sum(round((p.length * p.width * p.height) / 5000, 2) for p in request.packages) if request else 0.0
        chargeable_wt = max(actual_wt, vol_wt)
        
        results = json_data.get("result", [])
        if not results:
            return quotes
            
        # The first object is often the table headers ("Vendor": "Vendor")
        if results and results[0].get("Vendor") == "Vendor":
            results = results[1:]
            
        for r in results:
            try:
                vendor = str(r.get("Vendor", "")).strip()
                service = str(r.get("Service", "")).strip()
                if not vendor or not service:
                    continue
                    
                amount = float(r.get("Amount", 0))
                other_charges = float(r.get("Other Charges", 0))
                fuel = float(r.get("Fuel", 0))
                tax = float(r.get("Tax", 0))
                
                # Base total before tax is "Total" (or amount + other + fuel)
                base_total = float(r.get("Total", 0))
                
                charges = []
                if amount > 0:
                    charges.append(Charge(name="Freight", amount=amount, total=amount))
                if other_charges > 0:
                    charges.append(Charge(name="Other Charges", amount=other_charges, total=other_charges))
                if fuel > 0:
                    charges.append(Charge(name="Fuel Surcharge", amount=fuel, total=fuel))
                    
                vendor_upper = vendor.upper()
                logo_url = None
                
                # Material Design truck SVG for fallback
                truck_svg = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%234a5568'><path d='M20 8h-3V4H3c-1.1 0-2 .9-2 2v11h2c0 1.66 1.34 3 3 3s3-1.34 3-3h6c0 1.66 1.34 3 3 3s3-1.34 3-3h2v-5l-3-4zM6 18.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm13.5-9l1.96 2.5H17V9.5h2.5zm-1.5 9c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z'/></svg>"

                if "UPS" in vendor_upper:
                    logo_url = "https://cdn.simpleicons.org/ups/00688B" # UPS brown/gold-ish
                elif "FEDEX" in vendor_upper:
                    logo_url = "https://cdn.simpleicons.org/fedex/4d148c" # FedEx purple
                elif "DHL" in vendor_upper:
                    logo_url = "https://cdn.simpleicons.org/dhl/D40511" # DHL Red
                elif "ARAMEX" in vendor_upper:
                    logo_url = truck_svg
                elif "ATLANTIC" in vendor_upper:
                    logo_url = truck_svg
                    
                quote = ShipmentQuote(
                    providerCode="atlantic",
                    provider=vendor,  # We use their Vendor name as the UI provider group (e.g., UPS, DHL)
                    logo=logo_url,
                    serviceCode=f"{vendor}_{service}".replace(" ", "_").upper(),
                    service=service,
                    totalPrice=base_total,
                    currency="INR",
                    chargeableWeight=chargeable_wt,
                    volumetricWeight=vol_wt,
                    deadWeight=actual_wt,
                    transitEstimate=r.get("Display Days", ""),
                    charges=charges,
                    gst=tax
                )
                
                dedup_key = (service, base_total)
                if dedup_key in seen_quotes:
                    existing_vendor = seen_quotes[dedup_key].provider.upper()
                    # If we already have a quote for this exact service and price, only replace it if 
                    # the new one is the actual carrier (e.g. UPS) and not the generic 'ATLANTIC INTL EXP'
                    if "ATLANTIC" in existing_vendor and "ATLANTIC" not in vendor.upper():
                        seen_quotes[dedup_key] = quote
                else:
                    seen_quotes[dedup_key] = quote
                    
            except Exception as e:
                logger.warning(f"Error parsing Atlantic rate row: {e}")
                continue
                
        return list(seen_quotes.values())
