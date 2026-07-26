from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import List, Dict, Any

def get_best_rates(
    db: Session, 
    weight: float, 
    destination_or_zone: str,
    postal_code: str = None,
    suburb: str = None,
    state: str = None
) -> List[Dict[str, Any]]:
    """
    Optimized ORM query to fetch quotes across all approved tariffs for a given weight and destination.
    Dynamically resolves destination strings (e.g. 'Denmark') to abstract zones (e.g. 'F Zone') per carrier.
    Returns the cheapest options first.
    """
    from app.models import ZoneMapping, TariffRateRow, TariffSection, TariffDocument, Vendor, ReusableZoneResolver
    from sqlalchemy import or_, and_
    import re
    
    def normalize_country(name: str) -> str:
        if not name: return ""
        name = name.lower().strip()
        overrides = {
            "uk": "united kingdom",
            "u.k.": "united kingdom",
            "great britain": "united kingdom",
            "u.s.a.": "united states",
            "u.s.a": "united states",
            "usa": "united states",
            "us": "united states",
            "u.s.": "united states",
            "united states of america": "united states",
            "uae": "united arab emirates",
            "u.a.e.": "united arab emirates",
        }
        name_no_dots = name.replace(".", "").replace(" ", "")
        if name in overrides:
            name = overrides[name]
        elif name_no_dots in overrides:
            name = overrides[name_no_dots]
            
        try:
            import pycountry
            return pycountry.countries.lookup(name).name.lower().strip()
        except Exception:
            try:
                import pycountry
                return pycountry.countries.lookup(name.replace(".", "")).name.lower().strip()
            except Exception:
                return name
                
    search_term = destination_or_zone.lower().strip()
    resolved_name = normalize_country(destination_or_zone)

    
    # 1. Scan ZoneMappings to find matching (carrier, service, zone) tuples for this destination
    zone_mappings = db.query(ZoneMapping).all()
    valid_combos = []
    transit_days_lookup = {}
    
    for m in zone_mappings:
        dests = [d.upper() for d in m.mapped_destinations]
        
        if destination_or_zone.upper() in dests or resolved_name.upper() in dests:
            valid_combos.append({
                "carrier": m.carrier,
                "service": m.service,
                "zone_name": m.zone_name
            })
            transit_days_lookup[(m.carrier, m.service, m.zone_name)] = getattr(m, 'transit_days', None)

    # 1b. Evaluate ReusableZoneResolvers
    resolvers = db.query(ReusableZoneResolver).all()
    resolver_conditions = []
    for r in resolvers:
        matched_zone = None
        for mapping in (r.mapping_data or []):
            k_type = mapping.get("key_type", "").lower()
            k_val = str(mapping.get("key", "")).lower().strip()
            zone = str(mapping.get("zone", "")).strip()
            
            if k_type == "postcode" and postal_code:
                norm_postal = postal_code.lower().replace(" ", "")
                norm_k = k_val.lower().replace(" ", "")
                
                # Check for numeric range (e.g. "4000-4010")
                if "-" in norm_k and norm_postal.isdigit():
                    parts = norm_k.split("-")
                    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                        if int(parts[0]) <= int(norm_postal) <= int(parts[1]):
                            matched_zone = zone
                            break
                            
                # Fallback to exact or prefix matching
                if norm_postal == norm_k or norm_postal.startswith(norm_k):
                    matched_zone = zone
                    break
            elif k_type == "suburb" and suburb and suburb.lower().strip() == k_val:
                matched_zone = zone
                break
            elif k_type == "state" and state and state.lower().strip() == k_val:
                matched_zone = zone
                break
            elif k_type == "country":
                # Normalize the resolver's key so "U.S.A" becomes "united states"
                norm_k = normalize_country(k_val)
                if norm_k == resolved_name or k_val == search_term:
                    matched_zone = zone
                    break
                
        if matched_zone:
            # If matched_zone is "3", we want to match "ZONE 3", "3", "Z3", etc.
            # We match if the digits of the TariffRateRow zone end with or match the digits of the matched_zone
            # The most foolproof way in generic SQL without regex is ilike
            # For exact number matching, since we don't have REGEXP in base sqlite, we use multiple ilike
            resolver_conditions.append(and_(
                TariffSection.zone_resolver_id == r.id,
                or_(
                    TariffRateRow.zone.ilike(matched_zone),
                    TariffRateRow.zone.ilike(f"% {matched_zone}"),
                    TariffRateRow.zone.ilike(f"%0{matched_zone}")
                )
            ))

    # 2. Build the query
    query = db.query(
        Vendor.name.label("vendor_name"),
        TariffSection.id.label("section_id"),
        TariffSection.carrier.label("carrier"),
        TariffSection.service.label("service"),
        TariffSection.valid_from.label("valid_from"),
        TariffSection.valid_to.label("valid_to"),
        TariffRateRow.weight.label("weight"),
        TariffRateRow.weight_max.label("weight_max"),
        TariffRateRow.zone.label("zone"),
        TariffRateRow.price.label("price"),
        TariffRateRow.price_type.label("price_type"),
        TariffDocument.uploaded_at.label("uploaded_at"),
        TariffDocument.original_filename.label("original_filename")
    ).join(TariffSection, TariffRateRow.section_id == TariffSection.id) \
     .join(TariffDocument, TariffSection.document_id == TariffDocument.id) \
     .join(Vendor, TariffDocument.vendor_id == Vendor.id) \
     .filter(TariffDocument.status.in_(['APPROVED', 'PARTIALLY_APPROVED']))
     
    # 3. Dynamic Zone/Destination conditions
    # Fallback: Allow direct search by zone name (e.g. if the caller passed "F Zone") to prevent breaking APIs
    conditions = [TariffRateRow.zone == destination_or_zone]
    
    # Add mapped combinations
    for combo in valid_combos:
        conditions.append(and_(
            TariffSection.carrier == combo["carrier"],
            TariffSection.service == combo["service"],
            TariffRateRow.zone == combo["zone_name"]
        ))
        
    if resolver_conditions:
        conditions.extend(resolver_conditions)
        
    query = query.filter(or_(*conditions))
    
    result = query.all()
    
    from collections import defaultdict
    
    # Find the latest uploaded_at date for each service to prevent duplicates
    latest_dates = {}
    for row in result:
        service_key = f"{row.vendor_name}|{row.carrier}|{row.service}"
        if service_key not in latest_dates or row.uploaded_at > latest_dates[service_key]:
            latest_dates[service_key] = row.uploaded_at
            
    services = defaultdict(list)
    for row in result:
        service_key = f"{row.vendor_name}|{row.carrier}|{row.service}"
        if row.uploaded_at == latest_dates[service_key]:
            transit_val = transit_days_lookup.get((row.carrier, row.service, row.zone))
            services[service_key].append({
                "vendor_name": row.vendor_name,
                "section_id": row.section_id,
                "carrier": row.carrier,
                "service": row.service,
                "weight": float(row.weight),
                "weight_max": float(row.weight_max) if row.weight_max is not None else None,
                "zone": row.zone,
                "price": float(row.price),
                "price_type": row.price_type,
                "transit_days": transit_val,
                "source_filename": row.original_filename
            })
        
    quotes = []
    
    for service_key, rows in services.items():
        flats = [r for r in rows if r['price_type'] == 'FLAT']
        per_kgs = [r for r in rows if r['price_type'] == 'PER_KG']
        
        best_rate = None
        logic = ''
        total_price = float('inf')
        
        def row_covers_weight(r, w):
            w_min = float(r['weight'])
            w_max = r.get('weight_max')
            if w_max is not None:
                # Bracket row: weight must fall within [w_min, w_max]
                return w_min <= w <= float(w_max)
            else:
                # Legacy point row for PER_KG: w_min <= requested_weight
                return w_min <= w
        
        if per_kgs:
            valid_per_kgs = [r for r in per_kgs if row_covers_weight(r, weight)]
            if valid_per_kgs:
                # If multiple brackets match, pick tightest range
                best_bracket = min(valid_per_kgs, key=lambda x: (float(x.get('weight_max') or 99999) - float(x['weight'])))
                total_price = float(best_bracket['price']) * weight
                best_rate = dict(best_bracket)
                w_max_v = best_bracket.get('weight_max')
                w_max_disp = f"{float(w_max_v)}" if w_max_v and float(w_max_v) < 99999 else "∞"
                logic = f"Using {float(best_bracket['weight'])}–{w_max_disp}kg bracket rate ({float(best_bracket['price'])}/kg) for {weight}kg."
            else:
                min_bracket = min(per_kgs, key=lambda x: float(x['weight']))
                billed_w = max(weight, float(min_bracket['weight']))
                total_price = float(min_bracket['price']) * billed_w
                best_rate = dict(min_bracket)
                if billed_w > weight:
                    logic = f"Billed at {billed_w}kg bracket minimum ({float(min_bracket['price'])}/kg) for {weight}kg package."
                else:
                    logic = f"Under minimum weight bracket. Using {float(min_bracket['weight'])}kg minimum ({float(min_bracket['price'])}/kg) for {weight}kg."
                
        if flats:
            def flat_covers_weight(r, w):
                w_min = float(r['weight'])
                w_max = r.get('weight_max')
                if w_max is not None:
                    # Bracket FLAT row: weight must fall within [w_min, w_max]
                    return w_min <= w <= float(w_max)
                else:
                    # Legacy point FLAT: use >= (next slab up)
                    return w_min >= w
            
            valid_flats = [r for r in flats if flat_covers_weight(r, weight)]
            if valid_flats:
                best_flat = min(valid_flats, key=lambda x: float(x['price']))
                flat_price = float(best_flat['price'])
                if flat_price < total_price:
                    total_price = flat_price
                    best_rate = dict(best_flat)
                    w_max_v = best_flat.get('weight_max')
                    w_max_disp = f"{float(w_max_v)}" if w_max_v and float(w_max_v) < 99999 else "∞"
                    logic = f"Flat rate in {float(best_flat['weight'])}–{w_max_disp}kg bracket for {weight}kg."
                        
        if best_rate:
            best_rate['calculated_total_price'] = total_price
            best_rate['calculation_logic'] = logic
            quotes.append(best_rate)
            
    quotes.sort(key=lambda x: x['calculated_total_price'])
    quotes = quotes[:10]
    
    section_ids = [q["section_id"] for q in quotes]
    notes_by_section = {}
    if section_ids:
        from app.models import TariffNote
        all_notes = db.query(TariffNote).filter(TariffNote.section_id.in_(section_ids)).all()
        for n in all_notes:
            key = (n.section_id, n.zone) # zone is None for global/section rules
            if key not in notes_by_section:
                notes_by_section[key] = []
            notes_by_section[key].append({"text": n.text, "category": n.category})
    
    final_quotes = []
    for q in quotes:
        matched_notes = notes_by_section.get((q["section_id"], None), []) + notes_by_section.get((q["section_id"], q["zone"]), [])
        final_quotes.append({
            "vendor": q["vendor_name"],
            "carrier": q["carrier"],
            "service": q["service"],
            "db_weight": float(q["weight"]),
            "zone": q["zone"],
            "base_price": float(q["price"]),
            "price_type": q["price_type"],
            "total_price": float(q["calculated_total_price"]),
            "calculation_logic": q["calculation_logic"],
            "notes": matched_notes,
            "transit_days": q.get("transit_days"),
            "valid_from": str(q.get("valid_from")) if q.get("valid_from") else None,
            "valid_to": str(q.get("valid_to")) if q.get("valid_to") else None,
            "extracted_at": str(q.get("uploaded_at")) if q.get("uploaded_at") else None,
            "source_file": q.get("original_filename")
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
        TariffDocument.status.in_(['APPROVED', 'PARTIALLY_APPROVED']),
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
        TariffDocument.status.in_(['APPROVED', 'PARTIALLY_APPROVED'])
    ).order_by(TariffDocument.uploaded_at.desc()).first()
    
    if not old_doc:
        return {"old_doc_id": None, "diffs": []}
        
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
            
    return {
        "old_doc_id": old_doc.id if old_doc else None, 
        "diffs": diffs, 
        "knowledge_diffs": knowledge_diffs
    }
