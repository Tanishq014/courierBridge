from bs4 import BeautifulSoup
from typing import List
import re
from app.rates.domain.models import ShipmentQuote, Charge

class OverseasParser:
    @staticmethod
    def parse_rates(html_content: str) -> List[ShipmentQuote]:
        soup = BeautifulSoup(html_content, 'html.parser')
        quotes = []
        
        cards = soup.select('.rate-card')
        for card in cards:
            try:
                # Service and Provider name
                service_name_el = card.select_one('h5.mb-1')
                service_name = service_name_el.text.strip() if service_name_el else "Unknown Service"
                
                # Try to get provider/network from onclick of book button
                book_btn = card.select_one('.book-now-btn')
                provider = "Overseas"
                service_code = service_name
                if book_btn and 'onclick' in book_btn.attrs:
                    # handleBookNow('DHL_EXPRESS', 'DHL')
                    onclick = book_btn['onclick']
                    match = re.search(r"handleBookNow\('([^']*)',\s*'([^']*)'", onclick)
                    if match:
                        service_code = match.group(1)
                        provider = match.group(2)

                # Price
                price_tag = card.select_one('.price-tag')
                total_price = 0.0
                if price_tag:
                    price_text = price_tag.text.replace(',', '').strip()
                    price_match = re.search(r'[\d\.]+', price_text)
                    if price_match:
                        total_price = float(price_match.group(0))

                # Logo
                logo_el = card.select_one('.carrier-logo')
                logo = logo_el['src'] if logo_el and 'src' in logo_el.attrs else None
                if logo and logo.startswith('/'):
                    logo = f"https://app.overseaslogistic.com{logo}"

                # Breakdown / Charges
                charges = []
                fuel_surcharge_amount = 0.0
                base_rate = 0.0
                
                detail_cards = card.select('.rate-detail-card')
                for dcard in detail_cards:
                    rows = dcard.select('.d-flex.justify-content-between')
                    for row in rows:
                        spans = row.find_all('span')
                        if len(spans) == 2:
                            name = spans[0].text.strip()
                            val_text = spans[1].text.replace(',', '').strip()
                            val_match = re.search(r'[\d\.]+', val_text)
                            if val_match:
                                amount = float(val_match.group(0))
                                charges.append(Charge(name=name, amount=amount, gst=0.0, total=amount))
                                if "base" in name.lower():
                                    base_rate = amount

                # GST calculation (Overseas usually says "Inclusive All Tax" and doesn't break down GST in detail cards easily)
                # We'll just assume GST is part of total if not explicitly found.
                gst = 0.0
                for c in charges:
                    if "gst" in c.name.lower() or "igst" in c.name.lower() or "cgst" in c.name.lower():
                        gst += c.amount

                # Calculate chargeable weight (assume 0 for now as it's not prominently in the card)
                chg_weight = 0.0

                quotes.append(ShipmentQuote(
                    provider=provider,
                    providerCode="overseas",
                    service=service_name,
                    serviceCode=service_code,
                    providerQuoteId=service_code,
                    currency="INR",
                    totalPrice=total_price - gst,  # Our system expects totalPrice + GST = Final
                    gst=gst,
                    transitEstimate=None,
                    chargeableWeight=chg_weight,
                    volumetricWeight=0.0,
                    deadWeight=0.0,
                    zone=None,
                    charges=charges,
                    badges=[],
                    logo=logo
                ))
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Error parsing overseas rate card: {e}")
                continue
                
        return quotes
