/**
 * Backtest Worker
 * Processes backtest jobs using the LEAN engine via Docker
 *
 * Streaming Architecture:
 * - LEAN uses StreamingMessageHandler to push BacktestResultPacket via ZeroMQ
 * - We listen on a ZeroMQ Pull socket to receive real-time chart updates
 * - Chart updates are forwarded to SSE for live frontend visualization
 */

import { Worker, Job } from 'bullmq';
import { query, queryOne, execute } from '../services/database.js';
import { ensureMultipleSymbolsCached, getDailyBars } from '../services/market-data.js';
import {
  getLocalCacheDir,
  getHostCacheDir,
} from '../services/s3-cache.js';
import {
  createUsageRecord,
  markUsageRunning,
  completeUsageRecord,
  incrementDataPoints,
  linkBacktestToUsage,
  getDockerStats,
} from '../services/usage-tracking.js';
import type { BacktestJobData, LeanFile, MarketDataDaily } from '../types/index.js';
import { spawn } from 'child_process';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as os from 'os';
import archiver from 'archiver';
import { createWriteStream, createReadStream } from 'fs';
import * as unzipper from 'unzipper';
import * as zmq from 'zeromq';
import { publishChartUpdate } from '../services/chart-streaming.js';

// ============================================================================
// STREAMING TYPES
// ============================================================================

/**
 * Chart point for real-time streaming
 */
export interface ChartPoint {
  x: number;  // Unix timestamp (seconds)
  y: number;  // Value (equity, benchmark, etc.)
}

/**
 * Chart update streamed from LEAN
 */
export interface ChartUpdate {
  chartName: string;
  seriesName: string;
  points: ChartPoint[];
}

/**
 * Callback for receiving real-time chart updates
 */
export type OnChartUpdate = (update: ChartUpdate) => void;

// ============================================================================
// PORT POOL FOR CONCURRENT BACKTESTS (Distributed via Redis)
// ============================================================================

import {
  allocatePort as allocateDistributedPort,
  releasePort as releaseDistributedPort,
  refreshPort as refreshDistributedPort,
  cleanupStalePorts,
} from '../services/distributed-ports.js';

// Track allocated ports locally for fast cleanup on shutdown
const localAllocatedPorts = new Set<number>();

/**
 * Allocate a free port for ZeroMQ streaming
 * Uses Redis for distributed coordination across worker nodes
 */
async function allocateStreamingPort(): Promise<number | null> {
  const port = await allocateDistributedPort();
  if (port) {
    localAllocatedPorts.add(port);
  }
  return port;
}

/**
 * Release a streaming port back to the pool
 */
async function releaseStreamingPort(port: number): Promise<void> {
  localAllocatedPorts.delete(port);
  await releaseDistributedPort(port);
}

// ============================================================================
// ZEROMQ STREAMING SUBSCRIBER
// ============================================================================

/**
 * Series data structure within a LEAN chart
 */
interface LeanChartSeries {
  Name: string;
  Unit?: string;
  Index?: number;
  Values: Array<{ x: number; y: number }>;
  SeriesType?: number;
  Color?: string;
}

/**
 * Chart data structure from LEAN
 */
interface LeanChart {
  Name: string;
  ChartType?: number;
  Series: Record<string, LeanChartSeries>;
}

/**
 * LEAN's BacktestResultPacket structure (simplified - we only extract charts)
 * Full spec: QuantConnect.Packets.BacktestResultPacket
 */
interface LeanBacktestResultPacket {
  Type?: string;  // "BacktestResult"
  Progress?: number;
  Charts?: Record<string, LeanChart>;
  Results?: {
    Charts?: Record<string, LeanChart>;
  };
}

/**
 * Create a ZeroMQ Pull socket to receive LEAN's streaming packets
 * Returns an async iterator that yields chart updates
 *
 * Architecture:
 * - LEAN's StreamingMessageHandler creates a PUSH socket that BINDS to the port
 * - Our code creates a PULL socket that CONNECTS to that port
 * - LEAN pushes messages, we pull/receive them
 *
 * With --network host, LEAN binds to localhost:{port} on the host
 * Our Node.js also runs on host (or in Docker with host network), so we connect to localhost
 */
async function createStreamingSubscriber(
  port: number,
  onChartUpdate: OnChartUpdate,
  abortSignal: AbortSignal
): Promise<zmq.Pull> {
  const socket = new zmq.Pull();

  // Connect to LEAN's PUSH socket (LEAN binds, we connect)
  // ZeroMQ connect can happen before bind - it will reconnect automatically
  const connectAddress = `tcp://localhost:${port}`;
  console.log(`[LEAN Streaming] Connecting ZeroMQ Pull socket to ${connectAddress}`);

  await socket.connect(connectAddress);

  // Start receiving messages in the background
  (async () => {
    try {
      for await (const [msg] of socket) {
        if (abortSignal.aborted) {
          break;
        }

        try {
          const packet = JSON.parse(msg.toString()) as LeanBacktestResultPacket;

          // Extract chart updates from the packet
          const charts: Record<string, LeanChart> | undefined = packet.Charts || packet.Results?.Charts;
          if (charts && typeof charts === 'object') {
            for (const [chartName, chart] of Object.entries(charts)) {
              if (chart && typeof chart === 'object' && 'Series' in chart) {
                const typedChart = chart as LeanChart;
                for (const [seriesName, series] of Object.entries(typedChart.Series || {})) {
                  if (series.Values && series.Values.length > 0) {
                    onChartUpdate({
                      chartName,
                      seriesName,
                      points: series.Values,
                    });
                  }
                }
              }
            }
          }

          // Also extract progress if available
          if (packet.Progress !== undefined) {
            console.log(`[LEAN Streaming] Progress: ${(packet.Progress * 100).toFixed(1)}%`);
          }
        } catch (parseError) {
          // Ignore parse errors - LEAN may send non-JSON messages
          console.debug('[LEAN Streaming] Non-JSON message received');
        }
      }
    } catch (err) {
      if (!abortSignal.aborted) {
        console.error('[LEAN Streaming] Socket error:', err);
      }
    }
  })();

  return socket;
}

// ============================================================================
// WORKSPACE CONFIGURATION
// ============================================================================

// Workspace directory for LEAN backtests - must be accessible from both this container AND the Docker host
// When using sibling containers (Docker socket mounting), paths must exist on the host
const LEAN_WORKSPACES_DIR = process.env.LEAN_WORKSPACES_DIR || '/app/workspaces';

// Host path mapping - needed for sibling container pattern
// The container sees /app/workspaces but Docker daemon sees the host-mounted path
const LEAN_HOST_WORKSPACES_DIR = process.env.LEAN_HOST_WORKSPACES_DIR || LEAN_WORKSPACES_DIR;

// ============================================================================
// LEAN RESULT TYPES
// ============================================================================
interface LeanResult {
  Statistics?: Record<string, string>;
  RuntimeStatistics?: Record<string, string>;
  TotalPerformance?: {
    TradeStatistics?: Record<string, string>;
    PortfolioStatistics?: Record<string, string>;
  };
  Charts?: Record<string, {
    Name: string;
    Series: Record<string, {
      Name: string;
      Unit: string;
      Index: number;
      Values: Array<{ x: number; y: number }>;
      SeriesType: number;
      Color: string;
      ScatterMarkerSymbol: string;
    }>;
  }>;
  Orders?: Record<string, unknown>;
}

/**
 * Get cached date range from existing LEAN zip file
 * Returns null if no cache exists
 */
async function getCachedDateRange(symbol: string): Promise<{ firstDate: Date; lastDate: Date } | null> {
  const symbolLower = symbol.toLowerCase();
  const cacheDir = getLocalCacheDir();
  const zipPath = path.join(cacheDir, 'equity', 'usa', 'daily', `${symbolLower}.zip`);

  try {
    await fs.access(zipPath);
  } catch {
    return null;
  }

  try {
    // Read the zip file and extract date range from CSV content
    const directory = await unzipper.Open.file(zipPath);
    const csvFile = directory.files.find(f => f.path.endsWith('.csv'));

    if (!csvFile) {
      console.log(`[LEAN Data Cache] No CSV found in ${zipPath}`);
      return null;
    }

    const content = await csvFile.buffer();
    const lines = content.toString('utf-8').trim().split('\n');

    if (lines.length === 0) {
      return null;
    }

    // LEAN format: YYYYMMDD 00:00,open,high,low,close,volume
    const parseDate = (line: string): Date | null => {
      const dateStr = line.split(' ')[0]; // Get YYYYMMDD part
      if (dateStr.length !== 8) return null;
      const year = parseInt(dateStr.substring(0, 4));
      const month = parseInt(dateStr.substring(4, 6)) - 1;
      const day = parseInt(dateStr.substring(6, 8));
      return new Date(year, month, day);
    };

    const firstDate = parseDate(lines[0]);
    const lastDate = parseDate(lines[lines.length - 1]);

    if (!firstDate || !lastDate) {
      console.log(`[LEAN Data Cache] Could not parse dates from ${zipPath}`);
      return null;
    }

    console.log(`[LEAN Data Cache] ${symbol} cached range: ${firstDate.toISOString().split('T')[0]} to ${lastDate.toISOString().split('T')[0]}`);
    return { firstDate, lastDate };
  } catch (error) {
    console.error(`[LEAN Data Cache] Error reading cached range for ${symbol}:`, error);
    return null;
  }
}

/**
 * Check if LEAN data exists in cache and covers the required date range
 * Returns coverage info: 'full', 'partial', or 'none'
 */
async function checkCacheStatus(
  symbol: string,
  requiredStart: Date,
  requiredEnd: Date
): Promise<{
  status: 'full' | 'partial' | 'none';
  cachedRange: { firstDate: Date; lastDate: Date } | null;
  missingBefore: boolean;
  missingAfter: boolean;
}> {
  const symbolLower = symbol.toLowerCase();
  const cacheDir = getLocalCacheDir();
  const mapPath = path.join(cacheDir, 'equity', 'usa', 'map_files', `${symbolLower}.csv`);
  const factorPath = path.join(cacheDir, 'equity', 'usa', 'factor_files', `${symbolLower}.csv`);

  // Check if auxiliary files exist (map and factor files)
  let hasAuxFiles = true;
  try {
    await Promise.all([
      fs.access(mapPath),
      fs.access(factorPath),
    ]);
  } catch {
    hasAuxFiles = false;
  }

  const cachedRange = await getCachedDateRange(symbol);

  if (!cachedRange || !hasAuxFiles) {
    return {
      status: 'none',
      cachedRange: null,
      missingBefore: true,
      missingAfter: true,
    };
  }

  const missingBefore = cachedRange.firstDate > requiredStart;
  const missingAfter = cachedRange.lastDate < requiredEnd;

  if (!missingBefore && !missingAfter) {
    return {
      status: 'full',
      cachedRange,
      missingBefore: false,
      missingAfter: false,
    };
  }

  return {
    status: 'partial',
    cachedRange,
    missingBefore,
    missingAfter,
  };
}

/**
 * Convert our cached market data to LEAN's ZIP format and store in persistent cache
 * LEAN expects ZIP files at: /Data/equity/usa/daily/{symbol}.zip
 * containing CSV with format: YYYYMMDD HH:MM,Open,High,Low,Close,Volume
 * Prices are scaled by 10000 (QC format), no header row
 */
async function exportMarketDataToCache(
  symbol: string,
  bars: MarketDataDaily[]
): Promise<void> {
  const symbolLower = symbol.toLowerCase();
  const cacheDir = getLocalCacheDir();

  // Store in persistent cache
  const symbolDir = path.join(cacheDir, 'equity', 'usa', 'daily');
  await fs.mkdir(symbolDir, { recursive: true });

  // Create CSV content in LEAN format (no header, prices * 10000)
  const csvLines: string[] = [];
  for (const bar of bars) {
    // LEAN format: YYYYMMDD 00:00,open*10000,high*10000,low*10000,close*10000,volume
    const dateStr = new Date(bar.date).toISOString().split('T')[0].replace(/-/g, '');
    const scaledOpen = Math.round(Number(bar.open) * 10000);
    const scaledHigh = Math.round(Number(bar.high) * 10000);
    const scaledLow = Math.round(Number(bar.low) * 10000);
    const scaledClose = Math.round(Number(bar.close) * 10000);
    csvLines.push(`${dateStr} 00:00,${scaledOpen},${scaledHigh},${scaledLow},${scaledClose},${bar.volume}`);
  }

  const csvContent = csvLines.join('\n');
  const zipPath = path.join(symbolDir, `${symbolLower}.zip`);
  const csvFileName = `${symbolLower}.csv`;

  // Create ZIP file containing the CSV
  await new Promise<void>((resolve, reject) => {
    const output = createWriteStream(zipPath);
    const archive = archiver('zip', { zlib: { level: 9 } });

    output.on('close', () => resolve());
    archive.on('error', (err: Error) => reject(err));

    archive.pipe(output);
    archive.append(csvContent, { name: csvFileName });
    archive.finalize();
  });

  console.log(`[LEAN Data Cache] Created ${zipPath} with ${bars.length} bars`);
}

/**
 * Generate map file in persistent cache
 */
async function generateMapFileToCache(
  symbol: string,
  startDate: Date
): Promise<void> {
  const cacheDir = getLocalCacheDir();
  const mapFilesDir = path.join(cacheDir, 'equity', 'usa', 'map_files');
  await fs.mkdir(mapFilesDir, { recursive: true });

  const symbolLower = symbol.toLowerCase();

  const formatDate = (d: Date): string => {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}${month}${day}`;
  };

  const historicalStart = new Date(1998, 0, 2);
  const effectiveStart = startDate < historicalStart ? startDate : historicalStart;
  const futureEnd = new Date(2050, 11, 31);
  const exchange = 'Q';

  const mapContent = [
    `${formatDate(effectiveStart)},${symbolLower},${exchange}`,
    `${formatDate(futureEnd)},${symbolLower},${exchange}`,
  ].join('\n');

  const mapFilePath = path.join(mapFilesDir, `${symbolLower}.csv`);
  await fs.writeFile(mapFilePath, mapContent);
  console.log(`[LEAN Data Cache] Created map file: ${mapFilePath}`);
}

/**
 * Generate factor file in persistent cache
 */
async function generateFactorFileToCache(
  symbol: string,
  startDate: Date,
  referencePrice: number = 0
): Promise<void> {
  const cacheDir = getLocalCacheDir();
  const factorFilesDir = path.join(cacheDir, 'equity', 'usa', 'factor_files');
  await fs.mkdir(factorFilesDir, { recursive: true });

  const symbolLower = symbol.toLowerCase();

  const formatDate = (d: Date): string => {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}${month}${day}`;
  };

  const historicalStart = new Date(1998, 0, 2);
  const effectiveStart = startDate < historicalStart ? startDate : historicalStart;
  const futureEnd = new Date(2050, 11, 31);

  const factorContent = [
    `${formatDate(effectiveStart)},1,1,0`,
    `${formatDate(futureEnd)},1,1,${referencePrice}`,
  ].join('\n');

  const factorFilePath = path.join(factorFilesDir, `${symbolLower}.csv`);
  await fs.writeFile(factorFilePath, factorContent);
  console.log(`[LEAN Data Cache] Created factor file: ${factorFilePath}`);
}

/**
 * Read existing cached bars from LEAN zip file
 */
async function readCachedBars(symbol: string): Promise<MarketDataDaily[]> {
  const symbolLower = symbol.toLowerCase();
  const cacheDir = getLocalCacheDir();
  const zipPath = path.join(cacheDir, 'equity', 'usa', 'daily', `${symbolLower}.zip`);

  try {
    await fs.access(zipPath);
  } catch {
    return [];
  }

  try {
    const directory = await unzipper.Open.file(zipPath);
    const csvFile = directory.files.find(f => f.path.endsWith('.csv'));

    if (!csvFile) {
      return [];
    }

    const content = await csvFile.buffer();
    const lines = content.toString('utf-8').trim().split('\n');

    // Parse LEAN format: YYYYMMDD 00:00,open*10000,high*10000,low*10000,close*10000,volume
    return lines.map(line => {
      const [dateTime, open, high, low, close, volume] = line.split(',');
      const dateStr = dateTime.split(' ')[0];
      const year = parseInt(dateStr.substring(0, 4));
      const month = parseInt(dateStr.substring(4, 6));
      const day = parseInt(dateStr.substring(6, 8));

      return {
        date: new Date(year, month - 1, day),
        open: parseFloat(open) / 10000,
        high: parseFloat(high) / 10000,
        low: parseFloat(low) / 10000,
        close: parseFloat(close) / 10000,
        volume: parseInt(volume, 10),
        // These fields are not stored in LEAN format but satisfy the interface
        adjusted_close: parseFloat(close) / 10000,
        dividend: 0,
        split_coefficient: 1,
      } as unknown as MarketDataDaily;
    });
  } catch (error) {
    console.error(`[LEAN Data Cache] Error reading cached bars for ${symbol}:`, error);
    return [];
  }
}

/**
 * Merge and deduplicate bars, sorted by date
 */
function mergeBars(existing: MarketDataDaily[], newBars: MarketDataDaily[]): MarketDataDaily[] {
  // Use a Map to dedupe by date string
  const barMap = new Map<string, MarketDataDaily>();

  // Add existing bars first
  for (const bar of existing) {
    const dateStr = new Date(bar.date).toISOString().split('T')[0];
    barMap.set(dateStr, bar);
  }

  // Add/overwrite with new bars (new data takes priority)
  for (const bar of newBars) {
    const dateStr = new Date(bar.date).toISOString().split('T')[0];
    barMap.set(dateStr, bar);
  }

  // Convert back to array and sort by date
  const merged = Array.from(barMap.values());
  merged.sort((a, b) => new Date(a.date).getTime() - new Date(b.date).getTime());

  return merged;
}

/**
 * Ensure symbol data exists in persistent cache with required date range
 * Production-grade: checks existing cache and only extends if needed
 */
async function ensureSymbolInCache(
  symbol: string,
  newBars: MarketDataDaily[],
  requiredStart: Date,
  requiredEnd: Date
): Promise<void> {
  const cacheStatus = await checkCacheStatus(symbol, requiredStart, requiredEnd);

  if (cacheStatus.status === 'full') {
    console.log(`[LEAN Data Cache] ${symbol} cache covers required range, skipping`);
    return;
  }

  if (cacheStatus.status === 'partial') {
    console.log(`[LEAN Data Cache] ${symbol} cache partial coverage:`);
    console.log(`  - Required: ${requiredStart.toISOString().split('T')[0]} to ${requiredEnd.toISOString().split('T')[0]}`);
    console.log(`  - Cached: ${cacheStatus.cachedRange!.firstDate.toISOString().split('T')[0]} to ${cacheStatus.cachedRange!.lastDate.toISOString().split('T')[0]}`);
    console.log(`  - Missing before: ${cacheStatus.missingBefore}, after: ${cacheStatus.missingAfter}`);

    // Read existing cached data and merge with new bars
    const existingBars = await readCachedBars(symbol);
    const mergedBars = mergeBars(existingBars, newBars);

    console.log(`[LEAN Data Cache] Merging ${existingBars.length} existing + ${newBars.length} new = ${mergedBars.length} total bars`);

    if (mergedBars.length > 0) {
      await exportMarketDataToCache(symbol, mergedBars);
      const lastBar = mergedBars[mergedBars.length - 1];
      const firstBar = mergedBars[0];
      const referencePrice = Number(lastBar.close);
      await generateMapFileToCache(symbol, new Date(firstBar.date));
      await generateFactorFileToCache(symbol, new Date(firstBar.date), referencePrice);
    }

    console.log(`[LEAN Data Cache] ${symbol} cache extended`);
    return;
  }

  // status === 'none' - generate from scratch
  console.log(`[LEAN Data Cache] Generating LEAN data for ${symbol}...`);

  if (newBars.length > 0) {
    await exportMarketDataToCache(symbol, newBars);
    const lastBar = newBars[newBars.length - 1];
    const firstBar = newBars[0];
    const referencePrice = Number(lastBar.close);
    await generateMapFileToCache(symbol, new Date(firstBar.date));
    await generateFactorFileToCache(symbol, new Date(firstBar.date), referencePrice);
  } else {
    // Generate auxiliary files even without price data
    await generateMapFileToCache(symbol, requiredStart);
    await generateFactorFileToCache(symbol, requiredStart, 0);
  }

  console.log(`[LEAN Data Cache] ${symbol} written to cache (persisted via s3fs)`);
}

/**
 * Ensure LEAN static data files are available (symbol-properties, market-hours)
 * Downloads from GitHub if not already in cache (which is s3fs-mounted)
 */
async function ensureLeanStaticData(): Promise<void> {
  const cacheDir = getLocalCacheDir();

  const staticFiles = [
    { relativePath: 'symbol-properties/symbol-properties-database.csv',
      url: 'https://raw.githubusercontent.com/QuantConnect/Lean/master/Data/symbol-properties/symbol-properties-database.csv' },
    { relativePath: 'market-hours/market-hours-database.json',
      url: 'https://raw.githubusercontent.com/QuantConnect/Lean/master/Data/market-hours/market-hours-database.json' },
  ];

  for (const { relativePath, url } of staticFiles) {
    const localPath = path.join(cacheDir, relativePath);

    // Check if already exists
    try {
      await fs.access(localPath);
      console.log(`[LEAN Data Cache] Static data ${relativePath} available`);
      continue;
    } catch {
      // Not in cache, download from GitHub
    }

    console.log(`[LEAN Data Cache] Downloading ${relativePath} from GitHub...`);
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`Failed to download ${relativePath}: ${response.status}`);
    }
    const content = await response.text();

    await fs.mkdir(path.dirname(localPath), { recursive: true });
    await fs.writeFile(localPath, content);
    console.log(`[LEAN Data Cache] Downloaded ${relativePath}`);
  }
}

// ============================================================================
// BROKERAGE CREDENTIALS
// ============================================================================

interface BrokerageCredentials {
  provider: 'alpaca' | 'interactive_brokers';
  apiKey: string;
  apiSecret: string;
}

/**
 * Fetch user's brokerage credentials from the main app.
 * Users MUST connect a broker before running backtests (data licensing requirement).
 */
async function getUserBrokerageCredentials(userId: string): Promise<BrokerageCredentials | null> {
  const mainAppUrl = process.env.MAIN_APP_URL || 'http://localhost:3000';
  const apiSecret = process.env.LEAN_API_SECRET;

  if (!apiSecret) {
    console.warn('[Backtest Worker] LEAN_API_SECRET not configured, cannot fetch user credentials');
    return null;
  }

  try {
    // Fetch all credentials, then find the best Alpaca one
    const response = await fetch(
      `${mainAppUrl}/api/internal/user-credentials?userId=${encodeURIComponent(userId)}`,
      { headers: { Authorization: `Bearer ${apiSecret}` } }
    );

    if (!response.ok) {
      console.error(`[Backtest Worker] Failed to fetch user credentials: ${response.status}`);
      return null;
    }

    const data = await response.json() as { credentials?: Array<{ provider: string; apiKey: string; apiSecret: string }> };

    if (data.credentials && data.credentials.length > 0) {
      // Accept any Alpaca variant - paper keys work fine for historical data
      const alpacaCred = data.credentials.find(c =>
        c.provider === 'alpaca' || c.provider === 'alpaca_paper'
      );

      if (alpacaCred && alpacaCred.apiKey && alpacaCred.apiSecret) {
        console.log(`[Backtest Worker] Found ${alpacaCred.provider} credentials for user ${userId}`);
        return {
          provider: 'alpaca',
          apiKey: alpacaCred.apiKey,
          apiSecret: alpacaCred.apiSecret,
        };
      }
    }

    // Could add IB fallback here in the future
    return null;
  } catch (err) {
    console.error('[Backtest Worker] Error fetching user credentials:', err);
    return null;
  }
}

/**
 * Create LEAN config.json for the backtest
 * Parameters are passed to LEAN and can be accessed via self.get_parameter() in the algorithm
 *
 * When streamingPort is provided, LEAN will use StreamingMessageHandler to push
 * real-time chart updates via ZeroMQ on that port.
 *
 * When brokerageCredentials is provided, LEAN will use DownloaderDataProvider to
 * fetch data directly from the user's brokerage (Alpaca, IB, etc.) for licensing compliance.
 */

function createLeanConfig(
  startDate: Date,
  endDate: Date,
  cash: number,
  parameters: Record<string, unknown> = {},
  streamingPort?: number,
  brokerageCredentials?: BrokerageCredentials
): object {
  // Note: These paths are INSIDE the Docker container, not host paths
  // The Docker volumes map:
  //   hostAlgorithmDir -> /Algorithm
  //   hostDataDir -> /Data
  //   hostResultsDir -> /Results

  // Convert parameters to LEAN format (all values as strings)
  const leanParameters: Record<string, string> = {};
  for (const [key, value] of Object.entries(parameters)) {
    leanParameters[key] = String(value);
  }

  const config: Record<string, unknown> = {
    'environment': 'backtesting',
    'algorithm-type-name': 'main',
    'algorithm-language': 'Python',
    'algorithm-location': '/Algorithm/main.py',  // Container path
    'data-folder': '/Data',                       // Container path
    'results-destination-folder': '/Results',     // Container path
    'job-queue-handler': 'QuantConnect.Queues.JobQueue',
    'api-handler': 'QuantConnect.Api.Api',
    // Use LocalDiskMapFileProvider and LocalDiskFactorFileProvider
    // We generate map_files and factor_files for each symbol before running LEAN
    // These files are placed in /Data/equity/usa/map_files/ and /Data/equity/usa/factor_files/
    'map-file-provider': 'QuantConnect.Data.Auxiliary.LocalDiskMapFileProvider',
    'factor-file-provider': 'QuantConnect.Data.Auxiliary.LocalDiskFactorFileProvider',
    'alpha-handler': 'QuantConnect.Lean.Engine.Alphas.DefaultAlphaHandler',
    'data-channel-provider': 'DataChannelProvider',
    'log-handler': 'QuantConnect.Logging.CompositeLogHandler',
    'parameters': leanParameters,
    'close-automatically': true,
    'start-date': startDate.toISOString().split('T')[0],
    'end-date': endDate.toISOString().split('T')[0],
    'cash-amount': cash,
  };

  // Configure data provider based on whether brokerage credentials are provided
  // When credentials are provided, LEAN fetches data directly from the brokerage
  // This is required for data licensing compliance - users must use their own data
  if (brokerageCredentials) {
    config['data-provider'] = 'QuantConnect.Lean.Engine.DataFeeds.DownloaderDataProvider';
    config['data-downloader'] = 'BrokerageDataDownloader';

    if (brokerageCredentials.provider === 'alpaca') {
      config['data-downloader-brokerage'] = 'AlpacaBrokerage';
      config['alpaca-api-key'] = brokerageCredentials.apiKey;
      config['alpaca-api-secret'] = brokerageCredentials.apiSecret;
      config['alpaca-paper-trading'] = true; // Use paper for data only
    } else if (brokerageCredentials.provider === 'interactive_brokers') {
      config['data-downloader-brokerage'] = 'InteractiveBrokersBrokerage';
      config['ib-user-name'] = brokerageCredentials.apiKey;
      config['ib-password'] = brokerageCredentials.apiSecret;
      // IB requires additional config that user would need to provide
    }
  } else {
    // Fallback: use local data files (pre-cached)
    // This is only for development/testing - production requires user credentials
    config['data-provider'] = 'QuantConnect.Lean.Engine.DataFeeds.DefaultDataProvider';
  }

  // Configure messaging handler based on whether streaming is enabled
  if (streamingPort) {
    // Use StreamingMessageHandler for real-time chart updates via ZeroMQ
    // LEAN will push BacktestResultPacket messages to tcp://*:{port}
    config['messaging-handler'] = 'QuantConnect.Messaging.StreamingMessageHandler';
    config['desktop-http-port'] = streamingPort;
  } else {
    // Default handler - no streaming
    config['messaging-handler'] = 'QuantConnect.Messaging.Messaging';
  }

  return config;
}

/**
 * Parse LEAN results JSON file
 */
async function parseLeanResults(resultsDir: string): Promise<LeanResult | null> {
  try {
    // LEAN outputs results to main.json (not the log or summary files)
    const mainJsonPath = path.join(resultsDir, 'main.json');

    try {
      await fs.access(mainJsonPath);
    } catch {
      console.error('[LEAN] main.json not found in:', resultsDir);

      // Fallback: try to find any result file
      const files = await fs.readdir(resultsDir);
      console.log('[LEAN] Available files:', files);
      return null;
    }

    const content = await fs.readFile(mainJsonPath, 'utf-8');
    console.log('[LEAN] Parsing main.json, size:', content.length);
    return JSON.parse(content) as LeanResult;
  } catch (error) {
    console.error('[LEAN] Error parsing results:', error);
    return null;
  }
}

/**
 * Extended statistics interface for comprehensive LEAN result extraction
 */
interface ExtendedStatistics {
  netProfit: number;
  sharpeRatio: number;
  cagr: number;
  drawdown: number;
  totalTrades: number;
  winRate: number;
  totalWins: number;
  totalLosses: number;
  profitLossRatio: number;
  // Additional stats from LEAN
  alpha: number | null;
  beta: number | null;
  sortinoRatio: number | null;
  treynorRatio: number | null;
  informationRatio: number | null;
  trackingError: number | null;
  annualStdDev: number | null;
  annualVariance: number | null;
  totalFees: number | null;
  averageWin: number | null;
  averageLoss: number | null;
  endEquity: number | null;
}

/**
 * Extract statistics from LEAN results
 * Note: LEAN outputs use camelCase keys (statistics, totalPerformance)
 */
function extractStatistics(results: LeanResult): ExtendedStatistics {
  // LEAN uses camelCase in output JSON
  const rawResults = results as unknown as Record<string, unknown>;
  const stats = (rawResults.statistics || rawResults.Statistics || {}) as Record<string, string>;
  const totalPerf = (rawResults.totalPerformance || rawResults.TotalPerformance || {}) as Record<string, unknown>;
  const portfolioStats = (totalPerf.portfolioStatistics || totalPerf.PortfolioStatistics || {}) as Record<string, unknown>;
  const tradeStats = (totalPerf.tradeStatistics || totalPerf.TradeStatistics || {}) as Record<string, unknown>;


  // Extract numeric values from LEAN portfolioStatistics/tradeStatistics
  // Note: LEAN returns these as STRINGS (e.g., "0.0775"), not numbers
  // Handle NaN, Infinity, and -Infinity which PostgreSQL cannot store
  const getNumber = (val: unknown): number => {
    if (val === undefined || val === null) return 0;
    let num: number;
    if (typeof val === 'number') {
      num = val;
    } else if (typeof val === 'string') {
      num = parseFloat(val);
    } else {
      return 0;
    }
    // PostgreSQL cannot store NaN or Infinity
    if (isNaN(num) || !isFinite(num)) return 0;
    return num;
  };

  const getNumberOrNull = (val: unknown): number | null => {
    if (val === undefined || val === null) return null;
    let num: number;
    if (typeof val === 'number') {
      num = val;
    } else if (typeof val === 'string') {
      num = parseFloat(val);
    } else {
      return null;
    }
    // PostgreSQL cannot store NaN or Infinity
    if (isNaN(num) || !isFinite(num)) return null;
    return num;
  };

  // LEAN returns decimals (0.05 = 5%), but QC Cloud API returns percentages (5.0 = 5%)
  // UI expects QC Cloud format, so multiply LEAN decimals by 100
  const toPercent = (val: unknown): number => getNumber(val) * 100;
  const toPercentOrNull = (val: unknown): number | null => {
    const n = getNumberOrNull(val);
    return n !== null ? n * 100 : null;
  };

  return {
    netProfit: toPercent(portfolioStats['totalNetProfit']),
    sharpeRatio: getNumber(portfolioStats['sharpeRatio']),
    cagr: toPercent(portfolioStats['compoundingAnnualReturn']),
    drawdown: toPercent(portfolioStats['drawdown']),
    totalTrades: getNumber(tradeStats['totalNumberOfTrades']),
    winRate: getNumber(tradeStats['winRate']), // Already 0-1 decimal, UI handles
    totalWins: getNumber(tradeStats['numberOfWinningTrades']),
    totalLosses: getNumber(tradeStats['numberOfLosingTrades']),
    profitLossRatio: getNumber(tradeStats['profitLossRatio']),
    alpha: toPercentOrNull(portfolioStats['alpha']),
    beta: getNumberOrNull(portfolioStats['beta']), // Beta is not a percentage
    sortinoRatio: getNumberOrNull(portfolioStats['sortinoRatio']),
    treynorRatio: getNumberOrNull(portfolioStats['treynorRatio']),
    informationRatio: getNumberOrNull(portfolioStats['informationRatio']),
    trackingError: toPercentOrNull(portfolioStats['trackingError']),
    annualStdDev: toPercentOrNull(portfolioStats['annualStandardDeviation']),
    annualVariance: getNumberOrNull(portfolioStats['annualVariance']),
    totalFees: getNumberOrNull(tradeStats['totalFees']),
    // LEAN uses 'averageProfit' for avg winning trade, 'averageLoss' for avg losing trade
    // These are dollar amounts, not percentages
    averageWin: getNumberOrNull(tradeStats['averageProfit']),
    averageLoss: getNumberOrNull(tradeStats['averageLoss']),
    endEquity: getNumberOrNull(portfolioStats['endEquity']),
  };
}

/**
 * Extract orders from LEAN results
 */
function extractOrders(results: LeanResult): Record<string, unknown> {
  const rawResults = results as unknown as Record<string, unknown>;
  return (rawResults.orders || rawResults.Orders || {}) as Record<string, unknown>;
}

/**
 * Extract insights/alphas from LEAN results
 */
function extractInsights(results: LeanResult): unknown[] {
  const rawResults = results as unknown as Record<string, unknown>;
  return (rawResults.insights || rawResults.Insights || []) as unknown[];
}

/**
 * Extract chart data from LEAN results
 */
function extractChartData(results: LeanResult): Record<string, unknown> {
  // LEAN uses camelCase in output
  const rawResults = results as unknown as Record<string, unknown>;
  const charts = (rawResults.charts || rawResults.Charts || {}) as Record<string, { series?: Record<string, unknown>; Series?: Record<string, unknown> }>;
  const rollingWindow: Record<string, unknown> = {};

  for (const [chartName, chart] of Object.entries(charts)) {
    const series = chart.series || chart.Series || {};
    for (const [seriesName, seriesData] of Object.entries(series)) {
      const data = seriesData as Record<string, unknown>;
      const key = chartName === 'Strategy Equity' ? seriesName : `${chartName} - ${seriesName}`;
      rollingWindow[key] = {
        Name: data.name || data.Name,
        Unit: data.unit || data.Unit,
        Index: data.index || data.Index,
        Values: data.values || data.Values,
        SeriesType: data.seriesType || data.SeriesType,
        Color: data.color || data.Color,
        ScatterMarkerSymbol: data.scatterMarkerSymbol || data.ScatterMarkerSymbol,
      };
    }
  }

  return rollingWindow;
}

/**
 * Run LEAN backtest using Docker
 *
 * @param onChartUpdate - Optional callback for real-time chart updates via ZeroMQ streaming
 *                        When provided, LEAN will use StreamingMessageHandler to push chart data
 * @param userId - User ID for fetching their API keys (Alpaca, Alpha Vantage) for market data
 */
async function runLeanBacktest(
  projectId: number,
  files: LeanFile[],
  symbols: string[],
  startDate: Date,
  endDate: Date,
  cash: number,
  parameters: Record<string, unknown>,
  onProgress: (progress: number) => Promise<void>,
  onChartUpdate?: OnChartUpdate,
  userId?: string,
  brokerageCredentials?: BrokerageCredentials
): Promise<{
  success: boolean;
  error?: string;
  stats?: ExtendedStatistics;
  rollingWindow?: Record<string, unknown>;
  ordersJson?: Record<string, unknown>;
  insightsJson?: unknown[];
  resultJson?: unknown;
  peakMemoryMb?: number;  // Peak memory usage for billing
}> {
  // Create workspace directories for this backtest
  // Only algorithm files and results are per-backtest - data comes from persistent cache
  const workspaceId = `${projectId}-${Date.now()}`;
  const tempBase = path.join(LEAN_WORKSPACES_DIR, `lean-backtest-${workspaceId}`);
  const algorithmDir = path.join(tempBase, 'algorithm');
  const resultsDir = path.join(tempBase, 'results');

  // Host paths for Docker volume mounts (when using sibling container pattern)
  const hostTempBase = path.join(LEAN_HOST_WORKSPACES_DIR, `lean-backtest-${workspaceId}`);

  // Streaming setup
  let streamingPort: number | null = null;
  let zmqSocket: zmq.Pull | null = null;
  const abortController = new AbortController();
  const hostAlgorithmDir = path.join(hostTempBase, 'algorithm');
  const hostResultsDir = path.join(hostTempBase, 'results');
  const hostConfigPath = path.join(hostTempBase, 'config.json');

  try {
    await fs.mkdir(algorithmDir, { recursive: true });
    await fs.mkdir(resultsDir, { recursive: true });

    // Ensure persistent cache has static data (symbol-properties, market-hours)
    await ensureLeanStaticData();

    await onProgress(15);

    // Write algorithm files
    for (const file of files) {
      const filePath = path.join(algorithmDir, file.name);
      await fs.writeFile(filePath, file.content);
    }

    await onProgress(20);

    // When brokerageCredentials is provided, LEAN fetches data directly from the brokerage
    // This is the correct approach for data licensing - users use their own data
    if (brokerageCredentials) {
      console.log(`[LEAN Data] LEAN will fetch data directly from ${brokerageCredentials.provider}`);
      // LEAN handles data fetching internally via DownloaderDataProvider
      // We just need static data (symbol-properties, market-hours) which ensureLeanStaticData handles
    } else {
      // Fallback: use our cached data (development/testing only)
      console.log(`[LEAN Data] Using cached data for symbols: ${symbols.join(', ')}`);
      await ensureMultipleSymbolsCached(symbols, startDate, endDate, userId, 'alpaca');

      // Export to LEAN format
      for (const symbol of symbols) {
        const bars = await getDailyBars(symbol, startDate, endDate);
        await ensureSymbolInCache(symbol, bars, startDate, endDate);
      }
    }

    await onProgress(40);

    // Set up streaming if chart callback is provided
    if (onChartUpdate) {
      streamingPort = await allocateStreamingPort();
      if (streamingPort) {
        console.log(`[LEAN Streaming] Allocated port ${streamingPort} for real-time chart updates`);
        zmqSocket = await createStreamingSubscriber(streamingPort, onChartUpdate, abortController.signal);
      } else {
        console.warn('[LEAN Streaming] No ports available, running without streaming');
      }
    }

    await onProgress(45);

    // Create LEAN config (uses container paths, not host paths)
    // Parameters are injected here and accessible via self.get_parameter() in the algorithm
    // Pass streaming port for chart updates, and brokerage credentials for data fetching
    const config = createLeanConfig(
      startDate, endDate, cash, parameters,
      streamingPort || undefined,
      brokerageCredentials
    );
    const configPath = path.join(tempBase, 'config.json');
    await fs.writeFile(configPath, JSON.stringify(config, null, 2));

    await onProgress(50);

    // Run LEAN Docker container
    // Use pinned LEAN image version for stability
    // See: https://hub.docker.com/r/quantconnect/lean/tags
    // Note: 'latest' tag from Jan 16, 2026 has assembly issues with System.Private.ServiceModel.dll
    // Using 17469 from Jan 9, 2026 which is known to work
    const dockerImage = process.env.LEAN_DOCKER_IMAGE || 'quantconnect/lean:17469';

    // Mount persistent data cache as /Data (read-only)
    // Algorithm and results are per-backtest
    const hostDataCacheDir = getHostCacheDir();
    const dockerArgs = [
      'run',
      '--rm',
      // Force x86_64 platform - LEAN doesn't have ARM images
      // Required for Apple Silicon and other ARM hosts
      '--platform', 'linux/amd64',
      // Use host network so LEAN can push to our ZeroMQ socket
      // This is required for StreamingMessageHandler to reach the host
      ...(streamingPort ? ['--network', 'host'] : []),
      '-v', `${hostAlgorithmDir}:/Algorithm:ro`,
      '-v', `${hostDataCacheDir}:/Data:ro`,  // Persistent cache from S3/local
      '-v', `${hostResultsDir}:/Results`,
      '-v', `${hostConfigPath}:/Lean/config.json:ro`,
      '--memory', process.env.LEAN_MEMORY_LIMIT || '4g',
      '--cpus', process.env.LEAN_CPU_LIMIT || '2',
      dockerImage,
      // Command-line args to override LEAN's defaults
      '--data-folder', '/Data',
      '--results-destination-folder', '/Results',
      '--config', '/Lean/config.json',
    ];

    const localCacheDir = getLocalCacheDir();
    console.log('[LEAN] Starting Docker container...');
    console.log('[LEAN] Command: docker', dockerArgs.join(' '));
    console.log('[LEAN] Algorithm dir:', algorithmDir);
    console.log('[LEAN] Data cache (local):', localCacheDir);
    console.log('[LEAN] Data cache (host mount):', hostDataCacheDir);
    console.log('[LEAN] Results dir:', resultsDir);
    console.log('[LEAN] Streaming port:', streamingPort || 'disabled');
    console.log('[LEAN] Config:', JSON.stringify(config, null, 2));

    // Calculate total date range for progress estimation
    const totalDays = Math.max(1, (endDate.getTime() - startDate.getTime()) / (1000 * 60 * 60 * 24));
    let lastProgressUpdate = Date.now();

    // Poll main.json for intermediate results (LEAN writes every ~30 seconds)
    // This gives us chart updates without needing StreamingMessageHandler
    const mainJsonPath = path.join(resultsDir, 'main.json');
    let lastMainJsonSize = 0;
    let resultPollerInterval: NodeJS.Timeout | null = null;

    if (onChartUpdate) {
      resultPollerInterval = setInterval(async () => {
        try {
          const stat = await fs.stat(mainJsonPath).catch(() => null);
          if (stat && stat.size > lastMainJsonSize) {
            lastMainJsonSize = stat.size;
            const content = await fs.readFile(mainJsonPath, 'utf-8');
            const data = JSON.parse(content);

            // Extract chart updates from the intermediate results
            const charts = data.charts || data.Charts || {};
            for (const [chartName, chart] of Object.entries(charts)) {
              const chartData = chart as { Series?: Record<string, unknown>; series?: Record<string, unknown> };
              const series = chartData.Series || chartData.series || {};
              for (const [seriesName, seriesData] of Object.entries(series)) {
                const sd = seriesData as { Values?: Array<{ x: number; y: number }>; values?: Array<{ x: number; y: number }> };
                const values = sd.Values || sd.values || [];
                if (values.length > 0) {
                  // Send the latest points (last 10 to avoid duplicates)
                  const recentPoints = values.slice(-10);
                  onChartUpdate({
                    chartName,
                    seriesName,
                    points: recentPoints,
                  });
                }
              }
            }
            console.log(`[LEAN Results Poller] Read ${stat.size} bytes from main.json`);
          }
        } catch (err) {
          // File may not exist yet or be in the middle of being written
        }
      }, 5000); // Poll every 5 seconds
    }

    const result = await new Promise<{ code: number; stdout: string; stderr: string }>((resolve) => {
      const proc = spawn('docker', dockerArgs);

      let stdout = '';
      let stderr = '';

      proc.stdout.on('data', (data) => {
        stdout += data.toString();
        const output = data.toString();

        // Parse simulation date from LEAN log lines
        // Format: "TRACE:: Log: 2020-01-01 00:00:00 ..." or "Log: 2020-01-01 00:00:00 ..."
        const dateMatch = output.match(/(?:TRACE::\s+)?Log:\s+(\d{4}-\d{2}-\d{2})/);
        if (dateMatch) {
          const simDate = new Date(dateMatch[1]);
          if (!isNaN(simDate.getTime())) {
            const daysPassed = (simDate.getTime() - startDate.getTime()) / (1000 * 60 * 60 * 24);
            const dateProgress = Math.min(100, Math.max(0, (daysPassed / totalDays) * 100));
            // Map 0-100% to 50-90% range (50% is after data prep, 90% is before results parsing)
            const mappedProgress = 50 + (dateProgress * 0.4);

            // Throttle progress updates to every 2 seconds
            if (Date.now() - lastProgressUpdate > 2000) {
              lastProgressUpdate = Date.now();
              onProgress(mappedProgress).catch(() => {});
              console.log(`[LEAN] Simulation date: ${dateMatch[1]} (${dateProgress.toFixed(1)}%)`);
            }
          }
        }

        // Also check for explicit Progress: lines (backup method)
        if (output.includes('Progress:')) {
          const match = output.match(/Progress:\s*(\d+)%/);
          if (match) {
            const leanProgress = parseInt(match[1], 10);
            const mappedProgress = 50 + (leanProgress * 0.4);
            onProgress(mappedProgress).catch(() => {});
          }
        }
      });

      proc.stderr.on('data', (data) => {
        stderr += data.toString();
        console.error('[LEAN stderr]', data.toString());
      });

      proc.on('close', (code) => {
        resolve({ code: code || 0, stdout, stderr });
      });

      proc.on('error', (err) => {
        resolve({ code: 1, stdout, stderr: err.message });
      });
    });

    // Stop the results poller
    if (resultPollerInterval) {
      clearInterval(resultPollerInterval);
      console.log('[LEAN Results Poller] Stopped');
    }

    await onProgress(90);

    console.log('[LEAN] Docker exit code:', result.code);
    console.log('[LEAN] Docker stdout length:', result.stdout.length);
    console.log('[LEAN] Docker stderr length:', result.stderr.length);

    // List files in results dir
    try {
      const resultFiles = await fs.readdir(resultsDir);
      console.log('[LEAN] Files in results dir:', resultFiles);
    } catch (e) {
      console.log('[LEAN] Could not read results dir:', e);
    }

    if (result.code !== 0) {
      console.error('[LEAN] Docker exited with code:', result.code);
      console.error('[LEAN] Stderr:', result.stderr);
      console.error('[LEAN] Stdout:', result.stdout.substring(0, 2000));

      // Build detailed error message from all available information
      const errorParts: string[] = [`Docker exit code: ${result.code}`];

      // Always include stderr if present (trimmed)
      if (result.stderr && result.stderr.trim()) {
        errorParts.push(`stderr: ${result.stderr.trim().substring(0, 500)}`);
      }

      // Look for specific error patterns in stdout
      if (result.stdout) {
        // Look for Python exceptions
        const exceptionMatch = result.stdout.match(/(?:Exception|Error|Traceback).*?(?=\n\n|\n[A-Z]|$)/s);
        if (exceptionMatch) {
          errorParts.push(`LEAN error: ${exceptionMatch[0].substring(0, 500)}`);
        }

        // Look for LEAN runtime errors
        const runtimeError = result.stdout.match(/Runtime Error:.*$/m);
        if (runtimeError) {
          errorParts.push(runtimeError[0]);
        }
      }

      return {
        success: false,
        error: errorParts.join(' | '),
      };
    }

    // Parse results
    const leanResults = await parseLeanResults(resultsDir);

    if (!leanResults) {
      return {
        success: false,
        error: 'Failed to parse LEAN results',
      };
    }

    const stats = extractStatistics(leanResults);
    const rollingWindow = extractChartData(leanResults);
    const ordersJson = extractOrders(leanResults);
    const insightsJson = extractInsights(leanResults);

    console.log('[LEAN] Extracted stats:', JSON.stringify(stats));
    console.log('[LEAN] Orders count:', Object.keys(ordersJson).length);
    console.log('[LEAN] Insights count:', insightsJson.length);

    return {
      success: true,
      stats,
      rollingWindow,
      ordersJson,
      insightsJson,
      resultJson: leanResults,
    };

  } finally {
    // Clean up streaming resources
    if (zmqSocket) {
      console.log('[LEAN Streaming] Cleaning up ZeroMQ socket');
      abortController.abort();
      try {
        zmqSocket.close();
      } catch (e) {
        console.warn('[LEAN Streaming] Failed to close socket:', e);
      }
    }
    if (streamingPort) {
      await releaseStreamingPort(streamingPort);
      console.log(`[LEAN Streaming] Released port ${streamingPort}`);
    }

    // Skip cleanup for debugging if LEAN_KEEP_TEMP=true
    if (process.env.LEAN_KEEP_TEMP !== 'true') {
      try {
        await fs.rm(tempBase, { recursive: true, force: true });
      } catch (e) {
        console.warn('[LEAN] Failed to cleanup temp dir:', tempBase);
      }
    } else {
      console.log('[LEAN] Keeping temp dir for debugging:', tempBase);
    }
  }
}

/**
 * Extract symbols from algorithm code
 * Handles multiple patterns: equity, crypto, forex, futures, and variable assignments
 */
function extractSymbols(code: string): string[] {
  const symbols = new Set<string>();

  // Patterns for various add methods (Python snake_case and C# PascalCase)
  const addPatterns = [
    // Equity
    /(?:self\.)?add_equity\s*\(\s*["']([A-Z0-9.]+)["']/gi,
    /(?:this\.)?AddEquity\s*\(\s*["']([A-Z0-9.]+)["']/gi,
    // ETF (same as equity but explicit pattern)
    /(?:self\.)?add_equity\s*\(\s*["']([A-Z]{2,5})["']/gi,
    // Crypto
    /(?:self\.)?add_crypto\s*\(\s*["']([A-Z0-9]+)["']/gi,
    /(?:this\.)?AddCrypto\s*\(\s*["']([A-Z0-9]+)["']/gi,
    // Forex
    /(?:self\.)?add_forex\s*\(\s*["']([A-Z0-9]+)["']/gi,
    /(?:this\.)?AddForex\s*\(\s*["']([A-Z0-9]+)["']/gi,
    // CFD
    /(?:self\.)?add_cfd\s*\(\s*["']([A-Z0-9]+)["']/gi,
    /(?:this\.)?AddCfd\s*\(\s*["']([A-Z0-9]+)["']/gi,
    // Future
    /(?:self\.)?add_future\s*\(\s*["']([A-Z0-9]+)["']/gi,
    /(?:this\.)?AddFuture\s*\(\s*["']([A-Z0-9]+)["']/gi,
    // Option (underlying symbol)
    /(?:self\.)?add_option\s*\(\s*["']([A-Z0-9]+)["']/gi,
    /(?:this\.)?AddOption\s*\(\s*["']([A-Z0-9]+)["']/gi,
    // Index
    /(?:self\.)?add_index\s*\(\s*["']([A-Z0-9]+)["']/gi,
    /(?:this\.)?AddIndex\s*\(\s*["']([A-Z0-9]+)["']/gi,
    // set_holdings with symbol as first arg
    /(?:self\.)?set_holdings\s*\(\s*["']([A-Z0-9.]+)["']/gi,
    /(?:this\.)?SetHoldings\s*\(\s*["']([A-Z0-9.]+)["']/gi,
  ];

  // Extract symbols from add* method calls
  for (const pattern of addPatterns) {
    let match;
    pattern.lastIndex = 0; // Reset regex state
    while ((match = pattern.exec(code)) !== null) {
      const symbol = match[1].toUpperCase();
      // Filter out very short or very long symbols, and common false positives
      if (symbol.length >= 1 && symbol.length <= 10 && !['TRUE', 'FALSE', 'NONE', 'NULL'].includes(symbol)) {
        symbols.add(symbol);
      }
    }
  }

  // Also look for common symbol variable assignments like: symbol = "SPY" or SYMBOL = "AAPL"
  const assignmentPattern = /(?:symbol|ticker|equity)\s*=\s*["']([A-Z0-9.]+)["']/gi;
  let match;
  while ((match = assignmentPattern.exec(code)) !== null) {
    const symbol = match[1].toUpperCase();
    if (symbol.length >= 1 && symbol.length <= 10) {
      symbols.add(symbol);
    }
  }

  // Look for symbols in list literals like: symbols = ["SPY", "QQQ", "IWM"]
  const listPattern = /(?:symbols|tickers|equities)\s*=\s*\[([^\]]+)\]/gi;
  while ((match = listPattern.exec(code)) !== null) {
    const listContent = match[1];
    const symbolMatches = listContent.match(/["']([A-Z0-9.]+)["']/g);
    if (symbolMatches) {
      for (const sm of symbolMatches) {
        const symbol = sm.replace(/["']/g, '').toUpperCase();
        if (symbol.length >= 1 && symbol.length <= 10) {
          symbols.add(symbol);
        }
      }
    }
  }

  return Array.from(symbols);
}

/**
 * Extract dates from algorithm code
 * Looks for set_start_date/set_end_date calls in various formats
 */
function extractDatesFromAlgorithm(code: string): { startDate: Date; endDate: Date } | null {
  // Match patterns like:
  // - self.set_start_date(2023, 1, 1) (snake_case)
  // - self.SetStartDate(2023, 1, 1) (CamelCase)
  // - set_start_date(2023, 1, 1)
  // - self.set_start_date(datetime(2023, 1, 1))
  // Note: Using alternation for snake_case and CamelCase since /i flag doesn't help with underscores
  const startPattern = /(?:self\.)?(set_start_date|SetStartDate)\s*\(\s*(?:datetime\s*\(\s*)?(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/;
  const endPattern = /(?:self\.)?(set_end_date|SetEndDate)\s*\(\s*(?:datetime\s*\(\s*)?(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/;

  const startMatch = code.match(startPattern);
  const endMatch = code.match(endPattern);

  // Groups: [0]=full match, [1]=method name, [2]=year, [3]=month, [4]=day
  console.log(`[extractDatesFromAlgorithm] startMatch: ${startMatch ? startMatch.slice(2, 5).join('-') : 'null'}`);
  console.log(`[extractDatesFromAlgorithm] endMatch: ${endMatch ? endMatch.slice(2, 5).join('-') : 'null'}`);

  if (!startMatch || !endMatch) {
    console.log('[extractDatesFromAlgorithm] Could not extract dates, using fallback');
    return null;
  }

  // Python months are 1-indexed, JS Date months are 0-indexed
  // Groups: [2]=year, [3]=month, [4]=day (group [1] is the method name)
  const startDate = new Date(
    parseInt(startMatch[2]),
    parseInt(startMatch[3]) - 1,
    parseInt(startMatch[4])
  );
  const endDate = new Date(
    parseInt(endMatch[2]),
    parseInt(endMatch[3]) - 1,
    parseInt(endMatch[4])
  );

  console.log(`[extractDatesFromAlgorithm] Extracted: ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}`);
  return { startDate, endDate };
}

// Worker node identifier for distributed tracking
const WORKER_NODE = process.env.WORKER_NODE_ID || `worker-${os.hostname()}-${process.pid}`;

/**
 * Process a backtest job with usage tracking for billing
 */
async function processBacktest(job: Job<BacktestJobData>): Promise<void> {
  const { backtestId, projectId, userId, startDate, endDate, cash, parameters } = job.data;

  console.log(`[Backtest Worker] Processing backtest ${backtestId} for user ${userId}`);

  // Track timing and resources
  const jobStartTime = Date.now();
  let usageTrackingId: string | null = null;
  let peakMemoryMb = 0;
  let dataPointsFetched = 0;

  // Parse memory limit from env (e.g., "6g" -> 6144, "1400m" -> 1400)
  const memLimitStr = process.env.LEAN_MEMORY_LIMIT || '4g';
  let memoryLimitMb = 4096;
  const memMatch = memLimitStr.match(/^(\d+)(g|m)$/i);
  if (memMatch) {
    memoryLimitMb = memMatch[2].toLowerCase() === 'g'
      ? parseInt(memMatch[1]) * 1024
      : parseInt(memMatch[1]);
  }

  const cpuCores = parseFloat(process.env.LEAN_CPU_LIMIT || '2');

  const updateProgress = async (progress: number) => {
    await execute(
      'UPDATE qc_backtests SET progress = $2 WHERE qc_backtest_id = $1',
      [backtestId, progress]
    );
  };

  try {
    // Create usage tracking record for billing
    try {
      usageTrackingId = await createUsageRecord({
        userId,
        backtestId,
        cpuCoresUsed: cpuCores,
        memoryLimitMb,
        workerNode: WORKER_NODE,
      });
      console.log(`[Backtest Worker] Created usage tracking record: ${usageTrackingId}`);

      // Link backtest to usage record
      await linkBacktestToUsage(backtestId, usageTrackingId);
    } catch (err) {
      // Usage tracking is non-critical - log and continue
      console.warn('[Backtest Worker] Failed to create usage tracking record:', err);
    }

    // Update status to running
    await execute(
      `UPDATE qc_backtests SET status = 'running', started_at = NOW(), progress = 0
       WHERE qc_backtest_id = $1`,
      [backtestId]
    );

    // Mark usage as running
    if (usageTrackingId) {
      await markUsageRunning(usageTrackingId).catch(() => {});
    }

    // CRITICAL: Users must connect a broker for data licensing compliance
    // LEAN will fetch data directly from their brokerage
    const brokerageCredentials = await getUserBrokerageCredentials(userId);
    if (!brokerageCredentials) {
      throw new Error('BROKER_NOT_CONNECTED: Please connect a broker (e.g., Alpaca) in Settings → API Keys before running backtests. This is required for market data access.');
    }
    console.log(`[Backtest Worker] Using ${brokerageCredentials.provider} for market data`);

    // Get algorithm files
    const files = await query<LeanFile>(
      'SELECT * FROM project_files WHERE project_id = $1',
      [projectId]
    );

    const mainFile = files.find(f => f.isMain || f.name.toLowerCase() === 'main.py');
    if (!mainFile) {
      throw new Error('main.py not found');
    }

    // Extract symbols from algorithm
    const symbols = extractSymbols(mainFile.content);
    console.log(`[Backtest Worker] Found symbols: ${symbols.join(', ') || '(none)'}`);

    // Extract dates from algorithm code - these are the ACTUAL dates LEAN will use
    const algoDates = extractDatesFromAlgorithm(mainFile.content);
    const dataStartDate = algoDates?.startDate || new Date(startDate);
    const dataEndDate = algoDates?.endDate || new Date(endDate);
    console.log(`[Backtest Worker] Algorithm dates: ${dataStartDate.toISOString().split('T')[0]} to ${dataEndDate.toISOString().split('T')[0]}`);

    await updateProgress(5);

    // LEAN fetches data directly from user's brokerage via DownloaderDataProvider
    // No need to pre-cache data - this is handled by createLeanConfig with brokerageCredentials
    console.log(`[Backtest Worker] LEAN will fetch data from ${brokerageCredentials.provider} for symbols: ${symbols.join(', ') || '(none)'}`);

    // Estimate data points for tracking (symbols * days)
    const days = Math.ceil((dataEndDate.getTime() - dataStartDate.getTime()) / (1000 * 60 * 60 * 24));
    dataPointsFetched = symbols.length * days;

    await updateProgress(10);

    // Run LEAN backtest with algorithm's dates
    // Enable real-time chart streaming via Redis pub/sub
    console.log('[Backtest Worker] Running LEAN engine...');
    const leanStartTime = Date.now();

    const onChartUpdate: OnChartUpdate = (update) => {
      // Publish chart update to Redis for SSE to pick up
      publishChartUpdate(backtestId, {
        chartName: update.chartName,
        seriesName: update.seriesName,
        points: update.points,
      }).catch((err) => {
        console.error('[Backtest Worker] Failed to publish chart update:', err);
      });
    };

    // Use file-based streaming instead of StreamingMessageHandler
    // LEAN writes main.json every ~30 seconds, we poll it for chart updates
    // This avoids the broken System.Private.ServiceModel.dll assembly issue
    const result = await runLeanBacktest(
      projectId,
      files,
      symbols,
      dataStartDate,
      dataEndDate,
      cash,
      parameters,
      updateProgress,
      onChartUpdate,        // Enable chart streaming via file polling
      userId,               // Pass userId for tracking
      brokerageCredentials  // Pass brokerage credentials for LEAN to fetch data
    );

    const leanDurationSeconds = (Date.now() - leanStartTime) / 1000;
    peakMemoryMb = result.peakMemoryMb || memoryLimitMb * 0.5; // Estimate if not available

    await updateProgress(95);

    if (!result.success) {
      throw new Error(result.error || 'Backtest failed');
    }

    // Store results with comprehensive statistics
    const stats = result.stats!;
    await execute(
      `UPDATE qc_backtests SET
         status = 'completed',
         completed_at = NOW(),
         progress = 100,
         net_profit = $2,
         sharpe_ratio = $3,
         cagr = $4,
         drawdown = $5,
         total_trades = $6,
         win_rate = $7,
         total_wins = $8,
         total_losses = $9,
         profit_loss_ratio = $10,
         rolling_window = $11,
         result_json = $12,
         orders_json = $13,
         insights_json = $14,
         alpha = $15,
         beta = $16,
         sortino_ratio = $17,
         treynor_ratio = $18,
         information_ratio = $19,
         tracking_error = $20,
         annual_std_dev = $21,
         annual_variance = $22,
         total_fees = $23,
         average_win = $24,
         average_loss = $25,
         end_equity = $26
       WHERE qc_backtest_id = $1`,
      [
        backtestId,
        stats.netProfit,
        stats.sharpeRatio,
        stats.cagr,
        stats.drawdown,
        stats.totalTrades,
        stats.winRate,
        stats.totalWins,
        stats.totalLosses,
        stats.profitLossRatio,
        JSON.stringify(result.rollingWindow),
        JSON.stringify(result.resultJson),
        JSON.stringify(result.ordersJson),
        JSON.stringify(result.insightsJson),
        stats.alpha,
        stats.beta,
        stats.sortinoRatio,
        stats.treynorRatio,
        stats.informationRatio,
        stats.trackingError,
        stats.annualStdDev,
        stats.annualVariance,
        stats.totalFees,
        stats.averageWin,
        stats.averageLoss,
        stats.endEquity,
      ]
    );

    // Complete usage tracking with success
    const totalDurationSeconds = (Date.now() - jobStartTime) / 1000;
    const computeSeconds = leanDurationSeconds * cpuCores; // CPU-seconds for billing

    if (usageTrackingId) {
      await completeUsageRecord({
        usageId: usageTrackingId,
        computeSeconds,
        memoryPeakMb: Math.round(peakMemoryMb),
        memoryMbSeconds: Math.round(peakMemoryMb * leanDurationSeconds),
        dataPointsFetched,
        status: 'completed',
      }).catch((err) => {
        console.warn('[Backtest Worker] Failed to complete usage tracking:', err);
      });
    }

    console.log(`[Backtest Worker] Completed backtest ${backtestId} in ${totalDurationSeconds.toFixed(1)}s (${computeSeconds.toFixed(1)} CPU-seconds)`);
  } catch (error) {
    console.error(`[Backtest Worker] Error in backtest ${backtestId}:`, error);

    // Update status to error
    await execute(
      `UPDATE qc_backtests SET
         status = 'error',
         completed_at = NOW(),
         error_message = $2
       WHERE qc_backtest_id = $1`,
      [backtestId, (error as Error).message]
    );

    // Complete usage tracking with error
    const totalDurationSeconds = (Date.now() - jobStartTime) / 1000;
    if (usageTrackingId) {
      await completeUsageRecord({
        usageId: usageTrackingId,
        computeSeconds: totalDurationSeconds * cpuCores,
        memoryPeakMb: Math.round(peakMemoryMb),
        dataPointsFetched,
        status: 'error',
        errorMessage: (error as Error).message,
      }).catch(() => {});
    }

    throw error;
  }
}

/**
 * Start the backtest worker
 * Initializes distributed port allocation and starts processing jobs
 */
export async function startBacktestWorker(): Promise<Worker> {
  // Clean up any stale ports from previous worker sessions
  const cleaned = await cleanupStalePorts(WORKER_NODE);
  if (cleaned > 0) {
    console.log(`[Backtest Worker] Cleaned up ${cleaned} stale port(s) from previous session`);
  }

  const connection = {
    host: process.env.REDIS_HOST || 'localhost',
    port: parseInt(process.env.REDIS_PORT || '6379', 10),
    maxRetriesPerRequest: null as null,
  };

  const worker = new Worker('backtests', processBacktest, {
    connection,
    concurrency: parseInt(process.env.BACKTEST_CONCURRENCY || '2', 10),
  });

  worker.on('completed', (job) => {
    console.log(`[Backtest Worker] Job ${job.id} completed`);
  });

  worker.on('failed', (job, err) => {
    console.error(`[Backtest Worker] Job ${job?.id} failed:`, err.message);
  });

  worker.on('error', (err) => {
    console.error('[Backtest Worker] Worker error:', err);
  });

  // Graceful shutdown - release all ports
  const shutdown = async () => {
    console.log('[Backtest Worker] Shutting down, releasing ports...');
    for (const port of localAllocatedPorts) {
      await releaseStreamingPort(port).catch(() => {});
    }
    await worker.close();
    process.exit(0);
  };

  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);

  console.log(`[Backtest Worker] Started (node: ${WORKER_NODE})`);

  return worker;
}
