/**
 * LEAN Engine Routes
 * POST /api/v2/lean/versions - Get LEAN version info
 */

import { Router, type IRouter } from 'express';

const router: IRouter = Router();

// Current LEAN version (pulled from Docker image)
const LEAN_VERSION = process.env.LEAN_VERSION || '15.0.0';
const LEAN_DOCKER_IMAGE = process.env.LEAN_DOCKER_IMAGE || 'quantconnect/lean:latest';

/**
 * POST /lean/versions - Get available LEAN versions
 * Self-hosted mode returns only the currently installed version
 */
router.post('/versions', async (req, res) => {
  res.json({
    success: true,
    versions: [
      {
        id: 1,
        version: LEAN_VERSION,
        description: `LEAN Engine ${LEAN_VERSION} (self-hosted)`,
        dockerImage: LEAN_DOCKER_IMAGE,
        latest: true,
        releaseDate: new Date().toISOString(),
      },
    ],
    errors: [],
  });
});

/**
 * POST /lean/version/read - Get LEAN version for a project
 */
router.post('/version/read', async (req, res) => {
  res.json({
    success: true,
    version: {
      id: 1,
      version: LEAN_VERSION,
      description: `LEAN Engine ${LEAN_VERSION} (self-hosted)`,
      latest: true,
    },
    errors: [],
  });
});

export default router;
