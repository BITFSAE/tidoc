import { h, nextTick, onMounted, watch } from 'vue'
import { useRoute } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import mediumZoom from 'medium-zoom'
import TidocDownload from './components/TidocDownload.vue'
import './custom.css'
import './home.css'

// 首屏以下带 data-reveal 的元素：进入视口时才播放入场动画。
// 只在脚本就绪后才给它们加等待状态，没有脚本或减少动态效果时直接显示最终样子。
function initReveal() {
  if (typeof IntersectionObserver === 'undefined') return
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  const targets = document.querySelectorAll<HTMLElement>('[data-reveal]')
  if (!targets.length) return
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue
        entry.target.classList.remove('reveal-wait')
        observer.unobserve(entry.target)
      }
    },
    { threshold: 0.3 }
  )
  targets.forEach((el) => {
    if (el.getBoundingClientRect().top < window.innerHeight * 0.7) return
    el.classList.add('reveal-wait')
    observer.observe(el)
  })
}

export default {
  extends: DefaultTheme,
  Layout: () => h(DefaultTheme.Layout),
  enhanceApp({ app }) {
    app.component('TidocDownload', TidocDownload)
  },
  setup() {
    const route = useRoute()
    const enhance = () => {
      mediumZoom('.vp-doc img, .app-window-body img', { background: 'var(--vp-c-bg)' })
      initReveal()
    }
    onMounted(enhance)
    watch(
      () => route.path,
      () => nextTick(enhance)
    )
  }
}
