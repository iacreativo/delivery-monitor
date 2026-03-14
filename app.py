from flask import Flask, jsonify
import os
import time
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
    cutoff_time = time.time() - (DELIVERY_TIMEOUT_MINUTES * 60)
    
    try:
        # Get pending deliveries older than timeout
        response = supabase.table('deliveries').select('*').eq('status', 'processing').lt('created_at', cutoff_time).execute()
        pending = response.data if response.data else []
        
        if not pending:
            return
            
        print(f"[Monitor] Checking {len(pending)} pending deliveries...")
        
        for delivery in pending:
            user_id = delivery['user_id']
            credits_used = delivery['credits_used']
            delivery_id = delivery['id']
            
            # Check if image exists in personal gallery
            try:
                gallery_resp = httpx.get(f"{STORAGE_PROXY_URL}/user-gallery?user_id={user_id}", timeout=15)
                
                if gallery_resp.status_code == 200:
                    gallery = gallery_resp.json()
                    
                    # Check if any image is recent (within processing time - 10 min buffer)
                    buffer_time = cutoff_time - (10 * 60)
                    recent_images = [img for img in gallery if img.get('timestamp', 0) > buffer_time]
                    
                    if recent_images:
                        # Image arrived - mark as delivered
                        supabase.table('deliveries').update({'status': 'delivered'}).eq('id', delivery_id).execute()
                        print(f"[Monitor] Delivery {delivery_id}: SUCCESS")
                    else:
                        # No recent image - mark as failed and refund
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
            print(f"[Monitor] Delivery {delivery_id}: ERROR calling refund endpoint: {resp.status_code}")
            
    except Exception as e:
        print(f"[Monitor] Error in handle_failure: {e}")
    
    try:
        # Update delivery status
        supabase.table('deliveries').update({'status': 'failed'}).eq('id', delivery_id).execute()
    except Exception as e:
        print(f"[Monitor] Error updating delivery status: {e}")


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


# Start scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(check_deliveries, 'interval', minutes=CHECK_INTERVAL_MINUTES, id='delivery_check')
scheduler.start()
print(f"[Monitor] Started - checking every {CHECK_INTERVAL_MINUTES} minute(s), timeout at {DELIVERY_TIMEOUT_MINUTES} minutes")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
