<template>
  <section
    v-if="state"
    class="coverage-panel"
    data-testid="sample-coverage-panel"
    :data-revision="state.report.revision"
  >
    <header class="coverage-head">
      <div>
        <h3>样本覆盖对照明细</h3>
        <p>
          多来源日志的原始分母、去重重复组、归并代表与实际覆盖；展开来源可核对每一条实际代表。
        </p>
      </div>
      <div class="coverage-actions">
        <button class="btn" type="button" data-testid="coverage-refresh" @click="load">刷新</button>
        <button class="btn" type="button" data-testid="coverage-reset" @click="reset">重置剧本</button>
        <button
          class="btn primary"
          type="button"
          data-testid="coverage-run-scenario"
          :disabled="busy"
          @click="runScenario"
        >
          {{ busy ? '运行中…' : '运行重启/更正/并发剧本' }}
        </button>
        <button
          class="btn primary"
          type="button"
          data-testid="coverage-claim"
          :disabled="busy || !state.report.priority_gap"
          @click="claim"
        >
          领取当前优先缺口
        </button>
      </div>
    </header>

    <div class="coverage-metrics">
      <span>明细版本：<b data-testid="coverage-revision">{{ state.report.revision }}</b></span>
      <span>口径：{{ basisLabel }}</span>
      <span>原始分母：<b data-testid="coverage-count-raw">{{ state.report.counts.raw }}</b></span>
      <span>去重后：<b data-testid="coverage-count-dedup">{{ state.report.counts.dedup }}</b></span>
      <span>重复组：<b>{{ state.report.counts.duplicate_groups }}</b></span>
      <span>归并组：<b data-testid="coverage-count-merged">{{ state.report.counts.merged_groups }}</b></span>
      <span>已覆盖：<b>{{ state.report.counts.covered_groups }}</b></span>
      <span>未覆盖：<b data-testid="coverage-count-uncovered">{{ state.report.counts.uncovered_groups }}</b></span>
    </div>

    <p v-if="state.report.unreceipted_deliveries?.length" class="coverage-warning" data-testid="coverage-unreceipted">
      采样台账已派出但覆盖对照尚无回执：{{ state.report.unreceipted_deliveries.join('、') }}。
      重开后先核清交付，不能重复发送。
    </p>
    <p v-if="message" class="coverage-message" data-testid="coverage-message">{{ message }}</p>
    <p v-if="error" class="error-text" data-testid="coverage-error">{{ error }}</p>

    <div class="squeezed-grid" data-testid="coverage-squeezed-out">
      <div>
        <h4>被去重挤掉的原行</h4>
        <span v-for="item in state.report.squeezed_out.dedup_duplicate_lines" :key="`d-${item.line_id}`" class="badge warn">
          {{ item.line_id }}→{{ item.kept_representative_id }}
        </span>
        <span v-if="!state.report.squeezed_out.dedup_duplicate_lines.length" class="muted">无</span>
      </div>
      <div>
        <h4>被归并代表合并的原行</h4>
        <span v-for="item in state.report.squeezed_out.merged_non_representative_lines" :key="`m-${item.line_id}`" class="badge">
          {{ item.line_id }}→{{ item.kept_representative_id }}
        </span>
        <span v-if="!state.report.squeezed_out.merged_non_representative_lines.length" class="muted">无</span>
      </div>
      <div>
        <h4>被额度挤出的未覆盖组</h4>
        <span v-for="item in state.report.squeezed_out.uncovered_groups" :key="`g-${item.group_key}`" class="badge gap">
          {{ item.source_id }}:{{ item.group_key }}→{{ item.candidate_line_id }}
        </span>
        <span v-if="!state.report.squeezed_out.uncovered_groups.length" class="muted">无</span>
      </div>
    </div>

    <div v-if="state.report.priority_gap" class="priority-gap" data-testid="coverage-priority-gap">
      当前优先来源：
      <b>{{ state.report.priority_gap.source_id }}</b>
      ｜实际未覆盖组：{{ state.report.priority_gap.uncovered_group_count }}
      ｜先补原行：<b data-testid="coverage-priority-line">{{ state.report.priority_gap.line_id }}</b>
      <div class="ranking">
        <span v-for="item in state.report.priority_gap.ranking" :key="item.source_id">
          {{ item.source_id }}={{ item.uncovered_group_count }}
        </span>
      </div>
    </div>

    <table class="coverage-table" data-testid="coverage-table">
      <thead>
        <tr>
          <th style="width: 42px">展开</th>
          <th>来源</th>
          <th>原始分母</th>
          <th>原始 → 去重</th>
          <th>归并组分母</th>
          <th>覆盖</th>
          <th>未覆盖</th>
          <th>优先候选</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="source in state.report.sources" :key="source.source_id">
          <tr :data-testid="`coverage-source-row-${source.source_id}`">
            <td>
              <button
                class="link"
                type="button"
                :data-testid="`coverage-expand-${source.source_id}`"
                :aria-expanded="Boolean(expanded[source.source_id])"
                @click="toggle(source.source_id)"
              >
                {{ expanded[source.source_id] ? '收起' : '展开' }}
              </button>
            </td>
            <td><b>{{ source.source_id }}</b></td>
            <td :data-testid="`coverage-denominator-${source.source_id}`">{{ source.raw_denominator }}</td>
            <td>{{ source.raw_count }} → {{ source.dedup_count }}</td>
            <td>{{ source.merged_denominator }}</td>
            <td>{{ source.covered_group_count }}/{{ source.merged_denominator }}</td>
            <td>{{ source.uncovered_group_count }}</td>
            <td data-testid="`coverage-candidate-${source.source_id}`">
              {{ firstCandidate(source) || '—' }}
            </td>
          </tr>
          <tr v-if="expanded[source.source_id]" :data-testid="`coverage-detail-${source.source_id}`">
            <td></td>
            <td colspan="7" class="coverage-detail-cell">
              <div class="detail-grid">
                <section>
                  <h4>① 原始段（分母 {{ source.raw_denominator }}，列出实际原行）</h4>
                  <ul class="line-list">
                    <li v-for="line in source.raw_lines" :key="line.line_id">
                      <code>{{ line.line_id }}</code>
                      <span>{{ line.occurred_at }}</span>
                      <span class="muted">{{ line.raw_text }}</span>
                    </li>
                  </ul>
                </section>

                <section>
                  <h4>② 去重段（实际 {{ source.dedup_count }}，重复组 {{ source.duplicate_group_count }}）</h4>
                  <div v-for="group in source.dedup_groups" :key="group.fingerprint" class="detail-card">
                    <div>
                      代表 <code>{{ group.representative_id }}</code>
                      <span v-if="group.duplicate_ids.length" class="badge warn">
                        重复：{{ group.duplicate_ids.join('、') }}
                      </span>
                      <span v-else class="badge">无重复</span>
                    </div>
                    <div class="muted">指纹 {{ group.fingerprint.slice(0, 16) }}…</div>
                  </div>
                </section>

                <section>
                  <h4>③ 已归并段（组分母 {{ source.merged_denominator }}，列出实际代表）</h4>
                  <div
                    v-for="group in source.merged_groups"
                    :key="group.group_key"
                    class="detail-card"
                    :data-covered="group.covered"
                  >
                    <div class="group-title">
                      <code>{{ group.group_key }}</code>
                      <span class="badge" :class="group.covered ? 'ok' : 'gap'">
                        {{ group.covered ? '已覆盖' : '未覆盖' }}
                      </span>
                    </div>
                    <div>归并代表：<code>{{ group.representative_id }}</code></div>
                    <div>原始成员：{{ group.raw_member_ids.join('、') }}</div>
                    <div>去重后成员：{{ group.deduped_member_ids.join('、') }}</div>
                    <div v-if="group.candidate_line_id">
                      最早待补原行：<b>{{ group.candidate_line_id }}</b>
                      （待补 {{ group.pending_line_ids.join('、') }}）
                    </div>
                    <div v-if="group.delivered_line_ids.length">
                      交付台账：{{ group.delivered_line_ids.join('、') }}
                    </div>
                    <div v-if="group.delivery_without_comparison_receipt.length" class="coverage-warning">
                      缺对照回执：{{ group.delivery_without_comparison_receipt.join('、') }}
                    </div>
                  </div>
                </section>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <section v-if="state.orders?.length" class="orders-panel">
      <h4>补抽领用与独立确认</h4>
      <table class="coverage-table">
        <thead>
          <tr>
            <th>单号</th><th>采用明细</th><th>来源/组/原行</th><th>状态</th>
            <th>明细确认</th><th>交付确认</th><th>并发/去重</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="order in state.orders" :key="order.order_id" :data-testid="`order-row-${order.order_id}`">
            <td>{{ order.order_id }}</td>
            <td>{{ order.detail_revision }}</td>
            <td>{{ order.source_id }} / {{ order.group_key }} / <b>{{ order.line_id }}</b></td>
            <td>{{ order.status }}</td>
            <td>{{ order.detail_confirmed ? '是' : '否' }}</td>
            <td>{{ order.delivery_confirmed ? '是' : '否' }}</td>
            <td>{{ order.confirm_calls }} / {{ order.duplicate_confirmations }}</td>
            <td class="order-actions">
              <button class="link" type="button" @click="sample(order.order_id)">sample 取样</button>
              <button class="link" type="button" @click="reconcile(order.order_id)">核清交付</button>
              <button class="link" type="button" :data-testid="`confirm-${order.order_id}`" @click="confirm(order.order_id)">
                确认
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-for="order in state.orders" :key="`conflict-${order.order_id}`">
        <p v-if="order.conflict_with_current" class="coverage-warning">
          {{ order.order_id }} 新旧范围冲突：旧明细应补 {{ order.conflict_with_current.adopted_line_id }}，
          当前整份对照优先 {{ order.conflict_with_current.current_line_id }}；
          处理：{{ order.conflict_with_current.resolution }}。
          可重算结果：{{ order.recomputed_choice?.line_id }}。
        </p>
      </div>
    </section>

    <details class="ledger-panel" data-testid="coverage-ledger">
      <summary>采样交付台账与时间线（实际发出原行可回查）</summary>
      <p>
        <b data-testid="coverage-delivery-lines">{{ deliveryLines }}</b>
      </p>
      <ul>
        <li v-for="delivery in state.deliveries" :key="delivery.delivery_id">
          {{ delivery.delivery_id }} → 原行 <code>{{ delivery.line_id }}</code>
          （来源 {{ delivery.source_id }}，{{ delivery.carrier_status }}，幂等键 {{ delivery.idempotency_key }}）
        </li>
      </ul>
      <ol class="timeline-list">
        <li v-for="event in state.timeline" :key="event.seq">
          <time>{{ event.at }}</time>
          <span class="badge">{{ event.stage }}</span>
          {{ event.message }}
        </li>
      </ol>
    </details>
  </section>

  <section v-else class="coverage-panel" data-testid="sample-coverage-panel-loading">
    <p>样本覆盖对照加载中；若后端未启动，请先运行 FastAPI 服务。</p>
    <p v-if="error" class="error-text">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type CoverageState = Record<string, any>

const state = ref<CoverageState | null>(null)
const expanded = ref<Record<string, boolean>>({ A: true, B: true, C: true })
const busy = ref(false)
const error = ref('')
const message = ref('')

const basisLabel = computed(() => {
  if (!state.value) return ''
  return state.value.report.selection_basis === 'delivery_reconciled'
    ? '已与采样交付台账核对'
    : '仅覆盖回执，待核交付'
})

const deliveryLines = computed(() =>
  (state.value?.deliveries ?? []).map((item: Record<string, unknown>) => item.line_id).join('、'),
)

function firstCandidate(source: Record<string, any>): string {
  const group = source.merged_groups?.find((item: Record<string, any>) => item.candidate_line_id)
  return group?.candidate_line_id ?? ''
}

function toggle(sourceId: string) {
  expanded.value[sourceId] = !expanded.value[sourceId]
}

async function readJson(response: Response) {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload.detail ?? payload.message ?? `接口返回 ${response.status}`)
  }
  return payload
}

async function load() {
  error.value = ''
  try {
    const response = await request('/api/sample-coverage')
    state.value = await readJson(response)
  } catch (err) {
    error.value = err instanceof Error ? err.message : '覆盖对照读取失败'
  }
}

async function post(path: string, body: Record<string, unknown> = {}) {
  const response = await request(`/api/sample-coverage${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return readJson(response)
}

async function runScenario() {
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const payload = await post('/scenarios/run')
    state.value = payload.state
    message.value = '剧本完成：C1 沿用旧领用确认，更正后整份对照优先 B2；每轮并发确认仅一个结果。'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '剧本运行失败'
  } finally {
    busy.value = false
  }
}

async function reset() {
  busy.value = true
  try {
    const payload = await post('/reset')
    state.value = payload.state
    message.value = '已恢复初始场景'
  } catch (err) {
    error.value = err instanceof Error ? err.message : '重置失败'
  } finally {
    busy.value = false
  }
}

async function claim() {
  try {
    const payload = await post('/claims')
    message.value = payload.message
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '领取失败'
  }
}

async function sample(orderId: string) {
  try {
    const payload = await post(`/claims/${orderId}/sample`)
    message.value = payload.message
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '取样失败'
  }
}

async function reconcile(orderId: string) {
  try {
    const payload = await post(`/claims/${orderId}/reconcile`)
    message.value = payload.message
    state.value = payload.state
  } catch (err) {
    error.value = err instanceof Error ? err.message : '交付核对失败'
  }
}

async function confirm(orderId: string) {
  try {
    const payload = await post(`/claims/${orderId}/confirm`, { actor: 'workbench-duty' })
    message.value = payload.deduplicated ? '重复确认已去重，仍只保留原结果' : payload.message
    await load()
  } catch (err) {
    error.value = err instanceof Error ? err.message : '确认失败'
  }
}

onMounted(load)
</script>

<style scoped>
.coverage-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
  margin: 12px 0 16px;
}
.coverage-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}
.coverage-head h3 { margin: 0 0 4px; font-size: 16px; }
.coverage-head p { margin: 0; color: var(--muted); font-size: 12px; }
.coverage-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
.coverage-metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 12px 0;
  padding: 10px;
  background: #f8fafc;
  border-radius: 8px;
  font-size: 13px;
}
.priority-gap {
  border-left: 3px solid var(--brand);
  background: #eff6ff;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 10px;
}
.ranking { display: flex; gap: 10px; margin-top: 6px; color: var(--muted); font-size: 12px; }
.squeezed-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 10px; }
.squeezed-grid h4 { margin: 0 0 6px; font-size: 12px; color: var(--muted); }
.squeezed-grid > div { border: 1px dashed var(--border); border-radius: 6px; padding: 8px; display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
.coverage-table { margin-top: 8px; }
.coverage-detail-cell { background: #fbfdff; }
.detail-grid { display: grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap: 12px; }
.detail-grid h4 { margin: 0 0 8px; font-size: 13px; }
.detail-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  margin-bottom: 8px;
  font-size: 12px;
  display: grid;
  gap: 4px;
}
.group-title { display: flex; align-items: center; gap: 8px; }
.line-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }
.line-list li { display: grid; gap: 2px; border-bottom: 1px dashed var(--border); padding-bottom: 4px; font-size: 12px; }
.badge {
  display: inline-block;
  border-radius: 999px;
  padding: 1px 8px;
  font-size: 11px;
  border: 1px solid var(--border);
  background: #f1f5f9;
}
.badge.ok { background: #dcfce7; color: #166534; border-color: #86efac; }
.badge.gap { background: #fee2e2; color: #991b1b; border-color: #fca5a5; }
.badge.warn { background: #fef3c7; color: #92400e; border-color: #fcd34d; }
.coverage-warning {
  background: #fffbeb;
  border: 1px solid #fcd34d;
  color: #92400e;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
}
.coverage-message { color: #166534; font-size: 13px; }
.orders-panel { margin-top: 16px; }
.order-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.ledger-panel { margin-top: 14px; font-size: 13px; }
.timeline-list { max-height: 260px; overflow: auto; padding-left: 20px; display: grid; gap: 6px; }
.timeline-list time { color: var(--muted); font-size: 11px; margin-right: 6px; }
.muted { color: var(--muted); }
@media (max-width: 1100px) {
  .detail-grid, .squeezed-grid { grid-template-columns: 1fr; }
}
</style>
