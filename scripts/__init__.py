"""维护脚本包。让测试可以用 ``from scripts.xxx import`` 稳定导入；
没有它，本目录只是命名空间包，会被环境里任何名为 scripts 的普通包遮蔽。"""
