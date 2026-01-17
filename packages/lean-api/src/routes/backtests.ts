/**
 * Backtests Routes - QC API Compatible
 * POST /api/v2/backtests/create
 * POST /api/v2/backtests/list
 * POST /api/v2/backtests/read
 * POST /api/v2/backtests/delete
 * POST /api/v2/backtests/chart/read
 */

import { Router, type IRouter } from 'express';
import { query, queryOne, execute } from '../services/database.js';
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
import type { LeanBacktest, LeanProject } from '../types/index.js';
import { getBacktestQueue } from '../workers/queue.js';

const router: IRouter = Router();

/**
 * Look up project by QC project ID and verify ownership
 * Uses the main 'projects' table which has qc_project_id
 * Returns internal project id if found, null otherwise
 */
async function getProjectByQcId(qcProjectId: number, userId: string): Promise<number | null> {
  // First try: look up by qc_project_id in main projects table
  const project = await queryOne<{ id: number }>(
    'SELECT id FROM projects WHERE qc_project_id = $1 AND user_id = $2',
    [String(qcProjectId), userId]
  );
  if (project) {
    return project.id;
  }

  // Fallback: maybe it's already an internal id in lean_projects
  const leanProject = await queryOne<{ id: number }>(
    'SELECT id FROM lean_projects WHERE id = $1 AND user_id = $2',
    [qcProjectId, userId]
  );
  return leanProject?.id || null;
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
 */
function toQCBacktest(bt: LeanBacktest) {
  // Build statistics dictionary (string keys matching QC format)
  const statistics: Record<string, string> = {
    'Total Orders': String(bt.totalTrades || 0),
    'Average Win': '0%',
    'Average Loss': '0%',
    'Compounding Annual Return': `${((bt.cagr || 0) * 100).toFixed(2)}%`,
    'Drawdown': `${((bt.drawdown || 0) * 100).toFixed(2)}%`,
    'Expectancy': '0',
    'Start Equity': String(bt.cash || 100000),
    'End Equity': String(Math.round((bt.cash || 100000) * (1 + (bt.netProfit || 0) / 100))),
    'Net Profit': `${((bt.netProfit || 0)).toFixed(2)}%`,
    'Sharpe Ratio': String((bt.sharpeRatio || 0).toFixed(3)),
    'Sortino Ratio': '0',
    'Probabilistic Sharpe Ratio': '0%',
    'Loss Rate': `${(((1 - (bt.winRate || 0)) * 100)).toFixed(0)}%`,
    'Win Rate': `${((bt.winRate || 0) * 100).toFixed(0)}%`,
    'Profit-Loss Ratio': String((bt.profitLossRatio || 0).toFixed(2)),
    'Alpha': '0',
    'Beta': '0',
    'Annual Standard Deviation': '0',
    'Annual Variance': '0',
    'Information Ratio': '0',
    'Tracking Error': '0',
    'Treynor Ratio': '0',
    'Total Fees': '$0.00',
  };

  // Build runtime statistics
  const runtimeStatistics: Record<string, string> = {
    'Equity': String(Math.round((bt.cash || 100000) * (1 + (bt.netProfit || 0) / 100))),
    'Fees': '$0.00',
    'Holdings': '0',
    'Net Profit': `${((bt.netProfit || 0)).toFixed(2)}%`,
    'Return': `${((bt.netProfit || 0)).toFixed(2)}%`,
    'Unrealized': '$0.00',
    'Volume': '$0.00',
  };

  // Build totalPerformance (camelCase to match QC)
  const totalPerformance = {
    tradeStatistics: {
      totalNumberOfTrades: bt.totalTrades || 0,
      winRate: bt.winRate || 0,
      lossRate: bt.winRate ? 1 - bt.winRate : 0,
      profitLossRatio: bt.profitLossRatio || 0,
      averageProfit: 0,
      averageLoss: 0,
      averageProfitLoss: 0,
      totalProfit: 0,
      totalLoss: 0,
      totalProfitLoss: 0,
    },
    portfolioStatistics: {
      sharpeRatio: bt.sharpeRatio || 0,
      compoundingAnnualReturn: bt.cagr || 0,
      totalNetProfit: bt.netProfit || 0,
      drawdown: bt.drawdown || 0,
      startEquity: bt.cash || 100000,
      endEquity: (bt.cash || 100000) * (1 + (bt.netProfit || 0) / 100),
      winRate: bt.winRate || 0,
      lossRate: bt.winRate ? 1 - bt.winRate : 0,
      profitLossRatio: bt.profitLossRatio || 0,
    },
    closedTrades: [],
  };

  return {
    backtestId: bt.backtestId,
    projectId: bt.projectId,
    name: bt.name,
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
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
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

    const backtest = await queryOne<LeanBacktest>(
      `INSERT INTO lean_backtests
       (backtest_id, project_id, user_id, name, status, start_date, end_date, cash)
       VALUES ($1, $2, $3, $4, 'queued', $5, $6, $7)
       RETURNING *`,
      [backtestId, internalProjectId, userId, backtestName, startDate, endDate, cash]
    );

    if (!backtest) {
      throw new Error('Failed to create backtest record - INSERT returned no rows');
    }

    // Queue the backtest job
    const queue = getBacktestQueue();
    await queue.add('backtest', {
      backtestId,
      projectId: internalProjectId,
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
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        backtests: [],
        errors: ['Project not found or access denied'],
      });
    }

    const backtests = await query<LeanBacktest>(
      'SELECT * FROM lean_backtests WHERE project_id = $1 ORDER BY created_at DESC',
      [internalProjectId]
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
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        backtest: null,
        errors: ['Project not found or access denied'],
      });
    }

    const backtest = await queryOne<LeanBacktest>(
      'SELECT * FROM lean_backtests WHERE project_id = $1 AND backtest_id = $2',
      [internalProjectId, backtestId]
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
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        errors: ['Project not found or access denied'],
      });
    }

    const deleted = await execute(
      'DELETE FROM lean_backtests WHERE project_id = $1 AND backtest_id = $2',
      [internalProjectId, backtestId]
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
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        chart: {},
        errors: ['Project not found or access denied'],
      });
    }

    const backtest = await queryOne<LeanBacktest>(
      'SELECT rolling_window, status FROM lean_backtests WHERE project_id = $1 AND backtest_id = $2',
      [internalProjectId, backtestId]
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

export default router;
