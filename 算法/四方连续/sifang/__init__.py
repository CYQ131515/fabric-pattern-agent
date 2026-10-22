"""四方连续生成：算法搭骨架 + 大模型补缝。

模块划分
    io_utils   读写、尺寸规整、平铺拼图
    seam       位移、mask、羽化、强制还原、边界对齐（纯 Pillow + NumPy）
    metrics    接缝体检指标
    probe      输入体检（花钱之前先判断该走哪条路）
    apimart    APIMart 客户端：deepseek 诊断 + gpt-image 出图
    pipeline   编排
    service    对外服务入口，给主程序按子进程调用（见 service.py 顶部的契约说明）
"""
from .pipeline import Config, Result, run
from .probe import probe

__all__ = ["Config", "Result", "probe", "run"]
__version__ = "1.1.0"
