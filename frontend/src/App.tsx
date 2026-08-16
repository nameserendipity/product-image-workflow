import {
  Activity,
  Bot,
  Check,
  CircleStop,
  FileSpreadsheet,
  FileImage,
  FolderOpen,
  Image as ImageIcon,
  KeyRound,
  Layers3,
  Link2,
  LoaderCircle,
  MessageSquareText,
  Monitor,
  PackageCheck,
  Play,
  Power,
  Send,
  Upload,
  Library,
  X,
} from 'lucide-react';
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { jsonRequest, request } from './api';
import type { ActionResponse, AppStatus, ChatMessage, GenerationMode, SupplementCategory, WorkflowName } from './types';
import SharedLibraryView from './SharedLibraryView';

const workflowMeta: Record<WorkflowName, { label: string; subtitle: string; Icon: typeof ImageIcon }> = {
  main: { label: '主图', subtitle: '首屏展示与卖点表达', Icon: ImageIcon },
  sku: { label: 'SKU 图', subtitle: '规格与款式识别', Icon: Layers3 },
  detail: { label: '详情图', subtitle: '结构化场景与细节', Icon: FileImage },
};

const emptyProgress = { analyzing: 0, prompt_ready: 0, generating: 0, completed: 0, failed: 0 };
const emptyBatch = { mode: 'image_search' as const, run_mode: 'full' as const, input_path: null, input_name: null, output_path: null, running: false, stop_requested: false, total: 0, completed: 0, collected: 0, failed: 0, stopped: 0, current: 0, valid: 0, invalid: 0, unsupported: 0, missing_images: 0, missing_links: 0, pairing_conflicts: 0, events: [] };
const perImageProgressLog = /批处理：(main|sku|detail) #\d+：(视觉提示词分析|提示词已生成|调用 gpt-image-2 生图)$/;

type SupplementControlsProps = {
  batch: AppStatus['batch'];
  supplement: AppStatus['supplement'];
  supplementWorkbook: string | null;
  supplementCategory: SupplementCategory;
  supplementCount: string;
  supplementPending: boolean;
  supplementSelectPending: boolean;
  onSubmit: (event: FormEvent) => void;
  onSelectWorkbook: () => void;
  onCategoryChange: (value: SupplementCategory) => void;
  onStop: () => void;
  onCountChange: (value: string) => void;
};

function SupplementControls({
  batch,
  supplement,
  supplementWorkbook,
  supplementCategory,
  supplementCount,
  supplementPending,
  supplementSelectPending,
  onSubmit,
  onSelectWorkbook,
  onCategoryChange,
  onCountChange,
  onStop,
}: SupplementControlsProps) {
  const available = Boolean(supplementWorkbook);
  return (
    <form className="supplement-panel" onSubmit={onSubmit}>
      <div className="supplement-heading"><FileImage size={17} /><span><strong>补充生成</strong><small>{supplement.running ? (supplement.stop_requested ? '正在停止补图，已完成图片会保留' : '主图、SKU 图、详情图可与其它任务并行') : '优先填入失败或缺失的原位置'}</small></span></div>
      <div className="supplement-workbook">
        <button className="button" type="button" disabled={supplementSelectPending || supplementPending} onClick={onSelectWorkbook}>
          {supplementSelectPending ? <LoaderCircle className="spin" size={16} /> : <FileSpreadsheet size={16} />}
          选择结果表格
        </button>
        <span title={supplementWorkbook || ''}>{supplementWorkbook ? fileName(supplementWorkbook) : '请选择此前导出的单商品表格'}</span>
      </div>
      <label><span>图片类型</span><select value={supplementCategory} onChange={event => onCategoryChange(event.target.value as SupplementCategory)}><option value="all">全部缺失</option><option value="main">主图</option><option value="sku">SKU 图</option><option value="detail">详情图</option></select></label>
      {supplementCategory !== 'all' && <label><span>补图数量</span><input type="number" min="1" max={supplementCategory === 'sku' ? 8 : supplementCategory === 'detail' ? 15 : 999} step="1" value={supplementCount} onChange={event => onCountChange(event.target.value)} /></label>}
      <button className="button primary" type="submit" disabled={!available || supplementPending}>{supplementPending ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}开始补图</button>
      <button className="button stop" type="button" disabled={!supplement.running || supplement.stop_requested} onClick={onStop}><CircleStop size={16} />停止补图</button>
    </form>
  );
}

function fileName(path: string | null): string {
  if (!path) return '';
  return path.split(/[\\/]/).pop() || path;
}

function getRunState(status: AppStatus | null, connected: boolean) {
  if (!connected) return { label: '连接中断', tone: 'error' };
  if (!status) return { label: '正在连接', tone: 'busy' };
  if (status.batch?.stop_requested) return { label: '正在停止批处理', tone: 'busy' };
  if (status.batch?.running) return { label: '正在批处理', tone: 'busy' };
  if (status.collection_stop_requested) return { label: '正在停止采集', tone: 'busy' };
  if (status.collecting) return { label: '正在采集', tone: 'busy' };
  if (status.generating) return { label: '正在生成', tone: 'busy' };
  if (status.collection_paused) return { label: '采集已暂停', tone: 'warning' };
  if (status.generated_count) return { label: '任务已完成', tone: 'success' };
  return { label: '等待操作', tone: 'idle' };
}

function App() {
  const [activeView, setActiveView] = useState<'link' | 'batch' | 'library'>(() => window.location.hash === '#batch' ? 'batch' : window.location.hash === '#library' ? 'library' : 'link');
  const [linkEntry, setLinkEntry] = useState<'single' | 'batch'>('single');
  const [directBatchMode, setDirectBatchMode] = useState<'direct_link' | 'direct_replace'>('direct_link');
  const [status, setStatus] = useState<AppStatus | null>(null);
  const [connected, setConnected] = useState(true);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [referenceUrl, setReferenceUrl] = useState('');
  const [referenceDirty, setReferenceDirty] = useState(false);
  const [browserChoice, setBrowserChoice] = useState('');
  const [chatText, setChatText] = useState('');
  const [visionKey, setVisionKey] = useState('');
  const [imageKey, setImageKey] = useState('');
  const [apiSetupOpen, setApiSetupOpen] = useState(false);
  const [apiSetupError, setApiSetupError] = useState('');
  const [urlPending, setUrlPending] = useState(false);
  const [chatPending, setChatPending] = useState(false);
  const [uploadPending, setUploadPending] = useState(false);
  const [batchUploadPending, setBatchUploadPending] = useState(false);
  const [batchStartPending, setBatchStartPending] = useState(false);
  const [singleExportPending, setSingleExportPending] = useState(false);
  const [supplementPending, setSupplementPending] = useState(false);
  const [supplementSelectPending, setSupplementSelectPending] = useState(false);
  const [supplementCategory, setSupplementCategory] = useState<SupplementCategory>('all');
  const [supplementCount, setSupplementCount] = useState('1');
  const [keyPending, setKeyPending] = useState(false);
  const [quantityPending, setQuantityPending] = useState(false);
  const [mainQuantityMode, setMainQuantityMode] = useState<'default' | 'reference' | 'custom'>('default');
  const [mainQuantityCount, setMainQuantityCount] = useState('10');
  const [shutdownPending, setShutdownPending] = useState(false);
  const [logsOpen, setLogsOpen] = useState(false);
  const messageId = useRef(0);
  const greeted = useRef(false);
  const mainQuantityEditing = useRef(false);
  const directBatchModeInitialized = useRef(false);
  const messagesContainer = useRef<HTMLDivElement>(null);
  const batchWorkbookInput = useRef<HTMLInputElement>(null);
  const directLinkWorkbookInput = useRef<HTMLInputElement>(null);
  const visibleLogs = useMemo(
    () => (status?.logs || []).filter(log => !perImageProgressLog.test(log)),
    [status?.logs],
  );

  const appendMessage = useCallback((role: ChatMessage['role'], text: string) => {
    setMessages(current => [...current, { id: ++messageId.current, role, text }]);
  }, []);

  const applyStatus = useCallback((next: AppStatus) => {
    setStatus(next);
    setConnected(true);
    if (!next.vision_api_ready || !next.image_api_ready) setApiSetupOpen(true);
    if (!referenceDirty) setReferenceUrl(next.agent.reference_url || '');
    setBrowserChoice(next.browser_choice || '');
    if (!directBatchModeInitialized.current && (next.batch.mode === 'direct_link' || next.batch.mode === 'direct_replace')) {
      directBatchModeInitialized.current = true;
      setDirectBatchMode(next.batch.mode);
    }
    if (!mainQuantityEditing.current) {
      setMainQuantityMode(next.agent.main_quantity_mode || (next.agent.max_main_images == null ? 'reference' : 'custom'));
      setMainQuantityCount(String(next.agent.max_main_images ?? 10));
    }
    if (!greeted.current) {
      greeted.current = true;
      appendMessage(
        'agent',
        next.agent.reference_url
          ? '已恢复当前任务。你可以继续补充生成数量、图片类型或停止条件。'
          : '请先提交对标商品链接，再告诉我需要主图、SKU 图、详情图中的哪些类型。',
      );
    }
  }, [appendMessage, referenceDirty]);

  const poll = useCallback(async () => {
    try {
      applyStatus(await request<AppStatus>('/api/status'));
    } catch {
      setConnected(false);
    }
  }, [applyStatus]);

  useEffect(() => {
    void poll();
    const timer = window.setInterval(() => void poll(), 1200);
    return () => window.clearInterval(timer);
  }, [poll]);

  useEffect(() => {
    const container = messagesContainer.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [messages]);

  const runState = getRunState(status, connected);
  const visibleRunState = activeView === 'link' && linkEntry === 'batch' && (status?.batch.mode === 'direct_link' || status?.batch.mode === 'direct_replace')
    ? status.batch.running
      ? { label: '正在批处理', tone: 'busy' }
      : status.batch.completed + status.batch.collected === status.batch.total && status.batch.total > 0
        ? { label: '批次已完成', tone: 'success' }
        : { label: '等待批处理', tone: 'idle' }
    : runState;
  const selectedWorkflows = status?.agent.workflows || [];
  const collected = status?.collected_summary || { main: 0, sku: 0, detail: 0, total: 0 };
  const batch = status?.batch || emptyBatch;
  const directBatchLoaded = batch.mode === directBatchMode;

  const queueMessage = useMemo(() => {
    if (!status) return '正在读取任务状态';
    if (status.batch?.running) return `正在处理第 ${status.batch.current || 1} / ${status.batch.total} 个商品，按原表顺序执行。`;
    if (status.collection_paused) return '采集已停止或失败。处理登录、验证或插件问题后，在对话中发送“继续采集”。';
    if (status.agent.generation_enabled === false) {
      return status.collecting ? '当前为仅采集模式，完成后不会自动生成。' : '当前任务已关闭自动生成。';
    }
    if (status.missing_workflows.length) {
      const names = status.missing_workflows.map(name => workflowMeta[name].label).join('、');
      return status.collecting ? `正在补采 ${names}，完成后自动进入生成。` : `缺少 ${names} 素材，生成暂未开始。`;
    }
    if (status.generating) return '视觉模型正在分析对标图，生成模型将按分析结果并发执行。';
    if (status.manifest_path) return '采集素材已就绪，条件齐全后会自动进入生成。';
    return '提交任务要求后，系统会自动采集、分析并生成。';
  }, [status]);

  async function submitReferenceUrl(event: FormEvent) {
    event.preventDefault();
    const value = referenceUrl.trim();
    if (!value || urlPending) return;
    setUrlPending(true);
    try {
      const body = await request<ActionResponse>('/api/reference-url', jsonRequest({ reference_url: value }));
      setReferenceDirty(false);
      appendMessage('agent', body.reply.message);
      applyStatus(body.status);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '商品链接提交失败。');
    } finally {
      setUrlPending(false);
    }
  }

  async function submitChat(event: FormEvent) {
    event.preventDefault();
    const value = chatText.trim();
    if (!value || chatPending) return;
    appendMessage('user', value);
    setChatText('');
    setChatPending(true);
    try {
      if (referenceDirty && referenceUrl.trim()) {
        const linkBody = await request<ActionResponse>(
          '/api/reference-url',
          jsonRequest({ reference_url: referenceUrl.trim() }),
        );
        setReferenceDirty(false);
        applyStatus(linkBody.status);
      }
      const body = await request<ActionResponse>('/api/chat', jsonRequest({ message: value }));
      appendMessage('agent', body.reply.message);
      applyStatus(body.status);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '消息发送失败。');
    } finally {
      setChatPending(false);
    }
  }

  async function uploadProductImage(file: File | undefined) {
    if (!file || uploadPending) return;
    setUploadPending(true);
    const form = new FormData();
    form.append('image', file);
    try {
      await request<{ path: string }>('/api/product-image', { method: 'POST', body: form });
      appendMessage('agent', `已加载我方产品图：${file.name}`);
      await poll();
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '产品图上传失败。');
    } finally {
      setUploadPending(false);
    }
  }

  async function stopTask(kind: 'collection' | 'generation') {
    try {
      const body = await request<{ status?: AppStatus }>(`/api/stop-${kind}`, jsonRequest({}));
      if (body.status) applyStatus(body.status);
      else await poll();
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '停止操作失败。');
    }
  }

  async function shutdownApplication() {
    if (shutdownPending) return;
    setShutdownPending(true);
    try {
      await request('/api/shutdown', jsonRequest({}));
      setConnected(false);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '退出程序失败。');
      setShutdownPending(false);
    }
  }

  async function uploadBatchWorkbook(file: File | undefined, mode: 'image_search' | 'direct_link' | 'direct_replace') {
    if (!file || batchUploadPending) return;
    setBatchUploadPending(true);
    const form = new FormData();
    form.append('workbook', file);
    form.append('batch_mode', mode);
    try {
      const body = await request<{ count: number; valid: number; invalid: number; unsupported: number; missing_images: number; missing_links: number; pairing_conflicts: number; status: AppStatus }>('/api/batch-upload', { method: 'POST', body: form });
      applyStatus(body.status);
      appendMessage('agent', mode === 'direct_link' || mode === 'direct_replace'
        ? `已识别 ${body.count} 行：有效 ${body.valid}，格式无效 ${body.invalid}，不支持平台 ${body.unsupported}，缺少商品图 ${body.missing_images}，缺少链接 ${body.missing_links}，配对冲突 ${body.pairing_conflicts}。`
        : `已识别 ${body.count} 个商品，确认后可开始批处理。`);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '表格上传失败。');
    } finally {
      setBatchUploadPending(false);
    }
  }

  function chooseBatchWorkbook(mode: 'image_search' | 'direct_link' | 'direct_replace') {
    if (batchUploadPending) return;
    const input = mode === 'direct_link' || mode === 'direct_replace' ? directLinkWorkbookInput.current : batchWorkbookInput.current;
    if (!input) return;
    input.value = '';
    input.click();
  }

  async function startBatch(runMode: 'full' | 'collect_only') {
    if (batchStartPending) return;
    setBatchStartPending(true);
    try {
      const body = await request<{ status: AppStatus }>('/api/batch-start', jsonRequest({ run_mode: runMode }));
      applyStatus(body.status);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '批处理启动失败。');
    } finally {
      setBatchStartPending(false);
    }
  }

  async function stopBatch() {
    try {
      const body = await request<{ status: AppStatus }>('/api/batch-stop', jsonRequest({}));
      applyStatus(body.status);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '停止批处理失败。');
    }
  }

  async function supplementBatchImage(event: FormEvent) {
    event.preventDefault();
    const count = Number(supplementCount);
    if (!status?.supplement_workbook) {
      appendMessage('agent', '请先选择此前导出的单商品结果表格。');
      return;
    }
    if (supplementCategory !== 'all' && (!Number.isInteger(count) || count < 1)) {
      appendMessage('agent', '补图数量必须是大于 0 的整数。');
      return;
    }
    setSupplementPending(true);
    try {
      const body = await request<{ status: AppStatus }>('/api/batch-supplement', jsonRequest({
        category: supplementCategory,
        ...(supplementCategory === 'all' ? {} : { count }),
      }));
      applyStatus(body.status);
      appendMessage('agent', supplementCategory === 'all'
        ? `已开始为 ${fileName(status.supplement_workbook)} 并发补齐主图、SKU 图和详情图的缺失图片。`
        : `已开始为 ${fileName(status.supplement_workbook)} 补充 ${workflowMeta[supplementCategory].label} ${count} 张，优先填入原空缺位置。`);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '补充生成启动失败。');
    } finally {
      setSupplementPending(false);
    }
  }

  async function stopSupplement() {
    try {
      const body = await request<{ status: AppStatus }>('/api/batch-supplement-stop', jsonRequest({}));
      applyStatus(body.status);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '停止补图失败。');
    }
  }

  async function selectSupplementWorkbook() {
    if (supplementSelectPending || supplementPending) return;
    setSupplementSelectPending(true);
    try {
      const body = await request<{ status: AppStatus }>('/api/supplement-select', jsonRequest({}));
      applyStatus(body.status);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '选择结果表格失败。');
    } finally {
      setSupplementSelectPending(false);
    }
  }

  async function openFolder(kind: 'collected' | 'generated' | 'batch') {
    try {
      await request('/api/open-folder', jsonRequest({ kind }));
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '无法打开文件夹。');
    }
  }

  async function exportSingleWorkbook() {
    if (singleExportPending) return;
    setSingleExportPending(true);
    try {
      const body = await request<{ workbook: string }>('/api/export-single', jsonRequest({}));
      appendMessage('agent', `表格已导出：${fileName(body.workbook)}`);
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '单个商品表格导出失败。');
    } finally {
      setSingleExportPending(false);
    }
  }

  async function saveApiKeys(event: FormEvent) {
    event.preventDefault();
    if (!visionKey.trim() || !imageKey.trim() || keyPending) return;
    setKeyPending(true);
    setApiSetupError('');
    try {
      await request('/api/api-keys', jsonRequest({ vision_api_key: visionKey.trim(), image_api_key: imageKey.trim() }));
      setVisionKey('');
      setImageKey('');
      setApiSetupOpen(false);
      appendMessage('agent', '视觉模型和生图模型 API Key 已保存到本机配置。');
      await poll();
    } catch (error) {
      const message = error instanceof Error ? error.message : 'API Key 保存失败。';
      setApiSetupError(message);
      appendMessage('agent', message);
    } finally {
      setKeyPending(false);
    }
  }

  async function persistMainQuantity(mode: typeof mainQuantityMode, count: number | null) {
    if (quantityPending) return;
    setQuantityPending(true);
    try {
      const body = await request<{ status: AppStatus }>('/api/main-quantity', jsonRequest({
        mode,
        count,
      }));
      mainQuantityEditing.current = false;
      applyStatus(body.status);
      appendMessage('agent', mode === 'default' ? '已恢复默认：主图生成 10 张。' : mode === 'reference' ? '已设置为按对标商品主图实际数量生成。' : `已设置为生成 ${count} 张主图。`);
    } catch (error) {
      mainQuantityEditing.current = false;
      await poll();
      appendMessage('agent', error instanceof Error ? error.message : '主图数量设置失败。');
    } finally {
      setQuantityPending(false);
    }
  }

  function handleMainQuantityModeChange(mode: typeof mainQuantityMode) {
    setMainQuantityMode(mode);
    mainQuantityEditing.current = true;
    if (mode !== 'custom') void persistMainQuantity(mode, null);
  }

  async function saveMainQuantity(event: FormEvent) {
    event.preventDefault();
    const count = Number(mainQuantityCount);
    if (mainQuantityMode === 'custom' && (!Number.isInteger(count) || count < 1 || count > 999)) {
      appendMessage('agent', '主图数量请输入 1 到 999 的整数。');
      return;
    }
    await persistMainQuantity(mainQuantityMode, mainQuantityMode === 'custom' ? count : null);
  }

  async function saveBrowserChoice(choice: string) {
    if (!choice) return;
    try {
      await request('/api/browser-choice', jsonRequest({ browser_choice: choice }));
      appendMessage('agent', choice === 'waxiang' ? '采集浏览器已切换为挖象浏览器。' : '采集浏览器已切换为微软 Edge。');
      await poll();
    } catch (error) {
      setBrowserChoice(status?.browser_choice || '');
      appendMessage('agent', error instanceof Error ? error.message : '采集浏览器设置保存失败。');
    }
  }

  async function saveGenerationMode(mode: GenerationMode) {
    if (!status || status.agent.generation_mode === mode) return;
    try {
      const body = await request<{ status: AppStatus }>('/api/generation-mode', jsonRequest({ mode }));
      applyStatus(body.status);
      appendMessage('agent', mode === 'competitor_reference'
        ? '已切换为直接参考对标商品模式，不会自动开始生成。第一张有效主图将作为商品身份锚点。'
        : '已切换为我方产品图模式，不会自动开始生成。生成前需要上传我方产品图。');
    } catch (error) {
      appendMessage('agent', error instanceof Error ? error.message : '生成模式切换失败。');
    }
  }

  function navigate(view: 'link' | 'batch' | 'library') {
    setActiveView(view);
    window.history.replaceState(null, '', view === 'batch' ? '#batch' : view === 'library' ? '#library' : '#link');
  }

  return (
    <div className="app-frame">
      {apiSetupOpen && <div className="api-setup-backdrop">
        <section className="api-setup-dialog" role="dialog" aria-modal="true" aria-labelledby="api-setup-title">
          <div className="api-setup-heading">
            <div className="api-setup-icon"><KeyRound size={22} /></div>
            <div>
              <span className="section-kicker">启动前配置</span>
              <h2 id="api-setup-title">配置模型 API</h2>
            </div>
            {status?.vision_api_ready && status?.image_api_ready && <button className="icon-button" type="button" onClick={() => setApiSetupOpen(false)} title="关闭" aria-label="关闭模型 API 配置"><X size={18} /></button>}
          </div>
          <p>采集后的分析和图片生成都依赖模型 API。两个 Key 只保存在这台电脑的本地配置中，后续打开网页无需重复输入。</p>
          <form className="api-setup-form" onSubmit={saveApiKeys}>
            <label><span>视觉模型 API Key</span><input type="password" value={visionKey} onChange={event => setVisionKey(event.target.value)} placeholder="输入视觉模型 API Key" autoComplete="new-password" autoFocus /></label>
            <label><span>生图模型 API Key</span><input type="password" value={imageKey} onChange={event => setImageKey(event.target.value)} placeholder="输入生图模型 API Key" autoComplete="new-password" /></label>
            {apiSetupError && <p className="api-setup-error" role="alert">{apiSetupError}</p>}
            <button className="button primary" type="submit" disabled={!visionKey.trim() || !imageKey.trim() || keyPending}>{keyPending ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}保存并继续</button>
          </form>
          <small>配置文件不会提交到 Git，页面和状态接口也不会回传 Key 内容。</small>
        </section>
      </div>}
      <aside className="sidebar" aria-label="工作区导航">
        <div className="sidebar-brand">
          <div className="brand-mark"><PackageCheck size={20} strokeWidth={2.1} /></div>
          <div><strong>图像工作台</strong><span>本地自动化</span></div>
        </div>
        <nav className="sidebar-nav">
          <button className={activeView === 'link' ? 'active' : ''} type="button" onClick={() => navigate('link')} aria-current={activeView === 'link' ? 'page' : undefined}>
            <Link2 size={17} /><span>链接生图</span>
          </button>
          <button className={activeView === 'batch' ? 'active' : ''} type="button" onClick={() => navigate('batch')} aria-current={activeView === 'batch' ? 'page' : undefined}>
            <FileSpreadsheet size={17} /><span>表格生图</span>
          </button>
          <button className={activeView === 'library' ? 'active' : ''} type="button" onClick={() => navigate('library')} aria-current={activeView === 'library' ? 'page' : undefined}>
            <Library size={17} /><span>共享素材库</span>
          </button>
        </nav>
        <div className="sidebar-footer"><span className="sidebar-dot" />本地服务在线</div>
      </aside>

      <div className="app-main">
      <header className="topbar">
        <div className="brand-block">
          <div className="brand-mark"><PackageCheck size={20} strokeWidth={2.1} /></div>
          <div>
            <strong>商品图片工作台</strong>
            <span>采集 · 分析 · 生成</span>
          </div>
        </div>
        <div className="topbar-actions">
          <button className={`api-status-button ${status?.vision_api_ready && status?.image_api_ready ? 'ready' : 'missing'}`} type="button" onClick={() => { setApiSetupError(''); setApiSetupOpen(true); }}>
            <KeyRound size={16} />
            <span>模型 API</span>
            <strong>{status?.vision_api_ready && status?.image_api_ready ? '已配置' : '未配置'}</strong>
          </button>
          <div className={`run-status ${visibleRunState.tone}`}>
            {visibleRunState.tone === 'busy' ? <LoaderCircle className="spin" size={16} /> : <Activity size={16} />}
            <span>{visibleRunState.label}</span>
          </div>
          <button className="exit-button" type="button" onClick={() => void shutdownApplication()} disabled={shutdownPending} title="退出程序">
            <Power size={16} />
            <span>{shutdownPending ? '正在退出' : '退出程序'}</span>
          </button>
        </div>
      </header>

      <main className="page-shell">
        {activeView !== 'library' && <section className="task-header" aria-labelledby="task-title">
          <div>
            <span className="section-kicker">当前任务</span>
            <h1 id="task-title">创建商品图片</h1>
          </div>
          <p>{activeView === 'link'
            ? linkEntry === 'batch'
              ? '上传商品链接表格，按原表顺序直接采集指定商品、生成并导出结果。'
              : '提交对标链接，Agent 会自动采集、分析并调度三条工作流。'
            : '上传商品表格，按原表顺序批量采集、生成并导出结果。'}</p>
          {activeView === 'link' && linkEntry === 'single' && <div className="mode-switch" aria-label="商品身份来源">
            <span>商品身份</span>
            <div className="segmented-control">
              <button type="button" disabled={Boolean(status?.collecting || status?.generating || status?.batch.running)} className={(status?.agent.generation_mode || 'competitor_reference') === 'competitor_reference' ? 'active' : ''} onClick={() => void saveGenerationMode('competitor_reference')}>参考对标商品</button>
              <button type="button" disabled={Boolean(status?.collecting || status?.generating || status?.batch.running)} className={status?.agent.generation_mode === 'own_product' ? 'active' : ''} onClick={() => void saveGenerationMode('own_product')}>使用我方产品图</button>
            </div>
          </div>}
        </section>}

        {activeView === 'library' && <SharedLibraryView />}

        {activeView === 'link' && <>
        <div className="link-entry-switch" aria-label="链接任务类型">
          <div className="segmented-control">
            <button type="button" className={linkEntry === 'single' ? 'active' : ''} disabled={Boolean(status?.batch.running || status?.collecting || status?.generating)} onClick={() => setLinkEntry('single')}>单个链接</button>
            <button type="button" className={linkEntry === 'batch' ? 'active' : ''} disabled={Boolean(status?.batch.running || status?.collecting || status?.generating)} onClick={() => setLinkEntry('batch')}>批量链接表格</button>
          </div>
        </div>
        {linkEntry === 'single' ? <>
        <section className="setup-grid" aria-label="任务输入">
          <form className="input-panel link-panel" onSubmit={submitReferenceUrl}>
            <div className="panel-icon link"><Link2 size={19} /></div>
            <div className="input-copy">
              <label htmlFor="reference-url">对标商品链接</label>
              <span>支持淘宝、天猫、京东</span>
            </div>
            <div className="url-control">
              <input
                id="reference-url"
                type="url"
                autoComplete="off"
                value={referenceUrl}
                onChange={event => {
                  setReferenceUrl(event.target.value);
                  setReferenceDirty(true);
                }}
                placeholder="粘贴商品详情页链接"
              />
              <button className="button primary" type="submit" disabled={!referenceUrl.trim() || urlPending}>
                {urlPending ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}
                确认链接
              </button>
            </div>
            <span className={`input-state ${status?.agent.reference_url ? 'ready' : ''}`}>
              {referenceDirty && referenceUrl.trim()
                ? '新链接尚未提交'
                : status?.agent.reference_url
                  ? '链接已加入当前任务'
                  : '等待商品链接'}
            </span>
          </form>

          <label className={`input-panel upload-panel ${status?.product_image ? 'has-file' : ''}`}>
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp"
              onChange={event => void uploadProductImage(event.target.files?.[0])}
            />
            <div className="panel-icon upload"><Upload size={19} /></div>
            <div className="input-copy">
              <span className="field-label">我方产品图</span>
              <span>{status?.product_image ? fileName(status.product_image) : 'JPG、PNG 或 WebP'}</span>
            </div>
            <span className="upload-action">
              {uploadPending ? <LoaderCircle className="spin" size={16} /> : status?.product_image ? '更换' : '选择图片'}
            </span>
          </label>
        </section>
        <div className={`identity-note ${status?.identity_ready ? 'ready' : ''}`}>
          <span>{status?.identity_source === 'uploaded_product' ? '我方产品图' : '对标商品第一张主图'}</span>
          <strong>{status?.identity_ready ? '身份图已就绪' : '等待身份图'}</strong>
          <small>{status?.identity_source === 'uploaded_product' ? '当前生成会以你上传的产品图为唯一身份来源。' : '未上传产品图时，采集完成后自动使用第一张有效主图。'}</small>
        </div>

        <section className="workflow-section" aria-labelledby="workflow-title">
          <div className="section-heading">
            <div><span className="section-kicker">自动化链路</span><h2 id="workflow-title">三条工作流</h2></div>
            <span>{selectedWorkflows.length ? `已选择 ${selectedWorkflows.length} 条` : '等待 Agent 识别需求'}</span>
          </div>
          <div className="workflow-lanes">
            {(Object.keys(workflowMeta) as WorkflowName[]).map((name, index) => {
              const meta = workflowMeta[name];
              const progress = status?.workflow_progress[name] || emptyProgress;
              const active = progress.analyzing + progress.prompt_ready + progress.generating;
              const total = active + progress.completed + progress.failed;
              const selected = selectedWorkflows.includes(name);
              const missing = status?.missing_workflows.includes(name);
              const value = total ? Math.round(((progress.completed + progress.failed) / total) * 100) : 0;
              let stateLabel = '未选择';
              if (selected && status?.generating && active) stateLabel = `处理中 ${active}`;
              else if (selected && missing && status?.collecting) stateLabel = '正在采集';
              else if (selected && missing) stateLabel = '等待素材';
              else if (selected && collected[name]) stateLabel = `已采集 ${collected[name]} 张`;
              else if (selected) stateLabel = '准备执行';
              if (selected && progress.completed && !active) stateLabel = `已生成 ${progress.completed} 张`;
              return (
                <article className={`workflow-lane lane-${name} ${selected ? 'selected' : ''}`} key={name}>
                  <span className="lane-index">0{index + 1}</span>
                  <div className="lane-icon"><meta.Icon size={20} /></div>
                  <div className="lane-copy"><h3>{meta.label}</h3><p>{meta.subtitle}</p></div>
                  <div className="lane-metric"><strong>{collected[name]}</strong><span>采集</span></div>
                  <div className="lane-metric"><strong>{progress.completed}</strong><span>生成</span></div>
                  <div className="lane-progress" aria-label={`${meta.label}进度`}>
                    <div><span style={{ width: `${value}%` }} /></div>
                    <small>{stateLabel}</small>
                  </div>
                </article>
              );
            })}
          </div>
        </section>

        <div className="work-grid">
          <section className="assistant-panel" aria-labelledby="assistant-title">
            <div className="panel-heading">
              <div className="heading-icon"><Bot size={19} /></div>
              <div><span className="section-kicker">任务 Agent</span><h2 id="assistant-title">需求对话</h2></div>
              <span className="agent-state"><span />在线</span>
            </div>
            <div className="messages" aria-live="polite" ref={messagesContainer}>
              {messages.map(message => (
                <div className={`message ${message.role}`} key={message.id}>
                  <span className="message-role">{message.role === 'agent' ? 'Agent' : '我'}</span>
                  <p>{message.text}</p>
                </div>
              ))}
              {chatPending && <div className="message agent pending"><LoaderCircle className="spin" size={16} /><span>正在理解任务...</span></div>}
            </div>
            <form className="composer" onSubmit={submitChat}>
              <MessageSquareText size={18} />
              <input value={chatText} onChange={event => setChatText(event.target.value)} placeholder="例如：按对标数量生成主图和详情图" autoComplete="off" />
              <button type="submit" title="发送" aria-label="发送" disabled={!chatText.trim() || chatPending}><Send size={18} /></button>
            </form>
          </section>

          <aside className="control-rail" aria-label="任务控制">
            <section className="rail-section">
              <div className="rail-heading"><span>执行状态</span><strong>{runState.label}</strong></div>
              <p className="queue-message">{queueMessage}</p>
              <dl className="summary-list">
                <div><dt>采集素材</dt><dd>{collected.total} 张</dd></div>
                <div><dt>生成结果</dt><dd>{status?.generated_count || 0} 张</dd></div>
                <div><dt>主图数量</dt><dd>{status?.agent.max_main_images == null ? '按对标数量' : `${status?.agent.max_main_images} 张`}</dd></div>
                <div><dt>自动生成</dt><dd>{status?.agent.generation_enabled === false ? '已关闭' : '已开启'}</dd></div>
              </dl>
              <form className="quantity-control" onSubmit={saveMainQuantity}>
                <label htmlFor="main-quantity-mode">主图生成数量</label>
                <select id="main-quantity-mode" value={mainQuantityMode} disabled={quantityPending} onChange={event => handleMainQuantityModeChange(event.target.value as typeof mainQuantityMode)}>
                  <option value="default">默认 10 张</option><option value="reference">按对标数量</option><option value="custom">自定义数量</option>
                </select>
                {mainQuantityMode === 'custom' && <input type="number" min="1" max="999" step="1" value={mainQuantityCount} onChange={event => setMainQuantityCount(event.target.value)} aria-label="自定义主图数量" />}
                <button className="button primary" type="submit" disabled={quantityPending}>{quantityPending ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}应用数量</button>
                <small>聊天中明确说“主图 N 张”或“按对标数量”时，以聊天指令为准。</small>
              </form>
              <div className="stop-actions">
                <button className="button stop" type="button" disabled={!status?.collecting || status?.collection_stop_requested} onClick={() => void stopTask('collection')}><CircleStop size={16} />停止采集</button>
                <button className="button stop" type="button" disabled={!status?.generating} onClick={() => void stopTask('generation')}><CircleStop size={16} />停止生成</button>
              </div>
            </section>

            <section className="rail-section assets-section">
              <div className="rail-heading"><span>本地文件</span><strong>共 {collected.total + (status?.generated_count || 0)} 张</strong></div>
              <button className="folder-row" type="button" disabled={!status?.folders.collected} onClick={() => void openFolder('collected')}><FolderOpen size={18} /><span><strong>采集素材</strong><small>{collected.total ? `${collected.total} 张，已就绪` : '完成采集后可打开'}</small></span></button>
              <button className="folder-row" type="button" disabled={!status?.folders.generated} onClick={() => void openFolder('generated')}><FolderOpen size={18} /><span><strong>生成结果</strong><small>{status?.generated_count ? `${status.generated_count} 张，已就绪` : '完成生成后可打开'}</small></span></button>
              <button className="folder-row" type="button" disabled={!status?.manifest_path || status?.collecting || status?.generating || singleExportPending} onClick={() => void exportSingleWorkbook()}>
                {singleExportPending ? <LoaderCircle className="spin" size={18} /> : <FileSpreadsheet size={18} />}<span><strong>导出当前商品表格</strong><small>{singleExportPending ? '正在导出' : '采集或生成后均可导出'}</small></span>
              </button>
            </section>

            <section className="rail-section key-section key-launcher">
              <div><span><KeyRound size={17} />模型 API</span><strong className={status?.vision_api_ready && status?.image_api_ready ? 'ready' : ''}>{status?.vision_api_ready && status?.image_api_ready ? '已配置' : '未配置'}</strong></div>
              {status?.vision_api_error && <p className="api-error">{status.vision_api_error}</p>}
              <button className="button" type="button" onClick={() => { setApiSetupError(''); setApiSetupOpen(true); }}><KeyRound size={15} />{status?.vision_api_ready && status?.image_api_ready ? '更换模型 Key' : '配置模型 API'}</button>
              <small>保存到本机配置，刷新页面或重启服务后无需重复输入。</small>
            </section>

            <details className="rail-section browser-section">
              <summary><span><Monitor size={17} />采集浏览器</span><strong className={status?.browser_choice ? 'ready' : ''}>{status?.browser_label || '请选择'}</strong></summary>
              <label className="browser-picker"><span>选择采集浏览器</span><select value={browserChoice} onChange={event => { const choice = event.target.value; setBrowserChoice(choice); void saveBrowserChoice(choice); }}><option value="" disabled>请选择</option><option value="waxiang">挖象浏览器（店透视）</option><option value="edge">微软 Edge</option></select></label>
              <small>选择后自动检测并保存，单链接采集和 Excel 批处理均使用此浏览器。</small>
            </details>
          </aside>
        </div>
        </> : <section className="batch-view direct-link-batch">
          <section className="batch-section" aria-labelledby="direct-batch-title">
            <div className="batch-heading">
              <div className="batch-icon"><Link2 size={20} /></div>
              <div>
                <span className="section-kicker">链接直采队列</span>
                <h2 id="direct-batch-title">批量商品链接</h2>
              </div>
              <div className="batch-count">
                <strong>{directBatchLoaded ? batch.completed + batch.collected : 0}</strong>
                <span>/ {directBatchLoaded ? batch.total : 0} 已处理</span>
              </div>
            </div>
            <div className="generation-mode direct-batch-mode" aria-label="批量链接生成模式">
              <button
                type="button"
                className={directBatchMode === 'direct_link' ? 'active' : ''}
                disabled={batch.running || batchUploadPending}
                onClick={() => { directBatchModeInitialized.current = true; setDirectBatchMode('direct_link'); }}
              >参考对标商品创作</button>
              <button
                type="button"
                className={directBatchMode === 'direct_replace' ? 'active' : ''}
                disabled={batch.running || batchUploadPending}
                onClick={() => { directBatchModeInitialized.current = true; setDirectBatchMode('direct_replace'); }}
              >产品图批量替换</button>
            </div>
            <div className="direct-batch-note">
              <strong>{directBatchMode === 'direct_replace' ? '每行产品图是唯一商品身份' : '固定使用参考对标商品模式'}</strong>
              <span>{directBatchMode === 'direct_replace'
                ? '逐行配对一张我方产品图和一个对标链接，复用单链接我方产品图生成链路；无明确 SKU 依据时不会编造 SKU。'
                : '逐行直接采集指定商品，不搜同款、不从第二款商品补采；同步采集参数、SKU 颜色/规格/价格和原视频 URL。'}</span>
            </div>
            <div className="batch-toolbar">
              <input
                ref={directLinkWorkbookInput}
                className="batch-file-input"
                type="file"
                accept="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                onChange={event => void uploadBatchWorkbook(event.target.files?.[0], directBatchMode)}
              />
              <button className="button batch-upload" type="button" onClick={() => chooseBatchWorkbook(directBatchMode)} disabled={batchUploadPending}>
                {batchUploadPending ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
                {directBatchLoaded && batch.input_name ? '更换链接表格' : '选择链接表格'}
              </button>
              <div className="batch-file">
                <strong>{directBatchLoaded && batch.input_name ? '链接表格已载入' : '尚未选择链接表格'}</strong>
                <span>{directBatchLoaded ? batch.input_name || 'XLSX' : directBatchMode === 'direct_replace' ? '识别同一行的我方商品图与商品链接' : '识别商品链接 / 对标链接 / URL 列'}</span>
              </div>
              <div className="batch-progress" aria-label="链接批处理进度">
                <div><span style={{ width: `${directBatchLoaded && batch.total ? Math.round(((batch.completed + batch.collected) / batch.total) * 100) : 0}%` }} /></div>
                <small>{directBatchLoaded
                  ? batch.running ? `第 ${batch.current || 1} 行；${batch.run_mode === 'collect_only' ? '仅采集' : '采集并生成'}` : `已采集 ${batch.collected}，已生成 ${batch.completed}；有效 ${batch.valid}；缺图 ${batch.missing_images}，缺链接 ${batch.missing_links}，配对冲突 ${batch.pairing_conflicts}`
                  : '等待上传链接表格'}</small>
              </div>
              <button className="button" type="button" disabled={!directBatchLoaded || !batch.input_path || batch.running || batchStartPending} onClick={() => void startBatch('collect_only')}>
                {batchStartPending ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
                仅采集
              </button>
              <button className="button primary" type="button" disabled={!directBatchLoaded || !batch.input_path || batch.running || batchStartPending} onClick={() => void startBatch('full')}>
                {batchStartPending ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
                采集并生成
              </button>
              <button className="button stop" type="button" disabled={!directBatchLoaded || !batch.running || batch.stop_requested} onClick={() => void stopBatch()}><CircleStop size={16} />停止</button>
              <button className="button folder-button" type="button" disabled={!directBatchLoaded || !batch.output_path} onClick={() => void openFolder('batch')} title="打开批次文件夹"><FolderOpen size={17} /><span>打开文件夹</span></button>
            </div>
            <SupplementControls
              batch={batch}
              supplement={status?.supplement || { running: false, stop_requested: false, workbook: null, events: [], completed: 0, failed: 0 }}
              supplementWorkbook={status?.supplement_workbook || null}
              supplementCategory={supplementCategory}
              supplementCount={supplementCount}
              supplementPending={supplementPending}
              supplementSelectPending={supplementSelectPending}
              onSubmit={supplementBatchImage}
              onSelectWorkbook={() => void selectSupplementWorkbook()}
              onCategoryChange={setSupplementCategory}
              onCountChange={setSupplementCount}
              onStop={() => void stopSupplement()}
            />
          </section>
        </section>}
        </>}

        {activeView === 'batch' && <section className="batch-view">
        <section className="batch-section" aria-labelledby="batch-title">
          <div className="batch-heading">
            <div className="batch-icon"><FileSpreadsheet size={20} /></div>
            <div>
              <span className="section-kicker">批量任务</span>
              <h2 id="batch-title">Excel 商品队列</h2>
            </div>
            <div className="batch-count">
                <strong>{batch.mode === 'image_search' ? batch.completed + batch.collected : 0}</strong>
                <span>/ {batch.mode === 'image_search' ? batch.total : 0} 已处理</span>
            </div>
          </div>
          <div className="batch-toolbar">
            <input
              ref={batchWorkbookInput}
              className="batch-file-input"
              type="file"
              accept="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={event => void uploadBatchWorkbook(event.target.files?.[0], 'image_search')}
            />
            <button className="button batch-upload" type="button" onClick={() => chooseBatchWorkbook('image_search')} disabled={batchUploadPending}>
              {batchUploadPending ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}
              {batch.mode === 'image_search' && batch.input_name ? '更换表格' : '选择表格'}
            </button>
            <div className="batch-file">
              <strong>{batch.mode === 'image_search' && batch.input_name ? '批处理表格已载入' : '尚未选择表格'}</strong>
              <span>{batch.mode === 'image_search' ? batch.input_name || 'XLSX' : '上传内嵌商品图表格'}</span>
            </div>
            <div className="batch-progress" aria-label="批处理进度">
              <div>
                <span style={{ width: `${batch.mode === 'image_search' && batch.total ? Math.round(((batch.completed + batch.collected) / batch.total) * 100) : 0}%` }} />
              </div>
              <small>{batch.mode !== 'image_search' ? '等待上传商品图片表格' : batch.running ? `第 ${batch.current || 1} 个商品；${batch.run_mode === 'collect_only' ? '仅采集' : '采集并生成'}` : batch.stopped ? `已暂停，可从第 ${batch.current || 1} 个继续` : batch.failed ? `${batch.failed} 个失败，点击继续重试` : batch.completed || batch.collected ? `已采集 ${batch.collected}，已生成 ${batch.completed}` : '等待开始'}</small>
            </div>
            <button className="button" type="button" disabled={batch.mode !== 'image_search' || !batch.input_path || batch.running || batchStartPending} onClick={() => void startBatch('collect_only')}>
              {batchStartPending ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              仅采集
            </button>
            <button className="button primary" type="button" disabled={batch.mode !== 'image_search' || !batch.input_path || batch.running || batchStartPending} onClick={() => void startBatch('full')}>
              {batchStartPending ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              采集并生成
            </button>
            <button className="button stop" type="button" disabled={batch.mode !== 'image_search' || !batch.running || batch.stop_requested} onClick={() => void stopBatch()}>
              <CircleStop size={16} />停止
            </button>
            <button className="button folder-button" type="button" disabled={batch.mode !== 'image_search' || !batch.output_path} onClick={() => void openFolder('batch')} title="打开批次文件夹">
              <FolderOpen size={17} />
              <span>打开文件夹</span>
            </button>
          </div>
          <SupplementControls
          batch={batch}
          supplement={status?.supplement || { running: false, stop_requested: false, workbook: null, events: [], completed: 0, failed: 0 }}
            supplementWorkbook={status?.supplement_workbook || null}
            supplementCategory={supplementCategory}
            supplementCount={supplementCount}
            supplementPending={supplementPending}
            supplementSelectPending={supplementSelectPending}
            onSubmit={supplementBatchImage}
            onSelectWorkbook={() => void selectSupplementWorkbook()}
            onCategoryChange={setSupplementCategory}
          onCountChange={setSupplementCount}
          onStop={() => void stopSupplement()}
          />
        </section>
        </section>}

        {/* The link view owns the workflow lanes and controls; the batch view owns the Excel queue. */}
        {false && <section className="workflow-section" aria-labelledby="workflow-title">
          <div className="section-heading">
            <div><span className="section-kicker">自动化链路</span><h2 id="workflow-title">三条工作流</h2></div>
            <span>{selectedWorkflows.length ? `已选择 ${selectedWorkflows.length} 条` : '等待 Agent 识别需求'}</span>
          </div>
          <div className="workflow-lanes">
            {(Object.keys(workflowMeta) as WorkflowName[]).map((name, index) => {
              const meta = workflowMeta[name];
              const progress = status?.workflow_progress[name] || emptyProgress;
              const active = progress.analyzing + progress.prompt_ready + progress.generating;
              const total = active + progress.completed + progress.failed;
              const selected = selectedWorkflows.includes(name);
              const missing = status?.missing_workflows.includes(name);
              const value = total ? Math.round(((progress.completed + progress.failed) / total) * 100) : 0;
              let stateLabel = '未选择';
              if (selected && status?.generating && active) stateLabel = `处理中 ${active}`;
              else if (selected && missing && status?.collecting) stateLabel = '正在采集';
              else if (selected && missing) stateLabel = '等待素材';
              else if (selected && collected[name]) stateLabel = `已采集 ${collected[name]} 张`;
              else if (selected) stateLabel = '准备执行';
              if (selected && progress.completed && !active) stateLabel = `已生成 ${progress.completed} 张`;
              return (
                <article className={`workflow-lane lane-${name} ${selected ? 'selected' : ''}`} key={name}>
                  <span className="lane-index">0{index + 1}</span>
                  <div className="lane-icon"><meta.Icon size={20} /></div>
                  <div className="lane-copy"><h3>{meta.label}</h3><p>{meta.subtitle}</p></div>
                  <div className="lane-metric"><strong>{collected[name]}</strong><span>采集</span></div>
                  <div className="lane-metric"><strong>{progress.completed}</strong><span>生成</span></div>
                  <div className="lane-progress" aria-label={`${meta.label}进度`}>
                    <div><span style={{ width: `${value}%` }} /></div>
                    <small>{stateLabel}</small>
                  </div>
                </article>
              );
            })}
          </div>
        </section>}

        {false && <div className="work-grid">
          <section className="assistant-panel" aria-labelledby="assistant-title">
            <div className="panel-heading">
              <div className="heading-icon"><Bot size={19} /></div>
              <div><span className="section-kicker">任务 Agent</span><h2 id="assistant-title">需求对话</h2></div>
              <span className="agent-state"><span />在线</span>
            </div>
            <div className="messages" aria-live="polite" ref={messagesContainer}>
              {messages.map(message => (
                <div className={`message ${message.role}`} key={message.id}>
                  <span className="message-role">{message.role === 'agent' ? 'Agent' : '我'}</span>
                  <p>{message.text}</p>
                </div>
              ))}
              {chatPending && <div className="message agent pending"><LoaderCircle className="spin" size={16} /><span>正在理解任务...</span></div>}
            </div>
            <form className="composer" onSubmit={submitChat}>
              <MessageSquareText size={18} />
              <input
                value={chatText}
                onChange={event => setChatText(event.target.value)}
                placeholder="例如：按对标数量生成主图和详情图"
                autoComplete="off"
              />
              <button type="submit" title="发送" aria-label="发送" disabled={!chatText.trim() || chatPending}>
                <Send size={18} />
              </button>
            </form>
          </section>

          <aside className="control-rail" aria-label="任务控制">
            <section className="rail-section">
              <div className="rail-heading"><span>执行状态</span><strong>{runState.label}</strong></div>
              <p className="queue-message">{queueMessage}</p>
              <dl className="summary-list">
                <div><dt>采集素材</dt><dd>{collected.total} 张</dd></div>
                <div><dt>生成结果</dt><dd>{status?.generated_count || 0} 张</dd></div>
                <div><dt>主图数量</dt><dd>{status?.agent.max_main_images == null ? '按对标数量' : `${status?.agent.max_main_images} 张`}</dd></div>
                <div><dt>自动生成</dt><dd>{status?.agent.generation_enabled === false ? '已关闭' : '已开启'}</dd></div>
              </dl>
              <form className="quantity-control" onSubmit={saveMainQuantity}>
                <label htmlFor="main-quantity-mode">主图生成数量</label>
                <select id="main-quantity-mode" value={mainQuantityMode} disabled={quantityPending} onChange={event => handleMainQuantityModeChange(event.target.value as typeof mainQuantityMode)}>
                  <option value="default">默认 10 张</option>
                  <option value="reference">按对标数量</option>
                  <option value="custom">自定义数量</option>
                </select>
                {mainQuantityMode === 'custom' && <input type="number" min="1" max="999" step="1" value={mainQuantityCount} onChange={event => setMainQuantityCount(event.target.value)} aria-label="自定义主图数量" />}
                <button className="button primary" type="submit" disabled={quantityPending}>
                  {quantityPending ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
                  应用数量
                </button>
                <small>聊天中明确说“主图 N 张”或“按对标数量”时，以聊天指令为准。</small>
              </form>
              <div className="stop-actions">
                <button
                  className="button stop"
                  type="button"
                  disabled={!status?.collecting || status?.collection_stop_requested}
                  onClick={() => void stopTask('collection')}
                >
                  <CircleStop size={16} />停止采集
                </button>
                <button
                  className="button stop"
                  type="button"
                  disabled={!status?.generating}
                  onClick={() => void stopTask('generation')}
                >
                  <CircleStop size={16} />停止生成
                </button>
              </div>
            </section>

            <section className="rail-section assets-section">
              <div className="rail-heading"><span>本地文件</span><strong>共 {collected.total + (status?.generated_count || 0)} 张</strong></div>
              <button className="folder-row" type="button" disabled={!status?.folders.collected} onClick={() => void openFolder('collected')}>
                <FolderOpen size={18} /><span><strong>采集素材</strong><small>{collected.total ? `${collected.total} 张，已就绪` : '完成采集后可打开'}</small></span>
              </button>
              <button className="folder-row" type="button" disabled={!status?.folders.generated} onClick={() => void openFolder('generated')}>
                <FolderOpen size={18} /><span><strong>生成结果</strong><small>{status?.generated_count ? `${status?.generated_count} 张，已就绪` : '完成生成后可打开'}</small></span>
              </button>
            </section>

            <section className="rail-section key-section key-launcher">
              <div><span><KeyRound size={17} />模型 API</span><strong className={status?.vision_api_ready && status?.image_api_ready ? 'ready' : ''}>{status?.vision_api_ready && status?.image_api_ready ? '已配置' : '未配置'}</strong></div>
              {status?.vision_api_error && <p className="api-error">{status?.vision_api_error}</p>}
              <button className="button" type="button" onClick={() => { setApiSetupError(''); setApiSetupOpen(true); }}><KeyRound size={15} />{status?.vision_api_ready && status?.image_api_ready ? '更换模型 Key' : '配置模型 API'}</button>
              <small>保存到本机配置，刷新页面或重启服务后无需重复输入。</small>
            </section>

            <details className="rail-section browser-section">
              <summary><span><Monitor size={17} />采集浏览器</span><strong className={status?.browser_choice ? 'ready' : ''}>{status?.browser_label || '请选择'}</strong></summary>
              <label className="browser-picker">
                <span>选择采集浏览器</span>
                <select
                  value={browserChoice}
                  onChange={event => {
                    const choice = event.target.value;
                    setBrowserChoice(choice);
                    void saveBrowserChoice(choice);
                  }}
                >
                  <option value="" disabled>请选择</option>
                  <option value="waxiang">挖象浏览器（店透视）</option>
                  <option value="edge">微软 Edge</option>
                </select>
              </label>
              <small>选择后自动检测并保存，单链接采集和 Excel 批处理均使用此浏览器。</small>
            </details>
          </aside>
        </div>}

        <section className={`log-panel ${logsOpen ? 'open' : ''}`}>
          <button className="log-toggle" type="button" onClick={() => setLogsOpen(value => !value)} aria-expanded={logsOpen}>
            <span><Activity size={17} />执行日志</span>
            <strong>{visibleLogs.length} 条记录</strong>
          </button>
          {logsOpen && <pre>{visibleLogs.length ? visibleLogs.join('\n') : '暂无执行记录'}</pre>}
        </section>
      </main>
      </div>
    </div>
  );
}

export default App;
