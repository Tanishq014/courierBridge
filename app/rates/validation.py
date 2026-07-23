from typing import Dict, Any, List

def validate_tariff_json(raw_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validates the raw JSON extracted by the AI and attaches validation errors directly to the payload.
    """
    validation_results = {
        "status": "CLEAN",
        "blockers": 0,
        "warnings": 0,
        "sections": []
    }
    
    sections = raw_json.get("sections", [])
    source_text = raw_json.get("metadata", {}).get("source_text", "")
    
    for s_idx, section in enumerate(sections):
        sec_result = {
            "index": s_idx,
            "carrier": section.get("carrier", "Unknown"),
            "service": section.get("service", "Unknown"),
            "errors": [],
            "rate_errors": {}
        }
        
        # Section level validation
        if not section.get("carrier"):
            sec_result["errors"].append({"type": "BLOCKER", "code": "MISSING_CARRIER", "message": "Carrier name is missing."})
            validation_results["blockers"] += 1
            
        rates = section.get("rates", [])
        if not rates:
            sec_result["errors"].append({"type": "BLOCKER", "code": "MISSING_RATES", "message": "No rates were extracted for this section."})
            validation_results["blockers"] += 1
            
        # Pre-process zone_segments for quick lookup
        zone_segments_data = section.get("zone_segments", [])
        
        def get_price_type(zone_name, weight_val):
            for zs in zone_segments_data:
                if zs.get("zone") == zone_name:
                    for seg in zs.get("segments", []):
                        start = seg.get("start_weight")
                        end = seg.get("end_weight")
                        if start is not None and weight_val >= start:
                            if end is None or weight_val <= end:
                                return seg.get("price_type", "UNKNOWN"), seg.get("confidence", 1.0)
            return "UNKNOWN", 0.0

        # Rate level validation
        seen_weight_zones = set()
        zone_prices = {} # zone: {weight: price}
        
        for r_idx, rate in enumerate(rates):
            r_errors = []
            
            weight = rate.get("weight")
            zone = rate.get("zone")
            price = rate.get("price")
            
            # Check empty row
            if weight is None and not zone and price is None:
                r_errors.append({"type": "WARNING", "code": "EMPTY_ROW", "message": "Empty rate row found."})
                validation_results["warnings"] += 1
                sec_result["rate_errors"][r_idx] = r_errors
                continue
                
            if weight is None:
                r_errors.append({"type": "BLOCKER", "code": "MISSING_WEIGHT", "message": "Weight is missing."})
                validation_results["blockers"] += 1
            elif type(weight) not in [int, float] or weight < 0:
                r_errors.append({"type": "BLOCKER", "code": "INVALID_WEIGHT", "message": "Weight must be a positive number."})
                validation_results["blockers"] += 1
                
            if not zone:
                r_errors.append({"type": "BLOCKER", "code": "MISSING_ZONE", "message": "Zone is missing."})
                validation_results["blockers"] += 1
                
            if price is None:
                r_errors.append({"type": "BLOCKER", "code": "MISSING_PRICE", "message": "Price is missing."})
                validation_results["blockers"] += 1
            elif type(price) not in [int, float] or price < 0:
                r_errors.append({"type": "BLOCKER", "code": "INVALID_PRICE", "message": "Price must be a positive number."})
                validation_results["blockers"] += 1
            elif price > 100000:
                r_errors.append({"type": "WARNING", "code": "UNUSUALLY_HIGH_PRICE", "message": "Price is unusually high (>100,000)."})
                validation_results["warnings"] += 1
                
            confidence = rate.get("confidence")
            if confidence is not None and type(confidence) in [int, float] and confidence < 0.8:
                r_errors.append({"type": "WARNING", "code": "LOW_AI_CONFIDENCE", "message": f"AI Confidence is low ({confidence}). Please verify."})
                validation_results["warnings"] += 1
                
            # Duplicate check
            if weight is not None and zone:
                key = f"{weight}-{zone}"
                if key in seen_weight_zones:
                    r_errors.append({"type": "BLOCKER", "code": "DUPLICATE_WEIGHT_ZONE", "message": f"Duplicate entry for weight {weight} and zone {zone}."})
                    validation_results["blockers"] += 1
                seen_weight_zones.add(key)
                
                # Monotonicity check
                if type(price) in [int, float] and type(weight) in [int, float]:
                    if zone not in zone_prices:
                        zone_prices[zone] = []
                        
                    price_type, pt_conf = get_price_type(zone, weight)
                    
                    if price_type == "UNKNOWN":
                        r_errors.append({"type": "WARNING", "code": "UNKNOWN_PRICE_TYPE", "message": "Price type (FLAT vs PER_KG) could not be determined from explicit evidence. Please verify."})
                        validation_results["warnings"] += 1
                        total_price = price # Fallback to flat calculation just for monotonicity check
                    else:
                        if pt_conf < 0.8:
                            r_errors.append({"type": "WARNING", "code": "LOW_PRICE_TYPE_CONF", "message": f"AI Confidence for {price_type} is low ({pt_conf}). Please verify."})
                            validation_results["warnings"] += 1
                        total_price = price * weight if price_type == "PER_KG" else price
                        
                    zone_prices[zone].append({
                        "weight": weight, 
                        "price": price, 
                        "total_price": total_price,
                        "r_idx": r_idx
                    })
                    
            if r_errors:
                sec_result["rate_errors"][r_idx] = r_errors
                
        # Post-process monotonicity
        for z, entries in zone_prices.items():
            # sort by weight
            entries.sort(key=lambda x: x["weight"])
            
            # Check monotonicity with correct total_prices
            for i in range(1, len(entries)):
                prev = entries[i-1]
                curr = entries[i]
                if curr["total_price"] < prev["total_price"]:
                    # Is this price literally exactly in the original document? If so, the AI didn't hallucinate it.
                    # It's a real anomaly in the rate card, but the user requested we don't flag these if they are verbatim.
                    is_verbatim = False
                    if source_text:
                        import re
                        str_price = str(curr["price"])
                        str_int_price = str(int(curr["price"])) if curr["price"] == int(curr["price"]) else str_price
                        
                        pattern = r'\b(?:' + re.escape(str_price) + r'|' + re.escape(str_int_price) + r')\b'
                        if re.search(pattern, source_text):
                            is_verbatim = True
                            
                    if not is_verbatim:
                        msg = f"Total price ({curr['total_price']}) is lower than lighter package ({prev['weight']}kg costs {prev['total_price']})."
                        warn = {"type": "WARNING", "code": "PRICE_NOT_INCREASING", "message": msg}
                        if curr["r_idx"] not in sec_result["rate_errors"]:
                            sec_result["rate_errors"][curr["r_idx"]] = []
                        sec_result["rate_errors"][curr["r_idx"]].append(warn)
                        validation_results["warnings"] += 1
                    
        validation_results["sections"].append(sec_result)
        
    if validation_results["blockers"] > 0:
        validation_results["status"] = "BLOCKER"
    elif validation_results["warnings"] > 0:
        validation_results["status"] = "WARNING"
        
    return validation_results
