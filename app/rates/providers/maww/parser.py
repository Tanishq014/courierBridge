import logging
from bs4 import BeautifulSoup
from typing import List
from app.rates.domain.models import ShipmentQuote, Charge

logger = logging.getLogger(__name__)

class MawwParser:
    @staticmethod
    def parse_rates(html_content: str) -> List[ShipmentQuote]:
        soup = BeautifulSoup(html_content, 'html.parser')
        quotes = []
        
        table = soup.select_one('table')
        if not table:
            return quotes
            
        col_map = {}
        header_row = table.select_one('thead tr')
        if header_row:
            col_idx = 0
            for th in header_row.select('th'):
                text = ''.join(th.stripped_strings).upper()
                colspan = int(th.get('colspan', 1))
                if text:
                    col_map[text] = col_idx
                col_idx += colspan
                
        def get_col(name: str, fallback_idx: int) -> int:
            return col_map.get(name, fallback_idx)

        idx_service = get_col('SERVICE', 1)
        idx_weight = get_col('WEIGHT', 11)
        idx_amount = get_col('AMOUNT', 12)
        idx_other = get_col('OTHER CHARGES', 13)
        idx_fsc = get_col('FSC', 14)
        idx_igst = get_col('IGST', 16)
        idx_cgst = get_col('CGST', 17)
        idx_sgst = get_col('SGST', 18)
        idx_total = get_col('TOTAL AMOUNT', 19)
            
        rows = table.select('tbody tr')
        for row in rows:
            cols = [td.text.strip() for td in row.select('td')]
            
            # The table has exactly 23 columns (including empty ones for rowspan/colspan padding)
            if len(cols) < 20:
                continue
                
            try:
                service_name = cols[idx_service] if idx_service < len(cols) else None
                if not service_name:
                    continue
                    
                def get_float(idx: int) -> float:
                    if idx < len(cols) and cols[idx]:
                        try:
                            return float(cols[idx])
                        except ValueError:
                            return 0.0
                    return 0.0
                    
                # Basic fields
                weight = get_float(idx_weight)
                freight = get_float(idx_amount)
                other_charges = get_float(idx_other)
                fsc = get_float(idx_fsc)
                igst = get_float(idx_igst)
                cgst = get_float(idx_cgst)
                sgst = get_float(idx_sgst)
                
                total_gst = igst + cgst + sgst
                total_amount = get_float(idx_total)
                
                # Build charges breakdown
                charges = []
                if freight > 0:
                    charges.append(Charge(name="Freight", amount=freight, total=freight))
                if other_charges > 0:
                    charges.append(Charge(name="Other Charges", amount=other_charges, total=other_charges))
                if fsc > 0:
                    charges.append(Charge(name="Fuel Surcharge", amount=fsc, total=fsc))
                    
                # Logo mapping based on service name
                service_upper = service_name.upper()
                logo_url = None
                
                # Material Design truck SVG for fallback
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
                    providerCode="maww",
                    provider="MAWW Logistics",
                    logo=logo_url,
                    serviceCode=service_name.replace(" ", "_").upper(),
                    service=service_name,
                    totalPrice=total_amount - total_gst, # Base price without GST
                    currency="INR",
                    chargeableWeight=weight,
                    volumetricWeight=0.0,
                    deadWeight=0.0,
                    transitEstimate=None,
                    charges=charges,
                    gst=total_gst
                )
                quotes.append(quote)
            except Exception as e:
                logger.warning(f"Error parsing MAWW rate row: {e}")
                continue
                
        return quotes
