"""子进程的小工具：起得干净、杀得彻底。

两个算法引擎（`core/psdlayer.py`、`core/seamless.py`）都是「图写临时文件 →
起算法自己的 venv 跑子进程 → 读回 JSON」这个形状。起进程的部分各写各的没问题，
**杀进程**这部分必须只有一份 —— 它有两个反直觉的点，抄来抄去一定会抄丢：

为什么不能只 `proc.kill()`
    子进程自己还会再起子进程（Pillow 的解码线程、网络栈、以后可能加的任何
    东西）。只杀最外面那个，里面那些会变成孤儿：继续占 CPU、继续往云端发
    请求、继续烧用户的钱。用户点的是「终止」，期待的是"停了"，不是
    "最外面那层停了"。

为什么要 `start_new_session=True`
    上面那条要成立，前提是子进程得自成**一个进程组**。默认情况下父子同组，
    `killpg` 会连带把主程序自己一起杀掉 —— 那是个灾难级的 bug。
    `start_new_session=True` 让子进程 `setsid()`，它当组长，于是
    「杀它那一组」= 「杀它和它所有的后代」，不多不少。

两处配合着用，缺一不可：只 killpg 不建新组会自杀，只建新组不 killpg
就还是只杀一层。
"""
from __future__ import annotations

import os
import signal
import subprocess


def spawn(argv, cwd=None, env=None):
    """按「自成进程组」的方式起子进程。用法同 subprocess.Popen。

    这里统一把 stdout/stderr 收到管道里（调用方失败时要拿 stderr 的尾巴
    给用户看），并且 `start_new_session=True` —— 理由见模块开头。
    """
    return subprocess.Popen(argv, cwd=cwd, env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            start_new_session=True)


def kill_tree(proc):
    """把 proc 连它整个进程组一起杀掉。已经退出就什么也不做。

    返回是否真的发过信号（给调用方记日志用，不影响正确性）。
    """
    if proc is None or proc.poll() is not None:
        return False
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return True
    except Exception:                                    # noqa: BLE001
        pass
    # killpg 走不通（进程已经脱离、或者平台不支持）时退到单杀。
    # 有总比没有强：至少最外面那层不会继续挂着。
    try:
        proc.kill()
        return True
    except Exception:                                    # noqa: BLE001
        return False
