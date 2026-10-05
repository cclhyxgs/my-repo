# -*- coding: utf-8 -*-
"""adapters 层：外部世界与进程内引擎的唯一收口层。

约束（补充约束 1 / T8）：**engine/* 只允许在本目录被 import**。
其余层（api / core / worker）一律经本目录取符号，不得直接 import engine。
"""
