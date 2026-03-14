from flask import Flask, jsonify
import os
import time
from datetime import datetime, timezone, timedelta
import httpx
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config from environment
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
STORAGE_PROXY_URL = os.getenv("STORAGE_PROXY_URL", "https://storage.vyzo.cloud")
DELIVERY_TIMEOUT_MINUTES = int(os.getenv("DELIVERY_TIMEOUT_MINUTES", "6"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def check_deliveries():
    """Check pending deliveries and verify if images arrived"""
    # Use proper ISO timestamp for Supabase comparison
    cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=DELIVERY_TIMEOUT_MINUTES)
    cutoff_iso = cutoff_dt.isoformat()
    
    print(f"[Monitor] Checking for deliveries older than {cutoff_iso}...")
    
    try:
        # Get pending deliveries older than timeout
        response = supabase.table('deliveries').select('*').eq('status', 'processing').lt('created_at', cutoff_iso).execute()
        pending = response.data if response.data else []
        
        print(f"[Monitor] Found {len(pending)} pending deliveries older than {DELIVERY_TIMEOUT_MINUTES} minutes")
        
        if not pending:
            return
        
        for delivery in pending:
            user_id = delivery['user_id']
            credits_used = delivery['credits_used']
            delivery_id = delivery['id']
            created_at = delivery.get('created_at', '')
            
            print(f"[Monitor] Checking delivery {delivery_id} for user {user_id}, created at {created_at}")
            
            # Check if image exists in personal gallery
            try:
                gallery_resp = httpx.get(f"{STORAGE_PROXY_URL}/user-gallery?user_id={user_id}", timeout=15)
                
                if gallery_resp.status_code == 200:
                    gallery = gallery_resp.json()
                    
                    # Check if there are any images at all for this user
                    if not gallery or len(gallery) == 0:
                        print(f"[Monitor] No images in gallery for user {user_id}")
                        handle_failure(user_id, credits_used, delivery_id, "No se encontró ninguna imagen en galería personal")
                    else:
                        # Get most recent image timestamp
                        latest_timestamp = max([img.get('timestamp', 0) for img in gallery])
                        print(f"[Monitor] Latest image timestamp: {latest_timestamp}")
                        
                        # Check if any image is newer than the delivery - use created_at
                        delivery_created = delivery.get('created_at', '')
                        if delivery_created:
                            # Parse the ISO timestamp
                            try:
                                if '+' in delivery_created:
                                    delivery_time = datetime.fromisoformat(delivery_created.replace('+00:00', '')).timestamp()
                                else:
                                    delivery_time = datetime.fromisoformat(delivery_created).timestamp()
                            except:
                                delivery_time = 0
                        else:
                            delivery_time = 0
                            
                        print(f"[Monitor] Delivery time: {delivery_time}, Latest image: {latest_timestamp}")
                        
                        if latest_timestamp > delivery_time:
                            # Image arrived - mark as delivered
                            supabase.table('deliveries').update({'status': 'delivered'}).eq('id', delivery_id).execute()
                            print(f"[Monitor] Delivery {delivery_id}: SUCCESS")
                        else:
                            handle_failure(user_id, credits_used, delivery_id, "No se recibió imagen en galería personal")
                else:
                    handle_failure(user_id, credits_used, delivery_id, f"Error al verificar galería: {gallery_resp.status_code}")
            except Exception as e:
                print(f"[Monitor] Error checking delivery {delivery_id}: {e}")
                handle_failure(user_id, credits_used, delivery_id, f"Error de conexión: {str(e)}")
                
    except Exception as e:
        print(f"[Monitor] Fatal error in check_deliveries: {e}")


def handle_failure(user_id, credits_used, delivery_id, error_message):
    """Register error and refund credits"""
    
    # FIRST: Mark as failed to prevent duplicate processing
    try:
        supabase.table('deliveries').update({'status': 'failed'}).eq('id', delivery_id).execute()
        print(f"[Monitor] Marked delivery {delivery_id} as failed")
    except Exception as e:
        print(f"[Monitor] Error marking delivery as failed: {e}")
    
    # SECOND: Try to refund credits and log error
    try:
        # Call storage-proxy error endpoint
        error_data = {
            "user_id": user_id,
            "error_message": f"Error en procesamiento: {error_message}",
            "refund_credits": True,
            "credits_amount": credits_used,
            "execution_id": delivery_id
        }
        
        resp = httpx.post(f"{STORAGE_PROXY_URL}/user-gallery/error", json=error_data, timeout=15)
        
        if resp.status_code == 200:
            result = resp.json()
            refund_status = result.get('refund', 'unknown')
            print(f"[Monitor] Delivery {delivery_id}: FAILED - Refunded {credits_used} credits (status: {refund_status})")
        else:
            print(f"[Monitor] Delivery {delivery_id}: ERROR calling refund endpoint: {resp.status_code} - {resp.text}")
            
    except Exception as e:
        print(f"[Monitor] Error in handle_failure: {e}")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok", 
        "service": "delivery-monitor",
        "timeout_minutes": DELIVERY_TIMEOUT_MINUTES,
        "check_interval_minutes": CHECK_INTERVAL_MINUTES
    })


@app.route("/check-now", methods=["POST"])
def check_now():
    """Manual trigger for checking deliveries"""
    check_deliveries()
    return jsonify({"status": "triggered"})


@app.route("/debug", methods=["GET"])
def debug():
    """Debug endpoint to check deliveries"""
    try:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=DELIVERY_TIMEOUT_MINUTES)
        cutoff_iso = cutoff_dt.isoformat()
        
        # Get all processing deliveries
        response = supabase.table('deliveries').select('*').eq('status', 'processing').execute()
        processing = response.data if response.data else []
        
        # Get old deliveries that should have failed
        old_response = supabase.table('deliveries').select('*').eq('status', 'processing').lt('created_at', cutoff_iso).execute()
        old_deliveries = old_response.data if old_response.data else []
        
        # Get failed deliveries
        failed_response = supabase.table('deliveries').select('*').eq('status', 'failed').order('created_at', desc=True).limit(5).execute()
        failed = failed_response.data if failed_response.data else []
        
        return jsonify({
            "status": "ok",
            "total_processing": len(processing),
            "should_fail": len(old_deliveries),
            "cutoff_time": cutoff_iso,
            "processing_deliveries": processing,
            "failed_deliveries": failed
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Start scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(check_deliveries, 'interval', minutes=CHECK_INTERVAL_MINUTES, id='delivery_check')
scheduler.start()
print(f"[Monitor] Started - checking every {CHECK_INTERVAL_MINUTES} minute(s), timeout at {DELIVERY_TIMEOUT_MINUTES} minutes")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
