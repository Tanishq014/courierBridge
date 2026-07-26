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
                sheets = [sheet.title for sheet in wb.worksheets if sheet.sheet_state == 'visible']
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
            ai_context=ai_context
        )
        
        # Phase 3: Hybrid Python Deterministic Parser (for massive zone tables)
        raw_json = process_deterministic_zones(file_path, raw_json)
        
        # Save raw extraction
        doc.raw_extraction_json = raw_json
        db.commit()
        
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
            "keyType": data[0].get("key_type", "unknown") if data else "unknown"
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
                        if len(phrase) <= 3 and phrase.lower() in ["in", "to", "or", "an", "is", "at", "be", "it", "do", "as", "he", "we", "me", "by", "my", "no", "so", "am", "us", "of", "on", "if", "up", "go", "ok", "hi", "and", "are", "can", "for", "the", "any", "new", "all", "not", "out", "our", "per", "via"]:
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
            "all_countries": [c.name for c in pycountry.countries]
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
    
    # Store audit log
    doc.audit_log = audit_log
    
    # Delete old normalized data if re-approving
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
                    mapping_data=mapping_data
                )
                db.add(resolver)
                db.flush()
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
            
    doc.status = "APPROVED"
    db.commit()
    
    return {"success": True, "message": "Tariff approved and normalized successfully."}

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
        
    return templates.TemplateResponse(
        "rates/diff_tariff.html",
        {
            "request": request, 
            "doc": diff_data["new_doc"],
            "old_doc": diff_data["old_doc"],
            "diffs": diff_data["diffs"]
        }
    )
