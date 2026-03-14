from flask import Flask, jsonify
import os
from datetime import datetime, timezone, timedelta
import httpx
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
STORAGE_PROXY_URL = os.getenv("STORAGE_PROXY_URL", "https://storage.vyzo.cloud")
DELIVERY_TIMEOUT_MINUTES = int(os.getenv("DELIVERY_TIMEOUT_MINUTES", "3"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

def check_deliveries():
    """Check deliveries - look for matching delivery_id in gallery"""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
    
    try:
        # Get recent deliveries
        response = supabase.table('deliveries').select('*').gte('created_at', cutoff.isoformat()).execute()
        deliveries = response.data or []
        
        print(f"[Monitor] Checking {len(deliveries)} recent deliveries...")
        
        for d in deliveries:
            user_id = d['user_id']
            credits_used = d['credits_used']
            delivery_id = d['id']
            status = d.get('status', 'unknown')
            
            # Skip if already failed
            if status == 'failed':
                print(f"[Monitor] Delivery {delivery_id}: Already failed, skipping")
                continue
            
            # Check gallery for this delivery_id
            try:
                resp = httpx.get(f"{STORAGE_PROXY_URL}/user-gallery?user_id={user_id}", timeout=10)
                if resp.status_code == 200:
                    gallery = resp.json()
                    
                    # Look for image with matching delivery_id
                    matching = [img for img in gallery if img.get('delivery_id') == delivery_id]
                    
                    if matching:
                        print(f"[Monitor] Delivery {delivery_id}: SUCCESS - Found image with delivery_id")
                        if status != 'delivered':
                            supabase.table('deliveries').update({'status': 'delivered'}).eq('id', delivery_id).execute()
                    else:
                        # Check if enough time has passed
                        created = d.get('created_at', '')
                        try:
                            dt = datetime.fromisoformat(created.replace('+00:00', '').replace('Z', ''))
                            mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
                        except:
                            mins = 0
                        
                        if mins >= DELIVERY_TIMEOUT_MINUTES:
                            print(f"[Monitor] Delivery {delivery_id}: FAIL - No image with delivery_id found after {mins} min")
                            handle_failure(user_id, credits_used, delivery_id, "No se recibió imagen en galería personal")
                        else:
                            print(f"[Monitor] Delivery {delivery_id}: Waiting... ({mins:.1f} min)")
                else:
                    print(f"[Monitor] Error checking gallery: {resp.status_code}")
            except Exception as e:
                print(f"[Monitor] Error: {e}")
                
    except Exception as e:
        print(f"[Monitor] Fatal: {e}")

def handle_failure(user_id, credits_used, delivery_id, message):
    """Register error and refund credits"""
    try:
        supabase.table('deliveries').update({'status': 'failed'}).eq('id', delivery_id).execute()
        
        httpx.post(f"{STORAGE_PROXY_URL}/user-gallery/error", json={
            "user_id": user_id,
            "error_message": message,
            "refund_credits": True,
            "credits_amount": credits_used,
            "execution_id": delivery_id
        }, timeout=15)
        
        print(f"[Monitor] Delivery {delivery_id}: FAILED - Refunded {credits_used} credits")
    except Exception as e:
        print(f"[Monitor] Handle failure error: {e}")

@app.route("/debug", methods=["GET"])
def debug():
    """Debug - show recent deliveries"""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        
        # Get ALL deliveries from last 15 min
        response = supabase.table('deliveries').select('*').gte('created_at', cutoff.isoformat()).order('created_at', desc=True).execute()
        deliveries = response.data or []
        
        result = []
        for d in deliveries:
            created = d.get('created_at', '')
            try:
                dt = datetime.fromisoformat(created.replace('+00:00', '').replace('Z', ''))
                mins_ago = (datetime.now(timezone.utc) - dt).total_seconds() / 60
            except:
                mins_ago = 0
                
            result.append({
                "id": str(d.get('id', ''))[:8],
                "status": d.get('status'),
                "credits": d.get('credits_used'),
                "mins_ago": round(mins_ago, 1)
            })
        
        return jsonify({
            "deliveries": result,
            "total": len(deliveries)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "timeout": DELIVERY_TIMEOUT_MINUTES, "interval": CHECK_INTERVAL_MINUTES})

@app.route("/check-now", methods=["POST"])
def check_now():
    check_deliveries()
    return jsonify({"status": "triggered"})

# Start scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(check_deliveries, 'interval', minutes=CHECK_INTERVAL_MINUTES)
scheduler.start()
print(f"[Monitor] Started - check every {CHECK_INTERVAL_MINUTES} min, timeout {DELIVERY_TIMEOUT_MINUTES} min")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
