import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 1. SET ENV VAR BEFORE IMPORTING APP
os.environ["COURIERBRIDGE_DATABASE_URL"] = "sqlite:///./courierbridge_test.db"

# Clean up old test db if exists before any imports lock it
if os.path.exists("./courierbridge_test.db"):
    try:
        os.remove("./courierbridge_test.db")
    except Exception:
        pass

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from app.database import Base, get_db, engine
from app.models import Shipment, TrackingNumber, now_ist
from app.main import app
from datetime import timedelta

TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def run_smoke_checks():
    print("Running Smoke Checks...")
    
    print("[1] Verifying app imports and FastAPI initialization...")
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200, "App health check failed"
    print("    -> Passed.")
    
    print("[2] Verifying database schema creation...")
    Base.metadata.create_all(bind=engine)
    print("    -> Passed.")
    
    db = TestingSessionLocal()
    try:
        print("[3] Testing POST /shipments/new with booking_date, status_raw_text, main AWB, and LM AWB...")
        # create a booking date 3 days ago to test "stuck" logic
        old_date = (now_ist() - timedelta(days=3)).strftime("%Y-%m-%d")
        
        form_data = {
            "customer_name": "Test Customer",
            "customer_phone": "1234567890",
            "receiver_name": "Test Receiver",
            "parcel_description": "Electronics",
            "destination_country": "USA",
            "booking_date": old_date,
            "billed_amount": "5000",
            "received_amount": "2000", # Balance > 0
            "main_tracking_number": "MAIN123",
            "main_tracking_courier": "FedEx",
            "lm_awb_number": "LM456",
            "lm_awb_courier": "USPS",
            "requires_lm_awb": "true",
            "status_raw_text": "In Transit", # Should set last_status_text and last_status_at
            "customer_notes": "Test Note",
            "weight_basis": "actual",
            "promised_days_number": "5"
        }
        res = client.post("/shipments/new", data=form_data, follow_redirects=True)
        assert res.status_code == 200, "Failed to create shipment"
        
        # Verify in DB
        db.expire_all()
        shipment = db.query(Shipment).filter(Shipment.customer_name == "Test Customer").first()
        assert shipment is not None
        assert shipment.booking_date.strftime("%Y-%m-%d") == old_date
        assert shipment.balance_amount == 3000
        assert shipment.receiver_name == "Test Receiver"
        assert shipment.parcel_description == "Electronics"
        assert shipment.customer_notes == "Test Note"
        
        # Check stuck logic
        assert shipment.last_status_text == "In Transit"
        assert shipment.last_status_at is not None
        assert shipment.is_stuck == True, "Shipment should be stuck (3 days old status)"
        
        # Check Tracking Numbers
        tns = db.query(TrackingNumber).filter(TrackingNumber.shipment_id == shipment.id).all()
        assert len(tns) == 2, f"Expected 2 tracking numbers, got {len(tns)}"
        
        main_tn = next((tn for tn in tns if tn.tracking_type == "main_awb"), None)
        assert main_tn and main_tn.tracking_number == "MAIN123"
        assert main_tn.is_primary == True
        
        lm_tn = next((tn for tn in tns if tn.tracking_type == "lm_awb"), None)
        assert lm_tn and lm_tn.tracking_number == "LM456"
        assert lm_tn.is_primary == False
        
        print("    -> Passed.")
        
        print("[4] Testing GET /shipments/{id}/edit...")
        res_get_edit = client.get(f"/shipments/{shipment.id}/edit")
        assert res_get_edit.status_code == 200, "Edit page failed to load"
        print("    -> Passed.")
        
        print("[5] Testing POST /shipments/{id}/edit (Field preservation & Upsert)...")
        edit_data_full = {
            **form_data,
            "main_tracking_number": "MAIN123-UPDATED",
            "lm_awb_number": "LM456",
            "receiver_name": "Test Receiver",
            "parcel_description": "Electronics",
            "customer_notes": "Test Note",
            "weight_basis": "actual",
            "promised_days_number": "5",
            "overall_status": "delivered"
        }
        res_edit = client.post(f"/shipments/{shipment.id}/edit", data=edit_data_full, follow_redirects=True)
        assert res_edit.status_code == 200
        
        db.expire_all()
        shipment_after = db.query(Shipment).filter(Shipment.id == shipment.id).first()
        assert shipment_after.receiver_name == "Test Receiver", "Receiver name was wiped"
        assert shipment_after.delivered_at.strftime("%Y-%m-%d") == old_date, "Delivered_at should equal booking_date for old shipment"
        
        tns_after = db.query(TrackingNumber).filter(TrackingNumber.shipment_id == shipment.id).all()
        assert len(tns_after) == 2, "Duplicate AWB rows created on edit!"
        
        updated_main = next((tn for tn in tns_after if tn.tracking_type == "main_awb"), None)
        assert updated_main.tracking_number == "MAIN123-UPDATED", "Main tracking number not updated"
        print("    -> Passed.")
        
        print("[6] Testing /tracking/number/new upsert logic...")
        # Add main_awb again
        res_track = client.post("/tracking/number/new", data={
            "shipment_id": shipment.id,
            "tracking_type": "main_awb",
            "courier_name": "FedEx",
            "tracking_number": "MAIN123-DUP",
            "is_primary": "true"
        }, follow_redirects=True)
        assert res_track.status_code == 200
        
        db.expire_all()
        tns_track = db.query(TrackingNumber).filter(TrackingNumber.shipment_id == shipment.id).all()
        assert len(tns_track) == 2, "Duplicate AWB rows created on tracking endpoint!"
        main_tn2 = next((tn for tn in tns_track if tn.tracking_type == "main_awb"), None)
        assert main_tn2.tracking_number == "MAIN123-DUP", "Main tracking number not updated"
        assert main_tn2.is_primary == True, "Main tracking number should be primary"
        
        # Test LM AWB upsert via endpoint
        res_track_lm = client.post("/tracking/number/new", data={
            "shipment_id": shipment.id,
            "tracking_type": "lm_awb",
            "courier_name": "USPS",
            "tracking_number": "LM456-DUP",
            "is_primary": "true" # Even if requested as true, backend should force False
        }, follow_redirects=True)
        assert res_track_lm.status_code == 200
        
        db.expire_all()
        tns_track_lm = db.query(TrackingNumber).filter(TrackingNumber.shipment_id == shipment.id).all()
        assert len(tns_track_lm) == 2, "Duplicate LM AWB row created!"
        lm_tn2 = next((tn for tn in tns_track_lm if tn.tracking_type == "lm_awb"), None)
        assert lm_tn2.tracking_number == "LM456-DUP", "LM tracking number not updated"
        assert lm_tn2.is_primary == False, "LM tracking number should never be primary"
        
        print("    -> Passed.")
        
    except Exception as e:
        print(f"Smoke check failed: {e}")
        raise e
    finally:
        db.close()

if __name__ == "__main__":
    try:
        run_smoke_checks()
        print("All smoke checks passed successfully!")
    finally:
        # Clean up test DB
        engine.dispose()
        if os.path.exists("./courierbridge_test.db"):
            try:
                os.remove("./courierbridge_test.db")
            except Exception:
                pass
