---
title: 更新日志：Tidoc 各版本变化记录
editLink: false
description: Tidoc 每个版本新增、调整和修复的内容，按时间倒序排列，软件更新弹窗里的「本次更新」取自这里。
---

<script setup>
import { data } from './changelog.data'
</script>

# 更新日志

每个版本新增、调整和修复的内容，按时间倒序排列。软件更新弹窗里的「本次更新」取自这里，只列前 6 条，完整内容以本页为准。想更新到最新版，见[软件和组件更新](/update/)。

<TidocChangelog :releases="data.releases" />
