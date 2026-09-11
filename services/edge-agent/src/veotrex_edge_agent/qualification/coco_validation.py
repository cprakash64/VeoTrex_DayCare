from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from veotrex_edge_agent.gpu_worker import GpuWorkerSupervisor
from veotrex_edge_agent.gpu_worker.decoder import (
    Candidate,
    DetectionConfig,
    non_maximum_suppression,
)
from veotrex_edge_agent.image_pipeline import (
    PixelFormat,
    add_source_coordinates,
    inverse_box,
    preprocess_image,
)
from veotrex_edge_agent.qualification.image_decoder import decode_image
from veotrex_edge_agent.qualification.person_evaluator import (
    GroundTruth,
    Prediction,
    coco_bbox_to_xyxy,
    density_bucket,
    match_persons,
    size_bucket,
)

THRESHOLDS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
NMS_THRESHOLDS = (0.45, 0.50, 0.60)


def percentile(values: list[float], quantile: float) -> float:
    return sorted(values)[min(len(values) - 1, int(len(values) * quantile))]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "mean": statistics.mean(values),
        "p50": percentile(values, 0.5),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
    }


def aggregate(
    records: list[dict[str, Any]], score: float, nms: float, iou: float
) -> dict[str, Any]:
    tp = fp = fn = ignored = 0
    size_total: dict[str, int] = defaultdict(int)
    size_matched: dict[str, int] = defaultdict(int)
    density: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for record in records:
        candidates = [Candidate(*item) for item in record["candidates"] if item[4] >= score]
        chosen = non_maximum_suppression(
            candidates, DetectionConfig(candidate_score_threshold=score, nms_iou_threshold=nms)
        )
        predictions = []
        for item in chosen:
            source = inverse_box((item.x1, item.y1, item.x2, item.y2), record["transform_object"])
            if source is not None:
                predictions.append(Prediction(source, item.score))
        truths = record["truth_objects"]
        result = match_persons(predictions, truths, iou_threshold=iou)
        tp += result.true_positives
        fp += result.false_positives
        fn += result.false_negatives
        ignored += result.ignored_predictions
        for index, truth in enumerate(truths):
            if truth.iscrowd:
                continue
            bucket = size_bucket(truth.area)
            size_total[bucket] += 1
            if index in result.matched_ground_truth_indices:
                size_matched[bucket] += 1
        count = sum(not truth.iscrowd for truth in truths)
        bucket = density_bucket(count)
        values = density[bucket]
        values[0] += 1
        values[1] += result.true_positives
        values[2] += result.false_positives
        values[3] += result.false_negatives
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    return {
        "threshold": score,
        "nms": nms,
        "iou": iou,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "ignored": ignored,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
        "fp_per_image": fp / len(records),
        "fn_per_image": fn / len(records),
        "detections_per_image": (tp + fp + ignored) / len(records),
        "size": {
            key: {
                "gt": size_total[key],
                "matched": size_matched[key],
                "recall": size_matched[key] / size_total[key] if size_total[key] else 0,
            }
            for key in ("small", "medium", "large")
        },
        "density": {
            key: {
                "images": v[0],
                "tp": v[1],
                "fp": v[2],
                "fn": v[3],
                "precision": v[1] / (v[1] + v[2]) if v[1] + v[2] else 0,
                "recall": v[1] / (v[1] + v[3]) if v[1] + v[3] else 0,
                "fp_per_image": v[2] / v[0],
                "fn_per_image": v[3] / v[0],
            }
            for key, v in sorted(density.items())
        },
    }


def draw_gallery(records: list[dict[str, Any]], output: Path, score: float, title: str) -> None:
    output.mkdir(parents=True, exist_ok=True)
    thumbs = []
    for record in records[:10]:
        image = Image.open(record["path"]).convert("RGB")
        draw = ImageDraw.Draw(image)
        for truth in record["truth_objects"]:
            if not truth.iscrowd:
                draw.rectangle(truth.box, outline=(0, 255, 0), width=3)
        candidates = [Candidate(*item) for item in record["candidates"] if item[4] >= score]
        for item in non_maximum_suppression(
            candidates, DetectionConfig(candidate_score_threshold=score)
        ):
            box = inverse_box((item.x1, item.y1, item.x2, item.y2), record["transform_object"])
            if box:
                draw.rectangle(box, outline=(255, 0, 0), width=3)
                draw.text((box[0], box[1]), f"{item.score:.2f}", fill=(255, 0, 0))
        image.thumbnail((320, 240))
        thumbs.append((record["image_id"], image.copy()))
    sheet = Image.new("RGB", (640, ((len(thumbs) + 1) // 2) * 270), (30, 30, 30))
    painter = ImageDraw.Draw(sheet)
    painter.text((5, 5), title, fill="white")
    for index, (image_id, image) in enumerate(thumbs):
        x = (index % 2) * 320
        y = (index // 2) * 270 + 25
        sheet.paste(image, (x, y))
        painter.text((x + 5, y + 242), f"image {image_id}", fill="white")
    sheet.save(output / "contact-sheet.jpg", quality=90)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    annotation_path = args.data / "annotations" / "instances_val2017.json"
    annotations = json.loads(annotation_path.read_text())
    people = defaultdict(list)
    for item in annotations["annotations"]:
        if item["category_id"] == 1:
            people[item["image_id"]].append(
                GroundTruth(
                    coco_bbox_to_xyxy(item["bbox"]), float(item["area"]), bool(item["iscrowd"])
                )
            )
    records = []
    timings = defaultdict(list)
    supervisor = GpuWorkerSupervisor()
    supervisor.start()
    supervisor.load_model()
    try:
        image_metadata = sorted(annotations["images"], key=lambda x: x["id"])
        if args.limit is not None:
            if args.limit <= 0:
                parser.error("--limit must be positive")
            image_metadata = image_metadata[: args.limit]
        for number, metadata in enumerate(image_metadata):
            started = time.perf_counter_ns()
            path = args.data / "val2017" / metadata["file_name"]
            stage = time.perf_counter_ns()
            decoded = decode_image(path)
            timings["decode_ms"].append((time.perf_counter_ns() - stage) / 1e6)
            stage = time.perf_counter_ns()
            prepared = preprocess_image(decoded.rgb, pixel_format=PixelFormat.RGB8)
            timings["preprocess_ms"].append((time.perf_counter_ns() - stage) / 1e6)
            timings["color_conversion_ms"].append(prepared.color_conversion_ms)
            timings["resize_ms"].append(prepared.resize_ms)
            timings["tensor_construction_ms"].append(prepared.tensor_construction_ms)
            stage = time.perf_counter_ns()
            result = supervisor.infer_tensor(
                prepared.tensor.tobytes(),
                frame_id=str(metadata["id"]),
                candidate_score_threshold=0.05,
                qualification_candidates=True,
            )
            timings["rpc_ms"].append((time.perf_counter_ns() - stage) / 1e6)
            stage = time.perf_counter_ns()
            add_source_coordinates(result["detections"], prepared.transform)
            timings["source_map_ms"].append((time.perf_counter_ns() - stage) / 1e6)
            timings["total_ms"].append((time.perf_counter_ns() - started) / 1e6)
            for key, value in result["timing"].items():
                timings[key].append(value)
            for key, value in result["parent_timing"].items():
                timings[key].append(value)
            records.append(
                {
                    "image_id": metadata["id"],
                    "path": str(path),
                    "width": metadata["width"],
                    "height": metadata["height"],
                    "candidates": result["qualification_candidates"],
                    "truth_objects": people[metadata["id"]],
                    "transform_object": prepared.transform,
                }
            )
            if (number + 1) % 100 == 0:
                print(f"processed={number + 1}", flush=True)
    finally:
        supervisor.stop()
    iou50_sweep = [aggregate(records, value, 0.45, 0.50) for value in THRESHOLDS]
    iou75_sweep = [aggregate(records, value, 0.45, 0.75) for value in THRESHOLDS]
    nms_sensitivity = [aggregate(records, 0.10, value, 0.50) for value in NMS_THRESHOLDS]
    metrics: dict[str, Any] = {
        "iou50_sweep": iou50_sweep,
        "iou75_sweep": iou75_sweep,
        "nms_sensitivity": nms_sensitivity,
        "timings": {key: summarize(value) for key, value in timings.items()},
        "images": len(records),
        "person_annotations": sum(len(x) for x in people.values()),
    }
    balanced = max(iou50_sweep, key=lambda item: float(item["f1"]))
    high = max(
        iou50_sweep,
        key=lambda item: (float(item["recall"]), float(item["precision"])),
    )
    metrics["balanced_candidate"] = balanced["threshold"]
    metrics["high_recall_candidate"] = high["threshold"]

    def rank(record: dict[str, Any], kind: str) -> float:
        result = aggregate([record], balanced["threshold"], 0.45, 0.50)
        if kind == "fn":
            return float(result["fn"])
        if kind == "fp":
            return float(result["fp"])
        if kind == "small":
            return float(sum(t.area < 1024 and not t.iscrowd for t in record["truth_objects"]))
        return float(sum(not t.iscrowd for t in record["truth_objects"]))

    gallery = args.output / "galleries"
    for kind, title in (
        ("fn", "high-false-negative"),
        ("fp", "high-false-positive"),
        ("small", "small-distant"),
        ("dense", "dense-occluded"),
    ):
        draw_gallery(
            sorted(records, key=lambda r: (-rank(r, kind), r["image_id"])),
            gallery / title,
            balanced["threshold"],
            title,
        )
    successful = [
        record
        for record in records
        if record["truth_objects"]
        and rank(record, "fn") == 0
        and rank(record, "fp") == 0
    ]

    def select(predicate: Any, chosen: list[dict[str, Any]]) -> None:
        match = next(
            (record for record in successful if predicate(record) and record not in chosen), None
        )
        if match is not None:
            chosen.append(match)

    successes: list[dict[str, Any]] = []
    select(lambda r: len(r["truth_objects"]) == 1, successes)
    select(lambda r: len(r["truth_objects"]) > 1, successes)
    select(lambda r: r["height"] > r["width"], successes)
    select(lambda r: r["width"] >= r["height"], successes)
    select(lambda r: any(t.area < 1024 for t in r["truth_objects"]), successes)
    select(lambda r: any(t.area >= 9216 for t in r["truth_objects"]), successes)
    select(
        lambda r: any(
            t.box[0] <= 1
            or t.box[1] <= 1
            or t.box[2] >= r["width"] - 1
            or t.box[3] >= r["height"] - 1
            for t in r["truth_objects"]
        ),
        successes,
    )
    successes.extend(record for record in successful if record not in successes)
    draw_gallery(
        successes,
        gallery / "representative-success",
        balanced["threshold"],
        "representative-success",
    )
    serializable = json.loads(json.dumps(metrics))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "metrics.json").write_text(json.dumps(serializable, indent=2, sort_keys=True))
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
