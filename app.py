import json
import re
import smtplib
import socket
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import os

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, render_template, send_from_directory  # type: ignore[import]  # pylint: disable=import-error

DB_FILE = "products.json"
CONFIG_FILE = "config.json"


app = Flask(
    __name__,
    static_folder="static",
    static_url_path="/static"
)

@app.route("/styles/<path:filename>")
def styles(filename):
    return app.send_from_directory("styles", filename)

@app.route("/sw.js")
def service_worker():
    return app.send_from_directory("static", "sw.js")
# ── DB helpers ────────────────────────────────────────────────────────────────

def load_db():
    if not os.path.exists(DB_FILE):
        return {"products": []}
    with open(DB_FILE) as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)

def find_product(db, product_id):
    return next((p for p in db["products"] if p["id"] == product_id), None)


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def get_base_url(cfg):
    """Return the clickable URL for the web UI (local IP or custom override)."""
    override = cfg.get("base_url", "").strip()
    if override:
        return override.rstrip("/")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"
    port = cfg.get("port", 5000)
    return f"http://{ip}:{port}"


# ── Scraping ──────────────────────────────────────────────────────────────────

def parse_price(text):
    text = text.strip()
    text = re.sub(r"[€$£¥\s]", "", text)
    m = re.match(r"^([\d.,]+)$", text)
    if not m:
        return None
    raw = m.group(1)
    if re.search(r",\d{2}$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None

def scrape_price(url, css_class):
    # ...existing code...
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }
    
    response = requests.get(url, headers=headers, timeout=10)
    # ...rest of code...

    try:
        # Add session for cookies and better handling
        session = requests.Session()
        session.headers.update(headers)
        
        # First request to get cookies
        r = session.get(url, timeout=15, allow_redirects=True)
        r.raise_for_status()
        
        soup = BeautifulSoup(r.text, "html.parser")
        el = soup.find(class_=css_class)
        if not el:
            # Try to find by alternative selectors
            el = soup.find(class_=css_class) or soup.find(attrs={"class": css_class})
            if not el:
                return None, f"Element with class '{css_class}' not found"
        
        price = parse_price(el.get_text())
        if price is None:
            return None, f"Could not parse price from: '{el.get_text().strip()}'"
        return price, None
    except requests.exceptions.RequestException as e:
        # Better error message
        if hasattr(e, 'response') and e.response is not None:
            if e.response.status_code == 403:
                return None, f"Website blocked the request (403). Try: 1) Check if the CSS class is correct 2) The site may have anti-bot protection"
        return None, str(e)


# ── Deal detection ─────────────────────────────────────────────────────────────

def get_deals(min_discount=1.0):
    db = load_db()
    deals = []
    for p in db["products"]:
        h = p.get("history", [])
        if len(h) < 2:
            continue
        current = h[-1]["price"]
        max_price = max(x["price"] for x in h[:-1])
        if max_price > 0 and current < max_price:
            discount = (max_price - current) / max_price * 100
            if discount >= min_discount:
                deals.append({
                    "id": p["id"],
                    "name": p.get("name", p["url"]),
                    "url": p["url"],
                    "price": current,
                    "prev_price": max_price,
                    "discount": discount,
                })
    return deals


# ── Notification backends ─────────────────────────────────────────────────────

def _deals_summary_text(deals, base_url):
    """Plain-text summary used by Telegram / ntfy / Gotify."""
    lines = [f"🏷️ {len(deals)} price drop(s) today\n"]
    for d in deals:
        lines.append(
            f"• {d['name']}\n"
            f"  {d['prev_price']:.2f} € → {d['price']:.2f} € (-{d['discount']:.1f}%)\n"
            f"  {d['url']}"
        )
    lines.append(f"\n🔗 {base_url}/?filter=deals")
    return "\n".join(lines)


def _send_email(cfg, deals, base_url):
    rows = ""
    for d in deals:
        rows += (
            f"<tr>"
            f"<td style='padding:8px;border-bottom:1px solid #333'>"
            f"<a href='{d['url']}' style='color:#6ee7b7'>{d['name']}</a></td>"
            f"<td style='padding:8px;border-bottom:1px solid #333;text-align:right'>{d['prev_price']:.2f} €</td>"
            f"<td style='padding:8px;border-bottom:1px solid #333;text-align:right;"
            f"color:#6ee7b7;font-weight:bold'>{d['price']:.2f} €</td>"
            f"<td style='padding:8px;border-bottom:1px solid #333;text-align:right;"
            f"color:#f87171'>-{d['discount']:.1f}%</td>"
            f"</tr>"
        )
    html = f"""
    <html><body style='background:#0f172a;color:#e2e8f0;font-family:var(--font-family);padding:24px'>
    <h2 style='color:#6ee7b7'>🏷️ Price Drops Today</h2>
    <table style='border-collapse:collapse;width:100%;max-width:640px'>
      <thead><tr style='color:#94a3b8;font-size:12px;text-transform:uppercase'>
        <th style='padding:8px;text-align:left'>Product</th>
        <th style='padding:8px;text-align:right'>Was</th>
        <th style='padding:8px;text-align:right'>Now</th>
        <th style='padding:8px;text-align:right'>Discount</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style='margin-top:20px'>
      <a href='{base_url}/?filter=deals'
         style='background:#34d399;color:#0b1120;padding:10px 20px;
                border-radius:6px;text-decoration:none;font-weight:700'>
        Open PriceTracker →
      </a>
    </p>
    </body></html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏷️ {len(deals)} price drop(s) detected"
    msg["From"] = cfg["smtp_from"]
    msg["To"] = cfg["smtp_to"]
    msg.attach(MIMEText(html, "html"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(cfg["smtp_host"], int(cfg.get("smtp_port", 465)), context=context) as server:
        server.login(cfg["smtp_user"], cfg["smtp_password"])
        server.sendmail(cfg["smtp_from"], cfg["smtp_to"], msg.as_string())


def _send_telegram(cfg, deals, base_url):
    token = cfg["telegram_token"]
    chat_id = cfg["telegram_chat_id"]
    text = _deals_summary_text(deals, base_url)
    # Inline keyboard with a single "Open PriceTracker" button
    keyboard = {
        "inline_keyboard": [[{
            "text": "Open PriceTracker →",
            "url": f"{base_url}/?filter=deals"
        }]]
    }
    requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "reply_markup": keyboard,
        },
        timeout=10,
    ).raise_for_status()


def _send_ntfy(cfg, deals, base_url):
    server = cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    topic  = cfg["ntfy_topic"]
    text   = _deals_summary_text(deals, base_url)
    requests.post(
        f"{server}/{topic}",
        data=text.encode("utf-8"),
        headers={
            "Title":    f"🏷️ {len(deals)} price drop(s)",
            "Priority": "high",
            "Tags":     "shopping,tada",
            "Click":    f"{base_url}/?filter=deals",
            "Actions":  f"view, Open PriceTracker, {base_url}/?filter=deals",
        },
        timeout=10,
    ).raise_for_status()


def _send_gotify(cfg, deals, base_url):
    server = cfg["gotify_server"].rstrip("/")
    token  = cfg["gotify_token"]
    text   = _deals_summary_text(deals, base_url)
    requests.post(
        f"{server}/message",
        headers={"X-Gotify-Key": token},
        json={
            "title":    f"🏷️ {len(deals)} price drop(s)",
            "message":  text,
            "priority": 7,
            "extras": {
                "client::notification": {
                    "click": {"url": f"{base_url}/?filter=deals"}
                }
            },
        },
        timeout=10,
    ).raise_for_status()


def dispatch_notifications(cfg, deals):
    """Fire all enabled notification channels. Returns list of results."""
    if not deals:
        return []
    base_url = get_base_url(cfg)
    results = []

    if cfg.get("email_enabled"):
        try:
            _send_email(cfg, deals, base_url)
            results.append({"channel": "email", "ok": True})
        except Exception as e:
            results.append({"channel": "email", "ok": False, "error": str(e)})

    if cfg.get("telegram_enabled"):
        try:
            _send_telegram(cfg, deals, base_url)
            results.append({"channel": "telegram", "ok": True})
        except Exception as e:
            results.append({"channel": "telegram", "ok": False, "error": str(e)})

    if cfg.get("ntfy_enabled"):
        try:
            _send_ntfy(cfg, deals, base_url)
            results.append({"channel": "ntfy", "ok": True})
        except Exception as e:
            results.append({"channel": "ntfy", "ok": False, "error": str(e)})

    if cfg.get("gotify_enabled"):
        try:
            _send_gotify(cfg, deals, base_url)
            results.append({"channel": "gotify", "ok": True})
        except Exception as e:
            results.append({"channel": "gotify", "ok": False, "error": str(e)})

    if cfg.get("webpush_enabled"):
        try:
            push_results = _send_web_push(cfg, deals, base_url)
            ok_count = sum(1 for r in push_results if r["ok"])
            results.append({"channel": "webpush", "ok": True, "sent": ok_count})
        except Exception as e:
            results.append({"channel": "webpush", "ok": False, "error": str(e)})

    return results


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/products", methods=["GET"])
def get_products():
    db = load_db()
    return jsonify(db["products"])


@app.route("/api/products", methods=["POST"])
def add_product():
    body = request.json
    url = body.get("url", "").strip()
    css_class = body.get("css_class", "").strip()
    name = body.get("name", "").strip() or url

    if not url or not css_class:
        return jsonify({"error": "url and css_class are required"}), 400

    db = load_db()
    if any(p["url"] == url and p["css_class"] == css_class for p in db["products"]):
        return jsonify({"error": "Product already tracked"}), 409

    price, err = scrape_price(url, css_class)
    if err:
        return jsonify({"error": err}), 422

    product = {
        "id": datetime.utcnow().isoformat(),
        "url": url,
        "css_class": css_class,
        "name": name,
        "history": [{"date": datetime.utcnow().isoformat(), "price": price}],
    }
    db["products"].append(product)
    save_db(db)
    return jsonify(product), 201


@app.route("/api/products/<product_id>", methods=["DELETE"])
def delete_product(product_id):
    db = load_db()
    before = len(db["products"])
    db["products"] = [p for p in db["products"] if p["id"] != product_id]
    if len(db["products"]) == before:
        return jsonify({"error": "Not found"}), 404
    save_db(db)
    return jsonify({"ok": True})


@app.route("/api/products/<product_id>/refresh", methods=["POST"])
def refresh_product(product_id):
    db = load_db()
    product = find_product(db, product_id)
    if not product:
        return jsonify({"error": "Not found"}), 404
    price, err = scrape_price(product["url"], product["css_class"])
    if err:
        return jsonify({"error": err}), 422
    product["history"].append({"date": datetime.utcnow().isoformat(), "price": price})
    save_db(db)
    return jsonify(product)


@app.route("/api/refresh-all", methods=["POST"])
def refresh_all():
    db = load_db()
    results = []
    for product in db["products"]:
        price, err = scrape_price(product["url"], product["css_class"])
        if err:
            results.append({"id": product["id"], "error": err})
        else:
            product["history"].append({"date": datetime.utcnow().isoformat(), "price": price})
            results.append({"id": product["id"], "price": price})
    save_db(db)
    return jsonify(results)


@app.route("/api/notify", methods=["POST"])
def send_notification():
    cfg = load_config()
    deals = get_deals()
    if not deals:
        return jsonify({"message": "No deals right now"})
    results = dispatch_notifications(cfg, deals)
    return jsonify({"results": results})


@app.route("/api/config", methods=["GET"])
def get_config_route():
    cfg = load_config()
    HIDDEN = {"smtp_password", "telegram_token", "gotify_token", "vapid_private_key"}
    safe = {k: v for k, v in cfg.items() if k not in HIDDEN}
    return jsonify(safe)


@app.route("/api/config", methods=["POST"])
def set_config():
    body = request.json
    cfg = load_config()
    cfg.update(body)
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/test-notify/<channel>", methods=["POST"])
def test_notify(channel):
    """Send a test notification on a single channel."""
    cfg = load_config()
    base_url = get_base_url(cfg)
    
    # Use real deals from tracked products
    deals = get_deals()
    
    if not deals:
        return jsonify({"message": "No deals right now — add products with price drops to test"}), 200
    
    try:
        if channel == "email":
            _send_email(cfg, deals, base_url)
        elif channel == "telegram":
            _send_telegram(cfg, deals, base_url)
        elif channel == "ntfy":
            _send_ntfy(cfg, deals, base_url)
        elif channel == "gotify":
            _send_gotify(cfg, deals, base_url)
        elif channel == "webpush":
            results = _send_web_push(cfg, deals, base_url)
            if not results:
                return jsonify({"error": "No browser subscriptions yet — enable notifications in the UI first."}), 400
            failed = [r for r in results if not r["ok"]]
            if failed:
                return jsonify({"error": str(failed[0]["error"])}), 500
        else:
            return jsonify({"error": "Unknown channel"}), 400
        return jsonify({"ok": True, "deals_sent": len(deals)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Also expose push sub count for the UI ─────────────────────────────────────
@app.route("/api/push/subscriptions/count")
def push_sub_count():
    return jsonify({"count": len(load_push_subs())})


# ── VAPID key generation + self-signed TLS cert ───────────────────────────────

def _ensure_vapid_keys():
    """Generate VAPID key pair and store in config if not already present."""
    cfg = load_config()
    if cfg.get("vapid_private_key") and cfg.get("vapid_public_key"):
        return
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    import base64
    priv = ec.generate_private_key(ec.SECP256R1())
    pub  = priv.public_key()
    priv_b64 = base64.urlsafe_b64encode(
        priv.private_numbers().private_value.to_bytes(32, "big")
    ).rstrip(b"=").decode()
    pub_b64 = base64.urlsafe_b64encode(
        pub.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    ).rstrip(b"=").decode()
    cfg["vapid_private_key"] = priv_b64
    cfg["vapid_public_key"]  = pub_b64
    save_config(cfg)
    print(f"[vapid] Generated new VAPID key pair.")


def _ensure_tls_cert(cert_file="cert.pem", key_file="key.pem"):
    """Create a self-signed certificate if one doesn't exist yet."""
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return cert_file, key_file
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime, ipaddress
    except ImportError:
        print("[tls] cryptography package not found; run: pip install cryptography")
        return None, None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); local_ip = s.getsockname()[0]; s.close()
    except Exception:
        local_ip = "127.0.0.1"

    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "PriceTracker.local"),
    ])
    san = x509.SubjectAlternativeName([
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address(local_ip)),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(san, critical=False)
        .sign(key, hashes.SHA256())
    )
    with open(cert_file, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_file, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    print(f"[tls] Self-signed certificate created for {local_ip} (valid 10 years).")
    return cert_file, key_file




# ══════════════════════════════════════════════════════════════════════════════
# WEB PUSH
# ══════════════════════════════════════════════════════════════════════════════
PUSH_SUB_FILE = "push_subscriptions.json"

def load_push_subs():
    if not os.path.exists(PUSH_SUB_FILE):
        return []
    with open(PUSH_SUB_FILE) as f:
        return json.load(f)

def save_push_subs(subs):
    with open(PUSH_SUB_FILE, "w") as f:
        json.dump(subs, f, indent=2)


@app.route("/api/push/vapid-public-key")
def vapid_public_key():
    cfg = load_config()
    return jsonify({"key": cfg.get("vapid_public_key", "")})


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    sub = request.json
    if not sub or "endpoint" not in sub:
        return jsonify({"error": "Invalid subscription"}), 400
    subs = load_push_subs()
    # Deduplicate by endpoint
    if not any(s["endpoint"] == sub["endpoint"] for s in subs):
        subs.append(sub)
        save_push_subs(subs)
    return jsonify({"ok": True})


@app.route("/api/push/unsubscribe", methods=["POST"])
def push_unsubscribe():
    body = request.json or {}
    endpoint = body.get("endpoint")
    subs = load_push_subs()
    subs = [s for s in subs if s.get("endpoint") != endpoint]
    save_push_subs(subs)
    return jsonify({"ok": True})


def _send_web_push(cfg, deals, base_url):
    """Push a notification to every saved browser subscription."""
    from pywebpush import webpush, WebPushException

    subs = load_push_subs()
    if not subs:
        return []

    vapid_private = cfg.get("vapid_private_key")
    vapid_public  = cfg.get("vapid_public_key")
    vapid_email   = cfg.get("vapid_email", "mailto:admin@PriceTracker.local")

    if not vapid_private or not vapid_public:
        raise ValueError("VAPID keys not configured")

    lines = [f"🏷️ {len(deals)} price drop(s) today"]
    for d in deals:
        lines.append(f"• {d['name']}  {d['prev_price']:.2f}€ → {d['price']:.2f}€  (-{d['discount']:.1f}%)")

    payload = json.dumps({
        "title": f"🏷️ {len(deals)} deal(s) on PriceTracker",
        "body":  "\n".join(lines[1:]) or "Tap to see the deals.",
        "url":   f"{base_url}/?filter=deals",
        "tag":   "PriceTracker-deals",
    })

    results = []
    dead = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=vapid_private,
                vapid_claims={"sub": vapid_email},
            )
            results.append({"endpoint": sub["endpoint"][:40], "ok": True})
        except WebPushException as e:
            code = e.response.status_code if e.response is not None else 0
            results.append({"endpoint": sub["endpoint"][:40], "ok": False, "error": str(e)})
            # 404 / 410 = subscription expired, remove it
            if code in (404, 410):
                dead.append(sub["endpoint"])

    if dead:
        alive = [s for s in subs if s["endpoint"] not in dead]
        save_push_subs(alive)

    return results
if __name__ == "__main__":
    _ensure_vapid_keys()
    cert, key = _ensure_tls_cert()

    cfg  = load_config()
    port = int(cfg.get("port", 5000))

    if cert and key:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(cert, key)
        print(f"[PriceTracker] Running on https://0.0.0.0:{port}  (self-signed TLS)")
        print(f"[PriceTracker] First visit: accept the certificate warning in your browser.")
        app.run(host="0.0.0.0", port=port, debug=False, ssl_context=(cert, key))
    else:
        print(f"[PriceTracker] Running on http://0.0.0.0:{port}  (no TLS — web push won't work from other devices)")
        app.run(host="0.0.0.0", port=port, debug=False)
