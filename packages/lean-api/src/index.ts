/**
 * LEAN API Server
 * QC-compatible REST API for self-hosted backtesting
 */

import dotenv from 'dotenv';
import path from 'path';

// Load env from root .env.local (relative to working dir)
dotenv.config({ path: path.resolve(process.cwd(), '../../.env.local') });

// Fallback: try from monorepo root if running from root
if (!process.env.DATABASE_URL) {
  dotenv.config({ path: path.resolve(process.cwd(), '.env.local') });
}
import express from 'express';
import cors from 'cors';
import { authMiddleware } from './middleware/auth.js';
import { checkHealth, closePool } from './services/database.js';
import { closeQueues } from './workers/queue.js';
import { startBacktestWorker } from './workers/backtest-worker.js';
import { startOptimizationWorker } from './workers/optimization-worker.js';

// Routes
import projectsRouter from './routes/projects.js';
import filesRouter from './routes/files.js';
import compileRouter from './routes/compile.js';
import backtestsRouter from './routes/backtests.js';
import optimizationsRouter from './routes/optimizations.js';
import leanRouter from './routes/lean.js';

const app = express();
const PORT = parseInt(process.env.PORT || '3001', 10);

// Middleware
app.use(cors({
  origin: process.env.CORS_ORIGIN || '*',
  credentials: true,
}));
app.use(express.json({ limit: '10mb' }));

// Health check (no auth required)
app.get('/health', async (req, res) => {
  const { healthy, error } = await checkHealth();
  res.json({
    status: healthy ? 'healthy' : 'unhealthy',
    timestamp: new Date().toISOString(),
    version: '0.1.0',
    ...(error && { error }),
  });
});

// API version info (no auth required)
app.get('/api/v2', (req, res) => {
  res.json({
    success: true,
    name: 'LEAN API (Self-Hosted)',
    version: '0.1.0',
    compatible: 'QuantConnect API v2',
    errors: [],
  });
});

// Apply auth middleware to all /api/v2/* routes
app.use('/api/v2', authMiddleware);

// Mount routes - QC API compatible paths
app.use('/api/v2/projects', projectsRouter);
app.use('/api/v2/files', filesRouter);
app.use('/api/v2/compile', compileRouter);
app.use('/api/v2/backtests', backtestsRouter);
app.use('/api/v2/optimizations', optimizationsRouter);
app.use('/api/v2/lean', leanRouter);

// 404 handler for unknown routes
app.use((req, res) => {
  res.status(404).json({
    success: false,
    errors: [`Unknown endpoint: ${req.method} ${req.path}`],
  });
});

// Global error handler - returns REAL error details for debugging
app.use((err: Error, req: express.Request, res: express.Response, next: express.NextFunction) => {
  const timestamp = new Date().toISOString();
  const errorObj = err as unknown as Record<string, unknown>;

  // Log full error details to console
  console.error(`\n[${timestamp}] UNHANDLED ERROR:`);
  console.error(`  Path: ${req.method} ${req.path}`);
  console.error(`  Message: ${err.message}`);
  console.error(`  Name: ${err.name}`);
  if (errorObj.code) console.error(`  Code: ${errorObj.code}`);
  if (errorObj.detail) console.error(`  Detail: ${errorObj.detail}`);
  if (errorObj.hint) console.error(`  Hint: ${errorObj.hint}`);
  if (errorObj.constraint) console.error(`  Constraint: ${errorObj.constraint}`);
  if (errorObj.table) console.error(`  Table: ${errorObj.table}`);
  if (err.stack) console.error(`  Stack: ${err.stack}`);

  // Build detailed error response - NO SANITIZATION
  const errorParts: string[] = [err.message];
  if (errorObj.code) errorParts.push(`code: ${errorObj.code}`);
  if (errorObj.detail) errorParts.push(`detail: ${errorObj.detail}`);
  if (errorObj.hint) errorParts.push(`hint: ${errorObj.hint}`);
  if (errorObj.constraint) errorParts.push(`constraint: ${errorObj.constraint}`);
  if (errorObj.table) errorParts.push(`table: ${errorObj.table}`);
  if (errorObj.column) errorParts.push(`column: ${errorObj.column}`);

  res.status(500).json({
    success: false,
    errors: [errorParts.join(' | ')],
  });
});

// Start server
let backtestWorker: ReturnType<typeof startBacktestWorker> | null = null;
let optimizationWorker: ReturnType<typeof startOptimizationWorker> | null = null;

async function start() {
  // Log env status on startup
  console.log('[Startup] DATABASE_URL:', process.env.DATABASE_URL ? `${process.env.DATABASE_URL.substring(0, 50)}...` : 'NOT SET');
  console.log('[Startup] REDIS_HOST:', process.env.REDIS_HOST || 'localhost (default)');
  console.log('[Startup] ALPHA_VANTAGE_API_KEY:', process.env.ALPHA_VANTAGE_API_KEY ? 'SET' : 'NOT SET');

  try {
    // Check database connection
    const { healthy, error } = await checkHealth();
    if (!healthy) {
      console.error('[Startup] Database connection failed:', error);
      process.exit(1);
    }
    console.log('✓ Database connected');

    // Start background workers
    if (process.env.ENABLE_WORKERS !== 'false') {
      backtestWorker = startBacktestWorker();
      console.log('✓ Backtest worker started');
      optimizationWorker = startOptimizationWorker();
      console.log('✓ Optimization worker started');
    }

    // Start HTTP server
    app.listen(PORT, () => {
      console.log(`✓ LEAN API server running on port ${PORT}`);
      console.log(`  Health: http://localhost:${PORT}/health`);
      console.log(`  API: http://localhost:${PORT}/api/v2`);
    });
  } catch (error) {
    console.error('Failed to start server:', error);
    process.exit(1);
  }
}

// Graceful shutdown
async function shutdown(signal: string) {
  console.log(`\n${signal} received, shutting down gracefully...`);

  try {
    if (backtestWorker) {
      await backtestWorker.close();
      console.log('✓ Backtest worker stopped');
    }
    if (optimizationWorker) {
      await optimizationWorker.close();
      console.log('✓ Optimization worker stopped');
    }

    await closeQueues();
    console.log('✓ Job queues closed');

    await closePool();
    console.log('✓ Database pool closed');

    process.exit(0);
  } catch (error) {
    console.error('Error during shutdown:', error);
    process.exit(1);
  }
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

start();
