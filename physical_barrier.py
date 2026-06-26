import threading
import time

class PhysicalBarrierController:
    """
    Simulateur de controleur de barriere physique IP.
    Dans une installation reelle, cette classe enverrait des requetes HTTP
    ou des sockets TCP/UDP à un automate ou un relais IP (ex: ESP32 / Arduino / Relais Ethernet).
    """

    @staticmethod
    def _send_network_command(ip_address: str, port: int, command: str, site_name: str):
        """
        Methode interne executee dans un thread secondaire pour simuler
        l'appel reseau asynchrone sans ralentir l'application Flask.
        """
        try:
            # Simulation d'un delai de latence reseau (ex: 400ms)
            time.sleep(0.4)
            print(f"[BARRIERE PHYSIQUE] Commande '{command}' envoyee avec succes a l'adresse IP {ip_address}:{port} (Site: {site_name})")
        except Exception as e:
            print(f"[BARRIERE PHYSIQUE] Erreur lors de l'envoi de la commande a {ip_address}:{port} : {str(e)}")

    @classmethod
    def trigger_gate(cls, action: str, ip_address: str | None = None, port: int | None = None, site_name: str = "Inconnu"):
        """
        Declenche l'ouverture ou la fermeture de la barriere.
        action: 'OPEN' ou 'CLOSE'
        """
        # Adresses par defaut
        ip = ip_address or "192.168.1.100"
        p = port or 80

        # Verification de la commande
        action = action.upper()
        if action not in ("OPEN", "CLOSE"):
            print(f"[BARRIERE PHYSIQUE] Commande invalide ignoree: {action}")
            return

        print(f"[BARRIERE PHYSIQUE] Preparation de l'envoi de '{action}' a {ip}:{p} (Site: {site_name})...")
        
        # Lancement dans un thread separe pour preserver les performances de l'application
        thread = threading.Thread(
            target=cls._send_network_command,
            args=(ip, p, action, site_name),
            daemon=True
        )
        thread.start()
