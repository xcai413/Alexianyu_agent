import { createContext, useContext, useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { BrowserRouter, NavLink, Navigate, Route, Routes, useNavigate, useLocation } from 'react-router-dom'

type Summary = {
  accounts: number
  online_accounts: number
  inbound_messages: number
  orders: number
  on_sale_items: number
}

type AccountStatus = {
  account_id: string
  enabled: boolean
  desired_state: string
  worker_state: string
  db_status: string
  reconnect_attempts: number
  last_heartbeat_at: string | null
  last_error: string | null
  owner_instance_id: string | null
  owner_lease_until: string | null
  risk_code: string | null
}

type RuntimeInstance = {
  instance_id: string
  pid: number
  status: string
  version: string
  started_at: string | null
  last_heartbeat_at: string | null
  lease_until: string | null
  stopped_at: string | null
  last_error: string | null
}

type MirroredMessage = {
  id: number
  chat_id: string
  item_id: string | null
  direction: string
  sender: string | null
  content_type: string
  content: string | null
  received_at: string | null
  image_url: string | null
}

type AccountProfile = { account_id: string; nickname: string | null; avatar_url: string | null }
type ItemPictures = { main_image_url: string | null; images: string[]; variant_images: { label: string; url: string }[]; platform_images?: string[]; platform_error?: string | null }

type MirroredItem = {
  id: number
  item_id: string
  title: string
  price: string | null
  main_image_url: string | null
  images: string[]
  sold_count: number
  has_multi_spec: boolean
  is_on_sale: boolean
  last_synced_at: string | null
}

type MirroredOrder = {
  id: number
  order_id: string
  item_id: string | null
  item_title: string | null
  buyer_id: string | null
  buyer_name: string | null
  amount: number
  quantity: number
  status: string
  placed_at: string | null
  paid_at: string | null
  delivered_at: string | null
  delivery_state: string
}

type PlatformOrder = {
  order_id: string | null; item_id: string | null; item_title: string | null; item_image: string | null
  buyer_id: string | null; buyer_nickname: string | null; buyer_avatar: string | null
  receiver_name: string | null; receiver_phone: string | null; receiver_address: string | null
  is_privacy: boolean | null; address_change_requested: boolean | null; in_refund: boolean | null
  status_text: string | null; created_at: string | null; paid_at: string | null; consigned_at: string | null; finished_at: string | null
  seller_rate_status: string | number | boolean | null; refund_status: string | null; consign_tips: string | null
  amount: string | number | null; unit_price: string | number | null; quantity: string | number | null
  post_fee: string | number | null; discount_fee: string | number | null; received_fee: string | number | null; refund_fee: string | number | null
  item_info: { name: string; value: string }[]
}
type ConsignInfo = {
  buyer_name: string; buyer_phone: string; buyer_address: string; quantity: number
  spec_name: string; spec_value: string; specifications: { name: string; value: string }[]
  need_serial_number: boolean | null; need_upload_report: boolean | null; support_consign_type: string[]
}
type LogisticsPackage = {
  trade_id: string; mail_no: string; status: string
  nodes: { time: string; desc: string; action: string; status_desc: string }[]
}
type LogisticsInfo = LogisticsPackage & { package_count: number; packages: LogisticsPackage[] }
type RefundInfo = Record<string, string | number | boolean | null | Record<string, string | number | boolean | null>[]>
type OrderReadResult<T> = { data: T | null; error: string | null }
type OrderDetails = {
  order: OrderReadResult<PlatformOrder>; consign: OrderReadResult<ConsignInfo>
  logistics: OrderReadResult<LogisticsInfo>; refund: OrderReadResult<RefundInfo>
}

type InventoryCard = {
  id: number
  account_id: string | null
  name: string
  type: string
  total: number
  remaining: number
  enabled: boolean
}

type ReplyRule = {
  id: number
  name: string
  type: string
  pattern: string
  priority: number
  enabled: boolean
  reply_image_url: string | null
  reply_once: boolean
}

type AuditEntry = {
  id: number
  actor: string
  action: string
  target: string | null
  result: string
  created_at: string | null
}

type BackupImportResult = {
  mode: string
  created: Record<string, number>
  skipped: Record<string, number>
  total_created: number
  total_skipped: number
}

type WorkerCommandStatus = {
  command_id: string
  action: string
  status: string
  daemon_instance_id: string | null
  requested_at: string | null
  claimed_at: string | null
  completed_at: string | null
}

type AnalyticsWindowRow = {
  days: number
  orders: number
  paid_orders: number
  delivered_orders: number
  amount: number
  inbound_messages: number
  outbound_messages: number
  new_items: number
}

type DailyRevenue = { date: string; amount: number; paid_orders: number }
type AnalyticsRead = LoadState<AnalyticsPayload> & { reload: () => void }
type StatisticsRange = { start: string; end: string }
type RangeControls = { range: StatisticsRange | null; onCustom: () => void; onPreset: () => void }

type AnalyticsPayload = {
  revenue_daily: DailyRevenue[]
  date_range: StatisticsRange | null
  data_quality?: { orders_missing_paid_at: number; orders_missing_delivered_at: number }
  windows: AnalyticsWindowRow[]
  automation: {
    jobs_completed: number
    jobs_failed: number
    jobs_waiting_human: number
    automation_completed: number
    automation_uncertain: number
  }
  on_sale_items: number
  attention_items: number
  last_item_sync_at: string | null
  last_order_sync_at: string | null
}

type ConversationSummary = {
  chat_id: string
  buyer_id: string | null
  item_id: string | null
  last_message_at: string | null
  unread_count: number
  bot_state: string
  takeover_state: string
}

type DeliveryJobRow = {
  id: number
  order_id: string
  state: string
  required_quantity: number
  reservation_id: number | null
  item_id: string | null
  last_error: string | null
  created_at: string | null
  completed_at: string | null
}

type DeliveryPlanResult = {
  allowed: boolean
  reason: string
  order_status: string | null
  guard_decision: string | null
  policy_name: string | null
  pool_id: number | null
  quantity: number
  delay_seconds: number
  platform_confirm: boolean
  hit_rules: string[]
}

type ApiDeliveryConfigRow = {
  id: number
  name: string
  url: string
  method: string
  header_names: string[]
  response_path: string
  timeout_seconds: number
  enabled: boolean
  description: string | null
  variables: string[]
  secret_refs: string[]
  secrets_configured: boolean
}

type DeliveryPoolRow = {
  id: number
  name: string
  type: string
  image_url: string | null
  account_scope: string | null
  item_scope: string | null
  sku_scope: string | null
  api_config_ref: string | null
  delay_seconds: number
  enabled: boolean
  available: number
  reserved: number
  uncertain: number
}

type DeliveryPolicyRow = {
  id: number
  name: string
  inventory_pool_id: number
  account_id: string | null
  item_id: string | null
  sku_id: string | null
  level: string
  delay_seconds: number
  priority: number
  enabled: boolean
  platform_confirm: boolean
}

type PlatformAutomationRow = {
  action: string
  enabled: boolean
  calibrated: boolean
  calibration_approved_at: string | null
  calibration_approved_by: string | null
  schedule_time: string
  delay_minutes: number
  quiet_hours: string | null
}

type PlatformAutomationRun = {
  id: number
  action: string
  target_id: string
  status: string
  result: string | null
  started_at: string | null
  finished_at: string | null
}

type CardUnitRow = {
  unit_id: number
  state: string
  preview: string
  reserved_by: string | null
  reservation_id: number | null
}

type DeliveryTemplateRow = {
  id: number
  name: string
  content: string
  variables: string[]
  enabled: boolean
  description: string | null
}

type AttentionRow = {
  kind: string
  target: string
  account_id: string | null
  state: string
  reason: string
  next_command: string
  since: string | null
}

type NotificationChannelRow = {
  id: number
  name: string
  kind: string
  target_masked: string
  secret_ref: string | null
  events: string[]
  enabled: boolean
  description: string | null
  accounts: string[]
}

type NotificationMessageRow = {
  id: number
  channel_id: number | null
  event: string
  target: string
  state: string
  ok: boolean
  error: string | null
  created_at: string | null
}

type ReplyRecordRow = {
  id: number
  rule_id: number | null
  rule_name: string | null
  conversation_id: number | null
  success: boolean
  source: string
  sent_at: string | null
  text_length: number
  text_preview: string | null
  has_image: boolean
  error: string | null
}

type ItemAutomationRow = {
  item_id: string
  title: string
  mode: string | null
  note: string | null
  by: string | null
  at: string | null
}

type QuickReplyRow = {
  id: number
  account_id: string | null
  title: string
  content: string
  sort_order: number
}

type BuyerNotePayload = {
  account_id: string
  buyer_id: string
  note: string
  updated_at: string | null
} | null

type LogSummary = {
  exists: boolean
  path: string
  total_lines?: number
  counts: Record<string, number>
  recent: string[]
}

type RiskEventRow = {
  id: number
  action: string
  target: string | null
  result: string
  created_at: string | null
}

type LoadState<T> = {
  data: T | null
  error: string | null
  loading: boolean
}

const links = [
  ['/dashboard', '概览'],
  ['/accounts', '账号管理'],
  ['/messages', '消息中心'],
  ['/items', '商品管理'],
  ['/orders', '订单管理'],
  ['/inventory', '卡密库存'],
  ['/delivery', '发货管理'],
  ['/automation', '自动化与回复'],
  ['/runtime', '运行状态'],
  ['/audit', '审计'],
  ['/notify', '通知'],
  ['/backup', '备份'],
] as const

function useProtectedData<T>(path: string | null) {
  const [state, setState] = useState<LoadState<T> & { path: string | null }>({
    data: null,
    error: null,
    loading: path !== null,
    path,
  })
  const [revision, setRevision] = useState(0)
  const reload = useCallback(() => {
    setState((current) => ({ ...current, loading: true, error: null }))
    setRevision((current) => current + 1)
  }, [])

  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    fetch(path, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 401 ? '登录已失效' : response.status === 403 ? '当前会话无权读取' : '读取失败')
        return (await response.json()) as { data: T }
      })
      .then((payload) => { if (!controller.signal.aborted) setState({ data: payload.data, error: null, loading: false, path }) })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        if (!controller.signal.aborted) setState({ data: null, error: error instanceof Error && ['登录已失效', '当前会话无权读取', '读取失败'].includes(error.message) ? error.message : '暂时无法读取数据', loading: false, path })
      })

    return () => controller.abort()
  }, [path, revision])

  // path 为空(例如还没选中卡券池)时不发起请求,也不把旧数据当成当前数据。
  return path
    ? state.path === path ? { ...state, reload } : { data: null, error: null, loading: true, reload }
    : { data: null, error: null, loading: false, reload }
}

function PageState({ error, loading, emptyText, onRetry }: { error: string | null; loading: boolean; emptyText: string; onRetry?: () => void }) {
  if (!loading && !error && !emptyText) return null
  const authenticationError = error === '登录已失效' || error === '当前会话无权读取'
  const title = loading ? '正在加载…' : error ?? emptyText
  return <div className={`page-state ${loading ? 'page-state-loading' : error ? 'page-state-error' : 'page-state-empty'}`} role={loading ? 'status' : error ? 'alert' : undefined}>
    <Icon name={loading ? 'refresh' : error ? 'activity' : 'archive'} />
    <div><strong>{title}</strong>{loading ? <p>正在读取本地数据，请稍候。</p> : error ? <p>{authenticationError ? '请重新登录管理员会话后查看。' : '请检查本地服务与连接后重试。'}</p> : null}</div>
    {!loading && authenticationError ? <NavLink className="secondary-link" to="/login">重新登录</NavLink> : !loading && error && onRetry ? <button type="button" onClick={onRetry}><Icon name="refresh" />重试读取</button> : null}
  </div>
}

function AccountPicker({ accounts, value, onChange }: { accounts: AccountStatus[]; value: string; onChange: (accountId: string) => void }) {
  return <label>账号 <select aria-label="选择账号" value={value} onChange={(event) => onChange(event.target.value)}>{accounts.map((account) => <option key={account.account_id} value={account.account_id}>{account.account_id}</option>)}</select></label>
}

function useListLocation(accountId?: string) {
  const location = useLocation()
  const navigate = useNavigate()
  const params = new URLSearchParams(location.search)
  const scopeMatches = !accountId || params.get('account') === accountId
  function read(key: string, fallback = '') { return scopeMatches ? params.get(key) ?? fallback : fallback }
  function update(patch: Record<string, string | null>, replace = true) {
    // BrowserRouter 的渲染可能滞后于地址变化；连续操作须合并最新条件。
    const current = new URL(window.location.href)
    if (current.pathname !== location.pathname) return
    const next = new URLSearchParams(current.search)
    if (accountId && params.get('account') !== next.get('account') && next.get('account') !== accountId) return
    if (accountId) next.set('account', accountId)
    for (const [key, value] of Object.entries(patch)) { if (value) next.set(key, value); else next.delete(key) }
    navigate({ pathname: location.pathname, search: next.toString(), hash: current.hash }, { replace })
  }
  return { read, update }
}

const navIcons: Record<string, string> = { '/dashboard': 'home', '/accounts': 'users', '/messages': 'message', '/items': 'cube', '/orders': 'shopping-bag', '/inventory': 'archive', '/delivery': 'truck', '/automation': 'bolt', '/runtime': 'activity', '/audit': 'file-text', '/notify': 'bell', '/backup': 'database' }
function Icon({ name }: { name: string }) { return <img className="ui-icon" src={`/icons/${name}.svg`} alt="" aria-hidden="true" /> }
function localTime(value: string | null) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}
function accountLabel(state: string) { return ({ online: '在线', no_worker: '未运行', disabled: '已停止', stopped: '已停止', connecting: '连接中', error: '异常', reconnecting: '重连中' } as Record<string, string>)[state] ?? state }

function safeImageUrl(value: string | null | undefined) {
  if (!value || !/^(https?:\/\/|\/\/)/i.test(value)) return null
  try { const url = new URL(value, window.location.origin); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null } catch { return null }
}
function MediaImage({ url, label, className = '', preview = false }: { url: string | null | undefined; label: string; className?: string; preview?: boolean }) {
  const source = safeImageUrl(url)
  const [failedSource, setFailedSource] = useState<string | null>(null)
  if (!source || failedSource === source) return <span className={`media-fallback ${className}`} role="img" aria-label={`${label} · ${source ? '加载失败' : '暂无图片'}`}><Icon name={className.includes('account-avatar') ? 'users' : 'cube'} /></span>
  const picture = <img className={`media-image ${className}`} src={source} alt={label} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailedSource(source)} />
  return preview ? <a className="image-preview" href={source} target="_blank" rel="noreferrer" aria-label={`查看原图：${label}`}>{picture}</a> : picture
}
const AccountProfiles = createContext<{ accounts: AccountStatus[]; profiles: Record<string, AccountProfile>; errors: string[]; refresh: () => void }>({ accounts: [], profiles: {}, errors: [], refresh: () => {} })
function AccountProfilesProvider({ children }: { children: ReactNode }) {
  const accounts = useProtectedData<AccountStatus[]>('/api/v1/web/operations/accounts')
  const [profiles, setProfiles] = useState<Record<string, AccountProfile>>({})
  const [errors, setErrors] = useState<string[]>([])
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useEffect(() => {
    if (!accounts.data) return
    const controller = new AbortController()
    void Promise.all(accounts.data.map(async account => {
      try {
        const response = await fetch(`/api/v1/web/operations/accounts/${encodeURIComponent(account.account_id)}/profile`, { signal: controller.signal })
        if (!response.ok) throw new Error('profile unavailable')
        const payload = await response.json() as { data: AccountProfile }
        return { id: account.account_id, profile: payload.data }
      } catch { return { id: account.account_id, profile: null } }
    })).then(results => {
      if (controller.signal.aborted) return
      setProfiles(Object.fromEntries(results.filter(row => row.profile).map(row => [row.id, row.profile as AccountProfile])))
      setErrors(results.filter(row => !row.profile).map(row => row.id))
    })
    return () => controller.abort()
  }, [accounts.data, revision])
  return <AccountProfiles.Provider value={{ accounts: accounts.data ?? [], profiles, errors, refresh }}>{children}</AccountProfiles.Provider>
}
function AccountIdentity({ accountId, compact = false, heading = false }: { accountId: string; compact?: boolean; heading?: boolean }) {
  const { profiles, errors } = useContext(AccountProfiles)
  const profile = profiles[accountId]
  return <div className={`account-identity${compact ? ' compact' : ''}`}><MediaImage url={profile?.avatar_url} label={`${accountId} 的头像`} className="account-avatar" /><div>{heading ? <h2>{profile?.nickname || accountId}</h2> : <strong>{profile?.nickname || accountId}</strong>}{!compact ? <span>{accountId}{errors.includes(accountId) ? ' · 平台资料读取失败' : ''}</span> : null}</div></div>
}
function CurrentAccountIdentity() {
  const location = useLocation()
  const { accounts } = useContext(AccountProfiles)
  const requested = new URLSearchParams(location.search).get('account')
  const account = accounts.find(row => row.account_id === requested) ?? accounts[0]
  return account ? <NavLink className="topbar-account" to={`/accounts?account=${encodeURIComponent(account.account_id)}`} aria-label={`查看账号 ${account.account_id}`}><AccountIdentity accountId={account.account_id} compact /></NavLink> : null
}

function Dashboard() {
  const location = useLocation()
  const navigate = useNavigate()
  const summary = useProtectedData<Summary>('/api/v1/dashboard/summary')
  const accounts = useProtectedData<AccountStatus[]>('/api/v1/web/operations/accounts')
  const requested = new URLSearchParams(location.search).get('account')
  const params = new URLSearchParams(location.search)
  const range = params.get('start') && params.get('end') ? { start: params.get('start')!, end: params.get('end')! } : null
  const [rangeEditor, setRangeEditor] = useState<'revenue' | 'reports' | null>(null)
  const selected = accounts.data?.find(row => row.account_id === requested)?.account_id ?? ''
  const profiles = useContext(AccountProfiles)
  const query = new URLSearchParams()
  if (selected) query.set('account_id', selected)
  if (range) { query.set('start_date', range.start); query.set('end_date', range.end) }
  const analytics = useProtectedData<AnalyticsPayload>(accounts.data ? `/api/v1/dashboard/analytics?${query}` : null)
  function applyRange(value: StatisticsRange | null) {
    const next = new URLSearchParams(location.search)
    if (value) { next.set('start', value.start); next.set('end', value.end) }
    else { next.delete('start'); next.delete('end') }
    setRangeEditor(null)
    navigate({ pathname: location.pathname, search: next.toString() })
  }
  const editor = (target: 'revenue' | 'reports') => rangeEditor === target ? <StatisticsRangeForm key={`${range?.start}:${range?.end}`} range={range} onApply={applyRange} onCancel={() => setRangeEditor(null)} /> : null
  const refresh = () => { summary.reload(); accounts.reload(); profiles.refresh(); analytics.reload() }
  function selectAccount(value: string) { const params = new URLSearchParams(location.search); if (value) params.set('account', value); else params.delete('account'); navigate({ pathname: location.pathname, search: params.toString() }) }
  return <main className="overview-page report-overview">
    <header className="overview-heading"><div><h1>运营概览</h1><p>查看关键指标与运营报表。</p></div><div className="overview-tools"><select aria-label="概览账号筛选" value={selected} onChange={e => selectAccount(e.target.value)}><option value="">全部账号</option>{accounts.data?.map(row => <option key={row.account_id} value={row.account_id}>{row.account_id}</option>)}</select><button onClick={refresh}><Icon name="refresh" />刷新数据</button></div></header>
    {(summary.error || summary.loading) ? <PageState error={summary.error} loading={summary.loading} emptyText="" onRetry={summary.reload} /> : null}
    {accounts.error ? <PageState error={accounts.error} loading={accounts.loading} emptyText="" onRetry={accounts.reload} /> : null}
    <RevenueChart key={selected} analytics={analytics} accountId={selected} range={range} onCustom={() => setRangeEditor('revenue')} onPreset={() => applyRange(null)} editor={editor('revenue')} />
    <div className="overview-metrics" aria-label="全部账号总量">
      {([['users', `${summary.data?.online_accounts ?? '—'} / ${summary.data?.accounts ?? '—'}`, '全部账号在线', '/accounts'], ['cube', summary.data?.on_sale_items ?? '—', '全部在售商品', '/items'], ['file-text', summary.data?.orders ?? '—', '全部订单镜像', '/orders']] as const).map(([icon, value, label, path]) => <NavLink key={path} to={path} className="overview-metric"><span className="metric-icon"><Icon name={icon} /></span><div><strong>{value}</strong><span>{label} <Icon name="chevron-right" /></span></div></NavLink>)}
    </div>
    <AnalyticsPanel accountId={selected} analytics={analytics} range={range} onCustom={() => setRangeEditor('reports')} onPreset={() => applyRange(null)} editor={editor('reports')} />
  </main>
}

function StatisticsRangeForm({ range, onApply, onCancel }: { range: StatisticsRange | null; onApply: (range: StatisticsRange) => void; onCancel: () => void }) {
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date())
  const defaultStart = new Date(Date.parse(`${today}T00:00:00Z`) - 6 * 86400000).toISOString().slice(0, 10)
  const [start, setStart] = useState(range?.start ?? defaultStart)
  const [end, setEnd] = useState(range?.end ?? today)
  const [error, setError] = useState('')
  function apply(event: React.FormEvent) {
    event.preventDefault()
    const days = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000 + 1
    if (!start || !end || !Number.isFinite(days)) { setError('请选择开始和结束日期。'); return }
    if (days < 1) { setError('结束日期不能早于开始日期。'); return }
    if (end > today) { setError('结束日期不能晚于今天。'); return }
    if (days > 365) { setError('自定义范围最多 365 天。'); return }
    setError(''); onApply({ start, end })
  }
  return <form className="statistics-range-form" aria-label="自定义统计范围" onSubmit={apply} noValidate>
    <label>开始日期<input type="date" aria-label="统计开始日期" value={start} max={today} required onChange={event => { setStart(event.target.value); setError('') }} /></label>
    <label>结束日期<input type="date" aria-label="统计结束日期" value={end} max={today} required onChange={event => { setEnd(event.target.value); setError('') }} /></label>
    <div className="statistics-range-actions"><button className="primary" type="submit">应用范围</button><button type="button" onClick={onCancel}>取消</button></div>
    <p className="statistics-range-hint">按上海时区统计，包含首尾日期，最多 365 天；收益、订单和消息共用此范围。</p>
    {error ? <p className="statistics-range-error" role="alert">{error}</p> : null}
  </form>
}

function CountBars({ label, rows }: { label: string; rows: { label: string; value: number; color: string }[] }) {
  const maximum = Math.max(1, ...rows.map(row => row.value))
  return <div className="count-bars" role="img" aria-label={`${label}：${rows.map(row => `${row.label} ${row.value}`).join('，')}`}>{rows.map(row => <div className="count-bar-row" key={row.label}><span>{row.label}</span><div className="count-bar-track"><div style={{ width: `${row.value / maximum * 100}%`, backgroundColor: row.color }} /></div><strong>{row.value.toLocaleString('zh-CN')}</strong></div>)}</div>
}
function RevenueChart({ analytics, accountId, range, onCustom, onPreset, editor }: { analytics: AnalyticsRead; accountId: string; editor: React.ReactNode } & RangeControls) {
  const [days, setDays] = useState(7)
  const [selectedDate, setSelectedDate] = useState('')
  const points = range ? analytics.data?.revenue_daily ?? [] : analytics.data?.revenue_daily?.slice(-days) ?? []
  const total = points.reduce((sum, point) => sum + Math.round(point.amount * 100), 0) / 100
  const peak = Math.max(1, ...points.map(point => point.amount))
  const unit = 10 ** Math.floor(Math.log10(peak))
  const maximum = Math.ceil(peak / unit) * unit
  const selected = points.find(point => point.date === selectedDate) ?? points.at(-1)
  const money = (value: number) => value.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  const missingPaid = analytics.data?.data_quality?.orders_missing_paid_at ?? 0
  const noPaidOrders = points.every(point => point.paid_orders === 0)
  return <section className="revenue-chart" aria-label="收益趋势" aria-busy={analytics.loading}>
    <header><div><h2>收益趋势</h2><p>{accountId ? `账号 ${accountId}` : '全部账号'} · 已付款订单金额 · 按上海时区付款日期统计</p></div><div className="analysis-windows" aria-label="收益时间范围">{[7, 30].map(value => <button type="button" key={value} aria-label={`收益近 ${value} 天`} aria-pressed={!range && days === value} onClick={() => { setDays(value); onPreset() }}>近 {value} 天</button>)}<button type="button" aria-label="收益自定义范围" aria-pressed={!!range} onClick={onCustom}>自定义</button></div></header>
    {editor}
    {analytics.error || analytics.loading ? <PageState error={analytics.error} loading={analytics.loading} emptyText="" onRetry={analytics.reload} /> : points.length ? <>
      <div className="revenue-summary"><div><strong>{missingPaid && noPaidOrders ? '—' : `¥ ${money(total)}`}</strong><span>{range ? `${range.start} 至 ${range.end}` : `近 ${days} 个自然日累计`}</span></div><p className="revenue-selection" aria-live="polite">{selected ? `${selected.date} · ¥ ${money(selected.amount)} · ${selected.paid_orders} 笔付款` : ''}</p></div>
      <div className="revenue-scroll" tabIndex={points.length > 31 ? 0 : undefined} aria-label={points.length > 31 ? '收益图，可横向滚动查看日期' : undefined}><div className="revenue-plot" style={points.length > 31 ? { minWidth: `${points.length * 12 + 48}px` } : undefined} role="group" aria-label="每日收益柱状图">
        {[0, .5, 1].map(ratio => <div className="revenue-gridline" key={ratio} style={{ top: `${ratio * 100}%` }}><span>{(maximum * (1 - ratio)).toLocaleString('zh-CN', { notation: 'compact', maximumFractionDigits: 1 })}</span></div>)}
        <div className="revenue-bars" style={{ gridTemplateColumns: `repeat(${points.length}, minmax(0, 1fr))` }}>{points.map((point, index) => <div className="revenue-column" key={point.date}><button type="button" className="revenue-day" aria-label={`${point.date}，收益 ${money(point.amount)} 元，${point.paid_orders} 笔付款`} aria-pressed={selected?.date === point.date} onMouseMove={() => setSelectedDate(point.date)} onFocus={() => setSelectedDate(point.date)} onClick={() => setSelectedDate(point.date)}><span className="revenue-bar" style={{ height: `${Math.max(0, point.amount) / maximum * 100}%` }} /></button><span className="revenue-date">{points.length <= 7 || index === 0 || index === points.length - 1 || index % (points.length > 31 ? 7 : Math.ceil(points.length / 6)) === 0 ? point.date.slice(5).replace('-', '/') : ''}</span></div>)}</div>
        {noPaidOrders ? <p className="revenue-empty">{missingPaid ? '付款时间未完整同步，暂无可统计的收益' : '此时间段暂无已付款订单'}</p> : null}
      </div></div><p className="revenue-note">来自本地订单镜像，未扣除成本和退款。{points.length > 31 ? ' 可横向滚动查看更多日期。' : ''}</p>
      {missingPaid ? <p className="statistics-quality" role="status">本地有 {missingPaid} 笔已付款订单缺少付款时间，未计入日期统计；请同步订单。</p> : null}
    </> : <p className="empty-inline">暂无按日收益数据。</p>}
  </section>
}
function AnalyticsPanel({ accountId, analytics, range, onCustom, onPreset, editor }: { accountId: string; analytics: AnalyticsRead; editor: React.ReactNode } & RangeControls) {
  const { data } = analytics
  const [days, setDays] = useState(7)
  const window = range ? data?.windows[0] : data?.windows.find(row => row.days === days) ?? data?.windows[0]
  const period = range ? `${range.start} 至 ${range.end}` : `近 ${window?.days ?? days} 天`
  const delivery = data?.automation
  const totalJobs = delivery ? delivery.jobs_completed + delivery.jobs_failed + delivery.jobs_waiting_human : 0
  const percentage = totalJobs && delivery ? Math.round(delivery.jobs_completed / totalJobs * 100) : 0
  return <div className="overview-analytics">
    <header className="analysis-heading"><div><h2>运营报表</h2><p>{accountId ? `账号 ${accountId}` : '全部账号'} · {range ? period : '最近各窗口的累计数据'}</p></div><div className="analysis-windows" aria-label="分析时间窗口">{[1, 7, 30].map(value => <button key={value} type="button" aria-pressed={!range && days === value} onClick={() => { setDays(value); onPreset() }}>近 {value} 天</button>)}<button type="button" aria-label="报表自定义范围" aria-pressed={!!range} onClick={onCustom}>自定义</button></div></header>
    {editor}
    {data?.data_quality?.orders_missing_delivered_at ? <p className="statistics-quality" role="status">本地有 {data.data_quality.orders_missing_delivered_at} 笔已发货／已完成订单缺少发货时间，未计入日期统计；请同步订单。</p> : null}
    {window && delivery ? <>
      <div className="analytics-chart-grid">
        <section className="analytics-card"><header><div><h3>订单与成交</h3><p>{period} · 按下单、付款、发货时间统计</p></div><Icon name="shopping-bag" /></header><div className="chart-highlight"><strong>¥ {window.amount.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><span>已付款订单累计金额</span></div><CountBars label="订单统计" rows={[{ label: '下单', value: window.orders, color: '#e6c634' }, { label: '已付款', value: window.paid_orders, color: '#779888' }, { label: '已发货', value: window.delivered_orders, color: '#9ba6bc' }]} />{!window.orders ? <p className="chart-empty">此窗口暂无新增订单</p> : null}</section>
        <section className="analytics-card"><header><div><h3>消息收发</h3><p>{period} · 本地消息镜像</p></div><Icon name="message" /></header><div className="chart-highlight"><strong>{(window.inbound_messages + window.outbound_messages).toLocaleString('zh-CN')} <small>条</small></strong><span>收到与发出的消息合计</span></div><CountBars label="消息统计" rows={[{ label: '收到', value: window.inbound_messages, color: '#e6c634' }, { label: '发出', value: window.outbound_messages, color: '#779888' }]} /><p className="chart-footnote">此窗口新增商品 {window.new_items} 件{!window.inbound_messages && !window.outbound_messages ? ' · 暂无消息记录' : ''}</p></section>
        <section className="analytics-card"><header><div><h3>发货任务</h3><p>已完成／失败／待人工 · 全部时间</p></div><Icon name="truck" /></header><div className="delivery-chart"><svg viewBox="0 0 120 120" role="img" aria-label={`发货完成 ${delivery.jobs_completed}，失败 ${delivery.jobs_failed}，待人工 ${delivery.jobs_waiting_human}`}><circle cx="60" cy="60" r="46" fill="none" stroke="#f0eee7" strokeWidth="12" />{totalJobs > 0 ? [delivery.jobs_completed, delivery.jobs_failed, delivery.jobs_waiting_human].map((count, index, counts) => <circle key={index} cx="60" cy="60" r="46" fill="none" stroke={['#779888', '#cb8875', '#e6c634'][index]} strokeWidth="12" pathLength="100" strokeDasharray={`${count / totalJobs * 100} 100`} strokeDashoffset={-counts.slice(0, index).reduce((sum, value) => sum + value, 0) / totalJobs * 100} transform="rotate(-90 60 60)" />) : null}<text x="60" y="57" textAnchor="middle">{totalJobs ? `${percentage}%` : '—'}</text><text className="donut-caption" x="60" y="76" textAnchor="middle">{totalJobs ? '完成占比' : '暂无任务'}</text></svg><ul className="chart-legend">{[['已完成', delivery.jobs_completed, '#779888'], ['失败', delivery.jobs_failed, '#cb8875'], ['待人工', delivery.jobs_waiting_human, '#e6c634']].map(([label, count, color]) => <li key={label}><span><i style={{ backgroundColor: String(color) }} />{label}</span><strong>{count}</strong></li>)}</ul></div><p className="chart-footnote">占比不含排队、执行中的任务<br />平台动作已完成 {delivery.automation_completed} · 结果不明 {delivery.automation_uncertain}</p></section>
      </div>
    </> : null}
  </div>
}

function ProfileRefreshPanel({ accountId }: { accountId: string }) {
  const profiles = useContext(AccountProfiles)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  async function refresh() {
    if (busy) return
    setBusy(true)
    setNotice('正在从平台读取资料…')
    try {
    const response = await fetch(
      `/api/v1/web/operations/accounts/${encodeURIComponent(accountId)}/refresh-profile`,
      {
        method: 'POST',
        headers: { 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
      },
    )
    if (!response.ok) {
      setNotice('读取失败：账号需要已完成扫码登录，且 Cookie 里有签名 token。')
      return
    }
    const payload = (await response.json()) as { data: { nickname: string | null } }
    setNotice(payload.data.nickname ? `已更新昵称：${payload.data.nickname}` : '平台响应里没有昵称，本地值未改动。')
    profiles.refresh()
    } catch { setNotice('资料读取失败，请检查连接后再处理。') } finally { setBusy(false) }
  }

  return (
    <div className="profile-refresh">
      <button type="button" disabled={busy} onClick={refresh}><Icon name="refresh" />{busy ? '读取中…' : '刷新平台资料'}</button>
      {notice ? <p role="status">{notice}</p> : null}
    </div>
  )
}

function AccountsPage() {
  const location = useLocation()
  const requestedAccount = new URLSearchParams(location.search).get('account')
  const { data, error, loading, reload } = useProtectedData<AccountStatus[]>('/api/v1/web/operations/accounts')
  const [commandResult, setCommandResult] = useState('')
  const [commandId, setCommandId] = useState('')
  const [busy, setBusy] = useState(false)
  const list = useListLocation()
  const query = list.read('accountQuery')
  const requestedFilter = list.read('accountState', 'all')
  const filter = ['all', 'online', 'attention'].includes(requestedFilter) ? requestedFilter : 'all'
  function setQuery(value: string) { list.update({ accountQuery: value }) }
  function setFilter(value: string) { list.update({ accountState: value === 'all' ? null : value }, false) }
  const actionLabels: Record<string, string> = { start: '启动', stop: '停止', restart: '重启' }
  const focusedAccount = data?.find(row => row.account_id === requestedAccount)?.account_id
  const visible = (data ?? []).filter(row => (!focusedAccount || row.account_id === focusedAccount) && row.account_id.toLowerCase().includes(query.toLowerCase()) && (filter !== 'online' || row.worker_state === 'online') && (filter !== 'attention' || !!row.risk_code || row.worker_state === 'error'))

  async function submitCommand(accountId: string, action: 'start' | 'stop' | 'restart') {
    if (busy || !window.confirm(`确认${actionLabels[action]}账号 ${accountId}？命令将提交给运行服务执行。`)) return
    setBusy(true)
    setCommandResult('正在提交命令…')
    try {
    const response = await fetch(`/api/v1/web/operations/accounts/${encodeURIComponent(accountId)}/commands`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      setCommandResult('命令未提交；请检查账号状态或重新登录。')
      return
    }
    const payload = (await response.json()) as { data: WorkerCommandStatus }
    setCommandId(payload.data.command_id)
    setCommandResult(`已提交${actionLabels[action]}命令，等待运行服务执行。`)
    reload()
    } catch { setCommandResult('命令提交结果无法确认，请核对运行状态，勿重复提交。') } finally { setBusy(false) }
  }

  async function refreshCommandStatus() {
    if (!commandId) return
    const response = await fetch(`/api/v1/web/operations/commands/${encodeURIComponent(commandId)}`)
    if (!response.ok) {
      setCommandResult('无法读取命令状态；请重新登录或刷新页面。')
      return
    }
    const payload = (await response.json()) as { data: WorkerCommandStatus }
    const states: Record<string, string> = { pending: '等待执行', claimed: '执行中', completed: '已完成', failed: '失败', uncertain: '结果不明' }
    setCommandResult(`${actionLabels[payload.data.action] ?? payload.data.action}命令：${states[payload.data.status] ?? payload.data.status}。`)
    reload()
  }

  return (
    <main className="business-page">
      <header className="business-heading"><div><h1>账号管理</h1><p>查看连接状态、处理账号异常，进入对应业务工作区。</p></div><button type="button" onClick={reload}><Icon name="refresh" />刷新状态</button></header>
      <div className="business-metrics"><div><span>账号总数</span><strong>{data?.length ?? '—'}</strong></div><div><span>当前在线</span><strong>{data?.filter(row => row.worker_state === 'online').length ?? '—'}</strong></div><div><span>需要关注</span><strong>{data?.filter(row => !!row.risk_code || row.worker_state === 'error').length ?? '—'}</strong></div></div>
      <div className="table-tools"><input aria-label="搜索账号" placeholder="搜索账号名称" value={query} onChange={e => setQuery(e.target.value)} /><select aria-label="筛选账号状态" value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部账号</option><option value="online">在线账号</option><option value="attention">需要关注</option></select>{query || filter !== 'all' ? <button type="button" onClick={() => list.update({ accountQuery: null, accountState: null }, false)}>清空筛选</button> : null}<span>显示 {visible.length} 个账号</span></div>
      {commandResult ? <p role="status">{commandResult} {commandId ? <button type="button" onClick={refreshCommandStatus}>刷新命令状态</button> : null}</p> : null}
      {focusedAccount ? <p className="scope-note">当前定位账号：{focusedAccount} · <NavLink to="/accounts">查看全部账号</NavLink></p> : null}
      <div className="account-cards">{visible.map(account => <section className="account-card" key={account.account_id}><header><div><AccountIdentity accountId={account.account_id} heading /><p>{account.enabled ? '已启用账号' : '账号配置已停用'} · 期望{account.desired_state === 'running' ? '运行' : accountLabel(account.desired_state)}</p></div><span className={`status-badge ${account.worker_state === 'online' ? 'success' : account.risk_code || account.worker_state === 'error' ? 'warning' : 'neutral'}`}>{accountLabel(account.worker_state)}</span></header><dl className="detail-grid"><div><dt>最近心跳</dt><dd>{localTime(account.last_heartbeat_at)}</dd></div><div><dt>重连次数</dt><dd>{account.reconnect_attempts}</dd></div><div><dt>风险状态</dt><dd>{account.risk_code ?? '无风险标记'}</dd></div></dl><div className="account-business-links"><NavLink to={`/messages?account=${encodeURIComponent(account.account_id)}`}><Icon name="message" />消息工作区</NavLink><NavLink to={`/items?account=${encodeURIComponent(account.account_id)}`}><Icon name="cube" />商品管理</NavLink><NavLink to={`/orders?account=${encodeURIComponent(account.account_id)}`}><Icon name="shopping-bag" />订单管理</NavLink></div><footer><div className="account-command-actions"><button className="primary-button" type="button" disabled={busy || !account.enabled} onClick={() => submitCommand(account.account_id, 'start')}>启动账号</button><button type="button" disabled={busy} onClick={() => submitCommand(account.account_id, 'stop')}>停止</button><button type="button" disabled={busy || !account.enabled} onClick={() => submitCommand(account.account_id, 'restart')}>重启</button></div><ProfileRefreshPanel accountId={account.account_id} /></footer><details className="account-diagnostics"><summary>连接诊断</summary><dl className="detail-grid"><div><dt>连接记录</dt><dd>{accountLabel(account.db_status)}</dd></div><div><dt>运行实例</dt><dd>{account.owner_instance_id ?? '未分配'}</dd></div><div><dt>租约有效期</dt><dd>{localTime(account.owner_lease_until)}</dd></div><div><dt>最近错误</dt><dd>{account.last_error === 'loop exited' ? '连接循环已退出' : account.last_error ?? '无'}</dd></div></dl></details></section>)}</div>
      {visible.length === 0 ? <PageState onRetry={reload} error={error} loading={loading} emptyText={query || filter !== 'all' ? '没有匹配的账号。' : '尚未添加账号。'} /> : null}
    </main>
  )
}

const runtimeStatusLabels: Record<string, string> = { starting: '启动中', running: '登记运行', stopping: '停止中', stopped: '已停止', error: '异常' }
function RuntimePage() {
  const { data, error, loading, reload } = useProtectedData<RuntimeInstance[]>('/api/v1/web/operations/runtime')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('')
  const [selectedId, setSelectedId] = useState('')
  const visible = (data ?? []).filter(row => `${row.instance_id} ${row.version} ${row.pid}`.toLowerCase().includes(query.toLowerCase()) && (!status || row.status === status))
  const selected = visible.find(row => row.instance_id === selectedId) ?? visible[0]
  return <main className="business-page">
    <header className="business-heading"><div><h1>运行状态</h1><p>查看服务登记、心跳和租约，再核对日志与风险记录。</p></div><button type="button" onClick={reload}><Icon name="refresh" />刷新实例</button></header>
    <PageTabs tabs={[{ id: 'instances', label: '服务实例', render: () => <>
      <div className="business-metrics"><div><span>已加载实例</span><strong>{data?.length ?? '—'}</strong></div><div><span>登记运行</span><strong>{data?.filter(row => row.status === 'running').length ?? '—'}</strong></div><div><span>记录有错误</span><strong>{data?.filter(row => !!row.last_error).length ?? '—'}</strong></div></div>
      <div className="table-tools"><input aria-label="搜索服务实例" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索实例 ID、版本或 PID" /><select aria-label="服务登记状态" value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(runtimeStatusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><span>显示 {visible.length} 条记录</span></div>
      <p className="scope-note">按启动时间读取最近 100 个实例；登记为运行不代表当前心跳与租约仍有效。</p>
      <div className="runtime-workspace"><section className="runtime-instance-list" aria-label="服务实例列表">{visible.length ? visible.map(row => <button className={`runtime-instance-row ${selected?.instance_id === row.instance_id ? 'selected' : ''}`} key={row.instance_id} type="button" aria-pressed={selected?.instance_id === row.instance_id} onClick={() => setSelectedId(row.instance_id)}><header><strong>{row.instance_id}</strong><span className={`status-badge ${row.status === 'error' ? 'warning' : 'neutral'}`}>{runtimeStatusLabels[row.status] ?? row.status}</span></header><p>版本 {row.version} · PID {row.pid}</p><div><span>最近心跳</span><time>{localTime(row.last_heartbeat_at)}</time></div></button>) : <PageState onRetry={reload} loading={loading} error={error} emptyText={query || status ? '没有匹配的服务实例。' : '尚无服务实例记录。'} />}</section>
      <section className="runtime-instance-detail" aria-label="服务实例详情">{selected ? <><div className="section-heading"><div><p className="eyebrow">当前实例</p><h2>{selected.instance_id}</h2></div></div><dl className="detail-grid"><div><dt>登记状态</dt><dd>{runtimeStatusLabels[selected.status] ?? selected.status}</dd></div><div><dt>版本 / PID</dt><dd>{selected.version} / {selected.pid}</dd></div><div><dt>启动时间</dt><dd>{localTime(selected.started_at)}</dd></div><div><dt>停止时间</dt><dd>{localTime(selected.stopped_at)}</dd></div><div><dt>最近心跳</dt><dd>{localTime(selected.last_heartbeat_at)}</dd></div><div><dt>租约有效期</dt><dd>{localTime(selected.lease_until)}</dd></div></dl><div className="runtime-error"><h3>最近错误</h3><p>{selected.last_error ?? '该记录没有错误信息。'}</p></div><NavLink className="text-action" to="/accounts">查看账号连接状态<Icon name="chevron-right" /></NavLink></> : <div className="empty-workspace"><Icon name="activity" /><p>选择服务实例，查看心跳、租约和错误。</p></div>}</section></div>
    </> }, { id: 'logs', label: '日志摘要', render: () => <LogSummaryPanel /> }, { id: 'risks', label: '风险事件', render: () => <RiskEventsPanel /> }]} />
  </main>
}

function LogSummaryPanel() {
  const { data, error, loading, reload } = useProtectedData<LogSummary>('/api/v1/web/operations/logs/summary?recent=20')
  return <section><div className="section-heading"><div><h2>日志摘要</h2><p>统计本机日志级别，展示最近 20 条脱敏告警；完整日志可通过 CLI 查看。</p></div><button type="button" onClick={reload}>刷新摘要</button></div>{data?.exists ? <><div className="business-metrics"><div><span>日志总行数</span><strong>{data.total_lines ?? 0}</strong></div><div><span>错误行</span><strong>{(data.counts.ERROR ?? 0) + (data.counts.CRITICAL ?? 0)}</strong></div><div><span>警告行</span><strong>{data.counts.WARNING ?? 0}</strong></div></div><div className="log-level-counts">{Object.entries(data.counts).map(([level, count]) => <span className="status-badge neutral" key={level}>{level} · {count}</span>)}</div><h3>最近告警</h3>{data.recent.length ? <pre className="runtime-log-lines">{data.recent.join('\n')}</pre> : <p className="empty-inline">最近没有告警行。</p>}<details className="runtime-log-location"><summary>查看日志位置与命令</summary><p><code>{data.path}</code></p><code>daemon logs</code></details></> : <PageState onRetry={reload} loading={loading} error={error} emptyText={data ? '本机尚无日志文件。' : '暂无日志摘要。'} />}</section>
}

function RiskEventsPanel() {
  const { data, error, loading, reload } = useProtectedData<RiskEventRow[]>('/api/v1/web/operations/risk-events?limit=50')
  const [query, setQuery] = useState('')
  const visible = (data ?? []).filter(row => `${row.target ?? ''} ${row.action} ${row.result}`.toLowerCase().includes(query.toLowerCase()))
  return <section><div className="section-heading"><div><h2>风险事件</h2><p>最近 50 条风控相关记录；历史事件不代表账号当前状态。</p></div><button type="button" onClick={reload}>刷新事件</button></div><div className="table-tools"><input aria-label="搜索风险事件" placeholder="搜索账号、动作或结果" value={query} onChange={event => setQuery(event.target.value)} /><NavLink className="text-action" to="/accounts">查看当前账号状态<Icon name="chevron-right" /></NavLink></div>{visible.length ? <div className="risk-event-cards">{visible.map(row => <article key={row.id}><header><strong>{row.target ?? '全局'}</strong><time>{localTime(row.created_at)}</time></header><p>{row.action}</p><div><span className="status-badge neutral">{row.result}</span>{row.target ? <NavLink to={`/accounts?account=${encodeURIComponent(row.target)}`}>查看账号</NavLink> : null}</div></article>)}</div> : <PageState onRetry={reload} loading={loading} error={error} emptyText={query ? '没有匹配的风险事件。' : '还没有风险事件记录。'} />}<p className="field-help">账号恢复仍通过人工验证与现有 CLI 流程完成。</p></section>
}

function ConversationThread({ messages, loading, error, onRetry }: { messages: MirroredMessage[] | null; loading: boolean; error: string | null; onRetry: () => void }) {
  const threadRef = useRef<HTMLDivElement>(null)
  useEffect(() => { if (threadRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight }, [messages])
  if (!messages?.length) return <div className="thread-empty"><PageState onRetry={onRetry} error={error} loading={loading} emptyText="这个会话暂无本地消息镜像。" /></div>
  return <div ref={threadRef} className="message-thread" aria-label="当前会话消息">{[...messages].reverse().map(message => <article key={message.id} className={`chat-message ${message.direction === 'outbound' ? 'outgoing' : 'incoming'}`}><div className="chat-message-meta">{message.direction === 'outbound' ? '卖家' : message.sender ?? '买家'} · {localTime(message.received_at)}</div><div className="chat-bubble">{message.content ?? `[${message.content_type}]`}{message.image_url ? <MediaImage key={message.image_url} url={message.image_url} label="消息图片" className="message-picture" preview /> : null}</div></article>)}</div>
}

function ConversationsList({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<ConversationSummary[]>(`/api/v1/web/operations/conversations?account_id=${encodeURIComponent(accountId)}`)
  const [notice, setNotice] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [selectedChat, setSelectedChat] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [busy, setBusy] = useState(false)
  const selected = data?.find(row => row.chat_id === selectedChat) ?? data?.[0]
  const recent = useProtectedData<MirroredMessage[]>(`/api/v1/web/operations/messages?account_id=${encodeURIComponent(accountId)}&limit=100`)
  const history = useProtectedData<MirroredMessage[]>(selected ? `/api/v1/web/operations/messages?account_id=${encodeURIComponent(accountId)}&chat_id=${encodeURIComponent(selected.chat_id)}&limit=100` : null)
  const latest = (chatId: string) => recent.data?.find(row => row.chat_id === chatId)
  const buyerName = (chatId: string) => recent.data?.find(row => row.chat_id === chatId && row.direction === 'inbound')?.sender
  const visible = (data ?? []).filter(row => (filter !== 'unread' || row.unread_count > 0) && (filter !== 'human' || row.takeover_state === 'human') && `${row.chat_id} ${row.buyer_id ?? ''} ${row.item_id ?? ''} ${buyerName(row.chat_id) ?? ''} ${latest(row.chat_id)?.content ?? ''}`.toLowerCase().includes(query.toLowerCase()))
  async function changeOwner(chatId: string, state: 'human' | 'bot') {
    const label = state === 'human' ? '人工接管' : '恢复自动回复'
    if (busy) return
    if (!window.confirm(`确认对会话 ${chatId} 执行「${label}」？`)) return
    setBusy(true)
    try {
    const response = await fetch(
      `/api/v1/web/operations/conversations/${encodeURIComponent(chatId)}/takeover?account_id=${encodeURIComponent(accountId)}`,
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
        },
        body: JSON.stringify({ state }),
      },
    )
    setNotice(response.ok ? `会话 ${chatId} 已切换为 ${state === 'human' ? '人工接管' : '自动回复'}。` : '切换失败；请重新登录或检查会话。')
    if (response.ok) reload()
    } catch { setNotice('无法确认接管结果，请刷新会话核对。') } finally { setBusy(false) }
  }

  async function sendReply(chatId: string) {
    const text = (drafts[chatId] ?? '').trim()
    if (!text) {
      setNotice('请输入回复内容。')
      return
    }
    if (busy) return
    if (!window.confirm(`确认向会话 ${chatId} 发送这条回复？这是真实外发动作。`)) return
    setBusy(true)
    try {
    const response = await fetch(
      `/api/v1/web/operations/conversations/${encodeURIComponent(chatId)}/reply?account_id=${encodeURIComponent(accountId)}`,
      {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
        },
        body: JSON.stringify({ text, confirm: true }),
      },
    )
    if (!response.ok) {
      setNotice('回复提交未成功；请检查账号运行状态和回复记录，核对结果后再处理。')
      return
    }
    setDrafts((current) => ({ ...current, [chatId]: '' }))
    setNotice(`会话 ${chatId} 的回复已入队，等待 daemon 外发。`)
    history.reload()
    } catch { setNotice('回复提交结果无法确认，请查看回复记录核对，勿重复发送。') } finally { setBusy(false) }
  }

  return <div className="message-workspace">
    <aside className="conversation-rail"><div className="conversation-tools"><input aria-label="搜索当前会话列表" placeholder="搜索买家、会话或消息摘要" value={query} onChange={e => setQuery(e.target.value)} /><div className="conversation-filters">{[['all','全部'],['unread','未读'],['human','人工接管']].map(([value,label]) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}<button aria-label="刷新会话与消息" onClick={() => { reload(); recent.reload(); history.reload() }}><Icon name="refresh" /></button></div></div><div className="conversation-list">{visible.map(row => <button key={row.chat_id} className={`conversation-row ${selected?.chat_id === row.chat_id ? 'selected' : ''}`} onClick={() => { setSelectedChat(row.chat_id); setNotice('') }}><div className="conversation-row-title"><strong>{buyerName(row.chat_id) ?? row.buyer_id ?? row.chat_id}</strong>{row.unread_count > 0 ? <span className="count-badge">{row.unread_count}</span> : null}</div><p>{latest(row.chat_id)?.content ?? '暂无近期消息摘要'}</p><small>{row.item_id ? `商品 ${row.item_id}` : '普通会话'} · {row.takeover_state === 'human' ? '人工接管' : '机器人管理'}</small></button>)}{visible.length === 0 ? <PageState onRetry={reload} error={error} loading={loading} emptyText={query || filter !== 'all' ? '没有匹配的会话。' : '暂无会话。'} /> : null}</div><p className="list-window-note">显示最近 100 个会话；搜索当前列表。</p></aside>
    <div className="conversation-detail">{selected ? <><header className="conversation-header"><div><h2>{buyerName(selected.chat_id) ?? selected.buyer_id ?? '买家会话'}</h2><p>会话 {selected.chat_id}{selected.item_id ? ` · 商品 ${selected.item_id}` : ''}</p></div><button disabled={busy} onClick={() => changeOwner(selected.chat_id, selected.takeover_state === 'human' ? 'bot' : 'human')}>{selected.takeover_state === 'human' ? '恢复自动回复' : '人工接管'}</button></header><div className="conversation-status"><span className={`status-badge ${selected.takeover_state === 'human' ? 'warning' : 'neutral'}`}>{selected.takeover_state === 'human' ? '人工接管中' : '机器人管理'}</span><span>消息为本地镜像，打开会话不会上报平台已读。</span></div>{notice ? <p role="status">{notice}</p> : null}<ConversationThread onRetry={history.reload} messages={history.data} loading={history.loading} error={history.error} /><form className="reply-composer" onSubmit={e => { e.preventDefault(); void sendReply(selected.chat_id) }}><textarea aria-label="回复当前会话" placeholder={selected.takeover_state === 'human' ? '输入回复，发送前需要确认' : '先人工接管，再发送回复'} disabled={selected.takeover_state !== 'human' || busy} value={drafts[selected.chat_id] ?? ''} onChange={e => setDrafts(current => ({ ...current, [selected.chat_id]: e.target.value }))} /><footer><span>仅在账号 Worker 在线时可发送；最近 100 条消息。</span><button className="primary-button" disabled={selected.takeover_state !== 'human' || busy || !(drafts[selected.chat_id] ?? '').trim()}>{busy ? '提交中…' : '发送回复'}</button></footer></form></> : <div className="thread-empty"><Icon name="message" /><p>选择一个会话，查看消息并处理买家咨询。</p></div>}</div>
  </div>
}

function ItemsList({ accountId, selectedItemId, onSelect, onConfigure }: { accountId: string; selectedItemId: string; onSelect: (itemId: string) => void; onConfigure: (itemId: string) => void }) {
  const { data, error, loading, reload } = useProtectedData<MirroredItem[]>(`/api/v1/web/operations/items?account_id=${encodeURIComponent(accountId)}&include_off_sale=true&limit=100`)
  const policies = useProtectedData<DeliveryPolicyRow[]>(`/api/v1/web/operations/delivery/policies?account_id=${encodeURIComponent(accountId)}`)
  const pools = useProtectedData<DeliveryPoolRow[]>(`/api/v1/web/operations/delivery/pools?account_id=${encodeURIComponent(accountId)}`)
  const automation = useProtectedData<ItemAutomationRow[]>(`/api/v1/web/operations/items/automation?account_id=${encodeURIComponent(accountId)}`)
  const list = useListLocation(accountId)
  const query = list.read('itemQuery')
  const requestedStatus = list.read('itemState', 'on-sale')
  const status = ['on-sale', 'off-sale', 'all'].includes(requestedStatus) ? requestedStatus : 'on-sale'
  function setQuery(value: string) { list.update({ itemQuery: value, item: null }) }
  function setStatus(value: string) { list.update({ itemState: value === 'on-sale' ? null : value, item: null }, false) }
  const visible = (data ?? []).filter(row => `${row.title} ${row.item_id}`.toLowerCase().includes(query.toLowerCase()) && (status === 'all' || row.is_on_sale === (status === 'on-sale')))
  const selected = visible.find(row => row.item_id === selectedItemId) ?? visible[0]
  const selectedOverride = automation.data?.find(row => row.item_id === selected?.item_id)
  const related = policies.data?.filter(row => !row.item_id || row.item_id === selected?.item_id) ?? []
  const modeLabels: Record<string, string> = { exclude: '已排除生命周期计划', keep: '人工强制保留' }
  const refresh = () => { reload(); policies.reload(); pools.reload(); automation.reload() }
  return <>
    <div className="business-metrics"><div><span>已加载商品</span><strong>{data?.length ?? '—'}</strong></div><div><span>在售商品</span><strong>{data?.filter(row => row.is_on_sale).length ?? '—'}</strong></div><div><span>下架商品</span><strong>{data?.filter(row => !row.is_on_sale).length ?? '—'}</strong></div></div>
    <div className="table-tools"><input aria-label="搜索商品" placeholder="搜索商品标题或商品 ID" value={query} onChange={e => setQuery(e.target.value)} /><select aria-label="筛选商品状态" value={status} onChange={e => setStatus(e.target.value)}><option value="on-sale">在售商品</option><option value="off-sale">下架商品</option><option value="all">全部商品</option></select>{query || status !== 'on-sale' ? <button type="button" onClick={() => list.update({ itemQuery: null, itemState: null, item: null }, false)}>清空筛选</button> : null}<span>最近 100 条镜像 · 匹配 {visible.length} 件</span><button type="button" onClick={refresh}><Icon name="refresh" />刷新镜像</button></div>
    <div className="product-workspace"><section className="product-list" aria-label="商品列表">{visible.map(item => <button className={`product-row ${selected?.item_id === item.item_id ? 'selected' : ''}`} key={item.id} type="button" aria-pressed={selected?.item_id === item.item_id} onClick={() => onSelect(item.item_id)}><div className="product-row-picture"><MediaImage url={item.main_image_url || item.images?.[0]} label={item.title} className="product-thumbnail" /><div className="product-row-heading"><strong>{item.title}</strong><span className="product-price">{item.price ? `¥ ${item.price}` : '价格未知'}</span></div></div><p>商品 ID {item.item_id}</p><div className="product-row-meta"><span className={`status-badge ${item.is_on_sale ? 'success' : 'neutral'}`}>{item.is_on_sale ? '在售' : '已下架'}</span><span>已售 {item.sold_count}</span><span>{item.has_multi_spec ? '多规格' : '普通商品'}</span></div></button>)}{visible.length === 0 ? <div className="empty-workspace"><Icon name="cube" /><PageState onRetry={reload} error={error} loading={loading} emptyText={query || status !== 'all' ? '没有匹配的商品。' : '暂无商品镜像。'} /></div> : null}</section>
    <section className="product-detail" aria-label="选中商品配置">{selected ? <><header><div><p className="eyebrow">当前商品</p><h2>{selected.title}</h2><p className="row-description">商品 ID {selected.item_id}</p></div><span className="product-price">{selected.price ? `¥ ${selected.price}` : '价格未知'}</span></header><ItemGallery key={`${accountId}:${selected.item_id}`} accountId={accountId} item={selected} /><dl className="detail-grid"><div><dt>销售状态</dt><dd>{selected.is_on_sale ? '在售' : '已下架'}</dd></div><div><dt>已售数量</dt><dd>{selected.sold_count}</dd></div><div><dt>规格模式</dt><dd>{selected.has_multi_spec ? '多规格' : '普通商品'}</dd></div><div><dt>最近同步</dt><dd>{localTime(selected.last_synced_at)}</dd></div></dl><div className="product-config-block"><header><h3>商品自动化</h3><button type="button" onClick={() => onConfigure(selected.item_id)}>设置自动化</button></header>{automation.loading || automation.error ? <PageState onRetry={automation.reload} loading={automation.loading} error={automation.error} emptyText="" /> : <><p>{selectedOverride?.mode ? modeLabels[selectedOverride.mode] ?? '按自动策略判断' : '按自动策略判断'}</p>{selectedOverride?.note ? <p className="row-description">设置原因：{selectedOverride.note}</p> : null}</>}</div><div className="product-config-block"><header><h3>关联发货策略</h3><NavLink className="text-action" to={`/delivery?account=${encodeURIComponent(accountId)}&tab=sources&item=${encodeURIComponent(selected.item_id)}`}>管理策略<Icon name="chevron-right" /></NavLink></header><p className="row-description">显示当前账号的专属与账号级策略；实际采用的策略以发货预演为准。</p>{policies.loading || policies.error ? <PageState onRetry={policies.reload} loading={policies.loading} error={policies.error} emptyText="" /> : related.length ? <ul className="related-policies">{related.map(policy => <li key={policy.id}><div><strong>{policy.name}</strong><p>{policy.item_id ? '商品专属' : '账号级'} · {policy.sku_id ? `规格 ${policy.sku_id}` : '全部规格'}</p><p>{pools.data?.find(pool => pool.id === policy.inventory_pool_id)?.name ?? `库存池 #${policy.inventory_pool_id}`}</p></div><span className={`status-badge ${policy.enabled ? 'success' : 'neutral'}`}>{policy.enabled ? '已启用' : '已停用'}</span></li>)}</ul> : <p className="empty-inline">当前账号没有关联此商品的策略。全局策略需到发货管理核对。</p>}{pools.error ? <p role="alert">库存池信息读取失败，当前仅显示库存池编号。</p> : null}</div><p className="list-window-note">这是本地镜像；刷新页面数据不会从平台重新同步。</p></> : <div className="empty-workspace"><Icon name="cube" /><p>选择一个商品，查看信息和相关配置。</p></div>}</section></div>
  </>
}

const orderStatusLabels: Record<string, string> = { pending: '待处理', paid: '已付款', shipped: '已发货', delivered: '已交付', completed: '已完成', cancelled: '已取消', refunded: '已退款', failed: '交付失败' }
const deliveryJobLabels: Record<string, string> = { pending: '待处理', checking: '检查中', needs_confirmation: '待确认', reserving: '预留中', reserved: '已预留', delivering: '发送中', confirming: '确认中', completed: '已完成', blocked: '已阻止', failed_retryable: '发送前失败', failed_final: '已失败', uncertain: '结果不明', cancelled: '已取消' }
function jobTone(state: string) { return state === 'completed' ? 'success' : ['uncertain', 'needs_confirmation', 'blocked', 'failed_final', 'failed_retryable'].includes(state) ? 'warning' : 'neutral' }

function orderValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '平台未返回'
  if (typeof value === 'boolean') return value ? '是' : '否'
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '平台未返回'
}
function OrderFields({ fields }: { fields: [string, unknown][] }) {
  return <dl className="detail-grid">{fields.map(([label, value], index) => <div className={['收货地址', '发货提示', '退款原因', '卖家备注'].includes(label) ? 'order-field-wide' : undefined} key={`${index}:${label}`}><dt>{label}</dt><dd>{orderValue(value)}</dd></div>)}</dl>
}
function ItemGallery({ accountId, item }: { accountId: string; item: MirroredItem }) {
  const pictures = useProtectedData<ItemPictures>(`/api/v1/web/operations/items/${encodeURIComponent(accountId)}/${encodeURIComponent(item.item_id)}/images?platform=true`)
  const data = pictures.data
  const urls = [...new Set([data?.main_image_url || item.main_image_url, ...(data?.images ?? item.images ?? []), ...(data?.platform_images ?? [])].filter((url): url is string => !!safeImageUrl(url)))]
  const variants = data?.variant_images ?? []
  return <div className="product-gallery" aria-label="商品图片"><header><h3>商品图片 <span>{pictures.loading ? '读取中' : `${urls.length} 张`}{variants.length ? ` · 规格图 ${variants.length} 张` : ''}</span></h3><button type="button" disabled={pictures.loading} onClick={pictures.reload}>刷新图片</button></header>{urls.length ? <div className="product-picture-grid">{urls.map((url, index) => <MediaImage key={url} url={url} label={`商品图片 ${index + 1}`} className="product-picture" preview />)}</div> : <p className="empty-inline">本地镜像暂无商品图片。</p>}{variants.length ? <div className="variant-picture-grid">{variants.map((variant, index) => <figure key={`${index}:${variant.url}`}><MediaImage url={variant.url} label={variant.label || `规格图 ${index + 1}`} className="product-picture" preview /><figcaption>{variant.label || `规格 ${index + 1}`}</figcaption></figure>)}</div> : null}{pictures.error ? <p role="status">详情图片读取失败，当前显示列表中已有图片。<button type="button" onClick={pictures.reload}>重试读取</button></p> : null}{data?.platform_error ? <p role="status">{data.platform_error}<button type="button" onClick={pictures.reload}>重试平台图库</button></p> : null}<p className="row-description">点击图片查看原图 · 包含已有主图、平台详情图和本地规格图</p></div>
}
function OrderImage({ url, label }: { url: string | null | undefined; label: string }) {
  if (!safeImageUrl(url)) return null
  return <MediaImage url={url} label={label} className="order-platform-image" preview />
}
const refundFieldLabels: Record<string, string> = { refundId: '退款单号', refundStatus: '退款状态', status: '状态', reason: '退款原因', refundFee: '退款金额', applyTime: '申请时间', updateTime: '更新时间', sellerRemark: '卖家备注' }
function RefundFields({ data }: { data: RefundInfo }) {
  const fields = Object.entries(refundFieldLabels).filter(([key]) => Object.hasOwn(data, key)).map(([key, label]) => [label, data[key]] as [string, unknown])
  const records = ['items', 'refundList', 'records'].flatMap(key => Array.isArray(data[key]) ? data[key] as Record<string, string | number | boolean | null>[] : [])
  return <>{fields.length ? <OrderFields fields={fields} /> : null}{records.map((record, index) => <div className="order-refund-record" key={index}><h4>记录 {index + 1}</h4><OrderFields fields={Object.entries(record).filter(([key]) => Object.hasOwn(refundFieldLabels, key)).map(([key, value]) => [refundFieldLabels[key], value])} /></div>)}{!fields.length && !records.length ? <p className="empty-inline">平台未返回退款记录。</p> : null}</>
}
function PlatformOrderDetails({ accountId, orderId }: { accountId: string; orderId: string }) {
  const { data, error, loading, reload } = useProtectedData<OrderDetails>(`/api/v1/web/operations/orders/${encodeURIComponent(orderId)}/details?account_id=${encodeURIComponent(accountId)}`)
  const order = data?.order.data, consign = data?.consign.data, logistics = data?.logistics.data
  const failed = data ? Object.values(data).some(result => result.error) : false
  const readError = (message: string | null | undefined) => message ? <p className="order-read-error" role="status">{message}</p> : null
  const packages = logistics?.packages ?? (logistics?.mail_no ? [logistics] : [])
  return <div className="order-platform-details" aria-label="平台订单信息" aria-busy={loading}>
    <header><div><h3>平台订单信息</h3><p>选中订单后实时读取，保留平台脱敏内容。</p></div><button type="button" disabled={loading} onClick={reload}><Icon name="refresh" />{loading ? '读取中' : '刷新平台信息'}</button></header>
    {loading ? <p className="empty-inline" role="status">正在读取收货、规格、物流与退款信息…</p> : null}
    {error ? <PageState loading={false} error={error} emptyText="" onRetry={reload} /> : null}
    {data ? <>
      {failed ? <p className="order-read-error">部分平台信息未能读取，已显示其他成功获取的信息。</p> : null}
      <div className="order-info-block"><h4>买家与收货信息</h4>{readError(data.order.error)}
        <div className="order-platform-buyer"><OrderImage key={order?.buyer_avatar} url={order?.buyer_avatar} label="买家头像" /><div><strong>{order?.buyer_nickname || '平台未返回买家昵称'}</strong><p>买家 ID {orderValue(order?.buyer_id)}</p></div></div>
        <OrderFields fields={[
          ['收件人', order?.receiver_name || consign?.buyer_name], ['联系电话', order?.receiver_phone || consign?.buyer_phone],
          ['收货地址', order?.receiver_address || consign?.buyer_address], ['平台隐私保护', order?.is_privacy], ['买家申请修改地址', order?.address_change_requested],
        ]} />
      </div>
      <div className="order-info-block"><h4>商品与交易</h4>
        <div className="order-platform-buyer"><OrderImage key={order?.item_image} url={order?.item_image} label="订单商品图片" /><div><strong>{orderValue(order?.item_title)}</strong><p>商品 ID {orderValue(order?.item_id)}</p></div></div>
        <OrderFields fields={[
          ['平台订单状态', order?.status_text], ['购买数量', order?.quantity], ['成交金额（元）', order?.amount], ['商品单价（元）', order?.unit_price],
          ['运费（元）', order?.post_fee], ['优惠金额（元）', order?.discount_fee], ['卖家实收（元）', order?.received_fee], ['退款金额（元）', order?.refund_fee],
          ['平台下单时间', order?.created_at], ['平台付款时间', order?.paid_at], ['平台发货时间', order?.consigned_at], ['平台完成时间', order?.finished_at],
          ['卖家评价状态（平台原值）', order?.seller_rate_status], ['退款中', order?.in_refund], ['平台退款状态', order?.refund_status], ['发货提示', order?.consign_tips],
          ...(order?.item_info ?? []).map(row => [row.name, row.value] as [string, unknown]),
        ]} />
      </div>
      <div className="order-info-block"><h4>规格与发货要求</h4>{readError(data.consign.error)}{consign ? <>
        <OrderFields fields={(consign.specifications?.length ? consign.specifications : consign.spec_name ? [{ name: consign.spec_name, value: consign.spec_value }] : [{ name: '商品规格', value: null }]).map(row => [row.name, row.value])} />
        <OrderFields fields={[
          ['发货页数量', consign.quantity], ['需要序列号', consign.need_serial_number], ['需要上传报告', consign.need_upload_report],
          ['支持的发货方式', consign.support_consign_type?.join('、')],
        ]} />
      </> : null}</div>
      <div className="order-info-block"><h4>物流轨迹</h4>{readError(data.logistics.error)}{logistics ? <>
        <p className="empty-inline">{logistics.package_count ? `${logistics.package_count} 个包裹` : '平台未返回物流包裹。'}</p>
        {packages.map((parcel, index) => <div className="order-parcel" key={index}><OrderFields fields={[
          ['包裹', index + 1], ['物流单号', parcel.mail_no], ['物流状态', parcel.status],
        ]} /><ol className="order-logistics-nodes">{parcel.nodes.map((node, i) => <li key={i}><time>{node.time || '时间未返回'}</time><p>{node.desc || node.status_desc || '节点描述未返回'}</p>{node.action ? <small>{node.action}</small> : null}</li>)}</ol></div>)}
      </> : null}</div>
      <div className="order-info-block"><h4>退款记录</h4>{readError(data.refund.error)}{data.refund.data ? <RefundFields data={data.refund.data} /> : null}</div>
    </> : null}
  </div>
}

function OrdersList({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<MirroredOrder[]>(`/api/v1/web/operations/orders?account_id=${encodeURIComponent(accountId)}&limit=100`)
  const jobs = useProtectedData<DeliveryJobRow[]>(`/api/v1/web/operations/delivery/jobs?account_id=${encodeURIComponent(accountId)}&limit=200`)
  const list = useListLocation(accountId)
  const query = list.read('orderQuery')
  const status = list.read('orderState')
  const selectedId = list.read('order')
  const [pane, setPane] = useState<'list' | 'detail'>('list')
  const detailRef = useRef<HTMLElement>(null)
  function setQuery(value: string) { setPane('list'); list.update({ orderQuery: value, order: null }) }
  function setStatus(value: string) { setPane('list'); list.update({ orderState: value, order: null }, false) }
  function setSelectedId(value: string) { setPane('detail'); list.update({ order: value }) }
  const statusOptions = Array.from(new Set([...(data ?? []).map(row => row.status), ...(status ? [status] : [])]))
  const visible = (data ?? []).filter(row => (!status || row.status === status) && `${row.order_id} ${row.item_title ?? ''} ${row.buyer_name ?? ''} ${row.buyer_id === 'unknown' ? '' : row.buyer_id ?? ''}`.toLowerCase().includes(query.toLowerCase()))
  const selected = visible.find(row => row.order_id === selectedId) ?? visible[0]
  const relatedJobs = (jobs.data ?? []).filter(row => row.order_id === selected?.order_id)
  useEffect(() => { detailRef.current?.scrollTo({ top: 0 }) }, [selected?.order_id])
  useEffect(() => {
    if (pane === 'detail' && window.matchMedia('(max-width: 900px)').matches) detailRef.current?.focus({ preventScroll: true })
  }, [pane])
  return <>
    <div className="business-metrics"><div><span>已加载订单</span><strong>{data?.length ?? '—'}</strong></div><div><span>已付款订单</span><strong>{data?.filter(row => row.status === 'paid').length ?? '—'}</strong></div><div><span>结果不明（近 200 任务）</span><strong>{jobs.data?.filter(row => row.state === 'uncertain').length ?? '—'}</strong></div></div>
    <div className="table-tools"><input aria-label="搜索当前订单列表" placeholder="搜索订单、商品或买家" title="支持订单号、商品标题、买家昵称和买家 ID" value={query} onChange={e => setQuery(e.target.value)} /><select aria-label="筛选订单状态" value={status} onChange={e => setStatus(e.target.value)}><option value="">全部状态</option>{statusOptions.map(value => <option key={value} value={value}>{orderStatusLabels[value] ?? value}</option>)}</select>{query || status ? <button type="button" onClick={() => list.update({ orderQuery: null, orderState: null, order: null }, false)}>清空筛选</button> : null}<span>最近 100 条订单 · 匹配 {visible.length} 条</span><button type="button" onClick={() => { reload(); jobs.reload() }}>刷新本地记录</button></div>
    <div className="order-pane-switch" aria-label="订单视图"><button type="button" aria-pressed={pane === 'list'} aria-controls="order-list-pane" onClick={() => setPane('list')}>订单列表</button><button type="button" aria-pressed={pane === 'detail'} aria-controls="order-detail-pane" onClick={() => setPane('detail')}>订单详情</button></div>
    <div className="order-workspace" data-pane={pane}>
      <section id="order-list-pane" className="order-list" tabIndex={0} aria-label="订单列表">{visible.length ? visible.map(row => <button className={`order-row ${selected?.order_id === row.order_id ? 'selected' : ''}`} key={row.id} type="button" aria-label={`查看订单 ${row.order_id}`} aria-pressed={selected?.order_id === row.order_id} onClick={() => setSelectedId(row.order_id)}><div className="product-row-heading"><strong>{row.item_title ?? '未同步商品标题'}</strong><span className="product-price">¥ {row.amount.toFixed(2)}</span></div><p className="order-number">{row.order_id}</p><p className="order-buyer">买家：{row.buyer_name || '未同步昵称'}</p><div className="product-row-meta"><span className={`status-badge ${row.status === 'paid' ? 'warning' : 'neutral'}`}>{orderStatusLabels[row.status] ?? row.status}</span><span>数量 {row.quantity}</span><time>{localTime(row.placed_at)}</time><Icon name="chevron-right" /></div></button>) : <PageState onRetry={reload} error={error} loading={loading} emptyText={query || status ? '没有匹配的订单。' : '该账号尚无订单镜像。'} />}</section>
      <section id="order-detail-pane" ref={detailRef} className="order-detail" tabIndex={0} aria-label="订单详情">{selected ? <>
        <header><div><p className="eyebrow">订单详情 · 本地镜像</p><h2>{selected.item_title ?? '未同步商品标题'}</h2><p className="order-number">{selected.order_id}</p></div><span className="status-badge neutral">{orderStatusLabels[selected.status] ?? selected.status}</span></header>
        <div className="order-amount"><strong>¥ {selected.amount.toFixed(2)}</strong><span>订单金额 · {selected.quantity} 件</span></div>
        <dl className="detail-grid"><div><dt>买家昵称</dt><dd>{selected.buyer_name || '未同步买家昵称'}</dd></div><div><dt>买家 ID</dt><dd>{selected.buyer_id && selected.buyer_id !== 'unknown' ? selected.buyer_id : '未同步买家 ID'}</dd></div><div><dt>所属账号</dt><dd>{accountId}</dd></div><div><dt>下单时间</dt><dd>{localTime(selected.placed_at)}</dd></div><div><dt>付款时间</dt><dd>{localTime(selected.paid_at)}</dd></div><div><dt>镜像交付时间</dt><dd>{localTime(selected.delivered_at)}</dd></div></dl>
        <PlatformOrderDetails key={`${accountId}:${selected.order_id}`} accountId={accountId} orderId={selected.order_id} />
        <div className="product-config-block"><header><h3>本地发货任务</h3><button type="button" onClick={jobs.reload}>刷新任务</button></header><p>在该账号最近 200 条任务中查询；平台订单状态与本地任务分别展示。</p>{relatedJobs.length ? <ul className="related-policies">{relatedJobs.map(job => <li key={job.id}><div><strong>任务 #{job.id} · 数量 {job.required_quantity}</strong><p>{localTime(job.created_at)}</p>{job.last_error ? <p>{job.last_error}</p> : null}</div><span className={`status-badge ${jobTone(job.state)}`}>{deliveryJobLabels[job.state] ?? job.state}</span></li>)}</ul> : <PageState onRetry={jobs.reload} loading={jobs.loading} error={jobs.error} emptyText="当前任务窗口中没有该订单的发货记录。" />}</div>
        <NavLink className="primary-button" to={`/delivery?account=${encodeURIComponent(accountId)}&tab=tasks&order=${encodeURIComponent(selected.order_id)}`}>进入发货工作区<Icon name="chevron-right" /></NavLink><p className="row-description">先预演，确认策略与库存，再决定是否发货。</p>
      </> : <div className="empty-workspace"><Icon name="shopping-bag" /><p>选择订单后查看详情与发货记录。</p></div>}</section>
    </div>
  </>
}

function DeliveriesList({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<MirroredOrder[]>(`/api/v1/web/operations/orders?account_id=${encodeURIComponent(accountId)}&limit=100`)
  return <section><div className="section-heading"><div><h2>订单交付镜像</h2><p>最近 100 条订单的本地状态；发货执行结果请查看发货工作区的任务记录。</p></div><button type="button" onClick={reload}>刷新本地记录</button></div>{data && data.length > 0 ? <div className="delivery-table-scroll"><table><thead><tr><th>订单与商品</th><th>订单状态</th><th>镜像交付状态</th><th>付款时间</th><th>镜像交付时间</th></tr></thead><tbody>{data.map(order => <tr key={order.id}><td><strong>{order.item_title ?? '未同步商品标题'}</strong><p className="row-description">{order.order_id}</p></td><td>{orderStatusLabels[order.status] ?? order.status}</td><td>{orderStatusLabels[order.delivery_state] ?? order.delivery_state}</td><td>{localTime(order.paid_at)}</td><td>{localTime(order.delivered_at)}</td></tr>)}</tbody></table></div> : <PageState onRetry={reload} error={error} loading={loading} emptyText="该账号尚无订单交付记录。" />}</section>
}

function DeliveryJobsList({ accountId }: { accountId: string }) {
  const location = useLocation()
  const { data, error, loading, reload } = useProtectedData<DeliveryJobRow[]>(`/api/v1/web/operations/delivery/jobs?account_id=${encodeURIComponent(accountId)}&limit=200`)
  const [orderId, setOrderId] = useState(() => { const params = new URLSearchParams(location.search); return params.get('account') === accountId ? params.get('order') ?? '' : '' })
  const [preview, setPreview] = useState<{ orderId: string; plan: DeliveryPlanResult } | null>(null)
  const [planError, setPlanError] = useState('')
  const [runNotice, setRunNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [query, setQuery] = useState('')
  const [state, setState] = useState('')
  const plan = preview?.orderId === orderId.trim() ? preview.plan : null
  const visible = (data ?? []).filter(job => (!state || job.state === state) && `${job.id} ${job.order_id} ${job.item_id ?? ''}`.includes(query.trim()))

  function changeOrder(value: string) { setOrderId(value); setPreview(null); setPlanError(''); setRunNotice('') }
  async function perform(action: () => Promise<void>) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    try { await action() } catch { setPreview(null); setRunNotice('请求未取得明确结果，请先核对任务记录，避免重复提交。') }
    finally { busyRef.current = false; setBusy(false) }
  }
  async function dryRun() {
    const target = orderId.trim()
    setPreview(null)
    setRunNotice('')
    if (!target) { setPlanError('请输入订单号。'); return }
    setPlanError('')
    const response = await fetch('/api/v1/web/operations/delivery/plan', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ account_id: accountId, order_id: target }),
    })
    if (!response.ok) { setPlanError('预演失败，请确认订单属于该账号且管理员会话有效。'); return }
    const payload = (await response.json()) as { data: DeliveryPlanResult }
    setPreview({ orderId: target, plan: payload.data })
  }
  async function runDelivery() {
    const target = orderId.trim()
    if (!plan?.allowed || preview?.orderId !== target) { setPlanError('请先为当前订单完成发货预演。'); return }
    const typed = window.prompt(`真实发货会把库存内容发给买家且可能扣减库存。请输入订单号确认: ${target}`)
    if (typed !== target) { setRunNotice('订单号不匹配，已取消发货。'); return }
    setPreview(null)
    setRunNotice('正在提交发货命令…')
    const response = await fetch('/api/v1/web/operations/delivery/run', {
      method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
      body: JSON.stringify({ account_id: accountId, order_id: target, confirm: true }),
    })
    if (!response.ok) { setRunNotice('未取得发货命令的成功确认，请核对任务记录与账号运行状态。'); return }
    const payload = (await response.json()) as { data: { command_id: string } }
    setRunNotice(`命令 ${payload.data.command_id.slice(0, 8)} 已入队，等待执行；请刷新任务记录核对结果。`)
    reload()
  }
  async function resolveJob(job: DeliveryJobRow, delivered: boolean) {
    const note = window.prompt(delivered ? `任务 #${job.id}：确认平台已发出？请写下依据。` : `任务 #${job.id}：确认未发出？请写下依据。`)
    if (!note?.trim()) { setRunNotice('收尾必须写明依据，已取消。'); return }
    setPreview(null)
    const response = await fetch(`/api/v1/web/operations/delivery/jobs/${job.id}/resolve`, {
      method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
      body: JSON.stringify({ delivered, note: note.trim(), resolved_by: 'web' }),
    })
    if (!response.ok) { setRunNotice('收尾被拒绝，只有结果不明的任务能人工收尾。'); return }
    setRunNotice(`任务 #${job.id} 已按${delivered ? '已发出' : '未发出'}收尾，库存同时核销或退回。`)
    reload()
  }

  return <div className="delivery-workspace">
    <section className="delivery-preview"><div className="section-heading"><div><p className="eyebrow">订单发货</p><h2>先预演，再执行</h2><p>预演检查订单、策略和库存，不预留库存或发送内容。</p></div></div>
      <form className="settings-form" onSubmit={event => { event.preventDefault(); void perform(dryRun) }}><label>订单号<input aria-label="发货订单号" required value={orderId} disabled={busy} onChange={event => changeOrder(event.target.value)} placeholder="输入订单号，或从订单详情进入" /></label><div className="form-actions"><button className="primary-button" type="submit" disabled={busy}>{busy ? '处理中…' : '预演发货'}</button><button type="button" disabled={busy || !plan?.allowed} onClick={() => void perform(runDelivery)}>真实发货</button></div></form>
      {planError ? <p className="operation-feedback" role="alert">{planError}</p> : null}{runNotice ? <p className="operation-feedback" role="status">{runNotice}</p> : null}
      {plan ? <div className={`delivery-plan ${plan.allowed ? 'allowed' : 'denied'}`} role="status"><span className={`status-badge ${plan.allowed ? 'success' : 'warning'}`}>{plan.allowed ? '预演通过' : '已阻止发货'}</span><h3>{plan.reason}</h3><p className="order-number">订单 {preview?.orderId}</p><dl className="detail-grid"><div><dt>订单状态</dt><dd>{plan.order_status ? orderStatusLabels[plan.order_status] ?? plan.order_status : '未找到'}</dd></div><div><dt>发货策略</dt><dd>{plan.policy_name ?? '未返回策略'}</dd></div><div><dt>库存池 / 数量</dt><dd>{plan.pool_id ? `#${plan.pool_id} / ${plan.quantity}` : '—'}</dd></div><div><dt>延迟 / 平台确认</dt><dd>{plan.pool_id ? `${plan.delay_seconds}s / ${plan.platform_confirm ? '需要' : '不需要'}` : '未评估'}</dd></div></dl>{plan.hit_rules.length ? <p>命中规则：{plan.hit_rules.join('、')}</p> : null}<p>更换订单后需重新预演；实际执行仍会重新检查。</p></div> : <div className="preview-placeholder"><Icon name="truck" /><p>预演结果会显示在这里</p><span>订单状态 · 发货策略 · 库存与数量</span></div>}
    </section>
    <section className="delivery-history"><div className="section-heading"><div><h2>任务记录</h2><p>该账号最近 200 条任务，结果不明时需人工核对。</p></div><button type="button" onClick={reload}>刷新任务</button></div><div className="table-tools"><input aria-label="搜索发货任务" placeholder="搜索任务、订单或商品 ID" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="筛选发货任务状态" value={state} onChange={event => setState(event.target.value)}><option value="">全部状态</option>{Array.from(new Set((data ?? []).map(job => job.state))).map(value => <option key={value} value={value}>{deliveryJobLabels[value] ?? value}</option>)}</select></div>
      {visible.length ? <div className="delivery-job-cards">{visible.map(job => <article key={job.id}><header><strong>任务 #{job.id}</strong><span className={`status-badge ${jobTone(job.state)}`}>{deliveryJobLabels[job.state] ?? job.state}</span></header><button className="job-order-button" type="button" disabled={busy} onClick={() => changeOrder(job.order_id)}>{job.order_id}<Icon name="chevron-right" /></button><dl className="detail-grid"><div><dt>数量 / 库存预留</dt><dd>{job.required_quantity} / {job.reservation_id ? `#${job.reservation_id}` : '—'}</dd></div><div><dt>创建 / 完成时间</dt><dd>{localTime(job.created_at)}<br />{localTime(job.completed_at)}</dd></div></dl>{job.last_error ? <p className="operation-feedback">{job.last_error}</p> : null}{job.state === 'uncertain' ? <div className="uncertain-actions"><p>先核对平台记录，再选择结果。</p><button type="button" disabled={busy} onClick={() => void perform(() => resolveJob(job, true))}>确认已发出 · 核销</button><button type="button" disabled={busy} onClick={() => void perform(() => resolveJob(job, false))}>确认未发出 · 退回</button></div> : null}</article>)}</div> : <PageState onRetry={reload} error={error} loading={loading} emptyText={query || state ? '没有匹配的发货任务。' : '该账号尚无发货任务，预演不会创建任务。'} />}
    </section>
  </div>
}

function ApiDeliveryConfigs({ accountId }: { accountId: string }) {
  const location = useLocation()
  const { data, error, loading, reload } = useProtectedData<ApiDeliveryConfigRow[]>('/api/v1/web/operations/delivery/api-configs')
  const { data: pools, error: poolsError, loading: poolsLoading, reload: reloadPools } = useProtectedData<DeliveryPoolRow[]>(`/api/v1/web/operations/delivery/pools?account_id=${encodeURIComponent(accountId)}`)
  const { data: policies, error: policiesError, loading: policiesLoading, reload: reloadPolicies } = useProtectedData<DeliveryPolicyRow[]>(`/api/v1/web/operations/delivery/policies?account_id=${encodeURIComponent(accountId)}`)
  const [secretValue, setSecretValue] = useState('')
  const [secretLabel, setSecretLabel] = useState('')
  const [secretRef, setSecretRef] = useState('')
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [method, setMethod] = useState('POST')
  const [headers, setHeaders] = useState('{}')
  const [body, setBody] = useState('{"order_id":"{{order_id}}"}')
  const [responsePath, setResponsePath] = useState('data.card')
  const [poolName, setPoolName] = useState('')
  const [poolConfigRef, setPoolConfigRef] = useState('')
  const [policyName, setPolicyName] = useState('')
  const [policyPoolId, setPolicyPoolId] = useState(() => { const params = new URLSearchParams(location.search); return params.get('account') === accountId ? params.get('pool') ?? '' : '' })
  const [policyItemId, setPolicyItemId] = useState(() => { const params = new URLSearchParams(location.search); return params.get('account') === accountId ? params.get('item') ?? '' : '' })
  const [notice, setNotice] = useState('')
  const [step, setStep] = useState(() => { const params = new URLSearchParams(location.search); return params.get('account') === accountId && (params.get('item') || params.get('pool')) ? 'policies' : 'sources' })
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  async function mutate(action: () => Promise<void>) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    try { await action() } catch { setNotice('请求未取得明确结果，请刷新记录核对后再操作。') }
    finally { busyRef.current = false; setBusy(false) }
  }


  const validPolicyPoolId = pools?.some(pool => String(pool.id) === policyPoolId) ? policyPoolId : ''

  async function saveSecret(event: FormEvent) {
    event.preventDefault()
    setNotice('正在加密保存密钥…')
    const response = await fetch('/api/v1/web/operations/delivery/api-secrets', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ value: secretValue, label: secretLabel || 'delivery_api' }),
    })
    setSecretValue('')
    if (!response.ok) {
      setNotice('密钥保存失败；请检查管理员会话和加密主密钥配置。')
      return
    }
    const payload = (await response.json()) as { data: { ref: string } }
    setSecretRef(payload.data.ref)
    setNotice(`密钥已加密保存。请在请求头或请求体模板中使用 {{secret:${payload.data.ref}}}。`)
  }

  async function saveConfig(event: FormEvent) {
    event.preventDefault()
    let parsedHeaders: Record<string, string>
    try {
      const parsed = JSON.parse(headers) as unknown
      if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw new Error('invalid')
      parsedHeaders = parsed as Record<string, string>
    } catch {
      setNotice('请求头必须是 JSON 对象。')
      return
    }
    const response = await fetch('/api/v1/web/operations/delivery/api-configs', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({
        name,
        url,
        method,
        headers: parsedHeaders,
        body,
        response_path: responsePath,
        timeout_seconds: 10,
        enabled: true,
      }),
    })
    if (!response.ok) {
      setNotice('API 配置未保存；请检查 HTTPS 地址、JSON 模板和变量名。')
      return
    }
    setNotice('API 配置已保存。只有绑定该配置的 API 库存池和启用策略才会触发调用。')
    reload()
  }

  async function setEnabled(config: ApiDeliveryConfigRow) {
    const response = await fetch(`/api/v1/web/operations/delivery/api-configs/${config.id}/enabled`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ enabled: !config.enabled }),
    })
    setNotice(response.ok ? `配置「${config.name}」已${config.enabled ? '停用' : '启用'}。` : '配置状态更新失败。')
    if (response.ok) reload()
  }

  async function testConfig(config: ApiDeliveryConfigRow) {
    setNotice(`正在试调用「${config.name}」…`)
    const response = await fetch(`/api/v1/web/operations/delivery/api-configs/${config.id}/test`, {
      method: 'POST',
      headers: { 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
    })
    if (!response.ok) {
      setNotice('试调用失败；请确认配置存在且管理员会话有效。')
      return
    }
    const payload = (await response.json()) as {
      data: { ok: boolean; dispatched: boolean; status_code: number | null; preview: string | null; length: number; error: string | null }
    }
    const result = payload.data
    if (result.ok) {
      setNotice(`试调用成功：取到 ${result.length} 个字符，预览 ${result.preview}（完整卡密不会显示）。`)
      return
    }
    setNotice(`试调用未取到卡密：${result.dispatched ? '结果不明，' : ''}${result.error ?? '未知原因'}`)
  }

  async function saveApiPool(event: FormEvent) {
    event.preventDefault()
    const response = await fetch('/api/v1/web/operations/delivery/pools', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({
        name: poolName,
        type: 'api',
        account_scope: accountId,
        api_config_ref: poolConfigRef,
      }),
    })
    setNotice(response.ok ? 'API 库存池已创建；还需创建启用的发货策略。' : 'API 库存池创建失败，请先保存有效的 API 配置。')
    if (response.ok) reloadPools()
  }

  async function savePolicy(event: FormEvent) {
    event.preventDefault()
    const response = await fetch('/api/v1/web/operations/delivery/policies', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({
        name: policyName,
        inventory_pool_id: Number(validPolicyPoolId),
        account_id: accountId,
        item_id: policyItemId || null,
        enabled: true,
      }),
    })
    setNotice(response.ok ? '自动发货策略已创建；真实订单仍会经过订单状态、守卫和幂等检查。' : '策略创建失败，请确认库存池和范围没有冲突。')
    if (response.ok) reloadPolicies()
  }

  return <div className="delivery-config">
    <div className="config-step-nav" aria-label="发货配置步骤">{[{ id: 'sources', label: '发卡源', count: data?.length }, { id: 'pools', label: '库存池', count: pools?.length }, { id: 'policies', label: '发货策略', count: policies?.length }].map((entry, index) => <button key={entry.id} type="button" disabled={busy} aria-pressed={step === entry.id} onClick={() => setStep(entry.id)}><span className="step-number">{index + 1}</span><span>{entry.label}<small>{entry.count ?? '—'} 个已配置</small></span><Icon name="chevron-right" /></button>)}</div>
    {notice ? <p className="operation-feedback" role="status">{notice}</p> : null}
    <fieldset className="config-fields" disabled={busy}>
    {step === 'sources' ? <div className="config-workspace">
      <section><div className="section-heading"><div><p className="eyebrow">第一步 · 发卡源</p><h2>配置外部接口</h2><p>保存不会调用接口；密钥以加密引用填入模板。</p></div></div>
        <details className="secret-settings"><summary>保存接口密钥（按需）</summary><form className="settings-form" onSubmit={event => { event.preventDefault(); void mutate(() => saveSecret(event)) }}><label>用途标签<input value={secretLabel} onChange={event => setSecretLabel(event.target.value)} /></label><label>密钥值<input type="password" autoComplete="new-password" required value={secretValue} onChange={event => setSecretValue(event.target.value)} /></label><button type="submit">加密保存密钥</button>{secretRef ? <p>模板引用：<code>{`{{secret:${secretRef}}}`}</code></p> : null}</form></details>
        <form className="settings-form" onSubmit={event => { event.preventDefault(); void mutate(() => saveConfig(event)) }}>
          <label>配置名称<input required value={name} onChange={event => setName(event.target.value)} placeholder="便于识别的发卡源名称" /></label>
          <label>HTTPS 地址<input required type="url" value={url} onChange={event => setUrl(event.target.value)} placeholder="https://…" /></label>
          <div className="form-field-pair"><label>请求方法<select value={method} onChange={event => setMethod(event.target.value)}><option>POST</option><option>PUT</option><option>GET</option></select></label><label>响应取值路径<input required value={responsePath} onChange={event => setResponsePath(event.target.value)} /></label></div>
          <label>请求头 JSON<textarea rows={3} spellCheck={false} value={headers} onChange={event => setHeaders(event.target.value)} /></label><label>参数对象 JSON 模板<textarea rows={4} spellCheck={false} value={body} onChange={event => setBody(event.target.value)} /></label><p className="field-help">POST / PUT 作为 JSON 请求体，GET 作为查询参数；如需密钥，请使用加密引用。</p>
          <div className="form-actions"><button className="primary-button" type="submit">{busy ? '处理中…' : '校验并保存配置'}</button></div>
        </form>
      </section>
      <section><div className="section-heading"><div><h2>已配置发卡源</h2><p>全局接口目录；试调用会真实请求外部接口。</p></div><button type="button" onClick={reload}>刷新目录</button></div>
        {data?.length ? <div className="config-cards">{data.map(config => <article key={config.id}><header><strong>{config.name}</strong><span className={`status-badge ${config.enabled ? 'success' : 'neutral'}`}>{config.enabled ? '已启用' : '已停用'}</span></header><p className="config-endpoint">{config.method} {config.url}</p><dl className="detail-grid"><div><dt>响应路径</dt><dd>{config.response_path}</dd></div><div><dt>密钥状态</dt><dd>{config.secret_refs.length === 0 ? '不需要密钥' : config.secrets_configured ? '已配置' : '密钥缺失'}</dd></div><div><dt>模板变量</dt><dd>{config.variables.join('、') || '—'}</dd></div><div><dt>请求头名称</dt><dd>{config.header_names.join('、') || '—'}</dd></div></dl><footer><button type="button" onClick={() => void mutate(() => setEnabled(config))}>{config.enabled ? '停用' : '启用'}</button><button type="button" onClick={() => void mutate(() => testConfig(config))}>试调用（真实请求）</button></footer></article>)}</div> : <PageState onRetry={reload} error={error} loading={loading} emptyText="尚无发卡源。保存接口后，到库存池中关联该配置。" />}
      </section>
    </div> : null}
    {step === 'pools' ? <div className="config-workspace">
      <section><div className="section-heading"><div><p className="eyebrow">第二步 · 库存池</p><h2>关联 API 发卡源</h2><p>新池属于当前账号；卡密导入使用独立入口。</p></div></div><form className="settings-form" onSubmit={event => { event.preventDefault(); void mutate(() => saveApiPool(event)) }}><label>池名称<input required value={poolName} onChange={event => setPoolName(event.target.value)} /></label><label>API 配置<select required value={poolConfigRef} onChange={event => setPoolConfigRef(event.target.value)}><option value="">请选择发卡源</option>{data?.map(config => <option key={config.id} value={config.name}>{config.name}{config.enabled ? '' : '（已停用）'}</option>)}</select></label><div className="form-actions"><button className="primary-button" type="submit">创建 API 库存池</button><button type="button" onClick={() => setStep('sources')}>配置发卡源</button></div></form></section>
      <section><div className="section-heading"><div><h2>可用库存池目录</h2><p>包含当前账号与全局范围；创建后用策略关联到订单。</p></div><button type="button" onClick={reloadPools}>刷新库存池</button></div>{pools?.length ? <div className="config-cards">{pools.map(pool => <article key={pool.id}><header><strong>#{pool.id} {pool.name}</strong><span className={`status-badge ${pool.enabled ? 'success' : 'neutral'}`}>{pool.enabled ? '已启用' : '已停用'}</span></header><dl className="detail-grid"><div><dt>类型 / 发卡源</dt><dd>{({ api: '外部 API', data: '逐条卡密', text: '固定文案', image: '图片地址' } as Record<string, string>)[pool.type] ?? pool.type} / {pool.api_config_ref ?? '—'}</dd></div><div><dt>账号范围</dt><dd>{pool.account_scope ?? '全局'}</dd></div><div><dt>商品 / 规格范围</dt><dd>{pool.item_scope ?? '全部商品'} / {pool.sku_scope ?? '全部规格'}</dd></div><div><dt>可用 / 预留 / 结果不明</dt><dd>{pool.type === 'api' ? '按接口获取，无本地卡密计数' : `${pool.available} / ${pool.reserved} / ${pool.uncertain}`}</dd></div></dl><button type="button" onClick={() => { setPolicyPoolId(String(pool.id)); setStep('policies') }}>为此池创建策略<Icon name="chevron-right" /></button></article>)}</div> : <PageState onRetry={reloadPools} error={poolsError} loading={poolsLoading} emptyText="该账号尚无库存池。" />}</section>
    </div> : null}
    {step === 'policies' ? <div className="config-workspace">
      <section><div className="section-heading"><div><p className="eyebrow">第三步 · 发货策略</p><h2>设置自动发货范围</h2><p>创建后启用；真实订单仍经过状态、守卫与幂等检查。</p></div></div><form className="settings-form" onSubmit={event => { event.preventDefault(); void mutate(() => savePolicy(event)) }}><label>策略名称<input required value={policyName} onChange={event => setPolicyName(event.target.value)} /></label><label>库存池<select aria-label="库存池" required value={validPolicyPoolId} onChange={event => setPolicyPoolId(event.target.value)}><option value="">请选择库存池</option>{pools?.map(pool => <option key={pool.id} value={pool.id}>#{pool.id} {pool.name}{pool.enabled ? '' : '（已停用）'}</option>)}</select></label><label>限定商品 ID<input value={policyItemId} onChange={event => setPolicyItemId(event.target.value)} placeholder="留空应用于当前账号全部商品" /></label><p className="field-help">当前范围：{policyItemId.trim() ? `账号 ${accountId} · 商品 ${policyItemId}` : `账号 ${accountId} · 全部商品`}。最终策略解析请在发货工作区预演。</p><div className="form-actions"><button className="primary-button" type="submit">创建并启用策略</button><button type="button" onClick={() => setStep('pools')}>管理库存池</button></div></form></section>
      <section><div className="section-heading"><div><h2>当前账号策略</h2><p>这里只列账号专属策略，不含全局策略。</p></div><button type="button" onClick={reloadPolicies}>刷新策略</button></div>{policies?.length ? <div className="config-cards">{policies.map(policy => <article key={policy.id}><header><strong>#{policy.id} {policy.name}</strong><span className={`status-badge ${policy.enabled ? 'success' : 'neutral'}`}>{policy.enabled ? '已启用' : '已停用'}</span></header><dl className="detail-grid"><div><dt>库存池 / 优先级</dt><dd>#{policy.inventory_pool_id} / {policy.priority}</dd></div><div><dt>范围层级</dt><dd>{({ account: '账号', item: '商品', sku: '规格', global: '全局' } as Record<string, string>)[policy.level] ?? policy.level}</dd></div><div><dt>商品 / 规格</dt><dd>{policy.item_id ?? '全部商品'} / {policy.sku_id ?? '全部规格'}</dd></div><div><dt>延迟 / 平台确认</dt><dd>{policy.delay_seconds}s / {policy.platform_confirm ? '需要' : '不需要'}</dd></div></dl></article>)}</div> : <PageState onRetry={reloadPolicies} error={policiesError} loading={policiesLoading} emptyText="该账号尚无自动发货策略。" />}</section>
    </div> : null}
    </fieldset>
  </div>
}

const inventoryTypeLabels: Record<string, string> = { api: '外部 API', data: '逐条卡密', text: '固定文案', image: '图片地址' }
const unitStateLabels: Record<string, string> = { available: '可用', reserved: '已预留', consumed: '已消耗', uncertain: '结果不明' }

function InventoryPoolsPanel({ accountId }: { accountId: string }) {
  const pools = useProtectedData<DeliveryPoolRow[]>(`/api/v1/web/operations/delivery/pools?account_id=${encodeURIComponent(accountId)}`)
  const policies = useProtectedData<DeliveryPolicyRow[]>(`/api/v1/web/operations/delivery/policies?account_id=${encodeURIComponent(accountId)}`)
  const [query, setQuery] = useState('')
  const [type, setType] = useState('')
  const [enabled, setEnabled] = useState('all')
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const visible = (pools.data ?? []).filter(pool => `${pool.id} ${pool.name} ${pool.item_scope ?? ''}`.toLowerCase().includes(query.toLowerCase()) && (!type || pool.type === type) && (enabled === 'all' || pool.enabled === (enabled === 'enabled')))
  const selected = visible.find(pool => pool.id === selectedId) ?? visible[0]
  const dataPools = pools.data?.filter(pool => pool.type === 'data')
  const related = (policies.data ?? []).filter(policy => policy.inventory_pool_id === selected?.id)
  return <>
    <div className="business-metrics"><div><span>已加载库存池</span><strong>{pools.data?.length ?? '—'}</strong></div><div><span>卡密池可用单元</span><strong>{dataPools?.reduce((sum, pool) => sum + pool.available, 0) ?? '—'}</strong></div><div><span>卡密池结果不明</span><strong>{dataPools?.reduce((sum, pool) => sum + pool.uncertain, 0) ?? '—'}</strong></div></div>
    <div className="table-tools"><input aria-label="搜索库存池" placeholder="搜索池名称、ID 或商品范围" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="库存池类型" value={type} onChange={event => setType(event.target.value)}><option value="">全部类型</option>{Object.entries(inventoryTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><select aria-label="库存池启用状态" value={enabled} onChange={event => setEnabled(event.target.value)}><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select><button type="button" onClick={() => { pools.reload(); policies.reload() }}>刷新本地库存</button></div>
    <p className="list-window-note">目录包含当前账号与全局池；摘要仅统计已加载池，API 和固定文案不计入卡密数量。</p>
    <div className="stock-workspace">
      <section className="stock-pool-list" aria-label="库存池列表">{visible.length ? visible.map(pool => <button key={pool.id} className={`stock-pool-row ${selected?.id === pool.id ? 'selected' : ''}`} aria-pressed={selected?.id === pool.id} type="button" onClick={() => setSelectedId(pool.id)}><header><div><strong>{pool.name}</strong><p>#{pool.id} · {inventoryTypeLabels[pool.type] ?? pool.type}</p></div><span className={`status-badge ${pool.enabled ? 'success' : 'neutral'}`}>{pool.enabled ? '已启用' : '已停用'}</span></header><div className="pool-row-counts">{pool.type === 'data' ? <><span>可用 <strong>{pool.available}</strong></span><span>预留 <strong>{pool.reserved}</strong></span><span>不明 <strong>{pool.uncertain}</strong></span></> : <span>{pool.type === 'api' ? '发货时从接口获取' : '按配置内容交付，不消耗卡密单元'}</span>}<Icon name="chevron-right" /></div></button>) : <PageState onRetry={pools.reload} error={pools.error} loading={pools.loading} emptyText={query || type || enabled !== 'all' ? '没有匹配的库存池。' : '当前目录尚无库存池。'} />}</section>
      <section className="stock-pool-detail" aria-label="库存池详情">{selected ? <><header><div><p className="eyebrow">库存池详情 · #{selected.id}</p><h2>{selected.name}</h2></div><span className="status-badge neutral">{inventoryTypeLabels[selected.type] ?? selected.type}</span></header><dl className="detail-grid"><div><dt>账号范围</dt><dd>{selected.account_scope ?? '全局'}</dd></div><div><dt>商品范围</dt><dd>{selected.item_scope ?? '全部商品'}</dd></div><div><dt>规格范围</dt><dd>{selected.sku_scope ?? '全部规格'}</dd></div><div><dt>发卡源 / 延迟</dt><dd>{selected.api_config_ref ?? '—'} / {selected.delay_seconds}s</dd></div></dl>
        {selected.type === 'image' && selected.image_url ? <div className="stock-picture-preview"><h3>交付图片预览</h3><MediaImage url={selected.image_url} label={`${selected.name} 的交付图片`} className="stock-picture" preview /></div> : null}
        {selected.type === 'data' ? <NavLink className="primary-button" to={`/delivery?account=${encodeURIComponent(accountId)}&tab=cards&pool=${selected.id}`}>查看与导入卡密<Icon name="chevron-right" /></NavLink> : <p className="empty-inline">{selected.type === 'api' ? 'API 池按接口获取内容，没有本地卡密余量。' : '此池按配置内容交付，不使用逐条卡密库存。'}</p>}
        <div className="product-config-block"><header><h3>关联发货策略</h3><NavLink className="text-action" to={`/delivery?account=${encodeURIComponent(accountId)}&tab=sources&pool=${selected.id}`}>配置策略<Icon name="chevron-right" /></NavLink></header><p>这里只展示当前账号专属策略；最终生效范围以订单预演为准。</p>{related.length ? <ul className="related-policies">{related.map(policy => <li key={policy.id}><div><strong>{policy.name}</strong><p>{policy.item_id ? `商品 ${policy.item_id}` : '账号全部商品'}</p></div><span className={`status-badge ${policy.enabled ? 'success' : 'neutral'}`}>{policy.enabled ? '已启用' : '已停用'}</span></li>)}</ul> : <PageState onRetry={policies.reload} error={policies.error} loading={policies.loading} emptyText="当前账号目录中没有引用该池的策略。" />}</div>
      </> : <div className="empty-workspace"><Icon name="archive" /><p>选择库存池后查看范围与关联策略。</p></div>}</section>
    </div>
  </>
}

function InventoryList({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<InventoryCard[]>(`/api/v1/web/operations/inventory?account_id=${encodeURIComponent(accountId)}`)
  return <section><div className="section-heading"><div><h2>原有库存卡</h2><p>独立于发货库存池的库存卡目录；池余量请在“库存池”标签查看。</p></div><button type="button" onClick={reload}>刷新库存卡</button></div>{data?.length ? <div className="stock-table-scroll"><table><thead><tr><th>名称</th><th>类型</th><th>总量</th><th>余量</th><th>状态</th></tr></thead><tbody>{data.map(card => <tr key={card.id}><td>{card.name}</td><td>{inventoryTypeLabels[card.type] ?? card.type}</td><td>{card.total}</td><td>{card.remaining}</td><td><span className={`status-badge ${card.enabled ? 'success' : 'neutral'}`}>{card.enabled ? '已启用' : '已停用'}</span></td></tr>)}</tbody></table></div> : <PageState onRetry={reload} error={error} loading={loading} emptyText="该账号尚无原有库存卡；发货库存池在另一标签中查看。" />}</section>
}

function ReplyRulesList({ accountId }: { accountId: string }) {
  const [scope, setScope] = useState('account')
  const [query, setQuery] = useState('')
  const [enabled, setEnabled] = useState('all')
  const { data, error, loading, reload } = useProtectedData<ReplyRule[]>(scope === 'account' ? `/api/v1/web/operations/rules?account_id=${encodeURIComponent(accountId)}` : '/api/v1/web/operations/rules')
  const labels: Record<string, string> = { keyword: '关键词', regex: '正则匹配', default: '默认回复' }
  const visible = (data ?? []).filter(row => `${row.name} ${row.pattern}`.toLowerCase().includes(query.toLowerCase()) && (enabled === 'all' || row.enabled === (enabled === 'enabled')))
  return <section className="reply-rules-panel"><header className="section-heading"><div><h2>自动回复规则</h2><p>{scope === 'account' ? '当前账号专属规则，不含全局规则。' : '所有账号与全局规则目录；此列表不代表当前账号的实际匹配结果。'}</p></div><button type="button" onClick={reload}><Icon name="refresh" />刷新规则</button></header><div className="table-tools"><input aria-label="搜索回复规则" placeholder="搜索规则名称或匹配条件" value={query} onChange={e => setQuery(e.target.value)} /><select aria-label="回复规则范围" value={scope} onChange={e => setScope(e.target.value)}><option value="account">当前账号专属</option><option value="all">全部规则目录</option></select><select aria-label="回复规则启用状态" value={enabled} onChange={e => setEnabled(e.target.value)}><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select><span>匹配 {visible.length} 条</span></div>{visible.length ? <div className="rule-table-scroll"><table><thead><tr><th>规则与匹配条件</th><th>匹配方式</th><th>优先级</th><th>状态</th><th>重复回复</th><th>配图</th></tr></thead><tbody>{visible.map(rule => <tr key={rule.id}><td><strong>{rule.name}</strong><p className="row-description">{rule.type === 'default' ? '默认回复条件' : rule.pattern}</p></td><td>{labels[rule.type] ?? rule.type}</td><td>{rule.priority}</td><td><span className={`status-badge ${rule.enabled ? 'success' : 'neutral'}`}>{rule.enabled ? '已启用' : '已停用'}</span></td><td>{rule.reply_once ? '仅回复一次' : '允许重复'}</td><td>{rule.reply_image_url ? <MediaImage url={rule.reply_image_url} label={`${rule.name} 的回复配图`} className="rule-picture" preview /> : '—'}</td></tr>)}</tbody></table></div> : <PageState onRetry={reload} error={error} loading={loading} emptyText={query || enabled !== 'all' ? '没有匹配的回复规则。' : scope === 'account' ? '当前账号尚无专属规则；可切换到全部目录查看全局规则。' : '尚未配置自动回复规则。'} />}<p className="list-window-note">优先级数值越小越优先。此页只查看规则，不会触发回复。</p></section>
}

function PlatformAutomationPanel({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<PlatformAutomationRow[]>(`/api/v1/web/operations/platform-automation?account_id=${encodeURIComponent(accountId)}`)
  const runs = useProtectedData<PlatformAutomationRun[]>(`/api/v1/web/operations/platform-automation/runs?account_id=${encodeURIComponent(accountId)}`)
  const [action, setAction] = useState('polish')
  const [enabled, setEnabled] = useState(false)
  const [scheduleTime, setScheduleTime] = useState('03:00')
  const [delayMinutes, setDelayMinutes] = useState('0')
  const [quietHours, setQuietHours] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const selectedConfig = data?.find(row => row.action === action)
  const actionLabels: Record<string, string> = { polish: '每日擦亮', request_flower: '自动求花' }
  function changeAction(next: string) {
    setAction(next)
    setEnabled(false)
    setScheduleTime(next === 'polish' ? '03:00' : '10:00')
    setDelayMinutes(next === 'polish' ? '0' : '1440')
    setQuietHours(next === 'polish' ? '' : '23:00-08:00')
    setNotice('')
  }
  function loadSaved() {
    if (!selectedConfig) return
    setEnabled(selectedConfig.enabled)
    setScheduleTime(selectedConfig.schedule_time)
    setDelayMinutes(String(selectedConfig.delay_minutes))
    setQuietHours(selectedConfig.quiet_hours ?? '')
    setNotice('已载入保存的设置，可在左侧修改。')
  }


  async function save(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    try {
    const response = await fetch(`/api/v1/web/operations/platform-automation?account_id=${encodeURIComponent(accountId)}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ action, enabled, schedule_time: scheduleTime, delay_minutes: Number(delayMinutes), quiet_hours: quietHours || null }),
    })
    setNotice(response.ok ? '自动化配置已保存。' : '配置被拒绝；启用前必须先完成并登记真实校准。')
    if (response.ok) reload()
    } catch { setNotice('无法确认配置是否保存，请刷新状态核对。') } finally { setBusy(false) }
  }

  async function approveCalibration() {
    if (!window.confirm(`仅当你已按 F5/F6 在测试账号真实验证 ${action} 后才能登记。确认已完成？`)) return
    const response = await fetch(`/api/v1/web/operations/platform-automation/calibration?account_id=${encodeURIComponent(accountId)}`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ action, confirm: true }),
    })
    setNotice(response.ok ? '真实校准已登记；动作仍保持停用，需要再次保存启用配置。' : '校准登记失败。')
    if (response.ok) reload()
  }

  return <><div className="automation-workspace"><section><header className="section-heading"><div><h2>擦亮与求花</h2><p>先完成真实校准，再启用定时执行。</p></div><Icon name="bolt" /></header>{notice ? <p role="status">{notice}</p> : null}<form className="settings-form" onSubmit={save}><label>动作<select disabled={busy} value={action} onChange={e => changeAction(e.target.value)}><option value="polish">每日擦亮</option><option value="request_flower">自动求花</option></select></label><div className="form-field-pair"><label>执行时间<input type="time" required value={scheduleTime} onChange={e => setScheduleTime(e.target.value)} /></label><label>延迟（分钟）<input type="number" min="0" max="43200" value={delayMinutes} onChange={e => setDelayMinutes(e.target.value)} /></label></div><label>静默时段<input placeholder="例如 23:00-08:00；留空为不限制" value={quietHours} onChange={e => setQuietHours(e.target.value)} /></label><label className="check-field"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用定时执行</label><div className="form-actions"><button className="primary-button" disabled={busy} type="submit">{busy ? '保存中…' : '保存配置'}</button><button disabled={busy || !selectedConfig} type="button" onClick={loadSaved}>载入已保存设置</button></div></form><p className="row-description">保存启用配置不会立即执行。未通过真实校准的动作会被服务端拒绝启用。</p></section><section className="automation-status-panel"><header className="section-heading"><h2>已保存状态</h2><button type="button" onClick={() => { reload(); runs.reload() }}><Icon name="refresh" />刷新</button></header>{data ? <><div className="automation-state-cards">{['polish','request_flower'].map(value => { const row = data.find(config => config.action === value); return <div key={value}><header><strong>{actionLabels[value]}</strong><span className={`status-badge ${row?.enabled ? 'success' : 'neutral'}`}>{row?.enabled ? '已启用' : '已停用'}</span></header><dl className="detail-grid"><div><dt>真实校准</dt><dd>{row?.calibrated ? '已通过' : '未通过'}</dd></div><div><dt>执行时间</dt><dd>{row?.schedule_time ?? '尚未配置'}</dd></div><div><dt>延迟</dt><dd>{row ? `${row.delay_minutes} 分钟` : '尚未配置'}</dd></div><div><dt>静默时段</dt><dd>{row ? row.quiet_hours ?? '不限' : '尚未配置'}</dd></div></dl></div> })}</div><div className="calibration-box"><h3>{actionLabels[action]} · 校准登记</h3><p>只有实际验证通过后才登记。登记不会启用动作。</p><button type="button" onClick={approveCalibration} disabled={busy}>登记真实校准通过</button></div></> : <PageState onRetry={reload} loading={loading} error={error} emptyText="" />}</section></div><section><header className="section-heading"><div><h2>最近执行记录</h2><p>结果不明的动作需要人工核查，系统不会自动重发。</p></div></header>{runs.data?.length ? <table><thead><tr><th>开始时间</th><th>动作</th><th>目标</th><th>状态</th><th>结果</th></tr></thead><tbody>{runs.data.map(row => <tr key={row.id}><td>{localTime(row.started_at)}</td><td>{actionLabels[row.action] ?? row.action}</td><td>{row.target_id}</td><td>{({ uncertain: '结果不明', completed: '已完成', running: '执行中', failed: '失败', pending: '等待执行' } as Record<string,string>)[row.status] ?? row.status}</td><td>{row.result ?? '—'}</td></tr>)}</tbody></table> : <PageState onRetry={runs.reload} error={runs.error} loading={runs.loading} emptyText="尚无执行记录。" />}</section></>
}

const auditActionLabels: Record<string, string> = {
  credential_cookie_saved: '更新账号凭据', account_lease_acquired: '建立账号连接', account_lease_released: '释放账号连接',
  'auth.validation_required': '账号需要验证', 'auth.long_login_renew': '登录态续期',
  'message.send.result': '消息发送结果', 'message.send.attempt': '消息发送请求',
  'account.refresh_profile': '刷新账号资料', 'backup.imported': '导入配置备份', worker_command_queued: '提交账号命令',
}
const auditResultLabels: Record<string, string> = { ok: '完成', success: '成功', renewed: '已续期', failed: '失败', error: '错误', blocked: '已阻止', uncertain: '结果不明', queued: '已入队', attempted: '已尝试', skipped: '已跳过' }
function auditResultTone(result: string) {
  const value = result.toLowerCase()
  return ['ok', 'success', 'renewed'].includes(value) ? 'success' : ['failed', 'error', 'blocked', 'uncertain'].includes(value) ? 'warning' : 'neutral'
}
function AuditPage() {
  const { data, error, loading, reload } = useProtectedData<AuditEntry[]>('/api/v1/web/operations/audit?limit=100')
  const list = useListLocation()
  const query = list.read('auditQuery')
  const actor = list.read('auditActor')
  const result = list.read('auditResult')
  const requestedId = Number(list.read('record'))
  const selectedId = Number.isSafeInteger(requestedId) && requestedId > 0 ? requestedId : null
  function setQuery(value: string) { list.update({ auditQuery: value, record: null }) }
  function setActor(value: string) { list.update({ auditActor: value, record: null }, false) }
  function setResult(value: string) { list.update({ auditResult: value, record: null }, false) }
  function setSelectedId(value: number) { list.update({ record: String(value) }) }
  const actors = [...new Set([...(data ?? []).map(row => row.actor), ...(actor ? [actor] : [])])]
  const results = [...new Set([...(data ?? []).map(row => row.result), ...(result ? [result] : [])])]
  const term = query.trim().toLowerCase()
  const rows = (data ?? []).filter(row => (!actor || row.actor === actor) && (!result || row.result === result) && `${row.id} ${row.actor} ${row.action} ${auditActionLabels[row.action] ?? ''} ${row.target ?? ''} ${row.result} ${auditResultLabels[row.result.toLowerCase()] ?? ''}`.toLowerCase().includes(term))
  const selected = rows.find(row => row.id === selectedId) ?? rows[0]
  function resetFilters() { list.update({ auditQuery: null, auditActor: null, auditResult: null, record: null }, false) }
  return <main className="business-page">
    <header className="business-heading"><div><h1>审计日志</h1><p>查找操作记录，核对动作、目标与登记结果。</p></div><button type="button" onClick={reload}><Icon name="refresh" />刷新审计</button></header>
    <div className="business-metrics"><div><span>已加载记录</span><strong>{data?.length ?? '—'}</strong></div><div><span>成功或完成结果</span><strong>{data ? data.filter(row => auditResultTone(row.result) === 'success').length : '—'}</strong></div><div><span>失败或需核对结果</span><strong>{data ? data.filter(row => auditResultTone(row.result) === 'warning').length : '—'}</strong></div></div>
    <div className="table-tools audit-tools"><input aria-label="搜索审计记录" placeholder="搜索动作、目标、主体或记录 ID" value={query} onChange={e => setQuery(e.target.value)} /><select aria-label="审计主体" value={actor} onChange={e => setActor(e.target.value)}><option value="">全部主体</option>{actors.map(value => <option key={value} value={value}>{value === 'system' ? 'system · 系统' : value}</option>)}</select><select aria-label="审计结果" value={result} onChange={e => setResult(e.target.value)}><option value="">全部结果</option>{results.map(value => <option key={value} value={value}>{auditResultLabels[value.toLowerCase()] ? `${auditResultLabels[value.toLowerCase()]} · ${value}` : value}</option>)}</select>{query || actor || result ? <button type="button" onClick={resetFilters}>清空筛选</button> : null}<span>显示 {rows.length} 条记录</span></div>
    <p className="list-window-note">全部账号 · 按时间倒序读取最近 100 条；搜索与统计仅针对已加载记录。审计结果不代表平台最终状态。</p>
    <div className="audit-workspace"><section className="audit-record-list" aria-label="审计记录列表">{rows.length ? rows.map(row => <button type="button" key={row.id} className={`audit-record-row ${selected?.id === row.id ? 'selected' : ''}`} aria-pressed={selected?.id === row.id} onClick={() => setSelectedId(row.id)}><header><strong>{auditActionLabels[row.action] ?? row.action}</strong><span className={`status-badge ${auditResultTone(row.result)}`}>{auditResultLabels[row.result.toLowerCase()] ?? row.result}</span></header><p>{row.target ?? '无目标记录'}</p><div><span>{row.actor === 'system' ? '系统' : row.actor} · #{row.id}</span><time>{localTime(row.created_at)}</time></div></button>) : <PageState onRetry={reload} error={error} loading={loading} emptyText={data?.length ? '当前筛选没有匹配记录。' : '尚无可显示的审计记录。'} />}</section>
      {selected ? <section className="audit-record-detail"><header className="section-heading"><div><span className="eyebrow">记录 #{selected.id}</span><h2>{auditActionLabels[selected.action] ?? selected.action}</h2></div><span className={`status-badge ${auditResultTone(selected.result)}`}>{auditResultLabels[selected.result.toLowerCase()] ?? selected.result}</span></header><dl className="detail-grid"><div><dt>记录时间</dt><dd>{localTime(selected.created_at)}</dd></div><div><dt>操作主体</dt><dd>{selected.actor}</dd></div><div><dt>原始动作</dt><dd><code>{selected.action}</code></dd></div><div><dt>原始结果</dt><dd><code>{selected.result}</code></dd></div><div className="audit-target"><dt>操作目标</dt><dd>{selected.target ?? '—'}</dd></div></dl><p className="row-description">此页只显示审计投影；参数和错误正文不在页面提供。入队、尝试与成功记录分别保留，请结合对应业务记录核对。</p></section> : null}
    </div>
  </main>
}

const backupSectionLabels: Record<string, string> = { accounts: '账号定义', reply_rules: '回复规则', api_configs: '发卡源配置', delivery_templates: '发货模板', inventory_pools: '库存池', delivery_policies: '发货策略' }
function BackupPage() {
  const [includeCards, setIncludeCards] = useState(false)
  const [documentText, setDocumentText] = useState('')
  const [dryRun, setDryRun] = useState(true)
  const [exportNotice, setExportNotice] = useState('')
  const [importNotice, setImportNotice] = useState('')
  const [report, setReport] = useState<BackupImportResult | null>(null)
  const [busy, setBusy] = useState<'export' | 'import' | null>(null)
  const busyRef = useRef(false)
  function editDocument(value: string) { setDocumentText(value); setReport(null); setImportNotice('') }
  function changeMode(value: boolean) { setDryRun(value); setReport(null); setImportNotice('') }

  async function exportBackup() {
    if (busyRef.current) return
    busyRef.current = true; setBusy('export'); setExportNotice('正在生成备份…')
    try {
      const response = await fetch(`/api/v1/web/operations/backup/export?include_cards=${includeCards}`)
      if (!response.ok) throw new Error('export rejected')
      const payload = (await response.json()) as { data: Record<string, unknown> }
      if (!payload.data || typeof payload.data !== 'object' || Array.isArray(payload.data)) throw new Error('invalid export')
      const blob = new Blob([JSON.stringify(payload.data, null, 2) + '\n'], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = 'xianyu-backup.json'; anchor.click()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setExportNotice(includeCards ? '备份已生成并触发下载，包含可用卡密正文，请按敏感文件保管。' : '备份已生成并触发下载，未包含卡密单元正文。')
    } catch {
      setExportNotice('备份未生成，请检查管理员会话和连接后再操作。')
    } finally { busyRef.current = false; setBusy(null) }
  }

  async function importBackup(event: FormEvent) {
    event.preventDefault()
    if (busyRef.current) return
    setReport(null)
    setImportNotice('')
    let document: Record<string, unknown>
    try {
      const parsed: unknown = JSON.parse(documentText)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) { setImportNotice('备份必须是 JSON 对象。'); return }
      document = parsed as Record<string, unknown>
    } catch { setImportNotice('JSON 格式错误，请检查后再提交。'); return }
    if (!dryRun && !window.confirm('确认写入本地运营配置？已存在对象会跳过，不覆盖或删除；本次操作不会调用闲鱼平台。')) return
    busyRef.current = true; setBusy('import'); setImportNotice(dryRun ? '正在预演导入…' : '正在写入配置…')
    try {
      const response = await fetch('/api/v1/web/operations/backup/import', {
        method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
        body: JSON.stringify({ document, dry_run: dryRun }),
      })
      if (!response.ok) throw new Error('import rejected')
      const payload = (await response.json()) as { data: BackupImportResult }
      const result = payload.data
      const countMap = (value: unknown) => typeof value === 'object' && value !== null && !Array.isArray(value) && Object.values(value).every(count => Number.isInteger(count) && count >= 0)
      if (!result || result.mode !== (dryRun ? 'dry_run' : 'apply') || !countMap(result.created) || !countMap(result.skipped) || !Number.isInteger(result.total_created) || result.total_created < 0 || !Number.isInteger(result.total_skipped) || result.total_skipped < 0) throw new Error('unconfirmed import result')
      setReport(result)
      setImportNotice(dryRun ? '预演完成，未写入运营配置；本次请求会登记审计。' : '配置写入完成，请到对应业务页核对。')
    } catch {
      setImportNotice(dryRun ? '预演未完成，请检查备份版本、会话和连接；输入已保留。' : '未确认写入完成，输入已保留。请先核对审计与现有配置，勿直接重复写入。')
    } finally { busyRef.current = false; setBusy(null) }
  }

  return <main className="business-page"><header className="business-heading"><div><h1>配置备份</h1><p>导出运营配置，先预演再核对导入结果。</p></div><Icon name="database" /></header>
    <p className="scope-note">全局配置 · 此页不按侧栏保留的账号范围筛选。账号凭据、密钥库和真实校准记录不包含在备份中。</p>
    <div className="backup-workspace"><section className="backup-export"><header className="section-heading"><div><span className="eyebrow">保存当前配置</span><h2>导出备份</h2></div><Icon name="archive" /></header><p>将当前运营配置保存为 JSON 文件，便于迁移与留档。</p><div className="backup-section-tags">{Object.values(backupSectionLabels).map(label => <span key={label}>{label}</span>)}</div><form className="settings-form" onSubmit={event => { event.preventDefault(); void exportBackup() }}><label className="check-field"><input type="checkbox" aria-label="导出可用卡密正文" disabled={busy !== null} checked={includeCards} onChange={e => { setIncludeCards(e.target.checked); setExportNotice('') }} />包含可用卡密单元正文</label><p className="row-description">默认不含卡密单元正文；勾选后各池读取前 50,000 条单元，仅导出其中可用单元。固定文案与模板内容仍作为运营配置导出。</p><div className="form-actions"><button className="primary-button" type="submit" disabled={busy !== null}>{busy === 'export' ? '正在生成…' : '导出 JSON'}</button></div></form>{exportNotice ? <p role="status" className="backup-notice">{exportNotice}</p> : null}<div className="backup-boundary"><h3>迁移后需要补齐</h3><p>账号登录凭据和密钥需单独配置；擦亮／求花的真实校准需在目标环境重新完成。备份不包含消息、订单和历史执行记录。</p></div></section>
      <section className="backup-import"><header className="section-heading"><div><span className="eyebrow">核对后恢复配置</span><h2>导入备份</h2></div><span className={`status-badge ${dryRun ? 'neutral' : 'warning'}`}>{dryRun ? '预演模式' : '写入模式'}</span></header><p>粘贴备份 JSON；已存在对象会跳过，不覆盖或删除。</p><form className="settings-form" onSubmit={importBackup}><label>导入模式<select aria-label="备份导入模式" disabled={busy !== null} value={dryRun ? 'dry_run' : 'apply'} onChange={e => changeMode(e.target.value === 'dry_run')}><option value="dry_run">预演 · 不写入运营配置</option><option value="apply">写入 · 新建运营配置</option></select></label><label>备份 JSON<textarea aria-label="备份 JSON" placeholder="粘贴导出的 JSON 对象" disabled={busy !== null} value={documentText} onChange={e => editDocument(e.target.value)} rows={12} spellCheck={false} required /></label><p className="row-description">预演预计新建／跳过数量；写入时仍由服务端校验，结果可能随现有配置变化。成功的预演与写入会登记审计。</p><div className="form-actions"><button className="primary-button" type="submit" disabled={busy !== null || !documentText.trim()}>{busy === 'import' ? dryRun ? '正在预演…' : '正在写入…' : dryRun ? '预演导入' : '确认写入配置'}</button><button type="button" disabled={busy !== null || !documentText} onClick={() => editDocument('')}>清空输入</button></div></form>{importNotice ? <p className="backup-notice" role="status">{importNotice}</p> : null}
        <div className="backup-result"><header className="section-heading"><div><h3>{report ? report.mode === 'dry_run' ? '预演结果' : '写入结果' : '导入结果'}</h3><p>{report ? '仅对应当前 JSON 与当前操作模式。' : '完成预演或写入后，按配置类型核对结果。'}</p></div><NavLink className="text-action" to="/audit">查看审计<Icon name="chevron-right" /></NavLink></header>{report ? <><div className="backup-result-totals"><div><span>{report.mode === 'dry_run' ? '预计新建' : '实际新建'}</span><strong>{report.total_created}</strong></div><div><span>{report.mode === 'dry_run' ? '预计跳过' : '实际跳过'}</span><strong>{report.total_skipped}</strong></div></div><div className="backup-result-table"><table><thead><tr><th>配置类型</th><th>{report.mode === 'dry_run' ? '预计新建' : '实际新建'}</th><th>{report.mode === 'dry_run' ? '预计跳过' : '实际跳过'}</th></tr></thead><tbody>{[...new Set([...Object.keys(report.created), ...Object.keys(report.skipped)])].map(key => <tr key={key}><td>{backupSectionLabels[key] ?? key}</td><td>{report.created[key] ?? 0}</td><td>{report.skipped[key] ?? 0}</td></tr>)}</tbody></table></div></> : <p className="row-description">尚无当前输入的导入结果。</p>}</div>
      </section></div>
  </main>
}

function PageTabs({ tabs, keepMounted = false }: { tabs: { id: string; label: string; render: () => ReactNode }[]; keepMounted?: boolean }) {
  const location = useLocation()
  const navigate = useNavigate()
  const requested = new URLSearchParams(location.search).get('tab')
  const tab = tabs.find(row => row.id === requested) ?? tabs[0]
  const navRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const nav = navRef.current
    if (!nav) return
    function showActive() {
      if (!nav || nav.scrollWidth <= nav.clientWidth) return
      const button = nav.querySelector<HTMLElement>('button[aria-pressed="true"]')
      if (!button) return
      const left = button.getBoundingClientRect().left - nav.getBoundingClientRect().left + nav.scrollLeft
      nav.scrollTo({ left: left - (nav.clientWidth - button.offsetWidth) / 2, behavior: 'instant' })
    }
    showActive()
    const observer = new ResizeObserver(showActive)
    observer.observe(nav)
    return () => observer.disconnect()
  }, [tab.id])
  function selectTab(id: string) { const params = new URLSearchParams(location.search); params.set('tab', id); navigate({ pathname: location.pathname, search: params.toString() }) }
  return <><div ref={navRef} className="page-tabs" aria-label="页面功能">{tabs.map(row => <button key={row.id} type="button" aria-pressed={row.id === tab.id} onClick={() => selectTab(row.id)}>{row.label}</button>)}</div>{keepMounted ? tabs.map(row => <div key={row.id} className="page-tab-content" hidden={row.id !== tab.id}>{row.render()}</div>) : <div className="page-tab-content">{tab.render()}</div>}</>
}
const pageDescriptions: Record<string, string> = { '消息中心': '按会话查看消息、接管回复；快捷短语与记录单独管理。', '商品管理': '查看商品镜像，管理商品级自动化与生命周期。', '订单管理': '查询本地订单镜像，按状态定位待处理订单。', '发货管理': '先处理交付任务，再配置发卡源、库存与模板。', '自动化与回复': '管理平台动作和回复规则，每项能力单独配置。', '卡密库存': '查看库存池余量和启用状态。' }
function AccountScopedPage({ title, children, className = '' }: { title: string; children: (accountId: string) => ReactNode; className?: string }) {
  const location = useLocation()
  const { data, error, loading, reload } = useProtectedData<AccountStatus[]>('/api/v1/web/operations/accounts')
  const navigate = useNavigate()
  const requestedAccount = new URLSearchParams(location.search).get('account')
  const accountId = data?.find(row => row.account_id === requestedAccount)?.account_id ?? data?.[0]?.account_id ?? ''
  function selectAccount(value: string) { const params = new URLSearchParams(location.search); params.set('account', value); for (const key of ['item', 'order', 'pool', 'target', 'scope', 'itemQuery', 'itemState', 'orderQuery', 'orderState']) params.delete(key); navigate({ pathname: location.pathname, search: params.toString() }) }
  return <main className={`business-page ${className}`}><header className="business-heading"><div><h1>{title}</h1><p>{pageDescriptions[title] ?? "查看账号本地镜像与运营配置。"}</p></div>{data?.length ? <AccountPicker accounts={data} value={accountId} onChange={selectAccount} /> : null}</header>{data && data.length > 0 ? <div key={accountId}>{children(accountId)}</div> : <PageState onRetry={reload} error={error} loading={loading} emptyText="请先添加账号后再查看本地镜像。" />}</main>
}

function ItemAutomationPanel({ accountId, initialItemId = '' }: { accountId: string; initialItemId?: string }) {
  const { data, error, loading, reload } = useProtectedData<ItemAutomationRow[]>(`/api/v1/web/operations/items/automation?account_id=${encodeURIComponent(accountId)}`)
  const [itemId, setItemId] = useState(initialItemId)
  const [mode, setMode] = useState('exclude')
  const [note, setNote] = useState('')
  const [notice, setNotice] = useState('')
  const modeLabels: Record<string, string> = { exclude: '排除', keep: '强制保留', clear: '恢复自动判断' }

  async function save(event: FormEvent) {
    event.preventDefault()
    const response = await fetch('/api/v1/web/operations/items/automation', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ account_id: accountId, item_id: itemId, mode, note: note || null }),
    })
    if (!response.ok) {
      setNotice('保存失败：请确认商品已同步到本地；选择强制保留时必须填写原因。')
      return
    }
    setNotice(`已设置 ${itemId} 为${modeLabels[mode]}。`)
    setNote('')
    reload()
  }

  return (
    <section>
      <h2>商品级自动化</h2>
      <p>人工设置优先于自动策略。排除后不参与生命周期计划；强制保留后不会被计划淘汰；恢复自动判断后重新按策略处理。</p>
      {notice ? <p role="status">{notice}</p> : null}
      <form className="item-automation-form" onSubmit={save}>
        <label>商品 ID <input required value={itemId} onChange={(event) => setItemId(event.target.value)} /></label>
        <label>模式 <select value={mode} onChange={(event) => setMode(event.target.value)}><option value="exclude">排除</option><option value="keep">强制保留</option><option value="clear">恢复自动判断</option></select></label>
        <label>原因 <input required={mode === 'keep'} value={note} onChange={(event) => setNote(event.target.value)} placeholder={mode === 'keep' ? '请填写强制保留的原因' : '可填写设置原因'} /></label>
        <div className="form-actions"><button className="primary-button" type="submit">保存设置</button><button type="button" onClick={reload}>刷新列表</button></div>
      </form>
      {data && data.length > 0 ? (
        <table><thead><tr><th>商品</th><th>标题</th><th>模式</th><th>原因</th><th>操作人</th><th>时间</th></tr></thead><tbody>
          {data.map((row) => <tr key={row.item_id}><td>{row.item_id}</td><td>{row.title}</td><td>{row.mode ? modeLabels[row.mode] ?? row.mode : '自动判断'}</td><td>{row.note ?? '—'}</td><td>{row.by ?? '—'}</td><td>{localTime(row.at)}</td></tr>)}
        </tbody></table>
      ) : <PageState onRetry={reload} error={error} loading={loading} emptyText="该账号没有人工覆盖的商品。" />}
    </section>
  )
}

function QuickRepliesPanel({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<QuickReplyRow[]>(`/api/v1/web/operations/chat/quick-replies?account_id=${encodeURIComponent(accountId)}`)
  const [title, setTitle] = useState('')
  const [content, setContent] = useState('')
  const [notice, setNotice] = useState('')

  async function save(event: FormEvent) {
    event.preventDefault()
    const response = await fetch('/api/v1/web/operations/chat/quick-replies', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ title, content, account_id: accountId }),
    })
    if (!response.ok) {
      setNotice('保存失败:标题与内容不能为空,且同一账号下标题不能重复。')
      return
    }
    setNotice(`快捷回复「${title}」已保存。`)
    setTitle('')
    setContent('')
    reload()
  }

  async function remove(row: QuickReplyRow) {
    const response = await fetch(`/api/v1/web/operations/chat/quick-replies/${row.id}`, {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
    })
    setNotice(response.ok ? `快捷回复「${row.title}」已删除。` : '删除失败。')
    if (response.ok) reload()
  }

  return (
    <section>
      <h2>快捷回复</h2>
      <p>只给人工客服复用,不会被自动回复引擎读取。</p>
      {notice ? <p role="status">{notice}</p> : null}
      <form onSubmit={save}>
        <label>标题 <input required value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <label>内容 <textarea rows={2} required value={content} onChange={(event) => setContent(event.target.value)} /></label>
        <button type="submit">保存快捷回复</button>
      </form>
      {data && data.length > 0 ? (
        <table><thead><tr><th>范围</th><th>标题</th><th>内容</th><th>操作</th></tr></thead><tbody>
          {data.map((row) => <tr key={row.id}><td>{row.account_id ?? '全局'}</td><td>{row.title}</td><td>{row.content}</td><td><button type="button" onClick={() => remove(row)}>删除</button></td></tr>)}
        </tbody></table>
      ) : <PageState onRetry={reload} error={error} loading={loading} emptyText="还没有快捷回复。" />}
    </section>
  )
}

function BuyerNotePanel({ accountId }: { accountId: string }) {
  const [buyerId, setBuyerId] = useState('')
  const [note, setNote] = useState('')
  const [notice, setNotice] = useState('')
  const noteUrl = buyerId ? `/api/v1/web/operations/chat/buyer-notes?account_id=${encodeURIComponent(accountId)}&buyer_id=${encodeURIComponent(buyerId)}` : null
  const current = useProtectedData<BuyerNotePayload>(noteUrl)

  async function save(event: FormEvent) {
    event.preventDefault()
    const response = await fetch('/api/v1/web/operations/chat/buyer-notes', {
      method: 'PUT',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ account_id: accountId, buyer_id: buyerId, note }),
    })
    setNotice(response.ok ? `买家 ${buyerId} 的备注已保存。` : '保存失败:内容不能为空。')
    if (response.ok) current.reload()
  }

  return (
    <section>
      <h2>买家备注</h2>
      <p>备注只给运营看,不会改变对买家说的话;同账号同买家只有一条。</p>
      {notice ? <p role="status">{notice}</p> : null}
      <label>买家 ID <input value={buyerId} onChange={(event) => setBuyerId(event.target.value)} /></label>
      {current.data ? <p>当前备注:{current.data.note}(更新于 {current.data.updated_at ?? '—'})</p> : null}
      <form onSubmit={save}>
        <label>备注 <textarea rows={2} required value={note} onChange={(event) => setNote(event.target.value)} /></label>
        <button type="submit">保存备注</button>
      </form>
    </section>
  )
}

function ReplyRecordsPanel({ accountId }: { accountId: string }) {
  const { data, error, loading, reload } = useProtectedData<ReplyRecordRow[]>(`/api/v1/web/operations/chat/reply-records?account_id=${encodeURIComponent(accountId)}`)
  const [notice, setNotice] = useState('')

  async function resetRecords() {
    if (!window.confirm('清空该账号的回复投递记录？清空后 reply-once 规则可以重新触发；消息与审计不会删除。')) return
    const response = await fetch('/api/v1/web/operations/chat/reply-records/reset', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ account_id: accountId }),
    })
    if (!response.ok) {
      setNotice('清空失败；请检查账号是否存在。')
      return
    }
    const payload = (await response.json()) as { data: { removed: number } }
    setNotice(`已清空 ${payload.data.removed} 条回复投递记录。`)
    reload()
  }

  return (
    <section>
      <h2>回复投递记录</h2>
      <p>这条记录是「只回一次」的判重依据，也是“机器人为什么没再回”的答案。页面只显示长度与前 8 位预览。</p>
      {notice ? <p role="status">{notice}</p> : null}
      <button type="button" onClick={reload}>刷新记录</button>
      <button type="button" onClick={resetRecords}>清空记录</button>
      {data && data.length > 0 ? (
        <table><thead><tr><th>id</th><th>规则</th><th>会话</th><th>结果</th><th>来源</th><th>正文长度</th><th>预览</th><th>图片</th><th>时间</th></tr></thead><tbody>
          {data.map((row) => <tr key={row.id}><td>{row.id}</td><td>{row.rule_id ? `#${row.rule_id}(${row.rule_name ?? '-'})` : '—'}</td><td>{row.conversation_id ?? '—'}</td><td>{row.success ? 'OK' : `FAIL:${row.error ?? '-'}`}</td><td>{row.source}</td><td>{row.text_length}</td><td>{row.text_preview ?? '—'}</td><td>{row.has_image ? '是' : '否'}</td><td>{row.sent_at ?? '—'}</td></tr>)}
        </tbody></table>
      ) : <PageState onRetry={reload} error={error} loading={loading} emptyText="还没有回复投递记录。" />}
    </section>
  )
}

function MessagesPage() { return <AccountScopedPage title="消息中心">{accountId => <PageTabs tabs={[{ id: 'conversations', label: '会话工作区', render: () => <ConversationsList accountId={accountId} /> }, { id: 'phrases', label: '快捷短语', render: () => <QuickRepliesPanel accountId={accountId} /> }, { id: 'notes', label: '买家备注', render: () => <BuyerNotePanel accountId={accountId} /> }, { id: 'records', label: '回复记录', render: () => <ReplyRecordsPanel accountId={accountId} /> }]} />}</AccountScopedPage> }
function ItemsWorkspace({ accountId }: { accountId: string }) {
  const location = useLocation()
  const list = useListLocation(accountId)
  const params = new URLSearchParams(location.search)
  const itemId = params.get('account') === accountId ? params.get('item') ?? '' : ''
  function selectItem(id: string, configure = false) { list.update(configure ? { item: id, tab: 'automation' } : { item: id }, !configure) }
  return <PageTabs tabs={[{ id: 'items', label: '商品列表', render: () => <ItemsList accountId={accountId} selectedItemId={itemId} onSelect={id => selectItem(id)} onConfigure={id => selectItem(id, true)} /> }, { id: 'automation', label: '商品自动化', render: () => <ItemAutomationPanel key={itemId} accountId={accountId} initialItemId={itemId} /> }, { id: 'lifecycle', label: '生命周期预览', render: () => <LifecyclePreviewPanel accountId={accountId} /> }]} />
}
function ItemsPage() { return <AccountScopedPage title="商品管理">{accountId => <ItemsWorkspace accountId={accountId} />}</AccountScopedPage> }
function OrdersPage() { return <AccountScopedPage title="订单管理" className="orders-page">{accountId => <OrdersList accountId={accountId} />}</AccountScopedPage> }

function CardUnitsPanel({ accountId }: { accountId: string }) {
  const location = useLocation()
  const pools = useProtectedData<DeliveryPoolRow[]>(`/api/v1/web/operations/delivery/pools?account_id=${encodeURIComponent(accountId)}`)
  const [poolId, setPoolId] = useState(() => { const params = new URLSearchParams(location.search); return params.get('account') === accountId ? params.get('pool') ?? '' : '' })
  const [contents, setContents] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [state, setState] = useState('')
  const [query, setQuery] = useState('')
  const dataPools = (pools.data ?? []).filter(pool => pool.type === 'data')
  const selectedPool = dataPools.find(pool => String(pool.id) === poolId) ?? dataPools[0]
  const selected = selectedPool ? String(selectedPool.id) : ''
  const units = useProtectedData<CardUnitRow[]>(selected ? `/api/v1/web/operations/delivery/pools/${selected}/units?limit=100${state ? `&state=${encodeURIComponent(state)}` : ''}` : null)
  const visible = (units.data ?? []).filter(unit => `${unit.unit_id} ${unit.preview} ${unit.reserved_by ?? ''}`.toLowerCase().includes(query.toLowerCase()))
  const inputLines = contents.split(/\r?\n/).filter(line => line.trim()).length
  function changePool(value: string) { setPoolId(value); setContents(''); setNotice(''); setState(''); setQuery('') }
  async function importUnits(event: FormEvent) {
    event.preventDefault()
    if (busyRef.current || !selectedPool || !contents.trim()) return
    busyRef.current = true
    setBusy(true)
    setNotice('正在导入到当前库存池…')
    try {
      const response = await fetch(`/api/v1/web/operations/delivery/pools/${selected}/units`, {
        method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
        body: JSON.stringify({ contents }),
      })
      if (!response.ok) { setNotice('未取得导入成功确认，请刷新卡密记录核对。'); return }
      const payload = (await response.json()) as { data: { added: number; skipped_duplicates: number; skipped_blank: number } }
      setContents('')
      setNotice(`池「${selectedPool.name}」已导入 ${payload.data.added} 条，跳过重复 ${payload.data.skipped_duplicates} 条、空白 ${payload.data.skipped_blank} 行。`)
      units.reload(); pools.reload()
    } catch { setNotice('导入请求未取得明确结果，请刷新记录核对后再操作。') }
    finally { busyRef.current = false; setBusy(false) }
  }
  if (!dataPools.length) return <section><div className="section-heading"><div><h2>卡密导入</h2><p>只支持逐条卡密池；目录包含当前账号和全局范围。</p></div></div><PageState onRetry={pools.reload} loading={pools.loading} error={pools.error} emptyText="尚无逐条卡密池，请通过 CLI 创建后再导入。" /><NavLink className="text-action" to={`/inventory?account=${encodeURIComponent(accountId)}`}>查看库存池目录<Icon name="chevron-right" /></NavLink></section>
  return <>
    <div className="stock-pool-picker"><label>卡密库存池<select aria-label="卡密库存池" value={selected} disabled={busy} onChange={event => changePool(event.target.value)}>{dataPools.map(pool => <option key={pool.id} value={pool.id}>#{pool.id} {pool.name}{pool.account_scope ? '' : '（全局）'}</option>)}</select></label><NavLink className="text-action" to={`/inventory?account=${encodeURIComponent(accountId)}`}>返回库存目录<Icon name="chevron-right" /></NavLink></div>
    <div className="business-metrics"><div><span>池内可用单元</span><strong>{selectedPool.available}</strong></div><div><span>已预留单元</span><strong>{selectedPool.reserved}</strong></div><div><span>结果不明单元</span><strong>{selectedPool.uncertain}</strong></div></div>
    <div className="card-unit-workspace"><section><div className="section-heading"><div><p className="eyebrow">导入到 · {selectedPool.name}</p><h2>批量导入卡密</h2><p>一行一条，重复与空白由服务端跳过；切换池会清空输入。</p></div></div><form className="settings-form" onSubmit={importUnits}><label>卡密内容<textarea aria-label="批量卡密内容" rows={8} disabled={busy} value={contents} onChange={event => setContents(event.target.value)} placeholder="在这里粘贴卡密，每行一条" /></label><p className="field-help">输入包含 {inputLines} 条非空行，实际新增数量以导入结果为准。</p><div className="form-actions"><button className="primary-button" type="submit" disabled={busy || !contents.trim()}>{busy ? '导入中…' : '导入当前池'}</button></div></form>{notice ? <p className="operation-feedback" role="status">{notice}</p> : null}</section>
      <section><div className="section-heading"><div><h2>卡密记录</h2><p>按状态读取前 100 条，仅显示掩码；数量摘要是整个池的当前计数。</p></div><button type="button" disabled={busy} onClick={() => { units.reload(); pools.reload() }}>刷新卡密</button></div><div className="table-tools"><input aria-label="搜索卡密记录" placeholder="搜索单元 ID、掩码或占用引用" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="卡密单元状态" value={state} onChange={event => setState(event.target.value)}><option value="">全部状态</option>{Object.entries(unitStateLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></div>{visible.length ? <div className="stock-table-scroll"><table><thead><tr><th>单元</th><th>掩码预览</th><th>状态</th><th>占用引用 / 预留</th></tr></thead><tbody>{visible.map(unit => <tr key={unit.unit_id}><td>#{unit.unit_id}</td><td><code>{unit.preview}</code></td><td><span className={`status-badge ${unit.state === 'available' ? 'success' : unit.state === 'uncertain' ? 'warning' : 'neutral'}`}>{unitStateLabels[unit.state] ?? unit.state}</span></td><td>{unit.reserved_by ?? '—'}<p className="row-description">{unit.reservation_id ? `预留 #${unit.reservation_id}` : '无预留编号'}</p></td></tr>)}</tbody></table></div> : <PageState onRetry={units.reload} error={units.error} loading={units.loading} emptyText={query || state ? '当前读取窗口中没有匹配的卡密。' : '当前池还没有卡密，可在左侧导入。'} />}</section>
    </div>
  </>
}

function DeliveryTemplates() {
  const { data, error, loading, reload } = useProtectedData<DeliveryTemplateRow[]>('/api/v1/web/operations/delivery/templates')
  const [name, setName] = useState('')
  const [content, setContent] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [query, setQuery] = useState('')
  const [enabled, setEnabledFilter] = useState('all')
  const matching = data?.find(row => row.name === name.trim())
  const visible = (data ?? []).filter(row => `${row.name} ${row.content}`.toLowerCase().includes(query.toLowerCase()) && (enabled === 'all' || row.enabled === (enabled === 'enabled')))
  const variables = [{ name: 'buyer_name', label: '买家名称' }, { name: 'item_title', label: '商品标题' }, { name: 'sku_name', label: '规格名称' }, { name: 'order_id', label: '订单号' }, { name: 'quantity', label: '数量' }, { name: 'card', label: '卡密内容' }]
  async function perform(action: () => Promise<void>) {
    if (busyRef.current) return
    busyRef.current = true; setBusy(true)
    try { await action() } catch { setNotice('请求未取得明确结果，请刷新模板目录核对。') }
    finally { busyRef.current = false; setBusy(false) }
  }
  async function save() {
    const response = await fetch('/api/v1/web/operations/delivery/templates', {
      method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
      body: JSON.stringify({ name, content, enabled: true }),
    })
    if (!response.ok) { setNotice('模板未保存，请检查变量、正文长度和管理员会话。'); return }
    setNotice(`模板「${name.trim()}」已保存并启用；同名内容按现有规则覆盖。`)
    setName(''); setContent(''); reload()
  }
  async function setEnabled(row: DeliveryTemplateRow) {
    const response = await fetch(`/api/v1/web/operations/delivery/templates/${row.id}/enabled`, {
      method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
      body: JSON.stringify({ enabled: !row.enabled }),
    })
    setNotice(response.ok ? `模板「${row.name}」已${row.enabled ? '停用' : '启用'}。` : '模板状态未更新，请核对会话与目录。')
    if (response.ok) reload()
  }
  function loadTemplate(row: DeliveryTemplateRow) { setName(row.name); setContent(row.content); setNotice(`已载入「${row.name}」供编辑；尚未保存。`) }
  return <>
    {notice ? <p className="operation-feedback" role="status">{notice}</p> : null}
    <div className="template-workspace">
      <section><div className="section-heading"><div><p className="eyebrow">发货文案</p><h2>{matching ? '编辑已有模板' : '创建发货模板'}</h2><p>全局模板按名称供策略引用；保存会启用，同名会覆盖。</p></div></div>
        <form className="settings-form" onSubmit={event => { event.preventDefault(); void perform(save) }}><label>模板名称<input aria-label="模板名称" required maxLength={128} value={name} disabled={busy} onChange={event => setName(event.target.value)} placeholder="例如：标准卡密交付" /></label><label>模板内容<textarea aria-label="模板内容" required maxLength={2000} rows={6} value={content} disabled={busy} onChange={event => setContent(event.target.value)} placeholder="你好 {buyer_name}，卡密：{card}" /></label><p className="field-help">{content.length} / 2000 字符。{matching ? `将覆盖「${matching.name}」并启用，影响引用它的策略。` : '只能使用下方变量，不支持表达式或脚本。'}</p><div className="form-actions"><button className="primary-button" disabled={busy || loading || !!error || !name.trim() || !content.trim()} type="submit">{busy ? '处理中…' : matching ? '覆盖并启用模板' : '保存并启用模板'}</button><button type="button" disabled={busy} onClick={() => { setName(''); setContent(''); setNotice('') }}>清空编辑</button></div></form>
        <div className="template-variable-guide"><h3>可用变量</h3><div>{variables.map(variable => <span key={variable.name}><code>{`{${variable.name}}`}</code>{variable.label}</span>)}</div></div>
        <div className="template-preview"><header><h3>编辑正文预览</h3><span>变量保持原样</span></header><pre>{content || '输入模板内容后在这里预览排版。'}</pre></div>
      </section>
      <section><div className="section-heading"><div><h2>全局模板目录</h2><p>各账号共用；停用会阻止引用该模板的策略发货。</p></div><button type="button" disabled={busy} onClick={reload}>刷新模板</button></div><div className="table-tools"><input aria-label="搜索发货模板" placeholder="搜索模板名称或正文" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="发货模板状态" value={enabled} onChange={event => setEnabledFilter(event.target.value)}><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></div>
        {visible.length ? <div className="template-cards">{visible.map(row => <article className={matching?.id === row.id ? 'editing' : ''} key={row.id}><header><div><strong>{row.name}</strong><p>模板 #{row.id}</p></div><span className={`status-badge ${row.enabled ? 'success' : 'neutral'}`}>{row.enabled ? '已启用' : '已停用'}</span></header><pre className="saved-template-body">{row.content}</pre><p className="row-description">变量：{row.variables.map(variable => `{${variable}}`).join('、') || '纯文字模板'}</p>{row.description ? <p className="row-description">{row.description}</p> : null}<footer><button type="button" disabled={busy} onClick={() => loadTemplate(row)}>载入编辑</button><button type="button" disabled={busy} onClick={() => void perform(() => setEnabled(row))}>{row.enabled ? '停用' : '启用'}</button></footer></article>)}</div> : <PageState onRetry={reload} error={error} loading={loading} emptyText={query || enabled !== 'all' ? '没有匹配的发货模板。' : '尚无发货模板，可先在编辑区创建。'} />}
      </section>
    </div>
  </>
}

function AttentionPanel({ accountId, currentAccountId }: { accountId?: string; currentAccountId: string }) {
  const location = useLocation()
  const navigate = useNavigate()
  const { data, error, loading, reload } = useProtectedData<AttentionRow[]>(accountId ? `/api/v1/web/operations/attention?account_id=${encodeURIComponent(accountId)}` : '/api/v1/web/operations/attention')
  const [query, setQuery] = useState(() => { const params = new URLSearchParams(location.search); return !accountId || params.get('account') === accountId ? params.get('target') ?? '' : '' })
  const visible = (data ?? []).filter(row => `${row.target} ${row.account_id ?? ''} ${row.reason}`.toLowerCase().includes(query.toLowerCase()))
  function changeScope(value: string) { const params = new URLSearchParams(location.search); params.delete('target'); if (value === 'all') params.set('scope', 'all'); else { params.delete('scope'); params.set('account', currentAccountId) } setQuery(''); navigate({ pathname: location.pathname, search: params.toString() }) }
  return <section><div className="section-heading"><div><h2>待人工处理 · {accountId ?? '全部账号'}</h2><p>核对不明结果或完成验证；这里不会自动执行收尾。</p></div><button type="button" onClick={reload}>刷新待办</button></div><div className="table-tools"><input aria-label="搜索待办" placeholder="搜索对象、账号或原因" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="待办范围" value={accountId ? 'current' : 'all'} onChange={event => changeScope(event.target.value)}><option value="current">当前账号</option><option value="all">全部账号</option></select></div>{visible.length ? <div className="attention-cards">{visible.map((row, index) => <article key={`${row.kind}-${row.target}-${index}`}><header><div><strong>{attentionKindLabels[row.kind] ?? row.kind}</strong><p>{row.account_id ?? '全局'} · {row.target}</p></div><span className="status-badge warning">待人工处理</span></header><p>{row.reason}</p><p className="row-description">状态 {row.state} · {localTime(row.since)}</p><details><summary>查看处理命令</summary><code>{row.next_command}</code></details>{row.kind === 'account_risk' && row.account_id ? <NavLink className="text-action" to={`/accounts?account=${encodeURIComponent(row.account_id)}`}>查看账号状态<Icon name="chevron-right" /></NavLink> : null}</article>)}</div> : <PageState onRetry={reload} loading={loading} error={error} emptyText={query ? '没有匹配的待办。' : '当前范围没有需要人工处理的事项。'} />}</section>
}

function DeliveryPage() {
  const location = useLocation()
  const allAccounts = new URLSearchParams(location.search).get('scope') === 'all'
  return <AccountScopedPage title="发货管理">{accountId => <PageTabs tabs={[{ id: 'tasks', label: '发货工作区', render: () => <DeliveryJobsList accountId={accountId} /> }, { id: 'attention', label: '待人工处理', render: () => <AttentionPanel accountId={allAccounts ? undefined : accountId} currentAccountId={accountId} /> }, { id: 'records', label: '交付记录', render: () => <DeliveriesList accountId={accountId} /> }, { id: 'sources', label: '发货配置', render: () => <ApiDeliveryConfigs accountId={accountId} /> }, { id: 'cards', label: '卡密导入', render: () => <CardUnitsPanel accountId={accountId} /> }, { id: 'templates', label: '发货模板', render: () => <DeliveryTemplates /> }]} />}</AccountScopedPage>
}

const notificationKindLabels: Record<string, string> = { webhook: '通用 Webhook', dingtalk: '钉钉', feishu: '飞书', bark: 'Bark', telegram: 'Telegram' }
const attentionKindLabels: Record<string, string> = { delivery_job: '发货任务', automation_run: '平台自动化', account_risk: '账号风控' }
function NotificationChannels() {
  const { data, error, loading, reload } = useProtectedData<NotificationChannelRow[]>('/api/v1/web/operations/notify/channels')
  const [name, setName] = useState('')
  const [kind, setKind] = useState('webhook')
  const [target, setTarget] = useState('')
  const [secretRef, setSecretRef] = useState('')
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const matching = data?.find(row => row.name === name.trim())
  const effectiveKind = matching?.kind ?? kind
  const effectiveSecret = secretRef.trim() || matching?.secret_ref || null
  const needsSecret = effectiveKind === 'telegram' || effectiveKind === 'bark'
  const visible = (data ?? []).filter(row => `${row.name} ${row.kind} ${row.accounts.join(' ')}`.toLowerCase().includes(query.toLowerCase()) && (filter === 'all' || row.enabled === (filter === 'enabled')))
  async function perform(action: () => Promise<void>) { if (busyRef.current) return; busyRef.current = true; setBusy(true); setNotice('正在处理…'); try { await action() } catch { setNotice('请求结果无法确认，请刷新渠道目录核对后再操作。') } finally { busyRef.current = false; setBusy(false) } }
  async function save() {
    const response = await fetch('/api/v1/web/operations/notify/channels', { method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' }, body: JSON.stringify({ name: name.trim(), kind: effectiveKind, target: target.trim(), secret_ref: effectiveSecret, events: matching?.events ?? [], description: matching?.description ?? null, enabled: true }) })
    if (!response.ok) { setNotice('渠道未保存，请检查目标格式、密钥引用和管理员会话。'); return }
    setNotice(`渠道「${name.trim()}」已保存并启用；本次保存没有发送测试通知。`); setTarget(''); setSecretRef(''); reload()
  }
  async function setEnabled(row: NotificationChannelRow) {
    const response = await fetch(`/api/v1/web/operations/notify/channels/${row.id}/enabled`, { method: 'POST', headers: { 'content-type': 'application/json', 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' }, body: JSON.stringify({ enabled: !row.enabled }) })
    setNotice(response.ok ? `渠道「${row.name}」已${row.enabled ? '停用' : '启用'}。` : '渠道状态未更新，请刷新目录核对。'); if (response.ok) reload()
  }
  async function sendTest(row: NotificationChannelRow) {
    if (busyRef.current || !window.confirm(`将向渠道「${row.name}」发送一条真实测试通知。确认发送？`)) return
    await perform(async () => { const response = await fetch(`/api/v1/web/operations/notify/channels/${row.id}/test`, { method: 'POST', headers: { 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' } }); if (!response.ok) { setNotice('测试发送未取得成功确认，请核对接收端与发送记录。'); return } const payload = (await response.json()) as { data: { ok: boolean } }; setNotice(payload.data.ok === true ? `渠道「${row.name}」测试通知已发出，请核对接收端。` : '测试通知发送失败，请核对渠道配置；未自动重发。') })
  }
  async function remove(row: NotificationChannelRow) {
    if (busyRef.current || !window.confirm(`删除通知渠道「${row.name}」及其账号绑定？此操作不会删除历史发送记录。`)) return
    await perform(async () => { const response = await fetch(`/api/v1/web/operations/notify/channels/${row.id}`, { method: 'DELETE', headers: { 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' } }); setNotice(response.ok ? `渠道「${row.name}」已删除。` : '删除未取得成功确认，请刷新目录核对。'); if (response.ok) reload() })
  }
  function clearEditor() { setName(''); setKind('webhook'); setTarget(''); setSecretRef(''); setNotice('') }
  return <>{notice ? <p className="operation-feedback" role="status">{notice}</p> : null}
    <div className="business-metrics"><div><span>渠道总数</span><strong>{data?.length ?? '—'}</strong></div><div><span>已启用</span><strong>{data?.filter(row => row.enabled).length ?? '—'}</strong></div><div><span>已停用</span><strong>{data?.filter(row => !row.enabled).length ?? '—'}</strong></div></div>
    <div className="notify-workspace"><section><div className="section-heading"><div><p className="eyebrow">通知配置</p><h2>{matching ? '覆盖已有渠道' : '创建通知渠道'}</h2><p>保存后启用；测试通知由目录中的独立操作发送。</p></div></div><form className="settings-form" onSubmit={event => { event.preventDefault(); void perform(save) }}><label>渠道名称<input aria-label="通知渠道名称" required maxLength={128} disabled={busy} value={name} onChange={event => setName(event.target.value)} placeholder="例如：运营告警" /></label><label>渠道类型<select aria-label="通知渠道类型" value={effectiveKind} disabled={busy || !!matching} onChange={event => { setKind(event.target.value); setTarget(''); setNotice('') }}>{Object.entries(notificationKindLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><label>{effectiveKind === 'telegram' ? '接收会话 ID' : '通知目标地址'}<input aria-label="通知目标" required type={effectiveKind === 'telegram' ? 'text' : 'url'} maxLength={2048} disabled={busy} value={target} onChange={event => setTarget(event.target.value)} placeholder={effectiveKind === 'telegram' ? '填写 Telegram chat_id' : effectiveKind === 'bark' ? 'https://你的 Bark 服务地址' : 'https://...'} /></label><label>密钥引用<input aria-label="通知密钥引用" required={needsSecret && !matching?.secret_ref} maxLength={64} disabled={busy} value={secretRef} onChange={event => setSecretRef(event.target.value)} placeholder={matching?.secret_ref ? '留空沿用已有引用' : needsSecret ? '填写 SecretVault 引用' : '可选，填写 SecretVault 引用'} /></label><p className="field-help">{needsSecret ? '该渠道发送时需要密钥引用。' : '这里填写密钥库引用，不填写密钥正文。'}{matching ? ` 同名将覆盖目标并启用，保留原类型、订阅和账号绑定。` : ' 新渠道订阅全部待办类型，适用于全部账号。'}</p><div className="form-actions"><button type="submit" className="primary-button" disabled={busy || loading || !!error || !name.trim() || !target.trim() || (needsSecret && !effectiveSecret)}>{busy ? '处理中…' : matching ? '覆盖并启用渠道' : '保存并启用渠道'}</button><button type="button" disabled={busy} onClick={clearEditor}>清空编辑</button></div></form></section>
    <section><div className="section-heading"><div><h2>通知渠道</h2><p>目标只显示掩码；密钥仅显示引用。</p></div><button type="button" disabled={busy} onClick={reload}>刷新渠道</button></div><div className="table-tools"><input aria-label="搜索通知渠道" placeholder="搜索名称、类型或账号" value={query} onChange={event => setQuery(event.target.value)} /><select aria-label="通知渠道启用状态" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select></div>{visible.length ? <div className="notify-channel-cards">{visible.map(row => <article key={row.id}><header><div><strong>{row.name}</strong><p>#{row.id} · {notificationKindLabels[row.kind] ?? row.kind}</p></div><span className={`status-badge ${row.enabled ? 'success' : 'neutral'}`}>{row.enabled ? '已启用' : '已停用'}</span></header><dl className="detail-grid"><div><dt>目标掩码</dt><dd>{row.target_masked}</dd></div><div><dt>密钥引用</dt><dd>{row.secret_ref ?? '未配置'}</dd></div><div><dt>订阅事件</dt><dd>{row.events.map(value => attentionKindLabels[value] ?? value).join('、') || '全部待办'}</dd></div><div><dt>绑定账号</dt><dd>{row.accounts.join('、') || '全部账号'}</dd></div></dl><footer><button type="button" disabled={busy} onClick={() => void perform(() => setEnabled(row))}>{row.enabled ? '停用' : '启用'}</button><button type="button" disabled={busy} onClick={() => void sendTest(row)}>发送测试通知</button><button type="button" disabled={busy} onClick={() => void remove(row)}>删除渠道</button></footer></article>)}</div> : <PageState onRetry={reload} loading={loading} error={error} emptyText={query || filter !== 'all' ? '没有匹配的通知渠道。' : '尚未配置通知渠道，可在编辑区创建。'} />}</section></div>
  </>
}

function NotificationMessages() {
  const [onlyFailed, setOnlyFailed] = useState(false)
  const [query, setQuery] = useState('')
  const { data, error, loading, reload } = useProtectedData<NotificationMessageRow[]>(`/api/v1/web/operations/notify/messages?limit=50&only_failed=${onlyFailed}`)
  const visible = (data ?? []).filter(row => `${row.target} ${row.channel_id ?? '未关联渠道'} ${row.error ?? ''}`.toLowerCase().includes(query.toLowerCase()))
  return <section><div className="section-heading"><div><h2>通知发送记录</h2><p>读取最近 50 条发送事实；失败筛选先在服务端执行，页面不会自动重发。</p></div><button type="button" onClick={reload}>刷新记录</button></div><div className="table-tools"><input aria-label="搜索通知记录" placeholder="搜索对象、渠道 ID 或错误" value={query} onChange={event => setQuery(event.target.value)} /><label className="check-field"><input type="checkbox" checked={onlyFailed} onChange={event => setOnlyFailed(event.target.checked)} />只看失败</label></div>{visible.length ? <div className="notification-records">{visible.map(row => <article key={row.id}><header><div><strong>{attentionKindLabels[row.event] ?? row.event}</strong><p>{row.target}</p></div><span className={`status-badge ${row.ok ? 'success' : 'warning'}`}>{row.ok ? '发送成功' : '发送失败'}</span></header><p className="row-description">渠道 {row.channel_id ?? '未关联渠道'} · 状态 {row.state} · {localTime(row.created_at)}</p>{row.error ? <p className="notification-error">{row.error}</p> : null}</article>)}</div> : <PageState onRetry={reload} loading={loading} error={error} emptyText={query || onlyFailed ? '当前读取窗口中没有匹配的发送记录。' : '还没有通知发送记录。'} />}</section>
}

function NotifyPage() { return <main className="business-page"><header className="business-heading"><div><h1>通知管理</h1><p>配置运营通知渠道，核对发送结果。</p></div></header><PageTabs keepMounted tabs={[{ id: 'channels', label: '渠道配置', render: () => <NotificationChannels /> }, { id: 'records', label: '发送记录', render: () => <NotificationMessages /> }]} /></main> }

function InventoryPage() { return <AccountScopedPage title="卡密库存">{accountId => <PageTabs tabs={[{ id: 'pools', label: '库存池', render: () => <InventoryPoolsPanel accountId={accountId} /> }, { id: 'legacy', label: '原有库存卡', render: () => <InventoryList accountId={accountId} /> }]} />}</AccountScopedPage> }
function PlatformRunNowPanel({ accountId }: { accountId: string }) {
  const [notice, setNotice] = useState('')

  async function runNow(targetAction: string) {
    if (!window.confirm(`确认立刻执行一次 ${targetAction}？门禁与定时执行完全相同。`)) return
    setNotice(`正在排队 ${targetAction}…`)
    const response = await fetch('/api/v1/web/operations/platform-automation/run-now', {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '',
      },
      body: JSON.stringify({ account_id: accountId, action: targetAction }),
    })
    if (!response.ok) {
      setNotice('未排队：需要全局 active、动作已校准且已启用，并且当前不在静默时段。')
      return
    }
    const payload = (await response.json()) as { data: { work_id: string } }
    setNotice(`已排队 ${targetAction}：${payload.data.work_id}；daemon 会在下一轮调度执行。`)
  }

  return (
    <section>
      <h2>立即执行一次</h2>
      <p>不想等到定时点时可以在这里手动排一次;它不绕过任何门禁(全局 active、动作已校准且已启用、非静默时段),执行仍由 daemon 完成。</p>
      {notice ? <p role="status">{notice}</p> : null}
      <button type="button" onClick={() => runNow('polish')}>立即擦亮一次</button>
      <button type="button" onClick={() => runNow('request_flower')}>立即求花一次</button>
    </section>
  )
}

type LifecyclePlanRow = { item_id: string; action: string; reason: string }
type LifecyclePlanPayload = {
  counts: Record<string, number>
  scanned: number
  truncated: boolean
  rows: LifecyclePlanRow[]
}

function LifecyclePreviewPanel({ accountId }: { accountId: string }) {
  const [delistAfterDays, setDelistAfterDays] = useState('30')
  const [notice, setNotice] = useState('')
  const [plan, setPlan] = useState<LifecyclePlanPayload | null>(null)

  async function preview() {
    setNotice('正在 Dry Run…')
    const query = new URLSearchParams({ account_id: accountId, delist_after_days: delistAfterDays })
    const response = await fetch(`/api/v1/web/operations/item-lifecycle/plan?${query.toString()}`)
    if (!response.ok) {
      setNotice('Dry Run 失败，请检查账号与天数。')
      return
    }
    const payload = (await response.json()) as { data: LifecyclePlanPayload }
    setPlan(payload.data)
    setNotice(payload.data.truncated ? '本次只扫描了一部分商品，见下方提示。' : 'Dry Run 完成。')
  }

  return (
    <section>
      <h2>生命周期 Dry Run</h2>
      <p>只读预览「多久没动静会被下架」的判定，不会改任何商品。</p>
      {notice ? <p role="status">{notice}</p> : null}
      <label>多久没动就下架（天） <input type="number" min="1" value={delistAfterDays} onChange={(event) => setDelistAfterDays(event.target.value)} /></label>
      <button type="button" onClick={preview}>预览判定</button>
      {plan ? (
        <>
          <p>{Object.entries(plan.counts).map(([action, count]) => `${action} ${count}`).join(' / ')}（扫描 {plan.scanned} 件）</p>
          {plan.truncated ? <p role="status">注意：只扫描了前 {plan.scanned} 件在售商品，镜像里还有更多未纳入。</p> : null}
          <table><thead><tr><th>商品</th><th>动作</th><th>原因</th></tr></thead><tbody>
            {plan.rows.map((row) => <tr key={row.item_id}><td>{row.item_id}</td><td>{row.action}</td><td>{row.reason}</td></tr>)}
          </tbody></table>
        </>
      ) : null}
    </section>
  )
}

function RulesPage() { return <AccountScopedPage title="自动化与回复">{accountId => <PageTabs tabs={[{ id: 'schedule', label: '平台自动化', render: () => <PlatformAutomationPanel accountId={accountId} /> }, { id: 'run', label: '单次动作', render: () => <PlatformRunNowPanel accountId={accountId} /> }, { id: 'replies', label: '回复规则', render: () => <ReplyRulesList accountId={accountId} /> }]} />}</AccountScopedPage> }

function Login() {
  const [password, setPassword] = useState('')
  const [passwordVisible, setPasswordVisible] = useState(false)
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const submissionRef = useRef(false)
  const navigate = useNavigate()

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (submissionRef.current || !password) return
    submissionRef.current = true
    setSubmitting(true)
    setError('')
    try {
      const response = await fetch('/api/v1/web/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password }),
      })
      if (!response.ok) {
        const problem = await response.json().catch(() => null) as { detail?: { code?: string } } | null
        setError(response.status === 401 ? '密码不正确，请核对后重试。' : response.status === 503 && problem?.detail?.code === 'WEB_AUTH_UNCONFIGURED' ? '管理员登录尚未配置，请先完成后台配置。' : '暂时无法登录，请稍后重试。')
        return
      }
      const payload = (await response.json().catch(() => null)) as { data?: { authenticated?: boolean; csrf_token?: string } } | null
      if (payload?.data?.authenticated !== true || typeof payload.data.csrf_token !== 'string' || !payload.data.csrf_token) {
        setError('登录结果未确认，请重新尝试。')
        return
      }
      sessionStorage.setItem('xianyu_csrf', payload.data.csrf_token)
      setPassword('')
      setPasswordVisible(false)
      navigate('/dashboard', { replace: true })
    } catch {
      setError('无法连接本地服务，请检查连接后重试。')
    } finally {
      setSubmitting(false)
      submissionRef.current = false
    }
  }

  return <main className="login-page">
    <header className="login-brand"><strong>每日运营工作台</strong><span>闲鱼运营助手</span></header>
    <div className="login-layout">
      <section className="login-intro" aria-labelledby="login-intro-title"><p className="login-eyebrow">把日常经营，放在一起</p><h2 id="login-intro-title">每日运营，<br />从这里开始。</h2><p className="login-intro-copy">集中查看账号、消息与订单，<br className="login-desktop-break" />在需要时接管处理。</p><ul className="login-capabilities">{([['users', '账号与商品', '查看账号状态，管理商品信息。'], ['message', '客户沟通', '查看买家消息，接管重要会话。'], ['truck', '订单与交付', '核对订单与发货结果，处理待办。']] as const).map(([icon, title, description]) => <li key={icon}><Icon name={icon} /><div><strong>{title}</strong><p>{description}</p></div></li>)}</ul></section>
      <form onSubmit={submit} className="login-card" aria-labelledby="login-title" aria-busy={submitting}>
        <p className="login-eyebrow">欢迎回来</p><h1 id="login-title">管理员登录</h1><p className="login-card-description">登录后查看运营概览，处理消息与订单。</p>
        <label htmlFor="admin-password">管理员密码</label><div className="login-password-field"><input id="admin-password" name="password" aria-label="管理员密码" autoComplete="current-password" type={passwordVisible ? 'text' : 'password'} value={password} onChange={(event) => { setPassword(event.target.value); setError('') }} required maxLength={1024} disabled={submitting} placeholder="请输入管理员密码" aria-invalid={error === '密码不正确，请核对后重试。'} aria-describedby={error ? 'login-password-hint login-error' : 'login-password-hint'} /><button type="button" className="login-password-toggle" aria-label={passwordVisible ? '隐藏密码' : '显示密码'} aria-pressed={passwordVisible} disabled={submitting} onClick={() => setPasswordVisible(current => !current)}><Icon name="eye" /><span>{passwordVisible ? '隐藏' : '显示'}</span></button></div>
        <p id="login-password-hint" className="login-password-hint">使用本机配置的管理员密码。</p>
        {error ? <p id="login-error" role="alert" className="login-error">{error}</p> : null}
        <button type="submit" className="login-submit" disabled={submitting || !password}>{submitting ? '正在登录…' : '登录工作台'}<Icon name={submitting ? 'refresh' : 'chevron-right'} /></button>
        <p className="login-account-note">此处登录运营后台，闲鱼账号在后台内管理。</p>
      </form>
    </div><footer className="login-footer">人类与 Agent 共治的闲鱼运营工作台</footer>
  </main>
}

function PlaceholderPage({ label }: { label: string }) {
  return <main style={{ padding: 32 }}><h1>{label}</h1><p>该页面的受保护数据接口尚未实现。</p></main>
}

const navigationGroups = [{ title: '经营概览', paths: ['/dashboard'] }, { title: '闲鱼业务', paths: ['/accounts', '/items', '/orders', '/inventory', '/delivery'] }, { title: '客户沟通与自动化', paths: ['/messages', '/automation'] }, { title: '系统与运维', paths: ['/runtime', '/audit', '/notify', '/backup'] }]
function ConsoleNavigation({ name, account, onNavigate }: { name: string; account: string | null; onNavigate?: () => void }) {
  return <nav aria-label={name}>{navigationGroups.map(group => <div className="nav-group" key={group.title}><p>{group.title}</p>{links.filter(([path]) => group.paths.includes(path)).map(([path, label]) => <NavLink key={path} to={account ? `${path}?account=${encodeURIComponent(account)}` : path} onClick={onNavigate}><Icon name={navIcons[path]} /><span>{label}</span></NavLink>)}</div>)}</nav>
}
function Shell() {
  const location = useLocation()
  const scopeAccount = new URLSearchParams(location.search).get('account')
  const session = useProtectedData<{ authenticated: boolean }>('/api/v1/web/auth/session')
  const [loggedOut, setLoggedOut] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const [logoutError, setLogoutError] = useState('')
  const [navigationOpen, setNavigationOpen] = useState(false)
  const navigationRef = useRef<HTMLDialogElement>(null)
  const navigate = useNavigate()
  const closeNavigation = useCallback(() => { navigationRef.current?.close(); setNavigationOpen(false) }, [])
  useEffect(() => { navigationRef.current?.close() }, [location.pathname, location.search])
  useEffect(() => {
    const desktop = window.matchMedia(location.pathname === '/orders' ? '(min-width: 901px)' : '(min-width: 601px)')
    const closeOnDesktop = () => { if (desktop.matches) closeNavigation() }
    desktop.addEventListener('change', closeOnDesktop)
    return () => desktop.removeEventListener('change', closeOnDesktop)
  }, [closeNavigation, location.pathname])
  function openNavigation() { navigationRef.current?.showModal(); setNavigationOpen(true) }
  async function logout() {
    if (loggingOut) return
    setLoggingOut(true)
    setLogoutError('')
    try {
      const response = await fetch('/api/v1/web/auth/logout', {
        method: 'POST',
        headers: { 'X-CSRF-Token': sessionStorage.getItem('xianyu_csrf') ?? '' },
      })
      if (response.status !== 401) {
        if (!response.ok) { setLogoutError('退出未完成，请稍后重试。'); return }
        const result = await response.json() as { data?: { authenticated?: boolean } }
        if (result.data?.authenticated !== false) { setLogoutError('退出结果未确认，请重试。'); return }
      }
      sessionStorage.removeItem('xianyu_csrf')
      setLoggedOut(true)
      navigate('/login', { replace: true })
    } catch {
      setLogoutError('退出未确认，请检查连接后重试。')
    } finally {
      setLoggingOut(false)
    }
  }
  if (loggedOut || session.data && !session.data.authenticated) return <Navigate to="/login" replace />
  if (!session.data) return <main className="session-state"><h1>管理后台</h1><PageState loading={session.loading} error={session.error} emptyText="会话尚未确认。" onRetry={session.reload} /></main>
  return <AccountProfilesProvider><div className={`console-shell${location.pathname === '/orders' ? ' orders-shell' : ''}`}>
    <aside className="console-sidebar"><div className="console-brand"><strong>每日运营工作台</strong><span>闲鱼运营助手</span></div><ConsoleNavigation name="主导航" account={scopeAccount} /></aside>
    <dialog ref={navigationRef} id="mobile-navigation" className="console-mobile-menu" aria-labelledby="mobile-navigation-title" onCancel={() => setNavigationOpen(false)} onClose={() => setNavigationOpen(navigationRef.current?.open ?? false)} onClick={event => {
      if (event.target !== event.currentTarget) return
      const bounds = event.currentTarget.getBoundingClientRect()
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeNavigation()
    }}><header><div><h2 id="mobile-navigation-title">页面导航</h2><p>每日运营工作台</p></div><button type="button" autoFocus onClick={closeNavigation}>关闭导航</button></header><ConsoleNavigation name="手机主导航" account={scopeAccount} onNavigate={closeNavigation} /></dialog>
    <div className="console-content"><header className="console-topbar"><div className="console-topbar-context"><button type="button" className="mobile-navigation-button" aria-haspopup="dialog" aria-controls="mobile-navigation" aria-expanded={navigationOpen} onClick={openNavigation}>导航</button><span className="topbar-caption">{links.find(([path]) => path === location.pathname)?.[1] ?? '运营工作台'}</span></div><div className="topbar-actions"><CurrentAccountIdentity /><button type="button" onClick={logout} disabled={loggingOut}>{loggingOut ? '正在退出…' : '退出登录'}</button></div></header>
    {logoutError && <div className="error-banner" role="alert">{logoutError}</div>}
      <Routes><Route path="/dashboard" element={<Dashboard />} /><Route path="/accounts" element={<AccountsPage />} /><Route path="/messages" element={<MessagesPage />} /><Route path="/items" element={<ItemsPage />} /><Route path="/orders" element={<OrdersPage />} /><Route path="/inventory" element={<InventoryPage />} /><Route path="/delivery" element={<DeliveryPage />} /><Route path="/automation" element={<RulesPage />} /><Route path="/runtime" element={<RuntimePage />} /><Route path="/audit" element={<AuditPage />} /><Route path="/notify" element={<NotifyPage />} /><Route path="/backup" element={<BackupPage />} />{links.filter(([path]) => !['/dashboard', '/accounts', '/messages', '/items', '/orders', '/inventory', '/delivery', '/automation', '/runtime', '/audit', '/notify', '/backup'].includes(path)).map(([path, label]) => <Route key={path} path={path} element={<PlaceholderPage label={label} />} />)}</Routes>
    </div>
  </div></AccountProfilesProvider>
}

export default function WebApp() {
  return <BrowserRouter><Routes><Route path="/login" element={<Login />} /><Route path="/" element={<Navigate to="/dashboard" replace />} /><Route path="/*" element={<Shell />} /></Routes></BrowserRouter>
}
