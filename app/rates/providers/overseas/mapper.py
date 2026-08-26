import datetime
from app.rates.domain.models import ShipmentRequest
from .dtos import OverseasQuoteRequestDict, OverseasPackageDict
from .country_repository import overseas_country_repository

class OverseasMapper:
    @staticmethod
    def to_provider_request(request: ShipmentRequest) -> OverseasQuoteRequestDict:
        country_data = overseas_country_repository.get_country_data(request.destinationCountry)
        
        if not country_data:
            # Fallback if we don't have explicit mapping
            iso2 = request.destinationCountry.upper()
            display_name = iso2
        else:
            iso2 = country_data.iso2
            display_name = country_data.display_name

        packages = []
        total_chg_wt = 0.0
        
        for idx, p in enumerate(request.packages):
            vol_wt = round((p.length * p.width * p.height) / 5000, 2)
            chg_wt = max(p.weight, vol_wt)
            total_chg_wt += chg_wt
            
            packages.append(OverseasPackageDict(
                PcsBoxNo=idx + 1,
                ActualWt=p.weight,
                Length=p.length,
                Width=p.width,
                Height=p.height,
                VolumetricWt=vol_wt,
                ChargeableWt=chg_wt
            ))
            
        # Network trace shows Overseas uses "D" for Document and "S" for Non-Document (Parcel/Sample)
        shipment_mode = "D" if request.shipmentType.lower() == "document" else "S"

        return OverseasQuoteRequestDict(
            ShpCode=None,
            ISOCode=iso2,
            DestinationName=display_name,
            Pincode=request.postalCode or None,
            City=request.destinationCity or None,
            State=request.destinationState or None,
            ShipmentMode=shipment_mode,
            Pcs=len(request.packages),
            ChgWeight=round(total_chg_wt, 2),
            Date=datetime.datetime.now().strftime("%Y-%m-%d"),
            PiecesDetailsTable=packages
        )
