import { h, nextTick, onMounted, watch } from 'vue'
import { useRoute } from 'vitepress'
import DefaultTheme from 'vitepress/theme'
import mediumZoom from 'medium-zoom'
import './custom.css'

export default {
  extends: DefaultTheme,
  Layout: () => h(DefaultTheme.Layout),
  setup() {
    const route = useRoute()
    const zoom = () =>
      mediumZoom('.vp-doc img, .VPHome .home-shot img, .window-content img', { background: 'var(--vp-c-bg)' })
    onMounted(zoom)
    watch(
      () => route.path,
      () => nextTick(zoom)
    )
  }
}
