"""Envoi d'emails (SMTP optionnel) et liens de contact propriétaires."""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

import config

_log = logging.getLogger("ucb_parking.email")


def _smtp_configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def send_owner_email(to_email: str, subject: str, body: str) -> bool:
    to_email = (to_email or "").strip()
    if not to_email:
        return False

    import threading

    def _send_thread():
        if not _smtp_configured():
            _log.info("[EMAIL SIMULÉ — SMTP non configuré] À: %s | Sujet: %s\n%s", to_email, subject, body)
            return

        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = config.SMTP_FROM
            msg["To"] = to_email
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as srv:
                srv.ehlo()
                srv.starttls()
                srv.ehlo()
                srv.login(config.SMTP_USER, config.SMTP_PASSWORD)
                srv.send_message(msg)
            _log.info("E-mail envoyé avec succès à %s", to_email)
        except Exception as exc:
            _log.error("Échec envoi email à %s (arrière-plan): %s", to_email, exc)

    t = threading.Thread(target=_send_thread, daemon=True)
    t.start()
    return True


def notify_owner_registration(
    *,
    owner_name: str,
    owner_email: str,
    plate: str,
    site: str | None,
    status: str,
    vehicle_model: str | None = None,
    pending: bool = False,
) -> bool:
    """E-mail professionnel de confirmation d'enregistrement au parking UCB."""
    name = owner_name or "Monsieur/Madame"
    site_label = site if site else "l'ensemble des sites du campus UCB (Bukavu)"
    model_line = f"\nModèle : {vehicle_model}" if vehicle_model else ""

    if pending:
        subject = f"[UCB Parking] Demande d'enregistrement — véhicule {plate}"
        body = (
            f"Bonjour {name},\n\n"
            f"Nous accusons réception de votre demande d'enregistrement au système de parking "
            f"de l'Université Catholique de Bukavu (UCB).\n\n"
            f"Récapitulatif :\n"
            f"  • Identifiant / plaque : {plate}{model_line}\n"
            f"  • Site demandé : {site_label}\n"
            f"  • Statut : en attente de validation par l'administration\n\n"
            f"Vous recevrez un second message dès que votre accès sera confirmé.\n\n"
            f"Pour toute question, contactez le gardien de votre site.\n\n"
            f"Cordialement,\n"
            f"Service Parking — UCB Bukavu\n"
            f"{config.SMTP_FROM}\n"
        )
    else:
        subject = f"[UCB Parking] Véhicule enregistré — {plate}"
        body = (
            f"Bonjour {name},\n\n"
            f"Votre véhicule est désormais enregistré et autorisé au parking de l'UCB.\n\n"
            f"Récapitulatif :\n"
            f"  • Identifiant / plaque : {plate}{model_line}\n"
            f"  • Site autorisé : {site_label}\n"
            f"  • Statut : actif — accès au parking autorisé\n\n"
            f"Merci de respecter le règlement du parking (horaires, durée de stationnement, "
            f"signalisation). En cas de problème, le gardien de site peut vous contacter "
            f"aux coordonnées enregistrées dans le système.\n\n"
            f"Cordialement,\n"
            f"Service Parking — Université Catholique de Bukavu (UCB)\n"
            f"Bukavu\n"
        )
    return send_owner_email(owner_email, subject, body)


def _site_label(site: str | None) -> str:
    return site if site else "l'ensemble des sites du campus UCB (Bukavu)"


def notify_owner_banned(
    *,
    owner_name: str,
    owner_email: str,
    plate: str,
    site: str | None,
    reason: str = "décision administrative",
) -> bool:
    name = owner_name or "Monsieur/Madame"
    subject = f"[UCB Parking] Accès parking suspendu — {plate}"
    body = (
        f"Bonjour {name},\n\n"
        f"Nous vous informons que l'accès de votre véhicule immatriculé {plate} "
        f"au parking de l'Université Catholique du Congo (UCB) a été suspendu.\n\n"
        f"Site concerné : {_site_label(site)}\n"
        f"Motif : {reason}\n\n"
        f"Pour toute contestation ou régularisation, veuillez contacter "
        f"l'administration du parking ou le gardien de votre site.\n\n"
        f"Cordialement,\n"
        f"Service Parking — UCB Bukavu\n"
    )
    return send_owner_email(owner_email, subject, body)


def notify_owner_reactivated(
    *,
    owner_name: str,
    owner_email: str,
    plate: str,
    site: str | None,
) -> bool:
    name = owner_name or "Monsieur/Madame"
    subject = f"[UCB Parking] Accès parking rétabli — {plate}"
    body = (
        f"Bonjour {name},\n\n"
        f"Bonne nouvelle : l'accès de votre véhicule {plate} au parking UCB "
        f"a été rétabli par l'administration.\n\n"
        f"Site autorisé : {_site_label(site)}\n"
        f"Statut : actif — vous pouvez à nouveau utiliser le parking selon le règlement en vigueur.\n\n"
        f"Cordialement,\n"
        f"Service Parking — Université Catholique de Bukavu (UCB)\n"
        f"Bukavu\n"
    )
    return send_owner_email(owner_email, subject, body)


def notify_owner_profile_updated(
    *,
    owner_name: str,
    owner_email: str,
    plate: str,
    site: str | None,
    changes: list[str],
) -> bool:
    name = owner_name or "Monsieur/Madame"
    subject = f"[UCB Parking] Mise à jour de votre dossier — {plate}"
    changes_text = "\n".join(f"  • {c}" for c in changes) if changes else "  • Informations mises à jour"
    body = (
        f"Bonjour {name},\n\n"
        f"Votre dossier véhicule au parking UCB a été mis à jour par l'administration.\n\n"
        f"Plaque / identifiant : {plate}\n"
        f"Site : {_site_label(site)}\n\n"
        f"Modifications enregistrées :\n{changes_text}\n\n"
        f"Si vous n'êtes pas à l'origine de cette demande, contactez le service parking UCB.\n\n"
        f"Cordialement,\n"
        f"Service Parking — UCB Bukavu\n"
    )
    return send_owner_email(owner_email, subject, body)


def build_long_stay_whatsapp_message(
    owner_name: str,
    plate: str,
    site: str,
    hours: float,
) -> str:
    name = owner_name or "Monsieur/Madame"
    return (
        f"Bonjour {name},\n\n"
        f"Nous vous informons que votre véhicule immatriculé {plate} est stationné "
        f"sur le site {site} depuis plus de {hours:.0f} heures, au-delà de la durée autorisée.\n"
        f"Merci de le déplacer dans les plus brefs délais ou de contacter le service sécurité UCB.\n\n"
        f"Cordialement,\nGardien parking — Université Catholique de Bukavu (UCB)"
    )


def notify_owner_long_stay(plate: str, owner_name: str, owner_email: str, hours: float, site: str) -> bool:
    subject = f"[UCB Parking] Véhicule {plate} — stationnement prolongé"
    body = (
        f"Bonjour {owner_name or 'propriétaire'},\n\n"
        f"Votre véhicule immatriculé {plate} est enregistré sur le site {site} "
        f"depuis environ {hours:.0f} heures.\n"
        f"Merci de le déplacer ou de contacter le gardien du parking.\n\n"
        f"— Système Parking UCB (projet académique)\n"
    )
    return send_owner_email(owner_email, subject, body)

