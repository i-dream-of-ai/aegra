/**
 * Projects Routes - QC API Compatible
 * POST /api/v2/projects/read
 * POST /api/v2/projects/create
 * POST /api/v2/projects/delete
 */

import { Router, type IRouter } from 'express';
import { query, queryOne, execute } from '../services/database.js';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import type {
  QCProjectsResponse,
  QCProjectCreateResponse,
  QCBaseResponse,
  ProjectsReadRequest,
  ProjectsCreateRequest,
  ProjectsDeleteRequest,
} from '../types/index.js';
import type { LeanProject } from '../types/index.js';

const router: IRouter = Router();

/**
 * Convert date to ISO string (handles both Date objects and strings)
 */
function toISOString(date: Date | string): string {
  if (date instanceof Date) {
    return date.toISOString();
  }
  // If it's already a string, ensure it's valid ISO format
  return new Date(date).toISOString();
}

/**
 * Convert internal project to QC format
 * Matches QuantConnect API response structure exactly
 */
function toQCProject(project: LeanProject) {
  return {
    projectId: project.id,
    organizationId: '', // Self-hosted doesn't use orgs
    name: project.name,
    created: toISOString(project.createdAt),
    modified: toISOString(project.modifiedAt),
    ownerId: 0,
    language: project.language,
    collaborators: [],
    leanVersionId: 0,
    leanPinnedToMaster: true,
    owner: true,
    description: '',
    channelId: '',
    parameters: {},
    libraries: [],
    grid: {},
    liveGrid: {},
    paperEquity: 0,
    lastLiveDeployment: null,
    liveForm: {},
    encrypted: false,
    codeRunning: false,
    leanEnvironment: 0,
    encryptionKey: {},
  };
}

/**
 * POST /projects/read - List projects or get specific project
 */
router.post('/read', async (req, res) => {
  const context = { endpoint: 'projects/read', userId: req.userId, body: req.body };

  try {
    const { projectId } = req.body as ProjectsReadRequest;
    const userId = req.userId;

    let projects: LeanProject[];

    if (projectId) {
      const project = await queryOne<LeanProject>(
        'SELECT * FROM lean_projects WHERE id = $1 AND user_id = $2',
        [projectId, userId]
      );
      projects = project ? [project] : [];
    } else {
      projects = await query<LeanProject>(
        'SELECT * FROM lean_projects WHERE user_id = $1 ORDER BY modified_at DESC',
        [userId]
      );
    }

    const response: QCProjectsResponse = {
      success: true,
      projects: projects.map(toQCProject),
      errors: [],
    };

    res.json(response);
  } catch (error) {
    logError('projects/read', error, context);
    const statusCode = getErrorStatusCode(error);
    const response: QCProjectsResponse = {
      success: false,
      projects: [],
      errors: [formatErrorForResponse(error)],
    };
    res.status(statusCode).json(response);
  }
});

/**
 * POST /projects/create - Create a new project
 */
router.post('/create', async (req, res) => {
  const context = { endpoint: 'projects/create', userId: req.userId, body: req.body };

  try {
    const { name, language = 'Py' } = req.body as ProjectsCreateRequest;
    const userId = req.userId;

    if (!name) {
      const response: QCProjectCreateResponse = {
        success: false,
        projects: [],
        errors: ['Project name is required'],
      };
      return res.status(400).json(response);
    }

    if (name.length > 100) {
      const response: QCProjectCreateResponse = {
        success: false,
        projects: [],
        errors: ['Project name must be 100 characters or less'],
      };
      return res.status(400).json(response);
    }

    if (!['Py', 'C#'].includes(language)) {
      const response: QCProjectCreateResponse = {
        success: false,
        projects: [],
        errors: ['Language must be "Py" or "C#"'],
      };
      return res.status(400).json(response);
    }

    const project = await queryOne<LeanProject>(
      `INSERT INTO lean_projects (user_id, name, language)
       VALUES ($1, $2, $3)
       RETURNING *`,
      [userId, name, language]
    );

    if (!project) {
      throw new Error('Failed to create project - no record returned');
    }

    // Create default main.py file with multi-asset template
    const className = name.replace(/[^a-zA-Z0-9]/g, '') || 'MyStrategy';
    const defaultCode = `# region imports
from AlgorithmImports import *
# endregion

# =============================================================================
# STARTER TEMPLATE - This is NOT the user's strategy!
# This is a placeholder template that should be replaced with the user's
# actual trading logic. The AI agent should help the user build their
# custom strategy from scratch or modify this template significantly.
# =============================================================================

class ${className}Algorithm(QCAlgorithm):

    def initialize(self):
        self.set_start_date(2023, 1, 1)
        self.set_end_date(2024, 1, 1)
        self.set_cash(100000)

        # Add multiple assets for diversified portfolio
        self.add_equity("SPY", Resolution.DAILY)
        self.add_equity("BND", Resolution.DAILY)
        self.add_equity("AAPL", Resolution.DAILY)

    def on_data(self, data: Slice):
        # Simple equal-weight portfolio (placeholder logic)
        if not self.portfolio.invested:
            self.set_holdings("SPY", 0.33)
            self.set_holdings("BND", 0.33)
            self.set_holdings("AAPL", 0.33)
`;

    await execute(
      `INSERT INTO lean_files (project_id, name, content, is_main)
       VALUES ($1, $2, $3, true)`,
      [project.id, 'main.py', defaultCode]
    );

    const response: QCProjectCreateResponse = {
      success: true,
      projects: [toQCProject(project)],
      errors: [],
    };

    res.json(response);
  } catch (error) {
    logError('projects/create', error, context);
    const statusCode = getErrorStatusCode(error);
    const response: QCProjectCreateResponse = {
      success: false,
      projects: [],
      errors: [formatErrorForResponse(error)],
    };
    res.status(statusCode).json(response);
  }
});

/**
 * POST /projects/update - Update project name or description
 */
router.post('/update', async (req, res) => {
  const context = { endpoint: 'projects/update', userId: req.userId, body: req.body };

  try {
    const { projectId, name, description } = req.body as {
      projectId: number;
      name?: string;
      description?: string;
    };
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        projects: [],
        errors: ['projectId is required'],
      });
    }

    if (name !== undefined && name.length > 100) {
      return res.status(400).json({
        success: false,
        projects: [],
        errors: ['Project name must be 100 characters or less'],
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
    if (description !== undefined) {
      updates.push(`description = $${paramIndex++}`);
      values.push(description);
    }

    if (updates.length === 0) {
      return res.status(400).json({
        success: false,
        projects: [],
        errors: ['No fields to update (provide name or description)'],
      });
    }

    updates.push(`modified_at = NOW()`);
    values.push(projectId, userId);

    const project = await queryOne<LeanProject>(
      `UPDATE lean_projects SET ${updates.join(', ')}
       WHERE id = $${paramIndex++} AND user_id = $${paramIndex}
       RETURNING *`,
      values
    );

    if (!project) {
      return res.status(404).json({
        success: false,
        projects: [],
        errors: ['Project not found or access denied'],
      });
    }

    res.json({
      success: true,
      projects: [toQCProject(project)],
      errors: [],
    });
  } catch (error) {
    logError('projects/update', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      projects: [],
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /projects/nodes/read - List project nodes (stub for self-hosted)
 * Returns empty list since self-hosted doesn't support live trading nodes
 */
router.post('/nodes/read', async (req, res) => {
  // Self-hosted doesn't support live trading nodes
  res.json({
    success: true,
    nodes: [],
    errors: [],
  });
});

/**
 * POST /projects/nodes/update - Update project nodes (stub for self-hosted)
 * Returns success but does nothing since self-hosted doesn't support live trading
 */
router.post('/nodes/update', async (req, res) => {
  res.json({
    success: true,
    errors: [],
    message: 'Live trading nodes not supported in self-hosted mode',
  });
});

/**
 * POST /projects/delete - Delete a project (and all associated files/backtests)
 */
router.post('/delete', async (req, res) => {
  const context = { endpoint: 'projects/delete', userId: req.userId, body: req.body };

  try {
    const { projectId } = req.body as ProjectsDeleteRequest;
    const userId = req.userId;

    if (!projectId) {
      const response: QCBaseResponse = {
        success: false,
        errors: ['projectId is required'],
      };
      return res.status(400).json(response);
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      const response: QCBaseResponse = {
        success: false,
        errors: ['projectId must be a positive integer'],
      };
      return res.status(400).json(response);
    }

    // Verify ownership and delete (CASCADE will handle files and backtests)
    const deleted = await execute(
      'DELETE FROM lean_projects WHERE id = $1 AND user_id = $2',
      [projectId, userId]
    );

    if (deleted === 0) {
      const response: QCBaseResponse = {
        success: false,
        errors: ['Project not found or access denied'],
      };
      return res.status(404).json(response);
    }

    const response: QCBaseResponse = {
      success: true,
      errors: [],
    };

    res.json(response);
  } catch (error) {
    logError('projects/delete', error, context);
    const statusCode = getErrorStatusCode(error);
    const response: QCBaseResponse = {
      success: false,
      errors: [formatErrorForResponse(error)],
    };
    res.status(statusCode).json(response);
  }
});

export default router;
