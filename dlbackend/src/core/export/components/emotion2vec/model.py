#!/usr/bin/env python3
# -*- encoding: utf-8 -*-
# Copyright FunASR (https://github.com/alibaba-damo-academy/FunASR). All Rights Reserved.
#  MIT License  (https://opensource.org/licenses/MIT)
# Modified from https://github.com/ddlBoJack/emotion2vec/tree/main

import logging
from contextlib import contextmanager
from functools import partial

import numpy as np
import torch
from distutils.version import LooseVersion
from omegaconf import OmegaConf

from .audio import AudioEncoder
from .modules import AltBlock

logger = logging.getLogger(__name__)
if LooseVersion(torch.__version__) >= LooseVersion("1.6.0"):
    from torch.cuda.amp import autocast
else:
    # Nothing to do if torch<1.6.0
    @contextmanager
    def autocast(enabled=True):
        """Autocast.
        
            Args:
                enabled: TODO.
            """
        yield


class Emotion2vec(torch.nn.Module):
    """
    Author: Ziyang Ma, Zhisheng Zheng, Jiaxin Ye, Jinchao Li, Zhifu Gao, Shiliang Zhang, Xie Chen
    emotion2vec: Self-Supervised Pre-Training for Speech Emotion Representation
    https://arxiv.org/abs/2312.15185
    """

    def __init__(self, **kwargs):
        """Initialize Emotion2vec.
        
            Args:
                **kwargs: Additional keyword arguments.
            """
        super().__init__()
        # import pdb; pdb.set_trace()
        cfg = OmegaConf.create(kwargs["model_conf"])
        self.cfg = cfg

        make_layer_norm = partial(
            torch.nn.LayerNorm, eps=cfg.get("norm_eps"), elementwise_affine=cfg.get("norm_affine")
        )

        def make_block(drop_path, dim=None, heads=None):
            """Make block.
            
                Args:
                    drop_path: TODO.
                    dim: TODO.
                    heads: TODO.
                """
            return AltBlock(
                cfg.get("embed_dim") if dim is None else dim,
                cfg.get("num_heads") if heads is None else heads,
                cfg.get("mlp_ratio"),
                qkv_bias=True,
                drop=cfg.get("encoder_dropout"),
                attn_drop=cfg.get("attention_dropout"),
                mlp_drop=cfg.get("activation_dropout"),
                post_mlp_drop=cfg.get("post_mlp_drop"),
                drop_path=drop_path,
                norm_layer=make_layer_norm,
                layer_norm_first=cfg.get("layer_norm_first"),
                ffn_targets=not cfg.get("end_of_block_targets"),
            )

        self.alibi_biases = {}
        self.modality_encoders = torch.nn.ModuleDict()

        enc = AudioEncoder(
            cfg.modalities.audio,
            cfg.get("embed_dim"),
            make_block,
            make_layer_norm,
            cfg.get("layer_norm_first"),
            self.alibi_biases,
        )
        self.modality_encoders["AUDIO"] = enc

        self.ema = None

        self.average_top_k_layers = cfg.get("average_top_k_layers")
        self.loss_beta = cfg.get("loss_beta")
        self.loss_scale = cfg.get("loss_scale")

        self.dropout_input = torch.nn.Dropout(cfg.get("dropout_input"))

        dpr = np.linspace(
            cfg.get("start_drop_path_rate"), cfg.get("end_drop_path_rate"), cfg.get("depth")
        )

        self.blocks = torch.nn.ModuleList([make_block(dpr[i]) for i in range(cfg.get("depth"))])

        self.norm = None
        if cfg.get("layer_norm_first"):
            self.norm = make_layer_norm(cfg.get("embed_dim"))

        vocab_size = kwargs.get("vocab_size", -1)
        self.proj = None
        if vocab_size > 0:
            self.proj = torch.nn.Linear(cfg.get("embed_dim"), vocab_size)

    def forward(
        self,
        source,
        target=None,
        id=None,
        mode=None,
        padding_mask=None,
        mask=True,
        features_only=False,
        force_remove_masked=False,
        remove_extra_tokens=True,
        precomputed_mask=None,
        **kwargs,
    ):

        """Forward pass for training.
        
            Args:
                source: TODO.
                target: TODO.
                id: TODO.
                mode: TODO.
                padding_mask: TODO.
                mask: TODO.
                features_only: TODO.
                force_remove_masked: TODO.
                remove_extra_tokens: TODO.
                precomputed_mask: TODO.
                **kwargs: Additional keyword arguments.
            """
        feature_extractor = self.modality_encoders["AUDIO"]

        mask_seeds = None

        extractor_out = feature_extractor(
            source,
            padding_mask,
            mask,
            remove_masked=not features_only or force_remove_masked,
            clone_batch=self.cfg.get("clone_batch") if not features_only else 1,
            mask_seeds=mask_seeds,
            precomputed_mask=precomputed_mask,
        )

        x = extractor_out["x"]
        encoder_mask = extractor_out["encoder_mask"]
        masked_padding_mask = extractor_out["padding_mask"]
        masked_alibi_bias = extractor_out.get("alibi_bias", None)
        alibi_scale = extractor_out.get("alibi_scale", None)

        if self.dropout_input is not None:
            x = self.dropout_input(x)

        layer_results = []
        for i, blk in enumerate(self.blocks):
            if (
                not self.training
                or self.cfg.get("layerdrop", 0) == 0
                or (np.random.random() > self.cfg.get("layerdrop", 0))
            ):
                ab = masked_alibi_bias
                if ab is not None and alibi_scale is not None:
                    scale = alibi_scale[i] if alibi_scale.size(0) > 1 else alibi_scale.squeeze(0)
                    ab = ab * scale.type_as(ab)

                x, lr = blk(
                    x,
                    padding_mask=masked_padding_mask,
                    alibi_bias=ab,
                )
                if features_only:
                    layer_results.append(lr)

        if self.norm is not None:
            x = self.norm(x)

        if features_only:
            if remove_extra_tokens:
                x = x[:, feature_extractor.modality_cfg.num_extra_tokens :]
                if masked_padding_mask is not None:
                    masked_padding_mask = masked_padding_mask[
                        :, feature_extractor.modality_cfg.num_extra_tokens :
                    ]

            return {
                "x": x,
                "padding_mask": masked_padding_mask,
                "layer_results": layer_results,
                "mask": encoder_mask,
            }

    def extract_features(
        self, source, mode=None, padding_mask=None, mask=False, remove_extra_tokens=True
    ):
        """Extract features.
        
            Args:
                source: TODO.
                mode: TODO.
                padding_mask: TODO.
                mask: TODO.
                remove_extra_tokens: TODO.
            """
        res = self.forward(
            source,
            mode=mode,
            padding_mask=padding_mask,
            mask=mask,
            features_only=True,
            remove_extra_tokens=remove_extra_tokens,
        )
        return res

    def export(self, **kwargs):
        """Export.

            Args:
                **kwargs: Additional keyword arguments.
            """
        from .export_meta import export_rebuild_model

        models = export_rebuild_model(model=self, **kwargs)
        return models

    @classmethod
    def load_from_hf(cls, model_id: str) -> tuple["Emotion2vec", list[str]]:
        """Load model and token list from HuggingFace Hub.

        Args:
            model_id: HuggingFace model identifier (e.g. "emotion2vec/emotion2vec_plus_large").

        Returns:
            Tuple of (model, token_list).
        """
        from pathlib import Path

        from huggingface_hub import hf_hub_download

        config_path = hf_hub_download(model_id, "config.yaml")
        weights_path = hf_hub_download(model_id, "model.pt")
        tokens_path = hf_hub_download(model_id, "tokens.txt")

        cfg = OmegaConf.load(config_path)
        token_list = Path(tokens_path).read_text(encoding="utf-8").strip().splitlines()
        vocab_size = len(token_list)

        model = cls(model_conf=OmegaConf.to_container(cfg.model_conf, resolve=True), vocab_size=vocab_size)

        state_dict = torch.load(weights_path, map_location="cpu", weights_only=True)
        # Strip scope prefix (e.g. "d2v_model.") from keys
        scope_map = cfg.get("scope_map", [])
        prefix = scope_map[0] if scope_map else ""
        if prefix and prefix != "none":
            state_dict = {k.removeprefix(prefix): v for k, v in state_dict.items()}

        model.load_state_dict(state_dict, strict=False)
        model.eval()

        return model, token_list
