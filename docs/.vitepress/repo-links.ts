import path from 'node:path'
import type MarkdownIt from 'markdown-it'

// 仓库里的 Markdown 彼此用相对路径互相链接（README、DESIGN、CHANGELOG、docs/…），
// 这些文件有的放进了文档站（站内地址不同），有的不放（改链到 GitHub）。
// 这里在渲染时把指向它们的相对链接改写成站内地址或 GitHub 地址，源文件保持原样。
const repoRoot = path.resolve(__dirname, '../..')

export const repo = 'https://github.com/BITFSAE/tidoc'

// 这几页只是把仓库根目录的文件嵌进来，正文里的相对链接是相对仓库根目录写的。
// 更新日志页不在其中：它在构建时解析 CHANGELOG.md，条目里的链接另行改写。
const rootIncludes = new Set(['docs/dev/design.md', 'docs/dev/contributing.md'])

export function repoLinks(repo: string) {
  const github = (rel: string, hash: string) => {
    const kind = path.extname(rel) || rel === 'LICENSE' ? 'blob' : 'tree'
    return `${repo}/${kind}/main/${rel.replace(/\/$/, '')}${hash}`
  }

  const mapLink = (rel: string, hash: string): string | undefined => {
    if (rel === 'README.md') return `${repo}${hash || ''}`
    if (rel === 'DESIGN.md') return `/dev/design${hash}`
    if (rel === 'CHANGELOG.md') return `/update/changelog${hash}`
    if (rel === 'CONTRIBUTING.md') return `/dev/contributing${hash}`
    if (rel === 'docs/UPDATE.md') return `/dev/release${hash}`
    if (rel === 'docs/TEAM_ADAPTER_PLAN.md') return github(rel, hash)
    const adapter = rel.match(/^docs\/adapters\/([^/]+)\.md$/)
    if (adapter) return `/dev/adapters/${adapter[1]}${hash}`
    // docs/ 里其它页面按原位置解析；docs/ 之外的文件和目录指向 GitHub。
    if (!rel.startsWith('docs/') && !rel.startsWith('..')) return github(rel, hash)
    return undefined
  }

  return (md: MarkdownIt) => {
    md.core.ruler.push('tidoc_repo_links', (state) => {
      // realPath 是源文件的真实位置；path 会随 rewrites 变成站内地址对应的虚拟位置。
      const file: string | undefined = state.env?.realPath ?? state.env?.path
      if (!file) return
      const fileRel = path.relative(repoRoot, file).split(path.sep).join('/')
      const dir = rootIncludes.has(fileRel) ? repoRoot : path.dirname(file)
      for (const block of state.tokens) {
        for (const token of block.children ?? []) {
          if (token.type !== 'link_open') continue
          const href = token.attrGet('href')
          if (!href || /^([a-z][a-z0-9+.-]*:|\/|#)/i.test(href)) continue
          const [target, hash = ''] = href.split('#')
          const rel = path.relative(repoRoot, path.resolve(dir, decodeURI(target))).split(path.sep).join('/')
          const mapped = mapLink(rel, hash ? `#${hash}` : '')
          if (mapped) token.attrSet('href', mapped)
        }
      }
    })
  }
}
