// VitePress 本地搜索默认按空格和标点切词，一整句中文会被当成一个词，搜「付款截图」找不到
// 「点击付款添加付款截图」，而版本号如 0.1.43 也会被拆成 0、1、43。
// 这里把连续汉字切成相邻两字的组合，并保留带点/短横线/下划线的英文数字词（如 0.1.43、tidoc-core）。
//
// 注意：VitePress 把这个函数转成源码字符串再放进浏览器执行，所以函数必须自包含，
// 不能引用模块级常量或 import 进来的东西。
export const miniSearch = {
  options: {
    tokenize: (text: string): string[] => {
      const tokenRe = /[㐀-鿿]+|[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*/g
      const tokens: string[] = []
      for (const match of text.matchAll(tokenRe)) {
        const run = match[0]
        const code = run.charCodeAt(0)
        if (code >= 0x3400 && code <= 0x9fff) {
          if (run.length === 1) tokens.push(run)
          else for (let i = 0; i < run.length - 1; i++) tokens.push(run.slice(i, i + 2))
        } else {
          const lower = run.toLowerCase()
          tokens.push(lower)
          if (/^v\d+\./.test(lower)) tokens.push(lower.slice(1))
        }
      }
      return tokens
    }
  },
  searchOptions: { combineWith: 'AND', fuzzy: false, prefix: true }
}
