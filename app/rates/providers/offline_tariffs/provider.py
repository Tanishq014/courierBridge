import logging
import asyncio
from typing import List
from app.rates.providers.base import RateProvider
from app.rates.domain.models import ShipmentRequest, ShipmentQuote, Charge
from app.database import SessionLocal
from app.rates.engine import get_best_rates

logger = logging.getLogger(__name__)

class OfflineTariffsProvider(RateProvider):
    """
    Acts as a bridge to fetch all rates from the local database (PDFs/Excels uploaded via AI).
    This single provider dynamically represents multiple vendors (e.g., Tobacco, Jatin).
    """
    
    @property
    def provider_code(self) -> str:
        return "offline_tariffs"

    @property
    def provider_name(self) -> str:
        return "Direct Contracts"

    async def get_quotes(self, request: ShipmentRequest) -> List[ShipmentQuote]:
        # Calculate total chargeable weight
        total_weight = 0.0
        total_volumetric = 0.0
        total_dead = 0.0
        
        for p in request.packages:
            total_dead += p.weight
            vol = (p.length * p.width * p.height) / 5000.0
            total_volumetric += vol
            total_weight += max(p.weight, vol)
            
        destination = request.destinationCountry
        
        db = SessionLocal()
        try:
            # Run the synchronous SQLAlchemy query in the asyncio executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            raw_quotes = await loop.run_in_executor(
                None, 
                get_best_rates, 
                db, 
                total_weight, 
                destination,
                request.postalCode,
                request.destinationCity,
                request.destinationState
            )
            
            final_quotes = []
            for rq in raw_quotes:
                # The vendor name (e.g., 'Tobacco' or 'Jatin') dynamically becomes the provider display name!
                vendor_name = rq.get("vendor") or rq.get("vendor_name", "Unknown Vendor")
                
                final_quotes.append(
                    ShipmentQuote(
                        provider=vendor_name,
                        providerCode=vendor_name.lower().replace(" ", "_"),
                        service=rq["service"],
                        serviceCode=rq["service"],
                        currency="INR",
                        totalPrice=rq["total_price"],
                        chargeableWeight=total_weight,
                        volumetricWeight=total_volumetric,
                        deadWeight=total_dead,
                        zone=rq["zone"],
                        transitEstimate=rq.get("transit_days"),
                        charges=[
                            Charge(name="Freight", amount=rq["total_price"], total=rq["total_price"])
                        ],
                        badges=["Offline Contract", rq["price_type"]],
                        metadata={
                            "carrier": rq["carrier"],
                            "logic": rq.get("calculation_logic", ""),
                            "notes": rq.get("notes", []),
                            "source_filename": rq.get("source_filename", "")
                        }
                    )
                )
            return final_quotes
        except Exception as e:
            logger.error(f"Error fetching offline tariffs: {e}")
            raise
        finally:
            db.close()
