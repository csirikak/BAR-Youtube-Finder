import cv2
import numpy as np
from pathlib import Path
from ultralytics import YOLO
import json
"""
YOLO-based UI Panel Detection for Beyond All Reason
====================================================

This script provides a complete pipeline for:
1. Creating a labeled dataset from your screenshots
2. Training a YOLO model to detect the UI panel
3. Running inference to get perfect bounding boxes

SETUP INSTRUCTIONS:
-------------------
1. Install ultralytics: pip install ultralytics
2. Run in LABELING mode first to create annotations
3. Train the model with your labeled data
4. Run in INFERENCE mode for production use

Usage Modes:
- LABELING: Manually label UI panels in screenshots
- TRAINING: Train YOLO model on labeled data
- INFERENCE: Detect UI panels automatically
"""

# ==================== CONFIGURATION ====================

SCREENSHOT_DIR = "training_screenshots"
LABELS_DIR = "yolo_labels"
DATASET_DIR = "yolo_dataset"
MODEL_PATH = "ui_detector.pt"  # Trained model path

# ===========================================================

class YOLOUIDetector:
    """
    Complete YOLO-based UI detection system with labeling, training, and inference.
    """
    
    def __init__(self, screenshot_dir, labels_dir):
        self.screenshot_dir = Path(screenshot_dir)
        self.labels_dir = Path(labels_dir)
        self.labels_dir.mkdir(exist_ok=True)
        
        self.images = list(self.screenshot_dir.glob("**/*.png"))
        if not self.images:
            print(f"ERROR: No PNG files found in {screenshot_dir}")
            return
        
        print(f"Found {len(self.images)} screenshots")
        self.current_idx = 0
        
        # For manual labeling
        # self.drawing is no longer needed
        self.start_point = None
        self.current_box = None
        
    def mouse_callback(self, event, x, y, flags, param):
        """
        Handle mouse events for manual labeling (Top-Left Click Only).
        The bottom-right corner of the bounding box is fixed to the bottom-right
        of the image.
        """
        # img_zoomed, zoom_scale, img_width, img_height, crop_start_x, crop_start_y are passed in param
        display_img, zoom_scale, img_width, img_height, crop_start_x, crop_start_y = param
        
        # Adjust for scale
        actual_x_zoomed = int(x / zoom_scale)
        actual_y_zoomed = int(y / zoom_scale)
        
        if event == cv2.EVENT_LBUTTONUP:
            # On mouse click release, set the top-left point
            self.start_point = (actual_x_zoomed, actual_y_zoomed)
            
            # The bottom-right point is fixed to the bottom-right of the current zoomed/cropped view.
            # The current_box is defined in zoomed/cropped coordinates.
            crop_H, crop_W = display_img.shape[:2]
            
            # Since the display_img is the zoomed view, its bottom-right is (crop_W, crop_H)
            # We must use the coordinates *before* scaling for the current_box, which is scaled in the loop.
            # But the start_point is in zoomed coordinates (x,y), so the end point must also be in zoomed coordinates.
            x1_zoom, y1_zoom = self.start_point
            x2_zoom, y2_zoom = display_img.shape[1], display_img.shape[0] # Bottom-Right of zoomed image
            
            # Ensure x1 < x2 and y1 < y2 for proper box definition
            x1_zoom = min(x1_zoom, x2_zoom)
            y1_zoom = min(y1_zoom, y2_zoom)

            self.current_box = (x1_zoom, y1_zoom, x2_zoom, y2_zoom)

        # Remove the rest of the original mouse logic (drawing=True, mousemove, LBUTTONDOWN)
        # as it is no longer needed for a single-click label.
    
    # save_yolo_label remains unchanged
    def save_yolo_label(self, img_path, box, img_width, img_height):
        """
        Save bounding box in YOLO format:
        <class_id> <x_center> <y_center> <width> <height>
        All values normalized to [0, 1]
        """
        x1, y1, x2, y2 = box
        
        # Ensure correct order
        x1, x2 = min(x1, x2), max(x1, x2)
        y1, y2 = min(y1, y2), max(y1, y2)
        
        # Convert to YOLO format (normalized center + size)
        x_center = ((x1 + x2) / 2) / img_width
        y_center = ((y1 + y2) / 2) / img_height
        width = (x2 - x1) / img_width
        height = (y2 - y1) / img_height
        
        # Class 0 = UI panel
        label_line = f"0 {x_center:.6f} {y_center:.6f} {width:.6f} {height:.6f}\n"
        
        # Save label file with same name as image
        label_path = self.labels_dir / (img_path.stem + ".txt")
        with open(label_path, 'w') as f:
            f.write(label_line)
        
        print(f"  ✓ Saved label: {label_path.name}")
        print(f"    Box: ({x1}, {y1}) to ({x2}, {y2})")
        print(f"    YOLO: class=0 x={x_center:.3f} y={y_center:.3f} w={width:.3f} h={height:.3f}")

    # load_yolo_label remains unchanged
    def load_yolo_label(self, img_path, img_width, img_height):
        """Load existing YOLO label if it exists"""
        label_path = self.labels_dir / (img_path.stem + ".txt")
        
        if not label_path.exists():
            return None
        
        with open(label_path, 'r') as f:
            line = f.readline().strip()
        
        if not line:
            return None
        
        parts = line.split()
        class_id, x_center, y_center, width, height = map(float, parts)
        
        # Convert back to pixel coordinates
        x1 = int((x_center - width/2) * img_width)
        y1 = int((y_center - height/2) * img_height)
        x2 = int((x_center + width/2) * img_width)
        y2 = int((y_center + height/2) * img_height)
        
        return (x1, y1, x2, y2)
    
    def labeling_mode(self):
        """
        Interactive labeling tool.
        Draw boxes around UI panels to create training data.
        """
        print("\n" + "="*70)
        print("LABELING MODE - Create Training Data")
        print("="*70)
        print("\nInstructions:")
        print("  1. **CLICK** the **TOP-LEFT** boundary of the UI panel")
        print("  2. The bottom-right corner is automatically set")
        print("  3. Press SPACE to save the label")
        print("  4. Press 's' to SKIP (no UI panel in this image)")
        print("  5. Press 'n' for next image, 'p' for previous")
        print("  6. Press 'c' to clear current box")
        print("  7. Press ESC to quit")
        print("\nTip: Image is scaled 1.5x and cropped to bottom-right for precision")
        print("="*70)
        
        window_name = "YOLO Labeling Tool (1.5x Zoom)"
        cv2.namedWindow(window_name)
        
        while True:
            img_path = self.images[self.current_idx]
            img = cv2.imread(str(img_path))
            
            if img is None:
                print(f"ERROR: Could not load {img_path}")
                self.current_idx = (self.current_idx + 1) % len(self.images)
                continue
            
            (H, W) = img.shape[:2]
            
            crop_start_x = int(W * 0.75)  # Start at 75% across (last 25%)
            crop_start_y = int(H * 0.25)  # Start at 25% down (last 75%)
            
            # Crop to bottom-right region
            img_cropped = img[crop_start_y:H, crop_start_x:W]
            (crop_H, crop_W) = img_cropped.shape[:2]
            

            zoom_scale = 1080/H
            img_zoomed = cv2.resize(img_cropped, None, fx=zoom_scale, fy=zoom_scale, 
                                     interpolation=cv2.INTER_CUBIC)
            
            # Load existing label if available
            existing_box = self.load_yolo_label(img_path, W, H)
            
            # Adjust existing box to cropped/zoomed coordinates
            if existing_box and self.current_box is None:
                x1, y1, x2, y2 = existing_box
                
                # Convert to cropped coordinates
                x1_crop = x1 - crop_start_x
                y1_crop = y1 - crop_start_y
                x2_crop = x2 - crop_start_x
                y2_crop = y2 - crop_start_y
                
                # Scale to zoomed coordinates
                x1_zoom = int(x1_crop * zoom_scale)
                y1_zoom = int(y1_crop * zoom_scale)
                x2_zoom = int(x2_crop * zoom_scale)
                y2_zoom = int(y2_crop * zoom_scale)
                
                self.current_box = (x1_zoom, y1_zoom, x2_zoom, y2_zoom)
                print(f"\n✓ Loaded existing label for {img_path.name}")
            
            display_img = img_zoomed.copy()
            
            # Draw existing/current box
            if self.current_box:
                x1, y1, x2, y2 = self.current_box
                cv2.rectangle(display_img, (x1, y1), (x2, y2), (0, 255, 0), 3)
                
                # Add info text
                cv2.putText(display_img, "Press SPACE to save", (10, 40),
                           cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 2)
            else:
                cv2.putText(display_img, "Click TOP-LEFT of UI panel", (10, 40),
                           cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2)
            
            # Add skip info
            cv2.putText(display_img, "Press 'S' to SKIP (no UI)", (10, 80),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 100, 100), 2)
            
            # Add image info
            labeled_count = len(list(self.labels_dir.glob("*.txt")))
            skipped_marker = self.labels_dir / (img_path.stem + ".skip")
            is_skipped = skipped_marker.exists()
            
            status = "[UNLABELED]"
            if is_skipped:
                status = "[SKIPPED]"
            elif existing_box or self.current_box: # Check if loaded or newly drawn
                status = "[LABELED]"
            
            info_text = f"Image {self.current_idx + 1}/{len(self.images)} | Labeled: {labeled_count} | {status}"
            cv2.putText(display_img, info_text, (10, img_zoomed.shape[0] - 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
            
            cv2.putText(display_img, "1.5x ZOOM - Bottom-Right 25% x 45%", (10, img_zoomed.shape[0] - 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
            
            # Set mouse callback with all required parameters
            cv2.setMouseCallback(window_name, self.mouse_callback, 
                                 param=(display_img, 1.0, W, H, crop_start_x, crop_start_y))
            cv2.imshow(window_name, display_img)
            cv2.imshow("debug", cv2.resize(img, None, fx=0.25, fy=0.25, 
                                    interpolation=cv2.INTER_CUBIC))
            
            key = cv2.waitKey(1) & 0xFF
            
            if key == 27:  # ESC
                break
                
            elif key == ord(' '):  # SPACE - Save label
                if self.current_box:
                    # Convert back from zoomed coordinates to original image coordinates
                    x1_zoom, y1_zoom, x2_zoom, y2_zoom = self.current_box
                    
                    # Unscale from zoom
                    x1_crop = int(x1_zoom / zoom_scale)
                    y1_crop = int(y1_zoom / zoom_scale)
                    # Note: x2_zoom/y2_zoom are the bottom-right of the zoomed image (crop_W*1.5, crop_H*1.5).
                    # Unscaling them gives the bottom-right of the cropped image (crop_W, crop_H).
                    x2_crop = int(x2_zoom / zoom_scale)
                    y2_crop = int(y2_zoom / zoom_scale)
                    
                    # Convert back to original image coordinates
                    x1_orig = x1_crop + crop_start_x
                    y1_orig = y1_crop + crop_start_y
                    # The bottom-right of the cropped image is the bottom-right of the original image
                    x2_orig = x2_crop + crop_start_x # This is effectively W
                    y2_orig = y2_crop + crop_start_y # This is effectively H
                    
                    original_box = (x1_orig, y1_orig, x2_orig, y2_orig)
                    
                    self.save_yolo_label(img_path, original_box, W, H)
                    
                    # Remove skip marker if it exists
                    if skipped_marker.exists():
                        skipped_marker.unlink()
                    
                    # Move to next image
                    self.current_idx = (self.current_idx + 1) % len(self.images)
                    self.current_box = None
                    self.start_point = None
                else:
                    print("  ⚠️  No box set. Click the top-left corner or press 'S' to skip.")
                    
            elif key == ord('s') or key == ord('S'):  # SKIP - No UI panel in this image
                # Create a skip marker file
                skipped_marker = self.labels_dir / (img_path.stem + ".skip")
                skipped_marker.touch()
                
                # Remove label if it exists
                label_path = self.labels_dir / (img_path.stem + ".txt")
                if label_path.exists():
                    label_path.unlink()
                
                print(f"  ⏭️  Skipped {img_path.name} (no UI panel)")
                
                # Move to next image
                self.current_idx = (self.current_idx + 1) % len(self.images)
                self.current_box = None
                self.start_point = None
                
            elif key == ord('n'):  # Next image
                self.current_idx = (self.current_idx + 1) % len(self.images)
                self.current_box = None
                self.start_point = None
                
            elif key == ord('p'):  # Previous image
                self.current_idx = (self.current_idx - 1) % len(self.images)
                self.current_box = None
                self.start_point = None
                
            elif key == ord('c'):  # Clear current box
                self.current_box = None
                self.start_point = None
                print("  Box cleared")
        
        cv2.destroyAllWindows()
        
        labeled_count = len(list(self.labels_dir.glob("*.txt")))
        skipped_count = len(list(self.labels_dir.glob("*.skip")))
        print(f"\n✓ Labeling complete:")
        print(f"  - {labeled_count} images labeled")
        print(f"  - {skipped_count} images skipped (no UI)")
        print(f"  - Labels saved to: {self.labels_dir}")


def prepare_yolo_dataset(screenshot_dir, labels_dir, dataset_dir, train_split=0.8):
    """
    Prepare YOLO dataset structure:
    dataset/
      ├── images/
      │   ├── train/
      │   └── val/
      ├── labels/
      │   ├── train/
      │   └── val/
      └── data.yaml
    """
    dataset_path = Path(dataset_dir)
    
    # Create directory structure
    (dataset_path / "images" / "train").mkdir(parents=True, exist_ok=True)
    (dataset_path / "images" / "val").mkdir(parents=True, exist_ok=True)
    (dataset_path / "labels" / "train").mkdir(parents=True, exist_ok=True)
    (dataset_path / "labels" / "val").mkdir(parents=True, exist_ok=True)
    
    # Get all labeled images
    labels = list(Path(labels_dir).glob("*.txt"))
    images = []
    
    for label_file in labels:
        img_path = Path(screenshot_dir) / f"{label_file.stem}.png"
        if img_path.exists():
            images.append((img_path, label_file))
    
    print(f"\nFound {len(images)} labeled images")
    
    if len(images) == 0:
        print("ERROR: No labeled images found! Run labeling mode first.")
        return None
    
    # Shuffle and split
    import random
    random.shuffle(images)
    split_idx = int(len(images) * train_split)
    train_set = images[:split_idx]
    val_set = images[split_idx:]
    
    print(f"Train set: {len(train_set)} images")
    print(f"Val set: {len(val_set)} images")
    
    # Copy files
    import shutil
    
    for img_path, label_path in train_set:
        shutil.copy(img_path, dataset_path / "images" / "train" / img_path.name)
        shutil.copy(label_path, dataset_path / "labels" / "train" / label_path.name)
    
    for img_path, label_path in val_set:
        shutil.copy(img_path, dataset_path / "images" / "val" / img_path.name)
        shutil.copy(label_path, dataset_path / "labels" / "val" / label_path.name)
    
    # Create data.yaml
    yaml_content = f"""# Beyond All Reason UI Panel Detection Dataset
path: {dataset_path.absolute()}
train: images/train
val: images/val

# Classes
names:
  0: ui_panel

# Number of classes
nc: 1
"""
    
    yaml_path = dataset_path / "data.yaml"
    with open(yaml_path, 'w') as f:
        f.write(yaml_content)
    
    print(f"\n✓ Dataset prepared at: {dataset_path}")
    print(f"✓ Config saved to: {yaml_path}")
    
    return yaml_path


def train_yolo_model(data_yaml, epochs=50, img_size=640):
    """
    Train a YOLO model on the prepared dataset.
    """
    print("\n" + "="*70)
    print("TRAINING YOLO MODEL")
    print("="*70)
    
    # Load a pre-trained YOLO model (YOLOv11 nano for speed)
    model = YOLO('yolov11n.pt')  # Use nano model as base
    
    print(f"\nTraining parameters:")
    print(f"  Data config: {data_yaml}")
    print(f"  Epochs: {epochs}")
    print(f"  Image size: {img_size}")
    print(f"  Model: YOLOv11n (nano)")
    print("\nStarting training...")
    
    # Train the model
    results = model.train(
        data=str(data_yaml),
        epochs=epochs,
        imgsz=img_size,
        batch=8,
        name='ui_panel_detector',
        patience=10,  # Early stopping
        save=True,
        plots=True
    )
    
    print("\n✓ Training complete!")
    print(f"✓ Model saved to: runs/detect/ui_panel_detector/weights/best.pt")
    
    return model


def run_inference(model_path, screenshot_dir, output_dir="detected_ui"):
    """
    Run inference on all screenshots and save results.
    """
    import os
    print("\n" + "="*70)
    print("RUNNING INFERENCE")
    print("="*70)
    
    model = YOLO(model_path)
    output_path = Path(output_dir)
    output_path.mkdir(exist_ok=True)
    
    screenshots = list(Path(screenshot_dir).glob("**/*.png"))
    print(f"\nProcessing {len(screenshots)} screenshots...")
    
    results_json = {}
    
    for img_path in screenshots:
        img = cv2.imread(str(img_path))
        if img is None:
            continue
        
        # Run inference
        results = model(img, verbose=False)
        
        # Get bounding boxes
        boxes = results[0].boxes
        
        if len(boxes) > 0:
            # Get the box with highest confidence
            best_box = boxes[0]
            x1, y1, x2, y2 = map(int, best_box.xyxy[0].tolist())
            confidence = float(best_box.conf[0])
            
            print(f"✓ {img_path.name}: UI panel at ({x1}, {y1}, {x2}, {y2}) - conf: {confidence:.3f}")
            
            # Save result
            results_json[img_path.name] = {
                "box": [x1, y1, x2, y2],
                "confidence": confidence
            }
            
            # Draw and save visualization
            display = img.copy()
            cv2.rectangle(display, (x1, y1), (x2, y2), (0, 255, 0), 3)
            cv2.putText(display, f"UI Panel {confidence:.2f}", (x1, y1-10),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            
            out_path = output_path / img_path.name
            cv2.imwrite(str(out_path), display)
        else:
            print(f"✗ {img_path.name}: No UI panel detected")
            results_json[img_path.name] = None
    
    # Save JSON results
    json_path = output_path / "detections.json"
    with open(json_path, 'w') as f:
        json.dump(results_json, f, indent=2)
    
    print(f"\n✓ Results saved to: {output_path}")
    print(f"✓ JSON data saved to: {json_path}")


def main():
    import sys
    
    print("="*70)
    print("YOLO UI Panel Detection System")
    print("="*70)
    
    if len(sys.argv) < 2:
        print("\nUsage:")
        print("  python yolo_ui_detector.py label      - Label training data")
        print("  python yolo_ui_detector.py train      - Train YOLO model")
        print("  python yolo_ui_detector.py infer      - Run inference on screenshots")
        print("  python yolo_ui_detector.py pipeline   - Complete pipeline (label + train + infer)")
        return
    
    mode = sys.argv[1].lower()
    
    if mode == "label":
        detector = YOLOUIDetector(SCREENSHOT_DIR, LABELS_DIR)
        detector.labeling_mode()
        
    elif mode == "train":
        yaml_path = prepare_yolo_dataset(SCREENSHOT_DIR, LABELS_DIR, DATASET_DIR)
        if yaml_path:
            train_yolo_model(yaml_path, epochs=100)
        
    elif mode == "infer":
        if not Path(MODEL_PATH).exists():
            # Try to find trained model
            trained_model = Path("runs/detect/ui_panel_detector/weights/best.pt")
            if trained_model.exists():
                MODEL_PATH_ACTUAL = str(trained_model)
            else:
                print(f"ERROR: Model not found at {MODEL_PATH}")
                print("Run 'train' mode first to create a model")
                return
        else:
            MODEL_PATH_ACTUAL = MODEL_PATH
        
        run_inference(MODEL_PATH_ACTUAL, SCREENSHOT_DIR)
        
    elif mode == "pipeline":
        print("\n🚀 Running complete pipeline...\n")
        
        # Step 1: Label
        print("STEP 1: Labeling")
        detector = YOLOUIDetector(SCREENSHOT_DIR, LABELS_DIR)
        detector.labeling_mode()
        
        # Step 2: Train
        print("\nSTEP 2: Training")
        yaml_path = prepare_yolo_dataset(SCREENSHOT_DIR, LABELS_DIR, DATASET_DIR)
        if yaml_path:
            train_yolo_model(yaml_path, epochs=100)
        else:
            return
        
        # Step 3: Infer
        print("\nSTEP 3: Inference")
        trained_model = Path("runs/detect/ui_panel_detector/weights/best.pt")
        if trained_model.exists():
            run_inference(str(trained_model), SCREENSHOT_DIR)
        
        print("\n✓ Complete pipeline finished!")
    
    else:
        print(f"ERROR: Unknown mode '{mode}'")
        print("Valid modes: label, train, infer, pipeline")


if __name__ == "__main__":
    main()