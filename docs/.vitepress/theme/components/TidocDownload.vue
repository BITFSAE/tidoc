<script setup lang="ts">
import { onMounted, ref } from 'vue'

// 官网的下载入口：按系统返回最新版安装包，版本更新时这里不用改。
const GATEWAY = 'https://www.bitfsae.com/api/downloads/tidoc-core'

type Platform = 'windows' | 'macos'

const targets: { id: Platform; name: string; hint: string }[] = [
  { id: 'windows', name: 'Windows', hint: 'Windows 安装程序 · .exe' },
  { id: 'macos', name: 'macOS', hint: 'macOS 磁盘映像 · .dmg' }
]

// 构建时无法知道访问者的系统，挂载后再标出当前设备；之前两行保持同等样式。
const current = ref<Platform | null>(null)
const mobile = ref(false)

onMounted(() => {
  const device = `${navigator.platform} ${navigator.userAgent}`.toLowerCase()
  const touchMac = device.includes('mac') && navigator.maxTouchPoints > 1
  mobile.value = /android|iphone|ipad|ipod/.test(device) || touchMac
  if (mobile.value) return
  if (device.includes('mac')) current.value = 'macos'
  else if (device.includes('win')) current.value = 'windows'
})
</script>

<template>
  <div class="tidoc-dl">
    <ul class="dl-list">
      <li v-for="item in targets" :key="item.id" class="dl-item" :class="{ 'is-current': current === item.id }">
        <span class="dl-icon" aria-hidden="true">
          <svg v-if="item.id === 'windows'" xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
            <path d="M3 5.55 10.4 4.5v7.05H3zM11.4 4.36 21 3v8.55h-9.6zM3 12.45h7.4v7.05L3 18.45zM11.4 12.45H21V21l-9.6-1.36z" />
          </svg>
          <svg v-else xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701" />
          </svg>
        </span>
        <span class="dl-info">
          <span class="dl-name">
            {{ item.name }}
            <span v-if="current === item.id" class="dl-badge">当前设备</span>
          </span>
          <span class="dl-hint">{{ item.hint }}</span>
        </span>
        <a
          class="home-btn no-icon"
          :class="{ 'is-brand': current === item.id }"
          data-icon="download"
          :href="`${GATEWAY}/${item.id}`"
          :aria-label="`下载 ${item.name} 版安装包`"
          rel="noopener"
        >下载</a>
      </li>
    </ul>
    <p v-if="mobile" class="dl-note">当前设备是手机或平板。Tidoc 只有 Windows 和 macOS 电脑版，请在电脑上打开本页下载。</p>
  </div>
</template>

<style scoped>
.tidoc-dl {
  margin: 20px 0 8px;
  overflow: hidden;
  border: 1px solid var(--vp-c-divider);
  border-radius: 14px;
  background: var(--vp-c-bg-soft);
}
.dl-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.dl-item {
  display: flex;
  align-items: center;
  gap: 14px;
  margin: 0;
  padding: 16px 18px;
  transition: background-color 160ms ease;
}
.dl-item + .dl-item {
  margin-top: 0;
  border-top: 1px solid var(--vp-c-divider);
}
.dl-item.is-current {
  background: var(--vp-c-bg);
  box-shadow: inset 3px 0 0 var(--vp-c-brand-1);
}
.dl-icon {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 42px;
  height: 42px;
  border: 1px solid var(--vp-c-divider);
  border-radius: 10px;
  background: var(--vp-c-bg);
  color: var(--vp-c-text-1);
}
.dl-item.is-current .dl-icon {
  border-color: color-mix(in srgb, var(--vp-c-brand-1) 35%, transparent);
  color: var(--vp-c-brand-1);
}
.dl-info {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 3px;
  min-width: 0;
}
.dl-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15.5px;
  font-weight: 650;
  line-height: 1.4;
  color: var(--vp-c-text-1);
}
.dl-badge {
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--vp-c-brand-soft);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--vp-c-brand-1);
}
.dl-hint {
  font-size: 13px;
  line-height: 1.5;
  color: var(--vp-c-text-2);
}
.dl-note {
  margin: 0;
  padding: 12px 18px;
  border-top: 1px solid var(--vp-c-divider);
  font-size: 13px;
  line-height: 1.7;
  color: var(--vp-c-text-2);
}
@media (max-width: 479px) {
  .dl-item {
    flex-wrap: wrap;
    padding: 16px;
  }
  .dl-info {
    flex-basis: calc(100% - 58px);
  }
  .dl-item .home-btn {
    flex: 1 1 100%;
  }
}
@media (prefers-reduced-motion: reduce) {
  .dl-item {
    transition: none;
  }
}
</style>
