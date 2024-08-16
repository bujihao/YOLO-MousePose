# YOLO-MousePose: Mouse Pose estimation model

## Introduction
English | [简体中文](/README_zh-CN.md)
This repository is the official implementation of the paper "**YOLO-MousePose: Improved YOLO-Pose forMouse Pose Estimation from a Top-down View**".YOLO-MousePose is an open-source deep learning model for mouse pose estimation based on PyTorch. It serves as a successful example of transplanting [YOLO-Pose](https://github.com/TexasInstruments/edgeai-yolov5/tree/yolo-pose) into the domain of mouse pose estimation. By incorporating the FCSE (Fusion Channel Specialized Encoder) module into the neck architecture and refining the loss function, YOLO-MousePose effectively addresses the challenges of slow training convergence and low final accuracy typically encountered during the transplantation process.

<p align="center">
<img src="utils/figures/overview.png" width="100%">
</p>



## Demo

<p align="center">
<img src="1-1.gif" width="40%">           <img src="1-2.gif" width="40%">
</p>

## Result

<p align="center">
<img src="utils/figures/result1.png" width="80%">           <img src="utils/figures/result2.png" width="80%">
</p>

## Requirements

* PyTorch>=1.7.0
* torchvision>=0.8.1
* numpy>=1.18.5
* opencv-python>=4.1.2
* PyYAML>=5.3.1
* matplotlib>=3.2.2
* tqdm>=4.41.0
* pycocotools>=2.0

## Data Preparation
The dataset needs to be prepared in YOLO format so that the dataloader can be enhanced to read the keypoints along with the bounding box informations. labelme2yolo_keypoints.py in this repository can convert the labelme annotated json file into a dataset in the required format.And our self-built dataset will be open sourced after the paper is reviewed.

Expected directoys structure:

```
YOLO-MousePose
│   README.md
│   ...   
│
data
│  dataset
│     images
│     └─────labeled-data
│           └───
│           └───
|           └───
|            '
|            .
|     labels
│     └─────labeled-data
│           └───
│           └───
|           └───
|            '
|            .
|      labelme2yolo_keypoints.py
|      train.txt
|      val.txt

```
## **Training: YOLO-MousePose**
Train a suitable model  by running the following command using a suitable pretrained ckpt from the previous section.

```
python train.py --data mouse_kpts.yaml --cfg YOLO-MousePose-T.yaml --weights 'path to the pre-trained ckpts' --adamW --batch-size 16 --img 640 --kpt-label --cache-images
                                       --cfg YOLO-MousePose-S.yaml 
                                       --cfg YOLO-MousePose-M.yaml
                                       --cfg YOLO-MousePose-L.yaml 
```






