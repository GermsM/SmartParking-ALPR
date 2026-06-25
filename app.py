from flask import Flask, flash, jsonify, redirect, render_template, request, Response, session, url_for
import cv2
from ultralytics import YOLO
import config
import pytesseract
import numpy as np
import time
import re
import threading
import urllib.request
import urllib.parse
from datetime import datetime

from access_logging import (
    confirm_entry_in_db,
    confirm_exit_in_db,
    get_present_plates,
    check_absence_exits,
    check_long_stay_violations,
    process_forbidden_vehicle,
    init_presence_from_db,
)
from admin_users import admin_bp
from auth import auth
from dashboard_stats import get_dashboard_kpis
from db_init import init_app_database
from email_service import build_long_stay_whatsapp_message, notify_owner_long_stay
from logs import logs_bp
from models import db, Vehicle, Site
from notifications import create_notification, maybe_create_export_reminder, notifications_bp
from security_alerts import (
    get_security_alert_state,
    get_banned_plates,
    get_vehicle_info,
    log_banned_detection_throttled,
    signal_banned_plate_detected,
    signal_forbidden_type_detected,
    signal_unknown_plate_detected,
)
from site_policies import site_policy_bp
from vehicles import vehicles_bp

app = Flask(__name__)
app.config.from_object(config)

db.init_app(app)
app.register_blueprint(auth, url_prefix='/auth')
app.register_blueprint(vehicles_bp)
app.register_blueprint(logs_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(notifications_bp)
app.register_blueprint(site_policy_bp)


@app.context_processor
def inject_template_globals():
    from notifications import count_unread
    from models import Site

    try:
        sites_list = [s.name for s in Site.query.order_by(Site.name).all()]
        site_config_dict = {
            s.name: {
                "capacity": s.capacity,
                "code": s.code,
                "camera_url_entry": s.camera_url_entry,
                "camera_url_exit": s.camera_url_exit,
                "max_hours_student": s.max_hours_student,
                "max_hours_visitor": s.max_hours_visitor,
                "access_start": s.access_start,
                "access_end": s.access_end,
                "long_stay_hours": s.long_stay_hours,
            }
            for s in Site.query.all()
        }
    except Exception:
        sites_list = []
        site_config_dict = {}

    ctx = {
        "ucb_sites": tuple(sites_list),
        "site_config": site_config_dict,
    }
    
    if session.get("user_id") and session.get("role") == "admin":
        ctx["pending_vehicle_count"] = Vehicle.query.filter_by(status="pending").count()
    else:
        ctx["pending_vehicle_count"] = 0
        
    if session.get("user_id"):
        ctx["unread_notifications"] = count_unread(
            session.get("role"), session.get("site"), session.get("user_id")
        )
    else:
        ctx["unread_notifications"] = 0
    return ctx


init_app_database(app)
init_presence_from_db(app)

def _normalize_url(url: str) -> str:
    """Normalise une URL video :
       - Ajoute http:// si une adresse IP ou un hostname est detecte sans protocole
       - Ajoute le chemin /video si l'URL contient juste un IP:port nu
       - Enleve les espaces superflus
    """
    u = url.strip()
    if u and not u.startswith("http://") and not u.startswith("https://"):
        if re.match(r"^\d+\.\d+\.\d+\.\d+", u) or re.match(r"^[a-zA-Z0-9.-]+\.(local|lan)$", u):
            u = "http://" + u
    if u.startswith("http://") or u.startswith("https://"):
        parsed = urllib.parse.urlparse(u)
        if not parsed.path or parsed.path in ("/", ""):
            u = u.rstrip("/") + "/video"
    return u


def _rotate_to_portrait(frame):
    """Tourne la frame en mode portrait si elle est en paysage (largeur > hauteur).
       Les IP Webcam envoient souvent du 640x480 meme en tenant le telephone verticalement.
    """
    if frame is None:
        return frame
    h, w = frame.shape[:2]
    if w > h:
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    return frame


class CameraStream:
    """Lecteur asynchrone de flux video avec thread dedie.

    - Flux locaux (fichiers .mp4) : utilise OpenCV VideoCapture avec bouclage
    - Flux HTTP (IP Webcam MJPEG) : parse le MJPEG via urllib en cherchant les marqueurs JPEG
    - La frame la plus recente est stockee dans self.frame et lue par self.read()
    - Le thread d'arriere-plan tente de se reconnecter automatiquement en cas d'erreur
    """
    def __init__(self, url: str):
        u = _normalize_url(url)
        self.url = u
        self.cap = None          # Capture OpenCV pour les fichiers locaux
        self.frame = None        # Derniere frame decodee
        self.success = False     # True si une frame valide est disponible
        self.running = True      # Le thread doit continuer
        self.is_http = self.url.startswith("http://") or self.url.startswith("https://")
        self.http_stream = None  # Connexion HTTP pour MJPEG
        self.http_buffer = b""   # Buffer d'accumulation MJPEG
        self.lock = threading.Lock()
        self.consecutive_errors = 0
        print(f"[CameraStream] Initialisation avec URL: {self.url}")
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()

    def _read_http_frame(self):
        """Lit et decode une frame JPEG depuis un flux MJPEG HTTP (IP Webcam).

        Lit par paquets de 16KB, accumule dans un buffer, cherche les marqueurs
        JPEG \xff\xd8 (SOI) et \xff\xd9 (EOI), decode avec OpenCV, et retourne
        la frame. Les buffers trop volumineux (>5MB) sans marqueur sont reinitialises.
        """
        try:
            if self.http_stream is None:
                print(f"[CameraStream] Connexion a {self.url}...")
                req = urllib.request.Request(self.url, headers={"User-Agent": "OpenCV"})
                self.http_stream = urllib.request.urlopen(req, timeout=15)
                print(f"[CameraStream] Connecte a {self.url}")
                self.http_buffer = b""
            while self.running:
                chunk = self.http_stream.read(16384)
                if not chunk:
                    raise ConnectionError("Fin du flux HTTP")
                self.http_buffer += chunk
                a = self.http_buffer.find(b"\xff\xd8")
                b = self.http_buffer.find(b"\xff\xd9")
                if a != -1 and b != -1 and b > a:
                    jpg = self.http_buffer[a:b+2]
                    self.http_buffer = self.http_buffer[b+2:]
                    frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        self.consecutive_errors = 0
                        frame = _rotate_to_portrait(frame)
                        return True, frame
                    continue
                if len(self.http_buffer) > 5_000_000:
                    self.http_buffer = b""
            # self.running est devenu False (release() a ete appelle)
            return False, None
        except Exception as e:
            print(f"[CameraStream] Erreur HTTP ({type(e).__name__}): {e}")
            if self.http_stream:
                try: self.http_stream.close()
                except: pass
            self.http_stream = None
            self.http_buffer = b""
            self.consecutive_errors += 1
            # Retry progressif : attendre 1s, 2s, 4s... max 10s
            backoff = min(10, 2 ** self.consecutive_errors)
            time.sleep(backoff)
            return False, None

    def _update(self):
        """Boucle principale du thread. Alterne entre lecture HTTP MJPEG et VideoCapture OpenCV."""
        while self.running:
            if self.is_http:
                success, frame = self._read_http_frame()
                with self.lock:
                    if success:
                        self.frame = frame
                        self.success = True
                    else:
                        self.success = False
                continue

            # --- Flux fichiers locaux (OpenCV) ---
            if self.cap is None or not self.cap.isOpened():
                cap = cv2.VideoCapture(self.url)
                if cap.isOpened():
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    self.cap = cap
                else:
                    with self.lock:
                        self.success = False
                    time.sleep(5.0)
                    continue

            success, frame = self.cap.read()

            if not success:
                # Bouclage auto : revenir au debut de la video
                try:
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    success, frame = self.cap.read()
                except Exception:
                    pass

            with self.lock:
                if success:
                    self.frame = _rotate_to_portrait(frame)
                    self.success = True
                else:
                    self.success = False
                    if self.cap:
                        self.cap.release()
                    self.cap = None
                    time.sleep(2.0)

    def read(self):
        """Retourne (success: bool, frame: np.ndarray | None) de maniere thread-safe."""
        with self.lock:
            if self.success and self.frame is not None:
                return True, self.frame.copy()
            return False, None

    def release(self):
        """Arrete le thread et libere les ressources."""
        self.running = False
        if self.http_stream:
            try: self.http_stream.close()
            except: pass
            self.http_stream = None
        if self.cap:
            try:
                self.cap.release()
            except Exception:
                pass


model = YOLO('yolov8n.pt')

frame_skip = 4
frame_count = 0
_last_detections_by_site: dict[str, list] = {}
_caps: dict[str, CameraStream] = {}
_caps_lock = threading.Lock()
_long_stay_notified: set[str] = set()

# State machine pour la double-lecture
_authorized_entries: dict[str, dict] = {}  # plate -> {timestamp, guardian_id}
_authorized_exits: dict[str, dict] = {}    # plate -> {timestamp, guardian_id}

_gate_states: dict[str, dict] = {}         # site_name -> {entry_gate, exit_gate, entry_plate, exit_plate, last_update}
_gate_lock = threading.Lock()


def get_gate_state_for_site(site_name: str | None) -> dict:
    """Retourne et met a jour l'etat de la barriere (ouverture/fermeture temporisee)."""
    key = site_name or "__default__"
    with _gate_lock:
        if key not in _gate_states:
            _gate_states[key] = {
                "entry_gate": "closed",
                "exit_gate": "closed",
                "entry_plate": None,
                "exit_plate": None,
                "last_update": time.time()
            }
        
        state = _gate_states[key]
        now = time.time()
        elapsed = now - state["last_update"]

        # Transitions automatiques pour la simulation
        if state["entry_gate"] == "opening" and elapsed >= 3.0:
            state["entry_gate"] = "open"
            state["last_update"] = now
        elif state["entry_gate"] == "open" and elapsed >= 8.0:
            state["entry_gate"] = "closing"
            state["last_update"] = now
        elif state["entry_gate"] == "closing" and elapsed >= 3.0:
            state["entry_gate"] = "closed"
            state["entry_plate"] = None
            state["last_update"] = now

        if state["exit_gate"] == "opening" and elapsed >= 3.0:
            state["exit_gate"] = "open"
            state["last_update"] = now
        elif state["exit_gate"] == "open" and elapsed >= 8.0:
            state["exit_gate"] = "closing"
            state["last_update"] = now
        elif state["exit_gate"] == "closing" and elapsed >= 3.0:
            state["exit_gate"] = "closed"
            state["exit_plate"] = None
            state["last_update"] = now

        return dict(state)


def trigger_gate_simulation(site_name: str | None, direction: str, action: str, plate: str | None = None):
    """Declenche la simulation de la barriere (logicielle et physique IP)."""
    key = site_name or "__default__"
    
    # Resolution de l'IP du site
    ip_addr = "192.168.1.100"
    port_num = 80
    try:
        s_obj = Site.query.filter_by(name=site_name).first() if site_name else None
        if s_obj:
            if s_obj.gate_ip:
                # Format supporte: "192.168.1.200" ou "192.168.1.200:8080"
                parts = s_obj.gate_ip.split(":")
                ip_addr = parts[0].strip()
                if len(parts) > 1:
                    try:
                        port_num = int(parts[1].strip())
                    except ValueError:
                        pass
            else:
                ip_addr = f"192.168.1.{100 + s_obj.id}"
    except Exception:
        pass

    # Appel du module physique
    from physical_barrier import PhysicalBarrierController
    PhysicalBarrierController.trigger_gate(action, ip_address=ip_addr, port=port_num, site_name=site_name or "Par defaut")

    with _gate_lock:
        if key not in _gate_states:
            _gate_states[key] = {
                "entry_gate": "closed",
                "exit_gate": "closed",
                "entry_plate": None,
                "exit_plate": None,
                "last_update": time.time()
            }
        state = _gate_states[key]
        now = time.time()
        
        if direction == "entry":
            if action == "OPEN":
                state["entry_gate"] = "opening"
                state["entry_plate"] = plate
            elif action == "CLOSE":
                state["entry_gate"] = "closing"
            state["last_update"] = now
        elif direction == "exit":
            if action == "OPEN":
                state["exit_gate"] = "opening"
                state["exit_plate"] = plate
            elif action == "CLOSE":
                state["exit_gate"] = "closing"
            state["last_update"] = now


def _get_stream(site: str | None, camera_type: str = "entry") -> CameraStream:
    key = f"{site or '__default__'}_{camera_type}"
    url = ""
    
    # Recuperation de l'URL dans la base
    if site:
        try:
            s = Site.query.filter_by(name=site).first()
            if s:
                url = (s.camera_url_entry if camera_type == "entry" else s.camera_url_exit) or ""
        except Exception:
            pass
            
    # Fallback config
    if not url:
        cfg = config.SITE_CONFIG.get(site or "")
        if cfg:
            url = cfg.get(f"camera_url_{camera_type}", "") or ""
            
    if not url:
        url = "uploads/demo_video.mp4"

    # Normaliser pour comparaison coherente avec CameraStream._normalize_url() (appele dans __init__)
    url_norm = _normalize_url(url)

    with _caps_lock:
        stream = _caps.get(key)
        if stream is None or stream.url != url_norm or not stream.running:
            if stream:
                print(f"[_get_stream] Release ancien stream (url stream={stream.url}, url request={url_norm})")
                stream.release()
            print(f"[_get_stream] Nouveau CameraStream pour {key} -> {url}")
            stream = CameraStream(url)
            _caps[key] = stream
        return stream


def _release_capture(site: str | None, camera_type: str = "entry"):
    key = f"{site or '__default__'}_{camera_type}"
    with _caps_lock:
        stream = _caps.pop(key, None)
        if stream is not None:
            try:
                stream.release()
            except Exception:
                pass


def _get_placeholder_frame(message="PAS DE SIGNAL"):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    
    text_size = cv2.getTextSize(message, font, 0.9, 2)[0]
    text_x = (640 - text_size[0]) // 2
    text_y = (480 + text_size[1]) // 2
    cv2.putText(frame, message, (text_x, text_y), font, 0.9, (0, 0, 255), 2, cv2.LINE_AA)
    
    t_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(frame, t_str, (15, 35), font, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    
    frame = cv2.resize(frame, (850, 650))
    ret, buffer = cv2.imencode('.jpg', frame)
    return buffer.tobytes()


def improve_plate_image(plate_img):
    if plate_img is None or plate_img.size == 0:
        return None
    gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.5, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.bilateralFilter(gray, 9, 75, 75)
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2)
    return thresh


def post_process_plate(text):
    text = re.sub(r'[^A-Z0-9]', '', text.upper().strip())
    if len(text) < 5 or len(text) > 10:
        return None
    if text.startswith("UCB"):
        return text if len(text) >= 8 else None
    if sum(c.isalpha() for c in text) < 2 or sum(c.isdigit() for c in text) < 2:
        return None
    return text


def _start_background_thread():
    def loop():
        while True:
            try:
                with app.app_context():
                    # Utilisation des sites dynamiques
                    active_sites = [s.name for s in Site.query.all()]
                    for s in active_sites:
                        maybe_create_export_reminder("gardien", s)
                    for v in check_long_stay_violations(app):
                        plate = v["plate"]
                        key = f"{plate}:{v['site']}"
                        if key in _long_stay_notified:
                            continue
                        _long_stay_notified.add(key)
                        info = get_vehicle_info(app, plate) or {}
                        owner_name = info.get("owner_name", "")
                        owner_phone = info.get("owner_phone", "")
                        owner_email = info.get("owner_email", "")
                        site_name = v["site"] or ""
                        hours = v["hours"]
                        if owner_email:
                            notify_owner_long_stay(
                                plate,
                                owner_name,
                                owner_email,
                                hours,
                                site_name,
                            )
                        wa_msg = build_long_stay_whatsapp_message(
                            owner_name, plate, site_name, hours
                        )
                        create_notification(
                            f"Stationnement prolonge : {plate} ({hours:.0f}h). Contactez le proprietaire.",
                            site=v["site"],
                            category="long_stay",
                            plate_number=plate,
                            contact_phone=owner_phone or None,
                            whatsapp_message=wa_msg,
                        )
            except Exception:
                pass
            time.sleep(60)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


_start_background_thread()


@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    site = session.get('site') if session.get('role') != 'admin' else None
    if session.get('role') == 'admin':
        capacity = sum(cfg.get('capacity', 0) for cfg in config.SITE_CONFIG.values())
    else:
        capacity = config.get_site_capacity(site)
    kpi = get_dashboard_kpis(session.get('role'), site, capacity)
    return render_template('dashboard.html', kpi=kpi)


@app.route('/live')
def live():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    site = session.get('site')
    if session.get('role') == 'admin':
        site = request.args.get('site') or (config.UCB_SITES[0] if config.UCB_SITES else None)
    policy = config.get_site_policy(site)
    return render_template('live_detection.html', site=site, policy=policy)


@app.route('/admin/videos')
def admin_videos():
    if 'user_id' not in session or session.get('role') != 'admin':
        flash('Acces reserve a l\'administrateur.', 'danger')
        return redirect(url_for('index'))
    
    sites = []
    # Charger les sites dynamiquement
    try:
        db_sites = Site.query.order_by(Site.name).all()
        for s in db_sites:
            sites.append({"name": s.name, "capacity": s.capacity})
    except Exception:
        for name in config.UCB_SITES:
            cfg = config.SITE_CONFIG.get(name, {})
            sites.append({"name": name, "capacity": cfg.get("capacity", 0)})

    focus_site = request.args.get("site")
    if focus_site and focus_site not in [s["name"] for s in sites]:
        focus_site = None
    return render_template("admin_videos.html", sites=sites, focus_site=focus_site)


@app.route('/api/security/banned-alert')
def api_banned_alert():
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    return jsonify(get_security_alert_state())


@app.route('/api/security/alert')
def api_security_alert():
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    return jsonify(get_security_alert_state())


@app.route('/api/gate-status')
def api_gate_status():
    """Endpoint API retournant l'etat actuel des barrieres pour le site actif."""
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    site = session.get('site')
    if session.get('role') == 'admin':
        site = request.args.get('site')
    state = get_gate_state_for_site(site)
    return jsonify(state)


@app.route('/api/gate-control', methods=['POST'])
def api_gate_control():
    """Endpoint API permettant au gardien de forcer l'ouverture ou la fermeture d'une barriere."""
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    
    site = session.get('site')
    if session.get('role') == 'admin':
        site = request.form.get('site') or request.json.get('site')

    direction = request.form.get('direction', 'entry') or request.json.get('direction', 'entry')
    action = request.form.get('action', 'OPEN') or request.json.get('action', 'OPEN')
    plate = request.form.get('plate') or request.json.get('plate')

    trigger_gate_simulation(site, direction, action, plate)
    return jsonify(status='success', site=site, direction=direction, action=action)


def generate_frames(site: str | None = None, camera_type: str = "entry", guardian_id: int | None = None):
    global frame_count
    site_key = f"{site or '__default__'}_{camera_type}"
    consecutive_failures = 0

    while True:
        try:
            stream = _get_stream(site, camera_type)
            success, frame = stream.read()
            
            if not success:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    placeholder = _get_placeholder_frame(f"PAS DE SIGNAL - {camera_type.upper()} {site or ''}")
                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n\r\n' + placeholder + b'\r\n')
                    time.sleep(1.0)
                else:
                    time.sleep(0.1)
                continue
            
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                placeholder = _get_placeholder_frame(f"ERREUR FLUX - {camera_type.upper()} {site or ''}")
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + placeholder + b'\r\n')
                time.sleep(1.0)
            else:
                time.sleep(0.1)
            continue

        frame_count += 1
        # Appliquer la rotation portrait si la frame est en paysage
        frame = _rotate_to_portrait(frame)
        display_frame = frame.copy()
        current_detections = []

        if frame_count % frame_skip == 0:
            results = model(frame, conf=0.38, verbose=False, imgsz=640)
            banned_set = get_banned_plates(app)

            for result in results[0].boxes:
                x1, y1, x2, y2 = map(int, result.xyxy[0])
                cls_id = int(result.cls[0])
                cls_name = model.names[cls_id]
                label = f"{cls_name} {float(result.conf[0]):.2f}"
                current_detections.append((x1, y1, x2, y2, label))

                # Gestion des types interdits (Camions/Bus)
                if cls_name in config.FORBIDDEN_YOLO_CLASSES:
                    signal_forbidden_type_detected(cls_name)
                    process_forbidden_vehicle(app, cls_name, site, guardian_id)
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 140, 255), 4)
                    cv2.putText(
                        display_frame,
                        f"INTERDIT {cls_name.upper()}",
                        (x1, max(35, y1 - 45)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.75,
                        (0, 140, 255),
                        3,
                    )
                    continue

                # Zone de plaque
                h = y2 - y1
                plate_roi = frame[int(y1 + h * 0.52):y2, x1:x2]

                if plate_roi.size > 0:
                    processed = improve_plate_image(plate_roi)
                    if processed is not None:
                        raw_text = pytesseract.image_to_string(processed, config=config.custom_config).strip()
                        plate = post_process_plate(raw_text)
                        if plate:
                            vinfo = get_vehicle_info(app, plate) or {}
                            
                            # Cas d'une plaque bannie
                            if plate in banned_set or vinfo.get("status") == "banned":
                                signal_banned_plate_detected(
                                    plate,
                                    vinfo.get("owner_phone", ""),
                                    vinfo.get("owner_email", ""),
                                )
                                log_banned_detection_throttled(app, plate, site, guardian_id)
                                cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 0, 255), 4)
                                cv2.putText(
                                    display_frame,
                                    f"INTERDIT {plate}",
                                    (x1, max(35, y1 - 45)),
                                    cv2.FONT_HERSHEY_SIMPLEX,
                                    0.85,
                                    (0, 0, 255),
                                    3,
                                )
                            # Cas d'une plaque non autorisee / inconnue
                            elif not vinfo or vinfo.get("status") not in ("active", "pending"):
                                signal_unknown_plate_detected(plate)
                                cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 165, 255), 4)
                                cv2.putText(
                                    display_frame,
                                    f"INCONNU {plate}",
                                    (x1, max(35, y1 - 45)),
                                    cv2.FONT_HERSHEY_SIMPLEX,
                                    0.8,
                                    (0, 165, 255),
                                    3,
                                )
                            # Cas d'une plaque autorisee -> Logique de double lecture
                            else:
                                now = time.time()
                                if camera_type == "entry":
                                    # Si le vehicule etait en attente de validation de sortie, sa presence sur la
                                    # camera d'entree (exterieure) confirme qu'il est sorti.
                                    if plate in _authorized_exits:
                                        confirm_exit_in_db(app, plate, site, guardian_id)
                                        _authorized_exits.pop(plate, None)
                                        trigger_gate_simulation(site, "exit", "CLOSE")
                                        cv2.putText(display_frame, f"SORTIE CONFIRMEE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                                    else:
                                        # Sinon, c'est une intention d'entree. On l'autorise temporairement si pas present.
                                        present_plates = get_present_plates()
                                        if plate not in present_plates:
                                            if plate not in _authorized_entries:
                                                _authorized_entries[plate] = {"timestamp": now, "guardian_id": guardian_id}
                                                trigger_gate_simulation(site, "entry", "OPEN", plate)
                                            cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 200, 80), 4)
                                            cv2.putText(display_frame, f"PORTAIL OUVERTURE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                                        else:
                                            cv2.putText(display_frame, f"DEJA PRESENT {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 165, 0), 3)

                                elif camera_type == "exit":
                                    # Si le vehicule etait en attente de validation d'entree, sa presence sur la
                                    # camera de sortie (interieure) confirme qu'il est entre.
                                    if plate in _authorized_entries:
                                        confirm_entry_in_db(app, plate, site, guardian_id)
                                        _authorized_entries.pop(plate, None)
                                        trigger_gate_simulation(site, "entry", "CLOSE")
                                        cv2.putText(display_frame, f"ENTREE CONFIRMEE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                                    else:
                                        # Sinon, c'est une intention de sortie. On l'autorise si present.
                                        present_plates = get_present_plates()
                                        if plate in present_plates:
                                            if plate not in _authorized_exits:
                                                _authorized_exits[plate] = {"timestamp": now, "guardian_id": guardian_id}
                                                trigger_gate_simulation(site, "exit", "OPEN", plate)
                                            cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 200, 80), 4)
                                            cv2.putText(display_frame, f"PORTAIL OUVERTURE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                                        else:
                                            cv2.putText(display_frame, f"NON PRESENT {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 165, 0), 3)

        if current_detections:
            _last_detections_by_site[site_key] = current_detections
        for det in _last_detections_by_site.get(site_key, []):
            x1, y1, x2, y2, label = det
            cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(display_frame, label, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)

        # Affichage du titre du flux
        label_flux = f"{site or ''} - {camera_type.upper()}"
        cv2.putText(
            display_frame,
            label_flux[:35],
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
        )

        display_frame = cv2.resize(display_frame, (850, 650))
        ret, buffer = cv2.imencode('.jpg', display_frame)
        frame_bytes = buffer.tobytes()

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


def _fetch_http_snapshot(url: str):
    """Requete HTTP directe vers une IP Webcam pour recuperer une frame JPEG.

    Essaie plusieurs chemins de snapshot (/shot.jpg, /photo.jpg, /capture...) puis,
    en fallback, lit les premiers 200KB du flux MJPEG et tente d'en extraire une frame.
    Retourne la frame en orientation portrait si detectee.
    """
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    paths_to_try = ["/shot.jpg", "/photo.jpg", "/capture", "/photo", "/snapshot.jpg"]
    # Si l'URL se termine par /video, /mjpeg ou /live, les remplacer par /shot.jpg
    current_path = re.sub(r'/(video|mjpeg|live)(\?.*)?$', '/shot.jpg', parsed.path)
    if current_path != parsed.path:
        paths_to_try.insert(0, base + current_path)

    for shot_url in paths_to_try:
        try:
            req = urllib.request.Request(shot_url, headers={"User-Agent": "OpenCV"})
            resp = urllib.request.urlopen(req, timeout=5)
            data = resp.read()
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                print(f"[Snapshot HTTP] OK via {shot_url} ({len(data)} octets)")
                return _rotate_to_portrait(frame)
            print(f"[Snapshot HTTP] {shot_url} retourne {len(data)} octets mais non decode")
        except Exception as e:
            print(f"[Snapshot HTTP] {shot_url} -> {type(e).__name__}: {e}")

    # Fallback : lecture partielle du flux MJPEG (premiers 200KB)
    try:
        print(f"[Snapshot HTTP] Fallback lecture partielle de {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "OpenCV"})
        resp = urllib.request.urlopen(req, timeout=4)
        data = resp.read(200000)
        a = data.find(b"\xff\xd8")
        b = data.find(b"\xff\xd9")
        if a != -1 and b != -1 and b > a:
            frame = cv2.imdecode(np.frombuffer(data[a:b+2], dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                print(f"[Snapshot HTTP] Frame extraite du MJPEG ({len(data)} octets lus)")
                return _rotate_to_portrait(frame)
        print(f"[Snapshot HTTP] Aucun JPEG dans le MJPEG ({len(data)} octets)")
    except Exception as e:
        print(f"[Snapshot HTTP] Erreur fallback: {e}")
    return None


@app.route('/camera_snapshot')
def camera_snapshot():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    site = request.args.get('site')
    camera_type = request.args.get('camera_type', 'entry')

    # Recuperer l'URL de la camera depuis la base de donnees
    url = ""
    if site:
        try:
            s = Site.query.filter_by(name=site).first()
            if s:
                url = (s.camera_url_entry if camera_type == "entry" else s.camera_url_exit) or ""
        except Exception:
            pass
    if not url:
        cfg = config.SITE_CONFIG.get(site or "")
        if cfg:
            url = cfg.get(f"camera_url_{camera_type}", "") or ""

    # Normaliser l'URL pour supporter aussi bien les adresses IP nues que les http:// completes
    url_norm = _normalize_url(url) if url else ""
    print(f"[DEBUG] camera_snapshot site={site} type={camera_type} url='{url_norm}'")
    if url_norm and (url_norm.startswith("http://") or url_norm.startswith("https://")):
        frame = _fetch_http_snapshot(url_norm)
        if frame is not None:
            display_frame = frame.copy()
            label_flux = f"{site or ''} - {camera_type.upper()}"
            cv2.putText(display_frame, label_flux[:35], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            display_frame = cv2.resize(display_frame, (850, 650))
            ret, buffer = cv2.imencode('.jpg', display_frame)
            return Response(buffer.tobytes(), mimetype='image/jpeg')

    try:
        stream = _get_stream(site, camera_type)
        success, frame = stream.read()
        if success and frame is not None:
            display_frame = frame.copy()
            label_flux = f"{site or ''} - {camera_type.upper()}"
            cv2.putText(display_frame, label_flux[:35], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            display_frame = cv2.resize(display_frame, (850, 650))
            ret, buffer = cv2.imencode('.jpg', display_frame)
            return Response(buffer.tobytes(), mimetype='image/jpeg')
    except Exception:
        pass

    placeholder = _get_placeholder_frame(f"PAS DE SIGNAL - {camera_type.upper()} {site or ''}")
    return Response(placeholder, mimetype='image/jpeg')


@app.route('/video_feed')
def video_feed():
    """Endpoint MJPEG utilise par la page /live pour la detection en temps reel.

    - Admin : le site est passe en parametre GET (?site=...) ou prend le premier site disponible
    - Gardien : le site vient du parametre GET ou de sa session (assignation par l'admin)
    """
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    # Le param GET prime sur la session pour les deux roles
    site = request.args.get('site') or session.get('site')
    if not site and session.get('role') == 'admin' and config.UCB_SITES:
        site = config.UCB_SITES[0]
    camera_type = request.args.get('camera_type', 'entry')
    gid = session.get('user_id') if session.get('role') == 'gardien' else None
    return Response(
        generate_frames(site=site, camera_type=camera_type, guardian_id=gid),
        mimetype='multipart/x-mixed-replace; boundary=frame',
    )


if __name__ == '__main__':
    print("Systeme Parking UCB - Authentification activee")
    app.run(host='0.0.0.0', port=5000, debug=False)
