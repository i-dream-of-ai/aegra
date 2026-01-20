/**
 * Compile Routes - QC API Compatible
 * POST /api/v2/compile/create
 */

import { Router, type IRouter } from 'express';
import { query, queryOne } from '../services/database.js';
import { v4 as uuidv4 } from 'uuid';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import type {
  QCCompileResponse,
  CompileCreateRequest,
  ProjectFile,
} from '../types/index.js';

const router: IRouter = Router();

/**
 * Look up project by QC project ID and verify ownership
 * Uses the main 'projects' table which has qc_project_id
 * Returns internal project id if found, null otherwise
 */
async function getProjectByQcId(qcProjectId: number, userId: string): Promise<number | null> {
  // Look up by qc_project_id in projects table
  const project = await queryOne<{ id: number }>(
    'SELECT id FROM projects WHERE qc_project_id = $1 AND user_id = $2',
    [String(qcProjectId), userId]
  );
  if (project) {
    return project.id;
  }

  // Fallback: maybe it's the internal project id directly
  const directProject = await queryOne<{ id: number }>(
    'SELECT id FROM projects WHERE id = $1 AND user_id = $2',
    [qcProjectId, userId]
  );
  return directProject?.id || null;
}

/**
 * Basic Python syntax validation
 *
 * IMPORTANT: This is intentionally minimal to avoid false positives.
 * Python syntax is complex (multi-line statements, f-strings, etc.)
 * and naive checks cause valid QC code to fail.
 *
 * We only check for things we can reliably detect:
 * - Required LEAN imports
 * - QCAlgorithm inheritance
 * - Initialize method presence
 */
function validatePythonSyntax(code: string): { valid: boolean; errors: string[] } {
  const errors: string[] = [];

  // Check for required LEAN imports
  if (!code.includes('from AlgorithmImports import') && !code.includes('import AlgorithmImports')) {
    errors.push('Missing required import: from AlgorithmImports import *');
  }

  // Check for QCAlgorithm class
  if (!code.includes('QCAlgorithm')) {
    errors.push('Algorithm must inherit from QCAlgorithm');
  }

  // Check for initialize method (case-insensitive check)
  if (!code.includes('def initialize(self)') && !code.includes('def Initialize(self)')) {
    errors.push('Algorithm must have an initialize(self) method');
  }

  return {
    valid: errors.length === 0,
    errors,
  };
}

/**
 * POST /compile/create - Compile/validate a project
 */
router.post('/create', async (req, res) => {
  const context = { endpoint: 'compile/create', userId: req.userId, body: req.body };

  try {
    const { projectId } = req.body as CompileCreateRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        compileId: '',
        projectId: 0,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['projectId is required'],
        errors: ['projectId is required'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        compileId: '',
        projectId: projectId || 0,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['projectId must be a positive integer'],
        errors: ['projectId must be a positive integer'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        compileId: '',
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['Project not found or access denied'],
        errors: ['Project not found or access denied'],
      });
    }

    const files = await query<ProjectFile>(
      'SELECT * FROM project_files WHERE project_id = $1',
      [internalProjectId]
    );

    if (files.length === 0) {
      return res.status(400).json({
        success: false,
        compileId: '',
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['No files found in project'],
        errors: ['No files found in project'],
      });
    }

    const mainFile = files.find(f => f.isMain || f.name.toLowerCase() === 'main.py');
    if (!mainFile) {
      return res.status(400).json({
        success: false,
        compileId: '',
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['main.py not found'],
        errors: ['main.py not found'],
      });
    }

    const allLogs: string[] = [];
    let hasErrors = false;

    for (const file of files) {
      if (file.name.endsWith('.py')) {
        const validation = validatePythonSyntax(file.content);
        if (!validation.valid) {
          hasErrors = true;
          allLogs.push(`Errors in ${file.name}:`);
          allLogs.push(...validation.errors.map(e => `  ${e}`));
        }
      }
    }

    const compileId = uuidv4();

    if (hasErrors) {
      return res.json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: allLogs,
        errors: allLogs,
      });
    }

    res.json({
      success: true,
      compileId,
      projectId,
      state: 'BuildSuccess',
      parameters: [],
      signature: compileId,
      signatureOrder: files.map(f => f.name),
      logs: ['Syntax validation passed', 'Ready to run backtest'],
      errors: [],
    });
  } catch (error) {
    logError('compile/create', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      compileId: '',
      projectId: req.body?.projectId || 0,
      state: 'BuildError',
      parameters: [],
      signature: '',
      signatureOrder: [],
      logs: [formatErrorForResponse(error)],
      errors: [formatErrorForResponse(error)],
    });
  }
});

/**
 * POST /compile/read - Read compile status
 *
 * QC API Compatible: Returns compilation state for a given compile ID.
 * For self-hosted LEAN, compilation is synchronous so this always returns
 * the same state as /compile/create.
 */
router.post('/read', async (req, res) => {
  const context = { endpoint: 'compile/read', userId: req.userId, body: req.body };

  try {
    const { projectId, compileId } = req.body as { projectId: number; compileId: string };
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        compileId: compileId || '',
        projectId: 0,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['projectId is required'],
        errors: ['projectId is required'],
      });
    }

    if (!compileId) {
      return res.status(400).json({
        success: false,
        compileId: '',
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['compileId is required'],
        errors: ['compileId is required'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['Project not found or access denied'],
        errors: ['Project not found or access denied'],
      });
    }

    // For self-hosted LEAN, compilation is synchronous (done in /compile/create)
    // So /compile/read just re-validates the current files state

    const files = await query<ProjectFile>(
      'SELECT * FROM project_files WHERE project_id = $1',
      [internalProjectId]
    );

    if (files.length === 0) {
      return res.status(400).json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['No files found in project'],
        errors: ['No files found in project'],
      });
    }

    const mainFile = files.find(f => f.isMain || f.name.toLowerCase() === 'main.py');
    if (!mainFile) {
      return res.status(400).json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: ['main.py not found'],
        errors: ['main.py not found'],
      });
    }

    // Re-validate the files
    const allLogs: string[] = [];
    let hasErrors = false;

    for (const file of files) {
      if (file.name.endsWith('.py')) {
        const validation = validatePythonSyntax(file.content);
        if (!validation.valid) {
          hasErrors = true;
          allLogs.push(`Errors in ${file.name}:`);
          allLogs.push(...validation.errors.map(e => `  ${e}`));
        }
      }
    }

    if (hasErrors) {
      return res.json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: allLogs,
        errors: allLogs,
      });
    }

    // Return success - compilation is complete for self-hosted LEAN
    res.json({
      success: true,
      compileId,
      projectId,
      state: 'BuildSuccess',
      parameters: [],
      signature: compileId,
      signatureOrder: files.map(f => f.name),
      logs: ['Syntax validation passed', 'Ready to run backtest'],
      errors: [],
    });
  } catch (error) {
    logError('compile/read', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      compileId: req.body?.compileId || '',
      projectId: req.body?.projectId || 0,
      state: 'BuildError',
      parameters: [],
      signature: '',
      signatureOrder: [],
      logs: [formatErrorForResponse(error)],
      errors: [formatErrorForResponse(error)],
    });
  }
});

export default router;
