"""乾纱凪资料驱动与心情感知聊天。

包内分层：入口薄适配（``cli*``/``app``）、领域流程（``ingest``/``retrieval``/
``dialogue``/``state``）、基础设施（``storage``/``scheduling``/``adapters``）。
领域流程只通过 :mod:`sana_bot.ports` 的协议协作，便于注入 fake 依赖离线验证。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
