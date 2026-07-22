import re
import logging
from bs4 import BeautifulSoup
from typing import List
from app.rates.domain.models import ShipmentRequest, ShipmentQuote, Charge

logger = logging.getLogger(__name__)

class SkyNetParser:
    @staticmethod
    def parse_rates(html_content: str, request: ShipmentRequest = None) -> List[ShipmentQuote]:
        soup = BeautifulSoup(html_content, 'html.parser')
        quotes = []
        
        table = soup.find('table', id='rate_table')
        if not table:
            return quotes
            
        rows = table.find('tbody').find_all('tr') if table.find('tbody') else table.find_all('tr')
        
        actual_wt = sum(p.weight for p in request.packages) if request else 0.0
        
        # We will deduplicate identical quotes just in case, similar to Atlantic
        seen_quotes = {}
        
        for row in rows:
            cols = row.find_all('td')
            if len(cols) < 4:
                continue
                
            service_name = cols[0].text.strip()
            if not service_name:
                continue
                
            total_text = cols[1].text
            breakdown_text = cols[2].text
            weight_text = cols[3].text
            transit_time = cols[4].text.strip() if len(cols) > 4 else ""
            
            # Extract values
            grand_match = re.search(r'Grand Total\s*:\s*([\d\.]+)', total_text)
            freight_match = re.search(r'Freight\s*([\d\.]+)', breakdown_text)
            fsc_match = re.search(r'FSC(?!\s*%)\s*([\d\.]+)', breakdown_text)
            gst_match = re.search(r'GST\s*([\d\.]+)', breakdown_text)
            other_match = re.search(r'OTHER CHARGES\s+([\d\.]+)', breakdown_text)
            
            try:
                grand_total = float(grand_match.group(1)) if grand_match else 0.0
                freight = float(freight_match.group(1)) if freight_match else 0.0
                fsc = float(fsc_match.group(1)) if fsc_match else 0.0
                gst = float(gst_match.group(1)) if gst_match else 0.0
                other = float(other_match.group(1)) if other_match else 0.0
                
                # Chargeable weight
                cw_match = re.search(r'([\d\.]+)', weight_text)
                chargeable_wt = float(cw_match.group(1)) if cw_match else actual_wt
                
                if grand_total == 0:
                    continue
                    
                charges = []
                if freight > 0: charges.append(Charge(name="Freight", amount=freight, total=freight))
                if other > 0: charges.append(Charge(name="Other Charges", amount=other, total=other))
                if fsc > 0: charges.append(Charge(name="Fuel Surcharge", amount=fsc, total=fsc))
                
                # Deduct GST to get base total. 
                # Verified: SkyNet's Grand Total strictly equals Freight + FSC + Other + GST.
                base_total = grand_total - gst
                
                # Logo Logic
                service_upper = service_name.upper()
                logo_url = None
                
                truck_svg = "data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%234a5568'><path d='M20 8h-3V4H3c-1.1 0-2 .9-2 2v11h2c0 1.66 1.34 3 3 3s3-1.34 3-3h6c0 1.66 1.34 3 3 3s3-1.34 3-3h2v-5l-3-4zM6 18.5c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5zm13.5-9l1.96 2.5H17V9.5h2.5zm-1.5 9c-.83 0-1.5-.67-1.5-1.5s.67-1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z'/></svg>"

                if "UPS" in service_upper:
                    logo_url = "https://cdn.simpleicons.org/ups/00688B"
                elif "FEDEX" in service_upper:
                    logo_url = "https://cdn.simpleicons.org/fedex/4d148c"
                elif "DHL" in service_upper:
                    logo_url = "https://cdn.simpleicons.org/dhl/D40511"
                elif "ARAMEX" in service_upper:
                    logo_url = truck_svg
                else:
                    logo_url = truck_svg
                    
                quote = ShipmentQuote(
                    providerCode="skynet",
                    provider="SkyNet",
                    logo=logo_url,
                    serviceCode=service_name.replace(" ", "_").upper(),
                    service=service_name,
                    totalPrice=base_total,
                    currency="INR",
                    chargeableWeight=chargeable_wt,
                    volumetricWeight=0.0,
                    deadWeight=actual_wt,
                    transitEstimate=transit_time,
                    charges=charges,
                    gst=gst
                )
                
                dedup_key = (service_name, grand_total)
                seen_quotes[dedup_key] = quote
                
            except Exception as e:
                logger.warning(f"Error parsing SkyNet rate row: {e}")
                continue
                
        return list(seen_quotes.values())
