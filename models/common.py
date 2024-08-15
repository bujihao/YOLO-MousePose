# This file contains modules common to various models

import math
from copy import copy
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import torch
import torch.nn as nn
from PIL import Image
from torch.cuda import amp

from utils.datasets import letterbox
from utils.general import non_max_suppression, non_max_suppression_export, make_divisible, scale_coords, increment_path, xyxy2xywh, save_one_box
from utils.plots import colors, plot_one_box
from utils.torch_utils import time_synchronized

from torchvision import models


class ChannelAttentionModule(nn.Module):
    def __init__(self, c1, reduction=16):
        super(ChannelAttentionModule, self).__init__()
        mid_channel = c1 // reduction
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.max_pool = nn.AdaptiveMaxPool2d(1)

        self.shared_MLP = nn.Sequential(
            nn.Linear(in_features=c1, out_features=mid_channel),
            nn.LeakyReLU(0.1, inplace=True),
            nn.Linear(in_features=mid_channel, out_features=c1)
        )
        self.act = nn.Sigmoid()
        # self.act=nn.SiLU()

    def forward(self, x):
        avgout = self.shared_MLP(self.avg_pool(x).view(x.size(0), -1)).unsqueeze(2).unsqueeze(3)
        maxout = self.shared_MLP(self.max_pool(x).view(x.size(0), -1)).unsqueeze(2).unsqueeze(3)
        return self.act(avgout + maxout)


class SpatialAttentionModule(nn.Module):
    def __init__(self):
        super(SpatialAttentionModule, self).__init__()
        self.conv2d = nn.Conv2d(in_channels=2, out_channels=1, kernel_size=7, stride=1, padding=3)
        self.act = nn.Sigmoid()

    def forward(self, x):
        avgout = torch.mean(x, dim=1, keepdim=True)
        maxout, _ = torch.max(x, dim=1, keepdim=True)
        out = torch.cat([avgout, maxout], dim=1)
        out = self.act(self.conv2d(out))
        return out


class CBAM(nn.Module):
    def __init__(self, c1, c2):
        super(CBAM, self).__init__()
        self.channel_attention = ChannelAttentionModule(c1)
        self.spatial_attention = SpatialAttentionModule()

    def forward(self, x):
        out = self.channel_attention(x) * x
        out = self.spatial_attention(out) * out
        return out

class ConvBNReLU(nn.Sequential):  # 该函数主要做卷积 池化 ReLU6激活操作
    def __init__(self, in_planes, out_planes, kernel_size=3, stride=1, groups=1):
        padding = (kernel_size - 1) // 2  # 池化 = （步长-1）整除2
        super(ConvBNReLU, self).__init__(  # 调用ConvBNReLU父类添加模块
            nn.Conv2d(in_planes, out_planes, kernel_size, stride, padding, bias=False, groups=groups),  # bias默认为False
            nn.BatchNorm2d(out_planes),
            nn.ReLU6(inplace=True))


class InvertedResidual(nn.Module):  # 该模块主要实现了倒残差模块
    def __init__(self, inp, oup, stride, expand_ratio):  # inp 输入 oup 输出 stride步长 exoand_ratio 按比例扩张
        super(InvertedResidual, self).__init__()
        self.stride = stride
        assert stride in [1, 2]
        hidden_dim = int(round(inp * expand_ratio))  # 由于有到残差模块有1*1,3*3的卷积模块，所以可以靠expand_rarton来进行升维
        self.use_res_connect = self.stride == 1 and inp == oup  # 残差连接的判断条件：当步长=1且输入矩阵与输出矩阵的shape相同时进行
        layers = []
        if expand_ratio != 1:  # 如果expand_ratio不等于1，要做升维操作，对应图中的绿色模块
            # pw
            layers.append(ConvBNReLU(inp, hidden_dim, kernel_size=1))  # 这里添加的是1*1的卷积操作
        layers.extend([
            # dw
            ConvBNReLU(hidden_dim, hidden_dim, stride=stride, groups=hidden_dim),
            # 这里做3*3的卷积操作，步长可能是1也可能是2,groups=hidden_dim表示这里使用了分组卷积的操作，对应图上的蓝色模块

            # pw-linear
            nn.Conv2d(hidden_dim, oup, 1, 1, 0, bias=False),  # 对应图中的黄色模块
            nn.BatchNorm2d(oup),
        ])
        self.conv = nn.Sequential(*layers)  # 将layers列表中的元素解开依次传入nn.Sequential

    def forward(self, x):
        if self.use_res_connect:  # 如果使用了残差连接，就会进行一个x+的操作
            return x + self.conv(x)
        else:
            return self.conv(x)  # 否则不做操作


def autopad(k, p=None):  # kernel, padding
    # Pad to 'same'
    if p is None:
        p = k // 2 if isinstance(k, int) else [x // 2 for x in k]  # auto-pad
    return p


def DWConv(c1, c2, k=1, s=1, act=True):
    # Depthwise convolution
    return Conv(c1, c2, k, s, g=math.gcd(c1, c2), act=act)
#math.gcd是找最大公约数

class Conv(nn.Module):
    # Standard convolution
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Conv, self).__init__()
        self.conv = nn.Conv2d(c1, c2, k, s, autopad(k, p), groups=g, bias=False)
        self.bn = nn.BatchNorm2d(c2)
        if act != "ReLU":
            self.act = nn.SiLU() if act is True else (act if isinstance(act, nn.Module) else nn.Identity())
        else:
            self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.act(self.bn(self.conv(x)))

    def fuseforward(self, x):
        return self.act(self.conv(x))
class TransformerLayer(nn.Module):
    # Transformer layer https://arxiv.org/abs/2010.11929 (LayerNorm layers removed for better performance)
    def __init__(self, c, num_heads):
        super().__init__()
        self.q = nn.Linear(c, c, bias=False)
        self.k = nn.Linear(c, c, bias=False)
        self.v = nn.Linear(c, c, bias=False)
        self.ma = nn.MultiheadAttention(embed_dim=c, num_heads=num_heads)
        self.fc1 = nn.Linear(c, c, bias=False)
        self.fc2 = nn.Linear(c, c, bias=False)

    def forward(self, x):
        x = self.ma(self.q(x), self.k(x), self.v(x))[0] + x
        x = self.fc2(self.fc1(x)) + x
        return x


class TransformerBlock(nn.Module):
    # Vision Transformer https://arxiv.org/abs/2010.11929
    def __init__(self, c1, c2, num_heads, num_layers):
        super().__init__()
        self.conv = None
        if c1 != c2:
            self.conv = Conv(c1, c2)
        self.linear = nn.Linear(c2, c2)  # learnable position embedding
        self.tr = nn.Sequential(*[TransformerLayer(c2, num_heads) for _ in range(num_layers)])
        self.c2 = c2

    def forward(self, x):
        if self.conv is not None:
            x = self.conv(x)
        b, _, w, h = x.shape
        p = x.flatten(2)
        p = p.unsqueeze(0)
        p = p.transpose(0, 3)
        p = p.squeeze(3)
        e = self.linear(p)
        x = p + e

        x = self.tr(x)
        x = x.unsqueeze(3)
        x = x.transpose(0, 3)
        x = x.reshape(b, self.c2, w, h)
        return x


class Bottleneck(nn.Module):
    # Standard bottleneck
    def __init__(self, c1, c2, shortcut=True, g=1, e=0.5, act=True):  # ch_in, ch_out, shortcut, groups, expansion
        super(Bottleneck, self).__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1, act=act)
        self.cv2 = Conv(c_, c2, 3, 1, g=g, act=act)
        self.add = shortcut and c1 == c2

    def forward(self, x):
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))


class BottleneckCSP(nn.Module):
    # CSP Bottleneck https://github.com/WongKinYiu/CrossStagePartialNetworks
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5, act=True):  # ch_in, ch_out, number, shortcut, groups, expansion
        super(BottleneckCSP, self).__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1, act=act)
        self.cv2 = nn.Conv2d(c1, c_, 1, 1, bias=False)
        self.cv3 = nn.Conv2d(c_, c_, 1, 1, bias=False)
        self.cv4 = Conv(2 * c_, c2, 1, 1, act=act)
        self.bn = nn.BatchNorm2d(2 * c_)  # applied to cat(cv2, cv3)
        #self.act = nn.LeakyReLU(inplace=True)
        self.act = nn.ReLU(inplace=True)
        self.m = nn.Sequential(*[Bottleneck(c_, c_, shortcut, g, e=1.0) for _ in range(n)])

    def forward(self, x):
        y1 = self.cv3(self.m(self.cv1(x)))
        y2 = self.cv2(x)
        return self.cv4(self.act(self.bn(torch.cat((y1, y2), dim=1))))


class C3(nn.Module):
    # CSP Bottleneck with 3 convolutions
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5, act=True):  # ch_in, ch_out, number, shortcut, groups, expansion
        super(C3, self).__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1, act=act)
        self.cv2 = Conv(c1, c_, 1, 1, act=act)
        self.cv3 = Conv(2 * c_, c2, 1, act=act)  # act=FReLU(c2)
        self.m = nn.Sequential(*[Bottleneck(c_, c_, shortcut, g, e=1.0, act=act) for _ in range(n)])
        # self.m = nn.Sequential(*[CrossConv(c_, c_, 3, 1, g, 1.0, shortcut) for _ in range(n)])

    def forward(self, x):
        return self.cv3(torch.cat((self.m(self.cv1(x)), self.cv2(x)), dim=1))


class C3TR(C3):
    # C3 module with TransformerBlock()
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5):
        super().__init__(c1, c2, n, shortcut, g, e)
        c_ = int(c2 * e)
        self.m = TransformerBlock(c_, c_, 4, n)


class SPP(nn.Module):
    # Spatial pyramid pooling layer used in YOLOv3-SPP
    def __init__(self, c1, c2, k=(3, 3, 3)):
        print(k)
        super(SPP, self).__init__()
        c_ = c1 // 2  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c_ * (len(k) + 1), c2, 1, 1)
        num_3x3_maxpool = []
        max_pool_module_list = []
        for pool_kernel in k:
            assert (pool_kernel-3)%2==0; "Required Kernel size cannot be implemented with kernel_size of 3"
            num_3x3_maxpool = 1 + (pool_kernel-3)//2
            max_pool_module_list.append(nn.Sequential(*num_3x3_maxpool*[nn.MaxPool2d(kernel_size=3, stride=1, padding=1)]))
            #max_pool_module_list[-1] = nn.ModuleList(max_pool_module_list[-1])
        self.m = nn.ModuleList(max_pool_module_list)

        #self.m = nn.ModuleList([nn.MaxPool2d(kernel_size=x, stride=1, padding=x // 2) for x in k])


    def forward(self, x):
        x = self.cv1(x)
        return self.cv2(torch.cat([x] + [m(x) for m in self.m], 1))


class SPPF(nn.Module):
    # Spatial Pyramid Pooling - Fast (SPPF) layer for YOLOv5 by Glenn Jocher
    def __init__(self, c1, c2, k=5):  # equivalent to SPP(k=(5, 9, 13))
        super().__init__()
        c_ =    c1 // 2  # hidden channels
        self.cv1 = Conv(c1, c_, 1, 1)
        self.cv2 = Conv(c_ * 4, c2, 1, 1)
        self.m = nn.MaxPool2d(kernel_size=k, stride=1, padding=k // 2)

    def forward(self, x):
        x = self.cv1(x)
        y1 = self.m(x)
        y2 = self.m(y1)
        return self.cv2(torch.cat([x, y1, y2, self.m(y2)], 1))


class Focus(nn.Module):
    # Focus wh information into c-space
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Focus, self).__init__()
        self.contract = Contract(gain=2)
        self.conv = Conv(c1 * 4, c2, k, s, p, g, act)

    def forward(self, x):  # x(b,c,w,h) -> y(b,4c,w/2,h/2)
        if hasattr(self, "contract"):
            x = self.contract(x)
        elif hasattr(self, "conv_slice"):
            x = self.conv_slice(x)
        else:
            x = torch.cat([x[..., ::2, ::2], x[..., 1::2, ::2], x[..., ::2, 1::2], x[..., 1::2, 1::2]], 1)
        return self.conv(x)


class ConvFocus(nn.Module):
    # Focus wh information into c-space
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1, act=True):  # ch_in, ch_out, kernel, stride, padding, groups
        super(ConvFocus, self).__init__()
        slice_kernel = 3
        slice_stride = 2
        self.conv_slice = Conv(c1, c1*4, slice_kernel, slice_stride, p, g, act)
        self.conv = Conv(c1 * 4, c2, k, s, p, g, act)

    def forward(self, x):  # x(b,c,w,h) -> y(b,4c,w/2,h/2)
        if hasattr(self, "conv_slice"):
            x = self.conv_slice(x)
        else:
            x = torch.cat([x[..., ::2, ::2], x[..., 1::2, ::2], x[..., ::2, 1::2], x[..., 1::2, 1::2]], 1)
        x = self.conv(x)
        return x


class Contract(nn.Module):
    # Contract width-height into channels, i.e. x(1,64,80,80) to x(1,256,40,40)
    def __init__(self, gain=2):
        super().__init__()
        self.gain = gain

    def forward(self, x):
        N, C, H, W = x.size()  # assert (H / s == 0) and (W / s == 0), 'Indivisible gain'
        s = self.gain
        x = x.view(N, C, H // s, s, W // s, s)  # x(1,64,40,2,40,2)
        x = x.permute(0, 3, 5, 1, 2, 4).contiguous()  # x(1,2,2,64,40,40)
        return x.view(N, C * s * s, H // s, W // s)  # x(1,256,40,40)


class Expand(nn.Module):
    # Expand channels into width-height, i.e. x(1,64,80,80) to x(1,16,160,160)
    def __init__(self, gain=2):
        super().__init__()
        self.gain = gain

    def forward(self, x):
        N, C, H, W = x.size()  # assert C / s ** 2 == 0, 'Indivisible gain'
        s = self.gain
        x = x.view(N, s, s, C // s ** 2, H, W)  # x(1,2,2,16,80,80)
        x = x.permute(0, 3, 4, 1, 5, 2).contiguous()  # x(1,16,80,2,80,2)
        return x.view(N, C // s ** 2, H * s, W * s)  # x(1,16,160,160)


class Concat(nn.Module):
    # Concatenate a list of tensors along dimension
    def __init__(self, dimension=1):
        super(Concat, self).__init__()
        self.d = dimension

    def forward(self, x):
        return torch.cat(x, self.d)


class Down(nn.Module):
    # Concatenate a list of tensors along dimension
    def __init__(self, rate=4):
        super(Down, self).__init__()
        self.pool = torch.nn.MaxPool2d(rate, rate)

    def forward(self, x):
        return self.pool(x)


class NMS(nn.Module):
    # Non-Maximum Suppression (NMS) module
    iou = 0.45  # IoU threshold
    classes = None  # (optional list) filter by class

    def __init__(self, conf=0.25, kpt_label=False):
        super(NMS, self).__init__()
        self.conf=conf
        self.kpt_label = kpt_label

    def forward(self, x):
        return non_max_suppression(x[0], conf_thres=self.conf, iou_thres=self.iou, classes=self.classes, kpt_label=self.kpt_label)


class NMS_Export(nn.Module):
    # Non-Maximum Suppression (NMS) module used while exporting ONNX model
    iou = 0.45  # IoU threshold
    classes = None  # (optional list) filter by class

    def __init__(self, conf=0.001, kpt_label=False):
        super(NMS_Export, self).__init__()
        self.conf = conf
        self.kpt_label = kpt_label

    def forward(self, x):
        return non_max_suppression_export(x[0], conf_thres=self.conf, iou_thres=self.iou, classes=self.classes, kpt_label=self.kpt_label)



class autoShape(nn.Module):
    # input-robust model wrapper for passing cv2/np/PIL/torch inputs. Includes preprocessing, inference and NMS
    conf = 0.25  # NMS confidence threshold
    iou = 0.45  # NMS IoU threshold
    classes = None  # (optional list) filter by class

    def __init__(self, model):
        super(autoShape, self).__init__()
        self.model = model.eval()

    def autoshape(self):
        print('autoShape already enabled, skipping... ')  # model already converted to model.autoshape()
        return self

    @torch.no_grad()
    def forward(self, imgs, size=640, augment=False, profile=False):
        # Inference from various sources. For height=640, width=1280, RGB images example inputs are:
        #   filename:   imgs = 'data/images/zidane.jpg'
        #   URI:             = 'https://github.com/ultralytics/yolov5/releases/download/v1.0/zidane.jpg'
        #   OpenCV:          = cv2.imread('image.jpg')[:,:,::-1]  # HWC BGR to RGB x(640,1280,3)
        #   PIL:             = Image.open('image.jpg')  # HWC x(640,1280,3)
        #   numpy:           = np.zeros((640,1280,3))  # HWC
        #   torch:           = torch.zeros(16,3,320,640)  # BCHW (scaled to size=640, 0-1 values)
        #   multiple:        = [Image.open('image1.jpg'), Image.open('image2.jpg'), ...]  # list of images

        t = [time_synchronized()]
        p = next(self.model.parameters())  # for device and type
        if isinstance(imgs, torch.Tensor):  # torch
            with amp.autocast(enabled=p.device.type != 'cpu'):
                return self.model(imgs.to(p.device).type_as(p), augment, profile)  # inference

        # Pre-process
        n, imgs = (len(imgs), imgs) if isinstance(imgs, list) else (1, [imgs])  # number of images, list of images
        shape0, shape1, files = [], [], []  # image and inference shapes, filenames
        for i, im in enumerate(imgs):
            f = f'image{i}'  # filename
            if isinstance(im, str):  # filename or uri
                im, f = np.asarray(Image.open(requests.get(im, stream=True).raw if im.startswith('http') else im)), im
            elif isinstance(im, Image.Image):  # PIL Image
                im, f = np.asarray(im), getattr(im, 'filename', f) or f
            files.append(Path(f).with_suffix('.jpg').name)
            if im.shape[0] < 5:  # image in CHW
                im = im.transpose((1, 2, 0))  # reverse dataloader .transpose(2, 0, 1)
            im = im[:, :, :3] if im.ndim == 3 else np.tile(im[:, :, None], 3)  # enforce 3ch input
            s = im.shape[:2]  # HWC
            shape0.append(s)  # image shape
            g = (size / max(s))  # gain
            shape1.append([y * g for y in s])
            imgs[i] = im if im.data.contiguous else np.ascontiguousarray(im)  # update
        shape1 = [make_divisible(x, int(self.stride.max())) for x in np.stack(shape1, 0).max(0)]  # inference shape
        x = [letterbox(im, new_shape=shape1, auto=False)[0] for im in imgs]  # pad
        x = np.stack(x, 0) if n > 1 else x[0][None]  # stack
        x = np.ascontiguousarray(x.transpose((0, 3, 1, 2)))  # BHWC to BCHW
        x = torch.from_numpy(x).to(p.device).type_as(p) / 255.  # uint8 to fp16/32
        t.append(time_synchronized())

        with amp.autocast(enabled=p.device.type != 'cpu'):
            # Inference
            y = self.model(x, augment, profile)[0]  # forward
            t.append(time_synchronized())

            # Post-process
            y = non_max_suppression(y, conf_thres=self.conf, iou_thres=self.iou, classes=self.classes)  # NMS
            for i in range(n):
                scale_coords(shape1, y[i][:, :4], shape0[i])

            t.append(time_synchronized())
            return Detections(imgs, y, files, t, self.names, x.shape)


class Detections:
    # detections class for YOLOv5 inference results
    def __init__(self, imgs, pred, files, times=None, names=None, shape=None):
        super(Detections, self).__init__()
        d = pred[0].device  # device
        gn = [torch.tensor([*[im.shape[i] for i in [1, 0, 1, 0]], 1., 1.], device=d) for im in imgs]  # normalizations
        self.imgs = imgs  # list of images as numpy arrays
        self.pred = pred  # list of tensors pred[0] = (xyxy, conf, cls)
        self.names = names  # class names
        self.files = files  # image filenames
        self.xyxy = pred  # xyxy pixels
        self.xywh = [xyxy2xywh(x) for x in pred]  # xywh pixels
        self.xyxyn = [x / g for x, g in zip(self.xyxy, gn)]  # xyxy normalized
        self.xywhn = [x / g for x, g in zip(self.xywh, gn)]  # xywh normalized
        self.n = len(self.pred)  # number of images (batch size)
        self.t = tuple((times[i + 1] - times[i]) * 1000 / self.n for i in range(3))  # timestamps (ms)
        self.s = shape  # inference BCHW shape

    def display(self, pprint=False, show=False, save=False, crop=False, render=False, save_dir=Path('')):
        for i, (im, pred) in enumerate(zip(self.imgs, self.pred)):
            str = f'image {i + 1}/{len(self.pred)}: {im.shape[0]}x{im.shape[1]} '
            if pred is not None:
                for c in pred[:, -1].unique():
                    n = (pred[:, -1] == c).sum()  # detections per class
                    str += f"{n} {self.names[int(c)]}{'s' * (n > 1)}, "  # add to string
                if show or save or render or crop:
                    for *box, conf, cls in pred:  # xyxy, confidence, class
                        label = f'{self.names[int(cls)]} {conf:.2f}'
                        if crop:
                            save_one_box(box, im, file=save_dir / 'crops' / self.names[int(cls)] / self.files[i])
                        else:  # all others
                            plot_one_box(box, im, label=label, color=colors(cls))

            im = Image.fromarray(im.astype(np.uint8)) if isinstance(im, np.ndarray) else im  # from np
            if pprint:
                print(str.rstrip(', '))
            if show:
                im.show(self.files[i])  # show
            if save:
                f = self.files[i]
                im.save(save_dir / f)  # save
                print(f"{'Saved' * (i == 0)} {f}", end=',' if i < self.n - 1 else f' to {save_dir}\n')
            if render:
                self.imgs[i] = np.asarray(im)

    def print(self):
        self.display(pprint=True)  # print results
        print(f'Speed: %.1fms pre-process, %.1fms inference, %.1fms NMS per image at shape {tuple(self.s)}' % self.t)

    def show(self):
        self.display(show=True)  # show results

    def save(self, save_dir='runs/hub/exp'):
        save_dir = increment_path(save_dir, exist_ok=save_dir != 'runs/hub/exp', mkdir=True)  # increment save_dir
        self.display(save=True, save_dir=save_dir)  # save results

    def crop(self, save_dir='runs/hub/exp'):
        save_dir = increment_path(save_dir, exist_ok=save_dir != 'runs/hub/exp', mkdir=True)  # increment save_dir
        self.display(crop=True, save_dir=save_dir)  # crop results
        print(f'Saved results to {save_dir}\n')

    def render(self):
        self.display(render=True)  # render results
        return self.imgs

    def pandas(self):
        # return detections as pandas DataFrames, i.e. print(results.pandas().xyxy[0])
        new = copy(self)  # return copy
        ca = 'xmin', 'ymin', 'xmax', 'ymax', 'confidence', 'class', 'name'  # xyxy columns
        cb = 'xcenter', 'ycenter', 'width', 'height', 'confidence', 'class', 'name'  # xywh columns
        for k, c in zip(['xyxy', 'xyxyn', 'xywh', 'xywhn'], [ca, ca, cb, cb]):
            a = [[x[:5] + [int(x[5]), self.names[int(x[5])]] for x in x.tolist()] for x in getattr(self, k)]  # update
            setattr(new, k, [pd.DataFrame(x, columns=c) for x in a])
        return new

    def tolist(self):
        # return a list of Detections objects, i.e. 'for result in results.tolist():'
        x = [Detections([self.imgs[i]], [self.pred[i]], self.names, self.s) for i in range(self.n)]
        for d in x:
            for k in ['imgs', 'pred', 'xyxy', 'xyxyn', 'xywh', 'xywhn']:
                setattr(d, k, getattr(d, k)[0])  # pop out of list
        return x

    def __len__(self):
        return self.n


class Classify(nn.Module):
    # Classification head, i.e. x(b,c1,20,20) to x(b,c2)
    def __init__(self, c1, c2, k=1, s=1, p=None, g=1):  # ch_in, ch_out, kernel, stride, padding, groups
        super(Classify, self).__init__()
        self.aap = nn.AdaptiveAvgPool2d(1)  # to x(b,c1,1,1)
        self.conv = nn.Conv2d(c1, c2, k, s, autopad(k, p), groups=g)  # to x(b,c2,1,1)
        self.flat = nn.Flatten()

    def forward(self, x):
        z = torch.cat([self.aap(y) for y in (x if isinstance(x, list) else [x])], 1)  # cat if list
        return self.flat(self.conv(z))  # flatten to x(b,c2)


class v8_C2fBottleneck(nn.Module):
    # Standard bottleneck
    def __init__(self, c1, c2, shortcut=True, g=1, k=(3, 3), e=0.5):  # ch_in, ch_out, shortcut, groups, kernels, expand
        super().__init__()
        c_ = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, c_, k[0], 1)
        self.cv2 = Conv(c_, c2, k[1], 1, g=g)
        self.add = shortcut and c1 == c2

    def forward(self, x):
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))


class C2f(nn.Module):
    # CSP Bottleneck with 2 convolutions
    def __init__(self, c1, c2, n=1, shortcut=False, g=1, e=0.5):  # ch_in, ch_out, number, shortcut, groups, expansion
        super().__init__()
        self.c = int(c2 * e)  # hidden channels
        self.cv1 = Conv(c1, 2 * self.c, 1, 1)
        self.cv2 = Conv((2 + n) * self.c, c2, 1)  # optional act=FReLU(c2)
        self.m = nn.ModuleList(
            v8_C2fBottleneck(self.c, self.c, shortcut, g, k=((3, 3), (3, 3)), e=1.0) for _ in range(n))

    def forward(self, x):
        y = list(self.cv1(x).split((self.c, self.c), 1))
        y.extend(m(y[-1]) for m in self.m)
        return self.cv2(torch.cat(y, 1))

import torch.nn.functional as F



def onnx_AdaptiveAvgPool2d(x, output_size):
    stride_size = np.floor(np.array(x.shape[-2:]) / output_size).astype(np.int32)
    kernel_size = np.array(x.shape[-2:]) - (output_size - 1) * stride_size
    avg = nn.AvgPool2d(kernel_size=list(kernel_size), stride=list(stride_size))
    x = avg(x)
    return x

class h_sigmoid(nn.Module):
    def __init__(self, inplace=True):
        super(h_sigmoid, self).__init__()
        self.relu = nn.ReLU6(inplace=inplace)

    def forward(self, x):
        return self.relu(x + 3) / 6

class FusionEncoder(nn.Module):
    def __init__(self, inc, ouc, embed_dim_p=96, fuse_block_num=3) -> None:
        super().__init__()
        self.conv = nn.Sequential(
            Conv(inc, embed_dim_p),
            *[C2f(embed_dim_p, embed_dim_p) for _ in range(fuse_block_num)],
            Conv(embed_dim_p, sum(ouc))
        )
    def forward(self, x):
        return self.conv(x)


# import torch
class WeightedInject(nn.Module):
    def __init__(
            self,
            inp: int,
            oup: int,
            global_inp: int
    ) -> None:
        super().__init__()
        self.global_inp = global_inp
        oup_2=int(oup/2)
        self.local_embedding = Conv(inp, oup_2, 1, act=False)
        self.global_embedding = Conv(global_inp, oup_2, 1, act=False)
        self.global_act = Conv(global_inp, oup_2, 1, act=False)
        self.act = h_sigmoid()


    def forward(self, x):
        '''
        x_g: global features
        x_l: local features
        '''
        x_l, x_g = x

        gloabl_info = x_g
        local_feat = self.local_embedding(x_l)
        global_act = self.global_act(gloabl_info)
        sig_act = self.act(global_act)
        global_inj=gloabl_info * sig_act
        out = torch.cat((local_feat , global_inj), dim=1)
        return out

class SE_HALF(nn.Module):
    def __init__(self, c1, ratio=16):
        super(SE_HALF, self).__init__()
        self.c1 = c1  # Record the number of input channels
        # c*1*1
        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.l1 = nn.Linear(c1, c1 // ratio, bias=False)
        self.relu = nn.ReLU(inplace=True)
        self.l2 = nn.Linear(c1 // ratio, c1, bias=False)
        self.sig = nn.Sigmoid()

    def forward(self, x):
        b, c, h, w = x.size()
        y = self.avgpool(x).view(b, c)
        y = self.l1(y)
        y = self.relu(y)
        y = self.l2(y)
        y = self.sig(y)

        # Sort channel weights
        _, indices = torch.sort(y, descending=True)

        # Retrieve the index of the top 50% channels
        half_ch = c // 2
        indices = indices[:, :half_ch]

        # Rearrange input channels based on sorting and only retain the first half
        x_sorted_half = x.new_zeros((b, half_ch, h, w))
        for b_idx in range(x.size(0)):
            x_sorted_half[b_idx] = x[b_idx, indices[b_idx]]

        y_half = y.gather(1, indices)

        y_half = y_half.view(b, half_ch, 1, 1)

        # Note that at this point, the channels x_sorted_half and y_half have already been arranged correspondingly

        return x_sorted_half * y_half.expand_as(x_sorted_half)

class ECA_SORT(nn.Module):
    def __init__(self, c1, c2, k_size=3):
        super(ECA_SORT, self).__init__()
        self.c1 = c1  # the number of input channels
        self.c2 = c2  # the number of output channels
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv = nn.Conv1d(1, 1, kernel_size=k_size, padding=(k_size - 1) // 2, bias=False)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # Calculate channel weights
        y = self.avg_pool(x)
        y = self.conv(y.squeeze(-1).transpose(-1, -2)).transpose(-1, -2).unsqueeze(-1)
        y = self.sigmoid(y).squeeze(-1).squeeze(-1)  # 将权重调整为一维

        # Sort the channel weights
        _, indices = torch.sort(y, descending=True)

        # Retrieve the index of the first c2 channels
        indices = indices[:, :self.c2]

        # Rearrange input channels based on sorting and only retain the first c2 channels
        b, _, h, w = x.size()
        x_sorted_out = x.new_zeros((b, self.c2, h, w))
        for b_idx in range(b):
            x_sorted_out[b_idx] = x[b_idx, indices[b_idx]]

        return x_sorted_out

class SE_SORT(nn.Module):
    def __init__(self, c1,c2 ,ratio=16):
        super(SE_SORT, self).__init__()
        self.c1 = c1  # Record the number of input channels
        self.c2 = c2  # Record the number of output channels
        # c*1*1
        self.avgpool = nn.AdaptiveAvgPool2d(1)
        self.l1 = nn.Linear(c1, c1 // ratio, bias=False)
        self.relu = nn.ReLU(inplace=True)
        self.l2 = nn.Linear(c1 // ratio, c1, bias=False)
        self.sig = nn.Sigmoid()

    def forward(self, x):
        b, c, h, w = x.size()
        y = self.avgpool(x).view(b, c)
        y = self.l1(y)
        y = self.relu(y)
        y = self.l2(y)
        y = self.sig(y)
        # Sort channel weights
        _, indices = torch.sort(y, descending=True)
        # Obtain the index of the output channel
        out_ch =self.c2
        indices = indices[:, :out_ch]
        # Rearranges the input channels according to sorting and retains only the top half.
        x_sorted_out = x.new_zeros((b, out_ch, h, w))
        for b_idx in range(x.size(0)):
            x_sorted_out[b_idx] = x[b_idx, indices[b_idx]]
        # y_out = y.gather(1, indices)
        # y_out = y_out.view(b, out_ch, 1, 1)
        return x_sorted_out
class ChannelSelection_Top(nn.Module):
    def __init__(self, in_channel_list, out_channels):
        super().__init__()
        out_channels_2=int(out_channels/3)
        self.cv0 = nn.Sequential(SE_SORT(in_channel_list[0], out_channels_2) if in_channel_list[0] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[0] > out_channels_2
                                 else Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
                                 )
        self.cv2 = nn.Sequential(SE_SORT(in_channel_list[2], out_channels_2) if in_channel_list[
                                                                                    2] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[2] > out_channels_2
                                 else Conv(in_channel_list[2], out_channels_2, act=nn.ReLU())
                                 )
        self.cv3 = nn.Sequential(SE_SORT(in_channel_list[3], out_channels_2) if in_channel_list[
                                                                                    3] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            3] > out_channels_2
                                 else Conv(in_channel_list[3], out_channels_2, act=nn.ReLU())
                                 )
        self.downsample = nn.functional.adaptive_avg_pool2d

    def forward(self, x):
        N, C, H, W = x[1].shape
        output_size = (H, W)
        if torch.onnx.is_in_onnx_export():
            self.downsample = onnx_AdaptiveAvgPool2d
            output_size = np.array([H, W])
        x_l = self.cv0(self.downsample(x[0], output_size))
        x_s =self.cv2(F.interpolate(x[2], size=(H, W), mode='bilinear', align_corners=False))
        x_n = self.cv3(F.interpolate(x[3], size=(H, W), mode='bilinear', align_corners=False))
        out = torch.cat([x_l,x_s,x_n], 1)
        return out

class ChannelSelection_Medium(nn.Module):
    def __init__(self, in_channel_list, out_channels):
        super().__init__()
        out_channels_2 = int(out_channels /3)
        self.cv0 = nn.Sequential(SE_SORT(in_channel_list[0], out_channels_2) if in_channel_list[
                                                                                    0] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            0] > out_channels_2
                                 else Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
                                 )
        self.cv1 = nn.Sequential(SE_SORT(in_channel_list[1], out_channels_2) if in_channel_list[
                                                                                    1] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            1] > out_channels_2
                                 else Conv(in_channel_list[1], out_channels_2, act=nn.ReLU())
                                 )
        self.cv3 = nn.Sequential(SE_SORT(in_channel_list[3], out_channels_2) if in_channel_list[
                                                                                    3] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            3] > out_channels_2
                                 else Conv(in_channel_list[3], out_channels_2, act=nn.ReLU())
                                 )
        self.downsample = nn.functional.adaptive_avg_pool2d

    def forward(self, x):
        x_l, x_m, x_s, x_n = x
        B, C, H, W = x[2].shape
        output_size = np.array([H, W])
        if torch.onnx.is_in_onnx_export():
            self.avg_pool = onnx_AdaptiveAvgPool2d
        #
        x_l = self.cv0(self.downsample(x[0], output_size))
        x_m = self.cv1(self.downsample(x[1], output_size))
        x_n = self.cv3(F.interpolate(x[3], size=(H, W), mode='bilinear', align_corners=False))
        out = torch.cat([x_l, x_m, x_n], 1)
        return out

class ChannelSelection_Bottom(nn.Module):
    def __init__(self, in_channel_list, out_channels):
        super().__init__()
        out_channels_2 = int(out_channels /3)
        self.cv0 = nn.Sequential(SE_SORT(in_channel_list[0], out_channels_2) if in_channel_list[
                                                                                    0] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            0] > out_channels_2
                                 else Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
                                 )

        self.cv1 = nn.Sequential(SE_SORT(in_channel_list[1],  out_channels_2) if in_channel_list[
                                                                                    1] >  out_channels_2 else nn.Identity(),
                                 Conv( out_channels_2,  out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            1] >  out_channels_2
                                 else Conv(in_channel_list[1],  out_channels_2, act=nn.ReLU())
                                 )

        self.cv2 = nn.Sequential(SE_SORT(in_channel_list[2], out_channels_2) if in_channel_list[
                                                                                    2] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            2] > out_channels_2
                                 else Conv(in_channel_list[2], out_channels_2, act=nn.ReLU())
                                 )
        self.downsample = nn.functional.adaptive_avg_pool2d

    def forward(self, x):
        x_l, x_m, x_s, x_n = x
        B, C, H, W = x[3].shape
        output_size = np.array([H, W])
        if torch.onnx.is_in_onnx_export():
            self.avg_pool = onnx_AdaptiveAvgPool2d

        x_l = self.cv0(self.downsample(x_l, output_size))
        x_m = self.cv1(self.downsample(x_m, output_size))
        x_s =self.cv2(self.downsample(x_s, output_size))
        out = torch.cat([x_l, x_m, x_s], 1)
        return out












class IFMF(nn.Module):
    def __init__(self, inc, ouc, embed_dim_p=96, fuse_block_num=3) -> None:
        super().__init__()
        # print(inc,"inc")
        # print(ouc,"ouc")
        # print(embed_dim_p, " embed_dim_p")
        # print(sum(ouc), "sum(ouc)")
        # self.conv =nn.Sequential(
        #     Conv(inc, sum(ouc)))
        self.conv = nn.Sequential(
            Conv(inc, embed_dim_p),
            *[C2f(embed_dim_p, embed_dim_p) for _ in range(fuse_block_num)],
            Conv(embed_dim_p, sum(ouc))
        )


    def forward(self, x):
        # print(x.size())
        # return x
        return self.conv(x)


# import torch
class Inject(nn.Module):
    def __init__(
            self,
            inp: int,
            oup: int,
            global_inp: int
    ) -> None:
        super().__init__()
        self.global_inp = global_inp
        oup_2=int(oup/2)
        # print(inp, oup_2,"inpinject1")

        # self.conv1=Conv(inp, oup/2, 1, act=False)
        self.conv1 = Conv(inp, oup_2, 1, act=False)
        self.conv2 = Conv(oup_2, oup_2, 1, act=False)
        self.local_embedding = Conv(inp, oup_2, 1, act=False)
        self.global_embedding = Conv(global_inp, oup_2, 1, act=False)
        self.global_act = Conv(global_inp, oup_2, 1, act=False)
        self.act = h_sigmoid()


    def forward(self, x):
        '''
        x_g: global features
        x_l: local features
        '''
        x_l, x_g = x
        # x_l = self.conv1(x_l)
        # print(x_g.size(),"x_g")
        # x_g = self.conv2(x_g)
        gloabl_info = x_g
        local_feat = self.local_embedding(x_l)
        global_act = self.global_act(gloabl_info)
        # global_feat = self.global_embedding(gloabl_info)
        sig_act = self.act(global_act)
        global_inj=gloabl_info * sig_act
        out = torch.cat((local_feat , global_inj), dim=1)  #
        # out = torch.cat((x_l, x_g ), dim=1)
        return out



class Fusion_4in_Top(nn.Module):
    def __init__(self, in_channel_list, out_channels):
        super().__init__()
        out_channels_2=int(out_channels/3)


        self.cv0 = nn.Sequential(SE_SORT(in_channel_list[0], out_channels_2) if in_channel_list[0] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[0] > out_channels_2
                                 else Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
                                 )
        self.cv2 = nn.Sequential(SE_SORT(in_channel_list[2], out_channels_2) if in_channel_list[
                                                                                    2] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[2] > out_channels_2
                                 else Conv(in_channel_list[2], out_channels_2, act=nn.ReLU())
                                 )
        self.cv3 = nn.Sequential(SE_SORT(in_channel_list[3], out_channels_2) if in_channel_list[
                                                                                    3] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            3] > out_channels_2
                                 else Conv(in_channel_list[3], out_channels_2, act=nn.ReLU())
                                 )

        #
        # self.cv0 = nn.Sequential(Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
        #                          )
        # self.cv2 = nn.Sequential(Conv(in_channel_list[2], out_channels_2, act=nn.ReLU())
        #                          )
        # self.cv3 = nn.Sequential(Conv(in_channel_list[3], out_channels_2, act=nn.ReLU())
        #                          )
        # self.cv0 = nn.Sequential(Conv(in_channel_list[0], in_channel_list[0], act=nn.ReLU())
        #                          )
        # self.cv2 = nn.Sequential(Conv(in_channel_list[2], in_channel_list[2], act=nn.ReLU())
        #                          )
        # self.cv3 = nn.Sequential(Conv(in_channel_list[3], in_channel_list[3], act=nn.ReLU())
        #                          )
        self.downsample = nn.functional.adaptive_avg_pool2d

    def forward(self, x):
        N, C, H, W = x[1].shape
        output_size = (H, W)
        if torch.onnx.is_in_onnx_export():
            self.downsample = onnx_AdaptiveAvgPool2d
            output_size = np.array([H, W])
        x_l = self.cv0(self.downsample(x[0], output_size))
        x_s =self.cv2(F.interpolate(x[2], size=(H, W), mode='bilinear', align_corners=False))
        x_n = self.cv3(F.interpolate(x[3], size=(H, W), mode='bilinear', align_corners=False))
        # x_l = self.downsample(x[0], output_size)
        # x_s = F.interpolate(x[2], size=(H, W), mode='bilinear', align_corners=False)
        # x_n = F.interpolate(x[3], size=(H, W), mode='bilinear', align_corners=False)

        out = torch.cat([x_l,x_s,x_n], 1)
        return out

class Fusion_4in_Medium(nn.Module):
    def __init__(self, in_channel_list, out_channels):
        super().__init__()
        # out_channels_2 = int(out_channels * 0.4)
        out_channels_2 = int(out_channels /3)
        #
        self.cv0 = nn.Sequential(SE_SORT(in_channel_list[0], out_channels_2) if in_channel_list[
                                                                                    0] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            0] > out_channels_2
                                 else Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
                                 )
        self.cv1 = nn.Sequential(SE_SORT(in_channel_list[1], out_channels_2) if in_channel_list[
                                                                                    1] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            1] > out_channels_2
                                 else Conv(in_channel_list[1], out_channels_2, act=nn.ReLU())
                                 )
        self.cv3 = nn.Sequential(SE_SORT(in_channel_list[3], out_channels_2) if in_channel_list[
                                                                                    3] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            3] > out_channels_2
                                 else Conv(in_channel_list[3], out_channels_2, act=nn.ReLU())
                                 )
        # self.cv0 = nn.Sequential(Conv(in_channel_list[0], out_channels_2 , act=nn.ReLU())
        #                          )
        # self.cv1 = nn.Sequential(Conv(in_channel_list[1], out_channels_2 , act=nn.ReLU())
        #                          )
        # self.cv3 = nn.Sequential(Conv(in_channel_list[3], out_channels_2 , act=nn.ReLU())
        #                          )

        # self.cv0 = nn.Sequential(Conv(in_channel_list[0], in_channel_list[0] , act=nn.ReLU())
        #                          )
        # self.cv1 = nn.Sequential(Conv(in_channel_list[1], in_channel_list[1] , act=nn.ReLU())
        #                          )
        # self.cv3 = nn.Sequential(Conv(in_channel_list[3], in_channel_list[3] , act=nn.ReLU())
        #                          )

        # self.cv_fuse = Conv(out_channels * 2, out_channels, act=nn.ReLU())
        self.downsample = nn.functional.adaptive_avg_pool2d

    def forward(self, x):
        x_l, x_m, x_s, x_n = x
        B, C, H, W = x[2].shape
        output_size = np.array([H, W])
        if torch.onnx.is_in_onnx_export():
            self.avg_pool = onnx_AdaptiveAvgPool2d
        #
        x_l = self.cv0(self.downsample(x[0], output_size))
        x_m = self.cv1(self.downsample(x[1], output_size))
        x_n = self.cv3(F.interpolate(x[3], size=(H, W), mode='bilinear', align_corners=False))
        # x_l = self.downsample(x[0], output_size)
        # x_m = self.downsample(x[1], output_size)
        # x_n = F.interpolate(x[3], size=(H, W), mode='bilinear', align_corners=False)
        out = torch.cat([x_l, x_m, x_n], 1)
        return out

class Fusion_4in_Bottom(nn.Module):
    def __init__(self, in_channel_list, out_channels):
        super().__init__()
        out_channels_2 = int(out_channels /3)



        #
        self.cv0 = nn.Sequential(SE_SORT(in_channel_list[0], out_channels_2) if in_channel_list[
                                                                                    0] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            0] > out_channels_2
                                 else Conv(in_channel_list[0], out_channels_2, act=nn.ReLU())
                                 )

        self.cv1 = nn.Sequential(SE_SORT(in_channel_list[1],  out_channels_2) if in_channel_list[
                                                                                    1] >  out_channels_2 else nn.Identity(),
                                 Conv( out_channels_2,  out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            1] >  out_channels_2
                                 else Conv(in_channel_list[1],  out_channels_2, act=nn.ReLU())
                                 )

        self.cv2 = nn.Sequential(SE_SORT(in_channel_list[2], out_channels_2) if in_channel_list[
                                                                                    2] > out_channels_2 else nn.Identity(),
                                 Conv(out_channels_2, out_channels_2, act=nn.ReLU()) if in_channel_list[
                                                                                            2] > out_channels_2
                                 else Conv(in_channel_list[2], out_channels_2, act=nn.ReLU())
                                 )
        # self.cv0 = nn.Sequential(Conv(in_channel_list[0],  out_channels_2 , act=nn.ReLU())
        #                          )
        # self.cv1 = nn.Sequential(Conv(in_channel_list[1],  out_channels_2 , act=nn.ReLU())
        #                          )
        # self.cv2 = nn.Sequential(Conv(in_channel_list[2],  out_channels_2 , act=nn.ReLU())
        #                          )
        # self.cv0 = nn.Sequential(Conv(in_channel_list[0],  in_channel_list[0] , act=nn.ReLU())
        #                          )
        # self.cv1 = nn.Sequential(Conv(in_channel_list[1],  in_channel_list[1], act=nn.ReLU())
        #                          )
        # self.cv2 = nn.Sequential(Conv(in_channel_list[2],  in_channel_list[2] , act=nn.ReLU())
        #                          )
        self.downsample = nn.functional.adaptive_avg_pool2d

    def forward(self, x):
        x_l, x_m, x_s, x_n = x
        B, C, H, W = x[3].shape
        output_size = np.array([H, W])
        if torch.onnx.is_in_onnx_export():
            self.avg_pool = onnx_AdaptiveAvgPool2d

        x_l = self.cv0(self.downsample(x_l, output_size))
        x_m = self.cv1(self.downsample(x_m, output_size))
        x_s =self.cv2(self.downsample(x_s, output_size))
        # x_l = self.downsample(x_l, output_size)
        # x_m = self.downsample(x_m, output_size)
        # x_s = self.downsample(x_s, output_size)
        out = torch.cat([x_l, x_m, x_s], 1)
        return out
