/**
 * Market Data Service
 * Handles data provider integration with aggressive caching
 *
 * Strategy: Fetch on demand, cache permanently, never fetch twice
 *
 * Supports:
 * - Platform data (our API keys, shared across users)
 * - User data (their own API keys, isolated per user)
 *
 * Priority: User data > Platform data (for same symbol/date)
 */

import { query, queryOne, execute } from './database.js';
import { logError } from '../utils/errors.js';
import type { MarketDataDaily, MarketDataSymbol } from '../types/index.js';

// Supported data providers
export type DataProvider = 'alpha_vantage' | 'alpaca' | 'polygon';

export interface ProviderCredentials {
  provider: DataProvider;
  apiKey: string;
  apiSecret?: string;
}

export interface DataOwnership {
  ownerType: 'platform' | 'user';
  ownerId?: string; // user_id for user-owned, null for platform
}

// Rate limiting per provider
interface RateLimiter {
  lastRequestTime: number;
  minIntervalMs: number;
}

const rateLimiters: Record<string, RateLimiter> = {
  alpha_vantage: { lastRequestTime: 0, minIntervalMs: 12000 }, // 5/min to be safe
  alpaca: { lastRequestTime: 0, minIntervalMs: 200 }, // 200 req/min
  polygon: { lastRequestTime: 0, minIntervalMs: 100 }, // Varies by plan
};

// ============================================
// Common Data Format (normalized from any provider)
// ============================================

interface NormalizedDailyBar {
  date: string; // YYYY-MM-DD
  open: number;
  high: number;
  low: number;
  close: number;
  adjustedClose: number;
  volume: number;
  dividend: number;
  splitCoefficient: number;
}

interface NormalizedMarketData {
  symbol: string;
  bars: NormalizedDailyBar[];
}

// ============================================
// Alpha Vantage Types & Functions
// ============================================

interface AlphaVantageTimeSeriesDaily {
  'Meta Data': {
    '1. Information': string;
    '2. Symbol': string;
    '3. Last Refreshed': string;
    '4. Output Size': string;
    '5. Time Zone': string;
  };
  'Time Series (Daily)': Record<string, {
    '1. open': string;
    '2. high': string;
    '3. low': string;
    '4. close': string;
    '5. adjusted close': string;
    '6. volume': string;
    '7. dividend amount': string;
    '8. split coefficient': string;
  }>;
}

function normalizeAlphaVantageData(data: AlphaVantageTimeSeriesDaily): NormalizedMarketData {
  const timeSeries = data['Time Series (Daily)'];
  const bars: NormalizedDailyBar[] = [];

  for (const [date, bar] of Object.entries(timeSeries)) {
    bars.push({
      date,
      open: parseFloat(bar['1. open']),
      high: parseFloat(bar['2. high']),
      low: parseFloat(bar['3. low']),
      close: parseFloat(bar['4. close']),
      adjustedClose: parseFloat(bar['5. adjusted close']),
      volume: parseInt(bar['6. volume'], 10),
      dividend: parseFloat(bar['7. dividend amount']),
      splitCoefficient: parseFloat(bar['8. split coefficient']),
    });
  }

  // Sort by date ascending
  bars.sort((a, b) => a.date.localeCompare(b.date));

  return {
    symbol: data['Meta Data']['2. Symbol'],
    bars,
  };
}

/**
 * Apply rate limiting delay for a provider
 */
async function applyRateLimit(provider: string): Promise<void> {
  const limiter = rateLimiters[provider] || { lastRequestTime: 0, minIntervalMs: 1000 };
  const now = Date.now();
  const timeSinceLastRequest = now - limiter.lastRequestTime;

  if (timeSinceLastRequest < limiter.minIntervalMs) {
    const waitTime = limiter.minIntervalMs - timeSinceLastRequest;
    await new Promise(resolve => setTimeout(resolve, waitTime));
  }

  limiter.lastRequestTime = Date.now();
}

/**
 * Rate-limited fetch (simple GET)
 */
async function rateLimitedFetch(provider: string, url: string): Promise<Response> {
  await applyRateLimit(provider);
  return fetch(url);
}

/**
 * Rate-limited fetch with custom options (for providers needing headers)
 */
async function rateLimitedFetchWithOptions(
  provider: string,
  url: string,
  options: RequestInit
): Promise<Response> {
  await applyRateLimit(provider);
  return fetch(url, options);
}

/**
 * Fetch full historical data from Alpha Vantage
 */
async function fetchFromAlphaVantage(
  symbol: string,
  apiKey: string,
  outputSize: 'full' | 'compact' = 'full'
): Promise<AlphaVantageTimeSeriesDaily | null> {
  const url = new URL('https://www.alphavantage.co/query');
  url.searchParams.set('function', 'TIME_SERIES_DAILY_ADJUSTED');
  url.searchParams.set('symbol', symbol);
  url.searchParams.set('outputsize', outputSize);
  url.searchParams.set('apikey', apiKey);

  try {
    const response = await rateLimitedFetch('alpha_vantage', url.toString());

    if (!response.ok) {
      logError('fetchFromAlphaVantage', new Error(`HTTP ${response.status}`), { symbol });
      return null;
    }

    const data = await response.json() as Record<string, unknown>;

    if (data['Error Message']) {
      logError('fetchFromAlphaVantage', new Error(String(data['Error Message'])), { symbol });
      return null;
    }

    if (data['Note']) {
      logError('fetchFromAlphaVantage', new Error(`Rate limit: ${data['Note']}`), { symbol });
      return null;
    }

    if (!data['Time Series (Daily)']) {
      logError('fetchFromAlphaVantage', new Error('No data returned'), { symbol });
      return null;
    }

    return data as unknown as AlphaVantageTimeSeriesDaily;
  } catch (err) {
    logError('fetchFromAlphaVantage', err, { symbol });
    return null;
  }
}

// ============================================
// Alpaca Types & Functions
// ============================================

/**
 * Alpaca bar format from the Market Data API v2
 * Documentation: https://docs.alpaca.markets/reference/stockbars
 */
interface AlpacaBar {
  t: string;  // Timestamp (RFC-3339)
  o: number;  // Open
  h: number;  // High
  l: number;  // Low
  c: number;  // Close
  v: number;  // Volume
  n: number;  // Number of trades
  vw: number; // Volume weighted average price
}

interface AlpacaBarsResponse {
  bars: AlpacaBar[];
  symbol: string;
  next_page_token?: string;
}

/**
 * Fetch historical data from Alpaca
 * Uses the free tier which provides delayed data
 * For real-time data, users need an Alpaca brokerage account
 */
async function fetchFromAlpaca(
  symbol: string,
  apiKey: string,
  apiSecret: string,
  startDate: Date,
  endDate: Date
): Promise<NormalizedMarketData | null> {
  // Alpaca uses ISO format dates
  const start = startDate.toISOString().split('T')[0];
  const end = endDate.toISOString().split('T')[0];

  const allBars: NormalizedDailyBar[] = [];
  let pageToken: string | undefined;

  try {
    // Paginate through all results
    do {
      const url = new URL(`https://data.alpaca.markets/v2/stocks/${symbol}/bars`);
      url.searchParams.set('timeframe', '1Day');
      url.searchParams.set('start', start);
      url.searchParams.set('end', end);
      url.searchParams.set('limit', '10000'); // Max per request
      url.searchParams.set('adjustment', 'all'); // Get split and dividend adjusted data
      if (pageToken) {
        url.searchParams.set('page_token', pageToken);
      }

      // Rate-limited fetch with Alpaca auth headers
      const response = await rateLimitedFetchWithOptions('alpaca', url.toString(), {
        headers: {
          'APCA-API-KEY-ID': apiKey,
          'APCA-API-SECRET-KEY': apiSecret,
        },
      });

      if (!response.ok) {
        const errorText = await response.text();
        logError('fetchFromAlpaca', new Error(`HTTP ${response.status}: ${errorText}`), { symbol });
        return null;
      }

      const data = await response.json() as AlpacaBarsResponse;

      if (!data.bars || data.bars.length === 0) {
        if (allBars.length === 0) {
          logError('fetchFromAlpaca', new Error('No data returned'), { symbol });
          return null;
        }
        break;
      }

      // Convert Alpaca bars to normalized format
      for (const bar of data.bars) {
        const dateStr = bar.t.split('T')[0];
        allBars.push({
          date: dateStr,
          open: bar.o,
          high: bar.h,
          low: bar.l,
          close: bar.c,
          adjustedClose: bar.c, // Alpaca returns already adjusted data with adjustment=all
          volume: bar.v,
          dividend: 0, // Alpaca doesn't provide dividend data separately
          splitCoefficient: 1, // Already adjusted
        });
      }

      pageToken = data.next_page_token;
    } while (pageToken);

    // Sort by date ascending
    allBars.sort((a, b) => a.date.localeCompare(b.date));

    console.log(`[Alpaca] Fetched ${allBars.length} bars for ${symbol}`);
    return { symbol, bars: allBars };
  } catch (err) {
    logError('fetchFromAlpaca', err, { symbol });
    return null;
  }
}

function normalizeAlpacaData(data: AlpacaBarsResponse): NormalizedMarketData {
  const bars: NormalizedDailyBar[] = data.bars.map(bar => ({
    date: bar.t.split('T')[0],
    open: bar.o,
    high: bar.h,
    low: bar.l,
    close: bar.c,
    adjustedClose: bar.c,
    volume: bar.v,
    dividend: 0,
    splitCoefficient: 1,
  }));

  bars.sort((a, b) => a.date.localeCompare(b.date));
  return { symbol: data.symbol, bars };
}

// ============================================
// Cache Management
// ============================================

/**
 * Store normalized market data in the database with ownership tracking
 * Works with data from any provider (Alpha Vantage, Alpaca, etc.)
 */
async function cacheNormalizedData(
  data: NormalizedMarketData,
  provider: DataProvider,
  ownership: DataOwnership
): Promise<void> {
  const { symbol, bars } = data;
  if (bars.length === 0) return;

  const { ownerType, ownerId } = ownership;

  // Batch insert
  const batchSize = 500;
  for (let i = 0; i < bars.length; i += batchSize) {
    const batch = bars.slice(i, i + batchSize);
    const values: unknown[] = [];
    const placeholders: string[] = [];
    let paramIndex = 1;

    for (const bar of batch) {
      placeholders.push(
        `($${paramIndex}, $${paramIndex + 1}, $${paramIndex + 2}, $${paramIndex + 3}, $${paramIndex + 4}, $${paramIndex + 5}, $${paramIndex + 6}, $${paramIndex + 7}, $${paramIndex + 8}, $${paramIndex + 9}, $${paramIndex + 10}, $${paramIndex + 11}, $${paramIndex + 12})`
      );
      values.push(
        symbol,
        bar.date,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.adjustedClose,
        bar.volume,
        bar.dividend,
        bar.splitCoefficient,
        provider,
        ownerType,
        ownerId || null
      );
      paramIndex += 13;
    }

    // Use unique index for upsert (handles ownership)
    await execute(
      `INSERT INTO market_data_daily
       (symbol, date, open, high, low, close, adjusted_close, volume, dividend, split_coefficient, provider, owner_type, owner_id)
       VALUES ${placeholders.join(', ')}
       ON CONFLICT (symbol, date, owner_type, COALESCE(owner_id, '__platform__'))
       DO UPDATE SET
         open = EXCLUDED.open,
         high = EXCLUDED.high,
         low = EXCLUDED.low,
         close = EXCLUDED.close,
         adjusted_close = EXCLUDED.adjusted_close,
         volume = EXCLUDED.volume,
         dividend = EXCLUDED.dividend,
         split_coefficient = EXCLUDED.split_coefficient,
         provider = EXCLUDED.provider,
         fetched_at = NOW()`,
      values
    );
  }

  // Update symbol tracking table
  const firstDate = bars[0].date;
  const lastDate = bars[bars.length - 1].date;

  await execute(
    `INSERT INTO market_data_symbols (symbol, first_date, last_date, last_fetched_at, is_complete, provider, owner_type, owner_id)
     VALUES ($1, $2, $3, NOW(), true, $4, $5, $6)
     ON CONFLICT (symbol, owner_type, COALESCE(owner_id, '__platform__'))
     DO UPDATE SET
       first_date = LEAST(market_data_symbols.first_date, EXCLUDED.first_date),
       last_date = GREATEST(market_data_symbols.last_date, EXCLUDED.last_date),
       last_fetched_at = NOW(),
       is_complete = true`,
    [symbol, firstDate, lastDate, provider, ownerType, ownerId || null]
  );
}

/**
 * Get symbol tracking info for a specific owner
 */
async function getSymbolInfo(
  symbol: string,
  ownership: DataOwnership
): Promise<MarketDataSymbol | null> {
  const { ownerType, ownerId } = ownership;

  return queryOne<MarketDataSymbol>(
    `SELECT * FROM market_data_symbols
     WHERE symbol = $1 AND owner_type = $2 AND COALESCE(owner_id, '__platform__') = COALESCE($3, '__platform__')`,
    [symbol, ownerType, ownerId || null]
  );
}

// ============================================
// User Provider Management
// ============================================

/**
 * Cached user credentials (refreshed periodically)
 */
interface CachedCredentials {
  provider: string;
  apiKey: string;
  apiSecret: string;
}

interface UserCredentialsCache {
  credentials: CachedCredentials[];
  fetchedAt: number;
}

const credentialsCache: Record<string, UserCredentialsCache> = {};
const CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Fetch user credentials from the main app's internal API
 */
async function fetchUserCredentials(userId: string): Promise<CachedCredentials[]> {
  const cached = credentialsCache[userId];
  if (cached && Date.now() - cached.fetchedAt < CACHE_TTL_MS) {
    return cached.credentials;
  }

  const mainAppUrl = process.env.MAIN_APP_URL || 'http://localhost:3000';
  const apiSecret = process.env.LEAN_API_SECRET;

  if (!apiSecret) {
    console.warn('[MarketData] LEAN_API_SECRET not configured, cannot fetch user credentials');
    return [];
  }

  try {
    const response = await fetch(
      `${mainAppUrl}/api/internal/user-credentials?userId=${encodeURIComponent(userId)}`,
      {
        headers: {
          Authorization: `Bearer ${apiSecret}`,
        },
      }
    );

    if (!response.ok) {
      console.error(`[MarketData] Failed to fetch user credentials: ${response.status}`);
      return [];
    }

    const data = await response.json() as { credentials?: CachedCredentials[] };
    const credentials = data.credentials || [];

    // Cache the results
    credentialsCache[userId] = {
      credentials,
      fetchedAt: Date.now(),
    };

    return credentials;
  } catch (err) {
    console.error('[MarketData] Error fetching user credentials:', err);
    return [];
  }
}

/**
 * Get a user's credentials for a specific provider
 */
async function getUserProviderCredentials(
  userId: string,
  provider: DataProvider
): Promise<{ apiKey: string; apiSecret?: string } | null> {
  const credentials = await fetchUserCredentials(userId);
  const match = credentials.find(c => c.provider === provider);

  if (match) {
    return {
      apiKey: match.apiKey,
      apiSecret: match.apiSecret,
    };
  }

  return null;
}

/**
 * Credentials structure for different providers
 */
export interface ProviderCredentialsResult {
  apiKey: string;
  apiSecret?: string; // Required for Alpaca
  ownership: DataOwnership;
}

/**
 * Get credentials for a provider (user's or platform's)
 * Priority: User credentials > Platform credentials (env vars)
 */
async function getProviderCredentials(
  provider: DataProvider,
  userId?: string
): Promise<ProviderCredentialsResult | null> {
  // Try user's credentials first (fetched from main app via internal API)
  if (userId) {
    const userCreds = await getUserProviderCredentials(userId, provider);
    if (userCreds) {
      console.log(`[MarketData] Using user credentials for ${provider}`);
      return {
        apiKey: userCreds.apiKey,
        apiSecret: userCreds.apiSecret,
        ownership: { ownerType: 'user', ownerId: userId },
      };
    }
  }

  // Fall back to platform credentials (env vars - for development/testing only)
  const envCreds = getEnvCredentials(provider);
  if (envCreds) {
    console.log(`[MarketData] Using platform credentials for ${provider}`);
    return {
      ...envCreds,
      ownership: { ownerType: 'platform' },
    };
  }

  return null;
}

function getEnvCredentials(provider: DataProvider): { apiKey: string; apiSecret?: string } | undefined {
  switch (provider) {
    case 'alpha_vantage': {
      const key = process.env.ALPHA_VANTAGE_API_KEY;
      if (key) return { apiKey: key };
      return undefined;
    }
    case 'alpaca': {
      const key = process.env.ALPACA_API_KEY;
      const secret = process.env.ALPACA_API_SECRET;
      if (key && secret) return { apiKey: key, apiSecret: secret };
      return undefined;
    }
    case 'polygon': {
      const key = process.env.POLYGON_API_KEY;
      if (key) return { apiKey: key };
      return undefined;
    }
  }
}

/**
 * Get list of available providers (platform level)
 */
function getAvailableProviders(): DataProvider[] {
  const providers: DataProvider[] = [];
  if (process.env.ALPHA_VANTAGE_API_KEY) providers.push('alpha_vantage');
  if (process.env.ALPACA_API_KEY && process.env.ALPACA_API_SECRET) providers.push('alpaca');
  if (process.env.POLYGON_API_KEY) providers.push('polygon');
  return providers;
}

// ============================================
// Main API
// ============================================

/**
 * Fetch data from a specific provider
 */
async function fetchFromProvider(
  provider: DataProvider,
  symbol: string,
  credentials: ProviderCredentialsResult,
  startDate: Date,
  endDate: Date,
  fullHistory: boolean
): Promise<NormalizedMarketData | null> {
  switch (provider) {
    case 'alpha_vantage': {
      const outputSize = fullHistory ? 'full' : 'compact';
      const data = await fetchFromAlphaVantage(symbol, credentials.apiKey, outputSize);
      if (data) {
        return normalizeAlphaVantageData(data);
      }
      return null;
    }
    case 'alpaca': {
      if (!credentials.apiSecret) {
        console.error('[MarketData] Alpaca requires API secret');
        return null;
      }
      // For Alpaca, we always specify date range (it doesn't have outputSize concept)
      // For full history, we go back to 2010
      const actualStartDate = fullHistory ? new Date('2010-01-01') : startDate;
      return fetchFromAlpaca(symbol, credentials.apiKey, credentials.apiSecret, actualStartDate, endDate);
    }
    case 'polygon':
      // TODO: Implement Polygon when needed
      console.log('[MarketData] Polygon not yet implemented');
      return null;
  }
}

/**
 * Ensure market data is cached for a symbol and date range
 * Production-grade: checks cached range and only fetches missing data
 * Supports both platform and user-owned data
 * Tries providers in order: preferred provider, then fallback providers
 */
export async function ensureMarketDataCached(
  symbol: string,
  startDate: Date,
  endDate: Date,
  userId?: string,
  preferredProvider: DataProvider = 'alpha_vantage'
): Promise<{ cached: boolean; source: 'platform' | 'user'; provider: DataProvider }> {
  // Build provider priority list: preferred first, then others
  const availableProviders = getAvailableProviders();
  const providerOrder = [
    preferredProvider,
    ...availableProviders.filter(p => p !== preferredProvider),
  ].filter(p => availableProviders.includes(p) || p === preferredProvider);

  // Try each provider until one works
  for (const provider of providerOrder) {
    const credentials = await getProviderCredentials(provider, userId);
    if (!credentials) continue;

    const { ownership } = credentials;
    const symbolInfo = await getSymbolInfo(symbol, ownership);

    if (!symbolInfo) {
      // First time seeing this symbol - fetch full history
      console.log(`[MarketData] First time caching ${symbol} via ${provider} (${ownership.ownerType})`);
      console.log(`[MarketData]   Required range: ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);

      const data = await fetchFromProvider(provider, symbol, credentials, startDate, endDate, true);
      if (data && data.bars.length > 0) {
        console.log(`[MarketData]   Fetched ${data.bars.length} bars`);
        await cacheNormalizedData(data, provider, ownership);
        return { cached: true, source: ownership.ownerType, provider };
      }
      // This provider failed, try next
      console.log(`[MarketData] ${provider} failed for ${symbol}, trying next provider...`);
      continue;
    }

    // Check cached range vs required range
    const cachedFirst = symbolInfo.firstDate ? new Date(symbolInfo.firstDate) : null;
    const cachedLast = symbolInfo.lastDate ? new Date(symbolInfo.lastDate) : null;

    const needsEarlierData = cachedFirst && cachedFirst > startDate;
    const needsLaterData = cachedLast && cachedLast < endDate;

    if (!needsEarlierData && !needsLaterData) {
      console.log(`[MarketData] ${symbol} cache fully covers required range`);
      console.log(`[MarketData]   Required: ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);
      console.log(`[MarketData]   Cached: ${cachedFirst?.toISOString().split('T')[0]} to ${cachedLast?.toISOString().split('T')[0]}`);
      return { cached: true, source: ownership.ownerType, provider };
    }

    console.log(`[MarketData] ${symbol} cache partial - needs extension`);
    console.log(`[MarketData]   Required: ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);
    console.log(`[MarketData]   Cached: ${cachedFirst?.toISOString().split('T')[0]} to ${cachedLast?.toISOString().split('T')[0]}`);
    console.log(`[MarketData]   Needs earlier: ${needsEarlierData}, Needs later: ${needsLaterData}`);

    // For Alpha Vantage, it's more efficient to fetch full history since the API
    // returns all data regardless of start date. For other providers we could
    // optimize by fetching only the missing range.
    if (needsEarlierData && provider === 'alpha_vantage') {
      // Alpha Vantage TIME_SERIES_DAILY_ADJUSTED with outputsize=full returns all history
      // So just refetch full and merge (API call is same cost regardless)
      console.log(`[MarketData] Refetching full history for ${symbol} (need earlier data)`);
      const data = await fetchFromProvider(provider, symbol, credentials, startDate, endDate, true);
      if (data && data.bars.length > 0) {
        console.log(`[MarketData]   Fetched ${data.bars.length} bars, merging with cache`);
        await cacheNormalizedData(data, provider, ownership);
      }
    } else if (needsEarlierData && provider === 'alpaca') {
      // Alpaca supports date ranges, so fetch only the missing earlier data
      console.log(`[MarketData] Fetching earlier data for ${symbol}: ${startDate.toISOString().split('T')[0]} to ${cachedFirst!.toISOString().split('T')[0]}`);
      const data = await fetchFromProvider(provider, symbol, credentials, startDate, cachedFirst!, false);
      if (data && data.bars.length > 0) {
        console.log(`[MarketData]   Fetched ${data.bars.length} earlier bars`);
        await cacheNormalizedData(data, provider, ownership);
      }
    }

    // Fetch later data if needed
    if (needsLaterData) {
      console.log(`[MarketData] Fetching later data for ${symbol}: ${cachedLast!.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);
      const data = await fetchFromProvider(provider, symbol, credentials, cachedLast!, endDate, false);
      if (data && data.bars.length > 0) {
        console.log(`[MarketData]   Fetched ${data.bars.length} later bars`);
        await cacheNormalizedData(data, provider, ownership);
      }
    }

    return { cached: true, source: ownership.ownerType, provider };
  }

  // No provider worked
  throw new Error(
    `No API key available for any provider. Configure ALPHA_VANTAGE_API_KEY, ALPACA_API_KEY+ALPACA_API_SECRET, or add your own key.`
  );
}

/**
 * Get daily bars for a symbol and date range
 * Uses the fallback function to prefer user data over platform data
 */
export async function getDailyBars(
  symbol: string,
  startDate: Date,
  endDate: Date,
  userId?: string
): Promise<MarketDataDaily[]> {
  // Use the helper function that handles fallback
  return query<MarketDataDaily>(
    `SELECT * FROM get_market_data_with_fallback($1, $2, $3, $4)`,
    [
      symbol,
      startDate.toISOString().split('T')[0],
      endDate.toISOString().split('T')[0],
      userId || null,
    ]
  );
}

/**
 * Get daily bars without fallback (specific owner only)
 */
export async function getDailyBarsExact(
  symbol: string,
  startDate: Date,
  endDate: Date,
  ownership: DataOwnership
): Promise<MarketDataDaily[]> {
  const { ownerType, ownerId } = ownership;

  return query<MarketDataDaily>(
    `SELECT * FROM market_data_daily
     WHERE symbol = $1
       AND date >= $2 AND date <= $3
       AND owner_type = $4
       AND COALESCE(owner_id, '__platform__') = COALESCE($5, '__platform__')
     ORDER BY date ASC`,
    [
      symbol,
      startDate.toISOString().split('T')[0],
      endDate.toISOString().split('T')[0],
      ownerType,
      ownerId || null,
    ]
  );
}

/**
 * Ensure multiple symbols are cached
 */
export async function ensureMultipleSymbolsCached(
  symbols: string[],
  startDate: Date,
  endDate: Date,
  userId?: string,
  preferredProvider: DataProvider = 'alpha_vantage'
): Promise<void> {
  // Process sequentially to respect rate limits
  for (const symbol of symbols) {
    await ensureMarketDataCached(symbol, startDate, endDate, userId, preferredProvider);
  }
}

/**
 * Get all cached symbols for a specific owner
 */
export async function getCachedSymbols(
  ownership?: DataOwnership
): Promise<MarketDataSymbol[]> {
  if (ownership) {
    const { ownerType, ownerId } = ownership;
    return query<MarketDataSymbol>(
      `SELECT * FROM market_data_symbols
       WHERE owner_type = $1 AND COALESCE(owner_id, '__platform__') = COALESCE($2, '__platform__')
       ORDER BY symbol`,
      [ownerType, ownerId || null]
    );
  }

  // All symbols (platform only by default)
  return query<MarketDataSymbol>(
    `SELECT * FROM market_data_symbols WHERE owner_type = 'platform' ORDER BY symbol`
  );
}

/**
 * Check if a symbol has complete data for a date range
 */
export async function hasCompleteData(
  symbol: string,
  startDate: Date,
  endDate: Date,
  userId?: string
): Promise<boolean> {
  // Check user data first
  if (userId) {
    const userInfo = await getSymbolInfo(symbol, { ownerType: 'user', ownerId: userId });
    if (userInfo?.firstDate && userInfo?.lastDate) {
      if (new Date(userInfo.firstDate) <= startDate && new Date(userInfo.lastDate) >= endDate) {
        return true;
      }
    }
  }

  // Check platform data
  const platformInfo = await getSymbolInfo(symbol, { ownerType: 'platform' });
  if (!platformInfo?.firstDate || !platformInfo?.lastDate) return false;

  return (
    new Date(platformInfo.firstDate) <= startDate &&
    new Date(platformInfo.lastDate) >= endDate
  );
}

/**
 * Get data availability summary for a user
 */
export async function getDataAvailability(userId?: string): Promise<{
  platformSymbols: number;
  userSymbols: number;
  providers: DataProvider[];
}> {
  const platformCount = await queryOne<{ count: string }>(
    `SELECT COUNT(*) as count FROM market_data_symbols WHERE owner_type = 'platform'`
  );

  let userCount = { count: '0' };
  if (userId) {
    userCount = await queryOne<{ count: string }>(
      `SELECT COUNT(*) as count FROM market_data_symbols WHERE owner_type = 'user' AND owner_id = $1`,
      [userId]
    ) || { count: '0' };
  }

  // Get available providers (reuse the helper function)
  const providers = getAvailableProviders();

  return {
    platformSymbols: parseInt(platformCount?.count || '0', 10),
    userSymbols: parseInt(userCount?.count || '0', 10),
    providers,
  };
}
