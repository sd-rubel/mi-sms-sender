#!/usr/bin/env python3
import os
import sys
import json
import time
import urllib.request
import app  # আপনার বর্তমান app.py ফাইল থেকে সব ফাংশন ইমপোর্ট করবে

STATE_FILE = "state.json"

def wait_for_whatsapp(timeout=45):
    print("⏳ WhatsApp ব্রীজ কানেক্ট হওয়ার জন্য অপেক্ষা করা হচ্ছে...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen("http://127.0.0.1:3000/status", timeout=3) as r:
                data = json.loads(r.read().decode())
                if data.get("connected"):
                    print("✅ WhatsApp কানেক্টেড!")
                    return True
        except Exception:
            pass
        time.sleep(2)
    print("⚠️ WhatsApp কানেক্ট হয়নি (টাইমআউট)।")
    return False

def load_saved_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                app.state.update(saved)
        except Exception:
            pass

def save_current_state(wa_conn=True):
    out = dict(app.state)
    out["wa_connected"] = wa_conn
    out["scanning"] = False
    out["sending"] = False
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("💾 state.json সফলভাবে আপডেট হয়েছে।")

if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "scan"
    payload_raw = os.environ.get("ACTION_PAYLOAD", "{}")

    wa_ok = wait_for_whatsapp()
    load_saved_state()

    if action == "scan":
        app.run_scan_task(force_refresh=False)
        save_current_state(wa_ok)

    elif action == "force_scan":
        app.run_scan_task(force_refresh=True)
        save_current_state(wa_ok)

    elif action == "send":
        if not wa_ok:
            print("❌ WhatsApp কানেক্ট না থাকায় মেসেজ পাঠানো সম্ভব হয়নি।")
            sys.exit(1)
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        ids = payload.get("ids", [])
        custom_msgs = payload.get("custom_messages", {})
        print(f"📨 মোট {len(ids)} জনকে মেসেজ পাঠানো শুরু হচ্ছে...")
        app.run_send_task(ids, custom_msgs)
        save_current_state(wa_ok)
