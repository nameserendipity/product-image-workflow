import {
  Download,
  FolderOpen,
  Image as ImageIcon,
  Layers3,
  Library,
  LoaderCircle,
  Search,
} from 'lucide-react';
import { FormEvent, useCallback, useEffect, useState } from 'react';
import { jsonRequest, request } from './api';
import type { SharedCatalogItem, SharedLibraryPage, SharedPackageKind } from './types';

const packageLabels: Record<SharedPackageKind, string> = {
  complete: '完整包',
  main: '主图',
  sku: 'SKU',
  detail: '详情',
};

function formatBytes(value: number): string {
  if (!value) return '-';
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : date.toLocaleString('zh-CN', { hour12: false });
}

export default function SharedLibraryView() {
  const [platform, setPlatform] = useState<'all' | 'taobao' | 'tmall'>('all');
  const [query, setQuery] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const [cursor, setCursor] = useState('');
  const [nextCursor, setNextCursor] = useState('');
  const [items, setItems] = useState<SharedCatalogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [pending, setPending] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    const params = new URLSearchParams();
    if (platform !== 'all') params.set('platform', platform);
    if (submittedQuery) params.set('query', submittedQuery);
    if (cursor) params.set('cursor', cursor);
    try {
      const page = await request<SharedLibraryPage>(`/api/shared-library?${params.toString()}`);
      setItems(page.items);
      setNextCursor(page.next_cursor);
    } catch (reason) {
      setItems([]);
      setNextCursor('');
      setError(reason instanceof Error ? reason.message : '共享素材库暂时不可用。');
    } finally {
      setLoading(false);
    }
  }, [cursor, platform, submittedQuery]);

  useEffect(() => { void load(); }, [load]);

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setCursor('');
    setSubmittedQuery(query.trim());
  }

  async function download(item: SharedCatalogItem, kind: SharedPackageKind) {
    const key = `${item.product_key}:${kind}`;
    setPending(key);
    setError('');
    try {
      await request('/api/shared-library/reuse', jsonRequest({
        product_key: item.product_key,
        package_kind: kind,
      }));
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '共享素材下载失败。');
    } finally {
      setPending('');
    }
  }

  async function openFolder(productKey: string) {
    setPending(`${productKey}:folder`);
    setError('');
    try {
      await request('/api/shared-library/open-folder', jsonRequest({ product_key: productKey }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法打开共享素材文件夹。');
    } finally {
      setPending('');
    }
  }

  return (
    <section className="shared-library-view" aria-labelledby="shared-library-title">
      <header className="shared-library-header">
        <div>
          <span className="section-kicker">私有 OSS 资产</span>
          <h1 id="shared-library-title">共享素材库</h1>
          <p>仅展示淘宝、天猫的参考对标商品创作结果。</p>
        </div>
        <div className="library-summary"><Library size={18} /><strong>{items.length}</strong><span>当前页</span></div>
      </header>

      <div className="library-tools">
        <div className="segmented-control" aria-label="平台筛选">
          {(['all', 'taobao', 'tmall'] as const).map(value => (
            <button
              type="button"
              key={value}
              className={platform === value ? 'active' : ''}
              onClick={() => { setCursor(''); setPlatform(value); }}
            >{value === 'all' ? '全部' : value === 'taobao' ? '淘宝' : '天猫'}</button>
          ))}
        </div>
        <form className="library-search" onSubmit={submitSearch}>
          <Search size={17} />
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索商品 ID" />
          <button className="button" type="submit">搜索</button>
        </form>
        <button className="icon-button" type="button" onClick={() => void load()} title="刷新素材库" aria-label="刷新素材库">
          {loading ? <LoaderCircle className="spin" size={18} /> : <Library size={18} />}
        </button>
      </div>

      {error && <div className="library-message error" role="alert">{error}</div>}
      {loading ? (
        <div className="library-message"><LoaderCircle className="spin" size={20} />正在读取共享素材...</div>
      ) : items.length === 0 ? (
        <div className="library-empty"><Library size={28} /><strong>没有匹配的共享素材</strong><span>完成一套标准参考对标商品任务后会自动发布到这里。</span></div>
      ) : (
        <div className="library-grid">
          {items.map(item => (
            <article className="library-card" key={item.product_key}>
              <div className="library-preview">
                <img src={item.preview_url} alt={`${item.platform === 'taobao' ? '淘宝' : '天猫'}商品 ${item.product_id} 预览`} loading="lazy" />
                <span>{item.platform === 'taobao' ? '淘宝' : '天猫'}</span>
              </div>
              <div className="library-card-body">
                <div className="library-product-key"><strong>{item.product_id}</strong><span>{formatDate(item.created_at)}</span></div>
                <div className="library-counts">
                  <span><ImageIcon size={14} />主图 {item.main_count}</span>
                  <span><Layers3 size={14} />SKU {item.sku_count}</span>
                  <span><ImageIcon size={14} />详情 {item.detail_count}</span>
                </div>
                <div className="library-card-footer">
                  <span>{formatBytes(item.package_size)}</span>
                  <div className="library-actions">
                    {item.available_packages.map(kind => (
                      <button
                        className={kind === 'complete' ? 'button primary' : 'icon-button'}
                        type="button"
                        key={kind}
                        disabled={Boolean(pending)}
                        onClick={() => void download(item, kind)}
                        title={`下载${packageLabels[kind]}`}
                        aria-label={`下载${packageLabels[kind]}`}
                      >
                        {pending === `${item.product_key}:${kind}` ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
                        {kind === 'complete' && <span>完整包</span>}
                      </button>
                    ))}
                    <button className="icon-button" type="button" disabled={!item.local_directory || Boolean(pending)} onClick={() => void openFolder(item.product_key)} title="打开本地文件夹" aria-label="打开本地文件夹">
                      {pending === `${item.product_key}:folder` ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={16} />}
                    </button>
                  </div>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}

      {nextCursor && <div className="library-pagination"><button className="button" type="button" onClick={() => setCursor(nextCursor)}>下一页</button></div>}
    </section>
  );
}
