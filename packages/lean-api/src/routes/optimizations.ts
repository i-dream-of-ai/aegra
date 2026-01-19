/**
 * Optimizations Routes - QC API Compatible
 * POST /api/v2/optimizations/create
 * POST /api/v2/optimizations/list
 * POST /api/v2/optimizations/read
 * POST /api/v2/optimizations/delete
 */

import { Router, type IRouter } from 'express';
import { query, queryOne, execute } from '../services/database.js';
import { v4 as uuidv4 } from 'uuid';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import type {
  QCOptimizationsResponse,
  QCOptimizationResponse,
  OptimizationsListRequest,
  OptimizationsReadRequest,
} from '../types/index.js';
import type { LeanOptimization, LeanProject } from '../types/index.js';
import { getOptimizationQueue } from '../workers/queue.js';

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
 * Convert internal optimization to QC format
 */
function toQCOptimization(opt: LeanOptimization) {
  // Map internal status to QC status
  const statusMap: Record<string, 'New' | 'Running' | 'Completed' | 'Error' | 'Aborted'> = {
    'queued': 'New',
    'running': 'Running',
    'completed': 'Completed',
    'error': 'Error',
  };

  const results = opt.results as Array<{
    id: string;
    name: string;
    parameters: Record<string, number | string>;
    sharpeRatio: number;
    cagr: number;
    netProfit: number;
    drawdown: number;
    totalTrades: number;
    winRate: number;
  }> | null;

  return {
    optimizationId: opt.optimizationId,
    projectId: opt.projectId,
    name: opt.name,
    created: opt.createdAt.toISOString(),
    status: statusMap[opt.status] || 'Error',
    runtimeStatistics: {
      Completed: opt.completedBacktests,
      Failed: 0,
      Running: opt.status === 'running' ? 1 : 0,
      InQueue: opt.totalBacktests ? opt.totalBacktests - opt.completedBacktests : 0,
    },
    backtests: results?.map((r, idx) => ({
      id: r.id || `${opt.optimizationId}-${idx}`,
      name: r.name || `Backtest ${idx + 1}`,
      exitCode: 0,
      parameterSet: r.parameters,
      statistics: {
        SharpeRatio: r.sharpeRatio || 0,
        CompoundingAnnualReturn: r.cagr || 0,
        TotalNetProfit: r.netProfit || 0,
        Drawdown: r.drawdown || 0,
        TotalNumberOfTrades: r.totalTrades || 0,
        WinRate: r.winRate || 0,
      },
    })) || [],
  };
}

/**
 * POST /optimizations/list - List optimizations for a project
 */
router.post('/list', async (req, res) => {
  const context = { endpoint: 'optimizations/list', userId: req.userId, body: req.body };

  try {
    const { projectId } = req.body as OptimizationsListRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        optimizations: [],
        errors: ['projectId is required'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        optimizations: [],
        errors: ['projectId must be a positive integer'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        optimizations: [],
        errors: ['Project not found or access denied'],
      });
    }

    const optimizations = await query<LeanOptimization>(
      'SELECT * FROM lean_optimizations WHERE project_id = $1 ORDER BY created_at DESC',
      [internalProjectId]
    );

    res.json({
      success: true,
      optimizations: optimizations.map(toQCOptimization),
      errors: [],
    });
  } catch (error) {
    logError('optimizations/list', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      optimizations: [],
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /optimizations/read - Get optimization details
 */
router.post('/read', async (req, res) => {
  const context = { endpoint: 'optimizations/read', userId: req.userId, body: req.body };

  try {
    const { projectId, optimizationId } = req.body as OptimizationsReadRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['projectId is required'],
      });
    }

    if (!optimizationId) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['optimizationId is required'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['projectId must be a positive integer'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        optimization: null,
        errors: ['Project not found or access denied'],
      });
    }

    const optimization = await queryOne<LeanOptimization>(
      'SELECT * FROM lean_optimizations WHERE project_id = $1 AND optimization_id = $2',
      [internalProjectId, optimizationId]
    );

    if (!optimization) {
      return res.status(404).json({
        success: false,
        optimization: null,
        errors: [`Optimization not found: ${optimizationId}`],
      });
    }

    res.json({
      success: true,
      optimization: toQCOptimization(optimization),
      errors: [],
    });
  } catch (error) {
    logError('optimizations/read', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      optimization: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /optimizations/create - Create and start an optimization
 */
router.post('/create', async (req, res) => {
  const context = { endpoint: 'optimizations/create', userId: req.userId, body: req.body };

  try {
    const { projectId, compileId, name, target, parameters } = req.body as {
      projectId: number;
      compileId?: string;
      name: string;
      target?: string;
      parameters: Array<{
        name: string;
        min: number;
        max: number;
        step: number;
      }>;
    };
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['projectId is required'],
      });
    }

    if (!name) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['name is required'],
      });
    }

    if (!parameters || !Array.isArray(parameters) || parameters.length === 0) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['parameters array is required and must not be empty'],
      });
    }

    if (parameters.length > 3) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['Maximum 3 parameters allowed for grid search optimization'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['projectId must be a positive integer'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        optimization: null,
        errors: ['Project not found or access denied'],
      });
    }

    // Calculate total number of backtests needed (grid search)
    let totalBacktests = 1;
    for (const param of parameters) {
      const steps = Math.floor((param.max - param.min) / param.step) + 1;
      totalBacktests *= steps;
    }

    const optimizationId = uuidv4();
    const startDate = new Date('2023-01-01');
    const endDate = new Date('2024-01-01');
    const cash = 100000;

    const optimization = await queryOne<LeanOptimization>(
      `INSERT INTO lean_optimizations
       (optimization_id, project_id, user_id, name, status, parameters, target, start_date, end_date, cash, total_backtests)
       VALUES ($1, $2, $3, $4, 'queued', $5, $6, $7, $8, $9, $10)
       RETURNING *`,
      [optimizationId, internalProjectId, userId, name, JSON.stringify(parameters), target || 'SharpeRatio', startDate, endDate, cash, totalBacktests]
    );

    if (!optimization) {
      throw new Error('Failed to create optimization record');
    }

    // Queue the optimization job
    const queue = getOptimizationQueue();
    await queue.add('optimization', {
      optimizationId,
      projectId: internalProjectId,
      userId,
      parameters,
      target: target || 'SharpeRatio',
      startDate: startDate.toISOString(),
      endDate: endDate.toISOString(),
      cash,
    }, {
      jobId: optimizationId,
      removeOnComplete: true,
      removeOnFail: false,
    });

    res.json({
      success: true,
      optimization: toQCOptimization(optimization),
      errors: [],
    });
  } catch (error) {
    logError('optimizations/create', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      optimization: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /optimizations/estimate - Estimate optimization cost/runtime
 */
router.post('/estimate', async (req, res) => {
  const context = { endpoint: 'optimizations/estimate', userId: req.userId, body: req.body };

  try {
    const { projectId, parameters, nodeType, parallelNodes } = req.body as {
      projectId: number;
      organizationId?: string;
      compileId?: string;
      parameters: Array<{ name: string; min: number; max: number; step: number }>;
      nodeType?: string;
      parallelNodes?: number;
    };
    const userId = req.userId;

    if (!projectId || !parameters || parameters.length === 0) {
      return res.status(400).json({
        success: false,
        estimate: null,
        errors: ['projectId and parameters are required'],
      });
    }

    // Calculate total backtests needed (grid search)
    let totalBacktests = 1;
    for (const param of parameters) {
      const steps = Math.floor((param.max - param.min) / param.step) + 1;
      totalBacktests *= steps;
    }

    // Estimate based on parallel nodes (default 1)
    const nodes = parallelNodes || 1;
    const estimatedBacktestTimeSeconds = 30; // Assume 30s per backtest
    const totalTimeSeconds = Math.ceil((totalBacktests * estimatedBacktestTimeSeconds) / nodes);

    res.json({
      success: true,
      estimate: {
        totalBacktests,
        estimatedTimeSeconds: totalTimeSeconds,
        estimatedTimeFormatted: `${Math.ceil(totalTimeSeconds / 60)} minutes`,
        parallelNodes: nodes,
        nodeType: nodeType || 'default',
        // Self-hosted has no cost
        estimatedCost: 0,
        estimatedCostFormatted: '$0.00 (self-hosted)',
      },
      errors: [],
    });
  } catch (error) {
    logError('optimizations/estimate', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      estimate: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /optimizations/update - Update optimization name
 */
router.post('/update', async (req, res) => {
  const context = { endpoint: 'optimizations/update', userId: req.userId, body: req.body };

  try {
    const { projectId, optimizationId, name } = req.body as {
      projectId: number;
      optimizationId: string;
      name: string;
    };
    const userId = req.userId;

    if (!projectId || !optimizationId) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['projectId and optimizationId are required'],
      });
    }

    if (!name) {
      return res.status(400).json({
        success: false,
        optimization: null,
        errors: ['name is required'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        optimization: null,
        errors: ['Project not found or access denied'],
      });
    }

    const updated = await execute(
      'UPDATE lean_optimizations SET name = $1 WHERE project_id = $2 AND optimization_id = $3',
      [name, internalProjectId, optimizationId]
    );

    if (updated === 0) {
      return res.status(404).json({
        success: false,
        optimization: null,
        errors: [`Optimization not found: ${optimizationId}`],
      });
    }

    // Fetch updated optimization
    const optimization = await queryOne<LeanOptimization>(
      'SELECT * FROM lean_optimizations WHERE project_id = $1 AND optimization_id = $2',
      [internalProjectId, optimizationId]
    );

    res.json({
      success: true,
      optimization: optimization ? toQCOptimization(optimization) : null,
      errors: [],
    });
  } catch (error) {
    logError('optimizations/update', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      optimization: null,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /optimizations/abort - Abort a running optimization
 */
router.post('/abort', async (req, res) => {
  const context = { endpoint: 'optimizations/abort', userId: req.userId, body: req.body };

  try {
    const { projectId, optimizationId } = req.body as {
      projectId: number;
      optimizationId: string;
    };
    const userId = req.userId;

    if (!projectId || !optimizationId) {
      return res.status(400).json({
        success: false,
        errors: ['projectId and optimizationId are required'],
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

    // Check optimization exists and is running
    const optimization = await queryOne<LeanOptimization>(
      'SELECT * FROM lean_optimizations WHERE project_id = $1 AND optimization_id = $2',
      [internalProjectId, optimizationId]
    );

    if (!optimization) {
      return res.status(404).json({
        success: false,
        errors: [`Optimization not found: ${optimizationId}`],
      });
    }

    if (optimization.status !== 'running' && optimization.status !== 'queued') {
      return res.status(400).json({
        success: false,
        errors: [`Optimization is not running (status: ${optimization.status})`],
      });
    }

    // Update status to aborted
    await execute(
      "UPDATE lean_optimizations SET status = 'aborted', completed_at = NOW() WHERE project_id = $1 AND optimization_id = $2",
      [internalProjectId, optimizationId]
    );

    // Try to remove from queue if still queued
    try {
      const queue = getOptimizationQueue();
      const job = await queue.getJob(optimizationId);
      if (job) {
        await job.remove();
      }
    } catch (queueError) {
      // Job may have already started, that's ok
      console.warn('[Optimization] Could not remove job from queue:', queueError);
    }

    res.json({
      success: true,
      errors: [],
    });
  } catch (error) {
    logError('optimizations/abort', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /optimizations/delete - Delete an optimization
 */
router.post('/delete', async (req, res) => {
  const context = { endpoint: 'optimizations/delete', userId: req.userId, body: req.body };

  try {
    const { projectId, optimizationId } = req.body as { projectId: number; optimizationId: string };
    const userId = req.userId;

    if (!projectId || !optimizationId) {
      return res.status(400).json({
        success: false,
        errors: ['projectId and optimizationId are required'],
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
      'DELETE FROM lean_optimizations WHERE project_id = $1 AND optimization_id = $2',
      [internalProjectId, optimizationId]
    );

    if (deleted === 0) {
      return res.status(404).json({
        success: false,
        errors: [`Optimization not found: ${optimizationId}`],
      });
    }

    res.json({
      success: true,
      errors: [],
    });
  } catch (error) {
    logError('optimizations/delete', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      errors: [formatErrorForResponse(error)],
    });
  }
});

export default router;
