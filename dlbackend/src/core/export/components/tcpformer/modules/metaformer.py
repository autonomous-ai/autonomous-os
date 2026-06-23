import torch
from timm.models.layers import DropPath
from torch import nn

from .attention import Attention
from .mlp import MLP


class MetaFormerBlock(nn.Module):
    """
    Implementation of MetaFormer block.
    """

    def __init__(self, dim, mlp_ratio=4., act_layer=nn.GELU, attn_drop=0., drop=0., drop_path=0.,
                 num_heads=8, qkv_bias=False, qk_scale=None, use_layer_scale=True, layer_scale_init_value=1e-5,
                 mode='spatial', mixer_type="attention", use_temporal_similarity=True,
                 temporal_connection_len=1, tcn_dilations=(1, 2, 3, 4), tcn_kernel=3):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)
        self.mixers = nn.ModuleList()
        mixer_types = [mixer_type] if isinstance(mixer_type, str) else mixer_type
        dim_out = dim // len(mixer_types)
        for mixer_type in mixer_types:
            if mixer_type == 'attention':
                self.mixers.append(
                    Attention(dim, dim_out, num_heads, qkv_bias, qk_scale, attn_drop,
                              proj_drop=drop, mode=mode)
                )
            else:
                raise NotImplementedError("mixer_type is not supported")
        self.norm2 = nn.LayerNorm(dim)

        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = MLP(in_features=dim, hidden_features=mlp_hidden_dim,
                       act_layer=act_layer, drop=drop)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.use_layer_scale = use_layer_scale
        if use_layer_scale:
            self.layer_scale_1 = nn.Parameter(layer_scale_init_value * torch.ones(dim), requires_grad=True)
            self.layer_scale_2 = nn.Parameter(layer_scale_init_value * torch.ones(dim), requires_grad=True)

    def mixer_forward(self, x):
        mixer_outs = []
        for mixer in self.mixers:
            out = mixer(x)
            mixer_outs.append(out)
        out = torch.cat(mixer_outs, dim=-1)
        return out

    def forward(self, x):
        """
        x: tensor with shape [B, T, J, C]
        """
        if self.use_layer_scale:
            x = x + self.drop_path(
                self.layer_scale_1.unsqueeze(0).unsqueeze(0)
                * self.mixer_forward(self.norm1(x)))
            x = x + self.drop_path(
                self.layer_scale_2.unsqueeze(0).unsqueeze(0)
                * self.mlp(self.norm2(x)))
        else:
            x = x + self.drop_path(self.mixer_forward(self.norm1(x)))
            x = x + self.drop_path(self.mlp(self.norm2(x)))
        return x
