// VitePress 本地搜索默认按空格和标点切词，一整句中文会被当成一个词，搜「付款截图」找不到
// 「点击付款添加付款截图」。这里把连续汉字切成相邻两字的组合，其余文字仍按空格和标点切开。
//
// 注意：VitePress 把这个函数转成源码字符串再放进浏览器执行，所以函数必须自包含，
// 不能引用模块级常量或 import 进来的东西。
export const miniSearch = {
  options: {
    tokenize: (text: string): string[] => {
      const cjk = /[㐀-鿿]+/g
      const split = /[\s\p{P}\p{S}]+/u
      const tokens: string[] = []
      for (const piece of text.split(split)) {
        if (!piece) continue
        let last = 0
        for (const match of piece.matchAll(cjk)) {
          const start = match.index ?? 0
          const run = match[0]
          if (start > last) tokens.push(piece.slice(last, start).toLowerCase())
          if (run.length === 1) tokens.push(run)
          else for (let i = 0; i < run.length - 1; i++) tokens.push(run.slice(i, i + 2))
          last = start + run.length
        }
        if (last < piece.length) tokens.push(piece.slice(last).toLowerCase())
      }
      return tokens
    }
  },
  searchOptions: { combineWith: 'AND', fuzzy: false, prefix: true }
}
