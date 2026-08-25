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
import os
import subprocess
import contextlib
import io
import sys
from datetime import datetime


@contextlib.contextmanager
def _suppress_c_stderr():
    fd = sys.stderr.fileno()
    saved = os.dup(fd)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, fd)
    try:
        yield
    finally:
        os.dup2(saved, fd)
        os.close(saved)
        os.close(devnull)

from access_logging import (
    confirm_entry_in_db,
    confirm_exit_in_db,
    manual_access_in_db,
    get_present_plates,
    check_long_stay_violations,
    process_forbidden_vehicle,
    init_presence_from_db,
)
from admin_users import admin_bp
from auth import auth
from dashboard_stats import get_dashboard_kpis, get_dashboard_kpis_by_site
from db_init import init_app_database
from email_service import build_long_stay_whatsapp_message, notify_owner_long_stay
from logs import logs_bp
from models import db, Vehicle, Site, User
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
    if u.startswith("rtsp://"):
        return u
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

    - Flux HTTP (IP Webcam MJPEG) : parse le MJPEG via urllib en cherchant les marqueurs JPEG
    - Flux RTSP : utilise FFmpeg en subprocess pour decoder le flux
    - La frame la plus recente est stockee dans self.frame et lue par self.read()
    - Le thread d'arriere-plan tente de se reconnecter automatiquement en cas d'erreur
    """
    def __init__(self, url: str, site: str | None = None, camera_type: str = "entry"):
        u = _normalize_url(url)
        self.url = u
        self.site = site
        self.camera_type = camera_type
        self.cap = None          # Capture OpenCV pour les fichiers locaux
        self.frame = None        # Derniere frame decodee
        self.success = False     # True si une frame valide est disponible
        self.running = True      # Le thread doit continuer
        self.is_http = self.url.startswith("http://") or self.url.startswith("https://")
        self.is_rtsp = self.url.startswith("rtsp://")
        self.http_stream = None  # Connexion HTTP pour MJPEG
        self.http_buffer = b""   # Buffer d'accumulation MJPEG
        self.ffmpeg_process = None
        self.lock = threading.Lock()
        self.consecutive_errors = 0
        self.working_mjpeg_url = None  # URL MJPEG qui a reussi
        self.last_read = time.time()

        print(f"[CameraStream] Initialisation avec URL: {self.url} (http={self.is_http}, rtsp={self.is_rtsp})")
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()

    def _read_http_frame(self):
        """Lit et decode une frame JPEG depuis un flux MJPEG HTTP (IP Webcam).
        Essaie plusieurs endpoints MJPEG courants (/video, /mjpeg, /videofeed, /live) et memorise celui qui fonctionne."""
        if self.working_mjpeg_url is None:
            parsed = urllib.parse.urlparse(self.url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            current_path = parsed.path.rstrip("/")
            
            candidates = []
            if current_path and current_path != "/":
                candidates.append(self.url)
            
            mjpeg_paths = ["/video", "/mjpeg", "/videofeed", "/live", "/stream", "/mjpg"]
            for p in mjpeg_paths:
                candidates.append(base + p)
            
            print(f"[CameraStream] Test endpoints MJPEG pour {self.url}...")
            for test_url in candidates:
                try:
                    req = urllib.request.Request(test_url, headers={"User-Agent": "OpenCV"})
                    test_stream = urllib.request.urlopen(req, timeout=5)
                    test_chunk = test_stream.read(16384)
                    test_stream.close()
                    if test_chunk and (b"\xff\xd8" in test_chunk or b"\xff\xd9" in test_chunk):
                        self.working_mjpeg_url = test_url
                        print(f"[CameraStream] Endpoint MJPEG valide trouve: {test_url}")
                        break
                except Exception:
                    continue
            
            if self.working_mjpeg_url is None:
                self.working_mjpeg_url = self.url
                print(f"[CameraStream] Aucun endpoint MJPEG detecte, utilisation de l'URL originale: {self.url}")
        
        try:
            if self.http_stream is None:
                print(f"[CameraStream] Connexion a {self.working_mjpeg_url}...")
                req = urllib.request.Request(self.working_mjpeg_url, headers={"User-Agent": "OpenCV"})
                self.http_stream = urllib.request.urlopen(req, timeout=5)
                print(f"[CameraStream] Connecte a {self.working_mjpeg_url}")
                self.http_buffer = b""
            while self.running:
                chunk = self.http_stream.read(16384)
                if not chunk:
                    raise ConnectionError("Fin du flux HTTP")
                self.http_buffer += chunk
                a = self.http_buffer.find(b"\xff\xd8")
                while a != -1:
                    b = self.http_buffer.find(b"\xff\xd9", a + 2)
                    if b != -1 and b > a:
                        jpg = self.http_buffer[a:b+2]
                        with _suppress_c_stderr():
                            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            self.http_buffer = self.http_buffer[b+2:]
                            self.consecutive_errors = 0
                            frame = _rotate_to_portrait(frame)
                            return True, frame
                        a = self.http_buffer.find(b"\xff\xd8", a + 1)
                    else:
                        break
                if len(self.http_buffer) > 5_000_000:
                    self.http_buffer = b""
            return False, None
        except Exception as e:
            print(f"[CameraStream] Erreur HTTP ({type(e).__name__}): {e}")
            if self.http_stream:
                try: self.http_stream.close()
                except: pass
            self.http_stream = None
            self.http_buffer = b""
            self.consecutive_errors += 1
            backoff = min(2.0, 0.3 * self.consecutive_errors)
            time.sleep(backoff)
            return False, None

    def _update(self):
        """Boucle principale du thread. Alterne entre lecture HTTP MJPEG et RTSP via FFmpeg."""
        while self.running:
            if self.is_rtsp:
                success, frame = self._read_rtsp_frame()
                with self.lock:
                    if success:
                        self.frame = frame
                        self.success = True
                    else:
                        self.success = False
                continue
                
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
                    self.consecutive_errors += 1
                    time.sleep(2.0)
                    continue

            success, frame = self.cap.read()

            if not success:
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
                    self.consecutive_errors += 1
                    time.sleep(2.0)

    def _read_rtsp_frame(self):
        """Lit une trame depuis un flux RTSP via FFmpeg."""
        try:
            if self.ffmpeg_process is None:
                print(f"[CameraStream] Lancement FFmpeg pour RTSP: {self.url}")
                startupinfo = None
                if hasattr(subprocess, 'STARTUPINFO'):
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                self.ffmpeg_process = subprocess.Popen(
                    [
                        "ffmpeg",
                        "-rtsp_transport", "tcp",
                        "-i", self.url,
                        "-f", "image2pipe",
                        "-vcodec", "mjpeg",
                        "-qscale:v", "2",
                        "-an",
                        "-",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=10 ** 8,
                    startupinfo=startupinfo,
                )
                print(f"[CameraStream] FFmpeg PID {self.ffmpeg_process.pid} pour {self.url}")
                self.http_buffer = b""

            buf = self.http_buffer
            while self.running:
                chunk = self.ffmpeg_process.stdout.read(16384)
                if not chunk:
                    raise ConnectionError("Fin du pipe FFmpeg")
                buf += chunk
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9")
                if a != -1 and b != -1 and b > a:
                    jpg = buf[a:b + 2]
                    self.http_buffer = buf[b + 2:]
                    with _suppress_c_stderr():
                        frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if frame is not None:
                        self.consecutive_errors = 0
                        frame = _rotate_to_portrait(frame)
                        return True, frame
                    continue
                if len(buf) > 5_000_000:
                    buf = b""
            self.http_buffer = buf
            return False, None
        except FileNotFoundError:
            print("[CameraStream] FFmpeg introuvable.")
            self.consecutive_errors += 1
            time.sleep(10)
            return False, None
        except Exception as e:
            print(f"[CameraStream] Erreur RTSP: {e}")
            self._cleanup_ffmpeg()
            self.http_buffer = b""
            self.consecutive_errors += 1
            backoff = min(2.0, 0.3 * self.consecutive_errors)
            time.sleep(backoff)
            return False, None

    def _cleanup_ffmpeg(self):
        if self.ffmpeg_process:
            try: self.ffmpeg_process.kill()
            except: pass
            try: self.ffmpeg_process.wait(1)
            except: pass
            self.ffmpeg_process = None
            self.http_buffer = b""

    def read(self):
        with self.lock:
            self.last_read = time.time()
            if self.success and self.frame is not None:
                return True, self.frame.copy()
            return False, None

    def release(self):
        self.running = False
        if self.http_stream:
            try: self.http_stream.close()
            except: pass
            self.http_stream = None
        if self.cap:
            try: self.cap.release()
            except: pass
        self._cleanup_ffmpeg()


model = YOLO('yolov8n.pt')

PLATE_MODEL_PATH = os.path.join(os.path.dirname(__file__), 'models', 'license_plate.pt')
plate_model = None
if os.path.exists(PLATE_MODEL_PATH):
    try:
        plate_model = YOLO(PLATE_MODEL_PATH)
        print(f"[ALPR] Modele plaque charge : {PLATE_MODEL_PATH} (classes: {plate_model.names})")
    except Exception as e:
        print(f"[ALPR] Erreur chargement modele plaque: {e}")

frame_skip = 5
frame_count = 0
_last_detections_by_site: dict[str, list] = {}
_caps: dict[str, CameraStream] = {}
_caps_lock = threading.Lock()
_CAPS_CLEANUP_TIMEOUT = 45.0  # secondes sans lecture avant suppression
_long_stay_notified: set[str] = set()


def _cleanup_old_streams():
    while True:
        time.sleep(30)
        now = time.time()
        with _caps_lock:
            for key in list(_caps.keys()):
                stream = _caps.get(key)
                if stream is None:
                    continue
                if now - getattr(stream, "last_read", 0) > _CAPS_CLEANUP_TIMEOUT:
                    print(f"[CLEANUP] Release ancien stream {key} (non utilise depuis {now - stream.last_read:.0f}s)")
                    stream.release()
                    _caps.pop(key, None)


_cleanup_thread = threading.Thread(target=_cleanup_old_streams, daemon=True)
_cleanup_thread.start()

# State machine pour la double-lecture
_authorized_entries: dict[str, dict] = {}  # plate -> {timestamp, guardian_id}
_authorized_exits: dict[str, dict] = {}    # plate -> {timestamp, guardian_id}
_ENTRY_CONFIRMATION_TIMEOUT_SEC = 10.0      # backup : confirme l'entree si la camera de sortie ne la valide pas

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

        # Transitions automatiques d'etat de la barriere
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


def trigger_gate(site_name: str | None, direction: str, action: str, plate: str | None = None):
    """Declenche la barriere (etat logiciel + envoi commande physique IP)."""
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
    
    if site:
        try:
            with app.app_context():
                s = Site.query.filter_by(name=site).first()
                if s:
                    url = (s.camera_url_entry if camera_type == "entry" else s.camera_url_exit) or ""
        except Exception:
            pass
            
    if not url:
        try:
            cfg = config.SITE_CONFIG.get(site or "")
            if cfg:
                url = cfg.get(f"camera_url_{camera_type}", "") or ""
        except Exception:
            pass
            
    if not url:
        url = ""

    url_norm = _normalize_url(url)
    print(f"[STREAM] _get_stream site={site} camera={camera_type} url_raw='{url}' url_norm='{url_norm}'")

    with _caps_lock:
        stream = _caps.get(key)
        if stream is None or stream.url != url_norm or not stream.running:
            if stream:
                print(f"[_get_stream] Release ancien stream (url stream={stream.url}, url request={url_norm})")
                stream.release()
            other_type = "exit" if camera_type == "entry" else "entry"
            other_key = f"{site or '__default__'}_{other_type}"
            other_stream = _caps.get(other_key)
            if other_stream is not None and other_stream.url == url_norm and other_stream.running:
                print(f"[STREAM] Partage du stream {other_key} -> {key}")
                _caps[key] = other_stream
                return other_stream
            print(f"[_get_stream] Nouveau CameraStream pour {key} -> {url}")
            stream = CameraStream(url, site=site, camera_type=camera_type)
            _caps[key] = stream
        return stream


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
    ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buffer.tobytes()


def _resolve_site():
    if session.get('role') == 'admin':
        return request.args.get('site') or session.get('site') or (config.UCB_SITES[0] if config.UCB_SITES else None)
    
    site = session.get('site')
    user_id = session.get('user_id')
    
    if not site and user_id:
        try:
            user = User.query.get(user_id)
            if user and user.site:
                site = user.site
                session['site'] = site
                session.modified = True
                print(f"[SITE] Gardien {user_id} ({user.username}) : site session mis a jour = {site}")
        except Exception as e:
            print(f"[SITE] Erreur DB pour gardien {user_id} : {e}")
    
    if site:
        try:
            s = Site.query.filter_by(name=site).first()
            if not s:
                print(f"[SITE] Site '{site}' introuvable en base pour gardien {user_id}")
                site = None
        except Exception as e:
            print(f"[SITE] Erreur verification site pour gardien {user_id} : {e}")
    
    if not site:
        try:
            sites = config.UCB_SITES
            if sites:
                site = sites[0]
                session['site'] = site
                session.modified = True
                print(f"[SITE] Gardien {user_id} : fallback vers premier site = {site}")
            else:
                print(f"[SITE] Aucun site disponible pour gardien {user_id}")
        except Exception as e:
            print(f"[SITE] Erreur fallback site pour gardien {user_id} : {e}")
    
    return site


# Formats RDC depuis le décret 08/15 : 4 chiffres, 2 lettres, puis le code
# provincial à 2 chiffres.  Les 3 caractères CGO à gauche de la plaque ne font
# pas partie de l'immatriculation et sont volontairement ignorés par l'OCR.
_DRC_PLATE_RE = re.compile(r"^\d{4}[A-Z]{2}(?:0[1-9]|1\d|2[0-6])$")
_DRC_LEGACY_PLATE_RE = re.compile(r"^[A-Z]{2}\d{4}[A-Z]{2}$")
_UCB_PLATE_RE = re.compile(r"^UCB(?:\d{4,8}[A-Z]{0,4}|[A-Z]{2,6}\d{4,8})$")
_OCR_WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_DIGIT_CONFUSIONS = str.maketrans({
    "O": "0", "Q": "0", "D": "0", "I": "1", "L": "1",
    "Z": "2", "S": "5", "B": "8", "G": "6",
})
_LETTER_CONFUSIONS = str.maketrans({
    "0": "O", "1": "I", "5": "S", "8": "B", "2": "Z", "6": "G",
})


def _deskew_plate(gray):
    """Corrige une inclinaison légère sans déformer une plaque déjà droite."""
    _, foreground = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    points = np.column_stack(np.where(foreground > 0)).astype(np.float32)
    if len(points) < 20:
        return gray

    angle = cv2.minAreaRect(points)[-1]
    # OpenCV renvoie suivant les versions un angle dans [-90, 0[ ou ]0, 90].
    if angle < -45:
        angle += 90
    elif angle > 45:
        angle -= 90
    if not 1.0 <= abs(angle) <= 12.0:
        return gray

    height, width = gray.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (width, height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )


def improve_plate_image(plate_img, return_variants=False):
    """Prétraite une plaque pour Tesseract.

    Le redimensionnement n'est déclenché que pour les petits crops : le texte
    est amené vers ~28 px de haut, sans agrandir inutilement les grandes
    plaques (ce qui avait dégradé les essais 3x/4x). Avec ``return_variants``,
    retourne plusieurs binarisations et une version grayscale sans seuillage
    pour l'ensemble OCR.
    """
    if plate_img is None or plate_img.size == 0:
        return None

    if len(plate_img.shape) == 2:
        gray = plate_img.copy()
    elif plate_img.shape[2] == 4:
        gray = cv2.cvtColor(plate_img, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)

    height, width = gray.shape[:2]
    estimated_text_height = max(1.0, height * 0.55)
    scale = max(1.0, min(4.0, 28.0 / estimated_text_height))
    if scale > 1.01:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    gray = _deskew_plate(gray)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)
    gray = cv2.bilateralFilter(gray, 7, 60, 60)

    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 21, 5,
    )
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    median = cv2.medianBlur(gray, 3)
    _, fixed100 = cv2.threshold(median, 100, 255, cv2.THRESH_BINARY)
    _, fixed120 = cv2.threshold(median, 120, 255, cv2.THRESH_BINARY)

    clahe_strong = cv2.createCLAHE(clipLimit=3.5, tileGridSize=(8, 8))
    gray_strong = clahe_strong.apply(gray)
    gray_strong = cv2.bilateralFilter(gray_strong, 9, 75, 75)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    variants = []
    for name, binary in (
        ("adaptive", adaptive),
        ("otsu", otsu),
        ("fixed100", fixed100),
        ("fixed120", fixed120),
    ):
        cleaned = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=1)
        variants.append((name, cleaned))

    variants.append(("grayscale_strong", gray_strong))

    return variants if return_variants else variants[0][1]


def _repair_drc_candidate(candidate):
    """Applique les confusions OCR uniquement aux positions connues du format RDC."""
    if len(candidate) != 8:
        return None
    repaired = candidate[:4].translate(_DIGIT_CONFUSIONS)
    repaired += candidate[4:6].translate(_LETTER_CONFUSIONS)
    repaired += candidate[6:].translate(_DIGIT_CONFUSIONS)
    return repaired if _DRC_PLATE_RE.fullmatch(repaired) else None


def post_process_plate(text):
    """Normalise, corrige et valide un texte OCR de plaque RDC/UCB."""
    normalized = re.sub(r"[^A-Z0-9]", "", (text or "").upper())
    if not normalized:
        return None

    # Plaques institutionnelles : UCB0001XXXX ou UCB-BUG-0001 (tirets retirés).
    if _UCB_PLATE_RE.fullmatch(normalized):
        return normalized

    # Le texte peut contenir CGO, la numérotation laser ou un caractère parasite
    # avant/après la série. Examiner donc toutes les fenêtres de 8 caractères.
    tokens = re.findall(r"[A-Z0-9]{8,}", normalized)
    for token in tokens:
        for start in range(len(token) - 7):
            raw_candidate = token[start:start + 8]
            repaired = _repair_drc_candidate(raw_candidate)
            if repaired:
                return repaired

    return None


def read_plate_text(plate_img, psm_modes=(7, 6, 8)):
    """OCR multi-PSM/binarisation avec vote; retourne la plaque validée ou None."""
    variants = improve_plate_image(plate_img, return_variants=True)
    if not variants:
        return None

    candidates = []
    for variant_index, (_, processed) in enumerate(variants):
        for psm in psm_modes:
            ocr_config = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={_OCR_WHITELIST}"
            try:
                raw = pytesseract.image_to_string(processed, config=ocr_config).strip()
            except (pytesseract.TesseractError, OSError):
                continue
            plate = post_process_plate(raw)
            if plate:
                # PSM 7 convient normalement à une plaque sur une ligne. Le
                # score ne tranche qu'en cas d'égalité de votes.
                quality = (12 if psm == 7 else 8 if psm == 6 else 6) - variant_index
                if _DRC_PLATE_RE.fullmatch(plate):
                    quality += 4
                candidates.append((plate, quality))

    if not candidates:
        return None
    votes = {}
    quality_sums = {}
    for plate, quality in candidates:
        votes[plate] = votes.get(plate, 0) + 1
        quality_sums[plate] = quality_sums.get(plate, 0) + quality
    avg_qualities = {plate: quality_sums[plate] / votes[plate] for plate in votes}
    return max(votes, key=lambda plate: (avg_qualities[plate], votes[plate], plate))


def _format_wa_phone(phone: str) -> str:
    p = re.sub(r'\D', '', phone or '')
    if p.startswith('0'):
        p = '243' + p[1:]
    return p


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
    if not site and session.get('role') != 'admin' and session.get('user_id'):
        user = User.query.get(session['user_id'])
        if user and user.site:
            session['site'] = user.site
            session.modified = True
            site = user.site
    if session.get('role') == 'admin':
        capacity = sum(cfg.get('capacity', 0) for cfg in config.SITE_CONFIG.values())
    else:
        capacity = config.get_site_capacity(site)
    kpi = get_dashboard_kpis(session.get('role'), site, capacity)
    kpi_by_site = get_dashboard_kpis_by_site(session.get('role')) if session.get('role') == 'admin' else []
    return render_template('dashboard.html', kpi=kpi, kpi_by_site=kpi_by_site)


@app.route('/live')
def live():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    site = _resolve_site()
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


@app.route('/api/gate-status')
def api_gate_status():
    """Endpoint API retournant l'etat actuel des barrieres pour le site actif."""
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    site = _resolve_site()
    state = get_gate_state_for_site(site)
    return jsonify(state)


@app.route('/api/gate-control', methods=['POST'])
def api_gate_control():
    """Endpoint API permettant au gardien de forcer l'ouverture ou la fermeture d'une barriere."""
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    
    site = _resolve_site()
    if session.get('role') == 'admin':
        site = request.form.get('site') or request.json.get('site') or site

    direction = request.form.get('direction', 'entry') or request.json.get('direction', 'entry')
    action = request.form.get('action', 'OPEN') or request.json.get('action', 'OPEN')
    plate = request.form.get('plate') or request.json.get('plate')

    trigger_gate(site, direction, action, plate)
    return jsonify(status='success', site=site, direction=direction, action=action)


@app.route('/api/manual-access', methods=['POST'])
def api_manual_access():
    """Endpoint API permettant au gardien de saisir manuellement une plaque
    quand la reconnaissance automatique echoue ou n'est pas disponible.

    Cree un AccessLog avec status='manual' et, pour les vehicules autorises,
    declenche l'ouverture de la barriere. Le statut du registre (authorise /
    inconnu / banni) est renvoye pour affichage, comme une detection automatique.
    """
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401

    if session.get('role') != 'gardien':
        return jsonify(error='acces reserve aux gardiens'), 403

    data = request.get_json(silent=True) or {}
    if not data:
        data = request.form.to_dict()

    plate = (data.get('plate') or '').upper().strip()
    direction = (data.get('direction') or data.get('action') or 'entry').lower()
    if direction in ('exit', 'sortie', 'out'):
        direction = 'exit'
    elif direction in ('entry', 'entree', 'in'):
        direction = 'entry'

    if not plate:
        return jsonify(error='plaque requise'), 400

    site = _resolve_site()
    guardian_id = session.get('user_id')

    info = manual_access_in_db(app, plate, direction, site, guardian_id)
    registry_status = info.get('registry_status', 'unknown')

    gate_opened = False
    if registry_status in ('authorized', 'pending'):
        trigger_gate(site, direction, 'OPEN', plate)
        gate_opened = True

    return jsonify(
        status='success',
        plate=plate,
        direction=direction,
        site=site,
        registry_status=registry_status,
        gate_opened=gate_opened,
        vehicle={
            'owner_name': info.get('owner_name'),
            'owner_phone': info.get('owner_phone'),
            'owner_email': info.get('owner_email'),
            'site_authorized': info.get('site_authorized'),
        } if registry_status != 'unknown' else None,
    )


@app.route('/api/contact-owner', methods=['POST'])
def api_contact_owner():
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401

    data = request.get_json(silent=True) or {}
    plate = (data.get('plate') or '').upper().strip()
    method = (data.get('method') or 'call').lower()

    if not plate:
        return jsonify(error='plaque requise'), 400
    if method not in ('call', 'whatsapp'):
        return jsonify(error='methode invalide'), 400

    vinfo = get_vehicle_info(app, plate)
    if not vinfo:
        return jsonify(error='vehicule introuvable'), 404

    phone = vinfo.get('owner_phone', '') or ''
    if not phone:
        return jsonify(error='telephone non renseigne'), 404

    tel_link = f"tel:{phone}"
    wa_phone = _format_wa_phone(phone)
    wa_link = f"https://wa.me/{wa_phone}?text={urllib.parse.quote('Alerte parking UCB — vehicule ' + plate)}"
    # Une redirection tel:/wa.me depuis fetch() est bloquée ou suivie en arrière-plan
    # par les navigateurs. Le client reçoit donc les deux liens et ouvre celui demandé.
    return jsonify(tel_link=tel_link, wa_link=wa_link)


@app.route('/api/security/alert')
def api_security_alert():
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    state = get_security_alert_state()
    if session.get('role') != 'admin':
        state = dict(state)
        state.pop('owner_phone', None)
    return jsonify(state)


def generate_frames(site: str | None = None, camera_type: str = "entry", guardian_id: int | None = None):
    global frame_count
    site_key = f"{site or '__default__'}_{camera_type}"
    consecutive_failures = 0
    print(f"[STREAM] Debut generate_frames site={site} camera={camera_type} guardian={guardian_id}")
    
    stream = _get_stream(site, camera_type)
    
    while True:
        try:
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
        all_plate_boxes: list[tuple[int, int, int, int, float]] = []

        if frame_count % frame_skip == 0:
            # Backup timeout : si la camera de sortie n'a pas confirme l'entree
            # dans les delais (flux unique ou double-lecture echouee), on confirme
            # automatiquement pour ne pas perdre l'access log.
            if camera_type == "entry":
                _now = time.time()
                for _p in list(_authorized_entries.keys()):
                    _ei = _authorized_entries.get(_p) or {}
                    if _now - _ei.get("timestamp", _now) > _ENTRY_CONFIRMATION_TIMEOUT_SEC:
                        _gid = _ei.get("guardian_id")
                        confirm_entry_in_db(app, _p, site, _gid)
                        _authorized_entries.pop(_p, None)
                        trigger_gate(site, "entry", "CLOSE")
                        print(f"[ACCES] Entree auto-confirmee (timeout {_ENTRY_CONFIRMATION_TIMEOUT_SEC}s) plaque {_p} site={site}")

            results = model(frame, conf=0.38, verbose=False, imgsz=480)
            banned_set = get_banned_plates(app)

            plate_detections: dict[tuple, dict] = {}
            if plate_model is not None:
                p_results = plate_model(frame, conf=0.25, verbose=False, imgsz=416)
                for pbox in p_results[0].boxes:
                    if int(pbox.cls[0]) != 0:
                        continue
                    px1, py1, px2, py2 = map(int, pbox.xyxy[0])
                    pconf = float(pbox.conf[0])
                    all_plate_boxes.append((px1, py1, px2, py2, pconf))
                    pcx, pcy = (px1 + px2) // 2, (py1 + py2) // 2
                    matched = False
                    for vbox in results[0].boxes:
                        vx1, vy1, vx2, vy2 = map(int, vbox.xyxy[0])
                        if vx1 <= pcx <= vx2 and vy1 <= pcy <= vy2:
                            k = (vx1, vy1, vx2, vy2)
                            plate_img = frame[py1:py2, px1:px2]
                            if plate_img.size > 0:
                                ptext = read_plate_text(plate_img)
                                if ptext:
                                    plate_detections[k] = {"bbox": (px1, py1, px2, py2), "text": ptext}
                            matched = True
                            break

            for result in results[0].boxes:
                x1, y1, x2, y2 = map(int, result.xyxy[0])
                cls_id = int(result.cls[0])
                cls_name = model.names[cls_id]
                label = f"{cls_name} {float(result.conf[0]):.2f}"
                current_detections.append((x1, y1, x2, y2, label))

                if cls_name in config.FORBIDDEN_YOLO_CLASSES:
                    signal_forbidden_type_detected(cls_name)
                    process_forbidden_vehicle(app, cls_name, site, guardian_id)
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 140, 255), 4)
                    cv2.putText(display_frame, f"INTERDIT {cls_name.upper()}", (x1, max(35, y1 - 45)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 140, 255), 3)
                    continue

                h = y2 - y1
                roi_y1 = int(y1 + h * 0.45)
                roi_y2 = y2
                cv2.rectangle(display_frame, (x1, roi_y1), (x2, roi_y2), (180, 180, 180), 1)
                cv2.putText(display_frame, "PLATE ROI", (x1 + 2, roi_y1 - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (180, 180, 180), 1)

                plate = None
                plate_info = plate_detections.get((x1, y1, x2, y2))
                if plate_info is not None:
                    plate = plate_info["text"]
                    ppx1, ppy1, ppx2, ppy2 = plate_info["bbox"]
                    cv2.rectangle(display_frame, (ppx1, ppy1), (ppx2, ppy2), (0, 255, 255), 3)
                    cv2.putText(display_frame, plate, (ppx1, ppy1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                else:
                    h = y2 - y1
                    plate_roi = frame[int(y1 + h * 0.52):y2, x1:x2]
                    if plate_roi.size > 0:
                        plate = read_plate_text(plate_roi)
                    if not plate:
                        for ratio in (0.45, 0.55, 0.65):
                            roi = frame[int(y1 + h * ratio):y2, x1:x2]
                            if roi.size > 0 and roi.shape[0] > 5:
                                plate = read_plate_text(roi)
                                if plate:
                                    break

                if not plate:
                    continue

                vinfo = get_vehicle_info(app, plate) or {}

                if plate in banned_set or vinfo.get("status") == "banned":
                    signal_banned_plate_detected(plate, vinfo.get("owner_phone", ""), vinfo.get("owner_email", ""))
                    log_banned_detection_throttled(app, plate, site, guardian_id)
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 0, 255), 4)
                    cv2.putText(display_frame, f"INTERDIT {plate}", (x1, max(35, y1 - 45)), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 0, 255), 3)
                elif not vinfo or vinfo.get("status") not in ("active", "pending"):
                    signal_unknown_plate_detected(plate)
                    cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 165, 255), 4)
                    cv2.putText(display_frame, f"ENREGISTRER {plate}", (x1, max(35, y1 - 45)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 3)
                else:
                    now = time.time()
                    if camera_type == "entry":
                        if plate in _authorized_exits:
                            confirm_exit_in_db(app, plate, site, guardian_id)
                            _authorized_exits.pop(plate, None)
                            trigger_gate(site, "exit", "CLOSE")
                            cv2.putText(display_frame, f"SORTIE CONFIRMEE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                        else:
                            present_plates = get_present_plates()
                            if plate not in present_plates:
                                if plate not in _authorized_entries:
                                    _authorized_entries[plate] = {"timestamp": now, "guardian_id": guardian_id}
                                    trigger_gate(site, "entry", "OPEN", plate)
                                cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 200, 80), 4)
                                cv2.putText(display_frame, f"PORTAIL OUVERTURE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                            else:
                                cv2.putText(display_frame, f"DEJA PRESENT {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 165, 0), 3)
                    elif camera_type == "exit":
                        if plate in _authorized_entries:
                            confirm_entry_in_db(app, plate, site, guardian_id)
                            _authorized_entries.pop(plate, None)
                            trigger_gate(site, "entry", "CLOSE")
                            cv2.putText(display_frame, f"ENTREE CONFIRMEE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                        else:
                            present_plates = get_present_plates()
                            if plate in present_plates:
                                if plate not in _authorized_exits:
                                    _authorized_exits[plate] = {"timestamp": now, "guardian_id": guardian_id}
                                    trigger_gate(site, "exit", "OPEN", plate)
                                cv2.rectangle(display_frame, (x1, y1), (x2, y2), (0, 200, 80), 4)
                                cv2.putText(display_frame, f"PORTAIL OUVERTURE {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 3)
                            else:
                                cv2.putText(display_frame, f"NON PRESENT {plate}", (x1, y1 - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 165, 0), 3)

        if current_detections:
            _last_detections_by_site[site_key] = current_detections
        else:
            _last_detections_by_site.pop(site_key, None)

        for px1, py1, px2, py2, pconf in all_plate_boxes:
            cv2.rectangle(display_frame, (px1, py1), (px2, py2), (255, 255, 0), 2)
            cv2.putText(display_frame, f"PLATE {pconf:.2f}", (px1, py1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)

        # Dessin des bboxes vertes pour les vehicules
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
        ret, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        frame_bytes = buffer.tobytes()

        _store_latest_frame(site_key, frame, camera_type)

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')


_latest_frames: dict[str, tuple[np.ndarray, str]] = {}
_latest_frames_lock = threading.Lock()


def _store_latest_frame(site_key: str, frame: np.ndarray, camera_type: str):
    with _latest_frames_lock:
        _latest_frames[site_key] = (frame.copy(), camera_type)


def get_latest_frame(site: str | None, camera_type: str = "entry") -> tuple[np.ndarray | None, str]:
    site_key = f"{site or '__default__'}_{camera_type}"
    with _latest_frames_lock:
        entry = _latest_frames.get(site_key)
        if entry:
            return entry
    return None, camera_type


@app.route('/api/capture_ocr', methods=['POST'])
def api_capture_ocr():
    if 'user_id' not in session:
        return jsonify(error='unauthorized'), 401
    site = _resolve_site()
    camera_type = request.json.get('camera_type', 'entry') if request.is_json else request.form.get('camera_type', 'entry')
    frame, _ = get_latest_frame(site, camera_type)
    if frame is None:
        return jsonify(error='Aucune frame disponible. Le flux n\'est pas encore actif.'), 400

    results = model(frame, conf=0.38, verbose=False, imgsz=480)
    plate = None
    plate_bbox = None

    if plate_model is not None:
        p_results = plate_model(frame, conf=0.25, verbose=False, imgsz=416)
        for pbox in p_results[0].boxes:
            if int(pbox.cls[0]) != 0:
                continue
            px1, py1, px2, py2 = map(int, pbox.xyxy[0])
            pcx, pcy = (px1 + px2) // 2, (py1 + py2) // 2
            for vbox in results[0].boxes:
                vx1, vy1, vx2, vy2 = map(int, vbox.xyxy[0])
                if vx1 <= pcx <= vx2 and vy1 <= pcy <= vy2:
                    plate_img = frame[py1:py2, px1:px2]
                    if plate_img.size > 0:
                        ptext = read_plate_text(plate_img)
                        if ptext:
                            plate = ptext
                            plate_bbox = [px1, py1, px2, py2]
                    break

    if not plate:
        h, w = frame.shape[:2]
        candidates = []
        for vbox in results[0].boxes:
            vx1, vy1, vx2, vy2 = map(int, vbox.xyxy[0])
            vh = vy2 - vy1
            for ratio in (0.50, 0.60, 0.70):
                y_start = int(vy1 + vh * ratio)
                roi = frame[y_start:vy2, vx1:vx2]
                if roi.size > 0 and roi.shape[0] > 5 and roi.shape[1] > 10:
                    candidates.append(roi)
        if not candidates:
            candidates = [frame[int(h*0.5):h, int(w*0.1):int(w*0.9)]]
        for roi in candidates:
            ptext = read_plate_text(roi)
            if ptext:
                plate = ptext
                break

    if not plate:
        return jsonify(plate=None, message='Aucune plaque detectee sur cette capture.')

    vinfo = get_vehicle_info(app, plate) or {}
    status = vinfo.get("status", "unknown")
    if status not in ("active", "pending"):
        status = "unknown"

    return jsonify({
        "plate": plate,
        "status": status,
        "bbox": plate_bbox,
        "message": f"Plaque detectee : {plate} — Statut : {status}",
    })


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
            with _suppress_c_stderr():
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
            with _suppress_c_stderr():
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
    site = _resolve_site()
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
    if url_norm and (url_norm.startswith("http://") or url_norm.startswith("https://")):
        frame = _fetch_http_snapshot(url_norm)
        if frame is not None:
            display_frame = frame.copy()
            label_flux = f"{site or ''} - {camera_type.upper()}"
            cv2.putText(display_frame, label_flux[:35], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            display_frame = cv2.resize(display_frame, (850, 650))
            ret, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return Response(buffer.tobytes(), mimetype='image/jpeg')

    try:
        stream = _get_stream(site, camera_type)
        success, frame = stream.read()
        if success and frame is not None:
            display_frame = frame.copy()
            label_flux = f"{site or ''} - {camera_type.upper()}"
            cv2.putText(display_frame, label_flux[:35], (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            display_frame = cv2.resize(display_frame, (850, 650))
            ret, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return Response(buffer.tobytes(), mimetype='image/jpeg')
    except Exception:
        pass

    placeholder = _get_placeholder_frame(f"PAS DE SIGNAL - {camera_type.upper()} {site or ''}")
    return Response(placeholder, mimetype='image/jpeg')


@app.route('/video_feed')
def video_feed():
    if 'user_id' not in session:
        return redirect(url_for('auth.login'))
    
    site = _resolve_site()
    camera_type = request.args.get('camera_type', 'entry')
    gid = session.get('user_id') if session.get('role') == 'gardien' else None
    
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
    url = _normalize_url(url) if url else ""
    
    print(f"[VIDEO_FEED] role={session.get('role')} site={site} camera={camera_type} gid={gid}")
    return Response(
        generate_frames(site=site, camera_type=camera_type, guardian_id=gid),
        mimetype='multipart/x-mixed-replace; boundary=frame',
    )


if __name__ == '__main__':
    print("Systeme Parking UCB - Authentification activee")
    app.run(host='0.0.0.0', port=5000, debug=False)
