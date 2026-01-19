/**
 * Backtest Worker
 * Processes backtest jobs using the LEAN engine via Docker
 */

import { Worker, Job } from 'bullmq';
import { query, queryOne, execute } from '../services/database.js';
import { ensureMultipleSymbolsCached, getDailyBars } from '../services/market-data.js';
import type { BacktestJobData, LeanFile, MarketDataDaily } from '../types/index.js';
import { spawn } from 'child_process';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as os from 'os';
import archiver from 'archiver';
import { createWriteStream } from 'fs';

// Path to LEAN static data files (downloaded once)
const LEAN_STATIC_DATA_DIR = process.env.LEAN_STATIC_DATA_DIR || path.join(process.cwd(), 'data', 'lean-static');

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
 * Convert our cached market data to LEAN's ZIP format
 * LEAN expects ZIP files at: /Data/equity/usa/daily/{symbol}.zip
 * containing CSV with format: YYYYMMDD HH:MM,Open,High,Low,Close,Volume
 * Prices are scaled by 10000 (QC format), no header row
 */
async function exportMarketDataForLean(
  symbol: string,
  bars: MarketDataDaily[],
  dataDir: string
): Promise<void> {
  // LEAN expects data at: /Data/equity/usa/daily/{symbol}.zip
  const symbolDir = path.join(dataDir, 'equity', 'usa', 'daily');
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

  // Write to a temp CSV file first
  const csvContent = csvLines.join('\n');
  const zipPath = path.join(symbolDir, `${symbol.toLowerCase()}.zip`);
  const csvFileName = `${symbol.toLowerCase()}.csv`;

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

  console.log(`[LEAN Data] Created ${zipPath} with ${bars.length} bars`);
}

/**
 * Copy LEAN static data files (symbol-properties, market-hours)
 * These are required for LEAN to run and should be downloaded once
 */
async function copyLeanStaticData(dataDir: string): Promise<void> {
  const staticDirs = ['symbol-properties', 'market-hours'];

  for (const dir of staticDirs) {
    const srcDir = path.join(LEAN_STATIC_DATA_DIR, dir);
    const destDir = path.join(dataDir, dir);

    try {
      await fs.access(srcDir);
      await fs.mkdir(destDir, { recursive: true });

      const files = await fs.readdir(srcDir);
      for (const file of files) {
        await fs.copyFile(path.join(srcDir, file), path.join(destDir, file));
      }
      console.log(`[LEAN Data] Copied ${dir} static data`);
    } catch {
      console.warn(`[LEAN Data] Static data not found at ${srcDir}, downloading...`);
      await downloadLeanStaticData(dir, destDir);
    }
  }
}

/**
 * Download LEAN static data file from GitHub
 */
async function downloadLeanStaticData(type: string, destDir: string): Promise<void> {
  await fs.mkdir(destDir, { recursive: true });

  const files: Record<string, string> = {
    'symbol-properties': 'symbol-properties-database.csv',
    'market-hours': 'market-hours-database.json',
  };

  const filename = files[type];
  if (!filename) {
    console.warn(`[LEAN Data] Unknown static data type: ${type}`);
    return;
  }

  const url = `https://raw.githubusercontent.com/QuantConnect/Lean/master/Data/${type}/${filename}`;

  try {
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const content = await response.text();
    await fs.writeFile(path.join(destDir, filename), content);
    console.log(`[LEAN Data] Downloaded ${type}/${filename}`);

    // Also cache it in the static data dir for future use
    const cacheDir = path.join(LEAN_STATIC_DATA_DIR, type);
    await fs.mkdir(cacheDir, { recursive: true });
    await fs.writeFile(path.join(cacheDir, filename), content);
  } catch (error) {
    console.error(`[LEAN Data] Failed to download ${type}/${filename}:`, error);
  }
}

/**
 * Create LEAN config.json for the backtest
 */
function createLeanConfig(
  startDate: Date,
  endDate: Date,
  cash: number
): object {
  // Note: These paths are INSIDE the Docker container, not host paths
  // The Docker volumes map:
  //   hostAlgorithmDir -> /Algorithm
  //   hostDataDir -> /Data
  //   hostResultsDir -> /Results
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
    // Use LocalZipMapFileProvider which doesn't require map_files directory on disk
    // This avoids errors when running without full QC data infrastructure
    'map-file-provider': 'QuantConnect.Data.Auxiliary.LocalZipMapFileProvider',
    'factor-file-provider': 'QuantConnect.Data.Auxiliary.LocalZipFactorFileProvider',
    'data-provider': 'QuantConnect.Lean.Engine.DataFeeds.DefaultDataProvider',
    'alpha-handler': 'QuantConnect.Lean.Engine.Alphas.DefaultAlphaHandler',
    'data-channel-provider': 'DataChannelProvider',
    'log-handler': 'QuantConnect.Logging.CompositeLogHandler',
    'parameters': {},
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
 * Extract statistics from LEAN results
 * Note: LEAN outputs use camelCase keys (statistics, totalPerformance)
 */
function extractStatistics(results: LeanResult): {
  netProfit: number;
  sharpeRatio: number;
  cagr: number;
  drawdown: number;
  totalTrades: number;
  winRate: number;
  profitLossRatio: number;
} {
  // LEAN uses camelCase in output JSON
  const rawResults = results as unknown as Record<string, unknown>;
  const stats = (rawResults.statistics || rawResults.Statistics || {}) as Record<string, string>;
  const totalPerf = (rawResults.totalPerformance || rawResults.TotalPerformance || {}) as Record<string, unknown>;
  const portfolioStats = (totalPerf.portfolioStatistics || totalPerf.PortfolioStatistics || {}) as Record<string, unknown>;
  const tradeStats = (totalPerf.tradeStatistics || totalPerf.TradeStatistics || {}) as Record<string, unknown>;


  // Parse percentage strings (LEAN returns "12.34%" or decimals like 0.1234)
  const parsePercent = (val: string | number | undefined): number => {
    if (val === undefined || val === null) return 0;
    if (typeof val === 'number') return val * 100; // Already a decimal
    return parseFloat(String(val).replace('%', '')) || 0;
  };

  const parseNumber = (val: string | number | undefined): number => {
    if (val === undefined || val === null) return 0;
    return parseFloat(String(val)) || 0;
  };

  // Helper to safely extract value from unknown record
  const getValue = (obj: Record<string, unknown>, key: string): string | number | undefined => {
    const val = obj[key];
    if (val === undefined || val === null) return undefined;
    if (typeof val === 'string' || typeof val === 'number') return val;
    return undefined;
  };

  // Stats uses "Title Case", portfolioStats uses camelCase
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
    winRate: parsePercent(
      stats['Win Rate'] ||
      getValue(tradeStats, 'winRate')
    ),
    profitLossRatio: parseNumber(
      stats['Profit-Loss Ratio'] ||
      getValue(tradeStats, 'profitLossRatio')
    ),
  };
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
  netProfit?: number;
  sharpeRatio?: number;
  cagr?: number;
  drawdown?: number;
  totalTrades?: number;
  winRate?: number;
  profitLossRatio?: number;
  rollingWindow?: Record<string, unknown>;
  resultJson?: unknown;
}> {
  // Create workspace directories for this backtest
  // Uses shared volume accessible from both this container and the Docker host
  const workspaceId = `${projectId}-${Date.now()}`;
  const tempBase = path.join(LEAN_WORKSPACES_DIR, `lean-backtest-${workspaceId}`);
  const algorithmDir = path.join(tempBase, 'algorithm');
  const dataDir = path.join(tempBase, 'data');
  const resultsDir = path.join(tempBase, 'results');

  // Host paths for Docker volume mounts (when using sibling container pattern)
  const hostTempBase = path.join(LEAN_HOST_WORKSPACES_DIR, `lean-backtest-${workspaceId}`);
  const hostAlgorithmDir = path.join(hostTempBase, 'algorithm');
  const hostDataDir = path.join(hostTempBase, 'data');
  const hostResultsDir = path.join(hostTempBase, 'results');
  const hostConfigPath = path.join(hostTempBase, 'config.json');

  try {
    await fs.mkdir(algorithmDir, { recursive: true });
    await fs.mkdir(dataDir, { recursive: true });
    await fs.mkdir(resultsDir, { recursive: true });

    // Copy LEAN static data files (symbol-properties, market-hours)
    await copyLeanStaticData(dataDir);

    await onProgress(15);

    // Write algorithm files
    for (const file of files) {
      const filePath = path.join(algorithmDir, file.name);
      await fs.writeFile(filePath, file.content);
    }

    await onProgress(20);

    // Export market data for each symbol
    for (const symbol of symbols) {
      const bars = await getDailyBars(symbol, startDate, endDate);
      if (bars.length > 0) {
        await exportMarketDataForLean(symbol, bars, dataDir);
      } else {
        console.warn(`[LEAN Data] No data for ${symbol} in date range`);
      }
    }

    await onProgress(40);

    // Create LEAN config (uses container paths, not host paths)
    const config = createLeanConfig(startDate, endDate, cash);
    const configPath = path.join(tempBase, 'config.json');
    await fs.writeFile(configPath, JSON.stringify(config, null, 2));

    await onProgress(50);

    // Run LEAN Docker container
    const dockerImage = process.env.LEAN_DOCKER_IMAGE || 'quantconnect/lean:latest';

    // LEAN needs command-line arguments to use our config properly
    // Use HOST paths for volume mounts when using sibling container pattern
    const dockerArgs = [
      'run',
      '--rm',
      '-v', `${hostAlgorithmDir}:/Algorithm:ro`,
      '-v', `${hostDataDir}:/Data:ro`,
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

    console.log('[LEAN] Starting Docker container...');
    console.log('[LEAN] Command: docker', dockerArgs.join(' '));
    console.log('[LEAN] Algorithm dir:', algorithmDir);
    console.log('[LEAN] Data dir:', dataDir);
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

    console.log('[LEAN] Extracted stats:', JSON.stringify(stats));

    return {
      success: true,
      ...stats,
      rollingWindow,
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
 */
function extractSymbols(code: string): string[] {
  const symbols: string[] = [];
  const patterns = [
    /add_equity\s*\(\s*["']([A-Z]+)["']/gi,
    /AddEquity\s*\(\s*["']([A-Z]+)["']/gi,
    /self\.add_equity\s*\(\s*["']([A-Z]+)["']/gi,
    /self\.AddEquity\s*\(\s*["']([A-Z]+)["']/gi,
  ];

  for (const pattern of patterns) {
    let match;
    while ((match = pattern.exec(code)) !== null) {
      if (!symbols.includes(match[1])) {
        symbols.push(match[1]);
      }
    }
  }

  return symbols;
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
      'UPDATE lean_backtests SET progress = $2 WHERE backtest_id = $1',
      [backtestId, progress]
    );
  };

  try {
    // Update status to running
    await execute(
      `UPDATE lean_backtests SET status = 'running', started_at = NOW(), progress = 0
       WHERE backtest_id = $1`,
      [backtestId]
    );

    // Get algorithm files
    const files = await query<LeanFile>(
      'SELECT * FROM lean_files WHERE project_id = $1',
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

    // Store results
    await execute(
      `UPDATE lean_backtests SET
         status = 'completed',
         completed_at = NOW(),
         progress = 100,
         net_profit = $2,
         sharpe_ratio = $3,
         cagr = $4,
         drawdown = $5,
         total_trades = $6,
         win_rate = $7,
         profit_loss_ratio = $8,
         rolling_window = $9,
         result_json = $10
       WHERE backtest_id = $1`,
      [
        backtestId,
        result.netProfit,
        result.sharpeRatio,
        result.cagr,
        result.drawdown,
        result.totalTrades,
        result.winRate,
        result.profitLossRatio,
        JSON.stringify(result.rollingWindow),
        JSON.stringify(result.resultJson),
      ]
    );

    console.log(`[Backtest Worker] Completed backtest ${backtestId}`);
  } catch (error) {
    console.error(`[Backtest Worker] Error in backtest ${backtestId}:`, error);

    // Update status to error
    await execute(
      `UPDATE lean_backtests SET
         status = 'error',
         completed_at = NOW(),
         error_message = $2
       WHERE backtest_id = $1`,
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
