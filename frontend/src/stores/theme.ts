import { defineStore } from 'pinia'

/** Q9：主题偏好本地存储键；取值 'dark' / 'light'，缺省按暗色处理 */
const STORAGE_KEY = 'lc-theme'

function readInitialDark(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== 'light'
  } catch {
    return true
  }
}

/**
 * 主题 store：暗色为默认主题（:root 变量即暗色），亮色由 [data-theme='light'] 覆盖。
 * Element Plus 暗色走 html.dark（main.ts 已引入 dark/css-vars.css），与 lc 变量同步切换。
 */
export const useThemeStore = defineStore('theme', {
  state: () => ({
    isDark: readInitialDark(),
  }),
  actions: {
    /** 把当前 isDark 应用到 <html> 的 class 与 data-theme 属性 */
    apply(): void {
      document.documentElement.classList.toggle('dark', this.isDark)
      document.documentElement.dataset.theme = this.isDark ? 'dark' : 'light'
    },
    /** 应用启动时调用：按本地存储恢复主题（需在 pinia 注册后调用） */
    init(): void {
      this.apply()
    },
    toggle(): void {
      this.isDark = !this.isDark
      try {
        localStorage.setItem(STORAGE_KEY, this.isDark ? 'dark' : 'light')
      } catch {
        // 隐私模式等写不进 localStorage 时仅本次生效
      }
      this.apply()
    },
  },
})
