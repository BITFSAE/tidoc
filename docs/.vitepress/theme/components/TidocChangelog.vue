<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { ChangeKind, Release } from '../../../update/changelog.data'

// 更新日志：按版本分组的时间线。数据在构建时由 docs/update/changelog.data.ts 从 CHANGELOG.md 解析，
// 这里只负责显示、按类型筛选和展开收起。
const props = defineProps<{ releases: Release[] }>()

type Filter = 'all' | Exclude<ChangeKind, 'other'>

const filters: { id: Filter; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'added', label: '新增' },
  { id: 'changed', label: '调整' },
  { id: 'fixed', label: '修复' }
]

const filter = ref<Filter>('all')
// 默认只展开最新一版，其余版本显示一行摘要。
const defaultOpen = () => new Set(props.releases.slice(0, 1).map((release) => release.id))
const open = ref(defaultOpen())

const totals = computed(() => {
  const sum: Record<Filter, number> = { all: 0, added: 0, changed: 0, fixed: 0 }
  for (const release of props.releases) {
    for (const group of release.groups) {
      sum.all += group.items.length
      if (group.kind in sum) sum[group.kind as Filter] += group.items.length
    }
  }
  return sum
})

const visible = computed(() =>
  props.releases
    .map((release) => ({
      ...release,
      groups: release.groups.filter((group) => group.items.length && (filter.value === 'all' || group.kind === filter.value))
    }))
    .filter((release) => (filter.value === 'all' ? true : release.groups.length > 0))
)

const allOpen = computed(() => visible.value.every((release) => open.value.has(release.id)))

function pick(next: Filter) {
  if (totals.value[next] === 0 || next === filter.value) return
  filter.value = next
  // 筛选后直接展开结果；回到「全部」时恢复默认。
  open.value = next === 'all' ? defaultOpen() : new Set(visible.value.map((release) => release.id))
}

function toggleAll() {
  open.value = allOpen.value ? new Set() : new Set(visible.value.map((release) => release.id))
}

function onToggle(id: string, event: Event) {
  const expanded = (event.target as HTMLDetailsElement).open
  if (expanded === open.value.has(id)) return
  if (expanded) open.value.add(id)
  else open.value.delete(id)
}

// 从外部链接（如 #v0.1.38）或右侧目录进入时，把对应版本展开。
function openFromHash() {
  const id = decodeURIComponent(window.location.hash.slice(1))
  if (id && props.releases.some((release) => release.id === id)) open.value.add(id)
}

onMounted(() => {
  openFromHash()
  window.addEventListener('hashchange', openFromHash)
})
onUnmounted(() => window.removeEventListener('hashchange', openFromHash))
</script>

<template>
  <div class="tidoc-changelog">
    <div class="cl-bar">
      <div class="cl-filter" role="group" aria-label="按类型筛选">
        <button
          v-for="item in filters"
          :key="item.id"
          type="button"
          class="cl-chip"
          :class="[`is-${item.id}`, { 'is-active': filter === item.id }]"
          :aria-pressed="filter === item.id"
          :disabled="totals[item.id] === 0"
          @click="pick(item.id)"
        >
          {{ item.label }}
        </button>
      </div>
      <button type="button" class="cl-toggle" @click="toggleAll">{{ allOpen ? '全部收起' : '全部展开' }}</button>
    </div>

    <ol class="cl-list">
      <li v-for="(release, index) in visible" :key="release.id" class="cl-item">
        <details class="cl-release" :class="{ 'is-latest': index === 0 && filter === 'all' }" :open="open.has(release.id)" @toggle="onToggle(release.id, $event)">
          <summary class="cl-head">
            <span class="cl-dot" aria-hidden="true"></span>
            <h2 :id="release.id" class="cl-title">
              <span class="cl-version">{{ release.version ? `v${release.version}` : release.date }}</span>
              <time v-if="release.version" class="cl-date ignore-header" :datetime="release.date">{{ release.date }}</time>
              <span v-if="index === 0 && filter === 'all'" class="cl-latest ignore-header">最新</span>
            </h2>
            <span class="cl-counts" aria-hidden="true">
              <span v-for="group in release.groups" :key="group.label" class="cl-count" :class="`is-${group.kind}`">
                {{ group.label }} {{ group.items.length }}
              </span>
            </span>
            <svg class="cl-chevron" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="m4.5 6.5 3.5 3.5 3.5-3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </summary>

          <div class="cl-body">
            <p v-if="release.intro && filter === 'all'" class="cl-intro" v-html="release.intro"></p>
            <section v-for="group in release.groups" :key="group.label" class="cl-group" :class="`is-${group.kind}`">
              <p class="cl-label">{{ group.label }}</p>
              <ul>
                <li v-for="(item, i) in group.items" :key="i">
                  <span class="cl-lead" v-html="item.lead"></span><span v-if="item.rest" class="cl-rest" v-html="item.rest"></span>
                </li>
              </ul>
            </section>
          </div>
        </details>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.tidoc-changelog {
  --cl-added: #17804a;
  --cl-changed: var(--vp-c-brand-1);
  --cl-fixed: #b0620a;
  --cl-other: var(--vp-c-text-3);
  --cl-ease: cubic-bezier(0.22, 1, 0.36, 1);
  margin: 28px 0 8px;
}
.dark .tidoc-changelog {
  --cl-added: #4cc38a;
  --cl-fixed: #e5a24a;
}

/* 工具条：左边按类型筛选，右边展开收起。 */
.cl-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
  margin-bottom: 18px;
}
.cl-filter {
  display: inline-flex;
  gap: 2px;
  padding: 3px;
  border-radius: 10px;
  background: var(--vp-c-bg-soft);
}
.cl-chip {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 14px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--vp-c-text-2);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 160ms ease, color 160ms ease, transform 160ms var(--cl-ease);
}
.cl-chip:hover:not(:disabled):not(.is-active) {
  color: var(--vp-c-text-1);
}
.cl-chip:active:not(:disabled) {
  transform: scale(0.97);
}
.cl-chip.is-active {
  background: var(--vp-c-bg);
  color: var(--vp-c-text-1);
  box-shadow: 0 0 0 1px var(--vp-c-divider), 0 1px 2px rgba(0, 0, 0, 0.06);
}
.dark .cl-chip.is-active {
  background: var(--vp-c-bg-elv);
  box-shadow: 0 0 0 1px var(--vp-c-border);
}
.cl-chip:disabled {
  opacity: 0.45;
  cursor: default;
}
.cl-toggle {
  padding: 4px 2px;
  border: 0;
  background: transparent;
  color: var(--vp-c-text-2);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: color 160ms ease;
}
.cl-toggle:hover {
  color: var(--vp-c-brand-1);
}
.cl-chip:focus-visible,
.cl-toggle:focus-visible,
.cl-head:focus-visible {
  outline: 2px solid var(--vp-c-brand-1);
  outline-offset: 2px;
}

/* 时间线：竖线贯穿，每个版本一个圆点，线在最后一版之后淡出。 */
.cl-list {
  position: relative;
  margin: 0;
  padding: 0;
  list-style: none;
}
.cl-list::before {
  content: '';
  position: absolute;
  top: 22px;
  bottom: 0;
  left: 5px;
  width: 1px;
  background: var(--vp-c-divider);
  mask-image: linear-gradient(to bottom, #000 calc(100% - 56px), transparent);
}
.cl-item {
  margin: 0;
  padding: 0;
}
.vp-doc .cl-item + .cl-item {
  margin-top: 2px;
}

.cl-release {
  position: relative;
  padding-left: 26px;
}
.cl-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  align-items: center;
  gap: 4px 14px;
  min-height: 44px;
  padding: 6px 10px 6px 12px;
  margin: 0 0 0 -12px;
  border-radius: 10px;
  list-style: none;
  cursor: pointer;
  transition: background-color 160ms ease;
}
.cl-head::-webkit-details-marker {
  display: none;
}
@media (hover: hover) {
  .cl-head:hover {
    background: var(--vp-c-bg-soft);
  }
}
.cl-dot {
  position: absolute;
  top: 17px;
  left: 0;
  box-sizing: border-box;
  width: 11px;
  height: 11px;
  border: 2px solid var(--vp-c-text-3);
  border-radius: 50%;
  background: var(--vp-c-bg);
  transition: border-color 160ms ease, background-color 160ms ease, box-shadow 160ms ease;
}
.cl-release[open] .cl-dot {
  border-color: var(--vp-c-brand-1);
}
.cl-release.is-latest .cl-dot {
  border-color: var(--vp-c-brand-1);
  background: var(--vp-c-brand-1);
  box-shadow: 0 0 0 4px var(--vp-c-brand-soft);
}

.vp-doc .cl-title {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 2px 10px;
  margin: 0;
  padding: 0;
  border: 0;
  letter-spacing: 0;
  line-height: 1.4;
}
.cl-version {
  color: var(--vp-c-text-1);
  font-size: 18px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.cl-date {
  color: var(--vp-c-text-3);
  font-size: 13px;
  font-weight: 400;
  font-variant-numeric: tabular-nums;
}
.cl-latest {
  align-self: center;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--vp-c-brand-soft);
  color: var(--vp-c-brand-1);
  font-size: 12px;
  font-weight: 500;
  line-height: 18px;
}
.cl-counts {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 2px 12px;
}
.cl-count {
  color: var(--vp-c-text-2);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.cl-count::before,
.cl-label::before {
  content: '';
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 6px;
  border-radius: 50%;
  background: var(--cl-kind, var(--vp-c-text-3));
  vertical-align: 1px;
}
.is-added {
  --cl-kind: var(--cl-added);
}
.is-changed {
  --cl-kind: var(--cl-changed);
}
.is-fixed {
  --cl-kind: var(--cl-fixed);
}
.is-other {
  --cl-kind: var(--cl-other);
}
.cl-chevron {
  color: var(--vp-c-text-3);
  transition: transform 200ms var(--cl-ease), color 160ms ease;
}
.cl-release[open] .cl-chevron {
  transform: rotate(180deg);
}
.cl-head:hover .cl-chevron {
  color: var(--vp-c-text-2);
}

.cl-body {
  padding: 4px 4px 22px 0;
}
@media (prefers-reduced-motion: no-preference) {
  .cl-release[open] .cl-body {
    animation: cl-in 220ms var(--cl-ease);
  }
}
@keyframes cl-in {
  from {
    opacity: 0;
    transform: translateY(-4px);
  }
}
.cl-intro {
  margin: 4px 0 14px;
  color: var(--vp-c-text-2);
  font-size: 14px;
  line-height: 1.7;
}
.cl-group + .cl-group {
  margin-top: 16px;
}
.cl-label {
  display: flex;
  align-items: center;
  margin: 0 0 6px;
  color: var(--cl-kind);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.6;
}
.cl-label::before {
  margin-right: 8px;
}
.cl-group ul {
  margin: 0;
  padding: 0;
  list-style: none;
}
.cl-group li {
  position: relative;
  margin: 0;
  padding: 5px 0 5px 16px;
  color: var(--vp-c-text-1);
  font-size: 14.5px;
  line-height: 1.75;
}
.cl-group li + li {
  margin-top: 0;
}
.cl-rest {
  color: var(--vp-c-text-2);
}
.cl-group li::before {
  content: '';
  position: absolute;
  top: 15px;
  left: 3px;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--vp-c-text-3);
}
.cl-group :deep(code) {
  font-size: 0.86em;
}

@media (max-width: 639px) {
  .cl-head {
    grid-template-columns: minmax(0, 1fr) auto;
  }
  .cl-counts {
    grid-column: 1;
    grid-row: 2;
    justify-content: flex-start;
  }
  .cl-chevron {
    grid-column: 2;
    grid-row: 1;
  }
  .cl-version {
    font-size: 17px;
  }
}
</style>
