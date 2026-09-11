<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import {
  NAlert,
  NButton,
  NCard,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NDivider,
  NEmpty,
  NForm,
  NFormItem,
  NGi,
  NGrid,
  NIcon,
  NInput,
  NInputNumber,
  NModal,
  NRadioButton,
  NRadioGroup,
  NSelect,
  NSpace,
  NSpin,
  NSwitch,
  NTag,
  NText,
  useMessage,
  type DataTableColumns,
} from 'naive-ui'
import {
  CashOutline,
  OpenOutline,
  PieChartOutline,
  PlayCircleOutline,
  PulseOutline,
  RefreshOutline,
  SaveOutline,
  ShieldCheckmarkOutline,
  StatsChartOutline,
  StopCircleOutline,
  TrendingDownOutline,
  TrendingUpOutline,
  WalletOutline,
} from '@vicons/ionicons5'
import { use as useECharts } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import VChart from 'vue-echarts'
import {
  getPaperBotConfig,
  getPaperBotAccount,
  getPaperBotJournal,
  getPaperBotLedger,
  getPaperBotStatus,
  getBacktestHistory,
  getPositions,
  getResolvedFees,
  getScannerOpportunities,
  getStrategy,
  getVenues,
  post,
  startPaperBot,
  stopPaperBot,
  type BacktestDailyLog,
  type BacktestResult,
  type BacktestScanRun,
  type BacktestTrade,
  type PaperBotAccount,
  type PaperBotLedgerEntry,
  type OpportunityItem,
  type PaperBotSummary,
  type PositionItem,
  type StrategyParams,
} from '@/composables/useApi'
import { useI18n } from 'vue-i18n'

useECharts([LineChart, GridComponent, TooltipComponent, CanvasRenderer])

type PageMode = 'trade' | 'backtest'
type BotState = 'qualified' | 'waiting'
type StatusFilter = 'open' | 'closed' | 'all'

interface BotCandidate extends OpportunityItem {
  id: string
  amount_usd: number
  open_fee_usd: number
  round_trip_fee_usd: number
  long_settle_ms?: number
  short_settle_ms?: number
  max_exec_usd?: number
  depth_ok?: boolean
  state: BotState
}

interface BotRules {
  initialBalanceUsdt: number
  depthMultiple: number
  consecutiveHits: number
  minSettleMinutes: number
  maxSettleMinutes: number
  maxHoldHours: number
  maxActionsPerRun: number
  activeExitEnabled: boolean
  activeExitConfirmMinutes: number
  activeExitWindowMinutes: number
  activeExitMaxMarkSpreadPct: number
}

interface JournalRow {
  id: string
  ts: string
  action: string
  pair: string
  route: string
  edge: number | null
  reason: string
  result: string
}

interface BacktestScanRow extends BacktestScanRun {
  id: string
  actionSummary: string
  skippedSummary: string
}

const { t } = useI18n()
const message = useMessage()
const scanner = getScannerOpportunities('pure')
const positions = getPositions()
const strategy = getStrategy()
const venues = getVenues()
const resolvedFees = getResolvedFees()
const botConfig = getPaperBotConfig()
const botAccount = getPaperBotAccount()
const botStatus = getPaperBotStatus()
const botJournal = getPaperBotJournal(50)
const botLedger = getPaperBotLedger(100)
const backtestHistory = getBacktestHistory()

const pageMode = ref<PageMode>('trade')
const botEnabled = ref(false)
const saving = ref(false)
const runningOnce = ref(false)
const autoBusy = ref(false)
const statusFilter = ref<StatusFilter>('open')
const backtestRunning = ref(false)
const backtestResult = ref<BacktestResult | null>(null)
const backtestError = ref('')
const backtestSynced = ref(false)
let pollTimer: number | null = null
let polling = false

const strategyForm = reactive({
  min_spread_annual: 0,
  min_edge_annual: 0,
  max_mark_spread_pct: 0,
  trade_usd: 0,
  max_positions: 0,
  scan_interval_sec: 0,
  scan_venues: [] as string[],
  min_edge_1h: 0,
  min_edge_mismatch: 0,
  fee_mode: 'auto' as 'auto' | 'api' | 'vip_tier',
  venue_fee_tiers: {} as Record<string, string>,
})

const botRules = reactive<BotRules>({
  initialBalanceUsdt: 100000,
  depthMultiple: 3,
  consecutiveHits: 2,
  minSettleMinutes: 10,
  maxSettleMinutes: 90,
  maxHoldHours: 9,
  maxActionsPerRun: 5,
  activeExitEnabled: true,
  activeExitConfirmMinutes: 2,
  activeExitWindowMinutes: 60,
  activeExitMaxMarkSpreadPct: 0.15,
})

const backtestForm = reactive({
  historyBases: '',
  historyBaseLimit: 80,
  historyDays: 90,
  historyVenues: [] as string[],
  capital: 100000,
  tradeUsd: 5000,
  minSpread: 0.08,
  minEdge: 0.01,
  minEdge1h: 0,
  minEdgeMismatch: 0,
  exitEdge: 0.01,
  maxMarkSpread: 1,
  maxPositions: 3,
  maxHoldingHours: 9,
  consecutiveHits: 2,
  minSettleMinutes: 10,
  maxSettleMinutes: 90,
  maxActionsPerRun: 5,
  activeExitEnabled: true,
  activeExitConfirmMinutes: 2,
  activeExitWindowMinutes: 60,
  activeExitMaxMarkSpreadPct: 0.15,
  basisCostPct: 0.05,
  allowMismatch: false,
})

const loading = computed(() =>
  scanner.loading.value ||
  positions.loading.value ||
  strategy.loading.value ||
  venues.loading.value ||
  resolvedFees.loading.value ||
  botAccount.loading.value ||
  botStatus.loading.value,
)

const scanVenueOptions = computed(() =>
  (venues.data.value ?? []).map((venue) => ({ label: venue.name, value: venue.id })),
)

const tradeUsd = computed(() => strategyForm.trade_usd || 5000)

const hasBacktestResult = computed(() => backtestResult.value !== null)

function isPaperBotBacktest(row: BacktestResult | null | undefined): boolean {
  if (!row) return false
  return row.mode === 'paper_bot' || row.id.startsWith('pbot-bt-')
}

const isPaperBotBacktestResult = computed(() => isPaperBotBacktest(backtestResult.value))
const selectedBacktestIsLegacy = computed(() => hasBacktestResult.value && !isPaperBotBacktestResult.value)

function hydrateStrategyForm() {
  const s = strategy.data.value
  if (!s) return
  strategyForm.min_spread_annual = s.min_spread_annual
  strategyForm.min_edge_annual = s.min_edge_annual
  strategyForm.max_mark_spread_pct = s.max_mark_spread_pct
  strategyForm.trade_usd = s.trade_usd
  strategyForm.max_positions = s.max_positions
  strategyForm.scan_interval_sec = s.scan_interval_sec
  strategyForm.scan_venues = Array.isArray(s.scan_venues) ? [...s.scan_venues] : []
  strategyForm.min_edge_1h = typeof s.min_edge_1h === 'number' ? s.min_edge_1h : 0
  strategyForm.min_edge_mismatch = typeof s.min_edge_mismatch === 'number' ? s.min_edge_mismatch : 0
  strategyForm.fee_mode = s.fee_mode ?? 'auto'
  strategyForm.venue_fee_tiers = s.venue_fee_tiers ? { ...s.venue_fee_tiers } : {}
}

function hydrateBotRules() {
  const cfg = botConfig.data.value ?? botStatus.data.value?.config
  if (!cfg) return
  botEnabled.value = cfg.enabled
  botRules.initialBalanceUsdt = cfg.initialBalanceUsdt ?? 100000
  botRules.depthMultiple = cfg.depthMultiple
  botRules.consecutiveHits = cfg.consecutiveHits
  botRules.minSettleMinutes = cfg.minSettleMinutes
  botRules.maxSettleMinutes = cfg.maxSettleMinutes
  botRules.maxHoldHours = cfg.maxHoldHours
  botRules.maxActionsPerRun = cfg.maxActionsPerRun
  botRules.activeExitEnabled = cfg.activeExitEnabled ?? true
  botRules.activeExitConfirmMinutes = cfg.activeExitConfirmMinutes ?? 2
  botRules.activeExitWindowMinutes = cfg.activeExitWindowMinutes ?? 60
  botRules.activeExitMaxMarkSpreadPct = cfg.activeExitMaxMarkSpreadPct ?? 0.15
}

function syncBacktestFromBot(showSuccess = false) {
  backtestForm.capital = botRules.initialBalanceUsdt || backtestForm.capital
  backtestForm.tradeUsd = strategyForm.trade_usd || backtestForm.tradeUsd
  backtestForm.minSpread = strategyForm.min_spread_annual || backtestForm.minSpread
  backtestForm.minEdge = strategyForm.min_edge_annual || backtestForm.minEdge
  backtestForm.minEdge1h = strategyForm.min_edge_1h || 0
  backtestForm.minEdgeMismatch = strategyForm.min_edge_mismatch || 0
  backtestForm.maxMarkSpread = strategyForm.max_mark_spread_pct || backtestForm.maxMarkSpread
  backtestForm.maxPositions = strategyForm.max_positions || backtestForm.maxPositions
  backtestForm.maxHoldingHours = botRules.maxHoldHours || backtestForm.maxHoldingHours
  backtestForm.consecutiveHits = botRules.consecutiveHits || backtestForm.consecutiveHits
  backtestForm.minSettleMinutes = botRules.minSettleMinutes
  backtestForm.maxSettleMinutes = botRules.maxSettleMinutes
  backtestForm.maxActionsPerRun = botRules.maxActionsPerRun || backtestForm.maxActionsPerRun
  backtestForm.activeExitEnabled = botRules.activeExitEnabled
  backtestForm.activeExitConfirmMinutes = botRules.activeExitConfirmMinutes
  backtestForm.activeExitWindowMinutes = botRules.activeExitWindowMinutes
  backtestForm.activeExitMaxMarkSpreadPct = botRules.activeExitMaxMarkSpreadPct
  backtestForm.historyVenues = Array.isArray(strategyForm.scan_venues) ? [...strategyForm.scan_venues] : []
  backtestForm.allowMismatch = (strategyForm.min_edge_mismatch ?? 0) > 0

  const exitThreshold = botStatus.data.value?.last_summary?.thresholds?.exitThresholdPct
  if (typeof exitThreshold === 'number') {
    backtestForm.exitEdge = exitThreshold
  }

  backtestSynced.value = true
  if (showSuccess) message.success(t('paperBot.backtestSynced'))
}

function botConfigPayload(enabled = botEnabled.value) {
  return {
    enabled,
    initialBalanceUsdt: botRules.initialBalanceUsdt,
    depthMultiple: botRules.depthMultiple,
    consecutiveHits: botRules.consecutiveHits,
    minSettleMinutes: botRules.minSettleMinutes,
    maxSettleMinutes: botRules.maxSettleMinutes,
    maxHoldHours: botRules.maxHoldHours,
    maxActionsPerRun: botRules.maxActionsPerRun,
    activeExitEnabled: botRules.activeExitEnabled,
    activeExitConfirmMinutes: botRules.activeExitConfirmMinutes,
    activeExitWindowMinutes: botRules.activeExitWindowMinutes,
    activeExitMaxMarkSpreadPct: botRules.activeExitMaxMarkSpreadPct,
  }
}

function thresholdFor(item: OpportunityItem): number {
  if (item.settle_mismatch && strategyForm.min_edge_mismatch > 0) {
    return strategyForm.min_edge_mismatch
  }
  if (item.long_interval_h === 1 && item.short_interval_h === 1 && strategyForm.min_edge_1h > 0) {
    return strategyForm.min_edge_1h
  }
  return strategyForm.min_edge_annual
}

const allCandidates = computed<BotCandidate[]>(() => {
  const data = scanner.data.value
  if (!data) return []
  return [...data.forward, ...data.reverse]
    .map((item, idx) => {
      const extra = item as OpportunityItem & {
        long_settle_ms?: number
        short_settle_ms?: number
        max_exec_usd?: number
        depth_ok?: boolean
      }
      const feePct = item.fee_pct ?? 0
      const roundTripPct = item.round_trip_fee_pct ?? feePct * 2
      const amount = tradeUsd.value
      const sameInterval = item.same_interval ?? !item.settle_mismatch
      const realEdge = item.real_edge_pct ?? item.net_edge_pct ?? 0
      const markSpread = Math.abs(item.mark_spread_pct ?? 0)
      const minEdge = thresholdFor(item)
      const enoughDepth = extra.max_exec_usd === undefined
        ? true
        : extra.max_exec_usd >= amount * botRules.depthMultiple
      const qualified = Boolean(
        sameInterval &&
        realEdge >= minEdge &&
        markSpread <= strategyForm.max_mark_spread_pct &&
        enoughDepth &&
        extra.depth_ok !== false,
      )
      return {
        ...item,
        id: `${item.base}-${item.long_venue}-${item.short_venue}-${item.direction ?? idx}`,
        amount_usd: amount,
        open_fee_usd: amount * (feePct / 100),
        round_trip_fee_usd: amount * (roundTripPct / 100),
        long_settle_ms: extra.long_settle_ms,
        short_settle_ms: extra.short_settle_ms,
        max_exec_usd: extra.max_exec_usd,
        depth_ok: extra.depth_ok,
        state: (qualified ? 'qualified' : 'waiting') as BotState,
      }
    })
    .sort((a, b) => (b.real_edge_pct ?? 0) - (a.real_edge_pct ?? 0))
})

const qualifiedCandidates = computed(() => allCandidates.value.filter((row) => row.state === 'qualified'))
const waitingCandidates = computed(() => allCandidates.value.filter((row) => row.state === 'waiting'))

function manualHistoryBases(): string {
  return backtestForm.historyBases
    .split(',')
    .map((base) => base.trim().toUpperCase())
    .filter(Boolean)
    .join(',')
}

function backtestHistoryBasesPayload(): string | null {
  const manual = manualHistoryBases()
  if (manual) return manual
  return null
}

function isBotManagedPosition(row: PositionItem): boolean {
  return row.managed_by === 'paper_bot' || row.opened_by === 'paper_bot' || row.source === 'paper_bot'
}

const paperItems = computed(() =>
  (positions.data.value ?? []).filter((row) => row.dry_run === true && isBotManagedPosition(row)),
)

const manualPaperItems = computed(() =>
  (positions.data.value ?? []).filter((row) =>
    row.dry_run === true &&
    row.status === 'open' &&
    !isBotManagedPosition(row),
  ),
)

const filteredPositionItems = computed(() => {
  if (statusFilter.value === 'all') return paperItems.value
  return paperItems.value.filter((row) => row.status === statusFilter.value)
})

function toMs(ts: string | number | undefined): number | null {
  if (!ts) return null
  if (typeof ts === 'number') return ts
  const ms = new Date(ts).getTime()
  return Number.isNaN(ms) ? null : ms
}

function formatTime(ts: string | number | undefined): string {
  const ms = toMs(ts)
  if (ms === null) return '-'
  return new Date(ms).toLocaleString(navigator.language, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function formatDuration(openedAt: string | number | undefined, closedAt?: string | number): string {
  const start = toMs(openedAt)
  if (start === null) return '-'
  let end = Date.now()
  if (closedAt) {
    const close = toMs(closedAt)
    if (close !== null) end = close
  }
  const diff = Math.max(0, end - start)
  const days = Math.floor(diff / 86400000)
  const hours = Math.floor((diff % 86400000) / 3600000)
  const mins = Math.floor((diff % 3600000) / 60000)
  const parts: string[] = []
  if (days > 0) parts.push(`${days}${t('positions.days')}`)
  if (hours > 0) parts.push(`${hours}${t('positions.hours')}`)
  parts.push(`${mins}${t('positions.minutes')}`)
  return parts.join(' ')
}

function holdHours(openedAt: string | number | undefined, closedAt?: string | number): number {
  const start = toMs(openedAt)
  if (start === null) return 0
  let end = Date.now()
  if (closedAt) {
    const close = toMs(closedAt)
    if (close !== null) end = close
  }
  return Math.max(0, (end - start) / 3600000)
}

function fmtUsd(v: number | undefined | null, signed = false): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '-'
  const sign = signed && v >= 0 ? '+' : ''
  return sign + '$' + v.toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function fmtPct(v: number | undefined | null, digits = 4): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '-'
  return v.toFixed(digits) + '%'
}

function fmtPrice(v: number | undefined | null): string {
  if (v === undefined || v === null || Number.isNaN(v)) return '-'
  return v.toLocaleString(undefined, { maximumFractionDigits: 6 })
}

function getOpenPrices(p: PositionItem): { long?: number; short?: number; futures?: number; spot?: number } {
  if (p.futures_venue || p.spot_venue) {
    return { futures: p.futures_price, spot: p.spot_price }
  }
  return { long: p.long_price, short: p.short_price }
}

function getClosePrices(p: PositionItem): { long?: number; short?: number; futures?: number; spot?: number } {
  const ci = p.close_info
  if (!ci) return getOpenPrices(p)
  if (ci.futures_price !== undefined || ci.spot_price !== undefined) {
    return { futures: ci.futures_price as number, spot: ci.spot_price as number }
  }
  if (ci.long_price === undefined && ci.short_price === undefined) return getOpenPrices(p)
  return { long: ci.long_price as number, short: ci.short_price as number }
}

function realizedPnl(p: PositionItem): number | null {
  if (p.status !== 'closed' || !p.close_info) return null
  const qty = p.qty ?? 0
  if (qty <= 0) return null
  const open = getOpenPrices(p)
  const close = getClosePrices(p)
  if (open.long !== undefined && close.long !== undefined && open.short !== undefined && close.short !== undefined) {
    return (close.long - open.long) * qty + (open.short - close.short) * qty
  }
  if (open.futures !== undefined && close.futures !== undefined) {
    const futPnl = p.direction === 'forward'
      ? (open.futures - close.futures) * qty
      : (close.futures - open.futures) * qty
    let spotPnl = 0
    if (open.spot !== undefined && close.spot !== undefined) {
      spotPnl = p.direction === 'forward'
        ? (close.spot - open.spot) * qty
        : (open.spot - close.spot) * qty
    }
    return futPnl + spotPnl
  }
  return null
}

function unrealizedPnl(p: PositionItem): number | null {
  const v = p.unrealized_pnl_usd ?? p.pnl_usd
  return v === undefined ? null : v
}

function fundingPnl(p: PositionItem): number | null {
  const v = (p as PositionItem & { funding_pnl_est_usd?: number }).funding_pnl_est_usd
  return v === undefined ? null : v
}

function totalPnl(p: PositionItem): number | null {
  const v = (p as PositionItem & { total_pnl_usd?: number }).total_pnl_usd
  if (v !== undefined) return v
  const u = unrealizedPnl(p)
  const f = fundingPnl(p)
  if (u === null) return null
  return u + (f ?? 0)
}

function fundingSpread(p: PositionItem): number | null {
  const v = (p as PositionItem & { funding_rate_spread_pct?: number }).funding_rate_spread_pct
  return v === undefined ? null : v
}

function feeEstimate(p: PositionItem): number | null {
  const fees = resolvedFees.data.value
  if (!fees || !fees.venues) return null
  const amount = p.trade_usd ?? p.amount_usd ?? 0
  if (amount <= 0) return null

  let totalPct = 0
  const venueIds = [p.long_venue, p.short_venue, p.futures_venue, p.spot_venue].filter(Boolean) as string[]
  for (const venueId of venueIds) {
    const fee = fees.venues[venueId]
    if (!fee) continue
    const isSpot = venueId === p.spot_venue && p.spot_venue !== p.futures_venue
    totalPct += isSpot ? fee.spot_taker_pct : fee.futures_taker_pct
  }
  return amount * (totalPct / 100) * 2
}

function annualizedReturn(p: PositionItem): number | null {
  const pnl = realizedPnl(p) ?? unrealizedPnl(p)
  const amount = p.trade_usd ?? p.amount_usd ?? 0
  const hours = holdHours(p.opened_at ?? p.open_time, p.closed_at)
  if (pnl === null || amount <= 0 || hours <= 0) return null
  return (pnl / amount) * (8760 / hours) * 100
}

function netPnl(p: PositionItem): number | null {
  const gross = realizedPnl(p) ?? unrealizedPnl(p)
  const fees = feeEstimate(p)
  if (gross === null) return null
  return gross - (fees ?? 0)
}

const positionSummary = computed(() => {
  const items = paperItems.value
  const openItems = items.filter((row) => row.status === 'open')
  const closedItems = items.filter((row) => row.status === 'closed')
  const totalTrade = items.reduce((sum, row) => sum + (row.trade_usd ?? row.amount_usd ?? 0), 0)
  const closedPnls = closedItems.map((row) => realizedPnl(row)).filter((v): v is number => v !== null)
  const openPnls = openItems
    .map((row) => totalPnl(row) ?? unrealizedPnl(row))
    .filter((v): v is number => v !== null)
  const totalRealizedPnl = closedPnls.reduce((sum, v) => sum + v, 0)
  const totalUnrealizedPnl = openPnls.reduce((sum, v) => sum + v, 0)
  const wins = closedPnls.filter((v) => v > 0)
  const winRate = closedPnls.length > 0 ? (wins.length / closedPnls.length) * 100 : 0
  const totalFees = items.reduce((sum, row) => sum + (feeEstimate(row) ?? 0), 0)

  return {
    openCount: openItems.length,
    totalPnl: totalRealizedPnl + totalUnrealizedPnl,
    totalFees,
    winRate,
    totalTrade,
  }
})

const accountSummary = computed<PaperBotAccount | null>(() => botAccount.data.value)

const summaryCards = computed(() => [
  {
    label: t('positions.openPositions'),
    value: accountSummary.value?.open_positions ?? positionSummary.value.openCount,
    icon: OpenOutline,
    color: '#18a058',
  },
  {
    label: t('positions.totalPnl'),
    value: fmtUsd(accountSummary.value?.net_pnl_usdt ?? positionSummary.value.totalPnl, true),
    icon: (accountSummary.value?.net_pnl_usdt ?? positionSummary.value.totalPnl) >= 0 ? TrendingUpOutline : TrendingDownOutline,
    color: (accountSummary.value?.net_pnl_usdt ?? positionSummary.value.totalPnl) >= 0 ? '#18a058' : '#d03050',
  },
  {
    label: t('positions.totalFees'),
    value: fmtUsd(accountSummary.value?.fees_paid_usdt ?? positionSummary.value.totalFees),
    icon: CashOutline,
    color: '#f0a020',
  },
  {
    label: t('positions.winRate'),
    value: (accountSummary.value?.win_rate_pct ?? positionSummary.value.winRate).toFixed(1) + '%',
    icon: PieChartOutline,
    color: '#2080f0',
  },
  {
    label: t('positions.totalTrade'),
    value: fmtUsd(accountSummary.value?.total_trade_usdt ?? positionSummary.value.totalTrade),
    icon: WalletOutline,
    color: '#8a2be2',
  },
])

const backtestSummaryCards = computed(() => {
  if (!backtestResult.value) return []
  const s = backtestResult.value.summary
  return [
    {
      label: t('backtest.totalPnl'),
      value: fmtUsd(s.total_pnl_usd, true),
      icon: s.total_pnl_usd >= 0 ? TrendingUpOutline : TrendingDownOutline,
      color: s.total_pnl_usd >= 0 ? '#18a058' : '#d03050',
    },
    {
      label: t('backtest.totalReturn'),
      value: fmtPct(s.total_pnl_pct, 2),
      icon: s.total_pnl_pct >= 0 ? TrendingUpOutline : TrendingDownOutline,
      color: s.total_pnl_pct >= 0 ? '#18a058' : '#d03050',
    },
    {
      label: t('backtest.annualized'),
      value: fmtPct(s.annualized_pct, 2),
      icon: PulseOutline,
      color: '#f0a020',
    },
    {
      label: t('backtest.sharpe'),
      value: s.sharpe.toFixed(2),
      icon: StatsChartOutline,
      color: '#2080f0',
    },
    {
      label: t('backtest.winRate'),
      value: (s.win_rate * 100).toFixed(1) + '%',
      icon: s.win_rate >= 0.5 ? TrendingUpOutline : TrendingDownOutline,
      color: s.win_rate >= 0.5 ? '#18a058' : '#d03050',
    },
    {
      label: t('backtest.maxDrawdown'),
      value: fmtPct(s.max_drawdown_pct, 2),
      icon: TrendingDownOutline,
      color: '#d03050',
    },
  ]
})

const backtestChartOption = computed(() => {
  const curve = backtestResult.value?.equity_curve ?? []
  if (curve.length === 0) return null
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: Array<{ dataIndex: number }>) => {
        const idx = params[0]?.dataIndex ?? 0
        const pt = curve[idx]
        if (!pt) return ''
        return `${pt.ts}<br/>Equity: $${pt.equity.toLocaleString()}<br/>Open pairs: ${pt.open_pairs ?? 0}`
      },
    },
    grid: { left: 48, right: 16, top: 24, bottom: 32 },
    xAxis: {
      type: 'category',
      data: curve.map((p) => p.ts.slice(0, 10)),
      axisLabel: { fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { formatter: (v: number) => `$${(v / 1000).toFixed(0)}k` },
    },
    series: [
      {
        type: 'line',
        data: curve.map((p) => p.equity),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: '#18a058' },
        areaStyle: { color: 'rgba(24, 160, 88, 0.12)' },
      },
    ],
  }
})

const isBotRunning = computed(() =>
  Boolean(runningOnce.value || botStatus.data.value?.running || botStatus.data.value?.locked),
)
const isAutoEnabled = computed(() => botStatus.data.value?.config?.enabled ?? botEnabled.value)
const autoStateLabel = computed(() => {
  if (isBotRunning.value) return t('paperBot.running')
  return isAutoEnabled.value ? t('paperBot.autoOn') : t('paperBot.idle')
})
const autoStateType = computed<'success' | 'warning' | 'default'>(() => {
  if (isBotRunning.value) return 'success'
  return isAutoEnabled.value ? 'warning' : 'default'
})

const lastSummary = computed(() => botStatus.data.value?.last_summary ?? null)
const latestActionCount = computed(() => lastSummary.value?.actions?.length ?? 0)
const pendingHitCount = computed(() => Object.keys(botStatus.data.value?.hit_counts ?? {}).length)

const accountCells = computed(() => {
  const account = accountSummary.value
  return [
    { label: t('paperBot.initialBalance'), value: fmtUsd(account?.initial_balance_usdt ?? 0) },
    { label: t('paperBot.accountEquity'), value: fmtUsd(account?.equity_usdt ?? 0, true), tone: (account?.net_pnl_usdt ?? 0) >= 0 ? 'success' : 'error' },
    { label: t('paperBot.availableBalance'), value: fmtUsd(account?.available_balance_usdt ?? 0) },
    { label: t('paperBot.reservedMargin'), value: fmtUsd(account?.reserved_margin_usdt ?? 0) },
    { label: t('paperBot.unrealizedPnl'), value: fmtUsd(account?.unrealized_pnl_usdt ?? 0, true), tone: (account?.unrealized_pnl_usdt ?? 0) >= 0 ? 'success' : 'error' },
    { label: t('paperBot.fundingPnl'), value: fmtUsd(account?.funding_pnl_usdt ?? 0, true), tone: (account?.funding_pnl_usdt ?? 0) >= 0 ? 'success' : 'error' },
  ] as Array<{ label: string; value: string; tone?: 'success' | 'error' }>
})

function candidateKey(row: OpportunityItem): string {
  return [
    String(row.base ?? '').toUpperCase(),
    String(row.direction ?? 'forward').toLowerCase(),
    String(row.long_venue ?? '').toLowerCase(),
    String(row.short_venue ?? '').toLowerCase(),
  ].join(':')
}

function hitCountFor(row: BotCandidate): number {
  return botStatus.data.value?.hit_counts?.[candidateKey(row)] ?? 0
}

function actionLabel(action: string): string {
  if (action === 'open') return t('paperBot.openAction')
  if (action === 'close') return t('paperBot.closeAction')
  return t('paperBot.noAction')
}

function actionTagType(action: string): 'success' | 'warning' | 'default' {
  if (action === 'open') return 'success'
  if (action === 'close') return 'warning'
  return 'default'
}

const journalRows = computed<JournalRow[]>(() => {
  const rows: JournalRow[] = []
  const runs = [...(botJournal.data.value ?? [])].reverse()
  for (const run of runs) {
    const actions = Array.isArray(run.actions) ? run.actions : []
    if (actions.length === 0) {
      rows.push({
        id: `${run.ts}-none`,
        ts: run.ts,
        action: 'none',
        pair: '-',
        route: '-',
        edge: null,
        reason: `${t('paperBot.scanTotal')} ${run.scan_total} / ${t('paperBot.readyCandidates')} ${run.ready_candidates}`,
        result: '-',
      })
      continue
    }
    actions.forEach((action, idx) => {
      const candidate = action.candidate
      const base = action.base ?? candidate?.base
      const longVenue = candidate?.long_venue
      const shortVenue = candidate?.short_venue
      const result = action.result ?? {}
      rows.push({
        id: `${run.ts}-${idx}`,
        ts: run.ts,
        action: action.action,
        pair: base ? `${base}/USDT` : '-',
        route: longVenue && shortVenue ? `${longVenue} / ${shortVenue}` : '-',
        edge: action.edge ?? candidate?.real_edge_pct ?? candidate?.net_edge_pct ?? null,
        reason: action.reason ?? action.candidate_key ?? '-',
        result: String(result.state ?? result.status ?? result.position_id ?? '-'),
      })
    })
  }
  return rows.slice(0, 50)
})

const journalColumns = computed<DataTableColumns<JournalRow>>(() => [
  {
    title: t('paperBot.time'),
    key: 'ts',
    width: 135,
    render: (row) => formatTime(row.ts),
  },
  {
    title: t('paperBot.action'),
    key: 'action',
    width: 90,
    render: (row) => h(
      NTag,
      { size: 'small', type: actionTagType(row.action), bordered: false },
      { default: () => actionLabel(row.action) },
    ),
  },
  {
    title: t('paperBot.pair'),
    key: 'pair',
    width: 105,
  },
  {
    title: t('paperBot.route'),
    key: 'route',
    width: 160,
  },
  {
    title: t('paperBot.realEdge'),
    key: 'edge',
    width: 100,
    render: (row) => row.edge === null
      ? '-'
      : h(NText, { type: row.edge >= 0 ? 'success' : 'error' }, { default: () => fmtPct(row.edge) }),
  },
  {
    title: t('paperBot.reason'),
    key: 'reason',
    ellipsis: { tooltip: true },
  },
  {
    title: t('paperBot.result'),
    key: 'result',
    width: 110,
    ellipsis: { tooltip: true },
  },
])

function ledgerTypeLabel(type: string): string {
  if (type === 'open') return t('paperBot.openAction')
  if (type === 'close') return t('paperBot.closeAction')
  return type
}

function ledgerTypeTag(type: string): 'success' | 'warning' | 'default' {
  if (type === 'open') return 'success'
  if (type === 'close') return 'warning'
  return 'default'
}

const ledgerRows = computed<PaperBotLedgerEntry[]>(() =>
  [...(botLedger.data.value ?? [])].reverse(),
)

const ledgerColumns = computed<DataTableColumns<PaperBotLedgerEntry>>(() => [
  {
    title: t('paperBot.time'),
    key: 'ts',
    width: 135,
    render: (row) => formatTime(row.ts),
  },
  {
    title: t('paperBot.ledgerType'),
    key: 'type',
    width: 90,
    render: (row) => h(
      NTag,
      { size: 'small', type: ledgerTypeTag(row.type), bordered: false },
      { default: () => ledgerTypeLabel(row.type) },
    ),
  },
  {
    title: t('paperBot.pair'),
    key: 'base',
    width: 100,
    render: (row) => row.base ? `${row.base}/USDT` : '-',
  },
  {
    title: t('paperBot.route'),
    key: 'route',
    width: 165,
    render: (row) => `${row.long_venue || '-'} / ${row.short_venue || '-'}`,
  },
  {
    title: t('paperBot.amount'),
    key: 'trade_usd',
    width: 100,
    render: (row) => fmtUsd(row.trade_usd),
  },
  {
    title: t('paperBot.paidFee'),
    key: 'fee_usd',
    width: 95,
    render: (row) => fmtUsd(row.type === 'close' ? row.total_fee_usd ?? row.fee_usd : row.fee_usd),
  },
  {
    title: t('paperBot.pricePnl'),
    key: 'price_pnl_usd',
    width: 105,
    render: (row) => h(
      NText,
      { type: pnlColor(row.price_pnl_usd ?? 0) },
      { default: () => fmtUsd(row.price_pnl_usd ?? 0, true) },
    ),
  },
  {
    title: t('paperBot.fundingPnl'),
    key: 'funding_pnl_usd',
    width: 105,
    render: (row) => h(
      NText,
      { type: pnlColor(row.funding_pnl_usd ?? 0) },
      { default: () => fmtUsd(row.funding_pnl_usd ?? 0, true) },
    ),
  },
  {
    title: t('paperBot.netPnl'),
    key: 'net_pnl_usd',
    width: 105,
    render: (row) => h(
      NText,
      { type: pnlColor(row.net_pnl_usd ?? 0), strong: true },
      { default: () => fmtUsd(row.net_pnl_usd ?? 0, true) },
    ),
  },
  {
    title: t('paperBot.settlements'),
    key: 'settlements',
    width: 90,
    render: (row) => row.type === 'close'
      ? `${row.long_settlements ?? 0}/${row.short_settlements ?? 0}`
      : '-',
  },
  {
    title: t('paperBot.reason'),
    key: 'reason',
    ellipsis: { tooltip: true },
    render: (row) => row.reason || row.position_id || '-',
  },
])

const backtestTradeColumns = computed<DataTableColumns<BacktestTrade>>(() => [
  { title: t('backtest.pair'), key: 'base', width: 85, render: (row) => `${row.base}/USDT` },
  { title: t('backtest.direction'), key: 'direction', width: 95 },
  { title: t('backtest.longVenue'), key: 'long_venue', width: 120 },
  { title: t('backtest.shortVenue'), key: 'short_venue', width: 120 },
  { title: t('backtest.holdDays'), key: 'hold_days', width: 105, render: (row) => row.hold_days.toFixed(1) + 'd' },
  {
    title: t('paperBot.closeReason'),
    key: 'close_reason',
    width: 190,
    ellipsis: { tooltip: true },
    render: (row) => row.close_reason || '-',
  },
  {
    title: t('backtest.pnlUsdt'),
    key: 'pnl_usd',
    width: 125,
    render: (row) => h(
      NText,
      { type: row.pnl_usd >= 0 ? 'success' : 'error', strong: true },
      { default: () => fmtUsd(row.pnl_usd, true) },
    ),
  },
])

const backtestHistoryColumns = computed<DataTableColumns<BacktestResult>>(() => [
  {
    title: t('paperBot.recordType'),
    key: 'mode',
    width: 110,
    render: (row) => h(
      NTag,
      {
        size: 'small',
        type: isPaperBotBacktest(row) ? 'success' : 'default',
        bordered: false,
      },
      { default: () => isPaperBotBacktest(row) ? t('paperBot.botBacktest') : t('paperBot.legacyBacktest') },
    ),
  },
  { title: t('backtest.id'), key: 'id', width: 150 },
  { title: t('backtest.runTime'), key: 'run_time', width: 170, render: (row) => formatTime(row.run_time) },
  {
    title: t('backtest.pnl'),
    key: 'summary',
    width: 120,
    sorter: (a, b) => a.summary.total_pnl_usd - b.summary.total_pnl_usd,
    render: (row) => h(
      NText,
      { type: row.summary.total_pnl_usd >= 0 ? 'success' : 'error', strong: true },
      { default: () => fmtUsd(row.summary.total_pnl_usd, true) },
    ),
  },
  { title: t('backtest.annualized'), key: 'annualized', width: 110, render: (row) => fmtPct(row.summary.annualized_pct, 2) },
  { title: t('backtest.winRate'), key: 'win_rate', width: 95, render: (row) => (row.summary.win_rate * 100).toFixed(1) + '%' },
  { title: t('backtest.trades'), key: 'trades', width: 85, render: (row) => row.summary.total_trades },
])

function scanActionSummary(actions: BacktestScanRun['actions'] = []): string {
  if (!actions.length) return '-'
  const opens = actions.filter((action) => action.action === 'open').length
  const closes = actions.filter((action) => action.action === 'close').length
  const parts: string[] = []
  if (opens) parts.push(`${t('paperBot.openAction')} ${opens}`)
  if (closes) parts.push(`${t('paperBot.closeAction')} ${closes}`)
  return parts.length ? parts.join(' / ') : String(actions.length)
}

function skippedSummary(row: BacktestScanRun): string {
  const skipped = row.skipped ?? []
  if (!skipped.length) return '-'
  const first = skipped.slice(0, 2).map((item) => item.reason).join(' / ')
  return skipped.length > 2 ? `${first} +${skipped.length - 2}` : first
}

const backtestDailyRows = computed<BacktestDailyLog[]>(() =>
  [...(backtestResult.value?.daily_logs ?? [])].reverse(),
)

const backtestDailyColumns = computed<DataTableColumns<BacktestDailyLog>>(() => [
  { title: t('paperBot.date'), key: 'date', width: 110 },
  { title: t('paperBot.scanRuns'), key: 'scan_runs', width: 95 },
  { title: t('paperBot.pairsScanned'), key: 'pairs_scanned', width: 110 },
  { title: t('paperBot.spreadOk'), key: 'spread_ok', width: 95 },
  { title: t('paperBot.netEdgeOk'), key: 'net_edge_ok', width: 95 },
  { title: t('paperBot.realEdgeOk'), key: 'real_edge_ok', width: 95 },
  { title: t('paperBot.markSpreadOk'), key: 'mark_spread_ok', width: 95 },
  { title: t('paperBot.settleWindowOk'), key: 'settle_window_ok', width: 110 },
  { title: t('paperBot.finalCandidates'), key: 'final_candidates', width: 100 },
  { title: t('paperBot.consecutiveOk'), key: 'consecutive_ok', width: 100 },
  {
    title: t('paperBot.openTrades'),
    key: 'open_actions',
    width: 95,
    render: (row) => row.open_actions > 0
      ? h(NTag, { size: 'small', type: 'success', bordered: false }, { default: () => row.open_actions })
      : '0',
  },
])

const backtestScanRows = computed<BacktestScanRow[]>(() =>
  [...(backtestResult.value?.scan_journal ?? [])]
    .reverse()
    .slice(0, 250)
    .map((row, idx) => ({
      ...row,
      id: `${row.ts}-${idx}`,
      actionSummary: scanActionSummary(row.actions),
      skippedSummary: skippedSummary(row),
    })),
)

const backtestScanColumns = computed<DataTableColumns<BacktestScanRow>>(() => [
  {
    title: t('paperBot.time'),
    key: 'ts',
    width: 135,
    render: (row) => formatTime(row.ts),
  },
  { title: t('paperBot.scanTotal'), key: 'scan_total', width: 90 },
  { title: t('paperBot.filteredCandidates'), key: 'candidates_after_filter', width: 95 },
  { title: t('paperBot.readyCandidates'), key: 'ready_candidates', width: 90 },
  {
    title: t('paperBot.latestActions'),
    key: 'actionSummary',
    width: 130,
    render: (row) => row.actionSummary === '-'
      ? '-'
      : h(NTag, { size: 'small', type: row.actionSummary.includes(t('paperBot.openAction')) ? 'success' : 'warning', bordered: false }, { default: () => row.actionSummary }),
  },
  { title: t('positions.openPositions'), key: 'open_positions', width: 100 },
  {
    title: t('paperBot.accountEquity'),
    key: 'equity',
    width: 120,
    render: (row) => fmtUsd(row.equity),
  },
  {
    title: t('paperBot.reason'),
    key: 'skippedSummary',
    ellipsis: { tooltip: true },
  },
])

function statusTag(row: BotCandidate) {
  const type = row.state === 'qualified' ? 'success' : 'warning'
  const baseLabel = row.state === 'qualified' ? t('paperBot.qualified') : t('paperBot.waiting')
  const hits = hitCountFor(row)
  const label = hits > 0 ? `${baseLabel} ${hits}/${botRules.consecutiveHits}` : baseLabel
  return h(NTag, { type, size: 'small', bordered: false }, { default: () => label })
}

function pnlColor(v: number | null): 'success' | 'error' | undefined {
  if (v === null) return undefined
  return v >= 0 ? 'success' : 'error'
}

const candidateColumns = computed<DataTableColumns<BotCandidate>>(() => [
  {
    title: t('paperBot.status'),
    key: 'state',
    width: 90,
    render: statusTag,
  },
  {
    title: t('paperBot.pair'),
    key: 'base',
    width: 95,
    render: (row) => `${row.base}/USDT`,
  },
  {
    title: t('paperBot.route'),
    key: 'route',
    width: 190,
    render: (row) => `${row.long_venue} / ${row.short_venue}`,
  },
  {
    title: t('paperBot.amount'),
    key: 'amount_usd',
    width: 105,
    render: (row) => fmtUsd(row.amount_usd),
  },
  {
    title: t('paperBot.openFee'),
    key: 'open_fee_usd',
    width: 105,
    render: (row) => fmtUsd(row.open_fee_usd),
  },
  {
    title: t('paperBot.longRate'),
    key: 'long_rate_pct',
    width: 100,
    render: (row) => fmtPct(row.long_rate_pct),
  },
  {
    title: t('paperBot.shortRate'),
    key: 'short_rate_pct',
    width: 100,
    render: (row) => fmtPct(row.short_rate_pct),
  },
  {
    title: t('paperBot.realEdge'),
    key: 'real_edge_pct',
    width: 105,
    sorter: (a, b) => (a.real_edge_pct ?? 0) - (b.real_edge_pct ?? 0),
    defaultSortOrder: 'descend',
    render: (row) => h(
      NText,
      { type: (row.real_edge_pct ?? 0) > 0 ? 'success' : 'error', strong: true },
      { default: () => fmtPct(row.real_edge_pct) },
    ),
  },
  {
    title: t('paperBot.markSpread'),
    key: 'mark_spread_pct',
    width: 105,
    render: (row) => fmtPct(row.mark_spread_pct),
  },
  {
    title: t('paperBot.depth'),
    key: 'max_exec_usd',
    width: 110,
    render: (row) => row.max_exec_usd ? fmtUsd(row.max_exec_usd) : '-',
  },
  {
    title: t('paperBot.nextSettle'),
    key: 'settle',
    width: 115,
    render: (row) => fmtMinutes(Math.min(row.long_settle_ms ?? 0, row.short_settle_ms ?? 0) || undefined),
  },
])

const positionColumns = computed<DataTableColumns<PositionItem>>(() => [
  {
    type: 'expand',
    expandColumnWidth: 40,
    renderExpand: (row) => renderDetail(row),
  },
  { title: t('positions.id'), key: 'id', width: 110, ellipsis: { tooltip: true } },
  {
    title: t('positions.pair'),
    key: 'base',
    width: 90,
    render: (row) => `${row.base}/${row.quote ?? 'USDT'}`,
  },
  {
    title: t('positions.strategy'),
    key: 'strategy',
    width: 90,
    render: (row) => {
      const s = row.strategy ?? 'pure_futures'
      const label = s === 'pure_futures' || s === 'pure_futures_spread' ? 'Pure' : s === 'carry' ? 'Carry' : s
      return h(NTag, { size: 'tiny', bordered: false, type: 'info' }, { default: () => label })
    },
  },
  {
    title: t('positions.direction'),
    key: 'direction',
    width: 80,
    render: (row) => h(NTag, {
      size: 'small',
      type: row.direction === 'forward' ? 'success' : 'warning',
      bordered: false,
    }, { default: () => row.direction }),
  },
  {
    title: t('positions.long') + '/' + t('positions.short'),
    key: 'venues',
    width: 130,
    render: (row) => {
      if (row.futures_venue) return `${row.futures_venue} / ${row.spot_venue ?? '-'}`
      return `${row.long_venue} / ${row.short_venue}`
    },
  },
  {
    title: t('positions.amount'),
    key: 'trade_usd',
    width: 100,
    render: (row) => fmtUsd(row.trade_usd ?? row.amount_usd ?? 0),
  },
  {
    title: t('positions.quantity'),
    key: 'qty',
    width: 80,
    render: (row) => row.qty ? row.qty.toLocaleString(undefined, { maximumFractionDigits: 6 }) : '-',
  },
  {
    title: t('positions.openPrice'),
    key: 'open_price',
    width: 120,
    render: (row) => {
      const open = getOpenPrices(row)
      if (open.futures !== undefined) return `${fmtPrice(open.futures)} / ${fmtPrice(open.spot)}`
      return `${fmtPrice(open.long)} / ${fmtPrice(open.short)}`
    },
  },
  {
    title: t('positions.entrySpread'),
    key: 'mark_spread_pct',
    width: 100,
    render: (row) => {
      const val = row.mark_spread_pct ?? row.open_spread_pct ?? 0
      return fmtPct(row.mark_spread_pct !== undefined ? val : val * 100, 3)
    },
  },
  {
    title: t('positions.realizedPnl') + '/' + t('positions.unrealizedPnl'),
    key: 'pnl',
    width: 120,
    render: (row) => {
      const rpnl = realizedPnl(row)
      const upnl = unrealizedPnl(row)
      const v = rpnl ?? upnl
      if (v === null) return '-'
      return h(NText, { type: pnlColor(v), strong: true }, { default: () => fmtUsd(v, true) })
    },
  },
  {
    title: t('positions.feeEstimate'),
    key: 'fees',
    width: 90,
    render: (row) => fmtUsd(feeEstimate(row)),
  },
  {
    title: t('positions.annualized'),
    key: 'annualized',
    width: 80,
    render: (row) => {
      const v = annualizedReturn(row)
      if (v === null) return '-'
      return h(NText, { type: pnlColor(v) }, { default: () => fmtPct(v, 1) })
    },
  },
  {
    title: t('positions.holdTime'),
    key: 'duration',
    width: 100,
    render: (row) => formatDuration(row.opened_at ?? row.open_time, row.closed_at),
  },
  {
    title: t('positions.openedAt'),
    key: 'opened_at',
    width: 120,
    render: (row) => formatTime(row.opened_at ?? row.open_time),
  },
  {
    title: t('positions.statusCol'),
    key: 'status',
    width: 90,
    render: (row) => {
      const colorMap: Record<string, 'success' | 'warning' | 'error' | 'default'> = {
        open: 'success',
        closed: 'default',
      }
      return h(NTag, { size: 'small', type: colorMap[row.status] ?? 'default', bordered: false }, { default: () => row.status })
    },
  },
  {
    title: '',
    key: 'actions',
    width: 80,
    fixed: 'right',
    render: (row) => h(NButton, {
      size: 'tiny',
      type: 'error',
      secondary: true,
      disabled: row.status === 'closed' || row.dry_run !== true,
      onClick: () => showCloseConfirm(row),
    }, { default: () => t('positions.close') }),
  },
])

function fmtMinutes(ms: number | undefined): string {
  if (!ms) return '-'
  const minutes = Math.floor((ms - Date.now()) / 60000)
  if (minutes < 0) return t('paperBot.settlingNow')
  if (minutes < 60) return `${minutes}${t('paperBot.minutes')}`
  const hours = Math.floor(minutes / 60)
  const mins = minutes % 60
  return `${hours}${t('paperBot.hours')} ${mins}${t('paperBot.minutes')}`
}

function renderDetail(row: PositionItem) {
  const open = getOpenPrices(row)
  const close = getClosePrices(row)
  const rpnl = realizedPnl(row)
  const upnl = unrealizedPnl(row)
  const fees = feeEstimate(row)
  const npnl = netPnl(row)
  const ann = annualizedReturn(row)
  const spread = fundingSpread(row)
  const fpnl = fundingPnl(row)
  const isPureFutures = !row.futures_venue
  const items: Array<{ label: string; value: string; type?: 'success' | 'error' }> = []

  if (isPureFutures) {
    items.push({ label: t('positions.openPrice') + ' (Long/Short)', value: `${fmtPrice(open.long)} / ${fmtPrice(open.short)}` })
    if (close.long !== undefined) {
      items.push({ label: t('positions.closePrice') + ' (Long/Short)', value: `${fmtPrice(close.long)} / ${fmtPrice(close.short)}` })
    }
    if (row.long_qty || row.short_qty) {
      items.push({ label: t('positions.longQty') + '/' + t('positions.shortQty'), value: `${row.long_qty ?? '-'} / ${row.short_qty ?? '-'}` })
    }
  } else {
    items.push({ label: t('positions.futuresPrice'), value: fmtPrice(open.futures) })
    items.push({ label: t('positions.spotPrice'), value: fmtPrice(open.spot) })
    if (close.futures !== undefined) {
      items.push({ label: t('positions.closePrice') + ' (' + t('positions.futuresVenue') + ')', value: fmtPrice(close.futures) })
    }
    if (close.spot !== undefined) {
      items.push({ label: t('positions.closePrice') + ' (' + t('positions.spotVenue') + ')', value: fmtPrice(close.spot) })
    }
  }

  const entrySpread = row.mark_spread_pct ?? row.open_spread_pct
  if (entrySpread !== undefined) {
    items.push({
      label: t('positions.entrySpread'),
      value: fmtPct(row.mark_spread_pct !== undefined ? entrySpread : entrySpread * 100, 4),
    })
  }
  if (row.close_info?.close_mark_spread !== undefined) {
    items.push({ label: t('positions.exitSpread'), value: fmtPct(row.close_info.close_mark_spread as number, 4) })
  }
  items.push({ label: t('positions.holdTime'), value: formatDuration(row.opened_at ?? row.open_time, row.closed_at) })
  items.push({
    label: t('positions.grossPnl'),
    value: fmtUsd(rpnl ?? upnl, true),
    type: (rpnl ?? upnl ?? 0) >= 0 ? 'success' : 'error',
  })
  if (fpnl !== null) {
    items.push({ label: t('positions.realizedPnl') + ' (' + t('positions.funding') + ')', value: fmtUsd(fpnl, true), type: fpnl >= 0 ? 'success' : 'error' })
  }
  if (spread !== null) {
    items.push({ label: t('positions.funding') + ' Spread (ann)', value: fmtPct(spread, 1), type: spread >= 0 ? 'success' : 'error' })
  }
  items.push({ label: t('positions.feeEstimate'), value: fmtUsd(fees) })
  items.push({ label: t('positions.netPnl'), value: fmtUsd(npnl, true), type: (npnl ?? 0) >= 0 ? 'success' : 'error' })
  if (ann !== null) {
    items.push({ label: t('positions.annualized'), value: fmtPct(ann, 1), type: ann >= 0 ? 'success' : 'error' })
  }
  items.push({ label: t('positions.strategy'), value: row.dry_run === false ? t('positions.live') : t('positions.dryRun') })
  if (row.parallel_legs) items.push({ label: 'Mode', value: 'Parallel legs' })
  if (row.closed_at) items.push({ label: t('positions.closedAt'), value: formatTime(row.closed_at) })

  return h('div', { style: 'padding: 8px 0;' }, [
    h(NDivider, { style: 'margin: 4px 0 12px' }),
    h(NDescriptions, {
      'label-placement': 'left',
      bordered: true,
      size: 'small',
      column: 3,
    }, {
      default: () => items.map((item) => h(NDescriptionsItem, { label: item.label }, {
        default: () => h(NText, { type: item.type, strong: !!item.type }, { default: () => item.value }),
      })),
    }),
  ])
}

const showCloseModal = ref(false)
const closing = ref(false)
const closeTarget = ref<PositionItem | null>(null)

const emptyPositionsMessage = computed(() => {
  if (statusFilter.value === 'open') return t('positions.noOpenPositions')
  return t('positions.noPositions')
})

function showCloseConfirm(row: PositionItem) {
  closeTarget.value = row
  showCloseModal.value = true
}

function loadBacktestResult(row: BacktestResult) {
  backtestError.value = ''
  backtestResult.value = row
}

function clearBacktestResult() {
  backtestError.value = ''
  backtestResult.value = null
}

const backtestHistoryRowProps = (row: BacktestResult) => ({
  style: 'cursor: pointer',
  onClick: () => loadBacktestResult(row),
})

async function confirmClose() {
  const row = closeTarget.value
  if (!row) return
  closing.value = true
  try {
    await post(`/positions/${row.id}/close`, { reason: 'paper-bot-manual' })
    message.success(t('positions.closed', { base: row.base }))
    showCloseModal.value = false
    await Promise.all([
      positions.refresh(),
      botAccount.refresh(),
      botStatus.refresh(),
      botJournal.refresh(),
      botLedger.refresh(),
    ])
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('positions.failedToClose'))
  } finally {
    closing.value = false
  }
}

async function refreshAll() {
  await Promise.all([
    venues.refresh(),
    strategy.refresh(),
    resolvedFees.refresh(),
    scanner.refresh(),
    positions.refresh(),
    botConfig.refresh(),
    botAccount.refresh(),
    botStatus.refresh(),
    botJournal.refresh(),
    botLedger.refresh(),
    backtestHistory.refresh(),
  ])
  hydrateStrategyForm()
  hydrateBotRules()
  if (!backtestSynced.value) syncBacktestFromBot(false)
}

async function triggerScan() {
  await post('/scanner/trigger?strategy=pure', {})
  await Promise.all([scanner.refresh(), positions.refresh(), botAccount.refresh(), botStatus.refresh()])
}

async function refreshBotState() {
  if (polling) return
  polling = true
  try {
    await Promise.all([
      scanner.refresh(),
      positions.refresh(),
      botAccount.refresh(),
      botStatus.refresh(),
      botJournal.refresh(),
      botLedger.refresh(),
    ])
    hydrateBotRules()
  } finally {
    polling = false
  }
}

async function saveAll(showSuccess = true): Promise<boolean> {
  saving.value = true
  try {
    await post<StrategyParams>('/settings/strategy', strategyForm)
    await post('/paper-bot/config', botConfigPayload())
    await Promise.all([
      strategy.refresh(),
      resolvedFees.refresh(),
      botConfig.refresh(),
      botAccount.refresh(),
      botStatus.refresh(),
    ])
    hydrateStrategyForm()
    hydrateBotRules()
    try {
      await post('/scanner/recalc-fees', {})
    } catch {
      // no cached scanner result yet
    }
    if (showSuccess) message.success(t('settings.settingsSaved'))
    return true
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('settings.failedToSave'))
    return false
  } finally {
    saving.value = false
  }
}

async function handleSave() {
  await saveAll(true)
}

async function runBotBacktest() {
  backtestRunning.value = true
  backtestError.value = ''
  backtestResult.value = null
  try {
    const result = await post<BacktestResult>('/paper-bot/backtest', {
      jsonl_file: null,
      history_bases: backtestHistoryBasesPayload(),
      history_base_limit: Math.max(1, Math.floor(backtestForm.historyBaseLimit || 80)),
      history_venues: backtestForm.historyVenues.length > 0 ? backtestForm.historyVenues.join(',') : null,
      history_days: backtestForm.historyDays,
      capital: backtestForm.capital,
      trade_usd: backtestForm.tradeUsd,
      min_spread: backtestForm.minSpread,
      exit_edge: backtestForm.exitEdge,
      max_positions: backtestForm.maxPositions,
      min_edge_pct: backtestForm.minEdge,
      min_edge_1h: backtestForm.minEdge1h || null,
      min_edge_mismatch: backtestForm.allowMismatch ? backtestForm.minEdgeMismatch || null : null,
      max_mark_spread_pct: backtestForm.maxMarkSpread,
      max_holding_hours: backtestForm.maxHoldingHours,
      allow_mismatch: backtestForm.allowMismatch,
      consecutive_hits: backtestForm.consecutiveHits,
      min_settle_minutes: backtestForm.minSettleMinutes,
      max_settle_minutes: backtestForm.maxSettleMinutes,
      max_actions_per_run: backtestForm.maxActionsPerRun,
      basis_cost_pct: backtestForm.basisCostPct,
      active_exit_enabled: backtestForm.activeExitEnabled,
      active_exit_confirm_minutes: backtestForm.activeExitConfirmMinutes,
      active_exit_window_minutes: backtestForm.activeExitWindowMinutes,
      active_exit_max_mark_spread_pct: backtestForm.activeExitMaxMarkSpreadPct,
    })
    backtestResult.value = result
    message.success(t('backtest.backtestComplete'))
    await backtestHistory.refresh()
  } catch (e) {
    backtestError.value = e instanceof Error ? e.message : t('backtest.backtestFailed')
    message.error(backtestError.value)
  } finally {
    backtestRunning.value = false
  }
}

async function handleRunOnce() {
  runningOnce.value = true
  try {
    const saved = await saveAll(false)
    if (!saved) return
    await post<PaperBotSummary>('/paper-bot/run-once', {})
    message.success(t('paperBot.runComplete'))
    await Promise.all([
      scanner.refresh(),
      positions.refresh(),
      resolvedFees.refresh(),
      botAccount.refresh(),
      botStatus.refresh(),
      botJournal.refresh(),
      botLedger.refresh(),
    ])
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('paperBot.runFailed'))
    await botStatus.refresh()
  } finally {
    runningOnce.value = false
  }
}

async function handleStartAuto() {
  const previous = botEnabled.value
  botEnabled.value = true
  autoBusy.value = true
  try {
    const saved = await saveAll(false)
    if (!saved) {
      botEnabled.value = previous
      return
    }
    await startPaperBot()
    message.success(t('paperBot.autoStarted'))
    await refreshBotState()
  } catch (e) {
    botEnabled.value = previous
    message.error(e instanceof Error ? e.message : t('paperBot.autoStartFailed'))
    await botStatus.refresh()
  } finally {
    autoBusy.value = false
  }
}

async function handleStopAuto() {
  autoBusy.value = true
  try {
    await stopPaperBot()
    botEnabled.value = false
    message.success(t('paperBot.autoStopped'))
    await refreshBotState()
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('paperBot.autoStopFailed'))
    await botStatus.refresh()
  } finally {
    autoBusy.value = false
  }
}

function handleAutoToggle() {
  if (isAutoEnabled.value) {
    void handleStopAuto()
  } else {
    void handleStartAuto()
  }
}

function startPolling() {
  if (pollTimer !== null) return
  pollTimer = window.setInterval(() => {
    void refreshBotState()
  }, 10000)
}

onMounted(async () => {
  await refreshAll()
  startPolling()
})

onBeforeUnmount(() => {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <div class="paper-bot-page">
    <n-card size="small" class="mode-card">
      <div class="mode-bar">
        <div class="section-title">
          <n-icon size="20"><PieChartOutline /></n-icon>
          <span>{{ t('paperBot.title') }}</span>
        </div>
        <n-radio-group v-model:value="pageMode" size="small">
          <n-radio-button value="trade">{{ t('paperBot.tradeMode') }}</n-radio-button>
          <n-radio-button value="backtest">{{ t('paperBot.backtestMode') }}</n-radio-button>
        </n-radio-group>
      </div>
    </n-card>

    <template v-if="pageMode === 'trade'">
    <n-grid :cols="5" :x-gap="16" :y-gap="16" responsive="screen" class="summary-grid">
      <n-gi v-for="card in summaryCards" :key="card.label">
        <n-card size="small" class="summary-shell">
          <div class="summary-card">
            <div class="summary-icon" :style="{ backgroundColor: card.color + '22', color: card.color }">
              <n-icon size="22"><component :is="card.icon" /></n-icon>
            </div>
            <div class="summary-copy">
              <n-text depth="3" class="summary-label">{{ card.label }}</n-text>
              <n-text strong class="summary-value">{{ card.value }}</n-text>
            </div>
          </div>
        </n-card>
      </n-gi>
    </n-grid>

    <n-card size="small" class="strategy-card">
      <template #header>
        <div class="section-title">
          <n-icon size="20"><PlayCircleOutline /></n-icon>
          <span>{{ t('paperBot.title') }}</span>
          <n-tag size="small" type="warning" :bordered="false">{{ t('paperBot.dryRunOnly') }}</n-tag>
        </div>
      </template>
      <template #header-extra>
        <n-space align="center">
          <n-tag size="small" :type="autoStateType" :bordered="false">
            {{ autoStateLabel }}
          </n-tag>
          <n-switch
            :value="isAutoEnabled"
            :loading="autoBusy"
            :disabled="saving"
            @update:value="handleAutoToggle"
          >
            <template #checked><n-icon><PlayCircleOutline /></n-icon></template>
            <template #unchecked><n-icon><StopCircleOutline /></n-icon></template>
          </n-switch>
          <n-button
            size="small"
            :type="isAutoEnabled ? 'warning' : 'success'"
            secondary
            :loading="autoBusy"
            :disabled="saving"
            @click="handleAutoToggle"
          >
            <template #icon>
              <n-icon><component :is="isAutoEnabled ? StopCircleOutline : PlayCircleOutline" /></n-icon>
            </template>
            {{ isAutoEnabled ? t('paperBot.pauseAuto') : t('paperBot.startAuto') }}
          </n-button>
          <n-button
            size="small"
            type="primary"
            ghost
            :loading="runningOnce"
            :disabled="saving || isBotRunning"
            @click="handleRunOnce"
          >
            <template #icon><n-icon><PlayCircleOutline /></n-icon></template>
            {{ t('paperBot.runOnce') }}
          </n-button>
          <n-button size="small" type="primary" :loading="saving" @click="handleSave">
            <template #icon><n-icon><SaveOutline /></n-icon></template>
            {{ t('settings.save') }}
          </n-button>
        </n-space>
      </template>

      <n-form label-placement="top" size="small" class="strategy-form">
        <n-grid :cols="6" :x-gap="12" :y-gap="8" responsive="screen">
          <n-gi>
            <n-form-item :label="t('settings.minSpreadAnnual')">
              <n-input-number v-model:value="strategyForm.min_spread_annual" :min="0" :max="100" style="width:100%">
                <template #suffix>%</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.minEdgeAnnual')">
              <n-input-number v-model:value="strategyForm.min_edge_annual" :min="0" :max="100" style="width:100%">
                <template #suffix>%</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.minEdge1h')">
              <n-input-number v-model:value="strategyForm.min_edge_1h" :min="0" :max="100" :step="0.005" style="width:100%">
                <template #suffix>%</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.minEdgeMismatch')">
              <n-input-number v-model:value="strategyForm.min_edge_mismatch" :min="0" :max="100" :step="0.005" style="width:100%">
                <template #suffix>%</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.maxMarkSpread')">
              <n-input-number v-model:value="strategyForm.max_mark_spread_pct" :min="0" :max="100" style="width:100%">
                <template #suffix>%</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.tradeSize')">
              <n-input-number v-model:value="strategyForm.trade_usd" :min="100" :step="1000" style="width:100%">
                <template #suffix>USDT</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.maxPositions')">
              <n-input-number v-model:value="strategyForm.max_positions" :min="1" :max="20" style="width:100%" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.initialBalance')">
              <n-input-number v-model:value="botRules.initialBalanceUsdt" :min="0" :step="1000" style="width:100%">
                <template #suffix>USDT</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('settings.scanInterval')">
              <n-input-number v-model:value="strategyForm.scan_interval_sec" :min="10" :step="30" style="width:100%">
                <template #suffix>s</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.depthMultiple')">
              <n-input-number v-model:value="botRules.depthMultiple" :min="1" :step="1" style="width:100%">
                <template #suffix>x</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.consecutiveHits')">
              <n-input-number v-model:value="botRules.consecutiveHits" :min="1" :step="1" style="width:100%" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.maxHold')">
              <n-input-number v-model:value="botRules.maxHoldHours" :min="1" :step="1" style="width:100%">
                <template #suffix>h</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.maxActions')">
              <n-input-number v-model:value="botRules.maxActionsPerRun" :min="1" :max="20" :step="1" style="width:100%" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.activeExit')">
              <n-switch v-model:value="botRules.activeExitEnabled" />
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.settleWindow')">
              <div class="range-row">
                <n-input-number v-model:value="botRules.minSettleMinutes" :min="0" :show-button="false" />
                <span>-</span>
                <n-input-number v-model:value="botRules.maxSettleMinutes" :min="1" :show-button="false" />
              </div>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.activeExitConfirm')">
              <n-input-number v-model:value="botRules.activeExitConfirmMinutes" :min="0" :step="1" style="width:100%">
                <template #suffix>m</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.activeExitWindow')">
              <n-input-number v-model:value="botRules.activeExitWindowMinutes" :min="1" :step="5" style="width:100%">
                <template #suffix>m</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi>
            <n-form-item :label="t('paperBot.activeExitMarkSpread')">
              <n-input-number v-model:value="botRules.activeExitMaxMarkSpreadPct" :min="0" :max="100" :step="0.01" style="width:100%">
                <template #suffix>%</template>
              </n-input-number>
            </n-form-item>
          </n-gi>
          <n-gi :span="2">
            <n-form-item :label="t('settings.scanVenues')">
              <n-select
                v-model:value="strategyForm.scan_venues"
                :options="scanVenueOptions"
                multiple
                filterable
                max-tag-count="responsive"
                style="width:100%"
              />
            </n-form-item>
          </n-gi>
        </n-grid>
      </n-form>

      <div class="account-strip">
        <div v-for="cell in accountCells" :key="cell.label" class="status-cell">
          <n-text depth="3">{{ cell.label }}</n-text>
          <n-text :type="cell.tone" strong>{{ cell.value }}</n-text>
        </div>
      </div>

      <div class="bot-status-strip">
        <div class="status-cell">
          <n-text depth="3">{{ t('paperBot.lastRun') }}</n-text>
          <n-text strong>{{ formatTime(botStatus.data.value?.last_run_at || undefined) }}</n-text>
        </div>
        <div class="status-cell">
          <n-text depth="3">{{ t('paperBot.lastFinish') }}</n-text>
          <n-text strong>{{ formatTime(botStatus.data.value?.last_finished_at || undefined) }}</n-text>
        </div>
        <div class="status-cell">
          <n-text depth="3">{{ t('paperBot.nextRun') }}</n-text>
          <n-text strong>{{ formatTime(botStatus.data.value?.next_run_at || undefined) }}</n-text>
        </div>
        <div class="status-cell">
          <n-text depth="3">{{ t('paperBot.latestActions') }}</n-text>
          <n-text strong>{{ latestActionCount }}</n-text>
        </div>
        <div class="status-cell">
          <n-text depth="3">{{ t('paperBot.pendingHits') }}</n-text>
          <n-text strong>{{ pendingHitCount }}</n-text>
        </div>
        <div class="status-cell">
          <n-text depth="3">{{ t('paperBot.totalRuns') }}</n-text>
          <n-text strong>{{ botStatus.data.value?.total_runs ?? 0 }}</n-text>
        </div>
        <div v-if="botStatus.data.value?.last_error" class="status-cell status-cell-wide">
          <n-text depth="3">{{ t('paperBot.lastError') }}</n-text>
          <n-text type="error" strong class="single-line">{{ botStatus.data.value.last_error }}</n-text>
        </div>
      </div>
    </n-card>

    <n-card size="small" class="table-card">
      <template #header>
        <div class="section-title">
          <n-icon size="20"><ShieldCheckmarkOutline /></n-icon>
          <span>{{ t('paperBot.candidates') }}</span>
        </div>
      </template>
      <template #header-extra>
        <n-space align="center">
          <n-tag size="small" type="success" :bordered="false">
            {{ t('paperBot.qualified') }} {{ qualifiedCandidates.length }}
          </n-tag>
          <n-tag size="small" type="warning" :bordered="false">
            {{ t('paperBot.waiting') }} {{ waitingCandidates.length }}
          </n-tag>
          <n-button size="small" secondary @click="refreshAll">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            {{ t('paperBot.refresh') }}
          </n-button>
          <n-button size="small" type="primary" ghost @click="triggerScan">
            {{ t('paperBot.scanNow') }}
          </n-button>
        </n-space>
      </template>
      <n-spin :show="loading">
        <n-data-table
          v-if="allCandidates.length > 0"
          :columns="candidateColumns"
          :data="allCandidates"
          :bordered="false"
          :row-key="(row: BotCandidate) => row.id"
          :scroll-x="1250"
          :max-height="330"
          size="small"
          striped
        />
        <n-empty v-else :description="t('paperBot.noCandidates')" style="padding: 34px 0" />
      </n-spin>
    </n-card>

    <n-card :title="t('paperBot.botPositions')" size="small" class="positions-card">
      <template #header-extra>
        <n-space align="center">
          <n-tag size="small" type="default" :bordered="false">
            {{ t('paperBot.manualIgnored', { count: manualPaperItems.length }) }}
          </n-tag>
          <n-text depth="3" style="font-size: 12px">{{ t('positions.status') }}</n-text>
          <n-select
            v-model:value="statusFilter"
            :options="[
              { label: t('positions.openFilter'), value: 'open' },
              { label: t('positions.closedFilter'), value: 'closed' },
              { label: t('positions.allFilter'), value: 'all' },
            ]"
            style="width: 100px"
            size="small"
          />
          <n-button size="small" secondary @click="positions.refresh">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            {{ t('positions.refresh') }}
          </n-button>
        </n-space>
      </template>
      <n-spin :show="positions.loading.value || resolvedFees.loading.value">
        <n-data-table
          v-if="filteredPositionItems.length > 0"
          :columns="positionColumns"
          :data="filteredPositionItems"
          :bordered="false"
          :scroll-x="1400"
          :max-height="360"
          size="small"
          striped
        />
        <n-empty v-else :description="emptyPositionsMessage" style="padding: 36px 0" />
      </n-spin>
    </n-card>

    <n-card :title="t('paperBot.pnlLedger')" size="small" class="ledger-card">
      <template #header-extra>
        <n-button size="small" secondary @click="botLedger.refresh">
          <template #icon><n-icon><RefreshOutline /></n-icon></template>
          {{ t('paperBot.refresh') }}
        </n-button>
      </template>
      <n-spin :show="botLedger.loading.value">
        <n-data-table
          v-if="ledgerRows.length > 0"
          :columns="ledgerColumns"
          :data="ledgerRows"
          :bordered="false"
          :row-key="(row: PaperBotLedgerEntry) => row.id"
          :scroll-x="1200"
          :max-height="280"
          size="small"
          striped
        />
        <n-empty v-else :description="t('paperBot.noLedger')" style="padding: 30px 0" />
      </n-spin>
    </n-card>

    <n-card :title="t('paperBot.recentActions')" size="small" class="journal-card">
      <template #header-extra>
        <n-button size="small" secondary @click="botJournal.refresh">
          <template #icon><n-icon><RefreshOutline /></n-icon></template>
          {{ t('paperBot.refresh') }}
        </n-button>
      </template>
      <n-spin :show="botJournal.loading.value">
        <n-data-table
          v-if="journalRows.length > 0"
          :columns="journalColumns"
          :data="journalRows"
          :bordered="false"
          :row-key="(row: JournalRow) => row.id"
          :scroll-x="900"
          :max-height="260"
          size="small"
          striped
        />
        <n-empty v-else :description="t('paperBot.noJournal')" style="padding: 30px 0" />
      </n-spin>
    </n-card>
    </template>

    <template v-else>
      <n-card size="small" class="backtest-card">
        <template #header>
          <div class="section-title">
            <n-icon size="20"><StatsChartOutline /></n-icon>
            <span>{{ t('paperBot.backtestTitle') }}</span>
            <n-tag size="small" type="warning" :bordered="false">{{ t('paperBot.dryRunOnly') }}</n-tag>
          </div>
        </template>
        <template #header-extra>
          <n-space align="center">
            <n-button size="small" secondary @click="syncBacktestFromBot(true)">
              {{ t('paperBot.syncBacktestParams') }}
            </n-button>
            <n-button type="primary" size="small" :loading="backtestRunning" @click="runBotBacktest">
              <template #icon><n-icon><PlayCircleOutline /></n-icon></template>
              {{ t('backtest.runBacktest') }}
            </n-button>
          </n-space>
        </template>

        <n-form label-placement="top" size="small" class="strategy-form">
          <n-grid :cols="6" :x-gap="12" :y-gap="8" responsive="screen">
            <n-gi :span="2">
              <n-form-item :label="t('backtest.historicalBases')">
                <n-input
                  v-model:value="backtestForm.historyBases"
                  :placeholder="t('backtest.historicalBasesPlaceholder')"
                  style="width:100%"
                />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.historyBaseLimit')">
                <n-input-number v-model:value="backtestForm.historyBaseLimit" :min="1" :max="200" :step="10" style="width:100%" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('backtest.historyDays')">
                <n-input-number v-model:value="backtestForm.historyDays" :min="1" :max="365" style="width:100%" />
              </n-form-item>
            </n-gi>
            <n-gi :span="2">
              <n-form-item :label="t('backtest.venues')">
                <n-select
                  v-model:value="backtestForm.historyVenues"
                  :options="scanVenueOptions"
                  multiple
                  filterable
                  max-tag-count="responsive"
                  style="width:100%"
                />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('backtest.initialCapital')">
                <n-input-number v-model:value="backtestForm.capital" :min="1000" :step="1000" style="width:100%">
                  <template #suffix>USDT</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('backtest.tradeSize')">
                <n-input-number v-model:value="backtestForm.tradeUsd" :min="100" :step="1000" style="width:100%">
                  <template #suffix>USDT</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('backtest.maxPositions')">
                <n-input-number v-model:value="backtestForm.maxPositions" :min="1" :max="20" style="width:100%" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('backtest.minSpread')">
                <n-input-number v-model:value="backtestForm.minSpread" :min="0" :max="100" :step="0.01" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('settings.minEdgeAnnual')">
                <n-input-number v-model:value="backtestForm.minEdge" :min="0" :max="100" :step="0.005" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('settings.minEdge1h')">
                <n-input-number v-model:value="backtestForm.minEdge1h" :min="0" :max="100" :step="0.005" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('settings.minEdgeMismatch')">
                <n-input-number v-model:value="backtestForm.minEdgeMismatch" :min="0" :max="100" :step="0.005" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('backtest.exitEdge')">
                <n-input-number v-model:value="backtestForm.exitEdge" :min="0" :max="100" :step="0.005" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('settings.maxMarkSpread')">
                <n-input-number v-model:value="backtestForm.maxMarkSpread" :min="0" :max="100" :step="0.01" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.maxHold')">
                <n-input-number v-model:value="backtestForm.maxHoldingHours" :min="1" :max="2160" :step="1" style="width:100%">
                  <template #suffix>h</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.consecutiveHits')">
                <n-input-number v-model:value="backtestForm.consecutiveHits" :min="1" :max="20" :step="1" style="width:100%" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.maxActions')">
                <n-input-number v-model:value="backtestForm.maxActionsPerRun" :min="1" :max="50" :step="1" style="width:100%" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.activeExit')">
                <n-switch v-model:value="backtestForm.activeExitEnabled" />
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.settleWindow')">
                <div class="range-row">
                  <n-input-number v-model:value="backtestForm.minSettleMinutes" :min="0" :show-button="false" />
                  <span>-</span>
                  <n-input-number v-model:value="backtestForm.maxSettleMinutes" :min="1" :show-button="false" />
                </div>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.activeExitConfirm')">
                <n-input-number v-model:value="backtestForm.activeExitConfirmMinutes" :min="0" :step="1" style="width:100%">
                  <template #suffix>m</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.activeExitWindow')">
                <n-input-number v-model:value="backtestForm.activeExitWindowMinutes" :min="1" :step="5" style="width:100%">
                  <template #suffix>m</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.activeExitMarkSpread')">
                <n-input-number v-model:value="backtestForm.activeExitMaxMarkSpreadPct" :min="0" :max="100" :step="0.01" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.basisCost')">
                <n-input-number v-model:value="backtestForm.basisCostPct" :min="0" :max="5" :step="0.01" style="width:100%">
                  <template #suffix>%</template>
                </n-input-number>
              </n-form-item>
            </n-gi>
            <n-gi>
              <n-form-item :label="t('paperBot.crossInterval')">
                <n-switch v-model:value="backtestForm.allowMismatch" />
              </n-form-item>
            </n-gi>
          </n-grid>
        </n-form>
      </n-card>

      <n-alert
        v-if="backtestError"
        type="error"
        :title="t('backtest.backtestFailed')"
        class="backtest-result-card"
      >
        <div>{{ backtestError }}</div>
        <n-text depth="3">{{ t('paperBot.backtestFailureHint') }}</n-text>
      </n-alert>

      <n-alert
        v-if="selectedBacktestIsLegacy"
        type="warning"
        :title="t('paperBot.legacyBacktestTitle')"
        class="backtest-result-card"
      >
        {{ t('paperBot.legacyBacktestNotice') }}
      </n-alert>

      <n-grid v-if="hasBacktestResult" :cols="3" :x-gap="16" :y-gap="16" responsive="screen" class="summary-grid">
        <n-gi v-for="card in backtestSummaryCards" :key="card.label">
          <n-card size="small" class="summary-shell">
            <div class="summary-card">
              <div class="summary-icon" :style="{ backgroundColor: card.color + '22', color: card.color }">
                <n-icon size="22"><component :is="card.icon" /></n-icon>
              </div>
              <div class="summary-copy">
                <n-text depth="3" class="summary-label">{{ card.label }}</n-text>
                <n-text strong class="summary-value">{{ card.value }}</n-text>
              </div>
            </div>
          </n-card>
        </n-gi>
      </n-grid>

      <n-card v-if="hasBacktestResult && backtestChartOption" :title="t('backtest.equityCurve')" size="small" class="backtest-result-card">
        <template #header-extra>
          <n-space>
            <n-button size="small" secondary @click="clearBacktestResult">
              {{ t('backtest.backToHistory') }}
            </n-button>
            <n-button size="small" secondary @click="backtestHistory.refresh">
              <template #icon><n-icon><RefreshOutline /></n-icon></template>
              {{ t('backtest.refreshHistory') }}
            </n-button>
          </n-space>
        </template>
        <v-chart :option="backtestChartOption" autoresize class="backtest-chart" />
      </n-card>

      <n-card v-if="hasBacktestResult && isPaperBotBacktestResult" :title="t('paperBot.dailyScanLog')" size="small" class="backtest-result-card">
        <n-data-table
          v-if="backtestDailyRows.length > 0"
          :columns="backtestDailyColumns"
          :data="backtestDailyRows"
          :bordered="false"
          :row-key="(row: BacktestDailyLog) => row.date"
          :scroll-x="1100"
          :max-height="320"
          size="small"
          striped
        />
        <n-empty v-else :description="t('paperBot.noBacktestScanLog')" style="padding: 20px 0" />
      </n-card>

      <n-card v-if="hasBacktestResult && isPaperBotBacktestResult" :title="t('paperBot.scanHistory')" size="small" class="backtest-result-card">
        <n-data-table
          v-if="backtestScanRows.length > 0"
          :columns="backtestScanColumns"
          :data="backtestScanRows"
          :bordered="false"
          :row-key="(row: BacktestScanRow) => row.id"
          :scroll-x="900"
          :max-height="300"
          size="small"
          striped
        />
        <n-empty v-else :description="t('paperBot.noBacktestScanLog')" style="padding: 20px 0" />
      </n-card>

      <n-card v-if="hasBacktestResult" :title="t('backtest.tradeDetails')" size="small" class="backtest-result-card">
        <n-data-table
          v-if="backtestResult!.trades.length > 0"
          :columns="backtestTradeColumns"
          :data="backtestResult!.trades"
          :bordered="false"
          :scroll-x="1020"
          :max-height="400"
          virtual
          size="small"
          striped
        />
        <n-empty v-else :description="t('backtest.noTrades')" style="padding: 20px 0" />
      </n-card>

      <n-card v-if="!hasBacktestResult && backtestHistory.data.value?.length === 0" size="small" class="backtest-result-card">
        <n-empty :description="t('backtest.placeholder')" style="padding: 60px 0" />
      </n-card>

      <n-card
        v-if="!hasBacktestResult && backtestHistory.data.value && backtestHistory.data.value.length > 0"
        :title="t('backtest.historyTitle')"
        size="small"
        class="backtest-result-card"
      >
        <template #header-extra>
          <n-button size="small" secondary @click="backtestHistory.refresh">
            <template #icon><n-icon><RefreshOutline /></n-icon></template>
            {{ t('backtest.refreshHistory') }}
          </n-button>
        </template>
        <n-data-table
          :row-props="backtestHistoryRowProps"
          :columns="backtestHistoryColumns"
          :data="backtestHistory.data.value"
          :bordered="false"
          :scroll-x="890"
          size="small"
          striped
        />
      </n-card>
    </template>

    <n-modal v-model:show="showCloseModal" preset="card" :title="t('positions.confirmClose')" style="width: 420px">
      <n-text v-if="closeTarget">
        {{ t('paperBot.closePaperMessage', {
          base: closeTarget.base,
          long: closeTarget.long_venue || closeTarget.futures_venue || '-',
          short: closeTarget.short_venue || closeTarget.spot_venue || '-',
        }) }}
      </n-text>
      <template #footer>
        <n-space justify="end">
          <n-button size="small" @click="showCloseModal = false">{{ t('positions.cancel') }}</n-button>
          <n-button size="small" type="error" :loading="closing" @click="confirmClose">{{ t('positions.confirmCloseBtn') }}</n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.paper-bot-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
}

.mode-card,
.summary-grid {
  flex-shrink: 0;
}

.mode-card,
.summary-shell,
.strategy-card,
.table-card,
.positions-card,
.ledger-card,
.journal-card,
.backtest-card,
.backtest-result-card {
  border-radius: 8px;
}

.mode-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.summary-card {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 54px;
}

.summary-icon {
  width: 42px;
  height: 42px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  flex-shrink: 0;
}

.summary-copy {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.summary-label {
  font-size: 12px;
  white-space: nowrap;
}

.summary-value {
  font-size: 18px;
  line-height: 1.2;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.section-title span {
  font-weight: 600;
}

.strategy-form :deep(.n-form-item) {
  margin-bottom: 0;
}

.bot-status-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(112px, 1fr));
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--n-border-color);
}

.account-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(112px, 1fr));
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--n-border-color);
}

.status-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(128, 128, 128, 0.06);
}

.status-cell :deep(.n-text) {
  font-size: 12px;
}

.status-cell-wide {
  grid-column: span 2;
}

.single-line {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.range-row {
  display: grid;
  grid-template-columns: minmax(64px, 1fr) auto minmax(64px, 1fr);
  align-items: center;
  gap: 6px;
  width: 100%;
}

.table-card,
.positions-card,
.ledger-card,
.journal-card,
.backtest-card,
.backtest-result-card {
  min-height: 0;
}

.backtest-chart {
  height: 260px;
  width: 100%;
}

@media (max-width: 900px) {
  .paper-bot-page {
    height: auto;
  }

  .mode-bar {
    align-items: flex-start;
    flex-direction: column;
  }

  .bot-status-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .account-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .status-cell-wide {
    grid-column: span 2;
  }
}
</style>
