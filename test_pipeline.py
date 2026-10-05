"""Diagnostic du pipeline de detection : vehicules YOLO + plaques YOLO + OCR.

Usage:
    python test_pipeline.py [--outdir diag_out]

- Passe les images de uploads/, uploads/demo_images et uploads/Test_OCR
  dans le meme pipeline que l'app (meme logique que DetectionWorker._process_once).
- Utilise plate_ocr.py, le MEME module OCR que app.py : aucun risque de divergence.
- Compare le texte OCR au nom de fichier (verite terrain, ex: 0227AF22.jpg).
- Mesure le temps par etape et sauvegarde des images annotees dans diag_out/.
- Ne touche ni a la base de donnees ni au serveur : 100% lecture seule.
"""
from __future__ import annotations

import argparse
import os
import re
import time

import cv2
import numpy as np
from ultralytics import YOLO

import config  # definit pytesseract.pytesseract.tesseract_cmd (auto-detection)
from plate_ocr import read_plate_text

# ---------------------------------------------------------------------------
# Pipeline de test
# ---------------------------------------------------------------------------


def ground_truth_from_name(path: str) -> str | None:
    stem = os.path.splitext(os.path.basename(path))[0]
    m = re.fullmatch(r"(?:[A-Z]{2})?(\d{4}[A-Z]{2}\d{2})", stem)
    if m:
        return m.group(1)
    return stem.upper() if re.fullmatch(r"[A-Z0-9]{6,10}", stem) else None


def collect_images():
    roots = ["uploads", os.path.join("uploads", "demo_images"),
             os.path.join("uploads", "Test_OCR", "test_ocr", "plaques_seules"),
             os.path.join("uploads", "Test_OCR", "test_ocr", "vehicules_entiers")]
    seen, images = set(), []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            if not fn.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            path = os.path.normpath(os.path.join(root, fn))
            if path in seen:
                continue
            seen.add(path)
            images.append(path)
    return images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", default="diag_out")
    parser.add_argument("--plate-imgsz", type=int, default=416,
                        help="imgsz du modele plaque (valeur de l'app : 416)")
    parser.add_argument("--ocr-fast", action="store_true",
                        help="OCR reduit : 2 variantes x 2 PSM au lieu de 5 x 3 (test perf)")
    args = parser.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    print("Chargement des modeles...")
    t0 = time.time()
    model = YOLO("yolov8n.pt")
    plate_model = YOLO(os.path.join("models", "license_plate.pt"))
    print(f"  Modeles charges en {time.time() - t0:.1f}s")
    print(f"  Classes modele plaque : {plate_model.names}")

    images = collect_images()
    print(f"\n{len(images)} images a tester\n" + "=" * 70)

    grand_total = {"veh_ok": 0, "veh_total": 0, "det_ok": 0, "det_total": 0,
                   "ocr_ok": 0, "ocr_total": 0}
    timing = {"yolo_veh": 0.0, "yolo_plate": 0.0, "ocr": 0.0}

    ocr_kwargs = {"variant_names": {"adaptive", "otsu"}, "psm_modes": (7, 6)} if args.ocr_fast else {}

    for path in images:
        truth = ground_truth_from_name(path)
        frame = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            print(f"[SKIP] {path} (illisible)")
            continue
        H, W = frame.shape[:2]

        t = time.time()
        results = model(frame, conf=0.38, verbose=False, imgsz=480)
        timing["yolo_veh"] += time.time() - t

        t = time.time()
        p_results = plate_model(frame, conf=0.25, verbose=False, imgsz=args.plate_imgsz)
        timing["yolo_plate"] += time.time() - t

        plate_boxes = []
        for pbox in p_results[0].boxes:
            if int(pbox.cls[0]) != 0:
                continue
            px1, py1, px2, py2 = map(int, pbox.xyxy[0])
            plate_boxes.append((px1, py1, px2, py2, float(pbox.conf[0])))

        veh_boxes = []
        for vbox in results[0].boxes:
            x1, y1, x2, y2 = map(int, vbox.xyxy[0])
            veh_boxes.append((x1, y1, x2, y2, model.names[int(vbox.cls[0])], float(vbox.conf[0])))

        annotated = frame.copy()
        for (x1, y1, x2, y2, cname, cconf) in veh_boxes:
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (255, 128, 0), 2)
            cv2.putText(annotated, f"{cname} {cconf:.2f}", (x1, max(0, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 128, 0), 2)
        for (px1, py1, px2, py2, pconf) in plate_boxes:
            cv2.rectangle(annotated, (px1, py1), (px2, py2), (0, 255, 0), 2)

        ocr_results = []
        for (px1, py1, px2, py2, pconf) in plate_boxes:
            t = time.time()
            plate_img = frame[py1:py2, px1:px2]
            text = read_plate_text(plate_img, **ocr_kwargs)
            timing["ocr"] += time.time() - t
            status = "?"
            if truth and text:
                status = "OK" if text == truth else "DIFF"
            elif text:
                status = "LU"
            elif truth:
                status = "ECHEC"
            ocr_results.append((status, text or "-", pconf))
            color = {"OK": (0, 200, 0), "LU": (0, 200, 0), "DIFF": (0, 0, 255),
                     "ECHEC": (0, 0, 255), "?": (0, 160, 255)}[status]
            cv2.putText(annotated, f"{status}:{text or '?'}", (px1, max(0, py1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

        # Fallback identique a l'app : une seule tentative moitie basse du vehicule
        if not any(s in ("OK", "LU", "DIFF") for s, _, _ in ocr_results):
            for (x1, y1, x2, y2, cname, _) in veh_boxes:
                if cname in ("bus", "truck"):
                    continue
                h = y2 - y1
                roi = frame[int(y1 + h * 0.52):y2, x1:x2]
                if roi.size == 0:
                    continue
                t = time.time()
                text = read_plate_text(roi, **ocr_kwargs)
                timing["ocr"] += time.time() - t
                if text:
                    status = "OK" if text == truth else "DIFF"
                    ocr_results.append((status, text, 0.0))
                    cv2.rectangle(annotated, (x1, int(y1 + h * 0.52)), (x2, y2), (0, 140, 255), 2)
                    cv2.putText(annotated, f"FALLBACK:{status}:{text}", (x1, max(0, y1 - 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 140, 255), 2)
                    break

        # Stats
        veh_present = any(c in ("car", "motorcycle") for *_, c, _ in veh_boxes)
        grand_total["veh_total"] += 1
        grand_total["veh_ok"] += 1 if (veh_boxes or not veh_present) else 0
        grand_total["det_total"] += 1 if truth else 0
        det_ok = any(t_ and r[1] == t_ for t_, r in ((truth, r) for r in ocr_results))
        grand_total["det_ok"] += 1 if det_ok else 0
        grand_total["ocr_total"] += len(ocr_results)
        grand_total["ocr_ok"] += sum(1 for s, _, _ in ocr_results if s == "OK")

        truth_s = truth or "-"
        det_s = "OK" if det_ok else "KO"
        print(f"\n{path}  [{W}x{H}]  verite={truth_s}")
        print(f"  vehicules : {[(c, round(cf, 2)) for *_, c, cf in veh_boxes] or 'aucun'}")
        print(f"  plaques   : {len(plate_boxes)} detectee(s)")
        for status, text, pconf in ocr_results:
            print(f"    OCR -> {status:6s} texte={text!r} (conf plaque {pconf:.2f})")
        if not ocr_results:
            print("    OCR -> aucune plaque exploitable")
        print(f"  plaque trouvee == verite : {det_s}")

        out_name = os.path.basename(path)
        cv2.imwrite(os.path.join(args.outdir, f"diag_{out_name}"), annotated)

    print("\n" + "=" * 70)
    print("BILAN")
    print(f"  Images testees                 : {grand_total['veh_total']}")
    print(f"  Plaque lue == verite terrain   : {grand_total['det_ok']}/{grand_total['det_total']}")
    print(f"  Lectures OCR totales (OK/total): {grand_total['ocr_ok']}/{grand_total['ocr_total']}")
    tot_t = sum(timing.values())
    print(f"  Temps cumule  veh={timing['yolo_veh']:.1f}s  plaque={timing['yolo_plate']:.1f}s  ocr={timing['ocr']:.1f}s  (total {tot_t:.1f}s)")
    print(f"  Images annotees dans : {os.path.abspath(args.outdir)}")


if __name__ == "__main__":
    main()
