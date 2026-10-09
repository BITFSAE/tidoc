import { readFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { createMarkdownRenderer, defineLoader } from 'vitepress'
import { repo, repoLinks } from '../.vitepress/repo-links'

// 更新日志页的数据来自仓库根目录的 CHANGELOG.md。软件里的「本次更新」和 GitHub Release 说明
// 也取自这个文件的最新一节（scripts/release_notes.py），所以这里只解析，不另存一份。
//
// 标题格式（改动时要同步这里和 scripts/release_notes.py）：
//   ## Unreleased              尚未发布，不上站点
//   ## 2026-10-08 · v0.1.39    已发布的版本
//   ## 2026-09-14              没有版本号的日期记录
// 小节标题 Added / Changed / Fixed 对应「新增 / 调整 / 修复」。

export type ChangeKind = 'added' | 'changed' | 'fixed' | 'other'

// 条目的第一句单独取出，页面里用正常字色显示；后面的补充说明用次要字色，长条目更容易扫读。
export interface ChangeItem {
  lead: string
  rest: string
}

export interface ChangeGroup {
  kind: ChangeKind
  label: string
  items: ChangeItem[]
}

export interface Release {
  id: string
  version: string | null
  date: string
  intro: string
  groups: ChangeGroup[]
}

export interface ChangelogData {
  releases: Release[]
}

declare const data: ChangelogData
export { data }

const KINDS: Record<string, { kind: ChangeKind; label: string }> = {
  added: { kind: 'added', label: '新增' },
  changed: { kind: 'changed', label: '调整' },
  fixed: { kind: 'fixed', label: '修复' }
}

// 在第一个句号处切开（跳过行内代码里的句号），句号后紧跟的引号和括号留在第一句里。
function splitLead(text: string): [string, string] {
  let inCode = false
  for (let i = 0; i < text.length - 1; i += 1) {
    if (text[i] === '`') inCode = !inCode
    if (text[i] !== '。' || inCode) continue
    let end = i + 1
    while (end < text.length && '」』）)'.includes(text[end])) end += 1
    return [text.slice(0, end), text.slice(end).trimStart()]
  }
  return [text, '']
}

const HEADING = /^(\d{4}-\d{2}-\d{2})(?:\s+·\s+v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?))?$/

export default defineLoader({
  watch: ['../../CHANGELOG.md'],
  async load([file]: string[]): Promise<ChangelogData> {
    const md = await createMarkdownRenderer(dirname(file), {
      config: (it) => it.use(repoLinks(repo))
    })
    // 条目里的相对链接（README.md#状态 之类）按仓库根目录解析，再由 repoLinks 改成站内或 GitHub 地址。
    const inline = (text: string) => md.renderInline(text, { path: file, realPath: file })
    const item = (text: string): ChangeItem => {
      const [lead, rest] = splitLead(text)
      return { lead: inline(lead), rest: rest ? inline(rest) : '' }
    }

    const releases: Release[] = []
    const seen = new Set<string>()
    let release: Release | null = null
    let group: ChangeGroup | null = null
    let skipping = false

    for (const [index, raw] of readFileSync(file, 'utf-8').split(/\r?\n/).entries()) {
      const line = raw.trimEnd()
      const heading = line.match(/^(#{2,3})\s+(.+)$/)

      if (heading?.[1] === '##') {
        const title = heading[2].trim()
        group = null
        if (title === 'Unreleased') {
          release = null
          skipping = true
          continue
        }
        const found = title.match(HEADING)
        if (!found) {
          throw new Error(
            `CHANGELOG.md 第 ${index + 1} 行的标题「${title}」无法识别。` +
              '请写成「## 2026-10-08 · v0.1.39」「## 2026-09-14」或「## Unreleased」。'
          )
        }
        skipping = false
        const [, date, version] = found
        // 同一天可以有多条记录，锚点用版本号，没有版本号时在日期后加序号。
        let id = version ? `v${version}` : `d${date}`
        for (let n = 2; seen.has(id); n += 1) id = `d${date}-${n}`
        seen.add(id)
        release = { id, version: version ?? null, date, intro: '', groups: [] }
        releases.push(release)
        continue
      }

      if (skipping || !release) continue

      if (heading?.[1] === '###') {
        const name = heading[2].trim()
        const known = KINDS[name.toLowerCase()]
        group = { kind: known?.kind ?? 'other', label: known?.label ?? name, items: [] }
        release.groups.push(group)
        continue
      }

      const bullet = line.match(/^-\s+(.+)$/)
      if (bullet) {
        if (!group) {
          group = { kind: 'other', label: '更新', items: [] }
          release.groups.push(group)
        }
        group.items.push(item(bullet[1]))
      } else if (line.trim() && !group) {
        // 版本标题下、第一个小节之前的说明段落。
        release.intro += (release.intro ? ' ' : '') + inline(line.trim())
      }
    }

    return { releases: releases.filter((item) => item.intro || item.groups.some((g) => g.items.length)) }
  }
})
