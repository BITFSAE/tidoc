import { h, nextTick, onMounted, watch } from 'vue'
import { useRoute } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import mediumZoom from 'medium-zoom'
import TidocChangelog from './components/TidocChangelog.vue'
import TidocDownload from './components/TidocDownload.vue'
import './custom.css'
import './home.css'

export default {
  extends: DefaultTheme,
  Layout: () => h(DefaultTheme.Layout),
  enhanceApp({ app }) {
    app.component('TidocChangelog', TidocChangelog)
    app.component('TidocDownload', TidocDownload)
  },
  setup() {
    const route = useRoute()
    const enhance = () => {
      mediumZoom('.vp-doc img, .home-shot-frame img', { background: 'var(--vp-c-bg)' })
    }
    onMounted(enhance)
    watch(
      () => route.path,
      () => nextTick(enhance)
    )
  }
}
