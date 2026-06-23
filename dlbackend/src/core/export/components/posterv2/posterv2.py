import torch
from torch import nn
from torch.nn import functional as F

from core.export.components.posterv2.ir50 import Backbone
from core.export.components.posterv2.mobilefacenet import MobileFaceNet
from core.export.components.posterv2.utils import (
    FeedForward,
    Window,
    WindowAttentionGlobal,
    to_channel_first,
    to_channel_last,
    to_query,
)
from core.export.components.posterv2.vit import PatchEmbed, VisionTransformer


class Posterv2(nn.Module):
    def __init__(
        self,
        img_size=224,
        num_classes=7,
        window_size=[28, 14, 7],
        num_heads=[2, 4, 8],
        dims=[64, 128, 256],
        embed_dim=768,
    ):
        super().__init__()

        self.img_size = img_size
        self.num_heads = num_heads
        self.dim_head = []
        for num_head, dim in zip(num_heads, dims):
            self.dim_head.append(dim // num_head)
        self.num_classes = num_classes
        self.window_size = window_size
        self.N = [win * win for win in window_size]
        self.face_landback = MobileFaceNet([112, 112], 136)

        for param in self.face_landback.parameters():
            param.requires_grad = False

        self.VIT = VisionTransformer(depth=2, embed_dim=embed_dim)

        self.ir_back = Backbone(50, 0.0, "ir")

        self.attn1 = WindowAttentionGlobal(
            dim=dims[0], num_heads=num_heads[0], window_size=window_size[0]
        )
        self.attn2 = WindowAttentionGlobal(
            dim=dims[1], num_heads=num_heads[1], window_size=window_size[1]
        )
        self.attn3 = WindowAttentionGlobal(
            dim=dims[2], num_heads=num_heads[2], window_size=window_size[2]
        )
        self.window1 = Window(window_size=window_size[0], dim=dims[0])
        self.window2 = Window(window_size=window_size[1], dim=dims[1])
        self.window3 = Window(window_size=window_size[2], dim=dims[2])
        self.conv1 = nn.Conv2d(
            in_channels=dims[0],
            out_channels=dims[0],
            kernel_size=3,
            stride=2,
            padding=1,
        )
        self.conv2 = nn.Conv2d(
            in_channels=dims[1],
            out_channels=dims[1],
            kernel_size=3,
            stride=2,
            padding=1,
        )
        self.conv3 = nn.Conv2d(
            in_channels=dims[2],
            out_channels=dims[2],
            kernel_size=3,
            stride=2,
            padding=1,
        )

        dpr = [x.item() for x in torch.linspace(0, 0.5, 5)]
        self.ffn1 = FeedForward(
            dim=dims[0], window_size=window_size[0], layer_scale=1e-5, drop_path=dpr[0]
        )
        self.ffn2 = FeedForward(
            dim=dims[1], window_size=window_size[1], layer_scale=1e-5, drop_path=dpr[1]
        )
        self.ffn3 = FeedForward(
            dim=dims[2], window_size=window_size[2], layer_scale=1e-5, drop_path=dpr[2]
        )

        self.last_face_conv = nn.Conv2d(in_channels=512, out_channels=256, kernel_size=3, padding=1)

        self.embed_q = nn.Sequential(
            nn.Conv2d(dims[0], 768, kernel_size=3, stride=2, padding=1),
            nn.Conv2d(768, 768, kernel_size=3, stride=2, padding=1),
        )
        self.embed_k = nn.Sequential(nn.Conv2d(dims[1], 768, kernel_size=3, stride=2, padding=1))
        self.embed_v = PatchEmbed(img_size=14, patch_size=14, in_c=256, embed_dim=768)

    def forward(self, x):
        x_face = F.interpolate(x, size=112)
        x_face1, x_face2, x_face3 = self.face_landback(x_face)
        x_face3 = self.last_face_conv(x_face3)
        x_face1, x_face2, x_face3 = (
            to_channel_last(x_face1),
            to_channel_last(x_face2),
            to_channel_last(x_face3),
        )

        q1, q2, q3 = (
            to_query(x_face1, self.N[0], self.num_heads[0], self.dim_head[0]),
            to_query(x_face2, self.N[1], self.num_heads[1], self.dim_head[1]),
            to_query(x_face3, self.N[2], self.num_heads[2], self.dim_head[2]),
        )

        x_ir1, x_ir2, x_ir3 = self.ir_back(x)

        x_ir1, x_ir2, x_ir3 = self.conv1(x_ir1), self.conv2(x_ir2), self.conv3(x_ir3)
        x_window1, shortcut1 = self.window1(x_ir1)
        x_window2, shortcut2 = self.window2(x_ir2)
        x_window3, shortcut3 = self.window3(x_ir3)

        o1, o2, o3 = (
            self.attn1(x_window1, q1),
            self.attn2(x_window2, q2),
            self.attn3(x_window3, q3),
        )

        o1, o2, o3 = (
            self.ffn1(o1, shortcut1),
            self.ffn2(o2, shortcut2),
            self.ffn3(o3, shortcut3),
        )

        o1, o2, o3 = to_channel_first(o1), to_channel_first(o2), to_channel_first(o3)

        o1, o2, o3 = (
            self.embed_q(o1).flatten(2).transpose(1, 2),
            self.embed_k(o2).flatten(2).transpose(1, 2),
            self.embed_v(o3),
        )

        o = torch.cat([o1, o2, o3], dim=1)

        out = self.VIT(o)
        return out
