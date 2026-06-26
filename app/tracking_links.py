from urllib.parse import quote_plus

DEFAULT_TRACKING_TEMPLATES = {
    "quickship": "https://track.quickshipnow.com/?awb={awb}",
    "fedex": "https://www.fedex.com/fedextrack/?trknbr={awb}",
    "ups": "https://www.ups.com/track?HTMLVersion=5.0&loc=en_IN&Requester=UPSHome&tracknum={awb}/trackdetails",
    "aramax": "https://www.aramex.com/ae/en/track/results?source=aramex&ShipmentNumber={awb}",
    "aramex": "https://www.aramex.com/ae/en/track/results?source=aramex&ShipmentNumber={awb}",
    "dpd": "https://t.17track.net/en#nums={awb}",
}
LM_FALLBACK_TEMPLATE = "https://t.17track.net/en#nums={awb}"
COPY_AND_OPEN_TRACKING_SITES = {}


def normalize_courier_name(courier_name: str | None) -> str:
    return "".join(ch for ch in (courier_name or "").strip().lower() if ch.isalnum())


def build_tracking_url(
    courier_name: str | None,
    tracking_number: str | None,
    templates: dict[str, str] | None = None,
    tracking_type: str | None = None,
) -> str:
    tracking_number = (tracking_number or "").strip()
    if not tracking_number:
        return ""

    normalized_templates = {
        normalize_courier_name(name): template
        for name, template in DEFAULT_TRACKING_TEMPLATES.items()
    }
    normalized_templates.update({
        normalize_courier_name(name): template
        for name, template in (templates or {}).items()
        if name and template
    })

    normalized_courier = normalize_courier_name(courier_name)
    if normalized_courier in COPY_AND_OPEN_TRACKING_SITES or normalized_courier in {"atlantic", "overseas"}:
        return ""

    template = normalized_templates.get(normalized_courier)
    if not template and tracking_type == "lm_awb":
        template = LM_FALLBACK_TEMPLATE
    if not template or "{awb}" not in template:
        return ""

    return template.replace("{awb}", quote_plus(tracking_number))


def build_tracking_site_url(courier_name: str | None, tracking_number: str | None) -> str:
    tracking_number = (tracking_number or "").strip()
    if not tracking_number:
        return ""
    normalized = normalize_courier_name(courier_name)
    if normalized == "atlantic":
        return f"/tracking/atlantic?awb={quote_plus(tracking_number)}"
    if normalized == "overseas":
        return f"/tracking/overseas?awb={quote_plus(tracking_number)}"
    return COPY_AND_OPEN_TRACKING_SITES.get(normalized, "")
