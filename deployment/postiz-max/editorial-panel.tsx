'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { EditorialForm, EditorialSettings, formFromSettings, safeSourceUrl, settingsFromForm } from './editorial-form';
import styles from './editorial.module.css';

// noVNC строит WebSocket URL от корня origin; префикс закрытого прокси задаём явно.
export const OWNER_BROWSER_URL = '/owner-browser/vnc.html?autoconnect=1&resize=scale&path=owner-browser/websockify';

type Recent = { id: string; title?: string; source?: string; state?: string; updated_at?: string | number; url?: string; error?: string };
type State = {
  revision: number; paused: boolean; phase?: string; updated_at?: string | number; next_run_at?: string | number;
  last_error?: { code: string; message?: string } | null; recent?: Recent[];
  capabilities?: { reddit?: { available?: boolean; reason?: string } };
};
const errors: Record<string, string> = {
  EDITORIAL_SERVICE_UNAVAILABLE: 'Редакционный сервис недоступен. Проверьте его состояние на сервере и обновите страницу.',
  EDITORIAL_OWNER_REQUIRED: 'Управление редакцией доступно только владельцу этой организации.',
  EDITORIAL_COOKIE_SESSION_REQUIRED: 'Войдите в Postiz обычным способом и обновите страницу.',
  EDITORIAL_ORIGIN_REQUIRED: 'Откройте редакцию с того же адреса, что и Postiz.',
  EDITORIAL_NOT_CONFIGURED: 'Подключение редакционного сервиса ещё не настроено на сервере.',
  EDITORIAL_REVISION_CONFLICT: 'Настройки уже изменились. Обновите их и повторите сохранение.',
  EDITORIAL_PAUSE_BEFORE_SETTINGS: 'Сначала поставьте цикл на паузу, затем сохраните настройки.',
  EDITORIAL_SETTINGS_INVALID: 'Настройки не прошли проверку. Проверьте диапазоны и списки источников.',
  EDITORIAL_AUTOMATIC_POLICY_REQUIRED: 'Автоматическая политика публикации ещё не подключена. Цикл остался на паузе.',
  REDDIT_APPROVAL_REQUIRED: 'Доступ Reddit или права на перевод и внешнюю публикацию ещё не подтверждены.',
  REDDIT_SETTINGS_INVALID: 'Проверьте настройки Reddit: имена сообществ и авторов, рейтинг и период.',
  INPUT_INVALID: 'Проверьте заполненные поля и обновите сохранённые настройки.',
  NASA_RATING_UNAVAILABLE: 'Научные источники не предоставляют проверенный рейтинг материалов. Рейтинг должен оставаться равным нулю.',
  EDITORIAL_SOURCE_POLICY_REQUIRED: 'Для выбранного источника не настроены проверенные права и политика обработки. Публикация заблокирована.',
  ARTICLE_RECONCILIATION_REQUIRED: 'Есть незавершённая отправка. Требуется сверка результата; повторная отправка заблокирована.',
  EDITORIAL_LEADERSHIP_LOST: 'Потеряно право управления циклом. Требуется сверка серверного процесса.',
  REDDIT_COLLECTION_CONFIG_INVALID: 'Проверьте настройки Reddit: имена сообществ и авторов, рейтинг и период.',
  REDDIT_COLLECTION_SCOPE_REQUIRED: 'Для Reddit укажите хотя бы одно сообщество или одного автора.',
  ARTICLE_QUALITY_REJECTED: 'Перевод отклонён проверкой качества. Автоматической публикации нет.',
  ARTICLE_SPELLING_CHECK_UNAVAILABLE: 'Проверка орфографии недоступна. Публикация заблокирована.',
  OPENCODE_GENERATION_FAILED: 'Модель не завершила обработку. Публикация заблокирована.',
  EDITORIAL_FORM_INVALID: 'Проверьте поля: интервал от 60 минут, целые неотрицательные рейтинги и допустимые списки.',
  REDDIT_ACCESS_REQUIRED: 'Для Reddit требуется разрешённый доступ к API.',
  REDDIT_RIGHTS_REQUIRED: 'Для Reddit требуется подтверждение прав на внешнюю публикацию.',
};
export function errorText(code: string) { return errors[code] || `Действие остановлено. Код: ${code}`; }
export function readableTime(value?: string | number) {
  if (value === undefined || value === null || value === '') return '—';
  const date = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleString('ru-RU');
}
const phases: Record<string, string> = {
  idle: 'Ожидание следующего цикла', paused: 'На паузе', collecting: 'Подбор источников', preparing: 'Перевод и проверка',
  translating: 'Перевод', reviewing: 'Независимая проверка', publishing: 'Публикация', verifying: 'Проверка доставки',
  needs_attention: 'Требуется проверка ошибки', blocked: 'Цикл остановлен проверками', ready: 'Готово к следующему циклу',
  automatic_ready: 'Черновик прошёл машинные проверки', staging_automatic_draft: 'Сохранение черновика в Postiz',
  automatic_scheduling: 'Назначение даты в Postiz', automatic_pending_approval: 'Проверка точной версии и даты',
  prepared: 'Подготовлен', pending: 'Ожидает отправки', scheduled: 'Запланирован', sending: 'Отправляется',
  published: 'Доставка подтверждена', published_unverified: 'Публикация пока не подтверждена',
  unknown: 'Исход неизвестен — требуется сверка', cancelled: 'Отменён', failed: 'Ошибка', retry_wait: 'Ожидание',
  awaiting_human_review: 'Архивный черновик для ручной проверки', awaiting_postiz: 'Ожидает подтверждения Postiz',
  selecting: 'Подбор новой статьи', scheduling: 'Назначение публикации', waiting_dispatch: 'Ожидание отправки и подтверждения',
  reconciling: 'Сверка состояний Postiz и MAX', rejected: 'Материал отклонён проверками',
  hour_used: 'В этом часу новая статья уже обрабатывалась', no_source: 'Подходящий материал не найден',
  selection_slot_expired: 'Час отбора завершился — ожидаем следующий', waiting_interval: 'Ожидание допустимого интервала между доставками',
};
export function phaseText(phase?: string) { return phases[phase || ''] || phase || '—'; }
export function sourceText(source?: string) {
  return ({ nasa_svs: 'NASA SVS', nasa_svs_articles: 'NASA SVS', open_science: 'Открытая наука', noirlab_iotw: 'NOIRLab', esahubble_potw: 'ESA/Hubble', reddit: 'Reddit' } as Record<string, string>)[source || ''] || source || '—';
}
async function api(path: string, method = 'GET', body?: unknown, signal?: AbortSignal) {
  // Нативный fetch не переносит cookie в auth-header и не раскрывает внутренний ключ.
  const response = await fetch(`/api/editorial/${path}`, {
    method, credentials: 'same-origin', cache: 'no-store', signal,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  });
  let result: any;
  try { result = await response.json(); } catch { throw new Error('EDITORIAL_RESPONSE_INVALID'); }
  if (!response.ok) throw new Error(typeof result?.code === 'string' ? result.code : 'EDITORIAL_REQUEST_FAILED');
  return result;
}
export function EditorialRecords({ records }: { records: Recent[] }) {
  return <div className={styles.tableWrap}><table className={styles.table}>
    <thead><tr><th>Материал</th><th>Источник</th><th>Состояние</th><th>Время</th></tr></thead>
    <tbody>{records.map(record => <tr key={record.id}>
      <td>{safeSourceUrl(record.url) ? <a href={safeSourceUrl(record.url)} target="_blank" rel="noopener noreferrer">{record.title || record.id}</a> : record.title || record.id}</td>
      <td>{sourceText(record.source)}</td><td>{phaseText(record.state)}{record.error && <span className={styles.detail}>{errorText(record.error)}</span>}</td><td>{readableTime(record.updated_at)}</td>
    </tr>)}</tbody></table>{!records.length && <p className={styles.muted}>Записей пока нет. История появится после первого цикла.</p>}</div>;
}

export function NasaRatingField({ mixed = false }: { mixed?: boolean } = {}) {
  return <label className={styles.field}><span>{mixed ? 'Рейтинг источника' : 'Рейтинг NASA'}</span>
    <input type="number" value="0" disabled readOnly />
    <span className={styles.muted}>Источник не предоставляет оценку</span>
  </label>;
}

export function EditorialPanel() {
  const [state, setState] = useState<State | null>(null);
  const [settings, setSettings] = useState<EditorialSettings | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [form, setForm] = useState<EditorialForm | null>(null);
  const [dirty, setDirty] = useState(false), [pending, setPending] = useState(false), [error, setError] = useState('');
  const mounted = useRef(true);
  const refresh = useCallback(async (loadSettings: boolean, signal?: AbortSignal) => {
    const current = await api('state', 'GET', undefined, signal);
    if (!mounted.current) return;
    setState(current);
    if (loadSettings) {
      const result = await api('settings', 'GET', undefined, signal);
      if (!mounted.current) return;
      setSettings(result.settings); setRevision(result.revision); setForm(formFromSettings(result.settings)); setDirty(false);
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    const abort = new AbortController();
    refresh(true, abort.signal).catch(reason => { if (!abort.signal.aborted) setError(reason.message); });
    const timer = setInterval(() => { refresh(false, abort.signal).catch(reason => { if (!abort.signal.aborted) setError(reason.message); }); }, 20000);
    return () => { mounted.current = false; abort.abort(); clearInterval(timer); };
  }, [refresh]);
  const change = (key: keyof EditorialForm, value: string) => { setForm(previous => previous && ({ ...previous, [key]: value })); setDirty(true); };
  const perform = async (operation: () => Promise<unknown>, resetSettings = false) => {
    setPending(true); setError('');
    try { await operation(); await refresh(resetSettings); } catch (reason) { setError(reason instanceof Error ? reason.message : 'EDITORIAL_REQUEST_FAILED'); }
    finally { setPending(false); }
  };
  const control = (action: 'start' | 'pause') => perform(() => api('control', 'POST', { action, revision: state?.revision }), !dirty);
  const field = (label: string, key: keyof EditorialForm, kind = 'text', help?: string) => <label className={styles.field}>
    <span>{label}</span>{kind === 'textarea'
      ? <textarea value={form?.[key] || ''} onChange={event => change(key, event.target.value)} disabled={pending} />
      : <input type={kind} value={form?.[key] || ''} onChange={event => change(key, event.target.value)} disabled={pending}
        min={key === 'interval' ? 60 : ['redditAge', 'redditLimit'].includes(key) ? 1 : 0}
        max={key === 'interval' ? 1440 : key === 'redditAge' ? 8760 : key === 'redditLimit' ? 100 : 1000000} step="1" required />}
    {help && <span className={styles.muted}>{help}</span>}
  </label>;
  return <main className={styles.panel}>
    <div className={styles.header}><div><h1>Редакция</h1><p className={styles.muted}>Подбор → перевод → проверка → календарь Postiz → MAX</p></div>
      <span className={`${styles.badge} ${state?.paused === false ? styles.running : styles.paused}`}>
        {!state ? 'Проверяем подключение' : state.paused ? 'На паузе' : 'Работает'}</span></div>
    {error && <div role="alert" className={`${styles.notice} ${styles.error}`}>{errorText(error)}</div>}
    <section className={styles.card} aria-labelledby="cycle-heading"><h2 id="cycle-heading">Управление циклом</h2>
      <p aria-live="polite">{state ? phaseText(state.phase) : 'Загрузка состояния…'}</p>
      <p className={styles.muted}>Последнее обновление: {readableTime(state?.updated_at)} · Следующий цикл: {readableTime(state?.next_run_at)}</p>
      {state?.last_error && <div className={styles.notice}>{errorText(state.last_error.code)}</div>}
      <div className={styles.actions}>
        <button className={`${styles.button} ${styles.primary}`} disabled={pending || !state || !state.paused || dirty} onClick={() => control('start')}>Запустить цикл</button>
        <button className={`${styles.button} ${styles.danger}`} disabled={pending || !state || state.paused} onClick={() => control('pause')}>Поставить на паузу</button>
        <button className={styles.button} disabled={pending} onClick={() => perform(() => Promise.resolve())}>Обновить статус</button>
        <a className={styles.button} href={OWNER_BROWSER_URL}>Вход в MAX</a>
      </div><p className={styles.muted}>Пауза сохраняет черновики и историю. Отправка с неизвестным исходом требует сверки и не повторяется автоматически.</p>
    </section>
    <section className={styles.card} aria-labelledby="settings-heading"><h2 id="settings-heading">Источники и отбор</h2>
      {form && settings ? <form onSubmit={event => { event.preventDefault(); perform(() => api('settings', 'PUT', { revision, settings: settingsFromForm(settings, form) }), true); }}>
        <div className={styles.grid}>
          <label className={styles.field}>Источник<select value={form.source} onChange={event => change('source', event.target.value)} disabled={pending}><option value="open_science">Открытая наука: NASA, NOIRLab, ESA/Hubble</option><option value="nasa_svs">NASA SVS</option><option value="reddit">Reddit</option></select></label>
          {field('Интервал между статьями, минут', 'interval', 'number', 'Не менее 60 минут. Догоняющих пачек после простоя нет.')}
        </div>
        {['nasa_svs', 'open_science'].includes(form.source) ? <div className={styles.grid}>
          {field(form.source === 'open_science' ? 'Авторы' : 'Авторы NASA', 'nasaAuthors', 'textarea', 'По одному имени на строку или через запятую. Пусто — любой автор.')}
          {field(form.source === 'open_science' ? 'Ключевые слова' : 'Ключевые слова NASA', 'nasaKeywords', 'textarea', 'Подойдёт любая из указанных фраз. Пусто — без фильтра по словам.')}
          <NasaRatingField mixed={form.source === 'open_science'} />
        </div> : <>
          {state?.capabilities?.reddit?.available !== true && <div className={styles.notice}>Reddit включается только после проверки доступа к API и прав на публикацию. Выбор источника сам по себе не снимает эти проверки.</div>}
          <div className={styles.grid}>
            {field('Сообщества Reddit', 'subreddits', 'textarea', 'Имена без r/. Нужно хотя бы сообщество или автор.')}
            {field('Авторы Reddit', 'redditAuthors', 'textarea', 'Имена без u/. При заполненных сообществах выбираются только эти авторы в этих сообществах.')}
            {field('Ключевые слова Reddit', 'redditKeywords', 'textarea', 'Любая фраза, без учёта регистра.')}
            {field('Минимальный рейтинг', 'redditRating', 'number')}{field('Минимум комментариев', 'redditComments', 'number')}
            {field('Возраст публикации, максимум часов', 'redditAge', 'number')}{field('Кандидатов за запрос', 'redditLimit', 'number')}
            <label className={styles.field}>Сортировка<select value={form.redditSort} onChange={event => change('redditSort', event.target.value)} disabled={pending}><option value="top">Лучшие</option><option value="new">Новые</option><option value="hot">Популярные сейчас</option></select></label>
            <label className={styles.field}>Период рейтинга<select value={form.redditPeriod} onChange={event => change('redditPeriod', event.target.value)} disabled={pending}>{[['hour', 'Час'], ['day', 'День'], ['week', 'Неделя'], ['month', 'Месяц'], ['year', 'Год'], ['all', 'За всё время']].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          </div>
        </>}
        <div className={styles.actions} style={{ marginTop: 20 }}>
          <button type="submit" className={`${styles.button} ${styles.primary}`} disabled={pending || !dirty}>Сохранить настройки</button>
          <button type="button" className={styles.button} disabled={pending} onClick={() => perform(() => Promise.resolve(), true)}>Загрузить сохранённые</button>
          {dirty && <span className={styles.muted}>Есть несохранённые изменения</span>}
        </div>
      </form> : <p className={styles.muted}>Загрузка настроек…</p>}
    </section>
    <section className={styles.card}><h2>Последние материалы</h2><EditorialRecords records={state?.recent || []} /></section>
  </main>;
}
