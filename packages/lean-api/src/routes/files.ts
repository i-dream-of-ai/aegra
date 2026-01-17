/**
 * Files Routes - QC API Compatible
 * POST /api/v2/files/read
 * POST /api/v2/files/update
 */

import { Router, type IRouter } from 'express';
import { query, queryOne, execute } from '../services/database.js';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import type {
  QCFilesResponse,
  QCBaseResponse,
  FilesReadRequest,
  FilesUpdateRequest,
} from '../types/index.js';
import type { LeanFile, LeanProject } from '../types/index.js';

const router: IRouter = Router();

/**
 * Convert internal file to QC format
 * Matches QuantConnect API response structure exactly
 */
function toQCFile(file: LeanFile) {
  return {
    id: file.id,
    projectId: file.projectId,
    name: file.name,
    content: file.content,
    modified: file.modifiedAt.toISOString(),
    open: false,
    isLibrary: false,
  };
}

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
 * POST /files/read - List files or get specific file
 */
router.post('/read', async (req, res) => {
  const context = { endpoint: 'files/read', userId: req.userId, body: req.body };

  try {
    const { projectId, fileName } = req.body as FilesReadRequest;
    const userId = req.userId;

    if (!projectId) {
      const response: QCFilesResponse = {
        success: false,
        files: [],
        errors: ['projectId is required'],
      };
      return res.status(400).json(response);
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      const response: QCFilesResponse = {
        success: false,
        files: [],
        errors: ['projectId must be a positive integer'],
      };
      return res.status(400).json(response);
    }

    const hasAccess = await verifyProjectAccess(projectId, userId);
    if (!hasAccess) {
      const response: QCFilesResponse = {
        success: false,
        files: [],
        errors: ['Project not found or access denied'],
      };
      return res.status(404).json(response);
    }

    let files: LeanFile[];

    if (fileName) {
      const file = await queryOne<LeanFile>(
        'SELECT * FROM lean_files WHERE project_id = $1 AND name = $2',
        [projectId, fileName]
      );
      files = file ? [file] : [];
    } else {
      files = await query<LeanFile>(
        'SELECT * FROM lean_files WHERE project_id = $1 ORDER BY name',
        [projectId]
      );
    }

    const response: QCFilesResponse = {
      success: true,
      files: files.map(toQCFile),
      errors: [],
    };

    res.json(response);
  } catch (error) {
    logError('files/read', error, context);
    const statusCode = getErrorStatusCode(error);
    const response: QCFilesResponse = {
      success: false,
      files: [],
      errors: [formatErrorForResponse(error)],
    };
    res.status(statusCode).json(response);
  }
});

/**
 * POST /files/update - Create or update a file
 */
router.post('/update', async (req, res) => {
  const context = { endpoint: 'files/update', userId: req.userId, body: { ...req.body, content: req.body?.content?.length + ' chars' } };

  try {
    const { projectId, name, content } = req.body as FilesUpdateRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['projectId is required'],
      });
    }

    if (!name) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['name is required'],
      });
    }

    if (content === undefined || content === null) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['content is required'],
      });
    }

    if (typeof name !== 'string' || name.length > 255) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['name must be a string of 255 characters or less'],
      });
    }

    // Validate filename
    if (!/^[a-zA-Z0-9_\-\.]+$/.test(name)) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['Invalid filename. Use only alphanumeric characters, underscores, hyphens, and dots.'],
      });
    }

    const hasAccess = await verifyProjectAccess(projectId, userId);
    if (!hasAccess) {
      return res.status(404).json({
        success: false,
        files: [],
        errors: ['Project not found or access denied'],
      });
    }

    const isMain = name.toLowerCase() === 'main.py';

    const file = await queryOne<LeanFile>(
      `INSERT INTO lean_files (project_id, name, content, is_main)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (project_id, name) DO UPDATE SET
         content = EXCLUDED.content,
         modified_at = NOW()
       RETURNING *`,
      [projectId, name, content, isMain]
    );

    await execute(
      'UPDATE lean_projects SET modified_at = NOW() WHERE id = $1',
      [projectId]
    );

    const response: QCFilesResponse = {
      success: true,
      files: file ? [toQCFile(file)] : [],
      errors: [],
    };

    res.json(response);
  } catch (error) {
    logError('files/update', error, context);
    const statusCode = getErrorStatusCode(error);
    const response: QCFilesResponse = {
      success: false,
      files: [],
      errors: [formatErrorForResponse(error)],
    };
    res.status(statusCode).json(response);
  }
});

/**
 * POST /files/delete - Delete a file
 */
router.post('/delete', async (req, res) => {
  const context = { endpoint: 'files/delete', userId: req.userId, body: req.body };

  try {
    const { projectId, name } = req.body as { projectId: number; name: string };
    const userId = req.userId;

    if (!projectId || !name) {
      return res.status(400).json({
        success: false,
        errors: ['projectId and name are required'],
      });
    }

    const hasAccess = await verifyProjectAccess(projectId, userId);
    if (!hasAccess) {
      return res.status(404).json({
        success: false,
        errors: ['Project not found or access denied'],
      });
    }

    if (name.toLowerCase() === 'main.py') {
      return res.status(400).json({
        success: false,
        errors: ['Cannot delete main.py'],
      });
    }

    const deleted = await execute(
      'DELETE FROM lean_files WHERE project_id = $1 AND name = $2',
      [projectId, name]
    );

    if (deleted === 0) {
      return res.status(404).json({
        success: false,
        errors: ['File not found'],
      });
    }

    res.json({
      success: true,
      errors: [],
    });
  } catch (error) {
    logError('files/delete', error, context);
    const statusCode = getErrorStatusCode(error);
    res.status(statusCode).json({
      success: false,
      errors: [formatErrorForResponse(error)],
    });
  }
});

export default router;
