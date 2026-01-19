/**
 * Files Routes - QC API Compatible
 * POST /api/v2/files/create - Create a new file
 * POST /api/v2/files/read - List files or read specific file
 * POST /api/v2/files/update - Update file name or content
 * POST /api/v2/files/delete - Delete a file
 */

import { Router, Request, Response } from 'express';
import { query, queryOne, execute } from '../services/database.js';
import { logError, formatErrorForResponse, getErrorStatusCode } from '../utils/errors.js';
import type {
  QCFilesResponse,
  QCBaseResponse,
  FilesReadRequest,
  FilesUpdateRequest,
} from '../types/index.js';
import type { ProjectFile } from '../types/index.js';

/**
 * Request types matching QC API exactly
 */
interface FilesCreateRequest {
  projectId: number;
  name: string;
  content?: string;
  codeSourceId?: string;
}

interface FilesDeleteRequest {
  projectId: number;
  name: string;
}

interface FilesUpdateNameRequest {
  projectId: number;
  name: string;
  newName: string;
  codeSourceId?: string;
}

interface FilesUpdateContentRequest {
  projectId: number;
  name: string;
  content: string;
  codeSourceId?: string;
}

const router = Router();

/**
 * Convert internal file to QC format
 * Matches QuantConnect API response structure exactly
 */
function toQCFile(file: ProjectFile) {
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
 * POST /files/create - Create a new file
 * QC API: POST /api/v2/files/create
 * Request: { projectId: number, name: string, content?: string, codeSourceId?: string }
 * Response: { success: boolean, errors: string[], files: ProjectFile[] }
 */
router.post('/create', async (req: Request, res: Response) => {
  const context = { endpoint: 'files/create', userId: req.userId, body: { ...req.body, content: req.body?.content?.length + ' chars' } };

  try {
    const { projectId, name, content } = req.body as FilesCreateRequest;
    const userId = req.userId;

    if (!projectId) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['projectId is required'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['projectId must be a positive integer'],
      });
    }

    if (!name) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['name is required'],
      });
    }

    if (typeof name !== 'string' || name.length > 255) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['name must be a string of 255 characters or less'],
      });
    }

    // Validate filename - alphanumeric, underscores, hyphens, dots, and forward slashes (for paths)
    if (!/^[a-zA-Z0-9_\-\.\/]+$/.test(name)) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['Invalid filename. Use only alphanumeric characters, underscores, hyphens, dots, and forward slashes.'],
      });
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        files: [],
        errors: ['Project not found or access denied'],
      });
    }

    // Check if file already exists
    const existing = await queryOne<ProjectFile>(
      'SELECT * FROM project_files WHERE project_id = $1 AND name = $2',
      [internalProjectId, name]
    );

    if (existing) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['File already exists. Use /files/update to modify existing files.'],
      });
    }

    const isMain = name.toLowerCase() === 'main.py';
    const fileContent = content || '';

    const file = await queryOne<ProjectFile>(
      `INSERT INTO project_files (project_id, name, content, is_main)
       VALUES ($1, $2, $3, $4)
       RETURNING *`,
      [internalProjectId, name, fileContent, isMain]
    );

    // Update timestamp on main projects table
    await execute(
      'UPDATE projects SET updated_at = NOW() WHERE id = $1',
      [internalProjectId]
    );

    const response: QCFilesResponse = {
      success: true,
      files: file ? [toQCFile(file)] : [],
      errors: [],
    };

    res.json(response);
  } catch (error) {
    logError('files/create', error, context);
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
 * POST /files/read - List files or get specific file
 * QC API: POST /api/v2/files/read
 * Request: { projectId: number, name?: string, codeSourceId?: string }
 * Response: { success: boolean, errors: string[], files: ProjectFile[] }
 */
router.post('/read', async (req: Request, res: Response) => {
  const context = { endpoint: 'files/read', userId: req.userId, body: req.body };

  try {
    // QC API uses 'name' not 'fileName'
    const { projectId, name } = req.body as { projectId: number; name?: string; codeSourceId?: string };
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

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      const response: QCFilesResponse = {
        success: false,
        files: [],
        errors: ['Project not found or access denied'],
      };
      return res.status(404).json(response);
    }

    let files: ProjectFile[];

    // Handle wildcard "*" to return all files (same as no name provided)
    if (!name || name === '*') {
      files = await query<ProjectFile>(
        'SELECT * FROM project_files WHERE project_id = $1 ORDER BY name',
        [internalProjectId]
      );
    } else {
      const file = await queryOne<ProjectFile>(
        'SELECT * FROM project_files WHERE project_id = $1 AND name = $2',
        [internalProjectId, name]
      );
      files = file ? [file] : [];
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
 * POST /files/update - Update file name or content
 * QC API: POST /api/v2/files/update
 *
 * Two modes:
 * 1. Rename: { projectId, name, newName } - Rename a file
 * 2. Content: { projectId, name, content } - Update file content
 *
 * Response: { success: boolean, errors: string[], files: ProjectFile[] }
 */
router.post('/update', async (req: Request, res: Response) => {
  const context = {
    endpoint: 'files/update',
    userId: req.userId,
    body: { ...req.body, content: req.body?.content ? req.body.content.length + ' chars' : undefined }
  };

  try {
    const { projectId, name, newName, content } = req.body as {
      projectId: number;
      name: string;
      newName?: string;
      content?: string;
      codeSourceId?: string;
    };
    const userId = req.userId;

    // Validate required fields
    if (!projectId) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['projectId is required'],
      });
    }

    if (typeof projectId !== 'number' || projectId < 1) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['projectId must be a positive integer'],
      });
    }

    if (!name) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['name is required'],
      });
    }

    // Must have either newName OR content (not neither, can have both)
    if (newName === undefined && content === undefined) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['Either newName or content must be provided'],
      });
    }

    if (typeof name !== 'string' || name.length > 255) {
      return res.status(400).json({
        success: false,
        files: [],
        errors: ['name must be a string of 255 characters or less'],
      });
    }

    // Validate newName if provided
    if (newName !== undefined) {
      if (typeof newName !== 'string' || newName.length > 255) {
        return res.status(400).json({
          success: false,
          files: [],
          errors: ['newName must be a string of 255 characters or less'],
        });
      }
      // Allow forward slashes for paths
      if (!/^[a-zA-Z0-9_\-\.\/]+$/.test(newName)) {
        return res.status(400).json({
          success: false,
          files: [],
          errors: ['Invalid newName. Use only alphanumeric characters, underscores, hyphens, dots, and forward slashes.'],
        });
      }
    }

    // Look up internal project id from QC project id
    const internalProjectId = await getProjectByQcId(projectId, userId);
    if (!internalProjectId) {
      return res.status(404).json({
        success: false,
        files: [],
        errors: ['Project not found or access denied'],
      });
    }

    // Check if file exists
    const existingFile = await queryOne<ProjectFile>(
      'SELECT * FROM project_files WHERE project_id = $1 AND name = $2',
      [internalProjectId, name]
    );

    if (!existingFile) {
      return res.status(404).json({
        success: false,
        files: [],
        errors: ['File not found'],
      });
    }

    let file: ProjectFile | null = null;

    // Handle rename operation
    if (newName !== undefined && newName !== name) {
      // Check if target name already exists
      const targetExists = await queryOne<ProjectFile>(
        'SELECT * FROM project_files WHERE project_id = $1 AND name = $2',
        [internalProjectId, newName]
      );

      if (targetExists) {
        return res.status(400).json({
          success: false,
          files: [],
          errors: ['A file with that name already exists'],
        });
      }

      const isMain = newName.toLowerCase() === 'main.py';

      // Update with new name and optionally new content
      file = await queryOne<ProjectFile>(
        `UPDATE project_files
         SET name = $1, is_main = $2, modified_at = NOW()${content !== undefined ? ', content = $4' : ''}
         WHERE project_id = $3 AND name = $5
         RETURNING *`,
        content !== undefined
          ? [newName, isMain, internalProjectId, content, name]
          : [newName, isMain, internalProjectId, name]
      );
    } else if (content !== undefined) {
      // Content-only update
      file = await queryOne<ProjectFile>(
        `UPDATE project_files
         SET content = $1, modified_at = NOW()
         WHERE project_id = $2 AND name = $3
         RETURNING *`,
        [content, internalProjectId, name]
      );
    }

    // Update timestamp on main projects table
    await execute(
      'UPDATE projects SET updated_at = NOW() WHERE id = $1',
      [internalProjectId]
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
 * QC API: POST /api/v2/files/delete
 * Request: { projectId: number, name: string }
 * Response: { success: boolean, errors: string[] }
 */
router.post('/delete', async (req: Request, res: Response) => {
  const context = { endpoint: 'files/delete', userId: req.userId, body: req.body };

  try {
    const { projectId, name } = req.body as FilesDeleteRequest;
    const userId = req.userId;

    if (!projectId || !name) {
      return res.status(400).json({
        success: false,
        errors: ['projectId and name are required'],
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

    if (name.toLowerCase() === 'main.py') {
      return res.status(400).json({
        success: false,
        errors: ['Cannot delete main.py'],
      });
    }

    const deleted = await execute(
      'DELETE FROM project_files WHERE project_id = $1 AND name = $2',
      [internalProjectId, name]
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
