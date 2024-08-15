
import os
import json
from tqdm import tqdm
import glob
import numpy as np

# Category of box
bbox_class = {
    'mouse_rect': 0
}

classes = ["mouse"]
# Category of keypoints
keypoint_class = ['0', '1', '2', '3', '4', '5', '6']
json_path_list = glob.glob(r"data/dataset/labels/labeled-data/*/*.json")
for labelme_path in tqdm(json_path_list):
    with open(labelme_path, 'r', encoding='utf-8') as f:
        labelme = json.load(f)
    img_width = labelme['imageWidth']  # imageWidth
    img_height = labelme['imageHeight']  # imageHeight

    # txt file in YOLO format
    suffix = labelme_path.split('.')[-2]
    yolo_txt_path = suffix + '.txt'
    with open(yolo_txt_path, 'w', encoding='utf-8') as f:
        for each_ann in labelme['shapes']:  # Iterate through each annotation

            if each_ann['shape_type'] == 'rectangle' and each_ann['label'] == 'mouse_rect':  # 如果遇到矩形框

                yolo_str = ''

                ## Box information
                # Category ID of an box
                bbox_class_id = bbox_class[each_ann['label']]
                yolo_str += '{} '.format(bbox_class_id)
                # The XY pixel coordinates of the top left and bottom right corner of the rectangle box
                x1 = min(each_ann['points'][0][0], each_ann['points'][1][0])
                y1 = min(each_ann['points'][0][1], each_ann['points'][1][1])
                x2 = max(each_ann['points'][0][0], each_ann['points'][1][0])
                y2 = max(each_ann['points'][0][1], each_ann['points'][1][1])
                # The XY pixel coordinates of the center point of the box
                bbox_center_x = float((x1 + x2) / 2)
                bbox_center_y = float((y1 + y2) / 2)
                # BboxWidth
                bbox_width = x2 - x1
                # BboxHeight
                bbox_height = y2 - y1
                # The normalized coordinates of the center point of the box
                bbox_center_x_norm = bbox_center_x / img_width
                bbox_center_y_norm = bbox_center_y / img_height
                # Bbox normalized width
                bbox_width_norm = bbox_width / img_width
                # Bbox normalized Height
                bbox_height_norm = bbox_height / img_height

                yolo_str += '{:.16f} {:.16f} {:.16f} {:.16f} '.format(bbox_center_x_norm, bbox_center_y_norm,
                                                                      bbox_width_norm, bbox_height_norm)

                ## Find all keypoints in this box, stored in dictionary bbox_keypoints_dict
                bbox_keypoints_dict = {}
                for each_ann_keypoint in labelme['shapes']:  # Iterate over all annotations
                    if each_ann_keypoint['shape_type'] == 'point':  # Filter keypoints annotation
                        # Keypoints XY coordinates, categories
                        x_keypoint = float(each_ann_keypoint['points'][0][0])
                        y_keypoint = float(each_ann_keypoint['points'][0][1])
                        label_keypoint = each_ann_keypoint['label']
                        bbox_keypoints_dict[label_keypoint] = [x_keypoint, y_keypoint]

                ## Put the keypoints in order
                for each_class in keypoint_class:  # Iterate over all type of keypoints
                    if each_class in bbox_keypoints_dict:
                        keypoint_x_norm = bbox_keypoints_dict[each_class][0] / img_width
                        keypoint_y_norm = bbox_keypoints_dict[each_class][1] / img_height
                        yolo_str += '{:.16f} {:.16f} {} '.format(keypoint_x_norm, keypoint_y_norm,
                                                                 2)  # 2- Visible no occlusion 1- Occlusion 0- No dots
                    else:
                        yolo_str += '0 0 0 '.format(0, 0, 0)  # All points that don't exist are zero

                # txt file
                f.write(yolo_str + '\n')
