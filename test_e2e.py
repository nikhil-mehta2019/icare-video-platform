import os
import sys
import time
from io import BytesIO
from unittest.mock import patch
import pandas as pd

# ---------------------------------------------------------
# 1. SETUP ENVIRONMENT VARIABLES (Must run before app import)
# ---------------------------------------------------------
os.environ["MUX_TOKEN_ID"] = "test_mux_id"
os.environ["MUX_TOKEN_SECRET"] = "test_mux_secret"
os.environ["VIMEO_ACCESS_TOKEN"] = "test_vimeo_token"
# Use a separate test SQLite database so we don't wipe production data
os.environ["DATABASE_URL"] = "sqlite:///./test_e2e_icare.db"

from fastapi.testclient import TestClient
from app.main import app
from app.database.session import Base, engine, SessionLocal
from app.database.models import Video, MigrationJob

# Initialize Test Client
client = TestClient(app)
db = SessionLocal()

# Global Test Report Container
test_report = []

def log_test(name, result, details=""):
    """Helper to record and print test results."""
    status = "✅ PASS" if result else "❌ FAIL"
    test_report.append({"name": name, "status": status, "details": details})
    print(f"{status} | {name}")
    if not result:
        print(f"   => {details}")

# ---------------------------------------------------------
# 2. TEST SUITE
# ---------------------------------------------------------

def run_all_tests():
    print("\n🚀 Starting iCare Video Platform E2E Tests...\n")
    
    # --- TEST 1: Database Initialization ---
    try:
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        log_test("Database Reset & Init", True, "Test SQLite DB recreated successfully.")
    except Exception as e:
        log_test("Database Reset & Init", False, str(e))
        return # Stop execution if DB fails

    # --- TEST 2: Manual Video Import ---
    try:
        with patch("app.services.migration_service.get_video_download_url") as mock_vimeo_dl, \
             patch("app.services.migration_service.upload_video") as mock_mux_up:
            
            # Mock the external API responses
            mock_vimeo_dl.return_value = "https://vimeo.com/download/fake.mp4"
            mock_mux_up.return_value = {"asset_id": "asset_1001", "playback_id": "play_1001"}

            # Trigger manual import endpoint
            res = client.post("/videos/import-vimeo", json={
                "title": "Manual Test Video",
                "vimeo_url": "https://vimeo.com/1001"
            })
            
            data = res.json()
            assert res.status_code == 200, "Expected HTTP 200"
            assert data["status"] == "success", "Expected success status"
            assert data["mux_asset_id"] == "asset_1001", "Mux Asset ID mismatch"
            
            # Verify database entry
            video = db.query(Video).filter(Video.vimeo_id == "1001").first()
            assert video is not None, "Video not saved to DB"
            assert video.status == "pending", "Initial status should be pending"
            
            log_test("Manual Vimeo Import API", True, "Successfully imported single video and saved to DB.")
    except Exception as e:
        log_test("Manual Vimeo Import API", False, str(e))

    # --- TEST 3: Mux Webhook Processing ---
    try:
        res = client.post("/webhook/mux", json={
            "type": "video.asset.ready",
            "data": {"id": "asset_1001"}
        })
        
        assert res.status_code == 200, "Webhook endpoint failed"
        
        # Verify DB status updated
        db.expire_all() # Refresh session
        video = db.query(Video).filter(Video.vimeo_id == "1001").first()
        assert video.status == "ready", "Webhook failed to update video status to 'ready'"
        
        log_test("Mux Webhook Processing", True, "Successfully handled video.asset.ready event.")
    except Exception as e:
        log_test("Mux Webhook Processing", False, str(e))

    # --- TEST 4: Playback API ---
    try:
        res = client.get("/playback/1001")
        assert res.status_code == 200, "Playback endpoint failed"
        
        data = res.json()
        assert data["mux_playback_id"] == "play_1001", "Returned wrong playback ID"
        assert "stream.mux.com/play_1001.m3u8" in data["mux_stream_url"], "Invalid stream URL format"
        
        log_test("Playback Resolution API", True, "Successfully retrieved playback configuration.")
    except Exception as e:
        log_test("Playback Resolution API", False, str(e))

    # --- TEST 5: Bulk Account Migration (Background Task) ---
    try:
        # Note: FastAPI TestClient executes BackgroundTasks synchronously in the same thread
        with patch("app.services.migration_service.get_vimeo_videos") as mock_vimeo_list, \
             patch("app.services.migration_service.get_video_download_url") as mock_vimeo_dl, \
             patch("app.services.migration_service.upload_video") as mock_mux_up:
             
            # Mock 2 videos returning from Vimeo
            mock_vimeo_list.return_value = [
                {"uri": "/videos/2001", "name": "Bulk 1", "link": "https://vimeo.com/2001", "folders": {"data": [{"name": "Course A"}]}},
                {"uri": "/videos/2002", "name": "Bulk 2", "link": "https://vimeo.com/2002", "folders": {"data": []}}
            ]
            mock_vimeo_dl.return_value = "https://vimeo.com/download/fake.mp4"
            mock_mux_up.side_effect = [
                {"asset_id": "asset_2001", "playback_id": "play_2001"},
                {"asset_id": "asset_2002", "playback_id": "play_2002"}
            ]

            res = client.post("/migration/vimeo-account")
            assert res.status_code == 200, "Bulk migration trigger failed"
            
            # Since TestClient runs background tasks immediately, the DB should now be populated
            db.expire_all()
            job = db.query(MigrationJob).order_by(MigrationJob.id.desc()).first()
            
            assert job.total_videos == 2, "Job did not register total videos"
            assert job.imported_videos == 2, "Job did not import all videos"
            assert job.status == "completed", "Job did not complete successfully"
            
            # Verify folder extraction worked
            vid = db.query(Video).filter(Video.vimeo_id == "2001").first()
            assert vid.vimeo_folder_path == "Course A", "Folder path extraction failed"

            log_test("Bulk Account Migration", True, "Successfully migrated multiple videos and tracked job state.")
    except Exception as e:
        log_test("Bulk Account Migration", False, str(e))

    # --- TEST 6: Migration Idempotency (Locking) ---
    try:
        # Manually inject a running job to simulate concurrency
        running_job = MigrationJob(status="running")
        db.add(running_job)
        db.commit()

        # Attempt to start a new migration
        res = client.post("/migration/vimeo-account")
        assert res.status_code == 400, "Expected 400 Bad Request for concurrent migration"
        assert "already in progress" in res.json()["detail"], "Missing lock warning message"
        
        # Cleanup
        db.delete(running_job)
        db.commit()
        
        log_test("Migration Idempotency Lock", True, "Successfully blocked concurrent bulk migrations.")
    except Exception as e:
        log_test("Migration Idempotency Lock", False, str(e))

    # --- TEST 7: Excel Export API ---
    try:
        res = client.get("/migration/export")
        assert res.status_code == 200, "Export endpoint failed"
        assert "spreadsheetml.sheet" in res.headers["content-type"], "Invalid Content-Type"
        
        # Load the binary response into Pandas to verify the data integrity
        excel_data = pd.read_excel(BytesIO(res.content))
        with open("vimeo_mux_test_report.xlsx", "wb") as f:
            f.write(res.content)
        
        assert len(excel_data) == 3, f"Expected 3 rows in Excel, got {len(excel_data)}"
        assert list(excel_data.columns) == ["Vimeo Title", "Vimeo Folder Path", "Vimeo URL", "Mux Title", "Mux Asset ID", "Mux Playback URL"], "Excel headers mismatch"
        
        log_test("Excel Export Generation", True, "Successfully generated valid .xlsx mapping report.")
    except Exception as e:
        log_test("Excel Export Generation", False, str(e))


# ---------------------------------------------------------
# 3. REPORT GENERATION
# ---------------------------------------------------------
def generate_report():
    print("\n" + "="*50)
    print(" 📊 END-TO-END TEST REPORT")
    print("="*50)
    
    passed = sum(1 for t in test_report if "PASS" in t["status"])
    total = len(test_report)
    
    for i, test in enumerate(test_report, 1):
        print(f"{i}. {test['name'].ljust(30)} [{test['status']}]")
        if "FAIL" in test["status"]:
            print(f"   Reason: {test['details']}")
            
    print("-" * 50)
    print(f"   Success Rate: {passed}/{total} ({(passed/total)*100:.1f}%)")
    print("=" * 50 + "\n")

if __name__ == "__main__":
    try:
        run_all_tests()
        generate_report()
    finally:
        # Cleanup Test DB safely on Windows
        db.close()          # Close the active session
        engine.dispose()    # Terminate the connection pool locking the file
        
        time.sleep(0.5)     # Brief pause to let Windows release the file handle
        
        if os.path.exists("./test_e2e_icare.db"):
            try:
                os.remove("./test_e2e_icare.db")
                print("🧹 Test database cleaned up successfully.")
            except Exception as e:
                print(f"⚠️ Could not delete test DB automatically: {e}")

if __name__ == "__main__":
    run_all_tests()
    generate_report()