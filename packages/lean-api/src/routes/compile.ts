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
} from '../types/index.js';
import type { LeanFile, LeanProject } from '../types/index.js';

const router: IRouter = Router();

/**
 * Verify project ownership
 */
async function verifyProjectAccess(projectId: number, userId: string): Promise<boolean> {
  const project = await queryOne<LeanProject>(
    'SELECT id FROM lean_projects WHERE id = $1 AND user_id = $2',
    [projectId, userId]
  );
  return !!project;
}

/**
 * Basic Python syntax validation
 */
function validatePythonSyntax(code: string): { valid: boolean; errors: string[] } {
  const errors: string[] = [];
  const lines = code.split('\n');

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineNum = i + 1;
    const trimmed = line.trim();

    if (trimmed === '' || trimmed.startsWith('#')) continue;

    // Check for tabs mixed with spaces
    if (line.includes('\t') && line.includes('    ')) {
      errors.push(`Line ${lineNum}: Mixed tabs and spaces in indentation`);
    }

    // Check for unclosed strings
    const stringMatches = trimmed.match(/["']/g);
    if (stringMatches && stringMatches.length % 2 !== 0) {
      if (!trimmed.includes('"""') && !trimmed.includes("'''")) {
        errors.push(`Line ${lineNum}: Unclosed string literal`);
      }
    }

    // Check for colon at end of control structures
    const controlKeywords = ['if', 'elif', 'else', 'for', 'while', 'try', 'except', 'finally', 'with', 'def', 'class'];
    for (const keyword of controlKeywords) {
      if (trimmed.startsWith(keyword + ' ') || trimmed === keyword) {
        if (!trimmed.endsWith(':') && !trimmed.includes(':')) {
          errors.push(`Line ${lineNum}: Missing colon after '${keyword}' statement`);
        }
      }
    }

    // Check for invalid variable names
    const assignmentMatch = trimmed.match(/^([a-zA-Z_][a-zA-Z0-9_]*)\s*=/);
    if (assignmentMatch) {
      const varName = assignmentMatch[1];
      const pythonKeywords = ['and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue', 'def', 'del', 'elif', 'else', 'except', 'finally', 'for', 'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'try', 'while', 'with', 'yield', 'True', 'False', 'None'];
      if (pythonKeywords.includes(varName)) {
        errors.push(`Line ${lineNum}: Cannot use Python keyword '${varName}' as variable name`);
      }
    }
  }

  // Check for required LEAN imports
  if (!code.includes('from AlgorithmImports import') && !code.includes('import AlgorithmImports')) {
    errors.push('Missing required import: from AlgorithmImports import *');
  }

  // Check for QCAlgorithm class
  if (!code.includes('QCAlgorithm')) {
    errors.push('Algorithm must inherit from QCAlgorithm');
  }

  // Check for initialize method
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

    const hasAccess = await verifyProjectAccess(projectId, userId);
    if (!hasAccess) {
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

    const files = await query<LeanFile>(
      'SELECT * FROM lean_files WHERE project_id = $1',
      [projectId]
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

export default router;
