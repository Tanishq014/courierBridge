from typing import Any, Dict
import json
import urllib.request
import re
import os

def extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None

def parse_raw_text_for_import(raw_text: str, courier_options: list[str] = None) -> Dict[str, Any]:
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    model_name = (os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash").strip()
    
    # Truncate raw_text to prevent massive payloads (e.g. from base64 images) from overloading the AI
    if len(raw_text) > 300000:
        raw_text = raw_text[:300000] + "\n...[TRUNCATED]"

    print("\n" + "="*50)
    print(f"🤖 AI Magic Import Triggered")
    print(f"Model: {model_name}")
    print(f"Raw text length: {len(raw_text)} characters")
    print("-" * 50)
    print(f"Snippet: {raw_text[:300]}...")
    print("-" * 50)
    
    if not api_key:
        print("❌ Error: No Gemini API key found in environment.")
        return {"error": "No Gemini API key found"}

    prompt_template = """
You are an expert data entry assistant for an international courier company.
You will receive raw, unstructured text scraped from a vendor's website (like QuickShip, Overseas Logistics, FedEx, etc).
Extract the following information and return strict JSON ONLY.

Fields to extract:
- "customer_name": Sender / Shipper Name
- "customer_phone": Sender / Shipper Phone Number
- "receiver_name": Receiver / Consignee Name
- "receiver_address_line_1": Receiver Address (house/building/street)
- "receiver_address_line_2": Receiver Address (area/locality)
- "receiver_address_line_3": Receiver Address (landmark/optional)
- "destination_city": Receiver City
- "receiver_state": Receiver State / Province
- "receiver_zip": Receiver Postal Code / ZIP Code
- "destination_country": Receiver Country
- "contact_or_reference_raw": Receiver Phone Number OR Reference Number
- "item_raw_text": A single string listing the items / description of goods
- "dead_weight": The actual weight of the package (number only, in kg)
- "volumetric_length": Box length (number only, in cm)
- "volumetric_width": Box width (number only, in cm)
- "volumetric_height": Box height (number only, in cm)
- "main_tracking_number": The primary AWB (Air Waybill) or tracking number
- "main_tracking_courier": The name of the courier company handling the shipment.

Rules:
1. Return strictly a single JSON object.
2. If a field is missing or cannot be definitively found, return null for it.
3. For weights and dimensions, extract just the numeric value if possible. Assume kg and cm.
4. Try to separate address lines logically if they are squashed together.
5. Do not include markdown formatting or comments outside the JSON.
6. For "main_tracking_courier", you MUST select exactly one of the following options if it matches the parsed text: {couriers}. If no option matches, or you are unsure, return null. Do NOT make up a courier name.
"""
    prompt = prompt_template.format(couriers=json.dumps(courier_options or []))
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
    body = {
        "contents": [{
            "role": "user",
            "parts": [{"text": prompt + "\n\nRaw Text to Parse:\n" + raw_text}],
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    
    try:
        print("⏳ Waiting for Gemini response...")
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        
        text = ""
        for candidate in payload.get("candidates") or []:
            for part in (candidate.get("content") or {}).get("parts") or []:
                text += part.get("text") or ""
                
        print(f"✅ Gemini Response Received:\n{text}")
        print("="*50 + "\n")
        
        parsed = extract_json_object(text)
        if not parsed:
            print("❌ Error: Failed to parse JSON from AI response")
            return {"error": "Failed to parse JSON from AI"}
        
        return parsed
    except Exception as e:
        print(f"❌ Error calling Gemini API: {str(e)}")
        print("="*50 + "\n")
        return {"error": str(e)}
