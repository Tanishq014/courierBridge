import os
import time
import shutil
import uuid
import json
from fastapi import APIRouter, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Vendor, TariffDocument, TariffSection, TariffRateRow, TariffNote, ZoneMapping
from app.rates.ai import extract_rates_from_document
from app.rates.hybrid import process_deterministic_zones
from app.rates.validation import validate_tariff_json
from app.rates.engine import get_best_rates, search_tariffs, get_tariff_diff, get_pre_approval_diff

router = APIRouter(tags=["Tariffs"])
templates = Jinja2Templates(directory="app/templates")

UPLOAD_DIR = "app/static/uploads/tariffs"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@router.get("/tariffs", response_class=HTMLResponse)
async def list_tariffs_page(request: Request, db: Session = Depends(get_db)):
    """
    Renders the history dashboard for uploaded tariffs.
    """
    docs = db.query(TariffDocument).order_by(TariffDocument.uploaded_at.desc()).all()
    return templates.TemplateResponse(
        "rates/history.html",
        {"request": request, "docs": docs}
    )

@router.get("/tariffs/upload", response_class=HTMLResponse)
async def upload_page(request: Request, db: Session = Depends(get_db)):
    """
    Renders the UI for the tariff upload page.
    """
    vendors = db.query(Vendor).all()
    # Create a default vendor if none exists to make testing easier
    if not vendors:
        v = Vendor(name="Default Vendor")
        db.add(v)
        db.commit()
        db.refresh(v)
        vendors = [v]
        
    return templates.TemplateResponse(
        "rates/upload_tariff.html",
        {"request": request, "vendors": vendors}
    )

class VendorCreateRequest(BaseModel):
    name: str

@router.post("/api/vendors")
async def create_vendor(req: VendorCreateRequest, db: Session = Depends(get_db)):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Vendor name cannot be empty")
    existing = db.query(Vendor).filter(Vendor.name == name).first()
    if existing:
        return {"id": existing.id, "name": existing.name}
    v = Vendor(name=name)
    db.add(v)
    db.commit()
    db.refresh(v)
    return {"id": v.id, "name": v.name}

@router.post("/api/tariffs/analyze-file")
async def analyze_file(file: UploadFile = File(...)):
    try:
        file_extension = os.path.splitext(file.filename)[1].lower()
        allowed_extensions = ['.pdf', '.png', '.jpg', '.jpeg', '.xlsx', '.xls']
        if file_extension not in allowed_extensions:
            raise Exception(f"Invalid file extension. Allowed types: {', '.join(allowed_extensions)}")
            
        unique_filename = f"temp_{uuid.uuid4()}{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, unique_filename)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        sheets = []
        is_excel = file_extension in [".xlsx", ".xls"]
        if is_excel:
            try:
                import openpyxl
                wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
                try:
                    sheets = [sheet.title for sheet in wb.worksheets if sheet.sheet_state == 'visible']
                finally:
                    wb.close()
            except Exception as e:
                pass
                
        return {
            "success": True, 
            "temp_file_id": unique_filename,
            "original_filename": file.filename,
            "is_excel": is_excel,
            "sheets": sheets
        }
    except Exception as e:
        import traceback
        err_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=err_msg)

@router.post("/api/tariffs/check-duplicate")
async def check_duplicate(
    vendor_id: str = Form(...),
    original_filename: str = Form(...),
    selected_sheets: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Checks if a file with the same name and sheets was already uploaded for this vendor.
    """
    try:
        previous_docs = db.query(TariffDocument).filter(
            TariffDocument.vendor_id == vendor_id,
            TariffDocument.original_filename == original_filename,
            TariffDocument.status == "APPROVED"
        ).all()
        
        if not previous_docs:
            return {"is_duplicate": False, "duplicate_sheets": []}
            
        duplicate_sheets = set()
        is_duplicate = False
        
        if selected_sheets:
            new_sheets = set(json.loads(selected_sheets))
            for doc in previous_docs:
                if doc.selected_sheets:
                    try:
                        old_sheets = set(json.loads(doc.selected_sheets))
                        duplicate_sheets.update(new_sheets.intersection(old_sheets))
                    except:
                        pass
            
            if duplicate_sheets:
                is_duplicate = True
        else:
            # For photos or non-excel where selected_sheets is None
            is_duplicate = True
            
        return {"is_duplicate": is_duplicate, "duplicate_sheets": list(duplicate_sheets)}
    except Exception as e:
        import traceback
        err_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=err_msg)

@router.post("/api/tariffs/upload")
async def upload_tariff(
    vendor_id: str = Form(...),
    temp_file_id: str = Form(...),
    original_filename: str = Form(...),
    selected_sheets: Optional[str] = Form(None),
    skip_middle_sheets: Optional[str] = Form(None),
    force_all_sheets: Optional[str] = Form(None),
    ai_context: Optional[str] = Form(None),
    actor_model: Optional[str] = Form(None),
    critic_model: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Process endpoint for tariff documents.
    """
    try:
        file_path = os.path.join(UPLOAD_DIR, temp_file_id)
        if not os.path.exists(file_path):
            raise Exception("Temporary file not found. Please upload again.")
            
        # Create Draft Document in DB
        doc = TariffDocument(
            vendor_id=vendor_id,
            file_url=f"/static/uploads/tariffs/{temp_file_id}",
            original_filename=original_filename,
            selected_sheets=selected_sheets,
            status="DRAFT",
            prompt_version="v1"
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        
        sheets_list = None
        skip_middle = None
        force_all = None
        if selected_sheets:
            sheets_list = json.loads(selected_sheets)
        if skip_middle_sheets:
            skip_middle = json.loads(skip_middle_sheets)
        if force_all_sheets:
            force_all = json.loads(force_all_sheets)
            
        # Call Gemini Flash AI Pipeline
        raw_json = await extract_rates_from_document(
            file_path, 
            original_filename, 
            allowed_sheets=sheets_list, 
            skip_middle_sheets=skip_middle, 
            force_all_sheets=force_all,
            ai_context=ai_context,
            actor_model=actor_model,
            critic_model=critic_model
        )
        
        # Phase 3: Hybrid Python Deterministic Parser (for massive zone tables)
        raw_json = process_deterministic_zones(file_path, raw_json)
        
        # Save raw extraction
        doc.raw_extraction_json = raw_json
        db.commit()
        
        # Auto-delete the file to keep the server completely clean
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as e:
            print(f"Warning: Failed to delete temp file {file_path}: {e}")
        
        return {"success": True, "document_id": doc.id, "message": "File processed successfully."}
        
    except ValueError as e:
        if str(e).startswith("TOO_MANY_ROWS|"):
            try:
                violations_json = str(e).split("|", 1)[1]
                violations = json.loads(violations_json)
                return JSONResponse(status_code=400, content={
                    "error_type": "TOO_MANY_ROWS",
                    "violations": violations
                })
            except:
                pass
        import traceback
        err_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=err_msg)
    except Exception as e:
        import traceback
        err_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=err_msg)

@router.get("/tariffs/review/{doc_id}", response_class=HTMLResponse)
async def review_page(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = db.query(TariffDocument).filter(TariffDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    from app.models import ReusableZoneResolver
    resolvers_db = db.query(ReusableZoneResolver).all()
    all_resolvers = []
    for r in resolvers_db:
        data = r.mapping_data or []
        zone_counts = {}
        for m in data:
            z = str(m.get("zone", ""))
            zone_counts[z] = zone_counts.get(z, 0) + 1
            
        all_resolvers.append({
            "id": r.id,
            "name": r.name,
            "totalRecords": len(data),
            "sample": data[:15],
            "zoneCounts": zone_counts,
            "keyType": data[0].get("key_type", "unknown") if data else "unknown",
            "assignedCountry": r.assigned_country
        })
        
    validation_results = validate_tariff_json(doc.raw_extraction_json or {})
    pre_approval_diff = get_pre_approval_diff(db, doc.raw_extraction_json or {}, doc.vendor_id)
    
    import pycountry
    import re
    
    # Fetch historical ZoneMapping suggestions
    zone_suggestions = {}
    auto_suggestions = {}
    
    def parse_countries_from_text(text: str) -> list:
        if not text: return []
        text = str(text)
        clean_str = re.sub(r'\(.*?\)', '', text).strip().upper()
        from app.rates.constants import COUNTRY_ALIASES
        for k, v in COUNTRY_ALIASES.items():
            clean_str = re.sub(rf'\b{k}\b', v.upper(), clean_str)
            
        found = []
        try:
            found = [pycountry.countries.lookup(clean_str).name]
        except LookupError:
            words = [w.strip() for w in re.split(r'[\s/&,|]+', clean_str) if w.strip()]
            i = 0
            while i < len(words):
                matched = False
                for length in [3, 2, 1]:
                    if i + length <= len(words):
                        phrase = ' '.join(words[i:i+length])
                        # Ignore 2/3-letter codes for generic words like "IN", "TO", "BY", "AND", etc.
                        if len(phrase) <= 3 and phrase.lower() in ["in", "to", "or", "an", "is", "at", "be", "it", "do", "as", "he", "we", "me", "by", "my", "no", "so", "am", "us", "of", "on", "if", "up", "go", "ok", "hi", "and", "are", "can", "for", "the", "any", "new", "all", "not", "out", "our", "per", "via", "rs", "kg", "kgs", "lb", "lbs", "oz", "inr", "usd", "eur", "gbp", "aud", "cad", "sgd", "aed"]:
                            continue
                        try:
                            c = pycountry.countries.lookup(phrase)
                            if c.name not in found:
                                found.append(c.name)
                            i += length
                            matched = True
                            break
                        except LookupError:
                            pass
                if not matched:
                    i += 1
        return found
    
    if doc.raw_extraction_json and "sections" in doc.raw_extraction_json:
        for s in doc.raw_extraction_json["sections"]:
            carrier = s.get("carrier")
            service = s.get("service")
            
            # Scan notes for potential zone mappings
            for n in s.get("notes", []):
                if n.get("scope") == "zone" and n.get("applies_to"):
                    extracted = parse_countries_from_text(n.get("text", ""))
                    if extracted: # Allow single-country zones
                        for z in n.get("applies_to"):
                            if z not in auto_suggestions:
                                auto_suggestions[z] = extracted
                            else:
                                existing = auto_suggestions[z]
                                auto_suggestions[z] = list(dict.fromkeys(existing + extracted))
            
            rates = s.get("rates", [])
            for r in rates:
                z = r.get("zone")
                if z:
                    if carrier and service:
                        key = f"{carrier}|{service}|{z}"
                        if key not in zone_suggestions:
                            # query db
                            mapping = db.query(ZoneMapping).filter(
                                ZoneMapping.carrier == carrier,
                                ZoneMapping.service == service,
                                ZoneMapping.zone_name == z
                            ).first()
                            if mapping:
                                zone_suggestions[key] = mapping.mapped_destinations
                                
                    if z not in auto_suggestions:
                        extracted = parse_countries_from_text(z)
                        if extracted:
                            auto_suggestions[z] = extracted
    vendor_sections = db.query(TariffSection.carrier, TariffSection.service, TariffDocument.uploaded_at, TariffDocument.original_filename)\
        .join(TariffDocument, TariffDocument.id == TariffSection.document_id)\
        .filter(TariffDocument.vendor_id == doc.vendor_id)\
        .filter(TariffDocument.status.in_(["APPROVED", "PARTIALLY_APPROVED"]))\
        .all()
        
    all_carriers = set()
    all_services = set()
    carrier_services = {}
    existing_services_info = {}
    
    for c, s, uploaded_at, filename in vendor_sections:
        if not c: continue
        all_carriers.add(c)
        
        safe_s = s or ""
        if safe_s:
            all_services.add(safe_s)
            if c not in carrier_services:
                carrier_services[c] = set()
            carrier_services[c].add(safe_s)
            
        key = f"{c}|{safe_s}".lower()
        if key not in existing_services_info or (uploaded_at and existing_services_info[key].get('raw_date') and uploaded_at > existing_services_info[key]['raw_date']):
            existing_services_info[key] = {
                "filename": filename or "Unknown",
                "date": uploaded_at.strftime("%Y-%m-%d %H:%M") if uploaded_at else "Unknown",
                "raw_date": uploaded_at
            }
                
    # Clean up raw_date before sending to frontend
    for v in existing_services_info.values():
        v.pop("raw_date", None)
        
    all_carriers = sorted(list(all_carriers))
    all_services = sorted(list(all_services))
    carrier_services = {k: sorted(list(v)) for k, v in carrier_services.items()}
    
    return templates.TemplateResponse(
        "rates/review_tariff.html",
        {
            "request": request, 
            "doc": doc,
            "raw_json": doc.raw_extraction_json,
            "validation": validation_results,
            "diff_data": pre_approval_diff,
            "zone_suggestions": zone_suggestions,
            "auto_suggestions": auto_suggestions,
            "all_resolvers": all_resolvers,
            "all_countries": [c.name for c in pycountry.countries],
            "all_carriers": all_carriers,
            "all_services": all_services,
            "carrier_services": carrier_services,
            "existing_services_info": existing_services_info
        }
    )

@router.post("/api/tariffs/{doc_id}/approve")
async def approve_tariff(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = db.query(TariffDocument).filter(TariffDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    data = await request.json()
    sections = data.get("sections", [])
    knowledge = data.get("knowledge", [])
    audit_log = data.get("audit_log", [])
    
    is_partial = data.get("partial", False)
    partial_indices = data.get("partial_indices", [])
    
    # Store audit log
    doc.audit_log = audit_log
    
    # Delete old normalized data if re-approving (only if not partial)
    if not is_partial:
        for existing_sec in doc.sections:
            db.delete(existing_sec)
        db.flush()
    
    # Normalization Layer
    from datetime import datetime
    
    # Pre-process: Extract all document-scoped notes so they can be applied to EVERY section
    global_notes = set()
    for s in sections:
        for note_obj in s.get("notes", []):
            if isinstance(note_obj, dict) and note_obj.get("scope") == "document":
                text = note_obj.get("text", "").strip()
                cat = note_obj.get("category", "INFO")
                if text:
                    global_notes.add((text, cat))
    
    # Pre-process: Extract and save all Reusable Zone Resolvers first
    from app.models import ReusableZoneResolver
    doc_resolvers = []
    for s in sections:
        for zm in s.get("zone_mappings", []):
            mapping_data = zm.get("mapping", [])
            if mapping_data:
                sheet_name = s.get("source", {}).get("source_name", "Unknown Sheet")
                resolver_name = f"{s.get('carrier') or 'Unknown'} {s.get('service') or ''} Zones (from {sheet_name})".strip()
                resolver = ReusableZoneResolver(
                    name=resolver_name,
                    carrier=s.get("carrier"),
                    service=s.get("service"),
                    mapping_data=mapping_data,
                    source_document_id=doc.id,
                    assigned_country=s.get("fallback_country").strip() if s.get("fallback_country") and str(s.get("fallback_country")).strip() else None
                )
                db.add(resolver)
                db.commit()
                doc_resolvers.append(resolver.id)
                s["_extracted_resolver_id"] = resolver.id

    for s in sections:
        valid_from_str = s.get("valid_from")
        valid_to_str = s.get("valid_to")
        valid_from = None
        valid_to = None
        
        try:
            if valid_from_str: valid_from = datetime.strptime(valid_from_str, "%Y-%m-%d").date()
            if valid_to_str: valid_to = datetime.strptime(valid_to_str, "%Y-%m-%d").date()
        except ValueError:
            pass
            
        # Determine the resolver for this section
        final_resolver_id = s.get("zone_resolver_id")
        
        # If the user selected a draft resolver from the same document
        if final_resolver_id and str(final_resolver_id).startswith("draft_"):
            try:
                draft_idx = int(str(final_resolver_id).split("_")[1])
                if 0 <= draft_idx < len(sections):
                    final_resolver_id = sections[draft_idx].get("_extracted_resolver_id")
            except Exception:
                pass
        elif final_resolver_id:
            # If the user explicitly provided a fallback country for this linked resolver during review, update it.
            if s.get("fallback_country") and str(s.get("fallback_country")).strip():
                existing_res = db.query(ReusableZoneResolver).filter(ReusableZoneResolver.id == final_resolver_id).first()
                if existing_res:
                    existing_res.assigned_country = str(s.get("fallback_country")).strip()
                    db.commit()
                
        if not final_resolver_id:
            if "_extracted_resolver_id" in s:
                final_resolver_id = s["_extracted_resolver_id"]
            elif len(doc_resolvers) == 1:
                # If there's exactly one resolver extracted in this entire document, safely auto-link it to all sheets!
                final_resolver_id = doc_resolvers[0]
            
        sec_record = TariffSection(
            document_id=doc.id,
            carrier=s.get("carrier"),
            service=s.get("service") or "",
            valid_from=valid_from,
            valid_to=valid_to,
            zone_resolver_id=final_resolver_id
        )
        db.add(sec_record)
        db.flush() # to get sec_record.id
        
        zone_segments_data = s.get("zone_segments", [])
        
        def get_price_type(zone_name, weight_val):
            for zs in zone_segments_data:
                if zs.get("zone") == zone_name:
                    for seg in zs.get("segments", []):
                        start = seg.get("start_weight")
                        end = seg.get("end_weight")
                        if start is not None and weight_val >= start:
                            if end is None or weight_val <= end:
                                return seg.get("price_type", "FLAT")
            return "FLAT"

        # Extract transit_days map from zone_segments
        transit_days_map = {}
        for zs in s.get("zone_segments", []):
            if zs.get("zone") and zs.get("transit_days"):
                transit_days_map[str(zs["zone"])] = zs["transit_days"]

        # Save confirmed zone mappings (these come from UI)
        confirmed_mappings = s.get("confirmed_zone_mappings", {})
        for z, destinations in confirmed_mappings.items():
            if not isinstance(destinations, list):
                continue
                
            t_days = transit_days_map.get(str(z))
            
            # update or create
            existing = db.query(ZoneMapping).filter(
                ZoneMapping.carrier == sec_record.carrier,
                ZoneMapping.service == sec_record.service,
                ZoneMapping.zone_name == z
            ).first()
            if existing:
                existing.mapped_destinations = destinations
                if t_days is not None:
                    existing.transit_days = t_days
            else:
                db.add(ZoneMapping(
                    carrier=sec_record.carrier,
                    service=sec_record.service,
                    zone_name=z,
                    mapped_destinations=destinations,
                    transit_days=t_days
                ))
        
        for r in s.get("rates", []):
            try:
                w_min = float(r.get("weight"))
                w_max = r.get("weight_max")
                w_max_val = float(w_max) if w_max is not None else None
                rate_record = TariffRateRow(
                    section_id=sec_record.id,
                    weight=w_min,
                    weight_max=w_max_val,
                    zone=r.get("zone"),
                    price=float(r.get("price")),
                    price_type=get_price_type(r.get("zone"), w_min),
                    source_ref=r.get("source_ref"),
                    import_status=r.get("import_status", "AUTO_APPROVED")
                )
                db.add(rate_record)
            except (ValueError, TypeError):
                # Skip invalid rows during final save
                continue
            
        saved_texts = set()
        for note_obj in s.get("notes", []):
            if isinstance(note_obj, str):
                # Backwards compatibility for old JSON
                if note_obj not in saved_texts:
                    note_record = TariffNote(section_id=sec_record.id, text=note_obj, category="INFO")
                    db.add(note_record)
                    saved_texts.add(note_obj)
                continue
                
            text = note_obj.get("text", "")
            cat = note_obj.get("category", "INFO")
            scope = note_obj.get("scope", "section")
            applies_to = note_obj.get("applies_to", [])
            
            if not text:
                continue
                
            if scope == "zone" and applies_to:
                for z in applies_to:
                    note_record = TariffNote(
                        section_id=sec_record.id,
                        text=text,
                        zone=z,
                        category=cat
                    )
                    db.add(note_record)
            else:
                # Document or section level
                if text not in saved_texts:
                    note_record = TariffNote(
                        section_id=sec_record.id,
                        text=text,
                        category=cat
                    )
                    db.add(note_record)
                    saved_texts.add(text)
                    
        # Apply any document-scoped notes that weren't in this section's array
        for g_text, g_cat in global_notes:
            if g_text not in saved_texts:
                db.add(TariffNote(section_id=sec_record.id, text=g_text, category=g_cat))
                saved_texts.add(g_text)
            
    # Handle status transition and persistence of imported state
    from sqlalchemy.orm.attributes import flag_modified
    if doc.raw_extraction_json:
        raw_json = doc.raw_extraction_json
        
        if not is_partial:
            # Mark all as imported
            for s in raw_json.get("sections", []):
                s["_import_status"] = "IMPORTED"
            doc.status = "APPROVED"
        else:
            # Mark specific as imported
            for idx in partial_indices:
                if 0 <= idx < len(raw_json.get("sections", [])):
                    raw_json["sections"][idx]["_import_status"] = "IMPORTED"
                    
            # Check if all are now imported
            all_imported = all(s.get("_import_status") == "IMPORTED" for s in raw_json.get("sections", []))
            if all_imported:
                doc.status = "APPROVED"
            else:
                doc.status = "PARTIALLY_APPROVED"
                
        doc.raw_extraction_json = raw_json
        flag_modified(doc, "raw_extraction_json")
    else:
        doc.status = "PARTIALLY_APPROVED" if is_partial else "APPROVED"

    db.commit()
    
    return {"success": True, "message": "Tariff processed successfully.", "doc_status": doc.status}

@router.get("/api/tariffs/search")
async def search_tariffs_api(q: str, db: Session = Depends(get_db)):
    if not q:
        return {"results": []}
    results = search_tariffs(db, q)
    return {"results": results}

@router.get("/api/tariffs/quote")
async def get_tariff_quote(weight: float, zone: str, db: Session = Depends(get_db)):
    quotes = get_best_rates(db, weight, zone)
    return {"quotes": quotes}

@router.get("/tariffs/test-quotes", response_class=HTMLResponse)
async def test_quotes_page(request: Request):
    return templates.TemplateResponse("rates/quote_test.html", {"request": request})

@router.get("/tariffs/diff/{doc_id}", response_class=HTMLResponse)
async def diff_page(doc_id: str, request: Request, db: Session = Depends(get_db)):
    diff_data = get_tariff_diff(db, doc_id)
    if not diff_data:
        raise HTTPException(status_code=404, detail="Document not found")
    diffs = diff_data["diffs"]
    
    matrices = {}
    for d in diffs:
        svc_key = f"{d['carrier']} - {d['service']}"
        if svc_key not in matrices:
            matrices[svc_key] = {
                "zones": set(),
                "weights": set(),
                "data": {},
                "has_changes": False,
                "is_completely_new": True
            }
        
        matrices[svc_key]["zones"].add(d["zone"])
        matrices[svc_key]["weights"].add(d["weight"])
        
        if "section_id" not in matrices[svc_key] and d.get("section_id"):
            matrices[svc_key]["section_id"] = d["section_id"]
        
        w = d["weight"]
        z = d["zone"]
        
        if w not in matrices[svc_key]["data"]:
            matrices[svc_key]["data"][w] = {}
            
        matrices[svc_key]["data"][w][z] = d
        
        if d["status"] != "UNCHANGED":
            matrices[svc_key]["has_changes"] = True
            
        if d["status"] != "NEW":
            matrices[svc_key]["is_completely_new"] = False

    for svc, m in matrices.items():
        m["zones"] = sorted(list(m["zones"]))
        m["weights"] = sorted(list(m["weights"]))
        
        pricing_models = {}
        for z in m["zones"]:
            segments = []
            current_type = None
            current_start = None
            last_w = None
            
            for w in m["weights"]:
                cell = m["data"][w].get(z)
                if cell:
                    ptype = cell.get("price_type", "FLAT")
                    w_max = cell.get("weight_max")
                    display_w = w_max if w_max is not None else w
                    
                    if ptype != current_type:
                        if current_type is not None:
                            segments.append({"start": current_start, "end": last_w, "type": current_type})
                        current_type = ptype
                        current_start = w
                    last_w = display_w
            
            if current_type is not None:
                segments.append({"start": current_start, "end": last_w, "type": current_type})
            
            if segments:
                pricing_models[z] = segments
                
        m["pricing_models"] = pricing_models
        
    # Fetch resolvers linked to this document
    from app.models import ReusableZoneResolver, ZoneMapping, TariffDocument
    resolvers = {} # Keyed by section_id now
    for s in diff_data["new_doc"].sections:
        if s.zone_resolver_id:
            r_db = db.query(ReusableZoneResolver).filter(ReusableZoneResolver.id == s.zone_resolver_id).first()
            if r_db:
                source_doc_name = None
                if r_db.source_document_id and r_db.source_document_id != doc_id:
                    src_doc = db.query(TariffDocument).filter(TariffDocument.id == r_db.source_document_id).first()
                    if src_doc:
                        source_doc_name = src_doc.vendor.name if src_doc.vendor else "Another Document"
                
                summary_counts = {}
                for m in (r_db.mapping_data or []):
                    z = str(m.get("zone", "Unknown"))
                    summary_counts[z] = summary_counts.get(z, 0) + 1
                summary_str = " | ".join(f"{k} ➔ {v} records" for k, v in summary_counts.items())
                if not summary_str: summary_str = "No records"
                
                resolvers[s.id] = {"db": r_db, "resolver_id": r_db.id, "source_doc_name": source_doc_name, "source_doc_id": r_db.source_document_id, "summary": summary_str}
                
    # Fetch all resolvers for dropdown
    all_resolvers_db = db.query(ReusableZoneResolver).all()
    all_resolvers_list = []
    for r in all_resolvers_db:
        summary_counts = {}
        for m in (r.mapping_data or []):
            z = str(m.get("zone", "Unknown"))
            summary_counts[z] = summary_counts.get(z, 0) + 1
        summary_str = " | ".join(f"{k} ➔ {v} records" for k, v in summary_counts.items())
        if not summary_str: summary_str = "No records"
        all_resolvers_list.append({"id": r.id, "name": r.name, "summary": summary_str})

    # Fetch global ZoneMappings for the matrices
    global_mappings = {}
    for svc, m in matrices.items():
        svc_parts = svc.split(" - ", 1)
        if len(svc_parts) == 2:
            carr, serv = svc_parts[0], svc_parts[1]
            global_mappings[svc] = {}
            for z in m["zones"]:
                zm = db.query(ZoneMapping).filter(
                    ZoneMapping.carrier == carr,
                    ZoneMapping.service == serv,
                    ZoneMapping.zone_name == z
                ).first()
                if zm:
                    global_mappings[svc][z] = {
                        "id": zm.id,
                        "destinations": zm.mapped_destinations
                    }

    # Fetch resolvers extracted from this document
    extracted_resolvers = db.query(ReusableZoneResolver).filter(
        ReusableZoneResolver.source_document_id == doc_id
    ).all()
        
    return templates.TemplateResponse(
        "rates/diff_tariff.html",
        {
            "request": request, 
            "doc": diff_data["new_doc"],
            "old_doc": diff_data["old_doc"],
            "diffs": diffs,
            "matrices": matrices,
            "resolvers": resolvers,
            "all_resolvers": all_resolvers_list,
            "global_mappings": global_mappings,
            "extracted_resolvers": extracted_resolvers,
            "all_countries": [c.name for c in __import__("pycountry").countries]
        }
    )

class RateUpdateRequest(BaseModel):
    price: float

@router.put("/api/tariffs/rates/{rate_id}")
async def update_tariff_rate(rate_id: str, req: RateUpdateRequest, db: Session = Depends(get_db)):
    from app.models import TariffRateRow
    rate = db.query(TariffRateRow).filter(TariffRateRow.id == rate_id).first()
    if not rate:
        raise HTTPException(status_code=404, detail="Rate not found")
    rate.price = req.price
    db.commit()
    return {"success": True}

class ResolverUpdateRequest(BaseModel):
    mapping_data: list
    assigned_country: Optional[str] = None

@router.put("/api/tariffs/resolvers/{resolver_id}")
async def update_resolver(resolver_id: str, req: ResolverUpdateRequest, db: Session = Depends(get_db)):
    from app.models import ReusableZoneResolver
    resolver = db.query(ReusableZoneResolver).filter(ReusableZoneResolver.id == resolver_id).first()
    if not resolver:
        raise HTTPException(status_code=404, detail="Resolver not found")
    resolver.mapping_data = req.mapping_data
    if req.assigned_country is not None:
        resolver.assigned_country = req.assigned_country.strip() if req.assigned_country.strip() else None
    db.commit()
    return {"success": True}

class ResolverCountryRequest(BaseModel):
    assigned_country: str

@router.post("/api/tariffs/resolvers/{resolver_id}/country")
async def update_resolver_country(resolver_id: str, req: ResolverCountryRequest, db: Session = Depends(get_db)):
    from app.models import ReusableZoneResolver
    resolver = db.query(ReusableZoneResolver).filter(ReusableZoneResolver.id == resolver_id).first()
    if not resolver:
        raise HTTPException(status_code=404, detail="Resolver not found")
    resolver.assigned_country = req.assigned_country.strip() if req.assigned_country and req.assigned_country.strip() else None
    db.commit()
    return {"success": True}

@router.get("/tariffs/resolvers/{resolver_id}", response_class=HTMLResponse)
async def resolver_edit_page(resolver_id: str, request: Request, db: Session = Depends(get_db)):
    from app.models import ReusableZoneResolver
    resolver = db.query(ReusableZoneResolver).filter(ReusableZoneResolver.id == resolver_id).first()
    if not resolver:
        raise HTTPException(status_code=404, detail="Resolver not found")
    return templates.TemplateResponse("rates/resolver_edit.html", {
        "request": request, 
        "resolver": resolver,
        "all_countries": [c.name for c in __import__("pycountry").countries]
    })

class ZoneMappingUpdateRequest(BaseModel):
    destinations: list

@router.put("/api/tariffs/zonemappings/{mapping_id}")
async def update_zonemapping(mapping_id: int, req: ZoneMappingUpdateRequest, db: Session = Depends(get_db)):
    mapping = db.query(ZoneMapping).filter(ZoneMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found")
    mapping.mapped_destinations = req.destinations
    db.commit()
    return {"success": True}

class AttachResolverRequest(BaseModel):
    resolver_id: Optional[str] = None

@router.post("/api/tariffs/sections/{section_id}/resolver")
async def attach_resolver(section_id: str, req: AttachResolverRequest, db: Session = Depends(get_db)):
    from app.models import TariffSection, ReusableZoneResolver
    
    # Get the target section
    section = db.query(TariffSection).filter(TariffSection.id == section_id).first()
    if not section:
        raise HTTPException(status_code=404, detail="Section not found")
        
    if req.resolver_id:
        resolver = db.query(ReusableZoneResolver).filter(ReusableZoneResolver.id == req.resolver_id).first()
        if not resolver:
             raise HTTPException(status_code=404, detail="Resolver not found")
             
    # Find all sections in the same document with the same carrier/service
    matching_sections = db.query(TariffSection).filter(
        TariffSection.document_id == section.document_id,
        TariffSection.carrier == section.carrier,
        TariffSection.service == section.service
    ).all()
    
    # Update all of them so the UI matrix (which merges them) stays consistent
    for s in matching_sections:
        s.zone_resolver_id = req.resolver_id or None
        
    db.commit()
    return {"success": True}
