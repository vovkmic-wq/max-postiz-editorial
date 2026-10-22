export type EditorialSettings = {
  interval_seconds?: number; source?: string;
  nasa?: Record<string, unknown>; reddit?: Record<string, unknown>;
  [key: string]: unknown;
};
export type EditorialForm = {
  source: string; interval: string; nasaKeywords: string; nasaAuthors: string;
  nasaRating: string; subreddits: string; redditAuthors: string; redditKeywords: string; redditRating: string;
  redditComments: string; redditAge: string; redditLimit: string; redditSort: string; redditPeriod: string;
};
const listText = (value: unknown) => Array.isArray(value) ? value.join('\n') : '';
export const splitList = (value: string) => [...new Set(value.split(/[,\n]/).map(item => item.trim()).filter(Boolean))];
export function formFromSettings(settings: EditorialSettings): EditorialForm {
  return {
    source: settings.source || 'nasa_svs', interval: String((settings.interval_seconds || 3600) / 60),
    nasaKeywords: listText(settings.nasa?.keywords), nasaAuthors: listText(settings.nasa?.authors),
    nasaRating: '0',
    subreddits: listText(settings.reddit?.subreddits), redditAuthors: listText(settings.reddit?.authors),
    redditKeywords: listText(settings.reddit?.keywords), redditRating: String(settings.reddit?.min_score ?? 0),
    redditComments: String(settings.reddit?.min_comments ?? 0), redditAge: String(settings.reddit?.max_age_hours ?? 72),
    redditLimit: String(settings.reddit?.limit ?? 25), redditSort: String(settings.reddit?.sort ?? 'top'),
    redditPeriod: String(settings.reddit?.time_filter ?? 'week'),
  };
}
export function settingsFromForm(settings: EditorialSettings, form: EditorialForm): EditorialSettings {
  const minutes = Number(form.interval), nasaRating = Number(form.nasaRating), redditRating = Number(form.redditRating);
  const comments = Number(form.redditComments), age = Number(form.redditAge), limit = Number(form.redditLimit);
  if (!['nasa_svs', 'open_science', 'reddit'].includes(form.source) || !Number.isSafeInteger(minutes) || minutes < 60 || minutes > 1440 ||
      nasaRating !== 0 || !Number.isSafeInteger(redditRating) || redditRating < 0 || redditRating > 1000000 ||
      !Number.isSafeInteger(comments) || comments < 0 || comments > 1000000 || !Number.isSafeInteger(age) || age < 1 || age > 8760 ||
      !Number.isSafeInteger(limit) || limit < 1 || limit > 100 || !['top', 'new', 'hot'].includes(form.redditSort) ||
      !['hour', 'day', 'week', 'month', 'year', 'all'].includes(form.redditPeriod)) {
    throw new Error('EDITORIAL_FORM_INVALID');
  }
  const lists = [form.nasaKeywords, form.nasaAuthors, form.subreddits, form.redditAuthors, form.redditKeywords].map(splitList);
  if (lists.some((items, index) => items.length > (index < 2 ? 50 : index === 4 ? 30 : 20) || items.some(item => item.length > 120)) ||
      (form.source === 'reddit' && !lists[2].length && !lists[3].length)) throw new Error('EDITORIAL_FORM_INVALID');
  // Не теряем дополнительные серверные настройки при сохранении видимых полей.
  return {
    ...settings, source: form.source, interval_seconds: minutes * 60,
    nasa: { ...settings.nasa, keywords: lists[0], authors: lists[1], min_rating: nasaRating },
    reddit: { ...settings.reddit, subreddits: lists[2], authors: lists[3], keywords: lists[4], min_score: redditRating,
      min_comments: comments, max_age_hours: age, limit, sort: form.redditSort, time_filter: form.redditPeriod },
  };
}
export function safeSourceUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return;
  try { const url = new URL(value); if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return url.href; } catch { /* Неверная ссылка остаётся обычным текстом. */ }
}
