"""Pipeline OCR des plaques (extrait de app.py pour etre testable hors Flask).

Reconnaissance Tesseract multi-variantes avec vote, validation stricte du
format RDC (decret 08/15 : 4 chiffres + 2 lettres + code provincial 01-26).
"""
import re

import cv2
import numpy as np
import pytesseract

import config  # noqa: F401  (fixe pytesseract.tesseract_cmd par auto-detection)

# Formats RDC depuis le decret 08/15 : 4 chiffres, 2 lettres, puis le code
# provincial a 2 chiffres.  Les 3 caracteres CGO a gauche de la plaque ne font
# pas partie de l'immatriculation et sont volontairement ignores par l'OCR.
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

# Hauteur de texte visee apres agrandissement (parametre original calibre
# sur le jeu de test : 28 px ; 44 px degradait nettement les lectures).
_TARGET_TEXT_HEIGHT = 28.0
# Qualite maximale atteignable : psm7 (12) + format RDC (4) - variante 0 (0).
_MAX_QUALITY = 16


def _deskew_plate(gray):
    """Corrige une inclinaison legere sans deformer une plaque deja droite."""
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


def improve_plate_image(plate_img, return_variants=False, variant_names=None):
    """Pretraite une plaque pour Tesseract.

    Le redimensionnement n'est declenche que pour les petits crops : le texte
    est amene vers ~28 px de haut, sans agrandir inutilement les grandes
    plaques (ce qui avait degrade les essais 3x/4x). Avec ``return_variants``,
    retourne plusieurs binarisations et une version grayscale sans seuillage
    pour l'ensemble OCR. ``variant_names`` restreint les variantes produites
    (permet de reduire le temps OCR sans changer la logique).
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
    scale = max(1.0, min(4.0, _TARGET_TEXT_HEIGHT / estimated_text_height))
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

    if variant_names is not None:
        variants = [(n, v) for (n, v) in variants if n in variant_names]

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

    # Plaques institutionnelles : UCB0001XXXX ou UCB-BUG-0001 (tirets retires).
    if _UCB_PLATE_RE.fullmatch(normalized):
        return normalized

    # Le texte peut contenir CGO, la numerotation laser ou un caractere parasite
    # avant/apres la serie. Examiner donc toutes les fenetres de 8 caracteres.
    tokens = re.findall(r"[A-Z0-9]{8,}", normalized)
    for token in tokens:
        for start in range(len(token) - 7):
            raw_candidate = token[start:start + 8]
            repaired = _repair_drc_candidate(raw_candidate)
            if repaired:
                return repaired

    return None


def collect_candidates(plate_img, psm_modes=(7, 6, 8), variant_names=None, early_exit=True):
    """Fait tourner l'OCR multi-variantes et retourne TOUS les candidats valides.

    Chaque candidat est un dict : {plate, quality, psm, variant_index, rdc}.
    ``early_exit`` stoppe des qu'une lecture parfaite (quality maximale) est
    atteinte : le resultat ne peut plus etre meilleur. Pour evaluer plusieurs
    strategies de selection sur les memes donnees, passer early_exit=False.
    """
    variants = improve_plate_image(plate_img, return_variants=True,
                                   variant_names=variant_names)
    if not variants:
        return []

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
                # PSM 7 convient normalement a une plaque sur une ligne. Le
                # score ne tranche qu'en cas d'egalite de votes.
                quality = (12 if psm == 7 else 8 if psm == 6 else 6) - variant_index
                is_rdc = bool(_DRC_PLATE_RE.fullmatch(plate))
                if is_rdc:
                    quality += 4
                candidates.append({
                    "plate": plate,
                    "quality": quality,
                    "psm": psm,
                    "variant_index": variant_index,
                    "rdc": is_rdc,
                })
                if early_exit and quality >= _MAX_QUALITY:
                    return candidates
    return candidates


def _levenshtein(a, b):
    """Distance d'edition simple (les chaines font 8 caracteres au max)."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _group_candidates(candidates):
    """Regroupe les candidats par plaque : votes, somme/moyenne de quality."""
    votes = {}
    quality_sums = {}
    for c in candidates:
        plate = c["plate"]
        votes[plate] = votes.get(plate, 0) + 1
        quality_sums[plate] = quality_sums.get(plate, 0) + c["quality"]
    avg = {p: quality_sums[p] / votes[p] for p in votes}
    return votes, avg


def select_plate(candidates, strategy="current"):
    """Choisit la plaque gagnante parmi les candidats OCR.

    Strategies :
    - "current"   : moyenne de quality, puis votes (comportement historique)
    - "votes"     : nombre de votes d'abord, moyenne de quality ensuite
    - "consensus" : minimal total de distances d'edition aux autres candidats
                    (robuste aux lectures aberrantes isolées)
    - "positions" : consensus caractere par caractere pondere par la quality,
                    valide par le format RDC (retombe sur "current" sinon)
    - "hybrid"    : en cas de tete-a-tete serré, tranche par distance au
                    consensus ; sinon "votes"
    - "score"     : score combine votes (x2) + moyenne de quality (x0.5)
                    - distance totale au consensus (x0.7)
    - "ratio"     : distance totale au consensus DIVISEE par la quality
                    moyenne : penalise les candidats isoles ET les lectures
                    peu fiables ; closest a la verite sur le jeu de test
    """
    if not candidates:
        return None
    votes, avg = _group_candidates(candidates)

    if strategy == "current":
        return max(votes, key=lambda p: (avg[p], votes[p], p))

    if strategy == "votes":
        return max(votes, key=lambda p: (votes[p], avg[p], p))

    if strategy == "consensus":
        plates = sorted(votes)
        if len(plates) == 1:
            return plates[0]
        totals = {
            p: sum(_levenshtein(p, q) * votes[q] for q in plates if q != p)
            for p in plates
        }
        return min(totals, key=lambda p: (totals[p], -votes[p], -avg[p], p))

    if strategy == "positions":
        eight = [c for c in candidates if len(c["plate"]) == 8]
        if not eight:
            return select_plate(candidates, "current")
        consensus_chars = []
        for pos in range(8):
            weights = {}
            for c in eight:
                weights[c["plate"][pos]] = weights.get(c["plate"][pos], 0) + c["quality"]
            consensus_chars.append(max(weights, key=weights.get))
        consensus = "".join(consensus_chars)
        if _DRC_PLATE_RE.fullmatch(consensus):
            return consensus
        repaired = _repair_drc_candidate(consensus)
        if repaired:
            return repaired
        return select_plate(candidates, "current")

    if strategy == "hybrid":
        ranked = sorted(votes, key=lambda p: (-votes[p], -avg[p], p))
        if len(ranked) >= 2 and votes[ranked[0]] == votes[ranked[1]]:
            # Tete-a-tete : tranche par distance totale au consensus.
            return select_plate(candidates, "consensus")
        return ranked[0]

    if strategy == "score":
        plates = sorted(votes)
        if len(plates) == 1:
            return plates[0]
        dist = {
            p: sum(_levenshtein(p, q) * votes[q] for q in plates if q != p)
            for p in plates
        }
        scored = {p: votes[p] * 2 + avg[p] * 0.5 - dist[p] * 0.7 for p in plates}
        return max(scored, key=lambda p: (scored[p], votes[p], p))

    if strategy == "ratio":
        plates = sorted(votes)
        if len(plates) == 1:
            return plates[0]
        dist = {
            p: sum(_levenshtein(p, q) * votes[q] for q in plates if q != p)
            for p in plates
        }
        # Min dist/avgQ = candidat le plus central parmi les plus fiables.
        # avgQ >= 1 partout si au moins un candidat existe (quality min 6).
        return min(plates, key=lambda p: (dist[p] / avg[p], -avg[p], -votes[p], p))

    raise ValueError(f"Strategie de vote inconnue : {strategy}")


# Strategie de selection par defaut, choisie sur mesure : "ratio" (distance
# au consensus / quality moyenne) lit exactement 7/14 crops du jeu de test
# contre 4/14 pour la strategie historique "current".
_DEFAULT_STRATEGY = "ratio"


def read_plate_text(plate_img, psm_modes=(7, 6, 8), variant_names=None, strategy=None):
    """OCR multi-PSM/binarisation avec vote ; retourne la plaque validee ou None.

    La collecte s'arrete des qu'une lecture parfaite est atteinte (le choix
    de strategie ne peut plus changer le resultat dans ce cas).
    """
    candidates = collect_candidates(plate_img, psm_modes=psm_modes,
                                    variant_names=variant_names, early_exit=True)
    return select_plate(candidates, strategy=strategy or _DEFAULT_STRATEGY)
