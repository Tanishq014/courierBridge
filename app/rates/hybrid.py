import openpyxl
import logging

logger = logging.getLogger(__name__)

def process_deterministic_zones(file_path: str, raw_json: dict):
    """
    Phase 3: Python Deterministic Parser.
    Intercepts zone_mappings with mode: "PYTHON" and extracts data deterministically.
    """
    if not raw_json or not raw_json.get("sections"):
        return raw_json
        
    # Check if we need to open the workbook (only if at least one PYTHON mode exists)
    needs_python = False
    for sec in raw_json["sections"]:
        for zm in sec.get("zone_mappings", []):
            if zm.get("mode") == "PYTHON":
                needs_python = True
                break
                
    if not needs_python:
        return raw_json
        
    logger.info(f"Hybrid parser triggered for {file_path}")
    
    try:
        # data_only=True gets values instead of formulas
        wb = openpyxl.load_workbook(file_path, data_only=True)
    except Exception as e:
        logger.error(f"Failed to load workbook for hybrid parsing: {e}")
        return raw_json
        
    for section in raw_json["sections"]:
        source = section.get("source", {})
        if source.get("source_type") != "sheet":
            continue
            
        sheet_name = source.get("source_name", "")
        if not sheet_name:
            continue
            
        # ai.py injects the exact sheet name now
        actual_sheet = None
        if sheet_name in wb.sheetnames:
            actual_sheet = sheet_name
                
        if not actual_sheet:
            logger.warning(f"Could not find exact sheet '{sheet_name}' in workbook")
            continue
            
        sheet = wb[actual_sheet]
        
        zone_mappings = section.get("zone_mappings", [])
        for zm in zone_mappings:
            if zm.get("mode") != "PYTHON":
                continue
                
            bounds = zm.get("bounds", {})
            roles = zm.get("column_roles", {})
            
            top = bounds.get("top")
            left_col = bounds.get("left")
            right_col = bounds.get("right")
            
            if not top or not left_col or not right_col or not roles:
                logger.warning(f"Invalid bounds/roles for PYTHON mode extraction in sheet {sheet_name}")
                continue
                
            try:
                left_idx = openpyxl.utils.column_index_from_string(left_col)
                right_idx = openpyxl.utils.column_index_from_string(right_col)
            except Exception as e:
                logger.error(f"Failed to parse column bounds: {e}")
                continue
                
            extracted_mapping = []
            consecutive_empty_rows = 0
            max_row = sheet.max_row or 1048576
            
            min_col_idx = min(openpyxl.utils.column_index_from_string(c) for c in roles.keys())
            start_role = roles[openpyxl.utils.get_column_letter(min_col_idx)].lower()
            
            for row_idx in range(top, max_row + 1):
                has_valid_record = False
                current_record = {}
                is_empty_row = True
                
                # Unroll dense grids dynamically
                for col_idx in range(left_idx, right_idx + 1):
                    col_letter = openpyxl.utils.get_column_letter(col_idx)
                    if col_letter not in roles:
                        continue
                        
                    cell = sheet.cell(row=row_idx, column=col_idx)
                    val = cell.value
                    role = roles[col_letter].lower()

                    
                    if role == start_role and current_record:
                        if "zone" in current_record:
                            zone_val = current_record.pop("zone")
                            
                            note_val = current_record.pop("note", None)
                            for note_alias in ["notes", "remark", "remarks", "no_service", "service"]:
                                if note_alias in current_record:
                                    val = current_record.pop(note_alias)
                                    if not note_val:
                                        note_val = val
                                        
                            for k_type, k_val in current_record.items():
                                if k_type and k_val:
                                    record = {
                                        "key_type": k_type,
                                        "key": k_val,
                                        "zone": zone_val
                                    }
                                    if note_val:
                                        record["note"] = note_val
                                    extracted_mapping.append(record)
                                    has_valid_record = True
                        current_record = {}
                        
                    if val is not None and str(val).strip() != "":
                        val_str = str(val).strip()
                        current_record[role] = val_str
                        is_empty_row = False
                        
                # End of row commit
                if current_record and "zone" in current_record:
                    zone_val = current_record.pop("zone")
                    
                    # Extract any non-key metadata (notes, remarks)
                    note_val = current_record.pop("note", None)
                    for note_alias in ["notes", "remark", "remarks", "no_service", "service"]:
                        if note_alias in current_record:
                            val = current_record.pop(note_alias)
                            if not note_val:
                                note_val = val
                                
                    added_any = False
                    for k_type, k_val in current_record.items():
                        if k_type and k_val: # e.g. k_type="postcode", k_val="3000"
                            record = {
                                "key_type": k_type,
                                "key": k_val,
                                "zone": zone_val
                            }
                            if note_val:
                                record["note"] = note_val
                            extracted_mapping.append(record)
                            added_any = True
                    
                    if added_any:
                        has_valid_record = True
                    
                # Termination logic
                if is_empty_row:
                    consecutive_empty_rows += 1
                else:
                    consecutive_empty_rows = 0
                    
                # Stop if we hit 20 completely blank rows in a row
                if consecutive_empty_rows >= 20:
                    break
                    
            # Update the JSON payload
            zm["mapping"] = extracted_mapping
            # Don't change mode to AI, so the UI still knows it was a PYTHON-extracted massive dataset.
            
    return raw_json
