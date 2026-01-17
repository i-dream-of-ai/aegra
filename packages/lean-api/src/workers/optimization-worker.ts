/**
 * Optimization Worker
 * Processes optimization jobs by running multiple backtests with different parameters
 * Uses grid search strategy (same as QC cloud)
 */

import { Worker, Job } from 'bullmq';
import { query, queryOne, execute } from '../services/database.js';
import type { OptimizationJobData, LeanFile } from '../types/index.js';
import { getBacktestQueue } from './queue.js';
import { v4 as uuidv4 } from 'uuid';

interface ParameterConfig {
  name: string;
  min: number;
  max: number;
  step: number;
}

interface BacktestResult {
  id: string;
  name: string;
  parameters: Record<string, number>;
  sharpeRatio: number;
  cagr: number;
  netProfit: number;
  drawdown: number;
  totalTrades: number;
  winRate: number;
}

/**
 * Generate all parameter combinations for grid search
 */
function generateParameterGrid(parameters: ParameterConfig[]): Record<string, number>[] {
  if (parameters.length === 0) return [{}];

  const [first, ...rest] = parameters;
  const restCombinations = generateParameterGrid(rest);
  const combinations: Record<string, number>[] = [];

  for (let value = first.min; value <= first.max; value += first.step) {
    // Round to avoid floating point issues
    const roundedValue = Math.round(value * 1000000) / 1000000;
    for (const restCombo of restCombinations) {
      combinations.push({
        [first.name]: roundedValue,
        ...restCombo,
      });
    }
  }

  return combinations;
}

/**
 * Run a single backtest with specific parameters
 * Waits for the backtest to complete and returns the results
 */
async function runBacktestWithParameters(
  projectId: number,
  userId: string,
  parameters: Record<string, number>,
  startDate: string,
  endDate: string,
  cash: number
): Promise<BacktestResult | null> {
  const backtestId = uuidv4();
  const name = `Opt-${Object.entries(parameters).map(([k, v]) => `${k}=${v}`).join(',')}`;

  // Create backtest record
  await execute(
    `INSERT INTO lean_backtests
     (backtest_id, project_id, user_id, name, status, start_date, end_date, cash, parameters)
     VALUES ($1, $2, $3, $4, 'queued', $5, $6, $7, $8)`,
    [backtestId, projectId, userId, name, startDate, endDate, cash, JSON.stringify(parameters)]
  );

  // Queue the backtest job
  const queue = getBacktestQueue();
  await queue.add('backtest', {
    backtestId,
    projectId,
    userId,
    startDate,
    endDate,
    cash,
    parameters,
  }, {
    jobId: backtestId,
    removeOnComplete: true,
    removeOnFail: false,
  });

  // Wait for the backtest to complete (poll every 2 seconds)
  const maxWaitTime = 10 * 60 * 1000; // 10 minutes max
  const pollInterval = 2000;
  let elapsed = 0;

  while (elapsed < maxWaitTime) {
    await new Promise(resolve => setTimeout(resolve, pollInterval));
    elapsed += pollInterval;

    const backtest = await queryOne<{
      status: string;
      netProfit: number | null;
      sharpeRatio: number | null;
      cagr: number | null;
      drawdown: number | null;
      totalTrades: number | null;
      winRate: number | null;
      errorMessage: string | null;
    }>(
      `SELECT status, net_profit as "netProfit", sharpe_ratio as "sharpeRatio",
              cagr, drawdown, total_trades as "totalTrades", win_rate as "winRate",
              error_message as "errorMessage"
       FROM lean_backtests WHERE backtest_id = $1`,
      [backtestId]
    );

    if (!backtest) {
      console.error(`[Optimization] Backtest ${backtestId} not found`);
      return null;
    }

    if (backtest.status === 'completed') {
      return {
        id: backtestId,
        name,
        parameters,
        sharpeRatio: backtest.sharpeRatio || 0,
        cagr: backtest.cagr || 0,
        netProfit: backtest.netProfit || 0,
        drawdown: backtest.drawdown || 0,
        totalTrades: backtest.totalTrades || 0,
        winRate: backtest.winRate || 0,
      };
    }

    if (backtest.status === 'error') {
      console.error(`[Optimization] Backtest ${backtestId} failed: ${backtest.errorMessage}`);
      return null;
    }
  }

  console.error(`[Optimization] Backtest ${backtestId} timed out`);
  return null;
}

/**
 * Process an optimization job
 */
async function processOptimization(job: Job<OptimizationJobData>): Promise<void> {
  const { optimizationId, projectId, userId, parameters, target, startDate, endDate, cash } = job.data;

  console.log(`[Optimization Worker] Processing optimization ${optimizationId}`);

  try {
    // Update status to running
    await execute(
      `UPDATE lean_optimizations SET status = 'running', started_at = NOW()
       WHERE optimization_id = $1`,
      [optimizationId]
    );

    // Generate all parameter combinations
    const parameterGrid = generateParameterGrid(parameters);
    const totalBacktests = parameterGrid.length;

    console.log(`[Optimization Worker] Running ${totalBacktests} backtests for optimization ${optimizationId}`);

    const results: BacktestResult[] = [];
    let completedCount = 0;

    // Run backtests sequentially (could be parallelized with concurrency limit)
    for (const paramSet of parameterGrid) {
      const result = await runBacktestWithParameters(
        projectId,
        userId,
        paramSet,
        startDate,
        endDate,
        cash
      );

      if (result) {
        results.push(result);
      }

      completedCount++;

      // Update progress
      const progress = (completedCount / totalBacktests) * 100;
      await execute(
        `UPDATE lean_optimizations SET progress = $2, completed_backtests = $3
         WHERE optimization_id = $1`,
        [optimizationId, progress, completedCount]
      );

      console.log(`[Optimization Worker] Progress: ${completedCount}/${totalBacktests}`);
    }

    // Find best result based on target
    let bestResult: BacktestResult | null = null;
    if (results.length > 0) {
      bestResult = results.reduce((best, current) => {
        const targetKey = target.toLowerCase().replace(/\s+/g, '') as keyof BacktestResult;
        const bestValue = (best[targetKey] as number) || 0;
        const currentValue = (current[targetKey] as number) || 0;

        // For drawdown, lower is better
        if (targetKey === 'drawdown') {
          return currentValue < bestValue ? current : best;
        }
        return currentValue > bestValue ? current : best;
      });
    }

    // Store results
    await execute(
      `UPDATE lean_optimizations SET
         status = 'completed',
         completed_at = NOW(),
         progress = 100,
         results = $2,
         best_parameters = $3
       WHERE optimization_id = $1`,
      [
        optimizationId,
        JSON.stringify(results),
        bestResult ? JSON.stringify(bestResult.parameters) : null,
      ]
    );

    console.log(`[Optimization Worker] Completed optimization ${optimizationId}`);
    if (bestResult) {
      console.log(`[Optimization Worker] Best ${target}: ${bestResult[target.toLowerCase().replace(/\s+/g, '') as keyof BacktestResult]}`);
      console.log(`[Optimization Worker] Best parameters:`, bestResult.parameters);
    }

  } catch (error) {
    console.error(`[Optimization Worker] Error in optimization ${optimizationId}:`, error);

    // Update status to error
    await execute(
      `UPDATE lean_optimizations SET
         status = 'error',
         completed_at = NOW(),
         error_message = $2
       WHERE optimization_id = $1`,
      [optimizationId, (error as Error).message]
    );

    throw error;
  }
}

/**
 * Start the optimization worker
 */
export function startOptimizationWorker(): Worker {
  const connection = {
    host: process.env.REDIS_HOST || 'localhost',
    port: parseInt(process.env.REDIS_PORT || '6379', 10),
    maxRetriesPerRequest: null as null,
  };

  const worker = new Worker('optimizations', processOptimization, {
    connection,
    concurrency: 1, // Only one optimization at a time
  });

  worker.on('completed', (job) => {
    console.log(`[Optimization Worker] Job ${job.id} completed`);
  });

  worker.on('failed', (job, err) => {
    console.error(`[Optimization Worker] Job ${job?.id} failed:`, err.message);
  });

  worker.on('error', (err) => {
    console.error('[Optimization Worker] Worker error:', err);
  });

  console.log('[Optimization Worker] Started');

  return worker;
}
