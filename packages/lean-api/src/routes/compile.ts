/**
 * Compile Routes - QC API Compatible
 * POST /api/v2/compile/create
 *
 * Uses Python AST parser to validate algorithms properly.
 * This mirrors QC Cloud's behavior - real Python parsing, not string matching.
 */

import { Router, type IRouter } from 'express';
import { query, queryOne } from '../services/database.js';
import { v4 as uuidv4 } from 'uuid';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import { exec } from 'child_process';
import { promisify } from 'util';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as os from 'os';
import type {
  QCCompileResponse,
  CompileCreateRequest,
  ProjectFile,
} from '../types/index.js';

const execAsync = promisify(exec);

const router: IRouter = Router();

/**
 * Validation result from Python script
 */
interface ValidationResult {
  success: boolean;
  errors: string[];
  files_checked: string[];
}

/**
 * Validate project files using Python AST parser
 * Creates a temp directory with project files and runs validation script
 */
async function validateProjectWithPython(files: ProjectFile[]): Promise<ValidationResult> {
  // Create temp directory for project files
  const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'lean-compile-'));

  try {
    // Write all project files to temp directory
    for (const file of files) {
      const filePath = path.join(tempDir, file.name);
      await fs.writeFile(filePath, file.content, 'utf-8');
    }

    // Path to validation script
    // In dev (tsx): __dirname is src/routes, script is at src/scripts/
    // In prod (node dist/): __dirname is dist/routes, script is at src/scripts/ (not copied by tsc)
    // So we use process.cwd() which is /app in Docker, script is at /app/src/scripts/
    const scriptPath = path.join(process.cwd(), 'src', 'scripts', 'validate_algorithm.py');

    // Run Python validation
    const { stdout, stderr } = await execAsync(`python3 "${scriptPath}" "${tempDir}"`, {
      timeout: 30000, // 30 second timeout
    });

    // Parse JSON output
    try {
      return JSON.parse(stdout.trim()) as ValidationResult;
    } catch {
      // If JSON parsing fails, treat as error
      return {
        success: false,
        errors: [`Validation script error: ${stderr || stdout || 'Unknown error'}`],
        files_checked: [],
      };
    }
  } catch (error: unknown) {
    // Handle exec errors (timeout, script not found, etc.)
    const errMsg = error instanceof Error ? error.message : String(error);

    // Try to parse stdout if available (script might have returned JSON error)
    if (error && typeof error === 'object' && 'stdout' in error) {
      const stdout = (error as { stdout?: string }).stdout;
      if (stdout) {
        try {
          return JSON.parse(stdout.trim()) as ValidationResult;
        } catch {
          // Fall through to generic error
        }
      }
    }

    return {
      success: false,
      errors: [`Validation failed: ${errMsg}`],
      files_checked: [],
    };
  } finally {
    // Clean up temp directory
    try {
      await fs.rm(tempDir, { recursive: true, force: true });
    } catch {
      // Ignore cleanup errors
    }
  }
}

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

    // Run Python AST validation on all project files
    const validation = await validateProjectWithPython(files);
    const compileId = uuidv4();

    if (!validation.success) {
      return res.json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: validation.errors,
        errors: validation.errors,
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
      logs: [`Validated ${validation.files_checked.length} file(s)`, 'Ready to run backtest'],
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

    // Re-validate with Python AST parser
    const validation = await validateProjectWithPython(files);

    if (!validation.success) {
      return res.json({
        success: false,
        compileId,
        projectId,
        state: 'BuildError',
        parameters: [],
        signature: '',
        signatureOrder: [],
        logs: validation.errors,
        errors: validation.errors,
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
      logs: [`Validated ${validation.files_checked.length} file(s)`, 'Ready to run backtest'],
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
