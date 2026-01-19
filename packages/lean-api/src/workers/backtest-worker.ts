/**
 * Backtest Worker
 * Processes backtest jobs using the LEAN engine via Docker
 */

import { Worker, Job } from 'bullmq';
import { query, queryOne, execute } from '../services/database.js';
import { ensureMultipleSymbolsCached, getDailyBars } from '../services/market-data.js';
import {
  getLocalCacheDir,
  getHostCacheDir,
} from '../services/s3-cache.js';
import type { BacktestJobData, LeanFile, MarketDataDaily } from '../types/index.js';
import { spawn } from 'child_process';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as os from 'os';
import archiver from 'archiver';
import { createWriteStream } from 'fs';

// Workspace directory for LEAN backtests - must be accessible from both this container AND the Docker host
// When using sibling containers (Docker socket mounting), paths must exist on the host
const LEAN_WORKSPACES_DIR = process.env.LEAN_WORKSPACES_DIR || '/app/workspaces';

// Host path mapping - needed for sibling container pattern
// The container sees /app/workspaces but Docker daemon sees the host-mounted path
const LEAN_HOST_WORKSPACES_DIR = process.env.LEAN_HOST_WORKSPACES_DIR || LEAN_WORKSPACES_DIR;

// LEAN result types
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
 * Check if LEAN data exists in cache
 * Cache is backed by s3fs mount, so local check is sufficient
 */
async function isDataCached(symbol: string): Promise<boolean> {
  const symbolLower = symbol.toLowerCase();
  const cacheDir = getLocalCacheDir();
  const zipPath = path.join(cacheDir, 'equity', 'usa', 'daily', `${symbolLower}.zip`);
  const mapPath = path.join(cacheDir, 'equity', 'usa', 'map_files', `${symbolLower}.csv`);
  const factorPath = path.join(cacheDir, 'equity', 'usa', 'factor_files', `${symbolLower}.csv`);

  try {
    await Promise.all([
      fs.access(zipPath),
      fs.access(mapPath),
      fs.access(factorPath),
    ]);
    return true;
  } catch {
    return false;
  }
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
 * Ensure symbol data exists in persistent cache - generates if missing
 * Cache is backed by s3fs mount to DO Spaces, so writes persist automatically
 */
async function ensureSymbolInCache(
  symbol: string,
  bars: MarketDataDaily[],
  startDate: Date
): Promise<void> {
  if (await isDataCached(symbol)) {
    console.log(`[LEAN Data Cache] ${symbol} already cached, skipping generation`);
    return;
  }

  console.log(`[LEAN Data Cache] Generating LEAN data for ${symbol}...`);

  if (bars.length > 0) {
    await exportMarketDataToCache(symbol, bars);
    const lastBar = bars[bars.length - 1];
    const referencePrice = Number(lastBar.close);
    await generateMapFileToCache(symbol, startDate);
    await generateFactorFileToCache(symbol, startDate, referencePrice);
  } else {
    // Generate auxiliary files even without price data
    await generateMapFileToCache(symbol, startDate);
    await generateFactorFileToCache(symbol, startDate, 0);
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

/**
 * Create LEAN config.json for the backtest
 * Parameters are passed to LEAN and can be accessed via self.get_parameter() in the algorithm
 */
function createLeanConfig(
  startDate: Date,
  endDate: Date,
  cash: number,
  parameters: Record<string, unknown> = {}
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

  return {
    'environment': 'backtesting',
    'algorithm-type-name': 'main',
    'algorithm-language': 'Python',
    'algorithm-location': '/Algorithm/main.py',  // Container path
    'data-folder': '/Data',                       // Container path
    'results-destination-folder': '/Results',     // Container path
    'messaging-handler': 'QuantConnect.Messaging.Messaging',
    'job-queue-handler': 'QuantConnect.Queues.JobQueue',
    'api-handler': 'QuantConnect.Api.Api',
    // Use LocalDiskMapFileProvider and LocalDiskFactorFileProvider
    // We generate map_files and factor_files for each symbol before running LEAN
    // These files are placed in /Data/equity/usa/map_files/ and /Data/equity/usa/factor_files/
    'map-file-provider': 'QuantConnect.Data.Auxiliary.LocalDiskMapFileProvider',
    'factor-file-provider': 'QuantConnect.Data.Auxiliary.LocalDiskFactorFileProvider',
    'data-provider': 'QuantConnect.Lean.Engine.DataFeeds.DefaultDataProvider',
    'alpha-handler': 'QuantConnect.Lean.Engine.Alphas.DefaultAlphaHandler',
    'data-channel-provider': 'DataChannelProvider',
    'log-handler': 'QuantConnect.Logging.CompositeLogHandler',
    'parameters': leanParameters,
    'close-automatically': true,
    'start-date': startDate.toISOString().split('T')[0],
    'end-date': endDate.toISOString().split('T')[0],
    'cash-amount': cash,
  };
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


  // Parse percentage strings - QC Cloud stores percentages as-is (3.08 = 3.08%)
  // LEAN returns "3.08%" - we just strip the % sign and keep the number
  const parsePercent = (val: string | number | undefined): number => {
    if (val === undefined || val === null) return 0;
    if (typeof val === 'number') return val;
    const str = String(val);
    // Strip % sign and return the number as-is
    return parseFloat(str.replace('%', '')) || 0;
  };

  // Parse percentage to decimal format (for winRate which QC stores as 0.49 = 49%)
  // LEAN returns "49%" - we need to convert to decimal 0.49
  const parsePercentToDecimal = (val: string | number | undefined): number => {
    if (val === undefined || val === null) return 0;
    if (typeof val === 'number') {
      // If already a decimal (< 1), keep it; if percentage (>= 1), divide
      return val >= 1 ? val / 100 : val;
    }
    const str = String(val);
    if (str.includes('%')) {
      // "49%" -> 0.49
      return (parseFloat(str.replace('%', '')) || 0) / 100;
    }
    // Plain number - if >= 1, it's a percentage that needs dividing
    const num = parseFloat(str) || 0;
    return num >= 1 ? num / 100 : num;
  };

  const parseNumber = (val: string | number | undefined): number => {
    if (val === undefined || val === null) return 0;
    return parseFloat(String(val)) || 0;
  };

  const parseNumberOrNull = (val: string | number | undefined): number | null => {
    if (val === undefined || val === null) return null;
    const num = parseFloat(String(val).replace(/[$%,]/g, ''));
    return isNaN(num) ? null : num;
  };

  // Helper to safely extract value from unknown record
  const getValue = (obj: Record<string, unknown>, key: string): string | number | undefined => {
    const val = obj[key];
    if (val === undefined || val === null) return undefined;
    if (typeof val === 'string' || typeof val === 'number') return val;
    return undefined;
  };

  // Stats uses "Title Case", portfolioStats uses camelCase
  // Note: netProfit, cagr, drawdown stored as percentages (3.08 = 3.08%)
  // winRate stored as decimal (0.49 = 49%)
  return {
    netProfit: parsePercent(
      stats['Net Profit'] ||
      getValue(portfolioStats, 'totalNetProfit')
    ),
    sharpeRatio: parseNumber(
      stats['Sharpe Ratio'] ||
      getValue(portfolioStats, 'sharpeRatio')
    ),
    cagr: parsePercent(
      stats['Compounding Annual Return'] ||
      getValue(portfolioStats, 'compoundingAnnualReturn')
    ),
    drawdown: parsePercent(
      stats['Drawdown'] ||
      getValue(portfolioStats, 'drawdown')
    ),
    totalTrades: parseInt(
      stats['Total Orders'] ||
      String(getValue(tradeStats, 'totalNumberOfTrades') || 0),
      10
    ),
    winRate: parsePercentToDecimal(
      stats['Win Rate'] ||
      getValue(tradeStats, 'winRate')
    ),
    // Win/Loss counts
    totalWins: parseInt(
      stats['Total Wins'] ||
      String(getValue(tradeStats, 'numberOfWinningTrades') || 0),
      10
    ),
    totalLosses: parseInt(
      stats['Total Losses'] ||
      String(getValue(tradeStats, 'numberOfLosingTrades') || 0),
      10
    ),
    profitLossRatio: parseNumber(
      stats['Profit-Loss Ratio'] ||
      getValue(tradeStats, 'profitLossRatio')
    ),
    // Additional comprehensive statistics
    alpha: parseNumberOrNull(stats['Alpha'] || getValue(portfolioStats, 'alpha')),
    beta: parseNumberOrNull(stats['Beta'] || getValue(portfolioStats, 'beta')),
    sortinoRatio: parseNumberOrNull(stats['Sortino Ratio'] || getValue(portfolioStats, 'sortinoRatio')),
    treynorRatio: parseNumberOrNull(stats['Treynor Ratio'] || getValue(portfolioStats, 'treynorRatio')),
    informationRatio: parseNumberOrNull(stats['Information Ratio'] || getValue(portfolioStats, 'informationRatio')),
    trackingError: parseNumberOrNull(stats['Tracking Error'] || getValue(portfolioStats, 'trackingError')),
    annualStdDev: parseNumberOrNull(stats['Annual Standard Deviation'] || getValue(portfolioStats, 'annualStandardDeviation')),
    annualVariance: parseNumberOrNull(stats['Annual Variance'] || getValue(portfolioStats, 'annualVariance')),
    totalFees: parseNumberOrNull(stats['Total Fees'] || getValue(tradeStats, 'totalFees')),
    averageWin: parseNumberOrNull(stats['Average Win'] || getValue(tradeStats, 'averageWin')),
    averageLoss: parseNumberOrNull(stats['Average Loss'] || getValue(tradeStats, 'averageLoss')),
    endEquity: parseNumberOrNull(stats['End Equity'] || getValue(portfolioStats, 'endEquity')),
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
 */
async function runLeanBacktest(
  projectId: number,
  files: LeanFile[],
  symbols: string[],
  startDate: Date,
  endDate: Date,
  cash: number,
  parameters: Record<string, unknown>,
  onProgress: (progress: number) => Promise<void>
): Promise<{
  success: boolean;
  error?: string;
  stats?: ExtendedStatistics;
  rollingWindow?: Record<string, unknown>;
  ordersJson?: Record<string, unknown>;
  insightsJson?: unknown[];
  resultJson?: unknown;
}> {
  // Create workspace directories for this backtest
  // Only algorithm files and results are per-backtest - data comes from persistent cache
  const workspaceId = `${projectId}-${Date.now()}`;
  const tempBase = path.join(LEAN_WORKSPACES_DIR, `lean-backtest-${workspaceId}`);
  const algorithmDir = path.join(tempBase, 'algorithm');
  const resultsDir = path.join(tempBase, 'results');

  // Host paths for Docker volume mounts (when using sibling container pattern)
  const hostTempBase = path.join(LEAN_HOST_WORKSPACES_DIR, `lean-backtest-${workspaceId}`);
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

    // Ensure market data is cached from provider (Alpha Vantage, Alpaca, etc.)
    // This fetches from API if not in DB
    console.log(`[LEAN Data] Ensuring market data in DB for symbols: ${symbols.join(', ')}`);
    await ensureMultipleSymbolsCached(symbols, startDate, endDate);

    await onProgress(30);

    // Ensure LEAN-format data exists in persistent cache (generates if missing)
    for (const symbol of symbols) {
      const bars = await getDailyBars(symbol, startDate, endDate);
      await ensureSymbolInCache(symbol, bars, startDate);
    }

    await onProgress(40);

    // Create LEAN config (uses container paths, not host paths)
    // Parameters are injected here and accessible via self.get_parameter() in the algorithm
    const config = createLeanConfig(startDate, endDate, cash, parameters);
    const configPath = path.join(tempBase, 'config.json');
    await fs.writeFile(configPath, JSON.stringify(config, null, 2));

    await onProgress(50);

    // Run LEAN Docker container
    const dockerImage = process.env.LEAN_DOCKER_IMAGE || 'quantconnect/lean:latest';

    // Mount persistent data cache as /Data (read-only)
    // Algorithm and results are per-backtest
    const hostDataCacheDir = getHostCacheDir();
    const dockerArgs = [
      'run',
      '--rm',
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
    console.log('[LEAN] Config:', JSON.stringify(config, null, 2));

    const result = await new Promise<{ code: number; stdout: string; stderr: string }>((resolve) => {
      const proc = spawn('docker', dockerArgs);

      let stdout = '';
      let stderr = '';

      proc.stdout.on('data', (data) => {
        stdout += data.toString();
        // Log progress updates from LEAN
        const output = data.toString();
        if (output.includes('Progress:')) {
          const match = output.match(/Progress:\s*(\d+)%/);
          if (match) {
            const leanProgress = parseInt(match[1], 10);
            // Map LEAN's 0-100 to our 50-90 range
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
 * Looks for set_start_date/set_end_date calls
 */
function extractDatesFromAlgorithm(code: string): { startDate: Date; endDate: Date } | null {
  // Match patterns like: set_start_date(2023, 1, 1) or SetStartDate(2023, 1, 1)
  const startPattern = /set_start_date\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)/i;
  const endPattern = /set_end_date\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)/i;

  const startMatch = code.match(startPattern);
  const endMatch = code.match(endPattern);

  if (!startMatch || !endMatch) {
    return null;
  }

  // Python months are 1-indexed, JS Date months are 0-indexed
  const startDate = new Date(
    parseInt(startMatch[1]),
    parseInt(startMatch[2]) - 1,
    parseInt(startMatch[3])
  );
  const endDate = new Date(
    parseInt(endMatch[1]),
    parseInt(endMatch[2]) - 1,
    parseInt(endMatch[3])
  );

  return { startDate, endDate };
}

/**
 * Process a backtest job
 */
async function processBacktest(job: Job<BacktestJobData>): Promise<void> {
  const { backtestId, projectId, userId, startDate, endDate, cash, parameters } = job.data;

  console.log(`[Backtest Worker] Processing backtest ${backtestId}`);

  const updateProgress = async (progress: number) => {
    await execute(
      'UPDATE qc_backtests SET progress = $2 WHERE qc_backtest_id = $1',
      [backtestId, progress]
    );
  };

  try {
    // Update status to running
    await execute(
      `UPDATE qc_backtests SET status = 'running', started_at = NOW(), progress = 0
       WHERE qc_backtest_id = $1`,
      [backtestId]
    );

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

    // Ensure market data is cached for the algorithm's date range
    if (symbols.length > 0) {
      console.log('[Backtest Worker] Caching market data...');
      await ensureMultipleSymbolsCached(
        symbols,
        dataStartDate,
        dataEndDate
      );
    }

    await updateProgress(10);

    // Run LEAN backtest with algorithm's dates
    console.log('[Backtest Worker] Running LEAN engine...');
    const result = await runLeanBacktest(
      projectId,
      files,
      symbols,
      dataStartDate,
      dataEndDate,
      cash,
      parameters,
      updateProgress
    );

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

    console.log(`[Backtest Worker] Completed backtest ${backtestId}`);
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

    throw error;
  }
}

/**
 * Start the backtest worker
 */
export function startBacktestWorker(): Worker {
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

  console.log('[Backtest Worker] Started');

  return worker;
}
