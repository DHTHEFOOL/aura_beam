"""
Script danh gia metric cho AuraBeam.

Muc tieu:
- Chay truc tiep tren pipeline hien tai ma khong can mo cua so UI.
- Do cac proxy metric co the lay ngay tu project.
- Neu co ground truth CSV thi tinh them metric phuc vu paper
  nhu RMSE va GSSR.

Ground truth CSV duoc ho tro theo format toi thieu:
    frame,x1,y1,x2,y2

Co the bo sung:
    z

Moi frame toi da 1 dong, dai dien cho muc tieu uu tien can che choi.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path
import numpy as np
import cv2

from detector_ensemble import EnsembleDetector
from pseudo_radar import VirtualRadar
from sensor_fusion import KalmanFilter3D
from zone_logic import Matrix8x8Controller


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate AuraBeam metrics")
    parser.add_argument("--primary-model", type=str, default="model/yolov5.pt")
    parser.add_argument("--secondary-model", type=str, default="model/model_ai.pt")
    parser.add_argument("--source", type=str, default="demo_video/one_way.mp4")
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument(
        "--ablation-mode",
        type=str,
        default="yolo_kf_radar",
        choices=("yolo", "yolo_kf", "yolo_kf_radar"),
        help="Che do danh gia cho bai bao: YOLO only, YOLO+KF, hoac YOLO+KF+Radar.",
    )
    parser.add_argument("--radar-initial-z", type=float, default=100.0)
    parser.add_argument("--radar-target-speed", type=float, default=15.0)
    parser.add_argument("--track-hold-seconds", type=float, default=1.0)
    parser.add_argument("--gt-csv", type=str, default=None, help="Ground truth CSV voi cot frame,x1,y1,x2,y2[,z].")
    parser.add_argument("--gt-coco", type=str, default=None, help="COCO JSON annotation, vd snow_1/_annotations.coco.json")
    parser.add_argument(
        "--gt-folder",
        type=str,
        default=None,
        help="Thu muc Roboflow COCO chua _annotations.coco.json va anh da cat tu video.",
    )
    parser.add_argument(
        "--gt-align",
        type=str,
        default="uniform",
        choices=("none", "uniform"),
        help="Can chinh frame ground truth vao video. 'uniform' phu hop khi anh duoc cat deu tu video.",
    )
    parser.add_argument("--gssr-iou-threshold", type=float, default=0.5)
    parser.add_argument("--output-json", type=str, default="metrics.json")
    parser.add_argument("--output-csv", type=str, default="per_frame_metrics.csv")
    return parser.parse_args()


def open_capture(source_arg: str) -> cv2.VideoCapture:
    source = int(source_arg) if source_arg.isdigit() else source_arg
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open source: {source_arg}")
    return cap


def resolve_coco_annotation_path(gt_coco: str | None, gt_folder: str | None) -> Path | None:
    if gt_coco:
        coco_path = Path(gt_coco)
        if not coco_path.exists():
            raise FileNotFoundError(f"Khong tim thay COCO annotation: {gt_coco}")
        return coco_path

    if gt_folder:
        folder_path = Path(gt_folder)
        if not folder_path.is_dir():
            raise NotADirectoryError(f"Khong tim thay thu muc ground truth: {gt_folder}")

        coco_path = folder_path / "_annotations.coco.json"
        if not coco_path.exists():
            raise FileNotFoundError(f"Khong tim thay file _annotations.coco.json trong: {gt_folder}")
        return coco_path

    return None


def load_ground_truth(csv_path: str | None) -> dict[int, dict]:
    if not csv_path:
        return {}

    gt_map: dict[int, dict] = {}
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"frame", "x1", "y1", "x2", "y2"}
        if not required.issubset(reader.fieldnames or set()):
            raise ValueError("GT CSV must contain columns: frame,x1,y1,x2,y2")

        for row in reader:
            frame_id = int(row["frame"])
            gt_map[frame_id] = {
                "x1": float(row["x1"]),
                "y1": float(row["y1"]),
                "x2": float(row["x2"]),
                "y2": float(row["y2"]),
                "z": float(row["z"]) if row.get("z") not in (None, "") else None,
            }

    return gt_map


def extract_frame_id_from_name(name: str) -> int | None:
    match = re.search(r"-(\d+)(?:_jpg|\.jpg|\.png|\.jpeg)", name, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def load_ground_truth_coco(coco_path: str | None) -> dict[int, dict]:
    if not coco_path:
        return {}

    coco = json.loads(Path(coco_path).read_text(encoding="utf-8"))
    images_by_id = {image["id"]: image for image in coco.get("images", [])}
    anns_by_frame: dict[int, list[dict[str, float]]] = {}

    for ann in coco.get("annotations", []):
        image_info = images_by_id.get(ann["image_id"])
        if image_info is None:
            continue

        source_name = (
            image_info.get("extra", {}).get("name")
            or image_info.get("file_name")
            or ""
        )
        frame_id = extract_frame_id_from_name(source_name)
        if frame_id is None:
            continue

        bbox = ann.get("bbox", [])
        if len(bbox) != 4:
            continue

        x1, y1, width, height = map(float, bbox)
        x2 = x1 + width
        y2 = y1 + height
        area = float(ann.get("area", width * height))

        anns_by_frame.setdefault(frame_id + 1, []).append(
            {
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
                "area": area,
                "category_id": float(ann.get("category_id", -1)),
                "z": None,
            }
        )

    gt_map: dict[int, dict] = {}
    for frame_id, annotations in anns_by_frame.items():
        # Chon bbox co dien tich lon nhat lam muc tieu uu tien chong choi.
        primary = max(annotations, key=lambda item: item["area"])
        gt_map[frame_id] = {
            "x1": primary["x1"],
            "y1": primary["y1"],
            "x2": primary["x2"],
            "y2": primary["y2"],
            "z": primary["z"],
            "num_annotations": len(annotations),
        }

    return gt_map


def matrix_box_to_tuple(command: str | None) -> tuple[int, int, int, int] | None:
    if command is None:
        return None

    parts = command.strip().split(":")
    if len(parts) != 5 or parts[0] != "BOX":
        return None

    col_start, col_end, row_start, row_end = map(int, parts[1:])
    if col_start < 0 or col_end < 0 or row_start < 0 or row_end < 0:
        return None

    return col_start, col_end, row_start, row_end


def discrete_box_iou(box_a: tuple[int, int, int, int] | None, box_b: tuple[int, int, int, int] | None) -> float:
    if box_a is None or box_b is None:
        return 0.0

    ax1, ax2, ay1, ay2 = box_a
    bx1, bx2, by1, by2 = box_b

    inter_x1 = max(ax1, bx1)
    inter_x2 = min(ax2, bx2)
    inter_y1 = max(ay1, by1)
    inter_y2 = min(ay2, by2)

    if inter_x1 > inter_x2 or inter_y1 > inter_y2:
        return 0.0

    intersection = (inter_x2 - inter_x1 + 1) * (inter_y2 - inter_y1 + 1)
    area_a = (ax2 - ax1 + 1) * (ay2 - ay1 + 1)
    area_b = (bx2 - bx1 + 1) * (by2 - by1 + 1)
    union = area_a + area_b - intersection

    return float(intersection) / float(union) if union > 0 else 0.0


def euclidean_distance(point_a: tuple[float, float], point_b: tuple[float, float]) -> float:
    return math.hypot(point_a[0] - point_b[0], point_a[1] - point_b[1])


def rms(values: list[float]) -> float:
    if not values:
        return 0.0
    return math.sqrt(sum(value * value for value in values) / len(values))


def safe_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def estimate_depth_from_box_width(box: np.ndarray) -> float:
    width = max(1.0, float(box[2] - box[0]))
    return 8000.0 / width


def extract_annotation_track_id(annotation: dict) -> int | None:
    direct_candidates = [
        annotation.get("track_id"),
        annotation.get("instance_id"),
        annotation.get("object_id"),
    ]
    for candidate in direct_candidates:
        if candidate not in (None, ""):
            return int(candidate)

    attributes = annotation.get("attributes", {})
    if isinstance(attributes, dict):
        for key in ("track_id", "instance_id", "object_id", "id"):
            candidate = attributes.get(key)
            if candidate not in (None, ""):
                return int(candidate)
    return None


def load_ground_truth_track_map_coco(coco_path: str | None) -> tuple[dict[int, list[dict]], bool]:
    if not coco_path:
        return {}, False

    coco = json.loads(Path(coco_path).read_text(encoding="utf-8"))
    images_by_id = {image["id"]: image for image in coco.get("images", [])}
    track_map: dict[int, list[dict]] = {}
    has_track_ids = False

    for ann in coco.get("annotations", []):
        image_info = images_by_id.get(ann["image_id"])
        if image_info is None:
            continue

        source_name = image_info.get("extra", {}).get("name") or image_info.get("file_name") or ""
        frame_id = extract_frame_id_from_name(source_name)
        if frame_id is None:
            continue

        bbox = ann.get("bbox", [])
        if len(bbox) != 4:
            continue

        track_id = extract_annotation_track_id(ann)
        if track_id is None:
            continue

        has_track_ids = True
        x1, y1, width, height = map(float, bbox)
        track_map.setdefault(frame_id + 1, []).append(
            {
                "track_id": int(track_id),
                "cls": int(ann.get("category_id", -1)),
                "box": np.array([x1, y1, x1 + width, y1 + height], dtype=np.float64),
            }
        )

    return track_map, has_track_ids


def greedy_match_iou(
    predicted_objects: list[dict],
    gt_objects: list[dict],
    iou_threshold: float,
) -> list[tuple[int, int]]:
    candidates: list[tuple[float, int, int]] = []
    for pred_index, pred in enumerate(predicted_objects):
        for gt_index, gt in enumerate(gt_objects):
            if int(pred.get("cls", -1)) != int(gt.get("cls", -1)):
                continue
            iou = detector_box_iou(pred["box"], gt["box"])
            if iou >= iou_threshold:
                candidates.append((iou, pred_index, gt_index))

    candidates.sort(reverse=True, key=lambda item: item[0])
    used_pred: set[int] = set()
    used_gt: set[int] = set()
    matches: list[tuple[int, int]] = []
    for _, pred_index, gt_index in candidates:
        if pred_index in used_pred or gt_index in used_gt:
            continue
        used_pred.add(pred_index)
        used_gt.add(gt_index)
        matches.append((pred_index, gt_index))
    return matches


def detector_box_iou(box_a: np.ndarray, box_b: np.ndarray) -> float:
    x1 = max(float(box_a[0]), float(box_b[0]))
    y1 = max(float(box_a[1]), float(box_b[1]))
    x2 = min(float(box_a[2]), float(box_b[2]))
    y2 = min(float(box_a[3]), float(box_b[3]))

    inter_w = max(0.0, x2 - x1)
    inter_h = max(0.0, y2 - y1)
    intersection = inter_w * inter_h
    area_a = max(0.0, float(box_a[2] - box_a[0])) * max(0.0, float(box_a[3] - box_a[1]))
    area_b = max(0.0, float(box_b[2] - box_b[0])) * max(0.0, float(box_b[3] - box_b[1]))
    union = area_a + area_b - intersection
    if union <= 0.0:
        return 0.0
    return intersection / union


def compute_multi_target_idf1(
    pair_counts: dict[int, dict[int, int]],
    total_gt_detections: int,
    total_pred_detections: int,
) -> float | None:
    if total_gt_detections <= 0:
        return None

    weighted_pairs: list[tuple[int, int, int]] = []
    for pred_id, gt_counts in pair_counts.items():
        for gt_id, count in gt_counts.items():
            weighted_pairs.append((count, pred_id, gt_id))

    weighted_pairs.sort(reverse=True)
    used_pred_ids: set[int] = set()
    used_gt_ids: set[int] = set()
    idtp = 0
    for count, pred_id, gt_id in weighted_pairs:
        if pred_id in used_pred_ids or gt_id in used_gt_ids:
            continue
        used_pred_ids.add(pred_id)
        used_gt_ids.add(gt_id)
        idtp += count

    idfp = max(0, total_pred_detections - idtp)
    idfn = max(0, total_gt_detections - idtp)
    denominator = (2 * idtp) + idfp + idfn
    if denominator <= 0:
        return 0.0
    return (2.0 * idtp) / denominator


def remap_gt_frames_uniform(gt_map: dict[int, dict], total_video_frames: int) -> dict[int, dict]:
    if not gt_map or total_video_frames <= 0:
        return gt_map

    sorted_items = sorted(gt_map.items())
    if len(sorted_items) == 1:
        only_key, only_value = sorted_items[0]
        return {1: {**only_value, "source_gt_frame": only_key}}

    remapped: dict[int, dict] = {}
    last_target = 0
    num_items = len(sorted_items)

    for idx, (source_frame, gt_value) in enumerate(sorted_items):
        target_frame = int(round(idx * (total_video_frames - 1) / (num_items - 1))) + 1
        target_frame = max(target_frame, last_target + 1)
        target_frame = min(target_frame, total_video_frames)
        remapped[target_frame] = {**gt_value, "source_gt_frame": source_frame}
        last_target = target_frame

    return remapped


def main() -> None:
    args = parse_args()
    coco_annotation_path = resolve_coco_annotation_path(args.gt_coco, args.gt_folder)
    gt_map = load_ground_truth(args.gt_csv)
    gt_track_map: dict[int, list[dict]] = {}
    gt_has_track_ids = False
    if coco_annotation_path is not None:
        gt_map = load_ground_truth_coco(str(coco_annotation_path))
        gt_track_map, gt_has_track_ids = load_ground_truth_track_map_coco(str(coco_annotation_path))
    cap = open_capture(args.source)

    screen_width = cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1280.0
    screen_height = cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 720.0
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if gt_map and args.gt_align == "uniform":
        gt_map = remap_gt_frames_uniform(gt_map, total_video_frames)

    detector = EnsembleDetector(
        primary_model_path=args.primary_model,
        secondary_model_path=args.secondary_model,
        conf_threshold=args.conf,
    )

    effective_fps = fps if fps and fps > 1e-6 else 30.0
    sampled_fps = effective_fps
    if gt_map and total_video_frames > 0:
        sampled_fps = effective_fps * (len(gt_map) / total_video_frames)
        sampled_fps = max(sampled_fps, 1e-6)
    dt = 1.0 / sampled_fps

    radar = VirtualRadar(
        fps=sampled_fps,
        initial_z=args.radar_initial_z,
        target_speed=args.radar_target_speed,
    )
    matrix_controller = Matrix8x8Controller(matrix_width=8, matrix_height=8)
    max_missed_frames = max(1, int(round(sampled_fps * args.track_hold_seconds)))

    kalman_trackers: dict[int, KalmanFilter3D] = {}
    last_seen_frames: dict[int, int] = {}
    last_classes: dict[int, int] = {}
    predicted_states: dict[int, tuple[float, float, float]] = {}
    next_track_id = 1
    video_frame_index = 0
    processed_frame_index = 0

    total_processing_times_ms: list[float] = []
    detection_times_ms: list[float] = []
    fusion_times_ms: list[float] = []
    raw_jitter_px: list[float] = []
    smooth_jitter_px: list[float] = []
    raw_centers: list[tuple[float, float]] = []
    smooth_centers: list[tuple[float, float]] = []
    serial_box_changes = 0
    predicted_only_frames = 0
    active_target_frames = 0

    rmse_x_errors: list[float] = []
    rmse_y_errors: list[float] = []
    rmse_z_errors: list[float] = []
    dark_box_ious: list[float] = []
    glare_success_flags: list[int] = []
    gt_present_frames = 0
    gt_present_but_no_box = 0
    false_darkening_frames = 0
    predicted_dark_frames = 0
    id_pair_counts: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    idf1_total_gt_detections = 0
    idf1_total_pred_detections = 0

    last_command: str | None = None
    per_frame_rows: list[dict[str, object]] = []

    try:
        while True:
            frame_start = time.perf_counter()
            ret, frame = cap.read()
            if not ret:
                break

            video_frame_index += 1
            if gt_map and video_frame_index not in gt_map:
                continue

            processed_frame_index += 1
            z_radar = radar.get_distance(processed_frame_index)
            smoothed_vehicles: list[tuple[int, float, float, float, int]] = []
            assigned_tracks: set[int] = set()
            gt = gt_map.get(video_frame_index)
            gt_center: tuple[float, float] | None = None
            if gt is not None:
                gt_center = ((gt["x1"] + gt["x2"]) * 0.5, (gt["y1"] + gt["y2"]) * 0.5)

            use_kalman = args.ablation_mode in ("yolo_kf", "yolo_kf_radar")
            use_radar = args.ablation_mode == "yolo_kf_radar"

            if use_kalman:
                for track_id, tracker in list(kalman_trackers.items()):
                    missed_count = processed_frame_index - last_seen_frames.get(track_id, processed_frame_index)
                    if missed_count > max_missed_frames:
                        kalman_trackers.pop(track_id, None)
                        last_seen_frames.pop(track_id, None)
                        last_classes.pop(track_id, None)
                        predicted_states.pop(track_id, None)
                        continue

                    predicted_states[track_id] = tracker.predict()

            detect_start = time.perf_counter()
            fused_detections = detector.detect(frame)
            detection_times_ms.append((time.perf_counter() - detect_start) * 1000.0)
            assigned_detections: list[dict] = []
            predicted_objects_for_idf1: list[dict] = []
            if use_kalman:
                assigned_detections, assigned_tracks, next_track_id = detector.assign_tracks(
                    fused_detections=fused_detections,
                    kalman_trackers=kalman_trackers,
                    predicted_states=predicted_states,
                    last_classes=last_classes,
                    dt=dt,
                    next_track_id=next_track_id,
                )
            else:
                for detection in fused_detections:
                    assigned_detection = dict(detection)
                    assigned_detection["center_x"], assigned_detection["center_y"] = detector.center_of_box(detection["box"])
                    assigned_detections.append(assigned_detection)

            fusion_start = time.perf_counter()
            raw_active_center: tuple[float, float] | None = None
            smooth_active_center: tuple[float, float] | None = None
            active_target: tuple[int, float, float, float, int] | None = None

            if use_kalman:
                for detection in assigned_detections:
                    best_track_id = detection["track_id"]
                    center_x = detection["center_x"]
                    center_y = detection["center_y"]
                    cls_id = detection["cls"]
                    tracker = kalman_trackers[best_track_id]
                    if use_radar:
                        meas_z = z_radar
                    else:
                        meas_z = estimate_depth_from_box_width(detection["box"])

                    smoothed_state = tracker.update(center_x, center_y, meas_z, predict_first=False)
                    last_seen_frames[best_track_id] = processed_frame_index
                    last_classes[best_track_id] = cls_id

                    sx, sy, sz = smoothed_state
                    vehicle = (best_track_id, sx, sy, sz, cls_id)
                    smoothed_vehicles.append(vehicle)
                    predicted_objects_for_idf1.append(
                        {
                            "track_id": int(best_track_id),
                            "cls": int(cls_id),
                            "box": np.array(detection["box"], dtype=np.float64),
                        }
                    )

                frame_has_prediction_only = False
                for track_id, tracker in kalman_trackers.items():
                    if track_id in assigned_tracks:
                        continue

                    missed_count = processed_frame_index - last_seen_frames.get(track_id, processed_frame_index)
                    if missed_count <= max_missed_frames:
                        fill_z = z_radar if use_radar else tracker.get_state()[2]
                        sx, sy, sz = tracker.update(None, None, fill_z, predict_first=False)
                        cls_id = last_classes.get(track_id, -1)
                        smoothed_vehicles.append((track_id, sx, sy, sz, cls_id))
                        frame_has_prediction_only = True

                if frame_has_prediction_only:
                    predicted_only_frames += 1
            else:
                for detection_index, detection in enumerate(assigned_detections, start=1):
                    center_x = float(detection["center_x"])
                    center_y = float(detection["center_y"])
                    cls_id = int(detection["cls"])
                    meas_z = estimate_depth_from_box_width(detection["box"])
                    smoothed_vehicles.append((detection_index, center_x, center_y, meas_z, cls_id))
                    predicted_objects_for_idf1.append(
                        {
                            "track_id": int((processed_frame_index * 100000) + detection_index),
                            "cls": int(cls_id),
                            "box": np.array(detection["box"], dtype=np.float64),
                        }
                    )

            if assigned_detections:
                if gt_center is not None:
                    best_detection = min(
                        assigned_detections,
                        key=lambda item: euclidean_distance(
                            (float(item["center_x"]), float(item["center_y"])),
                            gt_center,
                        ),
                    )
                else:
                    best_detection = max(assigned_detections, key=lambda item: item["conf"])
                raw_active_center = (float(best_detection["center_x"]), float(best_detection["center_y"]))

            if smoothed_vehicles:
                if gt_center is not None:
                    active_target = min(
                        smoothed_vehicles,
                        key=lambda item: euclidean_distance((float(item[1]), float(item[2])), gt_center),
                    )
                else:
                    active_target = min(smoothed_vehicles, key=lambda item: item[3])
                _, smooth_x, smooth_y, smooth_z, _ = active_target
                smooth_active_center = (float(smooth_x), float(smooth_y))
                active_target_frames += 1
                active_command = matrix_controller.get_dark_box(
                    smooth_x=smooth_x,
                    smooth_y=smooth_y,
                    smooth_z=smooth_z,
                    screen_width=screen_width,
                    screen_height=screen_height,
                )
            else:
                active_command = None

            fusion_times_ms.append((time.perf_counter() - fusion_start) * 1000.0)

            if raw_active_center is not None:
                raw_centers.append(raw_active_center)
                if len(raw_centers) >= 2:
                    raw_jitter_px.append(euclidean_distance(raw_centers[-1], raw_centers[-2]))

            if smooth_active_center is not None:
                smooth_centers.append(smooth_active_center)
                if len(smooth_centers) >= 2:
                    smooth_jitter_px.append(euclidean_distance(smooth_centers[-1], smooth_centers[-2]))

            if active_command != last_command:
                serial_box_changes += 1
                last_command = active_command

            gt_matrix_box = None
            if gt is not None:
                gt_present_frames += 1
                gt_center_x = (gt["x1"] + gt["x2"]) * 0.5
                gt_center_y = (gt["y1"] + gt["y2"]) * 0.5
                gt_z = gt["z"] if gt["z"] is not None else (active_target[3] if active_target is not None else z_radar)
                gt_box_cmd = matrix_controller.get_dark_box(
                    smooth_x=gt_center_x,
                    smooth_y=gt_center_y,
                    smooth_z=gt_z,
                    screen_width=screen_width,
                    screen_height=screen_height,
                )
                gt_matrix_box = matrix_box_to_tuple(gt_box_cmd)

                if smooth_active_center is not None:
                    rmse_x_errors.append(smooth_active_center[0] - gt_center_x)
                    rmse_y_errors.append(smooth_active_center[1] - gt_center_y)
                    if gt["z"] is not None and active_target is not None:
                        rmse_z_errors.append(active_target[3] - float(gt["z"]))

                pred_matrix_box = matrix_box_to_tuple(active_command)
                iou = discrete_box_iou(pred_matrix_box, gt_matrix_box)
                dark_box_ious.append(iou)
                glare_success_flags.append(1 if iou >= args.gssr_iou_threshold else 0)

                if pred_matrix_box is not None:
                    predicted_dark_frames += 1
                    if gt_matrix_box is None or iou < args.gssr_iou_threshold:
                        false_darkening_frames += 1

                if gt_matrix_box is not None and pred_matrix_box is None:
                    gt_present_but_no_box += 1

            gt_track_objects = gt_track_map.get(video_frame_index, [])
            if gt_has_track_ids and gt_track_objects:
                idf1_total_gt_detections += len(gt_track_objects)
                idf1_total_pred_detections += len(predicted_objects_for_idf1)
                id_matches = greedy_match_iou(predicted_objects_for_idf1, gt_track_objects, args.gssr_iou_threshold)
                for pred_index, gt_index in id_matches:
                    pred_track_id = int(predicted_objects_for_idf1[pred_index]["track_id"])
                    gt_track_id = int(gt_track_objects[gt_index]["track_id"])
                    id_pair_counts[pred_track_id][gt_track_id] += 1

            total_processing_times_ms.append((time.perf_counter() - frame_start) * 1000.0)

            per_frame_rows.append(
                {
                    "frame": processed_frame_index,
                    "video_frame": video_frame_index,
                    "processed_frame": processed_frame_index,
                    "z_radar": round(z_radar, 4),
                    "num_detections": len(fused_detections),
                    "num_tracks": len(kalman_trackers),
                    "has_active_target": int(active_target is not None),
                    "raw_center_x": None if raw_active_center is None else round(raw_active_center[0], 4),
                    "raw_center_y": None if raw_active_center is None else round(raw_active_center[1], 4),
                    "smooth_center_x": None if smooth_active_center is None else round(smooth_active_center[0], 4),
                    "smooth_center_y": None if smooth_active_center is None else round(smooth_active_center[1], 4),
                    "smooth_z": None if active_target is None else round(active_target[3], 4),
                    "command": active_command.strip() if active_command is not None else "BOX:-1:-1:-1:-1",
                    "gt_box_iou": None if gt_matrix_box is None else round(dark_box_ious[-1], 4),
                }
            )
    finally:
        cap.release()

    raw_jitter_mean = safe_mean(raw_jitter_px)
    smooth_jitter_mean = safe_mean(smooth_jitter_px)
    jitter_reduction_percent = 0.0
    if raw_jitter_mean > 1e-9:
        jitter_reduction_percent = ((raw_jitter_mean - smooth_jitter_mean) / raw_jitter_mean) * 100.0

    metrics: dict[str, object] = {
        "source": args.source,
        "ablation_mode": args.ablation_mode,
        "gt_coco_source": None if coco_annotation_path is None else str(coco_annotation_path),
        "gt_has_track_ids": gt_has_track_ids,
        "video_frames_seen": video_frame_index,
        "frames_processed": processed_frame_index,
        "effective_video_fps": effective_fps,
        "evaluated_fps": round(sampled_fps, 4),
        "avg_detection_time_ms": round(safe_mean(detection_times_ms), 4),
        "avg_fusion_time_ms": round(safe_mean(fusion_times_ms), 4),
        "avg_total_latency_ms": round(safe_mean(total_processing_times_ms), 4),
        "estimated_pipeline_fps": round(1000.0 / safe_mean(total_processing_times_ms), 4) if total_processing_times_ms else 0.0,
        "active_target_rate_percent": round((active_target_frames / processed_frame_index) * 100.0, 4) if processed_frame_index else 0.0,
        "predicted_only_frame_ratio_percent": round((predicted_only_frames / processed_frame_index) * 100.0, 4) if processed_frame_index else 0.0,
        "avg_raw_jitter_px": round(raw_jitter_mean, 4),
        "avg_smooth_jitter_px": round(smooth_jitter_mean, 4),
        "jitter_reduction_percent": round(jitter_reduction_percent, 4),
        "box_command_change_rate_percent": round((serial_box_changes / processed_frame_index) * 100.0, 4) if processed_frame_index else 0.0,
        "box_command_changes": serial_box_changes,
    }

    if gt_present_frames > 0:
        multi_target_idf1 = compute_multi_target_idf1(id_pair_counts, idf1_total_gt_detections, idf1_total_pred_detections)
        metrics.update(
            {
                "gt_frames": gt_present_frames,
                "rmse_x_px": round(rms(rmse_x_errors), 4),
                "rmse_y_px": round(rms(rmse_y_errors), 4),
                "rmse_xy_px": round(
                    math.sqrt((rms(rmse_x_errors) ** 2 + rms(rmse_y_errors) ** 2) / 2.0),
                    4,
                ),
                "rmse_z_m": round(rms(rmse_z_errors), 4) if rmse_z_errors else None,
                "avg_dark_box_iou": round(safe_mean(dark_box_ious), 4),
                "gssr_percent": round((sum(glare_success_flags) / gt_present_frames) * 100.0, 4),
                "idf1_percent": None if multi_target_idf1 is None else round(multi_target_idf1 * 100.0, 4),
                "idf1_mode": "multi_target",
                "idf1_status": "ok" if multi_target_idf1 is not None else "unavailable_missing_gt_track_ids",
                "missed_glare_rate_percent": round((gt_present_but_no_box / gt_present_frames) * 100.0, 4),
                "false_darkening_rate_percent": round((false_darkening_frames / gt_present_frames) * 100.0, 4),
                "dark_box_precision_percent": round((sum(glare_success_flags) / predicted_dark_frames) * 100.0, 4)
                if predicted_dark_frames
                else 0.0,
                "gssr_iou_threshold": args.gssr_iou_threshold,
            }
        )

    output_json_path = Path(args.output_json)
    output_csv_path = Path(args.output_csv)

    output_json_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    with output_csv_path.open("w", encoding="utf-8", newline="") as f:
        if per_frame_rows:
            writer = csv.DictWriter(f, fieldnames=list(per_frame_rows[0].keys()))
            writer.writeheader()
            writer.writerows(per_frame_rows)

    print(json.dumps(metrics, indent=2))
    print(f"Saved summary to: {output_json_path}")
    print(f"Saved per-frame metrics to: {output_csv_path}")


if __name__ == "__main__":
    main()
