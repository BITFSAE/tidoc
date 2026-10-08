import { defineConfig } from 'vitepress'
import { miniSearch } from './search-tokenize'
import { repoLinks } from './repo-links'

const repo = 'https://github.com/BITFSAE/tidoc'

// 「开始使用」和「使用指南」共用一份侧栏，读完安装可以顺着往下读。
const usageSidebar = [
  {
    text: '开始使用',
    items: [
      { text: '下载与安装', link: '/start/install' },
      { text: '快速入门', link: '/start/quickstart' }
    ]
  },
  {
    text: '使用指南',
    items: [
      { text: '基本概念', link: '/guide/concepts' },
      { text: '导入发票', link: '/guide/import' },
      { text: '补齐材料', link: '/guide/materials' },
      { text: '核对识别结果', link: '/guide/check' },
      { text: '筛选、标签与批次', link: '/guide/organize' },
      { text: '导出、打印与交接', link: '/guide/export' },
      { text: '报账方案', link: '/guide/schemes' },
      { text: '设置与数据', link: '/guide/settings' },
      { text: '快捷操作', link: '/guide/shortcuts' }
    ]
  },
  {
    text: '可选功能',
    items: [{ text: '阿里云 OCR', link: '/guide/aliyun-ocr' }]
  }
]

export default defineConfig({
  lang: 'zh-CN',
  title: 'Tidoc',
  titleTemplate: ':title · Tidoc',
  description: 'Tidoc 是一款整理报账发票和相关材料的桌面软件，支持 Windows 和 macOS，识别在本机完成。',
  cleanUrls: false,

  head: [
    ['link', { rel: 'icon', type: 'image/png', href: '/favicon.png' }],
    ['meta', { name: 'theme-color', content: '#2b5fd1' }]
  ],

  // 团队适配计划是实施与验收记录，不放进文档站；README 只是仓库入口。
  srcExclude: ['TEAM_ADAPTER_PLAN.md', 'README.md', 'node_modules/**'],

  // 源文件保持原位置（命令行工具和仓库内链接都依赖它），站内地址放到「开发相关」下。
  rewrites: {
    'UPDATE.md': 'dev/release.md',
    'adapters/:page.md': 'dev/adapters/:page.md'
  },

  vite: {
    // 本地搜索索引按站点内容整体打包，体积随文档增长，默认的 500 kB 提示没有参考意义。
    build: { chunkSizeWarningLimit: 1500 }
  },

  markdown: {
    config: (md) => md.use(repoLinks(repo)),
    image: { lazyLoading: true },
    container: {
      tipLabel: '提示',
      warningLabel: '注意',
      dangerLabel: '警告',
      infoLabel: '说明',
      detailsLabel: '展开查看'
    }
  },

  themeConfig: {
    logo: '/logo.png',
    siteTitle: 'Tidoc',

    nav: [
      { text: '简介', link: '/intro/', activeMatch: '^/intro/' },
      { text: '开始使用', link: '/start/install', activeMatch: '^/start/' },
      { text: '使用指南', link: '/guide/concepts', activeMatch: '^/guide/' },
      { text: '更新与组件', link: '/update/', activeMatch: '^/update/' },
      { text: '常见问题', link: '/faq', activeMatch: '^/faq' },
      { text: '开发相关', link: '/dev/', activeMatch: '^/dev/' }
    ],

    sidebar: {
      '/start/': usageSidebar,
      '/guide/': usageSidebar,
      '/update/': [
        {
          text: '更新与组件',
          items: [
            { text: '软件和组件更新', link: '/update/' },
            { text: '更新日志', link: '/update/changelog' }
          ]
        }
      ],
      '/dev/': [
        {
          text: '开发相关',
          items: [
            { text: '概览', link: '/dev/' },
            { text: '参与贡献', link: '/dev/contributing' },
            { text: '设计文档', link: '/dev/design' },
            { text: '发布与更新机制', link: '/dev/release' }
          ]
        },
        {
          text: '团队适配包',
          items: [
            { text: '适配包开发', link: '/dev/adapters/README' },
            { text: '包格式', link: '/dev/adapters/FORMAT' },
            { text: '字段与设置', link: '/dev/adapters/FIELDS' },
            { text: '条件规则', link: '/dev/adapters/RULES' },
            { text: '模板', link: '/dev/adapters/TEMPLATES' },
            { text: '输出', link: '/dev/adapters/OUTPUTS' },
            { text: '兼容性', link: '/dev/adapters/COMPATIBILITY' },
            { text: 'AI 工作流', link: '/dev/adapters/AI_WORKFLOW' },
            { text: '注册表参考', link: '/dev/adapters/REFERENCE' }
          ]
        }
      ]
    },

    socialLinks: [{ icon: 'github', link: repo }],

    search: {
      provider: 'local',
      options: {
        miniSearch,
        // 设计文档和发布说明篇幅大、术语多，搜索使用说明时会挤掉真正要找的页面。
        exclude: (path: string) => path === 'dev/design.md' || path === 'dev/release.md',
        locales: {
          root: {
            translations: {
              button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
              modal: {
                displayDetails: '显示详细列表',
                resetButtonTitle: '清除查询',
                backButtonTitle: '返回',
                noResultsText: '没有找到相关内容',
                footer: {
                  selectText: '选择',
                  navigateText: '切换',
                  closeText: '关闭'
                }
              }
            }
          }
        }
      }
    },

    editLink: {
      pattern: `${repo}/edit/main/docs/:path`,
      text: '在 GitHub 上编辑此页'
    },

    outline: { level: [2, 3], label: '本页目录' },
    docFooter: { prev: '上一页', next: '下一页' },
    returnToTopLabel: '回到顶部',
    sidebarMenuLabel: '目录',
    darkModeSwitchLabel: '外观',
    lightModeSwitchTitle: '切换到浅色',
    darkModeSwitchTitle: '切换到深色',
    skipToContentLabel: '跳到正文',

    notFound: {
      title: '页面不存在',
      quote: '这个地址没有对应的页面，可能已经改名或移动。可以回到首页，或用搜索找到需要的内容。',
      linkLabel: '回到首页',
      linkText: '回到首页'
    },

    footer: {
      message: '以 MIT 许可证发布。',
      copyright: '© BITFSAE'
    }
  }
})
