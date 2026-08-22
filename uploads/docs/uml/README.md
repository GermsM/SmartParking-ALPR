# Modélisation UML — SmartParking UCB

Ces fichiers PlantUML ont été produits à partir de la structure actuelle du
projet. Ils couvrent les éléments habituellement demandés pour un projet
tutoré : cas d'utilisation, composants, classes, séquence et activité.

## Fichiers

- `01-cas-utilisation.puml` : fonctions disponibles pour l'administrateur et le gardien.
- `02-diagramme-classes.puml` : classes métier et relations de la base de données.
- `03-diagramme-composants.puml` : architecture Flask, vision, base de données et services.
- `04-sequence-acces.puml` : détection ALPR et décision d'accès.
- `05-activite-enregistrement.puml` : enregistrement et validation d'un véhicule.
- `06-diagramme-etats.puml` : machine à états du statut d'un véhicule (pending/active/banned).
- `07-sequence-notifications.puml` : enchaînement des e-mails automatiques (enregistrement, stationnement prolongé).
- `08-diagramme-etats-barriere.puml` : machine à états de la barrière physique (ouverture/fermeture temporisée).

## Dictionnaire des données

Le fichier [`../dictionnaire_donnees.md`](../dictionnaire_donnees.md) documente
toutes les tables (Site, User, Vehicle, AccessLog, Notification, NotificationRead)
: colonnes, types, contraintes, énumérations métier et relations de clés
étrangères.

## Export automatique

1. Ouvrir le dossier du projet dans VS Code.
2. Installer l'extension **PlantUML**.
3. Ouvrir un fichier `.puml` puis lancer la prévisualisation PlantUML.
4. Dans la prévisualisation, choisir **Export Current Diagram** au format PNG
   pour le rapport ou SVG pour conserver une image nette.

Les diagrammes décrivent le périmètre actuel : la gestion de chauffeurs n'est
pas incluse, car cette fonction n'est plus exposée par l'application.
