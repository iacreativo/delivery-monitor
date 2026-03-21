from flask import Flask, jsonify
import os
import time
from datetime import datetime, timezone, timedelta
import httpx
from supabase import create_client
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Config
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
DELIVERY_TIMEOUT_MINUTES = int(os.getenv("DELIVERY_TIMEOUT_MINUTES", "8"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "1"))
GALLERY_PROXY_URL = os.getenv("GALLERY_PROXY_URL", "https://storage.vyzo.cloud")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Store pending deliveries in memory
pending_deliveries = {}

# Store failed deliveries to avoid reprocessing
processed_failures = set()

def refund_credits(user_id, credits_amount):
    """Refund credits directly via Supabase RPC"""
    try:
        response = supabase.rpc('refund_credits', {
            'p_user_id': user_id,
            'p_amount': credits_amount
        }).execute()
        print(f"[Refund] ✓ Refunded {credits_amount} credits to user {user_id}")
        return True
    except Exception as e:
        print(f"[Refund] ✗ Error refunding credits: {e}")
        return False

def cleanup_orphan_deliveries():
    """Clean up deliveries stuck in pending/processing for too long"""
    try:
        # Get all pending/processing deliveries from the last 2 hours
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        
        response = supabase.table('deliveries').select('*').in_('status', ['pending', 'processing']).gte('created_at', cutoff.isoformat()).execute()
        deliveries = response.data or []
        
        for d in deliveries:
            delivery_id = d['id']
            
            # Skip if already processed as failed
            if delivery_id in processed_failures:
                continue
            
            # Skip if in current pending_deliveries
            if delivery_id in pending_deliveries:
                continue
            
            user_id = d['user_id']
            credits = d['credits_used']
            created_str = d.get('created_at', '')
            
            # Parse created date
            try:
                if '+' in created_str:
                    created = datetime.fromisoformat(created_str.replace('+00:00', ''))
                else:
                    created = datetime.fromisoformat(created_str)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except:
                created = datetime.now(timezone.utc)
            
            minutes_passed = (datetime.now(timezone.utc) - created).total_seconds() / 60
            
            # Only process if at least 8 minutes have passed
            if minutes_passed >= DELIVERY_TIMEOUT_MINUTES:
                print(f"[Cleanup] Found orphan delivery {delivery_id} ({minutes_passed:.1f} min old)")
                
                # Check if image exists in gallery
                try:
                    resp = httpx.get(f"{GALLERY_PROXY_URL}/user-gallery?user_id={user_id}", timeout=15)
                    if resp.status_code == 200:
                        gallery = resp.json()
                        found = any(img.get('delivery_id') == delivery_id for img in gallery)
                        
                        if not found:
                            print(f"[Cleanup] Orphan {delivery_id}: NO image found - failing")
                            processed_failures.add(delivery_id)
                            handle_failure(user_id, credits, delivery_id)
                        else:
                            print(f"[Cleanup] Orphan {delivery_id}: Image found!")
                except Exception as e:
                    print(f"[Cleanup] Error checking gallery: {e}")
                    
    except Exception as e:
        print(f"[Cleanup] Error in cleanup: {e}")

def check_deliveries():
    """Check pending deliveries for images in gallery"""
    now = datetime.now(timezone.utc)
    checked = []
    
    for delivery_id, info in list(pending_deliveries.items()):
        user_id = info['user_id']
        credits = info['credits']
        created = info['created']
        
        # Ensure created is timezone-aware
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        
        minutes_passed = (now - created).total_seconds() / 60
        
        # Check if enough time passed
        if minutes_passed < DELIVERY_TIMEOUT_MINUTES:
            continue
        
        # Check if already checked
        if delivery_id in checked:
            continue
        checked.append(delivery_id)
        
        print(f"[Monitor] Checking delivery {delivery_id} for user {user_id} ({minutes_passed:.1f} min ago)")
        
        try:
            # Check gallery for this specific delivery_id
            resp = httpx.get(f"{GALLERY_PROXY_URL}/user-gallery?user_id={user_id}", timeout=15)
            
            if resp.status_code == 200:
                gallery = resp.json()
                
                # Look for image with this delivery_id
                found = False
                for img in gallery:
                    if img.get('delivery_id') == delivery_id:
                        found = True
                        print(f"[Monitor] ✓ Delivery {delivery_id}: Found image in gallery")
                        break
                
                if not found:
                    print(f"[Monitor] ✗ Delivery {delivery_id}: NO image found - FAILING")
                    handle_failure(user_id, credits, delivery_id)
                else:
                    # Remove from pending
                    del pending_deliveries[delivery_id]
                    
            print(f"[Monitor] Gallery check complete for {delivery_id}")
                
        except Exception as e:
            print(f"[Monitor] Error checking {delivery_id}: {e}")
    
    # Sync with Supabase - find new pending deliveries
    try:
        cutoff = now - timedelta(minutes=DELIVERY_TIMEOUT_MINUTES + 5)
        
        # Get deliveries from last 10 minutes
        response = supabase.table('deliveries').select('*').gte('created_at', cutoff.isoformat()).execute()
        deliveries = response.data or []
        
        for d in deliveries:
            delivery_id = d['id']
            status = d.get('status', 'unknown')
            
            # Only track if not already tracked and not failed
            if delivery_id not in pending_deliveries and status != 'failed':
                user_id = d['user_id']
                credits = d['credits_used']
                created_str = d.get('created_at', '')
                
                try:
                    if '+' in created_str:
                        created = datetime.fromisoformat(created_str.replace('+00:00', ''))
                    else:
                        created = datetime.fromisoformat(created_str)
                except:
                    created = now
                
                pending_deliveries[delivery_id] = {
                    'user_id': user_id,
                    'credits': credits,
                    'created': created
                }
                print(f"[Monitor] Tracking delivery {delivery_id} (status={status})")
                
    except Exception as e:
        print(f"[Monitor] Error syncing: {e}")

def handle_failure(user_id, credits, delivery_id):
    """Mark delivery as failed and refund credits"""
    try:
        # 1. Mark as failed in Supabase
        supabase.table('deliveries').update({'status': 'failed'}).eq('id', delivery_id).execute()
        print(f"[Monitor] ✓ Marked delivery {delivery_id} as failed")
        
        # 2. Refund credits directly via Supabase RPC
        refund_credits(user_id, credits)
        
        print(f"[Monitor] ✓ Delivery {delivery_id} handled successfully")
        
    except Exception as e:
        print(f"[Monitor] Error in handle_failure: {e}")

@app.route("/health")
def health():
    return jsonify({
        "status": "ok", 
        "pending": len(pending_deliveries),
        "timeout": DELIVERY_TIMEOUT_MINUTES
    })

@app.route("/reset", methods=["POST"])
def reset():
    global pending_deliveries
    pending_deliveries = {}
    return jsonify({"status": "reset"})

@app.route("/pending")
def get_pending():
    """Get list of pending deliveries"""
    return jsonify({
        "count": len(pending_deliveries),
        "deliveries": [
            {"id": k, **v} for k, v in pending_deliveries.items()
        ]
    })

@app.route("/cleanup", methods=["POST"])
def trigger_cleanup():
    """Manually trigger orphan cleanup"""
    cleanup_orphan_deliveries()
    return jsonify({"status": "cleanup_triggered"})

# Start scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(check_deliveries, 'interval', minutes=CHECK_INTERVAL_MINUTES)
scheduler.add_job(cleanup_orphan_deliveries, 'interval', minutes=5)  # Check orphans every 5 min
scheduler.start()
print(f"[Monitor] Started - pending: {len(pending_deliveries)}, check every {CHECK_INTERVAL_MINUTES} min, cleanup every 5 min")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
