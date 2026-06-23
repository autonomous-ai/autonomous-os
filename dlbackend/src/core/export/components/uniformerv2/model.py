# Copyright (c) OpenMMLab. All rights reserved.
# Modified: removed mmcv/mmengine/mmaction dependencies for standalone use.

import logging
from collections import OrderedDict
from pathlib import Path

import torch
from timm.layers import DropPath
from torch import nn

logger = logging.getLogger(__name__)

CONFIGS: dict[str, dict] = {
    "base-k400": dict(
        input_resolution=224, patch_size=16, width=768, layers=12, heads=12,
        t_size=8, dw_reduction=1.5, backbone_drop_path_rate=0.0,
        temporal_downsample=False, no_lmhra=True, double_lmhra=True,
        return_list=[8, 9, 10, 11], n_layers=4, n_dim=768, n_head=12,
        mlp_factor=4.0, drop_path_rate=0.0, mlp_dropout=[0.5, 0.5, 0.5, 0.5],
        num_classes=400, in_channels=768, dropout_ratio=0.5,
    ),
    "base-k710": dict(
        input_resolution=224, patch_size=16, width=768, layers=12, heads=12,
        t_size=8, dw_reduction=1.5, backbone_drop_path_rate=0.0,
        temporal_downsample=False, no_lmhra=True, double_lmhra=True,
        return_list=[8, 9, 10, 11], n_layers=4, n_dim=768, n_head=12,
        mlp_factor=4.0, drop_path_rate=0.0, mlp_dropout=[0.5, 0.5, 0.5, 0.5],
        num_classes=710, in_channels=768, dropout_ratio=0.5,
    ),
    "large-k400": dict(
        input_resolution=224, patch_size=14, width=1024, layers=24, heads=16,
        t_size=8, dw_reduction=1.5, backbone_drop_path_rate=0.0,
        temporal_downsample=False, no_lmhra=True, double_lmhra=True,
        return_list=[20, 21, 22, 23], n_layers=4, n_dim=1024, n_head=16,
        mlp_factor=4.0, drop_path_rate=0.0, mlp_dropout=[0.5, 0.5, 0.5, 0.5],
        num_classes=400, in_channels=1024, dropout_ratio=0.5,
    ),
}

CHECKPOINT_MAP: dict[str, str] = {
    "base-k400": "uniformerv2-b-224-k400.pth",
    "base-k710": "uniformerv2-b-224-k710.pth",
    "large-k400": "uniformerv2-l-224-k400.pth",
}


class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * torch.sigmoid(1.702 * x)


class Local_MHRA(nn.Module):
    def __init__(self, d_model: int, dw_reduction: float = 1.5,
                 pos_kernel_size: int = 3) -> None:
        super().__init__()
        padding = pos_kernel_size // 2
        re_d_model = int(d_model // dw_reduction)
        self.pos_embed = nn.Sequential(
            nn.BatchNorm3d(d_model),
            nn.Conv3d(d_model, re_d_model, kernel_size=1, stride=1, padding=0),
            nn.Conv3d(re_d_model, re_d_model,
                      kernel_size=(pos_kernel_size, 1, 1),
                      stride=(1, 1, 1), padding=(padding, 0, 0),
                      groups=re_d_model),
            nn.Conv3d(re_d_model, d_model, kernel_size=1, stride=1, padding=0),
        )
        nn.init.constant_(self.pos_embed[3].weight, 0)
        nn.init.constant_(self.pos_embed[3].bias, 0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.pos_embed(x)


class ResidualAttentionBlock(nn.Module):
    def __init__(self, d_model: int, n_head: int, drop_path: float = 0.0,
                 dw_reduction: float = 1.5, no_lmhra: bool = False,
                 double_lmhra: bool = True) -> None:
        super().__init__()
        self.n_head = n_head
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()

        self.no_lmhra = no_lmhra
        self.double_lmhra = double_lmhra
        if not no_lmhra:
            self.lmhra1 = Local_MHRA(d_model, dw_reduction=dw_reduction)
            if double_lmhra:
                self.lmhra2 = Local_MHRA(d_model, dw_reduction=dw_reduction)

        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = nn.LayerNorm(d_model)
        self.mlp = nn.Sequential(
            OrderedDict([("c_fc", nn.Linear(d_model, d_model * 4)),
                         ("gelu", QuickGELU()),
                         ("c_proj", nn.Linear(d_model * 4, d_model))]))
        self.ln_2 = nn.LayerNorm(d_model)

    def attention(self, x: torch.Tensor) -> torch.Tensor:
        return self.attn(x, x, x, need_weights=False, attn_mask=None)[0]

    def forward(self, x: torch.Tensor, T: int = 8) -> torch.Tensor:
        if not self.no_lmhra:
            tmp_x = x[1:, :, :]
            L, NT, C = tmp_x.shape
            N = NT // T
            H = W = int(L**0.5)
            tmp_x = tmp_x.view(H, W, N, T, C).permute(2, 4, 3, 0, 1).contiguous()
            tmp_x = tmp_x + self.drop_path(self.lmhra1(tmp_x))
            tmp_x = tmp_x.view(N, C, T, L).permute(3, 0, 2, 1).contiguous().view(L, NT, C)
            x = torch.cat([x[:1, :, :], tmp_x], dim=0)

        x = x + self.drop_path(self.attention(self.ln_1(x)))

        if not self.no_lmhra and self.double_lmhra:
            tmp_x = x[1:, :, :]
            tmp_x = tmp_x.view(H, W, N, T, C).permute(2, 4, 3, 0, 1).contiguous()
            tmp_x = tmp_x + self.drop_path(self.lmhra2(tmp_x))
            tmp_x = tmp_x.view(N, C, T, L).permute(3, 0, 2, 1).contiguous().view(L, NT, C)
            x = torch.cat([x[:1, :, :], tmp_x], dim=0)

        x = x + self.drop_path(self.mlp(self.ln_2(x)))
        return x


class Extractor(nn.Module):
    def __init__(self, d_model: int, n_head: int, mlp_factor: float = 4.0,
                 dropout: float = 0.0, drop_path: float = 0.0) -> None:
        super().__init__()
        self.drop_path = DropPath(drop_path) if drop_path > 0.0 else nn.Identity()
        self.attn = nn.MultiheadAttention(d_model, n_head)
        self.ln_1 = nn.LayerNorm(d_model)
        d_mlp = round(mlp_factor * d_model)
        self.mlp = nn.Sequential(
            OrderedDict([("c_fc", nn.Linear(d_model, d_mlp)),
                         ("gelu", QuickGELU()),
                         ("dropout", nn.Dropout(dropout)),
                         ("c_proj", nn.Linear(d_mlp, d_model))]))
        self.ln_2 = nn.LayerNorm(d_model)
        self.ln_3 = nn.LayerNorm(d_model)

        nn.init.xavier_uniform_(self.attn.in_proj_weight)
        nn.init.constant_(self.attn.out_proj.weight, 0.0)
        nn.init.constant_(self.attn.out_proj.bias, 0.0)
        nn.init.xavier_uniform_(self.mlp[0].weight)
        nn.init.constant_(self.mlp[-1].weight, 0.0)
        nn.init.constant_(self.mlp[-1].bias, 0.0)

    def attention(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        d_model = self.ln_1.weight.size(0)
        q = (x @ self.attn.in_proj_weight[:d_model].T) + self.attn.in_proj_bias[:d_model]
        k = (y @ self.attn.in_proj_weight[d_model:-d_model].T) + self.attn.in_proj_bias[d_model:-d_model]
        v = (y @ self.attn.in_proj_weight[-d_model:].T) + self.attn.in_proj_bias[-d_model:]
        Tx, Ty, N = q.size(0), k.size(0), q.size(1)
        q = q.view(Tx, N, self.attn.num_heads, self.attn.head_dim).permute(1, 2, 0, 3)
        k = k.view(Ty, N, self.attn.num_heads, self.attn.head_dim).permute(1, 2, 0, 3)
        v = v.view(Ty, N, self.attn.num_heads, self.attn.head_dim).permute(1, 2, 0, 3)
        aff = (q @ k.transpose(-2, -1) / (self.attn.head_dim**0.5))
        aff = aff.softmax(dim=-1)
        out = aff @ v
        out = out.permute(2, 0, 1, 3).flatten(2)
        out = self.attn.out_proj(out)
        return out

    def forward(self, x: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        x = x + self.drop_path(self.attention(self.ln_1(x), self.ln_3(y)))
        x = x + self.drop_path(self.mlp(self.ln_2(x)))
        return x


class Transformer(nn.Module):
    def __init__(self, width: int, layers: int, heads: int,
                 backbone_drop_path_rate: float = 0.0, t_size: int = 8,
                 dw_reduction: float = 1.5, no_lmhra: bool = True,
                 double_lmhra: bool = False,
                 return_list: list[int] = [8, 9, 10, 11],
                 n_layers: int = 4, n_dim: int = 768, n_head: int = 12,
                 mlp_factor: float = 4.0, drop_path_rate: float = 0.0,
                 mlp_dropout: list[float] = [0.5, 0.5, 0.5, 0.5]) -> None:
        super().__init__()
        self.T = t_size
        self.return_list = return_list

        b_dpr = [x.item() for x in torch.linspace(0, backbone_drop_path_rate, layers)]
        self.resblocks = nn.ModuleList([
            ResidualAttentionBlock(width, heads, drop_path=b_dpr[i],
                                  dw_reduction=dw_reduction, no_lmhra=no_lmhra,
                                  double_lmhra=double_lmhra)
            for i in range(layers)
        ])

        assert n_layers == len(return_list)
        self.temporal_cls_token = nn.Parameter(torch.zeros(1, 1, n_dim))
        self.dpe = nn.ModuleList([
            nn.Conv3d(n_dim, n_dim, kernel_size=3, stride=1, padding=1,
                      bias=True, groups=n_dim)
            for _ in range(n_layers)
        ])
        for m in self.dpe:
            nn.init.constant_(m.bias, 0.0)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, n_layers)]
        self.dec = nn.ModuleList([
            Extractor(n_dim, n_head, mlp_factor=mlp_factor,
                      dropout=mlp_dropout[i], drop_path=dpr[i])
            for i in range(n_layers)
        ])

        self.norm = nn.LayerNorm(n_dim)
        self.balance = nn.Parameter(torch.zeros((n_dim)))
        self.sigmoid = nn.Sigmoid()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        T_down = self.T
        L, NT, C = x.shape
        N = NT // T_down
        H = W = int((L - 1)**0.5)
        cls_token = self.temporal_cls_token.repeat(1, N, 1)

        j = -1
        for i, resblock in enumerate(self.resblocks):
            x = resblock(x, T_down)
            if i in self.return_list:
                j += 1
                tmp_x = x.clone()
                tmp_x = tmp_x.view(L, N, T_down, C)
                _, tmp_feats = tmp_x[:1], tmp_x[1:]
                tmp_feats = tmp_feats.permute(1, 3, 2, 0).reshape(N, C, T_down, H, W)
                tmp_feats = self.dpe[j](tmp_feats.clone()).view(
                    N, C, T_down, L - 1).permute(3, 0, 2, 1).contiguous()
                tmp_x[1:] = tmp_x[1:] + tmp_feats
                tmp_x = tmp_x.permute(2, 0, 1, 3).flatten(0, 1)
                cls_token = self.dec[j](cls_token, tmp_x)

        weight = self.sigmoid(self.balance)
        residual = x.view(L, N, T_down, C)[0].mean(1)
        out = self.norm((1 - weight) * cls_token[0, :, :] + weight * residual)
        return out


class UniformerV2Backbone(nn.Module):
    def __init__(self, input_resolution: int = 224, patch_size: int = 16,
                 width: int = 768, layers: int = 12, heads: int = 12,
                 backbone_drop_path_rate: float = 0.0, t_size: int = 8,
                 kernel_size: int = 3, dw_reduction: float = 1.5,
                 temporal_downsample: bool = False, no_lmhra: bool = True,
                 double_lmhra: bool = False,
                 return_list: list[int] = [8, 9, 10, 11],
                 n_layers: int = 4, n_dim: int = 768, n_head: int = 12,
                 mlp_factor: float = 4.0, drop_path_rate: float = 0.0,
                 mlp_dropout: list[float] = [0.5, 0.5, 0.5, 0.5]) -> None:
        super().__init__()
        self.input_resolution = input_resolution
        padding = (kernel_size - 1) // 2
        if temporal_downsample:
            self.conv1 = nn.Conv3d(
                3, width, (kernel_size, patch_size, patch_size),
                (2, patch_size, patch_size), (padding, 0, 0), bias=False)
            t_size = t_size // 2
        else:
            self.conv1 = nn.Conv3d(
                3, width, (1, patch_size, patch_size),
                (1, patch_size, patch_size), (0, 0, 0), bias=False)

        scale = width**-0.5
        self.class_embedding = nn.Parameter(scale * torch.randn(width))
        self.positional_embedding = nn.Parameter(
            scale * torch.randn((input_resolution // patch_size)**2 + 1, width))
        self.ln_pre = nn.LayerNorm(width)

        self.transformer = Transformer(
            width, layers, heads, dw_reduction=dw_reduction,
            backbone_drop_path_rate=backbone_drop_path_rate, t_size=t_size,
            no_lmhra=no_lmhra, double_lmhra=double_lmhra,
            return_list=return_list, n_layers=n_layers, n_dim=n_dim,
            n_head=n_head, mlp_factor=mlp_factor,
            drop_path_rate=drop_path_rate, mlp_dropout=mlp_dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.conv1(x)
        N, C, T, H, W = x.shape
        x = x.permute(0, 2, 3, 4, 1).reshape(N * T, H * W, C)
        x = torch.cat([
            self.class_embedding.to(x.dtype) + torch.zeros(
                x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device),
            x], dim=1)
        x = x + self.positional_embedding.to(x.dtype)
        x = self.ln_pre(x)
        x = x.permute(1, 0, 2)
        out = self.transformer(x)
        return out


class UniformerHead(nn.Module):
    def __init__(self, in_channels: int, num_classes: int,
                 dropout_ratio: float = 0.5) -> None:
        super().__init__()
        self.in_channels = in_channels
        self.num_classes = num_classes
        self.dropout = nn.Dropout(p=dropout_ratio) if dropout_ratio > 0 else None
        self.fc_cls = nn.Linear(in_channels, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if self.dropout is not None:
            x = self.dropout(x)
        return self.fc_cls(x)


class UniformerV2(nn.Module):
    """Standalone UniformerV2 (backbone + classification head).

    State dict keys match mmaction2 structure (backbone.*, cls_head.*)
    so existing checkpoints load directly.
    """

    def __init__(self, backbone: UniformerV2Backbone,
                 cls_head: UniformerHead) -> None:
        super().__init__()
        self.backbone = backbone
        self.cls_head = cls_head

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # (B, NC, C, T, H, W) → (B*NC, C, T, H, W)
        B = x.shape[0]
        x = x.reshape((-1,) + x.shape[2:])
        feat = self.backbone(x)
        logits = self.cls_head(feat)
        return logits

    @classmethod
    def from_config(cls, config_name: str) -> "UniformerV2":
        cfg = CONFIGS[config_name]
        backbone_keys = {
            "input_resolution", "patch_size", "width", "layers", "heads",
            "t_size", "dw_reduction", "backbone_drop_path_rate",
            "temporal_downsample", "no_lmhra", "double_lmhra", "return_list",
            "n_layers", "n_dim", "n_head", "mlp_factor", "drop_path_rate",
            "mlp_dropout",
        }
        backbone = UniformerV2Backbone(**{k: v for k, v in cfg.items() if k in backbone_keys})
        head = UniformerHead(
            in_channels=cfg["in_channels"],
            num_classes=cfg["num_classes"],
            dropout_ratio=cfg["dropout_ratio"],
        )
        return cls(backbone, head)

    @classmethod
    def load_from_checkpoint(cls, config_name: str,
                             checkpoint: Path) -> "UniformerV2":
        model = cls.from_config(config_name)

        ckpt = torch.load(str(checkpoint), map_location="cpu", weights_only=False)
        state_dict = ckpt.get("state_dict", ckpt)

        # Filter out data_preprocessor keys
        state_dict = {k: v for k, v in state_dict.items()
                      if not k.startswith("data_preprocessor.")}

        missing, unexpected = model.load_state_dict(state_dict, strict=False)
        if missing:
            logger.warning("Missing keys (%d): %s...", len(missing), missing[:5])
        if unexpected:
            logger.warning("Unexpected keys (%d): %s...", len(unexpected), unexpected[:5])

        model.eval()
        return model
