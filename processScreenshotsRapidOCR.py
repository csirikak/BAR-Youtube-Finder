"""Extract BAR player names with one GPU model owner and bounded image batches."""

import argparse
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import tempfile
import time

import cv2

import console as ui
from ocr_names import PlayerNames


logger = ui.get("ocr")


DEBUG_MODE = False
MODEL_PATH = "ui_detector.pt"
YOLO_CONF_THRESHOLD = 0.80
SCREENSHOTS_DIR = "data/AllBarScreenshots"
JSON_OUTPUT_FILE = "data/screenshot_data.json"
PLAYER_DATABASE = "data/game_battles.db"
FRAME_NAME = re.compile(r"^(.+)_(\d+)s\.png$")


def load_metadata(filename):
    path = Path(filename)
    if not path.exists():
        return {}
    # A damaged database must stop the run, never be replaced with empty data.
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(info, dict)
        or not isinstance(info.get("screenshots", {}), dict)
        for info in data.values()
    ):
        raise ValueError(f"Invalid screenshot metadata structure: {filename}")
    return data


def save_metadata(filename, data):
    """Publish a complete checkpoint atomically, preserving the previous on error."""
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=4, sort_keys=True, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def pending_frames(directory, data, reprocess=False):
    frames = []
    for path in sorted(Path(directory).rglob("*.png")):
        match = FRAME_NAME.fullmatch(path.name)
        if not match:  # Includes unfinished scraper captures (*.part.png).
            continue
        video_id, timestamp = match.groups()
        if reprocess or timestamp not in data.get(video_id, {}).get("screenshots", {}):
            frames.append((path, video_id, timestamp))
    return frames


def merge_checkpoint(filename, results, metadata_lock):
    """Merge only OCR changes; the scraper may have added metadata meanwhile."""
    with metadata_lock:
        latest = load_metadata(filename)
        for (video_id, timestamp), names in results.items():
            latest.setdefault(video_id, {}).setdefault("screenshots", {})[timestamp] = names
        save_metadata(filename, latest)


# Recognition is a sequence of tiny GPU calls; a few readers with separate CUDA
# streams overlap its idle gaps without changing what any single panel returns.
DEFAULT_OCR_WORKERS = 3


def resolve_device(device="auto"):
    import torch

    if device == "auto":
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
    resolved = torch.device(device)
    if resolved.type not in {"cpu", "cuda"}:
        raise ValueError("OCR supports cpu or cuda[:index] (including AMD ROCm)")
    if resolved.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("GPU requested but PyTorch cannot access it")
    return resolved


def reader_params(resolved):
    from rapidocr import EngineType, ModelType, OCRVersion

    # Explicit versions keep upstream default changes from silently selecting
    # different models. Small PP-OCRv6 won our accuracy/speed comparison.
    return {
        "Det.engine_type": EngineType.TORCH,
        "Cls.engine_type": EngineType.TORCH,
        "Rec.engine_type": EngineType.TORCH,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Det.model_type": ModelType.SMALL,
        "Rec.model_type": ModelType.SMALL,
        "EngineConfig.torch.use_cuda": resolved.type == "cuda",
        "EngineConfig.torch.cuda_ep_cfg.device_id": resolved.index or 0,
        "Global.use_cls": False,  # Player lists are always upright.
        "Global.text_score": 0.5,
        "Global.log_level": "warning",
        "Det.limit_side_len": 384,
        "Det.box_thresh": 0.4,  # Recover dim, colored text on translucent panels.
        "Rec.rec_batch_num": 32,
    }


def create_engines(model_path, device="auto", workers=1):
    """Build the UI detector once and one RapidOCR reader per worker."""
    import torch
    from rapidocr import RapidOCR
    from ultralytics import YOLO

    if not Path(model_path).is_file():
        raise FileNotFoundError(f"UI detector model not found: {model_path}")
    resolved = resolve_device(device)
    torch.set_num_threads(4)
    cv2.setNumThreads(2)
    if resolved.type == "cuda":
        label = torch.cuda.get_device_name(resolved.index or 0)
    else:
        label = "CPU"
    readers = [RapidOCR(params=reader_params(resolved))
               for _ in range(max(1, workers))]
    logger.info("OCR device %s (%s) · %d reader(s)", resolved, label, len(readers))
    detector = YOLO(str(model_path))
    detector.to(str(resolved))
    return detector, readers


def create_models(model_path, device="auto"):
    """Single-reader convenience wrapper kept for callers and tests."""
    detector, readers = create_engines(model_path, device, 1)
    return detector, readers[0]


def worker_count(device="auto", requested=None):
    """Auto-select readers: overlap on GPU, stay single-process on CPU."""
    resolved = resolve_device(device)
    if requested:
        return max(1, int(requested))
    return DEFAULT_OCR_WORKERS if resolved.type == "cuda" else 1


def panel_bounds(result, image_shape):
    height, width = image_shape[:2]
    if result.boxes is None:
        return None
    candidates = []
    for box in result.boxes:
        confidence = float(box.conf[0])
        if confidence <= YOLO_CONF_THRESHOLD:
            continue
        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
        x1, x2 = max(0, min(x1, width)), max(0, min(x2, width))
        y1, y2 = max(0, min(y1, height)), max(0, min(y2, height))
        if x1 > width * 0.5 and x2 - x1 >= 30 and y2 - y1 >= 30:
            candidates.append((confidence, (x1, y1, x2, y2)))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def find_ui_panel(screenshot, yolo_model):
    # Let inference errors propagate: an error is not evidence of an empty frame.
    return panel_bounds(yolo_model(screenshot, verbose=False)[0], screenshot.shape)


def names_from_result(result, catalog, panel_width):
    if result is None or not result.txts:
        return []
    rows = sorted(zip(result.boxes, result.txts, result.scores),
                  key=lambda row: (min(p[1] for p in row[0]), min(p[0] for p in row[0])))
    names = []
    for box, text, score in rows:
        if score is not None and score < 0.5:
            continue
        # Rank icons resemble Chinese glyphs. Discard isolated badge text only
        # in the rank column; retain Unicode player names in the name column.
        if (max(p[0] for p in box) < panel_width * 0.30
                and re.fullmatch(r"[参谷会鑫米灸多系众業矣乡\d\s]+", text)
                and text.strip() not in catalog.names):
            continue
        name = catalog.resolve(text)
        if name is not None and name not in names:
            names.append(name)
    return names


def read_image(path):
    image = cv2.imread(str(path))
    if image is None:
        raise ValueError(f"Unreadable screenshot: {path}")
    return image


def recognize_panel(image, bounds, reader, catalog, debug=False):
    if bounds is None:
        return []
    x1, y1, x2, y2 = bounds
    panel = image[y1:y2, x1:x2]
    names = names_from_result(reader(panel), catalog, panel.shape[1])
    if debug:
        cv2.imshow("Player panel", panel)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
    return names


def ocr_bottom_right_element(screenshot_path, debug=False, reader=None,
                             yolo_model=None, catalog=None):
    """Single-frame entry point; empty results and inference failures are distinct."""
    image = read_image(screenshot_path)
    bounds = find_ui_panel(image, yolo_model)
    return recognize_panel(image, bounds, reader, catalog or PlayerNames(), debug)


def detect_batch(detector, images):
    """Retry individually after a batch failure so healthy frames still complete."""
    try:
        results = list(detector(images, verbose=False))
        if len(results) != len(images):
            raise RuntimeError("UI detector returned an incomplete batch")
        return results
    except Exception as error:
        logger.warning("UI detection batch failed; retrying individually: %s",
                       ui.escape(str(error)))
        results = []
        for image in images:
            try:
                results.append(detector(image, verbose=False)[0])
            except Exception as individual_error:
                results.append(individual_error)
        return results


def make_recognizer_pool(readers, device, debug=False):
    """Exclusive reader slots, each on its own CUDA stream so calls overlap."""
    if debug or len(readers) < 2:
        return None, None
    import torch
    from queue import Queue

    streams = ([torch.cuda.Stream(device=device) for _ in readers]
               if device.type == "cuda" else [None] * len(readers))
    slots = Queue()
    for reader, stream in zip(readers, streams):
        slots.put((reader, stream))
    return ThreadPoolExecutor(max_workers=len(readers)), slots


def main(*, screenshots_dir=SCREENSHOTS_DIR, output_file=JSON_OUTPUT_FILE,
         model_path=MODEL_PATH, player_database=PLAYER_DATABASE, device="auto",
         batch_size=8, checkpoint_every=50, reprocess=False, debug=DEBUG_MODE,
         frame_batches=None, metadata_lock=None, ocr_workers=None):
    if batch_size < 1 or checkpoint_every < 1:
        raise ValueError("Batch size and checkpoint interval must be positive")
    if frame_batches is not None and metadata_lock is None:
        raise ValueError("Streaming OCR requires the scraper's metadata lock")
    with metadata_lock if metadata_lock is not None else nullcontext():
        data = load_metadata(output_file)
    total = "stream"
    if frame_batches is None:
        frames = pending_frames(screenshots_dir, data, reprocess)
        total = len(frames)
        frame_batches = (frames[offset:offset + batch_size]
                         for offset in range(0, len(frames), batch_size))
    catalog = detector = reader = None
    ocr_pool = slots = None
    completed = 0
    dirty = {}
    failures = []
    started = time.perf_counter()

    def failed(frame, error):
        failures.append(frame[0].name)
        logger.warning("Frame %s left for retry: %s", frame[0].name,
                       ui.escape(str(error)))

    def record(frame, names):
        nonlocal completed
        _, video_id, timestamp = frame
        data.setdefault(video_id, {}).setdefault("screenshots", {})[timestamp] = names
        completed += 1
        dirty[(video_id, timestamp)] = names
        logger.debug("[%d/%s] %s: %s", completed, total, frame[0].name,
                     ui.escape(str(names)))
        if len(dirty) >= checkpoint_every:
            checkpoint()

    def recognize_with_slot(image, bounds):
        reader_instance, stream = slots.get()
        try:
            if stream is None:
                return recognize_panel(image, bounds, reader_instance, catalog, debug)
            import torch
            with torch.cuda.stream(stream):
                return recognize_panel(image, bounds, reader_instance, catalog, debug)
        finally:
            slots.put((reader_instance, stream))

    def checkpoint():
        if metadata_lock is None:
            save_metadata(output_file, data)
        else:
            merge_checkpoint(output_file, dirty, metadata_lock)
        dirty.clear()
        logger.info("OCR · %d%s frame(s) recognized", completed,
                    f"/{total}" if isinstance(total, int) else "")

    try:
        with ThreadPoolExecutor(max_workers=min(4, batch_size)) as decoders:
            for batch in frame_batches:
                batch = [frame for frame in batch if reprocess or frame[2] not in
                         data.get(frame[1], {}).get("screenshots", {})]
                if not batch:
                    continue
                if reader is None:
                    catalog = PlayerNames.from_database(player_database)
                    logger.info("OCR starting · %s · %d known player names",
                                f"{total} screenshot(s)" if isinstance(total, int)
                                else "streaming capture", len(catalog.names))
                    resolved = resolve_device(device)
                    detector, readers = create_engines(
                        model_path, str(resolved),
                        worker_count(str(resolved), ocr_workers))
                    reader = readers[0]
                    ocr_pool, slots = make_recognizer_pool(readers, resolved, debug)
                # Submit only this batch: full-resolution screenshots can be large.
                futures = [decoders.submit(read_image, frame[0]) for frame in batch]
                images, valid = [], []
                for frame, future in zip(batch, futures):
                    try:
                        images.append(future.result())
                        valid.append(frame)
                    except Exception as error:
                        failed(frame, error)
                if not images:
                    continue
                detections = detect_batch(detector, images)
                if ocr_pool is None:
                    for frame, image, detection in zip(valid, images, detections):
                        try:
                            if isinstance(detection, Exception):
                                raise detection
                            bounds = panel_bounds(detection, image.shape)
                            names = recognize_panel(image, bounds, reader, catalog, debug)
                        except Exception as error:
                            failed(frame, error)
                            continue
                        record(frame, names)
                else:
                    jobs = []
                    for frame, image, detection in zip(valid, images, detections):
                        if isinstance(detection, Exception):
                            failed(frame, detection)
                            continue
                        try:
                            bounds = panel_bounds(detection, image.shape)
                        except Exception as error:
                            failed(frame, error)
                            continue
                        jobs.append((frame, ocr_pool.submit(recognize_with_slot,
                                                            image, bounds)))
                    for frame, job in jobs:
                        try:
                            names = job.result()
                        except Exception as error:
                            failed(frame, error)
                            continue
                        record(frame, names)
    finally:
        if ocr_pool is not None:
            ocr_pool.shutdown(wait=True)
        # Preserve completed work even after Ctrl-C or an unexpected exception.
        if dirty:
            checkpoint()
    if reader is None:
        logger.info("No pending screenshots; OCR models were not loaded")
    logger.info("OCR · %d completed · %d failed · %s", completed, len(failures),
                ui.duration(time.perf_counter() - started))
    if failures:
        logger.error("OCR left %d frame(s) for retry; successful results were saved",
                     len(failures))
        # update.py must stop before its screenshot-directory cleanup.
        raise RuntimeError(f"OCR failed for {len(failures)} frame(s); successful results "
                           "were saved. Rerun to retry the failed frames.")
    return {"processed": completed, "failed": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots-dir", default=SCREENSHOTS_DIR)
    parser.add_argument("--output-file", default=JSON_OUTPUT_FILE)
    parser.add_argument("--model-path", default=MODEL_PATH)
    parser.add_argument("--player-database", default=PLAYER_DATABASE)
    parser.add_argument("--device", default="auto", help="auto, cpu, or cuda[:index] (CUDA/ROCm)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--ocr-workers", type=int, default=None,
                        help="parallel OCR readers (default: 3 on GPU, 1 on CPU)")
    parser.add_argument("--checkpoint-every", type=int, default=50)
    parser.add_argument("--reprocess", action="store_true", help="Replace results for available frames")
    parser.add_argument("--debug", action="store_true")
    main(**vars(parser.parse_args()))
