import type { Ref } from "vue";
import { ref, onUnmounted } from "vue";
import {
  isDemoMode,
  resolveDemoRoute,
  useDemoSnapshot,
} from "@/composables/useDemoSnapshot";

const API_BASE = "/api";

// In demo mode, prime the snapshot cache on module load so the very first
// `useApi(...)` call already has data to resolve against.
if (isDemoMode) {
  useDemoSnapshot()
    .ensure()
    .catch(() => {
      /* surfaced via useDemoSnapshot().error */
    });
}

// ─── Types (aligned with backend) ───────────────────────────────────

export interface ScannerStatus {
  scanning: boolean;
  scan_started_at?: string | null;
  scan_age_sec?: number;
  last_scan_time: string | null;
  has_data: boolean;
  live: boolean;
  last_error?: string | null;
}

export interface OpportunityItem {
  base: string;
  direction?: string;
  long_venue: string;
  short_venue: string;
  long_rate_pct: number;
  short_rate_pct: number;
  spread_pct: number;
  net_edge_pct: number;
  real_edge_pct?: number;
  annual_apy_pct?: number;
  net_apy_pct?: number;
  long_symbol?: string;
  short_symbol?: string;
  fee_pct?: number;
  round_trip_fee_pct?: number;
  long_mark?: number;
  short_mark?: number;
  mark_spread_pct?: number;
  settle_mismatch?: boolean;
  same_interval?: boolean;
  long_interval_h?: number;
  short_interval_h?: number;
  /** clean | caution | high — mark/basis risk vs strategy real-edge bar */
  basis_risk_level?: "clean" | "caution" | "high";
}

export interface ScannerOpportunities {
  venues: string[];
  total_assets_scanned: number;
  total_spreads_found: number;
  forward: OpportunityItem[];
  reverse: OpportunityItem[];
  venue_pair_stats: Array<{ pair: string; count: number }>;
  timestamp: string;
}

export interface PositionItem {
  id: string;
  base: string;
  direction: string;
  long_venue: string;
  short_venue: string;
  status: string;
  // Real data fields
  qty?: number;
  long_price?: number;
  short_price?: number;
  trade_usd?: number;
  amount_usd?: number; // legacy field name
  pnl_usd?: number;
  unrealized_pnl_usd?: number;
  mark_spread_pct?: number;
  open_spread_pct?: number; // legacy field name
  open_edge_pct?: number; // legacy field name
  opened_at?: number; // ms timestamp
  open_time?: string; // legacy ISO string
  closed_at?: number;
  dry_run?: boolean;
  managed_by?: string;
  opened_by?: string;
  source?: string;
  bot_strategy?: string;
  paper_margin_usd?: number;
  paper_open_fee_usd?: number;
  paper_close_fee_est_usd?: number;
  paper_round_trip_fee_est_usd?: number;
  paper_fee_pct?: number;
  paper_long_rate_pct?: number;
  paper_short_rate_pct?: number;
  paper_long_interval_h?: number;
  paper_short_interval_h?: number;
  strategy?: string;
  quote?: string;
  long_symbol?: string;
  short_symbol?: string;
  // ─── Detailed metrics (added for enhanced positions view) ──────
  /** Close information from executor (close prices, spreads) */
  close_info?: {
    long_price?: number;
    short_price?: number;
    futures_price?: number;
    spot_price?: number;
    open_mark_spread?: number;
    close_mark_spread?: number;
    dry_run?: boolean;
    [key: string]: unknown;
  };
  /** Long leg fill quantity (pure futures) */
  long_qty?: number;
  /** Short leg fill quantity (pure futures) */
  short_qty?: number;
  /** Futures venue (carry/unified strategies) */
  futures_venue?: string;
  /** Spot venue (carry/unified strategies) */
  spot_venue?: string;
  /** Futures open price (carry/unified) */
  futures_price?: number;
  /** Spot open price (carry/unified) */
  spot_price?: number;
  /** Whether legs were opened in parallel */
  parallel_legs?: boolean;
}

export interface BacktestSummary {
  total_pnl_usd: number;
  total_pnl_pct: number;
  annualized_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  win_rate: number;
  total_trades: number;
  avg_hold_days: number;
  scan_count?: number;
  avg_candidates_after_filter?: number;
  avg_ready_candidates?: number;
  open_actions?: number;
  close_actions?: number;
  final_equity?: number;
}

export interface BacktestTrade {
  base: string;
  direction: string;
  long_venue: string;
  short_venue: string;
  open_time: string;
  close_time: string;
  hold_days: number;
  pnl_usd: number;
  close_reason?: string;
  amount_usd?: number;
  funding_pct?: number;
  fee_pct?: number;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  open_pairs?: number;
  capital_free?: number;
}

export interface BacktestScanRun {
  ts: string;
  scan_total: number;
  candidates_after_filter: number;
  ready_candidates: number;
  actions: PaperBotAction[];
  open_positions: number;
  filter_counts?: Record<string, number>;
  capital_free?: number;
  equity?: number;
  synthetic_settle_windows?: number;
  skipped?: PaperBotSkipped[];
}

export interface BacktestDailyLog {
  date: string;
  scan_runs: number;
  pairs_scanned: number;
  spread_ok: number;
  net_edge_ok: number;
  real_edge_ok: number;
  mark_spread_ok: number;
  settle_window_ok: number;
  mismatch_ok: number;
  final_candidates: number;
  consecutive_ok: number;
  open_actions: number;
  close_actions: number;
}

export interface BacktestResult {
  id: string;
  mode?: string;
  params: Record<string, any>;
  summary: BacktestSummary;
  trades: BacktestTrade[];
  equity_curve?: EquityPoint[];
  scan_journal?: BacktestScanRun[];
  daily_logs?: BacktestDailyLog[];
  data_quality?: Record<string, any>;
  run_time: string;
  live: boolean;
}

export interface VenueConfig {
  id: string;
  name: string;
  type: string;
  configured: boolean;
  missing_keys: string[];
  status: string;
  scan_capable?: boolean;
  trade_capable?: boolean;
  trade_reason?: string;
  live_ready?: boolean;
  live_reason?: string;
}

export interface StrategyParams {
  min_spread_annual: number;
  min_edge_annual: number;
  max_mark_spread_pct: number;
  trade_usd: number;
  max_positions: number;
  scan_interval_sec: number;
  scan_venues?: string[];
  min_edge_1h?: number;
  min_edge_mismatch?: number;
  fee_mode?: "auto" | "api" | "vip_tier";
  venue_fee_tiers?: Record<string, string>;
}

export interface PaperBotConfig {
  enabled: boolean;
  initialBalanceUsdt: number;
  depthMultiple: number;
  consecutiveHits: number;
  minSettleMinutes: number;
  maxSettleMinutes: number;
  maxHoldHours: number;
  maxActionsPerRun: number;
  activeExitEnabled: boolean;
  activeExitConfirmMinutes: number;
  activeExitWindowMinutes: number;
  activeExitMaxMarkSpreadPct: number;
}

export interface PaperBotAction {
  action: string;
  position_id?: string;
  base?: string;
  reason?: string;
  edge?: number;
  hold_hours?: number;
  candidate_key?: string;
  candidate?: OpportunityItem & Record<string, any>;
  result?: Record<string, any>;
}

export interface PaperBotSkipped {
  key: string;
  base?: string;
  direction?: string;
  long_venue?: string;
  short_venue?: string;
  reason: string;
}

export interface PaperBotSummary {
  ts: string;
  strategy: string;
  dry_run: boolean;
  enabled: boolean;
  venues: string[];
  scan_total: number;
  candidates_after_filter: number;
  ready_candidates: number;
  actions: PaperBotAction[];
  open_positions: number;
  hit_counts: Record<string, number>;
  skipped: PaperBotSkipped[];
  thresholds: Record<string, any>;
  account?: PaperBotAccount;
}

export interface PaperBotAccount {
  currency: string;
  initial_balance_usdt: number;
  equity_usdt: number;
  available_balance_usdt: number;
  reserved_margin_usdt: number;
  gross_pnl_usdt: number;
  net_pnl_usdt: number;
  realized_pnl_usdt: number;
  unrealized_pnl_usdt: number;
  price_pnl_usdt: number;
  funding_pnl_usdt: number;
  fees_paid_usdt: number;
  open_fee_paid_usdt: number;
  close_fee_paid_usdt: number;
  total_trade_usdt: number;
  open_positions: number;
  closed_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  ledger_entries: number;
  updated_at: string;
}

export interface PaperBotLedgerEntry {
  id: string;
  ts: string;
  type: "open" | "close" | string;
  position_id: string;
  base: string;
  direction: string;
  long_venue: string;
  short_venue: string;
  trade_usd: number;
  gross_notional_usd?: number;
  qty?: number;
  fee_usd?: number;
  open_fee_usd?: number;
  close_fee_usd?: number;
  total_fee_usd?: number;
  margin_usd?: number;
  margin_released_usd?: number;
  price_pnl_usd?: number;
  funding_pnl_usd?: number;
  gross_pnl_usd?: number;
  net_pnl_usd?: number;
  long_settlements?: number;
  short_settlements?: number;
  real_edge_pct?: number;
  net_edge_pct?: number;
  edge_pct?: number | null;
  reason?: string;
}

export interface PaperBotStatus {
  running: boolean;
  last_run_at: string | null;
  last_finished_at: string | null;
  last_error: string | null;
  last_summary: PaperBotSummary | null;
  hit_counts: Record<string, number>;
  total_runs: number;
  next_run_at: string | null;
  auto_started_at: string | null;
  auto_stopped_at: string | null;
  auto_running: boolean;
  available: boolean;
  import_error: string | null;
  config: PaperBotConfig;
  locked: boolean;
  config_updated_at?: string;
}

export interface FeeTierOption {
  id: string;
  label: string;
  spot_taker_pct: number;
  futures_taker_pct: number;
}

export interface ResolvedVenueFee {
  has_credentials: boolean;
  uses_api: boolean;
  tier: string | null;
  spot_taker_pct: number;
  futures_taker_pct: number;
  spot_source: "api" | "tier" | "default";
  futures_source: "api" | "tier" | "default";
}

export interface ResolvedFees {
  fee_mode: string;
  venue_fee_tiers: Record<string, string>;
  venues: Record<string, ResolvedVenueFee>;
}

export interface CredentialsStatus {
  backends: Record<
    string,
    {
      available: boolean;
      description: string;
      path?: string;
    }
  >;
  venues_configured: string[];
  venues_missing: string[];
}

export interface ApiResponse<T> {
  data: Ref<T | null>;
  error: Ref<string | null>;
  loading: Ref<boolean>;
  refresh: () => Promise<void>;
}

// ─── Request helpers ────────────────────────────────────────────────

async function request<T>(url: string): Promise<T> {
  // Demo mode: short-circuit known GET paths against the snapshot cache.
  // Falls through for unknown paths (POST endpoints, write APIs, etc.) so
  // they fail loudly against the static host rather than silently stubbing.
  if (isDemoMode) {
    // Make sure the snapshot is loaded (no-op if already cached).
    await useDemoSnapshot().ensure();
    const demoData = resolveDemoRoute(url);
    if (demoData !== undefined) {
      return demoData as T;
    }
  }
  const response = await fetch(`${API_BASE}${url}`);
  if (!response.ok) {
    throw new Error(`API ${response.status}: ${response.statusText}`);
  }
  const json = await response.json();
  if (json && typeof json === "object" && "success" in json && "data" in json) {
    if (!json.success) {
      throw new Error(
        json.error || json.message || "API returned success=false",
      );
    }
    return json.data as T;
  }
  return json as T;
}

export async function post<T>(
  url: string,
  body: Record<string, any>,
): Promise<T> {
  const response = await fetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`API ${response.status}: ${response.statusText}`);
  }
  const json = await response.json();
  if (json && typeof json === "object" && "success" in json && "data" in json) {
    if (!json.success) {
      throw new Error(
        json.error || json.message || "API returned success=false",
      );
    }
    return json.data as T;
  }
  return json as T;
}

// ─── Composable ─────────────────────────────────────────────────────

function useApi<T>(url: string, initialData: T | null = null): ApiResponse<T> {
  const data = ref<T | null>(initialData) as Ref<T | null>;
  const error = ref<string | null>(null);
  const loading = ref(false);

  async function refresh() {
    loading.value = true;
    error.value = null;
    try {
      const result = await request<T>(url);
      if (result !== null && result !== undefined) {
        data.value = result;
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  return { data, error, loading, refresh };
}

// ─── Composables for each API endpoint ──────────────────────────────

export function getScannerStatus(strategy: string = "pure") {
  return useApi<ScannerStatus>(`/scanner/status?strategy=${strategy}`);
}

export function getScannerOpportunities(strategy: string = "pure") {
  return useApi<ScannerOpportunities>(
    `/scanner/opportunities?strategy=${strategy}`,
  );
}

export function getPositions() {
  return useApi<PositionItem[]>("/positions");
}

export function getBacktestHistory() {
  return useApi<BacktestResult[]>("/backtest/history");
}

export function getVenues() {
  return useApi<VenueConfig[]>("/settings/venues");
}

export function getCredentialsStatus() {
  return useApi<CredentialsStatus>("/settings/credentials/status");
}

export function getStrategy() {
  return useApi<StrategyParams>("/settings/strategy");
}

export function getFeeTiers() {
  return useApi<Record<string, FeeTierOption[]>>("/settings/fee-tiers");
}

export function getResolvedFees() {
  return useApi<ResolvedFees>("/settings/fees");
}

export function getPaperBotConfig() {
  return useApi<PaperBotConfig>("/paper-bot/config");
}

export function getPaperBotStatus() {
  return useApi<PaperBotStatus>("/paper-bot/status");
}

export function getPaperBotAccount() {
  return useApi<PaperBotAccount>("/paper-bot/account");
}

export function getPaperBotJournal(limit = 50) {
  return useApi<PaperBotSummary[]>(`/paper-bot/journal?limit=${limit}`);
}

export function getPaperBotLedger(limit = 100) {
  return useApi<PaperBotLedgerEntry[]>(`/paper-bot/ledger?limit=${limit}`);
}

export function startPaperBot() {
  return post<PaperBotStatus>("/paper-bot/start", {});
}

export function stopPaperBot() {
  return post<PaperBotStatus>("/paper-bot/stop", {});
}

// ─── Cash-and-Carry types ──────────────────────────────────────────

export interface CarryCand {
  base: string;
  symbol: string;
  rate_pct: number;
  annual_pct: number;
  next_ts: number;
  interval_h: number;
  has_spot?: boolean;
  borrowable?: boolean;
  spot_price?: number;
  net_edge_pct: number;
  mark_price?: number;
  fee_pct?: number;
  borrow_daily_pct?: number;
  borrow_annual_pct?: number;
}

export interface CarryVenue {
  venue: string;
  total_pairs: number;
  forward: CarryCand[];
  reverse: CarryCand[];
  spot_fee_pct?: number;
  futures_fee_pct?: number;
  two_leg_fee_pct?: number;
  error?: string;
}

export interface UnifiedCarryCand {
  base: string;
  direction: string;
  futures_venue: string;
  spot_venue: string;
  same_venue: boolean;
  funding_rate_pct: number;
  annual_pct: number;
  spot_fee_pct: number;
  futures_fee_pct: number;
  fee_pct: number;
  net_edge_pct: number;
  borrow_daily_pct?: number;
}

// ─── Wallet & Trading Mode types ────────────────────────────────

export interface WalletFieldSchema {
  key: string;
  label: string;
  type: "text" | "password" | "number" | "select";
  placeholder?: string;
  options?: string[];
  default?: string;
}

export interface WalletVenueSchema {
  name: string;
  chain: string;
  fields: WalletFieldSchema[];
  extra_fields: WalletFieldSchema[];
  live_flag: string | null;
}

export interface WalletVenueStatus {
  connected: boolean;
  chain: string;
  live_enabled: boolean;
  live_flag: string | null;
  fields_masked: Record<string, string>;
  balance_usdc: number;
}

export interface TradingModeVenue {
  mode: "backtest" | "dry_run" | "live";
  wallet_connected: boolean;
  live_enabled: boolean;
}

export interface TradingMode {
  mode: "backtest" | "dry_run" | "live";
  venues: Record<string, TradingModeVenue>;
}

export function getWalletSchemas() {
  return useApi<Record<string, WalletVenueSchema>>("/settings/wallet/schema");
}

export function getWalletStatus(venue?: string) {
  const url = venue
    ? `/settings/wallet/status?venue=${venue}`
    : "/settings/wallet/status";
  return useApi<Record<string, WalletVenueStatus>>(url);
}

export function getTradingMode() {
  return useApi<TradingMode>("/settings/trading-mode");
}

export async function connectWallet(
  venue: string,
  credentials: Record<string, string>,
) {
  return post<{ venue: string; connected: boolean }>(
    "/settings/wallet/connect",
    { venue, credentials },
  );
}

export async function disconnectWallet(venue: string) {
  return post<{ venue: string; connected: boolean }>(
    "/settings/wallet/disconnect",
    { venue },
  );
}

// ─── WebSocket (shared singleton) ─────────────────────────────────
//
// Only one WS connection is maintained per browser tab. Multiple callers
// (App.vue for connection status, Scanner.vue for scanner updates) share
// the same connection via a subscribe/unsubscribe pattern.

export interface WsMessage {
  event: string;
  data: Record<string, any>;
}

type WsSubscriber = (msg: WsMessage) => void;

const _wsState = {
  ws: null as WebSocket | null,
  connected: ref(false),
  subscribers: new Set<WsSubscriber>(),
  connectCallbacks: new Set<() => void>(),
  disconnectCallbacks: new Set<() => void>(),
  reconnectTimer: null as ReturnType<typeof setTimeout> | null,
  pingTimer: null as ReturnType<typeof setInterval> | null,
};

function _wsConnect() {
  if (_wsState.ws && _wsState.ws.readyState <= WebSocket.OPEN) return;

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  const url = `${protocol}//${host}/ws/events`;

  _wsState.ws = new WebSocket(url);

  _wsState.ws.onopen = () => {
    _wsState.connected.value = true;
    _wsState.connectCallbacks.forEach((cb) => cb());
    _wsState.pingTimer = setInterval(() => {
      if (_wsState.ws?.readyState === WebSocket.OPEN) {
        _wsState.ws.send("ping");
      }
    }, 30000);
  };

  _wsState.ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data) as WsMessage;
      if (msg.event === "pong") return;
      _wsState.subscribers.forEach((cb) => cb(msg));
    } catch {
      // ignore non-JSON messages
    }
  };

  _wsState.ws.onclose = () => {
    _wsState.connected.value = false;
    if (_wsState.pingTimer) {
      clearInterval(_wsState.pingTimer);
      _wsState.pingTimer = null;
    }
    _wsState.disconnectCallbacks.forEach((cb) => cb());
    _wsState.reconnectTimer = setTimeout(() => _wsConnect(), 3000);
  };

  _wsState.ws.onerror = () => {
    _wsState.ws?.close();
  };
}

function _wsDisconnect() {
  // Don't actually disconnect — the shared singleton stays alive as long
  // as the app is running. Individual components just unsubscribe via
  // onUnmounted. Real cleanup happens on page unload via the browser.
}

export function useWebSocket(
  onMessage?: WsSubscriber,
  onConnect?: () => void,
  onDisconnect?: () => void,
) {
  // Subscribe if callbacks provided
  if (onMessage) {
    _wsState.subscribers.add(onMessage);
  }
  if (onConnect) {
    _wsState.connectCallbacks.add(onConnect);
  }
  if (onDisconnect) {
    _wsState.disconnectCallbacks.add(onDisconnect);
  }

  // Cleanup on unmount
  onUnmounted(() => {
    if (onMessage) _wsState.subscribers.delete(onMessage);
    if (onConnect) _wsState.connectCallbacks.delete(onConnect);
    if (onDisconnect) _wsState.disconnectCallbacks.delete(onDisconnect);
  });

  return {
    connected: _wsState.connected,
    connect: _wsConnect,
    disconnect: _wsDisconnect,
  };
}
