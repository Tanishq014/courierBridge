import os
import time
import shutil
import uuid
import json
from fastapi import APIRouter, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import Vendor, TariffDocument, TariffSection, TariffRateRow, TariffNote, ZoneMapping
from app.rates.ai import extract_rates_from_document
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

@router.post("/api/tariffs/upload")
async def upload_tariff(
    vendor_id: str = Form(...),
    temp_file_id: str = Form(...),
    original_filename: str = Form(...),
    selected_sheets: Optional[str] = Form(None),
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
            status="DRAFT",
            prompt_version="v1"
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        
        sheets_list = None
        if selected_sheets:
            sheets_list = json.loads(selected_sheets)
            
        # Call Gemini Flash AI Pipeline
        raw_json = await extract_rates_from_document(file_path, original_filename, allowed_sheets=sheets_list)
        
        # Save raw extraction
        doc.raw_extraction_json = raw_json
        db.commit()
        
        return {"success": True, "document_id": doc.id, "message": "File processed successfully."}
        
    except Exception as e:
        import traceback
        err_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc()}"
        raise HTTPException(status_code=500, detail=err_msg)

@router.get("/tariffs/review/{doc_id}", response_class=HTMLResponse)
async def review_page(doc_id: str, request: Request, db: Session = Depends(get_db)):
    doc = db.query(TariffDocument).filter(TariffDocument.id == doc_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
        
    validation_results = validate_tariff_json(doc.raw_extraction_json or {})
    pre_approval_diff = get_pre_approval_diff(db, doc.raw_extraction_json or {}, doc.vendor_id)
    
    import pycountry
    import re
    
    # Fetch historical ZoneMapping suggestions
    zone_suggestions = {}
    auto_suggestions = {}
    
    if doc.raw_extraction_json and "sections" in doc.raw_extraction_json:
        for s in doc.raw_extraction_json["sections"]:
            carrier = s.get("carrier")
            service = s.get("service")
            
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
                        clean_str = re.sub(r'\(.*?\)', '', z).strip().upper()
                        
                        # Apply Aliases
                        aliases = {
                            'UK': 'United Kingdom',
                            'USA': 'United States',
                            'UAE': 'United Arab Emirates',
                            'ROI': 'Ireland'
                        }
                        for k, v in aliases.items():
                            clean_str = re.sub(rf'\b{k}\b', v.upper(), clean_str)
                            
                        found = []
                        
                        try:
                            found = [pycountry.countries.lookup(clean_str).name]
                        except LookupError:
                            # Heuristic: try splitting by spaces and checking combinations (up to 3 words)
                            words = [w.strip() for w in re.split(r'[\s/&,]+', clean_str) if w.strip()]
                            i = 0
                            while i < len(words):
                                matched = False
                                for length in [3, 2, 1]:
                                    if i + length <= len(words):
                                        phrase = ' '.join(words[i:i+length])
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
                                    
                        if found:
                            auto_suggestions[z] = found
    
    return templates.TemplateResponse(
        "rates/review_tariff.html",
        {
            "request": request, 
            "doc": doc,
            "raw_json": doc.raw_extraction_json,
            "validation": validation_results,
            "diff_data": pre_approval_diff,
            "zone_suggestions": zone_suggestions,
            "auto_suggestions": auto_suggestions
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
            
        sec_record = TariffSection(
            document_id=doc.id,
            carrier=s.get("carrier"),
            service=s.get("service") or "",
            valid_from=valid_from,
            valid_to=valid_to
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

        # Save confirmed zone mappings (these come from UI)
        confirmed_mappings = s.get("confirmed_zone_mappings", {})
        for z, destinations in confirmed_mappings.items():
            if not isinstance(destinations, list):
                continue
            # update or create
            existing = db.query(ZoneMapping).filter(
                ZoneMapping.carrier == sec_record.carrier,
                ZoneMapping.service == sec_record.service,
                ZoneMapping.zone_name == z
            ).first()
            if existing:
                existing.mapped_destinations = destinations
            else:
                db.add(ZoneMapping(
                    carrier=sec_record.carrier,
                    service=sec_record.service,
                    zone_name=z,
                    mapped_destinations=destinations
                ))
        
        for r in s.get("rates", []):
            try:
                rate_record = TariffRateRow(
                    section_id=sec_record.id,
                    weight=float(r.get("weight")),
                    zone=r.get("zone"),
                    price=float(r.get("price")),
                    price_type=get_price_type(r.get("zone"), float(r.get("weight"))),
                    source_ref=r.get("source_ref"),
                    import_status=r.get("import_status", "AUTO_APPROVED")
                )
                db.add(rate_record)
            except (ValueError, TypeError):
                # Skip invalid rows during final save
                continue
            
        for note_obj in s.get("notes", []):
            if isinstance(note_obj, str):
                # Backwards compatibility for old JSON
                note_record = TariffNote(section_id=sec_record.id, text=note_obj)
                db.add(note_record)
                continue
                
            text = note_obj.get("text", "")
            scope = note_obj.get("scope", "section")
            applies_to = note_obj.get("applies_to", [])
            
            if not text:
                continue
                
            if scope == "zone" and applies_to:
                for z in applies_to:
                    note_record = TariffNote(
                        section_id=sec_record.id,
                        text=text,
                        zone=z
                    )
                    db.add(note_record)
            else:
                # Document or section level
                note_record = TariffNote(
                    section_id=sec_record.id,
                    text=text
                )
                db.add(note_record)
            
    # Process Knowledge base updates
    for k in knowledge:
        if k.get("type") == "zone_mapping":
            c_carrier = k.get("carrier") or ""
            c_service = k.get("service") or ""
            c_zone = k.get("zone")
            c_countries = k.get("countries", [])
            
            if not c_zone or not c_countries:
                continue
                
            existing_zm = db.query(ZoneMapping).filter(
                ZoneMapping.carrier == c_carrier,
                ZoneMapping.service == c_service,
                ZoneMapping.zone_name == c_zone
            ).first()
            
            # Since the user clicked approve, we accept the AI's latest mapping
            if existing_zm:
                existing_zm.mapped_destinations = list(set(c_countries))
            else:
                new_zm = ZoneMapping(
                    carrier=c_carrier,
                    service=c_service,
                    zone_name=c_zone,
                    mapped_destinations=list(set(c_countries))
                )
                db.add(new_zm)
                
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
