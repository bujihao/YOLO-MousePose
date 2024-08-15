
import os
import json
from tqdm import tqdm

def labelme2coco(txt_path, labelme_json_base_folder, output_coco_json):
    # Create an empty COCO data format
    coco_format = {
        "images": [],
        "annotations": [],
        "categories": [{
            'id': 1,
            'name': 'mouse_rect',
            'supercategory': 'mouse_rect',
            'keypoints': ['0', '1', '2', '3', '4', '5', '6'],
            'skeleton': [[0, 1], [0, 5], [0, 6], [6, 3], [3, 4], [3, 2]]
        }]
    }

    image_id = 0  # Image ID
    annotation_id = 0  # annotation_id

    # Read each line in *.txt to get the json file path
    with open(txt_path, 'r') as f:
        lines = [line.strip() for line in f.readlines()]

    for line in tqdm(lines):
        image_path = line
        json_path = image_path.replace('images', 'labels').replace('.png', '.json')

        # Check if the json file exists
        if not os.path.exists(json_path):
            print(f" file {json_path} does not exist ")
            continue

        with open(json_path, 'r') as f:
            labelme_data = json.load(f)

        # Add picture information
        image_info = {
            "file_name": labelme_data["imagePath"],
            "height": labelme_data["imageHeight"],
            "width": labelme_data["imageWidth"],
            "id": image_id
        }
        coco_format["images"].append(image_info)

        # Initializes the keypoint list and bounding box list
        keypoints = []
        bbox = []

        # Initializes key tag and coordinate mapping dictionary
        bbox_keypoints_dict = {}

        # Iterate over the marked shape
        for shape in labelme_data["shapes"]:
            if shape["shape_type"] == "rectangle":
                x1 = min(shape['points'][0][0], shape['points'][1][0])
                y1 = min(shape['points'][0][1], shape['points'][1][1])
                x2 = max(shape['points'][0][0], shape['points'][1][0])
                y2 = max(shape['points'][0][1], shape['points'][1][1])
                # x1, y1 = shape["points"][0]
                # x2, y2 = shape["points"][1]
                width = x2 - x1
                height = y2 - y1
                bbox = [x1, y1, width, height]
            elif shape['shape_type'] == 'point':
                x, y = shape['points'][0]
                label = shape['label']
                bbox_keypoints_dict[label] = [x, y]

        #  Sort and populate key data according to a preset key list
        for keypoint_label in coco_format["categories"][0]['keypoints']:
            if keypoint_label in bbox_keypoints_dict:
                keypoints.extend(bbox_keypoints_dict[keypoint_label] + [2])
            else:
                keypoints.extend([0, 0, 0])  # For missing key points, fill [0, 0, 0]

        annotation = {
            "id": annotation_id,
            "image_id": image_id,
            "category_id": 1,
            "bbox": bbox,
            "area": width * height,
            "keypoints": keypoints,
            "num_keypoints": len(bbox_keypoints_dict),
            "iscrowd": 0,
            "segmentation": []
        }
        coco_format["annotations"].append(annotation)

        image_id += 1
        annotation_id += 1

    # Save as a json file in COCO format
    with open(output_coco_json, "w") as f:
        json.dump(coco_format, f, indent=4)

    print("Conversion finished!")

if __name__ == "__main__":
    txt_path = 'data/dataset/rval.txt'  # The path to *.txt
    labelme_json_base_folder = 'data/dataset'  # Labelme Basic directory for JSON files
    output_coco_json = "data/cocoformat/val/val_annotations_coco_format.json"  # Output COCO format JSON file path
    labelme2coco(txt_path, labelme_json_base_folder, output_coco_json)
#