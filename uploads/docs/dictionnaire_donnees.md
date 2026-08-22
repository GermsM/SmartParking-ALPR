# Dictionnaire des données — SmartParking ALPR

Ce document décrit le modèle de données persistant (SQLAlchemy / SQLite ou
PostgreSQL). Les colonnes `site_*` (texte) et `site_id` (entier) coexistent par
compatibilité historique : `site_id` est la clé étrangère vers `Site`, `site` est
le nom en clair conservé pour les lectures simples.

---

## Table `site`

Site (campus / parking) géré par le système.

| Colonne | Type | Contraintes | Description |
|---------|------|-------------|-------------|
| `id` | Integer | PK, auto | Identifiant unique du site |
| `name` | String(80) | UNIQUE, NOT NULL | Nom lisible (ex : *Mgr Mulindwa*) |
| `code` | String(20) | UNIQUE, NOT NULL | Trigramme site (ex : `MUL`) |
| `capacity` | Integer | défaut 50 | Nombre de places |
| `camera_url_entry` | String(255) | nullable | Flux caméra d'entrée (HTTP/RTSP/IP) |
| `camera_url_exit` | String(255) | nullable | Flux caméra de sortie |
| `max_hours_student` | Integer | défaut 8 | Durée max autorisée — étudiant (h) |
| `max_hours_visitor` | Integer | défaut 4 | Durée max autorisée — visiteur (h) |
| `access_start` | String(10) | défaut `06:00` | Heure d'ouverture (`HH:MM`) |
| `access_end` | String(10) | défaut `22:00` | Heure de fermeture (`HH:MM`) |
| `long_stay_hours` | Integer | défaut 48 | Seuil « stationnement prolongé » (h) |
| `gate_ip` | String(255) | nullable | Adresse IP du contrôleur de barrière |

---

## Table `user`

Comptes (administrateur / gardien).

| Colonne | Type | Contraintes | Description |
|---------|------|-------------|-------------|
| `id` | Integer | PK, auto | Identifiant |
| `username` | String(80) | UNIQUE, NOT NULL | Identifiant de connexion |
| `password` | String(255) | NOT NULL | Hash Werkzeug (`generate_password_hash`) |
| `role` | String(20) | NOT NULL | `admin` ou `gardien` |
| `full_name` | String(100) | nullable | Nom affiché |
| `site` | String(50) | nullable | Nom du site assigné (gardien) |
| `site_id` | Integer | FK `site.id`, nullable | Site assigné (gardien) |
| `is_active` | Boolean | NOT NULL, défaut True | Compte actif ? |
| `must_change_password` | Boolean | NOT NULL, défaut False | Doit changer le mot de passe ? |

---

## Table `vehicle`

Registre des véhicules et de leurs propriétaires.

| Colonne | Type | Contraintes | Description |
|---------|------|-------------|-------------|
| `id` | Integer | PK, auto | Identifiant |
| `plate_number` | String(20) | UNIQUE, NOT NULL | Plaque ou identifiant interne `UCB-CODE-0001` |
| `owner_name` | String(100) | NOT NULL | Nom du propriétaire |
| `owner_phone` | String(30) | nullable | Téléphone (appel / WhatsApp) |
| `owner_email` | String(120) | nullable | E-mail (notifications automatiques) |
| `owner_address` | String(255) | nullable | Adresse |
| `vehicle_model` | String(120) | nullable | Modèle |
| `vehicle_type` | String(30) | défaut `auto` | `auto`, `moto`, `sans_plaque` |
| `function` | String(100) | nullable | Fonction / catégorie (ex : visiteur) |
| `site_authorized` | String(80) | nullable | Site autorisé (NULL = tous) |
| `site_id` | Integer | FK `site.id`, nullable | Site autorisé (FK) |
| `status` | String(20) | défaut `pending` | `pending`, `active`, `banned` |
| `created_by` | Integer | FK `user.id` | Créateur (gardien) |
| `created_at` | DateTime | défaut `utcnow` | Date d'enregistrement |

---

## Table `access_log`

Journal des passages (entrées / sorties) et évènements de sécurité.

| Colonne | Type | Contraintes | Description |
|---------|------|-------------|-------------|
| `id` | Integer | PK, auto | Identifiant |
| `plate_number` | String(20) | NOT NULL | Plaque / identifiant détecté |
| `vehicle_id` | Integer | FK `vehicle.id`, nullable | Véhicule lié |
| `action` | String(10) | nullable | `entry` ou `exit` |
| `status` | String(20) | nullable | `authorized`, `pending`, `banned`, `unknown`, `forbidden_type`, `manual` |
| `site` | String(50) | nullable | Nom du site |
| `site_id` | Integer | FK `site.id`, nullable | Site (FK) |
| `timestamp` | DateTime | défaut `utcnow` | Date/heure de l'évènement |
| `guardian_id` | Integer | FK `user.id`, nullable | Gardien ayant validé |
| `duration_minutes` | Integer | nullable | Durée de stationnement (sortie) |

---

## Table `notification`

Alertes système visibles par les utilisateurs.

| Colonne | Type | Contraintes | Description |
|---------|------|-------------|-------------|
| `id` | Integer | PK, auto | Identifiant |
| `site` | String(50) | nullable | Site concerné (NULL = tous) |
| `site_id` | Integer | FK `site.id`, nullable | Site (FK) |
| `category` | String(30) | nullable | `approve`, `ban`, `reactivate`, `delete`, `long_stay`, `export_reminder`, `system`, … |
| `message` | String(500) | NOT NULL | Texte de l'alerte |
| `plate_number` | String(20) | nullable | Plaque concernée |
| `guardian_id` | Integer | FK `user.id`, nullable | Gardien ciblé |
| `contact_phone` | String(30) | nullable | Téléphone du propriétaire |
| `whatsapp_message` | String(1000) | nullable | Message WhatsApp pré-rempli |
| `created_at` | DateTime | défaut `utcnow` | Date de création |
| `is_read` | Boolean | NOT NULL, défaut False | Lu (globale, complétée par `notification_read`) |

---

## Table `notification_read`

Lecture *par utilisateur* d'une notification (une ligne par couple).

| Colonne | Type | Contraintes | Description |
|---------|------|-------------|-------------|
| `id` | Integer | PK, auto | Identifiant |
| `notification_id` | Integer | FK `notification.id`, NOT NULL | Notification |
| `user_id` | Integer | FK `user.id`, NOT NULL | Utilisateur |
| `read_at` | DateTime | défaut `utcnow` | Date de lecture |

Contrainte unique : `(notification_id, user_id)`.

---

## Énumérations métier

- **`user.role`** : `admin`, `gardien`
- **`vehicle.status`** : `pending` (en attente), `active` (autorisé), `banned` (interdit)
- **`vehicle.vehicle_type`** : `auto`, `moto`, `sans_plaque`
- **`access_log.action`** : `entry`, `exit`
- **`access_log.status`** : `authorized`, `pending`, `banned`, `unknown`, `forbidden_type`, `manual`
- **`notification.category`** : `approve`, `ban`, `reactivate`, `delete`, `long_stay`, `export_reminder`, `system`

---

## Relations (FK)

- `user.site_id` → `site.id`
- `vehicle.site_id` → `site.id`
- `vehicle.created_by` → `user.id`
- `access_log.vehicle_id` → `vehicle.id`
- `access_log.site_id` → `site.id`
- `access_log.guardian_id` → `user.id`
- `notification.site_id` → `site.id`
- `notification.guardian_id` → `user.id`
- `notification_read.notification_id` → `notification.id`
- `notification_read.user_id` → `user.id`
- `driver_assignment.vehicle_id` → `vehicle.id`
- `driver_assignment.assigned_by` → `user.id`
