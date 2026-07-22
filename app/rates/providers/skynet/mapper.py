import os
import json
from datetime import datetime
from app.rates.domain.models import ShipmentRequest

class SkyNetMapper:
    def __init__(self):
        self.country_map = self._load_countries()
        
    def _load_countries(self):
        map_path = os.path.join(os.path.dirname(__file__), "data", "skynet_countries.json")
        if not os.path.exists(map_path):
            return {}
        with open(map_path, "r") as f:
            return json.load(f)

    def map_request(self, request: ShipmentRequest) -> dict:
        iso2 = request.destinationCountry.upper()
        destination_id = self.country_map.get(iso2, "")
        
        # Determine document vs parcel
        # For SkyNet: assuming 1 = Document, 2 = Non-Document/Parcel
        pdt_format_id = "1" if request.shipmentType == "document" else "2"
        
        booking_date = datetime.now().strftime("%Y-%m-%d")
        
        pieces = len(request.packages)
        
        total_ch_weight = 0.0
        
        dim_len = []
        dim_wid = []
        dim_hei = []
        vol_wt = []
        char_wt = []
        
        for p in request.packages:
            vw = (p.length * p.width * p.height) / 5000.0  # SkyNet volumetric divisor is usually 5000
            cw = max(p.weight, vw)
            total_ch_weight += cw
            
            dim_len.append(str(p.length))
            dim_wid.append(str(p.width))
            dim_hei.append(str(p.height))
            vol_wt.append(f"{vw:.2f}")
            char_wt.append(f"{cw:.2f}")

        # The params are expected as a dictionary, which httpx or urllib.parse will urlencode.
        # Note: lists are encoded appropriately by httpx when we use multi-value dictionaries or tuples.
        # But httpx might encode lists differently. To be safe, we'll return a list of tuples for httpx params.
        
        params = [
            ("show_new_ui", "1"),
            ("pdt_format_id", pdt_format_id),
            ("booking_date", booking_date),
            ("pieces", str(pieces)),
            ("ch_weight", f"{total_ch_weight:.2f}"),
            ("ori_hub_id", "24"),
            ("destination_id", destination_id),
            ("dest_pincode", request.postalCode or ""),
            ("dest_state", request.destinationState or ""),
            ("dest_city", request.destinationCity or ""),
            ("shipment_value", ""),
            ("shi_currency_id", ""),
            ("csb_type", ""),
        ]
        
        # Append array parameters
        for l in dim_len: params.append(("dim_len[]", l))
        for w in dim_wid: params.append(("dim_wid[]", w))
        for h in dim_hei: params.append(("dim_hei[]", h))
        for v in vol_wt: params.append(("vol_wt[]", v))
        for c in char_wt: params.append(("char_wt[]", c))
        
        return params

skynet_mapper = SkyNetMapper()
