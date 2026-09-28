#!/usr/bin/env python3
import urllib.request
import urllib.parse
import json
import csv
import io
import re
import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

# ============ গোপন কনফিগারেশন (শুধু Termux-এ থাকবে) ============
PORT        = 5001
SHEET_ID    = '1pjtp8_W7ScOmrpakNHNN6fUAnuNfM4-ZC3LawHMtFIw'
SHEET_NAME  = 'OLT'
ISP_NAME    = "Meherpur Internet"
SENDER_NAME = "Shamim Reza"
WA_BRIDGE   = "http://127.0.0.1:3000"
CACHE_FILE  = "smart_cache.json"
# ===============================================================

BD_TZ = timezone(timedelta(hours=6))
cache_lock = threading.Lock()

state = {
    "scanning": False,
    "scan_progress": 0,
    "scan_total": 0,
    "scan_status": "স্ক্যান করার জন্য প্রস্তুত",
    "sending": False,
    "send_progress": 0,
    "send_total": 0,
    "send_status": "",
    "last_scan_time": "",
    "results": []
}

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                data.setdefault("users", {})
                data.setdefault("wa_numbers", {})
                data.setdefault("sent_log", {})
                return data
        except Exception:
            pass
    return {"users": {}, "wa_numbers": {}, "sent_log": {}}

def save_cache(cache_data):
    with cache_lock:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

def to_bangla_digits(text):
    return str(text).translate(str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯"))

def normalize_phone(phone):
    digits = re.sub(r'\D', '', phone)
    if digits.startswith('0') and len(digits) == 11:
        digits = '88' + digits
    elif digits.startswith('1') and len(digits) == 10:
        digits = '880' + digits
    return digits

def local_phone_format(digits):
    if digits.startswith('880') and len(digits) == 13:
        return digits[2:]
    return digits

def parse_expire(expire_str):
    clean_str = (expire_str or '').strip()
    for fmt in ('%d-%m-%Y %H:%M:%S', '%d-%m-%Y', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(clean_str, fmt)
        except ValueError:
            continue
    return None

def build_customer_message(name, idname, exp_dt, days_left):
    expire_date_bn = to_bangla_digits(exp_dt.strftime('%d-%m-%Y'))
    pay_link = f"https://ispbill.com/pay.php?c=1293&q={urllib.parse.quote(idname.lower())}"

    if days_left == 0:
        day_label = "আজ মেয়াদ শেষ"
        time_phrase = f"আজ {expire_date_bn} বিকাল ৪:০০টায়"
    elif days_left == 1:
        day_label = "আগামীকাল মেয়াদ শেষ"
        time_phrase = f"আগামীকাল, {expire_date_bn} বিকাল ৪:০০টায়"
    else:
        days_bn = to_bangla_digits(days_left)
        day_label = f"আগামী {days_bn} দিন পর"
        time_phrase = f"আগামী {days_bn} দিন পর, {expire_date_bn} বিকাল ৪:০০টায়"

    msg = (
        f"আসসালামু আলাইকুম {name},\n\n"
        f"আপনার ইন্টারনেট সংযোগের মেয়াদ {time_phrase} শেষ হবে। সংযোগ চালু রাখতে অনুগ্রহ করে এর পূর্বেই বিল পরিশোধ করুন। আপনার ইউজার আইডি: {idname}\n\n"
        f"অনলাইন পেমেন্ট:\n"
        f"{pay_link}\n\n"
        f"ধন্যবাদান্তে,\n"
        f"{SENDER_NAME}\n"
        f"{ISP_NAME}"
    )
    return day_label, msg

def fetch_ispbill(idname):
    url = f'https://ispbill.com/pay.php?c=1293&q={urllib.parse.quote(idname)}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode('utf-8', errors='ignore')

    def extract(label):
        pattern = rf'<td[^>]*>\s*{re.escape(label)}\s*:?\s*</td>\s*<td[^>]*>(.*?)</td>'
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if not m: return ''
        val = re.sub(r'<[^>]+>', ' ', m.group(1))
        return re.sub(r'\s+', ' ', val).strip()

    return {
        "expire_str": extract('Current Expire Date'),
        "bill": extract('Bill Amount'),
        "package": extract('Package Name')
    }

def check_wa_number(digits):
    payload = json.dumps({"phone": digits}).encode('utf-8')
    req = urllib.request.Request(f"{WA_BRIDGE}/check", data=payload, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return {"ok": False}

def send_wa_message(digits, jid, message):
    payload = json.dumps({"to": digits, "jid": jid, "message": message}).encode('utf-8')
    req = urllib.request.Request(f"{WA_BRIDGE}/send", data=payload, method='POST')
    req.add_header('Content-Type', 'application/json; charset=utf-8')
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            res = json.loads(resp.read().decode('utf-8'))
            return res.get('ok', False)
    except Exception:
        return False

def run_scan_task(force_refresh=False):
    state["scanning"] = True
    state["scan_progress"] = 0
    state["results"] = []
    state["scan_status"] = "Google Sheet থেকে গ্রাহকদের তালিকা লোড হচ্ছে..."

    print("\n" + "=" * 65)
    print("🔍 [SCAN STARTED] ৩ দিন, আগামীকাল ও আজকের গ্রাহক স্ক্যান হচ্ছে...")

    cache = load_cache()
    if force_refresh:
        cache["users"] = {}

    now_bd = datetime.now(BD_TZ).replace(tzinfo=None)
    today_date = now_bd.date()
    today_str = today_date.strftime('%Y-%m-%d')
    current_hour = now_bd.hour

    sheet_url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={SHEET_NAME}'
    try:
        req = urllib.request.Request(sheet_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode('utf-8')
    except Exception as e:
        state["scan_status"] = f"Google Sheet পড়া যায়নি: {e}"
        state["scanning"] = False
        return

    rows = []
    for r in csv.DictReader(io.StringIO(raw)):
        name = (r.get('NAME') or '').strip()
        idname = (r.get('ID NAME') or '').strip()
        phone = (r.get('PHONE NO') or '').strip()
        if name and idname and phone:
            rows.append({"name": name, "idname": idname, "phone": phone})

    state["scan_total"] = len(rows)
    if not rows:
        state["scanning"] = False
        return

    to_fetch = []
    for item in rows:
        idname = item["idname"]
        u_cache = cache["users"].get(idname)
        need_live = False

        if not u_cache:
            need_live = True
        elif u_cache.get("name") != item["name"] or u_cache.get("phone") != item["phone"]:
            need_live = True
        else:
            exp_dt = parse_expire(u_cache.get("expire_str", ""))
            if not exp_dt:
                need_live = True
            else:
                d_left = (exp_dt.date() - today_date).days
                if d_left <= 3 and u_cache.get("checked_date") != today_str:
                    need_live = True

        if need_live:
            to_fetch.append(item)

    completed_count = len(rows) - len(to_fetch)
    state["scan_progress"] = completed_count

    if to_fetch:
        state["scan_status"] = f"ispbill থেকে {len(to_fetch)} জনের লাইভ ডেটা আপডেট হচ্ছে..."
        def worker(item):
            try:
                return item, fetch_ispbill(item["idname"])
            except Exception:
                return item, {}

        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(worker, it) for it in to_fetch]
            for fut in as_completed(futures):
                item, info = fut.result()
                completed_count += 1
                state["scan_progress"] = completed_count
                state["scan_status"] = f"স্ক্যান হচ্ছে: {item['name']} ({completed_count}/{len(rows)})"
                if info.get("expire_str"):
                    cache["users"][item["idname"]] = {
                        "name": item["name"],
                        "phone": item["phone"],
                        "expire_str": info["expire_str"],
                        "bill": info.get("bill", ""),
                        "package": info.get("package", ""),
                        "checked_date": today_str
                    }

    save_cache(cache)

    candidates = []
    for item in rows:
        idname = item["idname"]
        u_cache = cache["users"].get(idname, {})
        expire_str = u_cache.get("expire_str", "")
        exp_dt = parse_expire(expire_str)
        if not exp_dt:
            continue

        days_left = (exp_dt.date() - today_date).days
        if 0 <= days_left <= 3:
            candidates.append((item, u_cache, exp_dt, days_left))

    candidates.sort(key=lambda x: (x[3], x[0]["idname"]))
    state["scan_status"] = f"প্রাপ্ত {len(candidates)} জন গ্রাহকের WhatsApp যাচাই হচ্ছে..."
    final_list = []

    for idx, (item, u_cache, exp_dt, days_left) in enumerate(candidates, 1):
        name = item["name"]
        idname = item["idname"]
        digits = normalize_phone(item["phone"])
        local_num = local_phone_format(digits)
        valid_format = (len(digits) == 13 and digits.startswith('8801'))

        has_wa = False
        jid = None

        if valid_format:
            if digits in cache["wa_numbers"]:
                has_wa = cache["wa_numbers"][digits].get("exists", False)
                jid = cache["wa_numbers"][digits].get("jid")
            else:
                res = check_wa_number(digits)
                if res.get("ok"):
                    has_wa = res.get("exists", False)
                    jid = res.get("jid")
                    cache["wa_numbers"][digits] = {"exists": has_wa, "jid": jid}

        day_label, msg = build_customer_message(name, idname, exp_dt, days_left)
        already_sent_today = (cache["sent_log"].get(idname) == today_str)
        expired_after_4pm = (days_left == 0 and (current_hour >= 16 or now_bd >= exp_dt))

        final_list.append({
            "name": name,
            "idname": idname,
            "phone": digits,
            "local_phone": local_num,
            "valid_phone": valid_format,
            "expire_date": exp_dt.strftime('%d-%m-%Y'),
            "days_left": days_left,
            "day_label": day_label,
            "has_wa": has_wa,
            "jid": jid,
            "message": msg,
            "sent": already_sent_today,
            "after_4pm": expired_after_4pm
        })

    save_cache(cache)
    state["results"] = final_list
    state["last_scan_time"] = now_bd.strftime('%I:%M %p')
    wa_yes = sum(1 for x in final_list if x["has_wa"])
    wa_no = len(final_list) - wa_yes
    state["scan_status"] = f"স্ক্যান সম্পন্ন! মোট {len(final_list)} জন (WA আছে: {wa_yes}, নেই: {wa_no})"
    state["scanning"] = False
    print(f"✅ [SCAN DONE] মোট: {len(final_list)} জন | WA আছে: {wa_yes} | নেই: {wa_no}")

def run_send_task(selected_ids, custom_messages=None):
    custom_messages = custom_messages or {}
    targets = [u for u in state["results"] if u["idname"] in selected_ids and u["has_wa"]]

    state["sending"] = True
    state["send_progress"] = 0
    state["send_total"] = len(targets)

    now_bd = datetime.now(BD_TZ).replace(tzinfo=None)
    today_str = now_bd.date().strftime('%Y-%m-%d')
    cache = load_cache()

    sent_now = 0
    for idx, user in enumerate(targets, 1):
        idname = user["idname"]
        msg_to_send = custom_messages.get(idname, user["message"])
        state["send_status"] = f"পাঠানো হচ্ছে: {user['name']} ({idx}/{len(targets)})"
        print(f"📨 [{idx}/{len(targets)}] {user['name']} ({idname}) → {user['phone']}")

        if send_wa_message(user["phone"], user["jid"], msg_to_send):
            user["sent"] = True
            cache["sent_log"][idname] = today_str
            save_cache(cache)
            sent_now += 1

        state["send_progress"] = idx
        if idx < len(targets):
            time.sleep(3.5)

    state["send_status"] = f"✅ সম্পন্ন! {sent_now} জন গ্রাহককে সফলভাবে মেসেজ পাঠানো হয়েছে।"
    state["sending"] = False
    print(f"✅ [SENDING DONE] মোট {sent_now} জনকে পাঠানো হয়েছে।")

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class BotRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Accept')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/state':
            wa_conn = False
            try:
                with urllib.request.urlopen(f"{WA_BRIDGE}/status", timeout=2) as r:
                    wa_conn = json.loads(r.read().decode()).get("connected", False)
            except Exception:
                pass
            resp = dict(state)
            resp["wa_connected"] = wa_conn
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode('utf-8'))
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status":"Termux Backend Active"}')

    def do_POST(self):
        content_len = int(self.headers.get('Content-Length', 0))
        raw_body = self.rfile.read(content_len).decode('utf-8') if content_len > 0 else '{}'

        if self.path.startswith('/api/scan'):
            force = 'force=1' in self.path
            if not state["scanning"]:
                threading.Thread(target=run_scan_task, args=(force,), daemon=True).start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        elif self.path == '/api/send':
            if not state["sending"]:
                try:
                    payload = json.loads(raw_body)
                except Exception:
                    payload = {}
                ids = payload.get("ids", [])
                custom_msgs = payload.get("custom_messages", {})
                threading.Thread(target=run_send_task, args=(ids, custom_msgs), daemon=True).start()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

if __name__ == '__main__':
    print("🚀 Termux ব্যাকএন্ড ইঞ্জিন চালু হয়েছে (Port 5001)")
    server = ThreadedHTTPServer(('127.0.0.1', PORT), BotRequestHandler)
    server.serve_forever()

