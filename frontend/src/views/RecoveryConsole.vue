<template>
  <section class="page" data-module="stream_checkpoint_recovery">
    <header class="page-head">
      <div>
        <h2>检查点恢复队列</h2>
        <p class="page-desc">
          把源分区偏移计算成分区缺口视图，从视图选中缺口安排补读，结果回写恢复台账；
          偏移按「分区 + 偏移」解释，不跨分区复用。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-all" type="button" @click="refreshAll">刷新</button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">缺口分区</span>
        <strong class="stat-value">{{ gapView.partitions.filter((row) => row.unconfirmed_count > 0).length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">未确认偏移</span>
        <strong class="stat-value">{{ gapView.total_gap_offsets }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">补读待重试</span>
        <strong class="stat-value">{{ ledger.pending }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">仍在写入分区</span>
        <strong class="stat-value">{{ gapView.writing_partitions.length }}</strong>
      </article>
    </div>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        :data-testid="`tab-${tab.key}`"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <p v-if="message" class="notice" :data-testid="'notice'">{{ message }}</p>

    <!-- ======================== Tab 1：分区缺口视图 ======================== -->
    <div v-if="activeTab === 'gap'" class="tab-panel" data-testid="panel-gap">
      <table class="data-table">
        <thead>
          <tr>
            <th>分区</th>
            <th>起始偏移</th>
            <th>当前水位</th>
            <th>未确认区间</th>
            <th>未确认条数</th>
            <th>仍在写入</th>
            <th>下一条写入偏移</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in gapView.partitions" :key="row.partition" :data-testid="`gap-row-${row.partition}`">
            <td>{{ row.partition }}</td>
            <td>{{ row.base_offset }}</td>
            <td>{{ row.watermark }}</td>
            <td :data-testid="`gap-ranges-${row.partition}`">
              <span v-if="row.gap_ranges.length">{{ formatRanges(row.gap_ranges) }}</span>
              <span v-else class="muted">无缺口</span>
            </td>
            <td>{{ row.unconfirmed_count }}</td>
            <td>
              <span :class="['badge', row.inflight ? 'badge-blue' : 'badge-gray']">
                {{ row.inflight ? '写入中' : '已停写' }}
              </span>
            </td>
            <td>{{ row.next_write_offset }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`late-confirm-${row.partition}`"
                @click="openLateConfirm(row)"
              >
                迟到确认
              </button>
              <button
                class="link"
                type="button"
                :disabled="!row.gap_ranges.length"
                :data-testid="`submit-recovery-${row.partition}`"
                @click="openRecovery(row)"
              >
                提交补读
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ======================== Tab 2：检查点恢复台账 ======================== -->
    <div v-else-if="activeTab === 'ledger'" class="tab-panel" data-testid="panel-ledger">
      <table class="data-table">
        <thead>
          <tr>
            <th>补读单号</th>
            <th>分区</th>
            <th>检查点</th>
            <th>目标表写入</th>
            <th>选中区间</th>
            <th>实际读取范围</th>
            <th>迟到跳过</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="item in ledger.items" :key="item.id">
            <tr :data-testid="`ledger-row-${item.id}`">
              <td>{{ item.recovery_id }}</td>
              <td>{{ item.partition }}</td>
              <td>{{ item.checkpoint_id }}</td>
              <td>{{ item.target_table }}</td>
              <td>{{ formatRanges(item.selected_ranges) }}</td>
              <td :data-testid="`ledger-read-${item.id}`">{{ formatRanges(item.read_ranges) }}</td>
              <td>{{ item.skipped_late_offsets.length ? item.skipped_late_offsets.join('，') : '—' }}</td>
              <td>
                <span :class="['badge', item.completed ? 'badge-green' : 'badge-amber']">
                  {{ item.status }}
                </span>
              </td>
              <td class="row-actions">
                <button class="link" type="button" @click="toggleDetail(item.id)">
                  {{ expandedId === item.id ? '收起明细' : '写入确认' }}
                </button>
                <button
                  v-if="!item.completed"
                  class="link"
                  type="button"
                  :data-testid="`retry-${item.id}`"
                  @click="handleRetry(item.id)"
                >
                  断线重试
                </button>
              </td>
            </tr>
            <tr v-if="expandedId === item.id" :data-testid="`ledger-detail-${item.id}`">
              <td colspan="9" class="detail-cell">
                <dl class="detail-grid">
                  <div>
                    <dt>已写入偏移</dt>
                    <dd>{{ item.written_offsets.length ? item.written_offsets.join('，') : '—' }}</dd>
                  </div>
                  <div>
                    <dt>尚未确认区间</dt>
                    <dd>{{ formatRanges(item.pending_ranges) }}</dd>
                  </div>
                  <div>
                    <dt>尝试次数</dt>
                    <dd>{{ item.attempts }}</dd>
                  </div>
                  <div>
                    <dt>幂等键</dt>
                    <dd>{{ item.idempotency_key || '—' }}</dd>
                  </div>
                </dl>
                <table class="data-table sub-table">
                  <thead>
                    <tr>
                      <th>尝试</th>
                      <th>类型</th>
                      <th>计划读取区间</th>
                      <th>实际读取偏移</th>
                      <th>写入确认偏移</th>
                      <th>是否断线</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="attempt in item.write_confirmations" :key="attempt.attempt">
                      <td>#{{ attempt.attempt }}</td>
                      <td>{{ attempt.kind === 'retry' ? '重试' : '首次' }}</td>
                      <td>{{ formatRanges(attempt.planned_ranges) }}</td>
                      <td>{{ attempt.read_offsets.join('，') || '—' }}</td>
                      <td>{{ attempt.confirmed_offsets.join('，') || '—' }}</td>
                      <td>{{ attempt.disconnected ? '是' : '否' }}</td>
                    </tr>
                  </tbody>
                </table>
              </td>
            </tr>
          </template>
          <tr v-if="!ledger.items.length">
            <td colspan="9" class="empty-state">暂无补读记录，可先在分区缺口视图提交补读</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ======================== Tab 3：检查点详情 ======================== -->
    <div v-else class="tab-panel" data-testid="panel-detail">
      <form class="filter-bar" @submit.prevent="loadCheckpointDetail">
        <label class="filter-item">
          <span>检查点</span>
          <select v-model="selectedCheckpoint" data-testid="checkpoint-select">
            <option v-for="cp in checkpoints" :key="cp.id" :value="String(cp['检查点标识'])">
              {{ cp['检查点标识'] }}
            </option>
          </select>
        </label>
        <button class="btn" data-testid="query-checkpoint" type="submit">查看详情</button>
      </form>

      <div v-if="checkpointDetail" class="detail-panel">
        <div class="stat-row">
          <article class="stat-card">
            <span class="stat-label">补读总数</span>
            <strong class="stat-value">{{ checkpointDetail.recovery_total }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">已完成补读</span>
            <strong class="stat-value">{{ checkpointDetail.recovery_completed }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">待完成补读</span>
            <strong class="stat-value">{{ checkpointDetail.recovery_pending }}</strong>
          </article>
          <article class="stat-card">
            <span class="stat-label">本次补读状态</span>
            <strong :data-testid="'checkpoint-completion'">
              <span :class="['badge', checkpointDetail.all_recovery_done ? 'badge-green' : 'badge-amber']">
                {{ checkpointDetail.all_recovery_done ? '已全部完成' : '进行中' }}
              </span>
            </strong>
          </article>
        </div>

        <h3>检查点视角的分区缺口</h3>
        <table class="data-table">
          <thead>
            <tr>
              <th>分区</th>
              <th>水位</th>
              <th>未确认区间</th>
              <th>未确认条数</th>
              <th>仍在写入</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in checkpointDetail.gap_view" :key="row.partition">
              <td>{{ row.partition }}</td>
              <td>{{ row.watermark }}</td>
              <td>{{ row.gap_ranges.length ? formatRanges(row.gap_ranges) : '无缺口' }}</td>
              <td>{{ row.unconfirmed_count }}</td>
              <td>{{ row.inflight ? '写入中' : '已停写' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- 迟到确认弹窗 -->
    <div v-if="lateConfirmRow" class="modal-backdrop" data-testid="late-confirm-modal">
      <form class="modal" @submit.prevent="handleLateConfirm">
        <h3>迟到确认 · 分区 {{ lateConfirmRow.partition }}</h3>
        <p class="page-desc">
          数据已实际入湖但确认通知晚到，登记后刷新视图，对应偏移会从缺口中剔除。
        </p>
        <label class="create-field">
          <span>已入湖偏移（逗号分隔）</span>
          <input v-model="lateConfirmOffsets" data-testid="late-confirm-offsets" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="lateConfirmRow = null">取消</button>
          <button class="btn primary" data-testid="late-confirm-submit" type="submit">确认入湖</button>
        </div>
      </form>
    </div>

    <!-- 提交补读弹窗 -->
    <div v-if="recoveryRow" class="modal-backdrop" data-testid="recovery-modal">
      <form class="modal" @submit.prevent="handleSubmitRecovery">
        <h3>提交补读 · 分区 {{ recoveryRow.partition }}</h3>
        <label class="create-field">
          <span>检查点</span>
          <select v-model="recoveryForm.checkpoint_id" data-testid="recovery-checkpoint">
            <option v-for="cp in checkpoints" :key="cp.id" :value="String(cp['检查点标识'])">
              {{ cp['检查点标识'] }}
            </option>
          </select>
        </label>
        <div class="create-field">
          <span>选中补读区间（来自当前缺口视图）</span>
          <label
            v-for="range in recoveryRow.gap_ranges"
            :key="`${range.start}-${range.end}`"
            class="check-line"
          >
            <input
              v-model="recoveryForm.selected"
              type="checkbox"
              :value="rangeKey(range)"
              data-testid="recovery-range-check"
            />
            区间 {{ formatRange(range) }}（{{ range.count }} 条）
          </label>
        </div>
        <label class="create-field">
          <span>幂等键（并发相同补读只确认一份）</span>
          <input v-model="recoveryForm.idempotency_key" data-testid="recovery-idempotency-key" />
        </label>
        <label class="check-line">
          <input v-model="recoveryForm.simulate_disconnect" type="checkbox" data-testid="recovery-disconnect" />
          模拟断线（首个偏移写入后中断，保留剩余未确认区间）
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="recoveryRow = null">取消</button>
          <button class="btn primary" data-testid="recovery-submit" type="submit">安排补读</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listEntries } from '@/api/backend-service'
import {
  confirmOffsets,
  fetchCheckpointDetail,
  fetchGapView,
  fetchRecoveryLedger,
  formatRanges,
  retryRecovery,
  submitRecovery,
  type CheckpointDetail,
  type GapRow,
  type GapView,
  type LedgerResult,
  type OffsetRange,
  type RecoveryRecord,
} from '@/api/recovery'
import type { EntryRow } from '@/data/types'

const tabs = [
  { key: 'gap', label: '分区缺口视图' },
  { key: 'ledger', label: '检查点恢复台账' },
  { key: 'detail', label: '检查点详情' },
] as const

type TabKey = (typeof tabs)[number]['key']

const activeTab = ref<TabKey>('gap')
const message = ref('')
const checkpoints = ref<EntryRow[]>([])

const emptyGapView: GapView = { ok: true, partitions: [], total_gap_offsets: 0, writing_partitions: [] }
const gapView = ref<GapView>(emptyGapView)
const ledger = ref<LedgerResult>({ ok: true, items: [], total: 0, pending: 0 })
const checkpointDetail = ref<CheckpointDetail | null>(null)
const selectedCheckpoint = ref('检查点样例1')
const expandedId = ref<number | null>(null)

const lateConfirmRow = ref<GapRow | null>(null)
const lateConfirmOffsets = ref('')
const recoveryRow = ref<GapRow | null>(null)
const recoveryForm = ref({
  checkpoint_id: '检查点样例1',
  selected: [] as string[],
  idempotency_key: '',
  simulate_disconnect: false,
})

function rangeKey(range: OffsetRange): string {
  return `${range.start}-${range.end}`
}
function formatRange(range: OffsetRange): string {
  return range.start === range.end ? String(range.start) : `${range.start}~${range.end}`
}

async function loadGapView() {
  gapView.value = await fetchGapView()
}

async function loadLedger() {
  ledger.value = await fetchRecoveryLedger()
}

async function loadCheckpoints() {
  const page = await listEntries('stream_checkpoint')
  checkpoints.value = page.items
}

async function loadCheckpointDetail() {
  checkpointDetail.value = await fetchCheckpointDetail(selectedCheckpoint.value)
}

async function refreshAll() {
  message.value = ''
  try {
    await Promise.all([loadGapView(), loadLedger(), loadCheckpoints()])
    if (activeTab.value === 'detail') await loadCheckpointDetail()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '刷新失败'
  }
}

async function switchTab(tab: TabKey) {
  activeTab.value = tab
  message.value = ''
  if (tab === 'gap') await loadGapView()
  if (tab === 'ledger') await loadLedger()
  if (tab === 'detail') await loadCheckpointDetail()
}

function openLateConfirm(row: GapRow) {
  lateConfirmRow.value = row
  lateConfirmOffsets.value = row.gap_ranges.slice(0, 1).flatMap((r) => [r.start, r.start + 1])
    .filter((offset) => offset <= row.watermark).join(',')
}

async function handleLateConfirm() {
  if (!lateConfirmRow.value) return
  const offsets = lateConfirmOffsets.value
    .split(/[，,]/)
    .map((item) => Number(item.trim()))
    .filter((item) => Number.isInteger(item))
  const result = await confirmOffsets(lateConfirmRow.value.partition, offsets)
  lateConfirmRow.value = null
  await loadGapView()
  message.value = result.ok
    ? `分区 ${result.partition} 新确认偏移：${result.newly_confirmed.join('，') || '无新增'}`
    : (result.message ?? '迟到确认失败')
}

function openRecovery(row: GapRow) {
  recoveryRow.value = row
  recoveryForm.value = {
    checkpoint_id: selectedCheckpoint.value,
    selected: row.gap_ranges.map(rangeKey),
    idempotency_key: `${row.partition}-${Date.now()}`,
    simulate_disconnect: false,
  }
}

async function handleSubmitRecovery() {
  if (!recoveryRow.value) return
  const partition = recoveryRow.value.partition
  const ranges: OffsetRange[] = recoveryRow.value.gap_ranges
    .filter((range) => recoveryForm.value.selected.includes(rangeKey(range)))
    .map((range) => ({ start: range.start, end: range.end, count: range.count }))
  if (!ranges.length) {
    message.value = '至少选择一个补读区间'
    return
  }
  const result = await submitRecovery({
    partition,
    ranges,
    checkpoint_id: recoveryForm.value.checkpoint_id,
    target_table: 'dwd.orders_lake',
    idempotency_key: recoveryForm.value.idempotency_key || undefined,
    simulate_disconnect: recoveryForm.value.simulate_disconnect,
  })
  recoveryRow.value = null
  await Promise.all([loadGapView(), loadLedger()])
  if (result.duplicate) {
    message.value = `并发重复提交已拦截：${result.message}，仅保留单号 ${result.recovery.recovery_id} 的一份写入`
  } else if (result.recovery.completed) {
    message.value = `分区 ${partition} 补读完成，实际读取 ${formatRanges(result.recovery.read_ranges)}，已写入目标表`
  } else {
    message.value = `分区 ${partition} 补读中断，单号 ${result.recovery.recovery_id} 仍有未确认区间，可在台账中断线重试`
  }
}

async function handleRetry(id: number) {
  const result = await retryRecovery(id)
  await Promise.all([loadGapView(), loadLedger()])
  message.value = result.ok
    ? `单号 ${result.recovery.recovery_id} 重试完成，补读状态：${result.recovery.status}`
    : result.message
}

function toggleDetail(id: number) {
  expandedId.value = expandedId.value === id ? null : id
}

onMounted(refreshAll)
</script>

<style scoped>
.tab-bar { display: flex; gap: 8px; margin-bottom: 12px; }
.tab-btn { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 16px; cursor: pointer; font-size: 13px; }
.tab-btn.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.notice { background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af; border-radius: 6px; padding: 8px 12px; font-size: 13px; }
.muted { color: var(--muted); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 12px; }
.badge-blue { background: #dbeafe; color: #1d4ed8; }
.badge-green { background: #dcfce7; color: #15803d; }
.badge-amber { background: #fef3c7; color: #b45309; }
.badge-gray { background: #f1f5f9; color: #64748b; }
.check-line { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-bottom: 6px; }
.detail-cell { background: #f8fafc; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { font-size: 13px; margin: 2px 0 0; }
.sub-table { margin-top: 8px; }
.detail-panel h3 { font-size: 14px; margin: 16px 0 8px; }
</style>
