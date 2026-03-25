import hashlib
import random
import string
import time
import uuid
import requests
import threading
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config["SECRET_KEY"] = "yollo-secret-2025"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Global job store ───────────────────────────────────────────────────────────
jobs      = {}   # job_id -> { status, logs, result_url, prompt, created_at, image_name }
jobs_lock = threading.Lock()

# ── YOLLO CONFIG ──────────────────────────────────────────────────────────────
BASE_URL  = "https://www.yollo.ai"
SCENE_JOB = "6"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Brave/146",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9", "en-GB,en;q=0.9", "de-DE,de;q=0.9,en;q=0.8",
    "fr-FR,fr;q=0.9,en;q=0.8", "es-ES,es;q=0.9,en;q=0.8",
    "tr-TR,tr;q=0.9,en;q=0.8", "pt-BR,pt;q=0.9,en;q=0.8",
]


def generate_finger() -> str:
    return hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()


def generate_ga_id() -> str:
    return f"GA1.1.{random.randint(100000000,999999999)}.{random.randint(1700000000,1780000000)}"


def generate_ga_session() -> str:
    ts  = random.randint(1770000000, 1780000000)
    seq = random.randint(1, 20)
    j   = random.randint(0, 60)
    return f"GS2.1.s{ts}$o{seq}$g0$t{ts}$j{j}$l0$h0"


def build_identity() -> dict:
    ga_key = "".join(random.choices(string.ascii_uppercase + string.digits, k=10))
    return {
        "finger":          generate_finger(),
        "user_agent":      random.choice(USER_AGENTS),
        "accept_language": random.choice(ACCEPT_LANGUAGES),
        "cookies": {
            "NEXT_LOCALE": "tr",
            "_ga":         generate_ga_id(),
            f"_ga_{ga_key}": generate_ga_session(),
        },
    }


def get_headers(identity: dict, auth_token: str = None) -> dict:
    h = {
        "accept":          "application/json, text/plain, */*",
        "accept-language": identity["accept_language"],
        "origin":          BASE_URL,
        "x-language":      "tr",
        "x-platform":      "web",
        "x-version":       "999.0.0",
        "x-finger":        identity["finger"],
        "user-agent":      identity["user_agent"],
    }
    if auth_token:
        h["x-auth-token"] = auth_token
    return h


# ── PROXY ─────────────────────────────────────────────────────────────────────
PROXYSCRAPE_URL = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&proxy_format=protocolipport&format=text"
)


def fetch_proxies() -> list:
    try:
        r = requests.get(PROXYSCRAPE_URL, timeout=10)
        proxies = [line.strip() for line in r.text.splitlines() if line.strip()]
        random.shuffle(proxies)
        return proxies
    except Exception:
        return []


def test_proxy(proxy_url: str, timeout: int = 5) -> bool:
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        r = requests.get(BASE_URL, proxies=proxies, timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def find_working_proxy(job_id: str, max_workers: int = 30):
    import queue as _queue
    proxy_list = fetch_proxies()
    log(job_id, "info", f"Proxy listesi çekildi — {len(proxy_list)} adet taranacak")
    if not proxy_list:
        return None

    result_q     = _queue.Queue()
    found_event  = threading.Event()
    counter_lock = threading.Lock()
    tested_count = [0]
    total        = len(proxy_list)

    def probe(proxy: str):
        if found_event.is_set():
            return
        ok = test_proxy(proxy)
        with counter_lock:
            tested_count[0] += 1
            idx  = tested_count[0]
            last = (idx == total)
        if ok and not found_event.is_set():
            found_event.set()
            result_q.put(proxy)
            log(job_id, "success", f"Çalışan proxy bulundu [{idx}/{total}]: {proxy}")
        else:
            log(job_id, "muted", f"[{idx}/{total}] ✗ {proxy}")
            if last:
                result_q.put(None)

    log(job_id, "info", f"Paralel proxy taraması başlıyor ({max_workers} thread)...")
    executor = ThreadPoolExecutor(max_workers=max_workers)
    executor.map(lambda p: probe(p), proxy_list)
    working = result_q.get()
    found_event.set()
    executor.shutdown(wait=False, cancel_futures=True)
    if not working:
        log(job_id, "warn", "Çalışan proxy bulunamadı, proxysiz devam edilecek.")
    return working


def make_session(proxy_url=None) -> requests.Session:
    s = requests.Session()
    if proxy_url:
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s


# ── LOGGING ───────────────────────────────────────────────────────────────────
def log(job_id: str, level: str, message: str):
    ts    = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    entry = {"ts": ts, "level": level, "msg": message}
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["logs"].append(entry)
    socketio.emit("log", {"job_id": job_id, **entry}, room=f"job_{job_id}")


def set_status(job_id: str, status: str, extra: dict = None):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = status
            if extra:
                jobs[job_id].update(extra)
    payload = {"job_id": job_id, "status": status}
    if extra:
        payload.update(extra)
    socketio.emit("status",      payload,          room=f"job_{job_id}")
    socketio.emit("jobs_update", get_jobs_summary())


# ── CORE JOB RUNNER ───────────────────────────────────────────────────────────
def run_job(job_id: str, image_bytes: bytes, image_name: str,
            prompt: str, resolution: str, length: int, max_workers: int):
    set_status(job_id, "running")
    log(job_id, "info", f"İş başlatıldı | Prompt: '{prompt}' | {resolution} | {length}s")

    direct_session = make_session(None)   # polling hep proxysiz
    attempt = 0

    while True:
        attempt += 1
        log(job_id, "info", f"── Deneme #{attempt} ──")

        with jobs_lock:
            if jobs[job_id].get("cancelled"):
                log(job_id, "warn", "İş iptal edildi.")
                set_status(job_id, "cancelled")
                return

        working_proxy   = find_working_proxy(job_id, max_workers=max_workers)
        proxied_session = make_session(working_proxy)
        identity        = build_identity()

        log(job_id, "info", f"Kimlik → finger: {identity['finger'][:16]}... | UA: {identity['user_agent'][:40]}...")

        # 1. Guest oluştur ────────────────────────────────────────────────────
        try:
            resp = proxied_session.post(
                f"{BASE_URL}/api/auth/createGuest",
                headers=get_headers(identity),
                cookies=identity["cookies"],
            )
            body = resp.json()
            assert body.get("code") == "200", f"createGuest hatası: {body}"
            guest     = body["data"]
            guest_uid = guest["guestUid"]
            guest_key = guest["guestKey"]
            log(job_id, "success", f"Guest oluşturuldu → uid: {guest_uid}")
        except Exception as e:
            log(job_id, "error", f"createGuest başarısız: {e} — yeniden deneniyor...")
            continue

        # 2. Token al ─────────────────────────────────────────────────────────
        try:
            resp = proxied_session.post(
                f"{BASE_URL}/api/auth/loginByGuest",
                headers=get_headers(identity),
                cookies=identity["cookies"],
                json={"guestKey": guest_key, "guestUid": guest_uid},
            )
            body = resp.json()
            assert body.get("code") == "200", f"loginByGuest hatası: {body}"
            auth_token = body["data"]["idToken"]
            log(job_id, "success", f"Token alındı → {auth_token[:24]}...")
        except Exception as e:
            log(job_id, "error", f"loginByGuest başarısız: {e} — yeniden deneniyor...")
            continue

        # 3. Görseli yükle ────────────────────────────────────────────────────
        try:
            ext  = os.path.splitext(image_name)[1].lower()
            mime = ("image/jpeg" if ext in (".jpg", ".jpeg")
                    else "image/png" if ext == ".png"
                    else "image/webp")
            files = {"file": (image_name, image_bytes, mime)}
            resp  = proxied_session.post(
                f"{BASE_URL}/api/upload/uploadTempFile",
                headers=get_headers(identity, auth_token),
                cookies=identity["cookies"],
                files=files,
            )
            body = resp.json()
            assert body.get("code") == "200", f"uploadTempFile hatası: {body}"
            cdn_url = body["data"]
            log(job_id, "success", f"Görsel yüklendi → {cdn_url[:60]}...")
        except Exception as e:
            log(job_id, "error", f"uploadTempFile başarısız: {e} — yeniden deneniyor...")
            continue

        # 4. Video oluştur ─────────────────────────────────────────────────────
        try:
            log(job_id, "info", "Video isteği gönderiliyor (createAiVideo)...")
            resp = proxied_session.post(
                f"{BASE_URL}/api/aiVideo/createAiVideo",
                headers=get_headers(identity, auth_token),
                cookies=identity["cookies"],
                json={
                    "baseImage":   cdn_url,
                    "extraImage":  "",
                    "prompt":      prompt,
                    "resolution":  resolution,
                    "length":      length,
                    "permission":  "2",
                    "enableAudio": False,
                },
            )
            body = resp.json()
            assert body.get("code") == "200", f"createAiVideo hatası: {body}"
            job_api_id = body["data"]["result"]["jobId"]
            log(job_id, "success", f"Job ID alındı: {job_api_id}")
            set_status(job_id, "polling", {"api_job_id": job_api_id})
        except Exception as e:
            log(job_id, "error", f"createAiVideo başarısız: {e} — yeniden deneniyor...")
            continue

        break  # başarılı, döngüden çık

    # ── Polling (proxysiz) ────────────────────────────────────────────────────
    log(job_id, "info", "Polling başladı (proxysiz)...")
    while True:
        with jobs_lock:
            if jobs[job_id].get("cancelled"):
                log(job_id, "warn", "İş iptal edildi (polling sırasında).")
                set_status(job_id, "cancelled")
                return
        try:
            resp = direct_session.post(
                f"{BASE_URL}/api/aiVideo/checkJobStatus",
                headers=get_headers(identity, auth_token),
                cookies=identity["cookies"],
                json={"scene": SCENE_JOB, "jobIds": [job_api_id]},
            )
            body       = resp.json()
            assert body.get("code") == "200", f"checkJobStatus hatası: {body}"
            job_data   = body["data"][0]
            status     = job_data["status"]
            result_url = job_data.get("resultUrl")
            log(job_id, "info", f"Durum: {status} | resultUrl: {'var' if result_url else 'yok'}")

            if status == 2 and result_url:
                log(job_id, "success", f"Video hazır! URL: {result_url}")
                set_status(job_id, "done", {"result_url": result_url})
                return
            if status == 3:
                log(job_id, "error", "Video üretimi başarısız (status=3).")
                set_status(job_id, "failed")
                return
        except Exception as e:
            log(job_id, "error", f"Polling hatası: {e}")

        time.sleep(5)


# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_jobs_summary():
    with jobs_lock:
        return [
            {
                "id":         jid,
                "status":     j["status"],
                "prompt":     j["prompt"],
                "image_name": j["image_name"],
                "created_at": j["created_at"],
                "result_url": j.get("result_url"),
            }
            for jid, j in jobs.items()
        ]


# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start_job():
    image_file  = request.files.get("image")
    prompt      = request.form.get("prompt", "")
    resolution  = request.form.get("resolution", "480p")
    length      = int(request.form.get("length", 5))
    max_workers = int(request.form.get("max_workers", 30))

    if not image_file:
        return jsonify({"error": "Görsel gerekli"}), 400

    image_bytes = image_file.read()
    image_name  = image_file.filename
    job_id      = uuid.uuid4().hex[:10]

    with jobs_lock:
        jobs[job_id] = {
            "status":     "queued",
            "logs":       [],
            "result_url": None,
            "prompt":     prompt or "(yok)",
            "image_name": image_name,
            "created_at": datetime.now().strftime("%H:%M:%S"),
            "cancelled":  False,
        }

    threading.Thread(
        target=run_job,
        args=(job_id, image_bytes, image_name, prompt, resolution, length, max_workers),
        daemon=True,
    ).start()

    socketio.emit("jobs_update", get_jobs_summary())
    return jsonify({"job_id": job_id})


@app.route("/api/jobs")
def list_jobs():
    return jsonify(get_jobs_summary())


@app.route("/api/job/<job_id>")
def get_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Bulunamadı"}), 404
    return jsonify(job)


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["cancelled"] = True
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/delete", methods=["DELETE"])
def delete_job(job_id):
    with jobs_lock:
        jobs.pop(job_id, None)
    socketio.emit("jobs_update", get_jobs_summary())
    return jsonify({"ok": True})


# ── SOCKETIO ──────────────────────────────────────────────────────────────────
@socketio.on("subscribe")
def on_subscribe(data):
    job_id = data.get("job_id")
    if job_id:
        join_room(f"job_{job_id}")
        with jobs_lock:
            job = jobs.get(job_id, {})
        for entry in job.get("logs", []):
            emit("log", {"job_id": job_id, **entry})


if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000)
