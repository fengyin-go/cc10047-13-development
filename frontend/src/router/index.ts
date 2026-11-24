import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RecoveryConsole from '@/views/RecoveryConsole.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 检查点恢复为定制工作台，固定路径需排在动态模块路由之前。
    { path: '/stream_checkpoint/recovery', name: 'checkpoint-recovery', component: RecoveryConsole },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
