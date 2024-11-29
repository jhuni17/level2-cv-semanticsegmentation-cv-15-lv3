import torch
import torch.nn as nn
import torchvision.models as models

from config import IMSIZE
import numpy as np
from Util.InitWeights import init_weights
from Util.SetSeed import set_seed
from .layer import unetConv2, BottleNeck
import torchvision.models as models
from torchvision.models import convnext_large, ConvNeXt_Large_Weights
from torchvision.models import resnet34, resnet50, resnet101,resnet152
import torch.nn.functional as F
set_seed()
from HRNet.lib.HRmodels.cls_hrnet import get_cls_net
import yaml


import torch
import torch.nn as nn
from HRNet.lib.HRmodels.cls_hrnet import HighResolutionNet, get_cls_net
import yaml


import torch.nn.init as init

class Bottleneck(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1, expansion=4):
        super(Bottleneck, self).__init__()
        mid_channels = out_channels // expansion

        # 1x1 Conv: Compression
        self.conv1 = nn.Conv2d(in_channels, mid_channels, kernel_size=1, stride=1, bias=False)
        self.bn1 = nn.BatchNorm2d(mid_channels)
        self.relu = nn.ReLU(inplace=True)

        # 3x3 Conv: Main operation
        self.conv2 = nn.Conv2d(mid_channels, mid_channels, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(mid_channels)

        # 1x1 Conv: Restoration
        self.conv3 = nn.Conv2d(mid_channels, out_channels, kernel_size=1, stride=1, bias=False)
        self.bn3 = nn.BatchNorm2d(out_channels)

        # Skip connection (if required)
        self.downsample = None
        if in_channels != out_channels or stride != 1:
            self.downsample = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(out_channels)
            )

        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        identity = x

        # Forward pass
        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)

        # Add skip connection
        if self.downsample is not None:
            identity = self.downsample(x)

        out += identity
        out = self.relu(out)

        return out
    

class HRNetEncoder(nn.Module):
    def __init__(self, hrnet_config_file, pretrained_weights=None):
        super(HRNetEncoder, self).__init__()

        # Load HRNet configuration from YAML
        with open(hrnet_config_file, "r") as f:
            hrnet_config = yaml.safe_load(f)

        # Initialize HRNet
        self.hrnet = get_cls_net(hrnet_config)

        # Load pretrained weights
        if pretrained_weights:
            self.hrnet.init_weights(pretrained_weights)

        # Add a new layer for h0
        self.h0_conv = nn.Conv2d(3, 64, kernel_size=3, stride=1, padding=1)
        self.h0_bn = nn.BatchNorm2d(64)
        self.h0_relu = nn.ReLU()

        # Add Bottleneck block for h0
        self.bottle_neck = Bottleneck(64, 64)

        # Add a layer to reduce back to 3 channels before h1
        self.h0_to_h1_conv = nn.Conv2d(64, 3, kernel_size=3, stride=1, padding=1)
        self.h0_to_h1_bn = nn.BatchNorm2d(3)
        self.h0_to_h1_relu = nn.ReLU()

        # Initialize layers with kaiming initialization
        self._initialize_weights()

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        # Create h0 (64 channels, original size)
        h0 = self.h0_conv(x)
        h0 = self.h0_bn(h0)
        h0 = self.h0_relu(h0)

        # Bottleneck processing for h0
        h0 = self.bottle_neck(h0)

        # Convert back to 3 channels for h1 processing
        x = self.h0_to_h1_conv(h0)
        x = self.h0_to_h1_bn(x)
        x = self.h0_to_h1_relu(x)

        # Remaining HRNet forward pass
        x = self.hrnet.conv1(x)
        x = self.hrnet.bn1(x)
        x = self.hrnet.relu(x)
        x = self.hrnet.conv2(x)
        x = self.hrnet.bn2(x)
        x = self.hrnet.relu(x)
        h1 = self.hrnet.layer1(x)  # First resolution (single scale)

        # Stage 2
        x_list = []
        for i in range(2):
            if self.hrnet.transition1[i] is not None:
                x_list.append(self.hrnet.transition1[i](h1))
            else:
                x_list.append(h1)
        y_list = self.hrnet.stage2(x_list)
        h2 = self._merge_multi_scale(y_list)

        # Stage 3
        x_list = []
        for i in range(3):
            if self.hrnet.transition2[i] is not None:
                x_list.append(self.hrnet.transition2[i](y_list[-1]))
            else:
                x_list.append(y_list[i])
        y_list = self.hrnet.stage3(x_list)
        h3 = self._merge_multi_scale(y_list)

        # Stage 4
        x_list = []
        for i in range(4):
            if self.hrnet.transition3[i] is not None:
                x_list.append(self.hrnet.transition3[i](y_list[-1]))
            else:
                x_list.append(y_list[i])
        y_list = self.hrnet.stage4(x_list)
        h4 = self._merge_multi_scale(y_list)

        return h0, h1, h2, h3, h4


    def freeze_hrnet(self):
        """Freeze the HRNet part of the model."""
        for param in self.hrnet.parameters():
            param.requires_grad = False

    def unfreeze_hrnet(self):
        """Unfreeze the HRNet part of the model."""
        for param in self.hrnet.parameters():
            param.requires_grad = True
            
    def _merge_multi_scale(self, features):
        """
        Merge multi-scale outputs into a single feature map by downsampling all to the lowest resolution.
        Args:
            features (list[torch.Tensor]): Multi-scale feature maps.
        Returns:
            torch.Tensor: Merged feature map.
        """
        # Determine the target size (lowest resolution in the list)
        target_size = features[-1].size()[2:]  # Height and Width of the last feature (lowest resolution)

        # Downsample all features to the target size and concatenate
        merged = torch.cat(
            [nn.functional.interpolate(feat, size=target_size, mode='bilinear', align_corners=True) if feat.size()[2:] != target_size else feat
             for feat in features],
            dim=1  # Concatenate along the channel dimension
        )
        return merged
    

class UNet3PlusHRNet(nn.Module):
    def __init__(self, in_channels=3, n_classes=1,
                 hrnet_config_file="/data/ephemeral/home/MCG/level2-cv-semanticsegmentation-cv-15-lv3/UNet3+/Code/HRNet/experiments/w64.yaml",
                 pretrained_weights="/data/ephemeral/home/MCG/hrnetv2_w64_imagenet_pretrained.pth"):
        super(UNet3PlusHRNet, self).__init__()

        filters = [64, 256, 192, 448, 960]

        # Define HRNet stages as encoder
        self.encoder=HRNetEncoder(hrnet_config_file=hrnet_config_file, pretrained_weights=pretrained_weights)



        ## -------------Decoder--------------
        self.CatChannels = filters[0]
        self.CatBlocks = 5
        self.UpChannels = self.CatChannels * self.CatBlocks

        '''stage 4d'''
        # h1->320*320, hd4->40*40, Pooling 8 times
        self.h1_PT_hd4 = nn.MaxPool2d(16, 16, ceil_mode=True)
        self.h1_PT_hd4_conv = nn.Conv2d(filters[0], self.CatChannels, 3, padding=1)
        self.h1_PT_hd4_bn = nn.BatchNorm2d(self.CatChannels)
        self.h1_PT_hd4_relu = nn.ReLU(inplace=True)

        # h2->160*160, hd4->40*40, Pooling 4 times
        self.h2_PT_hd4 = nn.MaxPool2d(4, 4, ceil_mode=True)
        self.h2_PT_hd4_conv = nn.Conv2d(filters[1], self.CatChannels, 3, padding=1)
        self.h2_PT_hd4_bn = nn.BatchNorm2d(self.CatChannels)
        self.h2_PT_hd4_relu = nn.ReLU(inplace=True)

        # h3->80*80, hd4->40*40, Pooling 2 times
        self.h3_PT_hd4 = nn.MaxPool2d(2, 2, ceil_mode=True)
        self.h3_PT_hd4_conv = nn.Conv2d(filters[2], self.CatChannels, 3, padding=1)
        self.h3_PT_hd4_bn = nn.BatchNorm2d(self.CatChannels)
        self.h3_PT_hd4_relu = nn.ReLU(inplace=True)

        # h4->40*40, hd4->40*40, Concatenation
        self.h4_Cat_hd4_conv = nn.Conv2d(filters[3], self.CatChannels, 3, padding=1)
        self.h4_Cat_hd4_bn = nn.BatchNorm2d(self.CatChannels)
        self.h4_Cat_hd4_relu = nn.ReLU(inplace=True)

        # hd5->20*20, hd4->40*40, Upsample 2 times (Using ConvTranspose2d)
        self.hd5_UT_hd4 = nn.Upsample(scale_factor=2, mode='bilinear')  # 14*14
        self.hd5_UT_hd4_conv = nn.Conv2d(filters[4], self.CatChannels, 3, padding=1)
        self.hd5_UT_hd4_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd5_UT_hd4_relu = nn.ReLU(inplace=True)

        # fusion(h1_PT_hd4, h2_PT_hd4, h3_PT_hd4, h4_Cat_hd4, hd5_UT_hd4)
        self.conv4d_1 = nn.Conv2d(self.UpChannels, self.UpChannels, 3, padding=1)  # 16
        self.bn4d_1 = nn.BatchNorm2d(self.UpChannels)
        self.relu4d_1 = nn.ReLU(inplace=True)


        '''stage 3d'''
        # h1->320*320, hd3->80*80, Pooling 4 times
        self.h1_PT_hd3 = nn.MaxPool2d(8, 8, ceil_mode=True)
        self.h1_PT_hd3_conv = nn.Conv2d(filters[0], self.CatChannels, 3, padding=1)
        self.h1_PT_hd3_bn = nn.BatchNorm2d(self.CatChannels)
        self.h1_PT_hd3_relu = nn.ReLU(inplace=True)

        # h2->160*160, hd3->80*80, Pooling 2 times
        self.h2_PT_hd3 = nn.MaxPool2d(2, 2, ceil_mode=True)
        self.h2_PT_hd3_conv = nn.Conv2d(filters[1], self.CatChannels, 3, padding=1)
        self.h2_PT_hd3_bn = nn.BatchNorm2d(self.CatChannels)
        self.h2_PT_hd3_relu = nn.ReLU(inplace=True)

        # h3->80*80, hd3->80*80, Concatenation
        self.h3_Cat_hd3_conv = nn.Conv2d(filters[2], self.CatChannels, 3, padding=1)
        self.h3_Cat_hd3_bn = nn.BatchNorm2d(self.CatChannels)
        self.h3_Cat_hd3_relu = nn.ReLU(inplace=True)

        # hd4->40*40, hd4->80*80, Upsample 2 times (Using ConvTranspose2d)
        self.hd4_UT_hd3 = nn.Upsample(scale_factor=2, mode='bilinear')  # 14*14
        self.hd4_UT_hd3_conv = nn.Conv2d(self.UpChannels, self.CatChannels, 3, padding=1)
        self.hd4_UT_hd3_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd4_UT_hd3_relu = nn.ReLU(inplace=True)

        # hd5->20*20, hd4->80*80, Upsample 4 times (Using ConvTranspose2d)
        self.hd5_UT_hd3 = nn.Upsample(scale_factor=4, mode='bilinear')  # 14*14
        self.hd5_UT_hd3_conv = nn.Conv2d(filters[4], self.CatChannels, 3, padding=1)
        self.hd5_UT_hd3_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd5_UT_hd3_relu = nn.ReLU(inplace=True)

        # fusion(h1_PT_hd3, h2_PT_hd3, h3_Cat_hd3, hd4_UT_hd3, hd5_UT_hd3)
        self.conv3d_1 = nn.Conv2d(self.UpChannels, self.UpChannels, 3, padding=1)  # 16
        self.bn3d_1 = nn.BatchNorm2d(self.UpChannels)
        self.relu3d_1 = nn.ReLU(inplace=True)


        '''stage 2d '''
        # h1->320*320, hd2->160*160, Pooling 2 times
        self.h1_PT_hd2 = nn.MaxPool2d(4, 4, ceil_mode=True)
        self.h1_PT_hd2_conv = nn.Conv2d(filters[0], self.CatChannels, 3, padding=1)
        self.h1_PT_hd2_bn = nn.BatchNorm2d(self.CatChannels)
        self.h1_PT_hd2_relu = nn.ReLU(inplace=True)

        # h2->160*160, hd2->160*160, Concatenation
        self.h2_Cat_hd2_conv = nn.Conv2d(filters[1], self.CatChannels, 3, padding=1)
        self.h2_Cat_hd2_bn = nn.BatchNorm2d(self.CatChannels)
        self.h2_Cat_hd2_relu = nn.ReLU(inplace=True)

        # hd3->80*80, hd2->160*160, Upsample 2 times (Using ConvTranspose2d)
        self.hd3_UT_hd2 = nn.Upsample(scale_factor=2, mode='bilinear')  # 14*14샘플링
        self.hd3_UT_hd2_conv = nn.Conv2d(self.UpChannels, self.CatChannels, 3, padding=1)
        self.hd3_UT_hd2_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd3_UT_hd2_relu = nn.ReLU(inplace=True)

        # hd4->40*40, hd2->160*160, Upsample 4 times (Using ConvTranspose2d)
        self.hd4_UT_hd2 = nn.Upsample(scale_factor=4, mode='bilinear')  # 14*14
        self.hd4_UT_hd2_conv = nn.Conv2d(self.UpChannels, self.CatChannels, 3, padding=1)
        self.hd4_UT_hd2_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd4_UT_hd2_relu = nn.ReLU(inplace=True)

        # hd5->20*20, hd2->160*160, Upsample 8 times (Using ConvTranspose2d)
        self.hd5_UT_hd2 = nn.Upsample(scale_factor=8, mode='bilinear')  # 14*14
        self.hd5_UT_hd2_conv = nn.Conv2d(filters[4], self.CatChannels, 3, padding=1)
        self.hd5_UT_hd2_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd5_UT_hd2_relu = nn.ReLU(inplace=True)

        # fusion(h1_PT_hd2, h2_Cat_hd2, hd3_UT_hd2, hd4_UT_hd2, hd5_UT_hd2)
        self.conv2d_1 = nn.Conv2d(self.UpChannels, self.UpChannels, 3, padding=1)  # 16
        self.bn2d_1 = nn.BatchNorm2d(self.UpChannels)
        self.relu2d_1 = nn.ReLU(inplace=True)


        '''stage 1d'''
        # h1->320*320, hd1->320*320, Concatenation
        self.h1_Cat_hd1_conv = nn.Conv2d(filters[0], self.CatChannels, 3, padding=1)
        self.h1_Cat_hd1_bn = nn.BatchNorm2d(self.CatChannels)
        self.h1_Cat_hd1_relu = nn.ReLU(inplace=True)

        # hd2->160*160, hd1->320*320, Upsample 2 times (Using ConvTranspose2d)
        self.hd2_UT_hd1 = nn.Upsample(scale_factor=4, mode='bilinear')  # 14*14
        self.hd2_UT_hd1_conv = nn.Conv2d(self.UpChannels, self.CatChannels, 3, padding=1)
        self.hd2_UT_hd1_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd2_UT_hd1_relu = nn.ReLU(inplace=True)

        # hd3->80*80, hd1->320*320, Upsample 4 times (Using ConvTranspose2d)
        self.hd3_UT_hd1 = nn.Upsample(scale_factor=8, mode='bilinear')  # 14*14
        self.hd3_UT_hd1_conv = nn.Conv2d(self.UpChannels, self.CatChannels, 3, padding=1)
        self.hd3_UT_hd1_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd3_UT_hd1_relu = nn.ReLU(inplace=True)

        # hd4->40*40, hd1->320*320, Upsample 8 times (Using ConvTranspose2d)
        self.hd4_UT_hd1 = nn.Upsample(scale_factor=16, mode='bilinear')  # 14*14
        self.hd4_UT_hd1_conv = nn.Conv2d(self.UpChannels, self.CatChannels, 3, padding=1)
        self.hd4_UT_hd1_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd4_UT_hd1_relu = nn.ReLU(inplace=True)

        # hd5->20*20, hd1->320*320, Upsample 16 times (Using ConvTranspose2d)
        self.hd5_UT_hd1 = nn.Upsample(scale_factor=32, mode='bilinear')  # 14*14
        self.hd5_UT_hd1_conv = nn.Conv2d(filters[4], self.CatChannels, 3, padding=1)
        self.hd5_UT_hd1_bn = nn.BatchNorm2d(self.CatChannels)
        self.hd5_UT_hd1_relu = nn.ReLU(inplace=True)

        # fusion(h1_Cat_hd1, hd2_UT_hd1, hd3_UT_hd1, hd4_UT_hd1, hd5_UT_hd1)
        self.conv1d_1 = nn.Conv2d(self.UpChannels, self.UpChannels, 3, padding=1)  # 16
        self.bn1d_1 = nn.BatchNorm2d(self.UpChannels)
        self.relu1d_1 = nn.ReLU(inplace=True)


        # -------------Bilinear Upsampling--------------
        # -------------Learnable Upsampling using ConvTranspose2d--------------
        self.upscore5 = nn.Upsample(size=(IMSIZE, IMSIZE), mode='bilinear', align_corners=True)  # 512x512로 고정
        self.upscore4 = nn.Upsample(size=(IMSIZE, IMSIZE), mode='bilinear', align_corners=True)  # 512x512로 고정
        self.upscore3 = nn.Upsample(size=(IMSIZE, IMSIZE), mode='bilinear', align_corners=True)  # 512x512로 고정
        self.upscore2 = nn.Upsample(size=(IMSIZE, IMSIZE), mode='bilinear', align_corners=True)  # 512x512로 고정
        self.upscore1 = nn.Upsample(size=(IMSIZE, IMSIZE), mode='bilinear', align_corners=True)  # 512x512로 고정


        # DeepSup
        self.outconv1 = nn.Conv2d(self.UpChannels, n_classes, 3, padding=1)
        self.outconv2 = nn.Conv2d(self.UpChannels, n_classes, 3, padding=1)
        self.outconv3 = nn.Conv2d(self.UpChannels, n_classes, 3, padding=1)
        self.outconv4 = nn.Conv2d(self.UpChannels, n_classes, 3, padding=1)
        self.outconv5 = nn.Conv2d(filters[4], n_classes, 3, padding=1)
        
        self.cls = nn.Sequential(
            nn.Dropout(p=0.3),               # Dropout으로 오버피팅 방지
            nn.Conv2d(filters[4], n_classes, 1),  # 클래스 수 반영
            nn.AdaptiveMaxPool2d(1),         # 클래스별 전역 정보 추출
            nn.Sigmoid()                     # 멀티라벨 환경에서 클래스 존재 확률 출력
        )
        self.encoder_ids = {id(module) for module in self.encoder.modules()}
        self.final_conv = nn.Conv2d(in_channels=n_classes*5, out_channels=n_classes, kernel_size=1)

        self._initialize_weights()

    def _initialize_weights(self):
        """
        Initialize weights for the decoder layers only, excluding the encoder.
        """
        for module in self.modules():
            # Skip initialization if the module belongs to the encoder
            if id(module) in self.encoder_ids:
                continue
            if isinstance(module, nn.Conv2d):
                init_weights(module, init_type='kaiming')
            elif isinstance(module, nn.BatchNorm2d):
                init_weights(module, init_type='kaiming')
            elif isinstance(module, nn.ConvTranspose2d):
                init_weights(module, init_type='kaiming')
    


    
    def dotProduct(self,seg,cls):
        B, N, H, W = seg.size()
        seg = seg.view(B, N, H * W)
        final = torch.einsum("ijk,ij->ijk", [seg, cls])
        final = final.view(B, N, H, W)
        return final
    
    def forward(self, inputs):
        ## -------------Encoder-------------
        #print(f"inputs shape: {inputs.shape}")
        h1,h2,h3,h4,hd5 = self.encoder(inputs) 

        # -------------Classification-------------
        cls_branch = self.cls(hd5).squeeze(3).squeeze(2)  # (B, N, 1, 1) -> (B, N)
        
        
        ## -------------Decoder-------------
        h1_PT_hd4 = self.h1_PT_hd4_relu(self.h1_PT_hd4_bn(self.h1_PT_hd4_conv(self.h1_PT_hd4(h1))))
        h2_PT_hd4 = self.h2_PT_hd4_relu(self.h2_PT_hd4_bn(self.h2_PT_hd4_conv(self.h2_PT_hd4(h2))))
        h3_PT_hd4 = self.h3_PT_hd4_relu(self.h3_PT_hd4_bn(self.h3_PT_hd4_conv(self.h3_PT_hd4(h3))))
        h4_Cat_hd4 = self.h4_Cat_hd4_relu(self.h4_Cat_hd4_bn(self.h4_Cat_hd4_conv(h4)))
        hd5_UT_hd4 = self.hd5_UT_hd4_relu(self.hd5_UT_hd4_bn(self.hd5_UT_hd4_conv(self.hd5_UT_hd4(hd5))))


        hd4 = self.relu4d_1(self.bn4d_1(self.conv4d_1(
            torch.cat((h1_PT_hd4, h2_PT_hd4, h3_PT_hd4, h4_Cat_hd4, hd5_UT_hd4), 1)))) # hd4->40*40*UpChannels

        h1_PT_hd3 = self.h1_PT_hd3_relu(self.h1_PT_hd3_bn(self.h1_PT_hd3_conv(self.h1_PT_hd3(h1))))
        h2_PT_hd3 = self.h2_PT_hd3_relu(self.h2_PT_hd3_bn(self.h2_PT_hd3_conv(self.h2_PT_hd3(h2))))
        h3_Cat_hd3 = self.h3_Cat_hd3_relu(self.h3_Cat_hd3_bn(self.h3_Cat_hd3_conv(h3)))
        hd4_UT_hd3 = self.hd4_UT_hd3_relu(self.hd4_UT_hd3_bn(self.hd4_UT_hd3_conv(self.hd4_UT_hd3(hd4))))
        hd5_UT_hd3 = self.hd5_UT_hd3_relu(self.hd5_UT_hd3_bn(self.hd5_UT_hd3_conv(self.hd5_UT_hd3(hd5))))
        hd3 = self.relu3d_1(self.bn3d_1(self.conv3d_1(
            torch.cat((h1_PT_hd3, h2_PT_hd3, h3_Cat_hd3, hd4_UT_hd3, hd5_UT_hd3), 1)))) # hd3->80*80*UpChannels

        h1_PT_hd2 = self.h1_PT_hd2_relu(self.h1_PT_hd2_bn(self.h1_PT_hd2_conv(self.h1_PT_hd2(h1))))
        h2_Cat_hd2 = self.h2_Cat_hd2_relu(self.h2_Cat_hd2_bn(self.h2_Cat_hd2_conv(h2)))
        hd3_UT_hd2 = self.hd3_UT_hd2_relu(self.hd3_UT_hd2_bn(self.hd3_UT_hd2_conv(self.hd3_UT_hd2(hd3))))
        hd4_UT_hd2 = self.hd4_UT_hd2_relu(self.hd4_UT_hd2_bn(self.hd4_UT_hd2_conv(self.hd4_UT_hd2(hd4))))
        hd5_UT_hd2 = self.hd5_UT_hd2_relu(self.hd5_UT_hd2_bn(self.hd5_UT_hd2_conv(self.hd5_UT_hd2(hd5))))
        hd2 = self.relu2d_1(self.bn2d_1(self.conv2d_1(
            torch.cat((h1_PT_hd2, h2_Cat_hd2, hd3_UT_hd2, hd4_UT_hd2, hd5_UT_hd2), 1)))) # hd2->160*160*UpChannels

        h1_Cat_hd1 = self.h1_Cat_hd1_relu(self.h1_Cat_hd1_bn(self.h1_Cat_hd1_conv(h1)))
        hd2_UT_hd1 = self.hd2_UT_hd1_relu(self.hd2_UT_hd1_bn(self.hd2_UT_hd1_conv(self.hd2_UT_hd1(hd2))))
        hd3_UT_hd1 = self.hd3_UT_hd1_relu(self.hd3_UT_hd1_bn(self.hd3_UT_hd1_conv(self.hd3_UT_hd1(hd3))))
        hd4_UT_hd1 = self.hd4_UT_hd1_relu(self.hd4_UT_hd1_bn(self.hd4_UT_hd1_conv(self.hd4_UT_hd1(hd4))))
        hd5_UT_hd1 = self.hd5_UT_hd1_relu(self.hd5_UT_hd1_bn(self.hd5_UT_hd1_conv(self.hd5_UT_hd1(hd5))))
        hd1 = self.relu1d_1(self.bn1d_1(self.conv1d_1(
            torch.cat((h1_Cat_hd1, hd2_UT_hd1, hd3_UT_hd1, hd4_UT_hd1, hd5_UT_hd1), 1)))) # hd1->320*320*UpChannels

        d5 = self.outconv5(hd5)
        d5 = self.upscore5(d5) # 16->256

        d4 = self.outconv4(hd4)
        d4 = self.upscore4(d4) # 32->256

        d3 = self.outconv3(hd3)
        d3 = self.upscore3(d3) # 64->256

        d2 = self.outconv2(hd2)
        d2 = self.upscore2(d2) # 128->256

        d1 = self.outconv1(hd1) # 256
        
        d1 = self.dotProduct(d1, cls_branch)
        d2 = self.dotProduct(d2, cls_branch)
        d3 = self.dotProduct(d3, cls_branch)
        d4 = self.dotProduct(d4, cls_branch)
        d5 = self.dotProduct(d5, cls_branch)
        
        
        outputs = [d1,d2,d3,d4,d5]
        '''for i, output in enumerate(outputs):
            print(f"Output {i} shape: {output.shape}")'''
        if self.training:
            return torch.cat(outputs, dim=0)
        else:
            return outputs[0]