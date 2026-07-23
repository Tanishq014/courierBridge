import os
import json
import json_repair
import base64
import httpx
from typing import Dict, Any, List
import csv
from io import StringIO
import openpyxl
import datetime
import time
import asyncio
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
MODEL_NAME = "gemini-3.6-flash"
GEMINI_MAX_CONCURRENT = int(os.environ.get("GEMINI_MAX_CONCURRENT", "3"))

PROMPT_V1 = """
You are an expert courier tariff data extractor.
Your job is to read a semantic JSON representation of a document and extract the tariff data.
The JSON provides 'merged_regions' and contiguous data 'blocks' (tables, titles, and notes).

BUSINESS CONTEXT:
This extracted data will power a courier quoting engine. The quoting engine needs to calculate and compare shipping prices across different vendors (like FedEx, DHL). 
It is CRITICAL that Terms and Conditions (T&Cs) are correctly categorized because some rules apply to the entire document, some apply only to a specific zone, and some apply only to a specific country. If a zone-specific or country-specific rule is extracted as a document-wide rule, the quoting engine will show incorrect rules to the customer!

CRITICAL EXTRACTION STRATEGY:
Step 1: Identify Document Structure
- Scan the blocks to locate Titles, the main Data Tables, and Footer Notes.
- Use `merged_regions` to understand if a header spans multiple columns (e.g. if C2:D2 is merged, the header in C2 applies to columns C and D).

Step 2: Parse Tables
- Find the rate matrices. Identify the Weight column and the Destination Zone headers.
- HORIZONTAL MATRICES: If you encounter a horizontal matrix where Countries/Zones are listed down the rows, and Weights are listed across the columns (e.g., 6, 8, 11+ as headers), you MUST transpose this data mentally. Our schema strictly requires `weight` to be the numeric weight and `zone` to be the country/region string. DO NOT output a zone name as a weight. Extract the data by mapping the column weight header to the row zone name.
- CONDITIONAL ZONES / MODIFIERS: If you encounter columns for the same zone but with different conditions (e.g., "Australia (With Food)" vs "Australia (Without Food)"), DO NOT merge this condition into the zone name. Instead, extract them as completely separate `sections` with different `service` names (e.g., Section 1: Service="With Food", Zone="Australia"; Section 2: Service="Without Food", Zone="Australia").

Step 3: Parse Notes & Rules
- Locate terms and conditions (T&Cs).
- If T&Cs appear directly below a specific column or are merged under a specific destination/zone, associate them ONLY with that destination/zone by setting scope='zone' and applies_to=[zone_name].
- If T&Cs span the full width of the table or are globally applicable, they belong to the 'section' or 'document' scope.
- If you find text defining which countries belong to a specific zone, put it in `notes` with `scope="zone"` and `applies_to=["Zone Name"]`. YOU MUST FORMAT the `text` of this note exactly as a pipe-separated list of countries (e.g., "Belgium|Denmark|France"). Do not include prefixes like "Countries:" or the zone name in the text itself.

Step 4: Output JSON
- Extract every single rate row. Do not summarize.
- Convert weights to KG. If there is a weight bracket (e.g., "21-30" or "0.5-2.5"), extract it EXACTLY as the string "21-30". Do NOT try to manually expand it. Our backend will handle the expansion.
- Determine pricing models (FLAT vs PER_KG). Because pricing models can change per zone and per weight bracket (e.g. 1-10kg is FLAT, 11+ is PER_KG), extract a `zone_segments` array for each section. Determine this from explicit evidence in the document (headers, table titles, notes such as "Per Kg", "Rate/Kg", "Additional Kg"). If there is no clear evidence, return "UNKNOWN" rather than guessing.
- Return the EXACT schema below.

JSON SCHEMA:
{
  "sections": [
    {
      "carrier": "Carrier name (e.g. FedEx, DHL)",
      "service": "Service name (e.g. IP, Express). If not explicitly stated, leave it empty (\"\"), do not guess or hallucinate.",
      "valid_from": "YYYY-MM-DD (if found, else null)",
      "valid_to": "YYYY-MM-DD (if found, else null)",
      "zone_segments": [
        {
          "zone": "Zone name (e.g. Zone 1, USA)",
          "segments": [
            {
              "start_weight": numeric (e.g. 1),
              "end_weight": numeric or null (e.g. 10 or null),
              "price_type": "FLAT|PER_KG|UNKNOWN",
              "confidence": numeric (0.0 to 1.0),
              "reason": "AI explanation for this segment"
            }
          ]
        }
      ],
      "rates": [
        {
          "weight": "Numeric weight OR string bracket (e.g. '21-30')",
          "zone": "Zone identifier",
          "price": "Numeric price (e.g. 15.50)",
          "source_ref": {"sheet": "SheetName", "row": 15},
          "confidence": numeric (0.0 to 1.0)
        }
      ],
      "notes": [
        {
          "text": "Note text",
          "scope": "document|section|zone",
          "applies_to": ["Zone Name"]
        }
      ]
    }
  ]
}

Ensure your output is just raw JSON, with no markdown formatting or backticks around it.
"""

def get_excel_extraction_units(file_path: str, allowed_sheets: List[str] = None) -> List[Dict[str, Any]]:
    """Reads an Excel file and converts it into a list of extraction units (one per sheet)."""
    wb = openpyxl.load_workbook(file_path, data_only=True)
    units = []
    
    for sheet_name in wb.sheetnames:
        if allowed_sheets is not None and sheet_name not in allowed_sheets:
            continue
        sheet = wb[sheet_name]
        
        sheet_data = {
            "sheet_name": sheet_name,
            "merged_regions": [str(r) for r in sheet.merged_cells.ranges],
            "blocks": []
        }
        
        max_r = min(sheet.max_row, 2000) if sheet.max_row else 2000
        max_c = min(sheet.max_column, 50) if sheet.max_column else 50
        
        current_block_rows = []
        block_start_r = None
        
        for row_idx in range(1, max_r + 1):
            row_cells = sheet[row_idx][:max_c]
            
            row_vals = []
            has_data = False
            for cell in row_cells:
                val = cell.value
                if val is not None and str(val).strip() != "":
                    has_data = True
                    if isinstance(val, (datetime.datetime, datetime.date)):
                        if val.day == 1:
                            val = f"{val.month}-{str(val.year)[-2:]}"
                        else:
                            val = f"{val.day}-{val.month}"
                    elif isinstance(val, float):
                        val = int(round(val))
                    row_vals.append(str(val).strip())
                else:
                    row_vals.append(None)
                    
            if has_data:
                while row_vals and row_vals[-1] is None:
                    row_vals.pop()
                    
                if not current_block_rows:
                    block_start_r = row_idx
                current_block_rows.append(row_vals)
            else:
                if current_block_rows:
                    max_len = max(len(r) for r in current_block_rows)
                    start_col = openpyxl.utils.get_column_letter(1)
                    end_col = openpyxl.utils.get_column_letter(max_len)
                    end_r = block_start_r + len(current_block_rows) - 1
                    
                    sheet_data["blocks"].append({
                        "range": f"{start_col}{block_start_r}:{end_col}{end_r}",
                        "rows": current_block_rows
                    })
                    current_block_rows = []
                    block_start_r = None
                    
        if current_block_rows:
            max_len = max(len(r) for r in current_block_rows)
            start_col = openpyxl.utils.get_column_letter(1)
            end_col = openpyxl.utils.get_column_letter(max_len)
            end_r = block_start_r + len(current_block_rows) - 1
            sheet_data["blocks"].append({
                "range": f"{start_col}{block_start_r}:{end_col}{end_r}",
                "rows": current_block_rows
            })
            
        units.append({
            "source_type": "sheet",
            "source_name": sheet_name,
            "data": sheet_data
        })
        
    return units

async def call_gemini_api_with_retries(parts: List[Dict], unit_name: str) -> Dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json"
        }
    }
    
    max_retries = 3
    retry_count = 0
    start_time = time.time()
    
    async with httpx.AsyncClient(timeout=300.0) as client:
        while retry_count <= max_retries:
            try:
                response = await client.post(url, json=body)
                
                # Retry on rate limits (429) and server errors (500+)
                if response.status_code == 429 or response.status_code >= 500:
                    retry_count += 1
                    if retry_count > max_retries:
                        raise RuntimeError(f"Gemini API Error after retries: {response.status_code} {response.text}")
                    await asyncio.sleep(2 ** retry_count) # Exponential backoff
                    continue
                    
                # Do NOT retry on 400 bad requests
                if response.status_code != 200:
                    raise RuntimeError(f"Gemini API Error: {response.status_code} {response.text}")
                    
                data = response.json()
                
                # Parse usage metrics
                usage = data.get("usageMetadata", {})
                prompt_tokens = usage.get("promptTokenCount", 0)
                resp_tokens = usage.get("candidatesTokenCount", 0)
                
                text_content = ""
                for candidate in data.get("candidates", []):
                    for part in candidate.get("content", {}).get("parts", []):
                        text_content += part.get("text", "")
                        
                # Clean markdown
                text_content = text_content.strip()
                if text_content.startswith("```json"): text_content = text_content[7:]
                if text_content.startswith("```"): text_content = text_content[3:]
                if text_content.endswith("```"): text_content = text_content[:-3]
                text_content = text_content.strip()
                
                raw_json = {}
                try:
                    raw_json = json_repair.loads(text_content)
                    if not isinstance(raw_json, dict):
                        # In case json_repair returns a string or list when a dict was expected
                        import re
                        match = re.search(r'\{.*\}', text_content, re.DOTALL)
                        if match:
                            raw_json = json_repair.loads(match.group(0))
                            
                    # Expand string brackets and deduplicate rates by weight and zone
                    if isinstance(raw_json, dict) and "sections" in raw_json:
                        for s in raw_json["sections"]:
                            if "rates" in s:
                                expanded_rates = []
                                for r in s["rates"]:
                                    w = r.get("weight")
                                    if isinstance(w, str) and "-" in w:
                                        import re
                                        # Parse e.g. "21-30" or "0.5-2.5" or "21-30 kg"
                                        nums = re.findall(r'\d+(?:\.\d+)?', w)
                                        if len(nums) == 2:
                                            start = float(nums[0])
                                            end = float(nums[1])
                                            step = 0.5 if "." in w else 1.0
                                            current = start
                                            while current <= end:
                                                new_r = dict(r)
                                                new_r["weight"] = current
                                                expanded_rates.append(new_r)
                                                current += step
                                        else:
                                            try:
                                                r["weight"] = float(w.replace("kg", "").replace("KG", "").replace("+", "").strip())
                                                expanded_rates.append(r)
                                            except ValueError:
                                                pass # drop invalid
                                    else:
                                        try:
                                            r["weight"] = float(str(w).replace("kg", "").replace("KG", "").replace("+", "").strip())
                                            expanded_rates.append(r)
                                        except (ValueError, TypeError):
                                            pass
                                
                                unique_rates = {}
                                for r in expanded_rates:
                                    w = r.get("weight")
                                    z = r.get("zone")
                                    if w is not None and z is not None:
                                        key = f"{z}_{w}"
                                        unique_rates[key] = r
                                s["rates"] = list(unique_rates.values())
                                
                except Exception as e:
                    logger.error(f"Failed to parse JSON using json_repair: {e}")
                    raise RuntimeError(f"Failed to parse JSON from AI response: {e}")
                        
                latency = time.time() - start_time
                logger.info(f"[Metrics - {unit_name}] Prompt Tokens: {prompt_tokens}, Resp Tokens: {resp_tokens}, Latency: {latency:.2f}s, Retries: {retry_count}, Status: SUCCESS")
                return raw_json
                
            except (httpx.RequestError, httpx.TimeoutException) as e:
                # Retry on transient network errors
                retry_count += 1
                if retry_count > max_retries:
                    logger.error(f"[Metrics - {unit_name}] Status: FAILED after {retry_count} retries. Error: {str(e)}")
                    raise RuntimeError(f"Network error calling Gemini: {str(e)}")
                await asyncio.sleep(2 ** retry_count)
                
    return {}

async def extract_rates_from_document(file_path: str, filename: str, allowed_sheets: List[str] = None) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set.")

    start_time = time.time()
    ext = os.path.splitext(filename)[1].lower()
    
    master_json = {"sections": []}
    all_source_text = ""
    
    if ext in [".xlsx", ".xls"]:
        # Chunked extraction pipeline
        units = get_excel_extraction_units(file_path, allowed_sheets)
        semaphore = asyncio.Semaphore(GEMINI_MAX_CONCURRENT)
        
        async def process_unit(unit):
            async with semaphore:
                unit_name = unit.get("source_name", "Unknown")
                json_str = json.dumps(unit["data"], ensure_ascii=False)
                # Cap the string length just in case a single sheet is maliciously huge
                if len(json_str) > 300000:
                    json_str = json_str[:300000] + "\n...[TRUNCATED]"
                
                parts = [
                    {"text": PROMPT_V1},
                    {"text": f"\n\nDocument Data (Semantic JSON):\n{json_str}"}
                ]
                
                unit_res = await call_gemini_api_with_retries(parts, unit_name)
                
                # Inject provenance (source) into each section
                for sec in unit_res.get("sections", []):
                    sec["source"] = {"source_type": unit["source_type"], "source_name": unit_name}
                    
                return unit_res, json_str

        tasks = [process_unit(u) for u in units]
        results = await asyncio.gather(*tasks)
        
        for res, source_str in results:
            master_json["sections"].extend(res.get("sections", []))
            all_source_text += source_str + "\n\n"
            
    else:
        # Single-pass extraction for PDFs/Images
        parts = [{"text": PROMPT_V1}]
        
        mime_type = "application/pdf"
        if ext in [".png"]: mime_type = "image/png"
        elif ext in [".jpg", ".jpeg"]: mime_type = "image/jpeg"
        
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
            
        parts.append({
            "inlineData": {
                "mimeType": mime_type,
                "data": b64_data
            }
        })
        
        unit_res = await call_gemini_api_with_retries(parts, "PDF/Image")
        for sec in unit_res.get("sections", []):
            sec["source"] = {"source_type": "file", "source_name": filename}
            master_json["sections"].append(sec)
            
    # Attach metadata for validation and UI
    master_json["metadata"] = {}
    master_json["metadata"]["extraction_time_sec"] = time.time() - start_time
    master_json["metadata"]["source_text"] = all_source_text
        
    # Post-process to guarantee zone_segments exist for UI
    for section in master_json.get("sections", []):
        rates = section.get("rates", [])
        unique_zones = set(str(r.get("zone")) for r in rates if r.get("zone"))
        
        if "zone_segments" not in section or not isinstance(section["zone_segments"], list):
            section["zone_segments"] = []
            
        existing_zones = set(str(zs.get("zone")) for zs in section["zone_segments"] if isinstance(zs, dict) and zs.get("zone"))
        missing_zones = unique_zones - existing_zones
        
        for z in missing_zones:
            section["zone_segments"].append({
                "zone": z,
                "segments": [
                    {
                        "start_weight": 0,
                        "end_weight": None,
                        "price_type": "UNKNOWN",
                        "confidence": 0.0,
                        "reason": "No explicit evidence extracted by AI."
                    }
                ]
            })
            
    return master_json
