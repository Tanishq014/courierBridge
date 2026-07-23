from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Dict, Any

def get_best_rates(db: Session, weight: float, zone: str) -> List[Dict[str, Any]]:
    """
    Optimized SQL query to fetch quotes across all approved tariffs for a given weight and zone.
    Returns the cheapest options first.
    """
    query = text("""
        SELECT 
            v.name as vendor_name,
            s.carrier,
            s.service,
            r.weight,
            r.zone,
            r.price,
            r.price_type
        FROM tariff_rate_rows r
        JOIN tariff_sections s ON r.section_id = s.id
        JOIN tariff_documents d ON s.document_id = d.id
        JOIN vendors v ON d.vendor_id = v.id
        WHERE d.status = 'APPROVED'
          AND r.zone = :zone
    """)
    
    result = db.execute(query, {"zone": zone})
    
    from collections import defaultdict
    services = defaultdict(list)
    for row in result.mappings():
        service_key = f"{row['vendor_name']}|{row['carrier']}|{row['service']}"
        services[service_key].append(row)
        
    quotes = []
    
    for service_key, rows in services.items():
        flats = [r for r in rows if r['price_type'] == 'FLAT']
        per_kgs = [r for r in rows if r['price_type'] == 'PER_KG']
        
        best_rate = None
        logic = ''
        total_price = float('inf')
        
        if per_kgs:
            valid_per_kgs = [r for r in per_kgs if float(r['weight']) <= weight]
            if valid_per_kgs:
                best_bracket = max(valid_per_kgs, key=lambda x: float(x['weight']))
                total_price = float(best_bracket['price']) * weight
                best_rate = dict(best_bracket)
                logic = f"Using {float(best_bracket['weight'])}kg bracket rate ({float(best_bracket['price'])}/kg) for {weight}kg."
            else:
                min_bracket = min(per_kgs, key=lambda x: float(x['weight']))
                total_price = float(min_bracket['price']) * weight
                best_rate = dict(min_bracket)
                logic = f"Under minimum weight. Using {float(min_bracket['weight'])}kg minimum bracket rate ({float(min_bracket['price'])}/kg) for {weight}kg."
                
        if flats:
            valid_flats = [r for r in flats if float(r['weight']) >= weight]
            if valid_flats:
                best_flat = min(valid_flats, key=lambda x: float(x['weight']))
                flat_price = float(best_flat['price'])
                if flat_price < total_price:
                    total_price = flat_price
                    best_rate = dict(best_flat)
                    if float(best_flat['weight']) > weight:
                        logic = f"Using {float(best_flat['weight'])}kg flat rate minimum for {weight}kg shipment."
                    else:
                        logic = f"Exact weight match ({weight}kg flat rate)."
                        
        if best_rate:
            best_rate['calculated_total_price'] = total_price
            best_rate['calculation_logic'] = logic
            quotes.append(best_rate)
            
    quotes.sort(key=lambda x: x['calculated_total_price'])
    quotes = quotes[:10]
    
    final_quotes = []
    for q in quotes:
        final_quotes.append({
            "vendor": q["vendor_name"],
            "carrier": q["carrier"],
            "service": q["service"],
            "db_weight": float(q["weight"]),
            "zone": q["zone"],
            "base_price": float(q["price"]),
            "price_type": q["price_type"],
            "total_price": float(q["calculated_total_price"]),
            "calculation_logic": q["calculation_logic"]
        })
        
    return final_quotes

def search_tariffs(db: Session, query_str: str) -> List[Dict[str, Any]]:
    """
    Simple search functionality to find tariffs by Carrier, Service, or Vendor.
    """
    search_term = f"%{query_str}%"
    
    # SQLite uses LIKE and is generally case-insensitive, but Postgres uses ILIKE. 
    # To be cross-compatible with SQLite in dev and Postgres in prod, we can lower() both sides.
    query = text("""
        SELECT DISTINCT
            v.name as vendor_name,
            s.carrier,
            s.service,
            d.id as document_id,
            d.uploaded_at
        FROM tariff_sections s
        JOIN tariff_documents d ON s.document_id = d.id
        JOIN vendors v ON d.vendor_id = v.id
        WHERE d.status = 'APPROVED'
          AND (
            LOWER(s.carrier) LIKE LOWER(:search_term)
            OR LOWER(s.service) LIKE LOWER(:search_term)
            OR LOWER(v.name) LIKE LOWER(:search_term)
          )
        ORDER BY d.uploaded_at DESC
        LIMIT 20
    """)
    
    result = db.execute(query, {"search_term": search_term})
    
    results = []
    for row in result.mappings():
        results.append({
            "vendor": row["vendor_name"],
            "carrier": row["carrier"],
            "service": row["service"],
            "document_id": row["document_id"],
            "uploaded_at": str(row["uploaded_at"])
        })
        
    return results

def get_tariff_diff(db: Session, new_doc_id: str):
    from app.models import TariffDocument
    
    new_doc = db.query(TariffDocument).filter(TariffDocument.id == new_doc_id).first()
    if not new_doc:
        return None
        
    # Find previous document for same vendor that is approved, before this one
    old_doc = db.query(TariffDocument).filter(
        TariffDocument.vendor_id == new_doc.vendor_id,
        TariffDocument.status == 'APPROVED',
        TariffDocument.uploaded_at < new_doc.uploaded_at
    ).order_by(TariffDocument.uploaded_at.desc()).first()
    
    if not old_doc:
        return {"old_doc": None, "new_doc": new_doc, "diffs": []}
        
    # Map old rates: (carrier, service, zone, weight) -> price
    old_rates = {}
    for s in old_doc.sections:
        for r in s.rate_rows:
            key = (s.carrier, s.service, r.zone, r.weight)
            old_rates[key] = float(r.price) if r.price is not None else 0
            
    # Map new rates
    diffs = []
    for s in new_doc.sections:
        for r in s.rate_rows:
            key = (s.carrier, s.service, r.zone, r.weight)
            new_price = float(r.price) if r.price is not None else 0
            old_price = old_rates.get(key)
            
            if old_price is None:
                status = "NEW"
                diff = new_price
            elif new_price > old_price:
                status = "INCREASED"
                diff = new_price - old_price
            elif new_price < old_price:
                status = "DECREASED"
                diff = old_price - new_price
            else:
                status = "UNCHANGED"
                diff = 0
                
            diffs.append({
                "carrier": s.carrier,
                "service": s.service,
                "zone": r.zone,
                "weight": float(r.weight) if r.weight is not None else 0,
                "old_price": old_price,
                "new_price": new_price,
                "status": status,
                "diff": diff
            })
            
    return {"old_doc": old_doc, "new_doc": new_doc, "diffs": diffs}

def get_pre_approval_diff(db: Session, raw_json: Dict[str, Any], vendor_id: str):
    from app.models import TariffDocument
    
    # Find previous document for same vendor that is approved
    old_doc = db.query(TariffDocument).filter(
        TariffDocument.vendor_id == vendor_id,
        TariffDocument.status == 'APPROVED'
    ).order_by(TariffDocument.uploaded_at.desc()).first()
    
    if not old_doc:
        return {"old_doc": None, "diffs": []}
        
    # Map old rates: (carrier, service, zone, weight) -> price
    old_rates = {}
    for s in old_doc.sections:
        for r in s.rate_rows:
            key = (s.carrier, s.service, r.zone, float(r.weight) if r.weight is not None else 0)
            old_rates[key] = float(r.price) if r.price is not None else 0
            
    # Map new rates from raw JSON
    diffs = []
    sections = raw_json.get("sections", [])
    
    for s_idx, s in enumerate(sections):
        carrier = s.get("carrier", "Unknown")
        service = s.get("service", "Unknown")
        
        rates = s.get("rates", [])
        for r_idx, r in enumerate(rates):
            zone = r.get("zone", "Unknown")
            weight = r.get("weight")
            price = r.get("price")
            
            if weight is None or price is None or not zone:
                continue
                
            weight = float(weight)
            new_price = float(price)
            
            key = (carrier, service, zone, weight)
            old_price = old_rates.get(key)
            
            if old_price is None:
                status = "NEW"
                diff = new_price
            elif new_price > old_price:
                status = "INCREASED"
                diff = new_price - old_price
            elif new_price < old_price:
                status = "DECREASED"
                diff = old_price - new_price
            else:
                status = "UNCHANGED"
                diff = 0
                
            diffs.append({
                "carrier": carrier,
                "service": service,
                "zone": zone,
                "weight": weight,
                "old_price": old_price,
                "new_price": new_price,
                "status": status,
                "diff": diff,
                "s_idx": s_idx,
                "r_idx": r_idx
            })
            
    # Process Knowledge Diffs
    from app.models import ZoneMapping
    knowledge_diffs = []
    knowledge = raw_json.get("knowledge", [])
    
    for k_idx, k in enumerate(knowledge):
        if k.get("type") == "zone_mapping":
            carrier = k.get("carrier") or ""
            service = k.get("service") or ""
            zone = k.get("zone")
            countries = k.get("countries", [])
            
            existing = db.query(ZoneMapping).filter(
                ZoneMapping.carrier == carrier,
                ZoneMapping.service == service,
                ZoneMapping.zone_name == zone
            ).first()
            
            old_countries = existing.mapped_destinations if existing else []
            
            if not existing:
                status = "NEW"
            else:
                # Compare sets
                if set(countries) == set(old_countries):
                    status = "UNCHANGED"
                else:
                    status = "UPDATED"
                    
            knowledge_diffs.append({
                "k_idx": k_idx,
                "type": "zone_mapping",
                "carrier": carrier,
                "service": service,
                "zone": zone,
                "old_countries": old_countries,
                "new_countries": countries,
                "status": status,
                "confidence": k.get("confidence")
            })
            
    return {"old_doc": old_doc, "diffs": diffs, "knowledge_diffs": knowledge_diffs}
