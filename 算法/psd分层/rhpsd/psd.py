"""写 PSD + 回读自检。

psd-tools 默认用 macroman 编码写图层名，中文直接抛 UnicodeEncodeError；
写和读都必须显式 encoding='utf-8'，否则读回来是乱码。
"""
from __future__ import annotations

import numpy as np
from PIL import Image
from psd_tools import PSDImage


def write_psd(path: str, rgbs: list[np.ndarray], alphas: list[np.ndarray],
              names: list[str]) -> None:
    h, w = rgbs[0].shape[:2]
    psd = PSDImage.new(mode="RGB", size=(w, h), depth=8)
    for rgb, a, name in zip(rgbs, alphas, names):
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
        rgba[..., 3] = np.clip(a * 255, 0, 255).astype(np.uint8)
        psd.create_pixel_layer(Image.fromarray(rgba, "RGBA"),
                               name=name, top=0, left=0, opacity=255)
    psd.save(path, encoding="utf-8")


def verify_psd(path: str, original: np.ndarray,
               names: list[str]) -> dict:
    """回读自己写的文件，逐层读回后重算合成，和原图比。"""
    p = PSDImage.open(path, encoding="utf-8")
    got = [ly.name for ly in p]
    o = np.asarray(original, dtype=np.float64)[..., :3]
    if o.shape[:2] != (p.height, p.width):
        o = np.asarray(Image.fromarray(o.astype(np.uint8)).resize(
            (p.width, p.height), Image.LANCZOS), dtype=np.float64)

    rgbs, als = [], []
    for ly in p:
        arr = np.asarray(ly.composite(force=True).convert("RGBA"),
                         dtype=np.float64)
        rgbs.append(arr[..., :3])
        als.append(arr[..., 3] / 255.0)
    comp = _composite(rgbs, als)
    d = np.abs(comp - o)
    return {
        "layer_count": len(got),
        "names": got,
        "names_match": got == list(names),
        "recon_max_diff": round(float(d.max()), 3),
        "recon_mean_diff": round(float(d.mean()), 4),
        "psd_composite_max_diff": round(float(
            np.abs(np.asarray(p.composite().convert("RGB"),
                              dtype=np.float64) - o).max()), 3),
    }


def _composite(rgbs: list[np.ndarray], alphas: list[np.ndarray]) -> np.ndarray:
    out = np.zeros_like(rgbs[0])
    for rgb, a in zip(rgbs, alphas):
        out = rgb * a[..., None] + out * (1.0 - a[..., None])
    return out


def composite(rgbs: list[np.ndarray], alphas: list[np.ndarray]) -> np.ndarray:
    return _composite(rgbs, alphas)
