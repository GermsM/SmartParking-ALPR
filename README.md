<div align="center">
  <img src="https://img.shields.io/badge/version-2.0.0-blue.svg" alt="Version">
  <img src="https://img.shields.io/badge/python-3.10%2B-brightgreen.svg" alt="Python">
  <img src="https://img.shields.io/badge/flask-3.0-lightgrey.svg" alt="Flask">
  <img src="https://img.shields.io/badge/YOLOv8-ultralytics-orange.svg" alt="YOLOv8">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</div>

<br>

<div align="center">
  <h1>SmartParking ALPR</h1>
  <h3>Système Intelligent de Contrôle d'Accès et Gestion de Parking</h3>
  <p><em>Automatic License Plate Recognition — Vision par Ordinateur & Deep Learning</em></p>
</div>

---

## Aperçu

**SmartParking ALPR** est un système complet de gestion de parking intelligent développé pour l'Université Catholique de Bukavu (UCB). Il automatise le contrôle d'accès des véhicules par reconnaissance automatique des plaques d'immatriculation (ALPR) grâce à la vision par ordinateur et au deep learning.

Bien qu'illustré pour l'UCB, son architecture modulaire permet un déploiement dans tout établissement ou parking d'entreprise.

### Fonctionnalités clés

- **Reconnaissance automatique des plaques** — Détection YOLOv8 + OCR Tesseract en temps réel
- **Double lecture entrée/sortie** — Validation du passage réel par deux caméras par site
- **Multi-sites dynamique** — Ajoutez des sites sans modifier le code
- **Barrière IP physique** — Contrôle automatisé avec simulation réseau
- **Tableau de bord temps réel** — KPIs, occupation, alertes
- **Export CSV** — Historique des fréquences appariées
- **Notifications** — Alertes internes et rappels export
- **Résilience** — Reconstruction d'état après coupure électrique

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Navigateur Web                         │
│            (Interface Bootstrap + JS)                    │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP / MJPEG
┌────────────────────▼────────────────────────────────────┐
│              Flask Backend (Python)                      │
│  ┌──────────┬──────────┬──────────┬──────────────────┐   │
│  │   Auth   │ Vehicles │   Logs   │   Notifications  │   │
│  ├──────────┼──────────┼──────────┼──────────────────┤   │
│  │   Site   │   Live   │   API    │   Admin          │   │
│  │ Policies │ Detection│  Gate    │   Staff          │   │
│  └──────────┴──────────┴──────────┴──────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│               Couche de Données                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │  SQLAlchemy  │  │    YOLOv8    │  │  Tesseract OCR│  │
│  │  (SQLite/    │  │  Detection   │  │  Reconnaissance│  │
│  │  PostgreSQL) │  │  Véhicules   │  │  Plaques      │  │
│  └──────────────┘  └──────────────┘  └───────────────┘  │
└─────────────────────────────────────────────────────────┘
```

### Stack technique

| Composant | Technologie |
|-----------|------------|
| **Backend** | Python 3.10+, Flask 3.0 |
| **ORM** | SQLAlchemy (SQLite / PostgreSQL) |
| **Computer Vision** | OpenCV, Ultralytics YOLOv8 (`yolov8n.pt`) |
| **OCR** | PyTesseract (Tesseract) |
| **Frontend** | Bootstrap 5.3, Jinja2, Vanilla JS |
| **Configuration** | YAML |
| **Physique** | Contrôleur IP (simulation socket) |

---

## Modèle de Données

### Diagramme relationnel

```
┌─────────┐       ┌──────────┐       ┌────────────┐
│   Site  │───────│ Vehicle  │       │  AccessLog │
├─────────┤  1:N  ├──────────┤  1:N  ├────────────┤
│ id      │       │ id       │       │ id         │
│ name    │       │ plate_nbr│       │ plate_nbr  │
│ code    │       │ owner    │       │ action     │
│ capacity│       │ model    │       │ timestamp  │
│ cameras │       │ status   │       │ site_id    │
│ policies│       │ site_id  │       │ guardian_id│
└─────────┘       └──────────┘       └────────────┘
                                              │
                     ┌────────────────────────┘
                     ▼
              ┌──────────────┐
              │ Notification │
              ├──────────────┤
              │ message      │
              │ category     │
              │ site_id      │
              │ guardian_id  │
              └──────────────┘
```

### Table `Site`

Champ | Type | Description
------|------|------------
`name` | String | Nom du site (ex: Site de Bugabo)
`code` | String | Trigramme unique (ex: BUG)
`capacity` | Integer | Places de stationnement
`camera_url_entry` | String | URL caméra entrée
`camera_url_exit` | String | URL caméra sortie
`max_hours_student` | Integer | Durée max étudiant (h)
`max_hours_visitor` | Integer | Durée max visiteur (h)
`access_start` | String | Heure d'ouverture
`access_end` | String | Heure de fermeture
`long_stay_hours` | Integer | Seuil dormeur (h)

---

## Algorithme de Double Lecture

Le système utilise **deux caméras par site** pour valider le passage réel des véhicules et éviter les fausses détections.

### Cycle d'entrée

```
1. Camera Entrée détecte la plaque
         │
2. Vérification : véhicule actif ET non présent ?
         │
         ├── Non → refus (log + notification)
         │
         └── Oui → Barrière s'ouvre
                    Plaque → _authorized_entries
                           │
3. Camera Sortie détecte la plaque
         │
4. Plaque dans _authorized_entries ?
         │
         ├── Oui → ENTRÉE CONFIRMÉE
         │         AccessLog(entry) créé
         │         Véhicule → _present
         │         Barrière se ferme
         │
         └── Non → ignoré (déjà présent)
```

### Cycle de sortie

```
1. Camera Sortie détecte la plaque
         │
2. Vérification : véhicule dans _present ?
         │
         ├── Non → refus
         │
         └── Oui → Barrière s'ouvre
                    Plaque → _authorized_exits
                           │
3. Camera Entrée détecte la plaque
         │
4. Plaque dans _authorized_exits ?
         │
         ├── Oui → SORTIE CONFIRMÉE
         │         AccessLog(exit) créé
         │         Durée calculée
         │         Véhicule retiré de _present
         │         Barrière se ferme
         │
         └── Non → ignoré
```

---

## Contrôle des Barrières Physiques

Le module `PhysicalBarrierController` gère l'envoi de commandes `OPEN`/`CLOSE` aux barrières IP réseau.

```
┌──────────────┐      Thread      ┌──────────────────┐
│  Flask App   │ ────────────────▶ │  Commande Réseau │
│  (instantané) │    asynchrone    │  (simulé 400ms)  │
└──────────────┘                   └──────────────────┘
                                          │
                                          ▼
                                  ┌──────────────────┐
                                  │ Barrière IP       │
                                  │ 192.168.1.10X:80 │
                                  └──────────────────┘
```

Les commandes réseau sont exécutées dans des **threads séparés** pour ne pas bloquer l'application. L'interface API `/api/gate-control` permet également une ouverture manuelle de secours.

---

## Résilience (Coupures Électriques)

Face aux coupures fréquentes dans la région de Bukavu, le système intègre :

1. **Reconstruction automatique** — Au démarrage, `init_presence_from_db()` analyse les derniers logs pour reconstruire l'état de présence en mémoire
2. **Journalisation transactionnelle** — Chaque passage est écrit immédiatement en base de données
3. **Recommandations matérielles** — Smart UPS, barrières fail-safe, débrayage manuel

---

## Installation

### Prérequis

- Python 3.10 ou supérieur
- Tesseract OCR installé sur le système
- Webcam ou fichier vidéo (mode démo)

### Étapes

```bash
# 1. Cloner le dépôt
git clone https://github.com/GermsM/SmartParking-ALPR.git
cd SmartParking-ALPR

# 2. Créer l'environnement virtuel
python -m venv venv

# 3. Activer l'environnement
# Windows :
venv\Scripts\activate
# Linux/Mac :
source venv/bin/activate

# 4. Installer les dépendances
pip install -r requirements.txt

# 5. Initialiser la base de données
python db_init.py

# 6. Lancer l'application
python app.py
```

### Configuration

Éditez `config.yaml` pour personnaliser :

- Les sites et leurs capacités
- Les URLs des caméras
- Les plages horaires d'accès
- Les seuils d'alerte

---

## Utilisation

### Comptes par défaut

| Rôle | Identifiant | Mot de passe |
|------|-------------|--------------|
| Administrateur | `admin` | `admin123` |
| Gardien | à créer via l'interface admin | généré automatiquement |

### Menu principal

| Menu | Rôle | Description |
|------|------|-------------|
| **Tableau de bord** | Tous | KPIs et statistiques temps réel |
| **Surveillance** | Admin | Mur vidéo multi-sites |
| **Détection live** | Gardien | Flux caméra avec détection YOLO |
| **Véhicules** | Tous | Gestion du registre des véhicules |
| **Historique** | Tous | Logs des mouvements |
| **Export CSV** | Tous | Export filtré avec appariement |
| **Politiques** | Admin | Configuration des sites |
| **Gardiens** | Admin | Gestion des comptes gardiens |
| **Notifications** | Tous | Centre d'alertes |

---

## API Endpoints

### Barrières

```
GET  /api/gate-status?site=<nom>     → État actuel des barrières
POST /api/gate-control               → Commande OPEN/CLOSE
     Body: site, direction, action, plate
```

### Sécurité

```
GET /api/security/alert              → État des alertes
GET /api/security/banned-alert       → Alertes plaques bannies
```

### Notifications

```
GET  /api/notifications/unread-count → Nombre de notifications non lues
POST /notifications/<id>/read        → Marquer comme lue
POST /notifications/<id>/supprimer   → Supprimer une notification
POST /notifications/clear-all        → Tout effacer
```

---

## Structure du Projet

```
SmartParking-ALPR/
├── app.py                  # Application Flask principale
├── config.py               # Configuration Python
├── config.yaml             # Configuration YAML centralisée
├── models.py               # Modèles SQLAlchemy (Site, Vehicle, AccessLog, etc.)
├── auth.py                 # Authentification et sessions
├── admin_users.py          # Gestion des comptes gardiens
├── vehicles.py             # CRUD véhicules
├── logs.py                 # Historique et export CSV
├── notifications.py        # Système de notifications
├── site_policies.py        # Configuration des sites
├── access_logging.py       # Logique entrée/sortie et présence
├── security_alerts.py      # Alertes de sécurité
├── dashboard_stats.py      # Calcul des KPIs
├── db_init.py              # Initialisation de la base de données
├── email_service.py        # Service d'e-mails (arrière-plan)
├── frequency_export.py     # Appariement des logs pour export
├── physical_barrier.py     # Contrôleur de barrière IP
├── scope.py                # Filtrage par rôle/site
├── templates/              # Templates Jinja2
│   ├── base.html           # Layout principal
│   ├── dashboard.html      # Tableau de bord
│   ├── live_detection.html # Détection en direct
│   ├── admin_videos.html   # Mur de surveillance
│   ├── vehicles.html       # Gestion des véhicules
│   ├── admin_gardiens.html # Gestion des gardiens
│   ├── admin_site_policies.html # Politiques des sites
│   ├── logs.html           # Historique
│   ├── export_frequency.html # Export CSV
│   ├── notifications.html  # Centre de notifications
│   └── ...                 # Autres pages
├── static/                 # Fichiers statiques (CSS, JS)
├── uploads/                # Vidéos démo uploadées
└── yolov8n.pt              # Modèle YOLOv8 pré-entraîné
```

---

## Roadmap

- [x] Reconnaissance automatique des plaques (ALPR)
- [x] Double lecture caméras entrée/sortie
- [x] Multi-sites dynamique
- [x] Contrôle barrière IP
- [x] Export CSV avec appariement
- [x] Notifications et alertes
- [ ] Intégration WhatsApp pour alertes dormeurs
- [ ] Tableau de bord analytique avancé
- [ ] Application mobile (Flutter)
- [ ] Mode hors-ligne (PWA)

---

## Licence

Ce projet est développé dans le cadre d'un mémoire de fin d'études. Tous droits réservés &mdash; Université Catholique de Bukavu.

---

<div align="center">
  <p>
    Développé avec ❤️ pour l'UCB &mdash; <em>Automatisation & Intelligence Artificielle</em>
  </p>
  <p>
    <a href="https://github.com/GermsM/SmartParking-ALPR/issues">Signaler un bug</a>
    ·
    <a href="https://github.com/GermsM/SmartParking-ALPR/discussions">Discussion</a>
  </p>
</div>
