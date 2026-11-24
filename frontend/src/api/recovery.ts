import { fetchJson } from './client'

/** 缺口区间：闭区间 [start, end]，count 为区间内偏移条数。 */
export type OffsetRange = {
  start: number
  end: number
  count: number
}

/** 分区缺口视图单行：未确认区间 + 仍在写入标记。 */
export type GapRow = {
  partition: string
  base_offset: number
  watermark: number
  confirmed_count: number
  gap_ranges: OffsetRange[]
  unconfirmed_count: number
  inflight: boolean
  next_write_offset: number
}

export type GapView = {
  ok: boolean
  partitions: GapRow[]
  total_gap_offsets: number
  writing_partitions: string[]
}

/** 单次补读尝试（写入确认明细）。 */
export type WriteConfirmation = {
  attempt: number
  kind: 'initial' | 'retry'
  planned_ranges: OffsetRange[]
  read_offsets: number[]
  written_offsets: number[]
  confirmed_offsets: number[]
  disconnected: boolean
}

/** 恢复台账记录：选中区间、读取范围、写入确认都可回查。 */
export type RecoveryRecord = {
  id: number
  recovery_id: string
  partition: string
  checkpoint_id: string
  target_table: string
  selected_ranges: OffsetRange[]
  read_ranges: OffsetRange[]
  skipped_late_offsets: number[]
  written_offsets: number[]
  pending_ranges: OffsetRange[]
  write_confirmations: WriteConfirmation[]
  attempts: number
  status: string
  completed: boolean
  disconnect: boolean
  idempotency_key?: string | null
}

export type RecoveryResult = {
  ok: boolean
  duplicate?: boolean
  message: string
  recovery: RecoveryRecord
}

export type LedgerResult = {
  ok: boolean
  items: RecoveryRecord[]
  total: number
  pending: number
}

export type CheckpointDetail = {
  ok: boolean
  checkpoint_id: string
  recovery_total: number
  recovery_completed: number
  recovery_pending: number
  all_recovery_done: boolean
  gap_view: GapRow[]
  recoveries: RecoveryRecord[]
}

export type ConfirmResult = {
  ok: boolean
  message?: string
  partition: string
  newly_confirmed: number[]
  already_confirmed: number[]
}

export async function fetchGapView(): Promise<GapView> {
  return fetchJson<GapView>('/api/stream_checkpoint/gap-view')
}

export async function confirmOffsets(partition: string, offsets: number[]): Promise<ConfirmResult> {
  return fetchJson<ConfirmResult>('/api/stream_checkpoint/confirm-offset', {
    method: 'POST',
    body: JSON.stringify({ partition, offsets }),
  })
}

export async function submitRecovery(payload: {
  partition: string
  ranges: OffsetRange[]
  checkpoint_id: string
  target_table: string
  idempotency_key?: string
  simulate_disconnect: boolean
}): Promise<RecoveryResult> {
  return fetchJson<RecoveryResult>('/api/stream_checkpoint/recovery', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function fetchRecoveryLedger(partition?: string): Promise<LedgerResult> {
  const query = partition ? `?partition=${encodeURIComponent(partition)}` : ''
  return fetchJson<LedgerResult>(`/api/stream_checkpoint/recovery-ledger${query}`)
}

export async function retryRecovery(id: number): Promise<RecoveryResult> {
  return fetchJson<RecoveryResult>(`/api/stream_checkpoint/recovery/${id}/retry`, {
    method: 'POST',
  })
}

export async function fetchCheckpointDetail(checkpointId: string): Promise<CheckpointDetail> {
  return fetchJson<CheckpointDetail>(
    `/api/stream_checkpoint/detail/${encodeURIComponent(checkpointId)}`,
  )
}

/** 区间渲染成 101~104 形式，单点只显示一个数字。 */
export function formatRange(range: OffsetRange): string {
  return range.start === range.end ? String(range.start) : `${range.start}~${range.end}`
}

export function formatRanges(ranges: OffsetRange[]): string {
  return ranges.length ? ranges.map(formatRange).join('，') : '—'
}
