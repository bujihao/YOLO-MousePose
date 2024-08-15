# YOLO-MousePose: Mouse Pose estimation model
This repository is the official implementation of the paper["**YOLO-MousePose: Improved YOLO-Pose forMouse Pose Estimation from a Top-down View**"], a This repository contains YOLOv5 based models for human pose estimation.


<p align="center">
<img src="pipeline.png" width="80%">
</p>

## Demo

<p align="center">
<img src="1-1.gif" width="40%">           <img src="1-2.gif" width="40%">
</p>

## Requirements
Tested with:
* PyTorch 1.4.0

* Torchvision 0.5.0

* Python 3.6.8

## Data Preparation
The dataset needs to be prepared in YOLO format so that the dataloader can be enhanced to read the keypoints along with the bounding box informations. This [repository](https://github.com/ultralytics/JSON2YOLO) was used with required changes to generate the dataset in the required format. 
Please download the processed labels from [here](https://drive.google.com/file/d/1irycJwXYXmpIUlBt88BZc2YzH__Ukj6A/view?usp=sharing) . It is advised to create a new directory coco_kpts and create softlink of the directory **images** and **annotations** from coco to this directory. Keep the **downloaded labels** and the files **train2017.txt** and **val2017.txt** inside this folder coco_kpts.

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
## Training

* Before running `train.py`, we need to compile Directionmax operation used in our paper, which is inspired by the corner pooling scheme in [CornerNet](https://github.com/princeton-vl/CornerNet).
```
`cd <CornerNet dir>/models/py_utils/_cpools/`
`python setup.py install --user`
```
* Then train the model
```
`python train.py`
```

## Ethical Proof
All experimental procedures were performed in accordance with the Guidance on the Operation of the Animals (Scientific Procedures) Act, 1986 (UK) and approved by the Queen’s University Belfast Animal.

## Citation
If you find this repository useful, please cite our paper:
```
@article{zhou2021structured,
  title={Structured Context Enhancement Network for Mouse Pose Estimation},
  author={Zhou, Feixiang and Jiang, Zheheng and Liu, Zhihua and Chen, Fang and Chen, Long and Tong, Lei and Yang, Zhile and Wang, Haikuan and Fei, Minrui and Li, Ling and others},
  journal={IEEE Transactions on Circuits and Systems for Video Technology},
  year={2021},
  publisher={IEEE}
}
```

## Contact
For any questions, feel free to contact: `fz64@leicester.ac.uk`
