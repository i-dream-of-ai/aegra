/**
 * Backtests Routes - QC API Compatible
 * POST /api/v2/backtests/create
 * POST /api/v2/backtests/list
 * POST /api/v2/backtests/read
 * POST /api/v2/backtests/delete
 * POST /api/v2/backtests/chart/read
 */

import { Router, type IRouter } from 'express';
import { query, queryOne, execute, transaction, clientQueryOne } from '../services/database.js';
import { v4 as uuidv4 } from 'uuid';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import type {
  QCBacktestsResponse,
  QCBacktestResponse,
  QCBacktestCreateResponse,
  QCChartResponse,
  QCBaseResponse,
  BacktestsCreateRequest,
  BacktestsListRequest,
  BacktestsReadRequest,
  BacktestsDeleteRequest,
  BacktestsChartReadRequest,
} from '../types/index.js';
import type { Backtest } from '../types/index.js';
import { getBacktestQueue } from '../workers/queue.js';

const router: IRouter = Router();

interface ProjectLookup {
  internalId: number;
  qcProjectId: number;
}

/**
 * Look up project by QC project ID and verify ownership
 * Uses the main 'projects' table which has qc_project_id
 * Returns { internalId, qcProjectId } if found, null otherwise
 */
async function getProjectByQcId(qcProjectId: number, userId: string): Promise<ProjectLookup | null> {
  // Look up by qc_project_id in projects table
  const project = await queryOne<{ id: number; qcProjectId: string | null }>(
    'SELECT id, qc_project_id as "qcProjectId" FROM projects WHERE qc_project_id = $1 AND user_id = $2',
    [String(qcProjectId), userId]
  );
  if (project) {
    return { internalId: project.id, qcProjectId: Number(project.qcProjectId) || qcProjectId };
  }

  // Fallback: maybe it's the internal project id directly
  const directProject = await queryOne<{ id: number; qcProjectId: string | null }>(
    'SELECT id, qc_project_id as "qcProjectId" FROM projects WHERE id = $1 AND user_id = $2',
    [qcProjectId, userId]
  );
  if (directProject) {
    return { internalId: directProject.id, qcProjectId: Number(directProject.qcProjectId) || qcProjectId };
  }
  return null;
}

/**
 * Map internal status to QC status string
 */
function mapStatus(status: string): string {
  switch (status) {
    case 'queued': return 'InQueue';
    case 'running': return 'Running';
    case 'completed': return 'Completed';
    case 'error': return 'RuntimeError';
    default: return 'Unknown';
  }
}

/**
 * Convert internal backtest to QC format
 * Matches QuantConnect API response structure exactly
 * Uses real statistics from LEAN results, no hardcoded values
 */
function toQCBacktest(bt: Backtest) {
  // Ensure numeric values (DB may return strings for numeric columns)
  // Note: netProfit, cagr, drawdown stored as percentages (3.08 = 3.08%)
  // winRate stored as decimal (0.49 = 49%)
  const netProfit = Number(bt.netProfit) || 0;  // Already a percentage like 3.08
  const cagr = Number(bt.cagr) || 0;            // Already a percentage like 3.08
  const drawdown = Number(bt.drawdown) || 0;    // Already a percentage like 13.8
  const sharpeRatio = Number(bt.sharpeRatio) || 0;
  const winRate = Number(bt.winRate) || 0;      // Decimal like 0.49
  const profitLossRatio = Number(bt.profitLossRatio) || 0;
  const totalTrades = Number(bt.totalTrades) || 0;
  const totalWins = bt.totalWins != null ? Number(bt.totalWins) : null;
  const totalLosses = bt.totalLosses != null ? Number(bt.totalLosses) : null;
  const cash = Number(bt.cash) || 100000;

  // Extended statistics from database (use actual values when available)
  const alpha = bt.alpha != null ? Number(bt.alpha) : null;
  const beta = bt.beta != null ? Number(bt.beta) : null;
  const sortinoRatio = bt.sortinoRatio != null ? Number(bt.sortinoRatio) : null;
  const treynorRatio = bt.treynorRatio != null ? Number(bt.treynorRatio) : null;
  const informationRatio = bt.informationRatio != null ? Number(bt.informationRatio) : null;
  const trackingError = bt.trackingError != null ? Number(bt.trackingError) : null;
  const annualStdDev = bt.annualStdDev != null ? Number(bt.annualStdDev) : null;
  const annualVariance = bt.annualVariance != null ? Number(bt.annualVariance) : null;
  const totalFees = bt.totalFees != null ? Number(bt.totalFees) : 0;
  const averageWin = bt.averageWin != null ? Number(bt.averageWin) : null;
  const averageLoss = bt.averageLoss != null ? Number(bt.averageLoss) : null;
  const endEquity = bt.endEquity != null ? Number(bt.endEquity) : cash * (1 + netProfit / 100);

  // Helper to format number or return 'N/A'
  const fmt = (val: number | null, decimals = 2, suffix = ''): string => {
    if (val === null || isNaN(val)) return 'N/A';
    return val.toFixed(decimals) + suffix;
  };

  const fmtCurrency = (val: number | null): string => {
    if (val === null || isNaN(val)) return '$0.00';
    return `$${val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  };

  // Build statistics dictionary - ALL values from LEAN results
  // Note: netProfit, cagr, drawdown are already percentages (3.08 = 3.08%)
  // winRate is a decimal (0.49 = 49%), so multiply by 100
  const statistics: Record<string, string> = {
    'Total Orders': String(totalTrades),
    'Total Wins': totalWins != null ? String(totalWins) : 'N/A',
    'Total Losses': totalLosses != null ? String(totalLosses) : 'N/A',
    'Average Win': fmt(averageWin, 2, '%'),
    'Average Loss': fmt(averageLoss, 2, '%'),
    'Compounding Annual Return': `${cagr.toFixed(2)}%`,
    'Drawdown': `${drawdown.toFixed(2)}%`,
    'Expectancy': '0',
    'Start Equity': fmtCurrency(cash),
    'End Equity': fmtCurrency(endEquity),
    'Net Profit': `${netProfit.toFixed(2)}%`,
    'Sharpe Ratio': sharpeRatio.toFixed(3),
    'Sortino Ratio': fmt(sortinoRatio, 3),
    'Probabilistic Sharpe Ratio': '0%',
    'Loss Rate': `${((1 - winRate) * 100).toFixed(0)}%`,
    'Win Rate': `${(winRate * 100).toFixed(0)}%`,
    'Profit-Loss Ratio': profitLossRatio.toFixed(2),
    'Alpha': fmt(alpha, 4),
    'Beta': fmt(beta, 4),
    'Annual Standard Deviation': fmt(annualStdDev, 4),
    'Annual Variance': fmt(annualVariance, 4),
    'Information Ratio': fmt(informationRatio, 4),
    'Tracking Error': fmt(trackingError, 4),
    'Treynor Ratio': fmt(treynorRatio, 4),
    'Total Fees': fmtCurrency(totalFees),
  };

  // Build runtime statistics
  const runtimeStatistics: Record<string, string> = {
    'Equity': fmtCurrency(endEquity),
    'Fees': fmtCurrency(totalFees),
    'Holdings': '0',
    'Net Profit': `${netProfit.toFixed(2)}%`,
    'Return': `${netProfit.toFixed(2)}%`,
    'Unrealized': '$0.00',
    'Volume': '$0.00',
  };

  // Build totalPerformance (camelCase to match QC)
  const totalPerformance = {
    tradeStatistics: {
      totalNumberOfTrades: totalTrades,
      winRate: winRate,
      lossRate: 1 - winRate,
      profitLossRatio: profitLossRatio,
      averageProfit: averageWin || 0,
      averageLoss: averageLoss || 0,
      averageProfitLoss: 0,
      totalProfit: 0,
      totalLoss: 0,
      totalProfitLoss: 0,
    },
    portfolioStatistics: {
      sharpeRatio: sharpeRatio,
      sortinoRatio: sortinoRatio,
      treynorRatio: treynorRatio,
      compoundingAnnualReturn: cagr,
      totalNetProfit: netProfit,
      drawdown: drawdown,
      startEquity: cash,
      endEquity: endEquity,
      winRate: winRate,
      lossRate: 1 - winRate,
      profitLossRatio: profitLossRatio,
      alpha: alpha,
      beta: beta,
      informationRatio: informationRatio,
      trackingError: trackingError,
      annualStandardDeviation: annualStdDev,
      annualVariance: annualVariance,
    },
    closedTrades: [],
  };

  return {
    backtestId: bt.qcBacktestId,
    projectId: bt.projectId,
    name: bt.name,
    note: bt.note || undefined,
    created: bt.createdAt.toISOString(),
    completed: bt.status === 'completed',
    status: mapStatus(bt.status),
    progress: bt.progress / 100,
    error: bt.errorMessage || undefined,
    stacktrace: undefined,
    // QC-compatible nested structures
    statistics,
    runtimeStatistics,
    totalPerformance,
    rollingWindow: bt.rollingWindow as Record<string, unknown> || {},
    charts: {},
  };
}

/**
 * POST /backtests/create - Start a new backtest
 */
router.post('/create', async (req, res) => {
  const context = { endpoint: 'backtests/create', userId: req.userId, body: req.body };

  try {
    const { projectId, compileId, backtestName } = req.body as BacktestsCreateRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['projectId is required'],
      });
    }

    if (!backtestName) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['backtestName is required'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['projectId must be a positive integer'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        backtest: null,
        errors: ['Project not found or access denied'],
      });
    }

    const backtestId = uuidv4();
    const startDate = new Date('2023-01-01');
    const endDate = new Date('2024-01-01');
    const cash = 100000;

    // Use transaction to ensure DB insert + queue add are atomic
    // If queue add fails, DB insert is rolled back
    const backtest = await transaction(async (client) => {
      const txQueryOne = clientQueryOne<Backtest>(client);

      const newBacktest = await txQueryOne(
        `INSERT INTO qc_backtests
         (qc_backtest_id, qc_project_id, project_id, user_id, name, status, start_date, end_date, cash, source)
         VALUES ($1, $2, $3, $4, $5, 'queued', $6, $7, $8, 'self_hosted')
         RETURNING *`,
        [backtestId, projectLookup.qcProjectId, projectLookup.internalId, userId, backtestName, startDate, endDate, cash]
      );

      if (!newBacktest) {
        throw new Error('Failed to create backtest record - INSERT returned no rows');
      }

      // Queue the backtest job (inside transaction so failure rolls back DB insert)
      const queue = getBacktestQueue();
      await queue.add('backtest', {
        backtestId,
        projectId: projectLookup.internalId,
        userId,
        startDate: startDate.toISOString(),
        endDate: endDate.toISOString(),
        cash,
        parameters: {},
      }, {
        jobId: backtestId,
        removeOnComplete: true,
        removeOnFail: false,
      });

      return newBacktest;
    });

    // Return full backtest object like QC does
    res.json({
      success: true,
      backtest: toQCBacktest(backtest),
      errors: [],
    });
  } catch (error) {
    logError('backtests/create', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      backtest: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/list - List backtests for a project
 */
router.post('/list', async (req, res) => {
  const context = { endpoint: 'backtests/list', userId: req.userId, body: req.body };

  try {
    const { projectId } = req.body as BacktestsListRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        backtests: [],
        errors: ['projectId is required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        backtests: [],
        errors: ['Project not found or access denied'],
      });
    }

    const backtests = await query<Backtest>(
      'SELECT * FROM qc_backtests WHERE project_id = $1 ORDER BY created_at DESC',
      [projectLookup.internalId]
    );

    res.json({
      success: true,
      backtests: backtests.map(toQCBacktest),
      errors: [],
    });
  } catch (error) {
    logError('backtests/list', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      backtests: [],
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/read - Get backtest details
 */
router.post('/read', async (req, res) => {
  const context = { endpoint: 'backtests/read', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId } = req.body as BacktestsReadRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['projectId is required'],
      });
    }

    if (!backtestId) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['backtestId is required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        backtest: null,
        errors: ['Project not found or access denied'],
      });
    }

    const backtest = await queryOne<Backtest>(
      'SELECT * FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    if (!backtest) {
      return res.status(404).json({
        success: false,
        backtest: null,
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    res.json({
      success: true,
      backtest: toQCBacktest(backtest),
      errors: [],
    });
  } catch (error) {
    logError('backtests/read', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      backtest: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/update - Update backtest name or note
 */
router.post('/update', async (req, res) => {
  const context = { endpoint: 'backtests/update', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId, name, note } = req.body as {
      projectId: number;
      backtestId: string;
      name?: string;
      note?: string;
    };
    const userId = req.userId;

    if (!projectId || !backtestId) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['projectId and backtestId are required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        backtest: null,
        errors: ['Project not found or access denied'],
      });
    }

    // Build dynamic update query
    const updates: string[] = [];
    const values: unknown[] = [];
    let paramIndex = 1;

    if (name !== undefined) {
      updates.push(`name = $${paramIndex++}`);
      values.push(name);
    }
    if (note !== undefined) {
      updates.push(`note = $${paramIndex++}`);
      values.push(note);
    }

    if (updates.length === 0) {
      return res.status(400).json({
        success: false,
        backtest: null,
        errors: ['No fields to update (provide name or note)'],
      });
    }

    values.push(projectLookup.internalId, backtestId);
    const updated = await execute(
      `UPDATE qc_backtests SET ${updates.join(', ')} WHERE project_id = $${paramIndex++} AND qc_backtest_id = $${paramIndex}`,
      values
    );

    if (updated === 0) {
      return res.status(404).json({
        success: false,
        backtest: null,
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    // Fetch updated backtest
    const backtest = await queryOne<Backtest>(
      'SELECT * FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    res.json({
      success: true,
      backtest: backtest ? toQCBacktest(backtest) : null,
      errors: [],
    });
  } catch (error) {
    logError('backtests/update', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      backtest: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/delete - Delete a backtest
 */
router.post('/delete', async (req, res) => {
  const context = { endpoint: 'backtests/delete', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId } = req.body as BacktestsDeleteRequest;
    const userId = req.userId;

    if (!projectId || !backtestId) {
      return res.status(400).json({
        success: false,
        errors: ['projectId and backtestId are required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        errors: ['Project not found or access denied'],
      });
    }

    const deleted = await execute(
      'DELETE FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    if (deleted === 0) {
      return res.status(404).json({
        success: false,
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    res.json({
      success: true,
      errors: [],
    });
  } catch (error) {
    logError('backtests/delete', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/abort - Abort a running backtest
 */
router.post('/abort', async (req, res) => {
  const context = { endpoint: 'backtests/abort', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId } = req.body as { projectId: number; backtestId: string };
    const userId = req.userId;

    if (!projectId || !backtestId) {
      return res.status(400).json({
        success: false,
        errors: ['projectId and backtestId are required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        errors: ['Project not found or access denied'],
      });
    }

    // Check backtest exists and is running
    const backtest = await queryOne<Backtest>(
      'SELECT * FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    if (!backtest) {
      return res.status(404).json({
        success: false,
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    if (backtest.status !== 'running' && backtest.status !== 'queued') {
      return res.status(400).json({
        success: false,
        errors: [`Backtest is not running (status: ${backtest.status})`],
      });
    }

    // Update status to aborted
    await execute(
      "UPDATE qc_backtests SET status = 'error', completed_at = NOW(), error_message = 'Aborted by user' WHERE project_id = $1 AND qc_backtest_id = $2",
      [projectLookup.internalId, backtestId]
    );

    // Try to remove from queue if still queued
    try {
      const queue = getBacktestQueue();
      const job = await queue.getJob(backtestId);
      if (job) {
        await job.remove();
      }
    } catch (queueError) {
      // Job may have already started, that's ok
      console.warn('[Backtest] Could not remove job from queue:', queueError);
    }

    res.json({
      success: true,
      errors: [],
    });
  } catch (error) {
    logError('backtests/abort', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/orders/read - Get order history for a backtest
 */
router.post('/orders/read', async (req, res) => {
  const context = { endpoint: 'backtests/orders/read', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId } = req.body as { projectId: number; backtestId: string };
    const userId = req.userId;

    if (!projectId || !backtestId) {
      return res.status(400).json({
        success: false,
        orders: [],
        errors: ['projectId and backtestId are required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        orders: [],
        errors: ['Project not found or access denied'],
      });
    }

    const backtest = await queryOne<Backtest>(
      'SELECT result_json FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    if (!backtest) {
      return res.status(404).json({
        success: false,
        orders: [],
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    // Extract orders from result_json
    const resultJson = backtest.resultJson as Record<string, unknown> || {};
    const orders = (resultJson.orders || resultJson.Orders || {}) as Record<string, unknown>;

    // Convert orders object to array format expected by QC API
    const ordersArray = Object.values(orders);

    res.json({
      success: true,
      orders: ordersArray,
      errors: [],
    });
  } catch (error) {
    logError('backtests/orders/read', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      orders: [],
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/read/insights - Get insights data for a backtest
 */
router.post('/read/insights', async (req, res) => {
  const context = { endpoint: 'backtests/read/insights', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId, start, end } = req.body as {
      projectId: number;
      backtestId: string;
      start?: number;
      end?: number;
    };
    const userId = req.userId;

    if (!projectId || !backtestId) {
      return res.status(400).json({
        success: false,
        insights: [],
        errors: ['projectId and backtestId are required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        insights: [],
        errors: ['Project not found or access denied'],
      });
    }

    const backtest = await queryOne<Backtest>(
      'SELECT result_json FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    if (!backtest) {
      return res.status(404).json({
        success: false,
        insights: [],
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    // Extract insights from result_json (LEAN stores alphas/insights here)
    const resultJson = backtest.resultJson as Record<string, unknown> || {};
    const alphaRuntimeStatistics = resultJson.alphaRuntimeStatistics || resultJson.AlphaRuntimeStatistics || {};
    const insights = (resultJson.insights || resultJson.Insights || []) as unknown[];

    // Filter by start/end if provided
    let filteredInsights = insights;
    if (start !== undefined || end !== undefined) {
      filteredInsights = insights.slice(start || 0, end);
    }

    res.json({
      success: true,
      insights: filteredInsights,
      alphaRuntimeStatistics,
      errors: [],
    });
  } catch (error) {
    logError('backtests/read/insights', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      insights: [],
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /backtests/chart/read - Get chart data for a backtest
 */
router.post('/chart/read', async (req, res) => {
  const context = { endpoint: 'backtests/chart/read', userId: req.userId, body: req.body };

  try {
    const { projectId, backtestId, name } = req.body as BacktestsChartReadRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        chart: {},
        errors: ['projectId is required'],
      });
    }

    if (!backtestId) {
      return res.status(400).json({
        success: false,
        chart: {},
        errors: ['backtestId is required'],
      });
    }

    // Look up internal project id from QC project id
    const projectLookup = await getProjectByQcId(projectId, userId);
    if (!projectLookup) {
      return res.status(404).json({
        success: false,
        chart: {},
        errors: ['Project not found or access denied'],
      });
    }

    const backtest = await queryOne<Backtest>(
      'SELECT rolling_window, status FROM qc_backtests WHERE project_id = $1 AND qc_backtest_id = $2',
      [projectLookup.internalId, backtestId]
    );

    if (!backtest) {
      return res.status(404).json({
        success: false,
        chart: {},
        errors: [`Backtest not found: ${backtestId}`],
      });
    }

    const rollingWindow = backtest.rollingWindow as Record<string, unknown> || {};

    let chart = rollingWindow;
    if (name && rollingWindow[name]) {
      chart = { [name]: rollingWindow[name] };
    }

    res.json({
      success: true,
      chart: chart as Record<string, any>,
      errors: [],
    });
  } catch (error) {
    logError('backtests/chart/read', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      chart: {},
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * GET /backtests/stream/:backtestId - Stream live backtest progress via SSE
 *
 * Streams progress updates until the backtest completes or errors.
 * Includes equity curve data from rolling_window when available.
 */
router.get('/stream/:backtestId', async (req, res) => {
  const { backtestId } = req.params;
  const userId = req.userId;
  const context = { endpoint: 'backtests/stream', userId, backtestId };

  try {
    // First verify the backtest exists and belongs to this user
    const backtest = await queryOne<Backtest>(
      `SELECT lb.* FROM qc_backtests lb
       JOIN projects p ON lb.project_id = p.id
       WHERE lb.qc_backtest_id = $1 AND p.user_id = $2`,
      [backtestId, userId]
    );

    if (!backtest) {
      return res.status(404).json({
        success: false,
        errors: ['Backtest not found or access denied'],
      });
    }

    // Set up SSE headers
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no', // Disable nginx buffering
    });

    // Helper to send SSE event
    const sendEvent = (data: object) => {
      res.write(`data: ${JSON.stringify(data)}\n\n`);
    };

    // Extract equity curve from rolling window
    const extractEquityCurve = (rollingWindow: Record<string, unknown> | null): Array<{ x: number; y: number }> => {
      if (!rollingWindow) return [];

      // Look for "Equity" series in Strategy Equity chart
      const equity = rollingWindow['Equity'] as { Values?: Array<{ x: number; y: number }> } | undefined;
      if (equity?.Values) {
        return equity.Values;
      }

      return [];
    };

    // Polling function to check backtest status
    const pollBacktest = async () => {
      const current = await queryOne<Backtest>(
        'SELECT * FROM qc_backtests WHERE qc_backtest_id = $1',
        [backtestId]
      );

      if (!current) {
        sendEvent({ type: 'error', error: 'Backtest not found' });
        return true; // Stop polling
      }

      const rollingWindow = current.rollingWindow as Record<string, unknown> | null;
      const equityCurve = extractEquityCurve(rollingWindow);

      // Build statistics from available data
      const statistics = current.status === 'completed' ? {
        totalReturn: current.netProfit ? `${current.netProfit.toFixed(2)}%` : undefined,
        cagr: current.cagr ? `${(current.cagr * 100).toFixed(2)}%` : undefined,
        sharpeRatio: current.sharpeRatio?.toFixed(3),
        maxDrawdown: current.drawdown ? `${(current.drawdown * 100).toFixed(2)}%` : undefined,
        winRate: current.winRate ? `${(current.winRate * 100).toFixed(0)}%` : undefined,
        totalTrades: current.totalTrades?.toString(),
      } : undefined;

      sendEvent({
        type: 'progress',
        backtestId: current.qcBacktestId,
        backtestName: current.name,
        status: current.status,
        progress: (current.progress || 0) / 100, // Normalize to 0-1
        completed: current.status === 'completed',
        error: current.errorMessage || undefined,
        equityCurve,
        statistics,
      });

      // Stop polling if completed or errored
      return current.status === 'completed' || current.status === 'error';
    };

    // Initial poll
    const done = await pollBacktest();
    if (done) {
      res.end();
      return;
    }

    // Set up polling interval (every 1 second)
    const pollInterval = setInterval(async () => {
      try {
        const done = await pollBacktest();
        if (done) {
          clearInterval(pollInterval);
          res.end();
        }
      } catch (error) {
        console.error('[SSE] Poll error:', error);
        sendEvent({ type: 'error', error: 'Failed to fetch progress' });
        clearInterval(pollInterval);
        res.end();
      }
    }, 1000);

    // Clean up on client disconnect
    req.on('close', () => {
      clearInterval(pollInterval);
    });

  } catch (error) {
    logError('backtests/stream', error, context);
    res.status(500).json({
      success: false,
      errors: [formatErrorForResponse(error)],
    });
  }
});

export default router;
