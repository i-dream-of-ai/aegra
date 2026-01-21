/**
 * Usage Tracking Service
 * Tracks compute resources for billing and quota enforcement
 */

import { query, queryOne, execute } from './database.js';
import { exec } from 'child_process';
import { promisify } from 'util';

const execAsync = promisify(exec);

export interface UsageRecord {
  id: string;
  userId: string;
  backtestId?: string;
  optimizationId?: string;
  computeSeconds?: number;
  memoryPeakMb?: number;
  memoryMbSeconds?: number;
  dataPointsFetched: number;
  apiCallsCount: number;
  containerId?: string;
  workerNode?: string;
  cpuCoresUsed: number;
  memoryLimitMb?: number;
  queuedAt: Date;
  startedAt?: Date;
  completedAt?: Date;
  status: 'queued' | 'running' | 'completed' | 'error' | 'aborted';
  errorMessage?: string;
}

export interface QuotaCheckResult {
  allowed: boolean;
  reason: string;
  quotaRemainingSeconds: number;
  currentRunningJobs: number;
}

/**
 * Create a usage tracking record when a job is queued
 */
export async function createUsageRecord(params: {
  userId: string;
  backtestId?: string;
  optimizationId?: string;
  cpuCoresUsed?: number;
  memoryLimitMb?: number;
  workerNode?: string;
}): Promise<string> {
  const result = await queryOne<{ id: string }>(
    `INSERT INTO usage_tracking (
      user_id, backtest_id, optimization_id,
      cpu_cores_used, memory_limit_mb, worker_node,
      status, queued_at
    ) VALUES ($1, $2, $3, $4, $5, $6, 'queued', NOW())
    RETURNING id`,
    [
      params.userId,
      params.backtestId || null,
      params.optimizationId || null,
      params.cpuCoresUsed || 1,
      params.memoryLimitMb || null,
      params.workerNode || null,
    ]
  );

  if (!result) {
    throw new Error('Failed to create usage tracking record');
  }

  return result.id;
}

/**
 * Mark a usage record as running and record the container ID
 */
export async function markUsageRunning(
  usageId: string,
  containerId?: string
): Promise<void> {
  await execute(
    `UPDATE usage_tracking SET
      status = 'running',
      started_at = NOW(),
      container_id = $2
    WHERE id = $1`,
    [usageId, containerId || null]
  );
}

/**
 * Complete a usage record with resource metrics
 */
export async function completeUsageRecord(params: {
  usageId: string;
  computeSeconds: number;
  memoryPeakMb?: number;
  memoryMbSeconds?: number;
  dataPointsFetched?: number;
  apiCallsCount?: number;
  status?: 'completed' | 'error' | 'aborted';
  errorMessage?: string;
}): Promise<void> {
  await execute(
    `UPDATE usage_tracking SET
      status = $2,
      completed_at = NOW(),
      compute_seconds = $3,
      memory_peak_mb = $4,
      memory_mb_seconds = $5,
      data_points_fetched = COALESCE($6, data_points_fetched),
      api_calls_count = COALESCE($7, api_calls_count),
      error_message = $8
    WHERE id = $1`,
    [
      params.usageId,
      params.status || 'completed',
      params.computeSeconds,
      params.memoryPeakMb || null,
      params.memoryMbSeconds || null,
      params.dataPointsFetched || null,
      params.apiCallsCount || null,
      params.errorMessage || null,
    ]
  );
}

/**
 * Increment data points fetched counter
 */
export async function incrementDataPoints(
  usageId: string,
  count: number
): Promise<void> {
  await execute(
    `UPDATE usage_tracking SET
      data_points_fetched = COALESCE(data_points_fetched, 0) + $2
    WHERE id = $1`,
    [usageId, count]
  );
}

/**
 * Increment API calls counter
 */
export async function incrementApiCalls(
  usageId: string,
  count: number = 1
): Promise<void> {
  await execute(
    `UPDATE usage_tracking SET
      api_calls_count = COALESCE(api_calls_count, 0) + $2
    WHERE id = $1`,
    [usageId, count]
  );
}

/**
 * Check if user can run a new job (quota and concurrency check)
 * Uses the database function for consistency
 */
export async function canUserRunJob(
  userId: string,
  jobType: 'backtest' | 'optimization' = 'backtest'
): Promise<QuotaCheckResult> {
  const result = await queryOne<{
    allowed: boolean;
    reason: string;
    quotaRemainingSeconds: number;
    currentRunningJobs: number;
  }>(
    `SELECT * FROM can_user_run_job($1, $2)`,
    [userId, jobType]
  );

  if (!result) {
    // If function doesn't exist yet (migration not run), allow by default
    return {
      allowed: true,
      reason: 'OK (quota check skipped)',
      quotaRemainingSeconds: 999999,
      currentRunningJobs: 0,
    };
  }

  return result;
}

/**
 * Get user's monthly usage summary
 */
export async function getUserMonthlyUsage(userId: string): Promise<{
  computeSecondsUsed: number;
  dataPointsUsed: number;
  backtestsRun: number;
  optimizationsRun: number;
}> {
  const result = await queryOne<{
    computeSecondsUsed: number;
    dataPointsUsed: number;
    backtestsRun: number;
    optimizationsRun: number;
  }>(
    `SELECT * FROM get_user_monthly_usage($1)`,
    [userId]
  );

  return result || {
    computeSecondsUsed: 0,
    dataPointsUsed: 0,
    backtestsRun: 0,
    optimizationsRun: 0,
  };
}

/**
 * Get docker container stats (memory, CPU)
 * Returns null if container not found or stats unavailable
 */
export async function getDockerStats(containerId: string): Promise<{
  memoryUsageMb: number;
  memoryLimitMb: number;
  cpuPercent: number;
} | null> {
  try {
    const { stdout } = await execAsync(
      `docker stats ${containerId} --no-stream --format "{{.MemUsage}}|{{.CPUPerc}}"`,
      { timeout: 5000 }
    );

    // Format: "123.4MiB / 4GiB|25.50%"
    const parts = stdout.trim().split('|');
    if (parts.length !== 2) return null;

    const memParts = parts[0].split(' / ');
    if (memParts.length !== 2) return null;

    const parseMemory = (str: string): number => {
      const match = str.match(/^([\d.]+)(\w+)$/);
      if (!match) return 0;
      const value = parseFloat(match[1]);
      const unit = match[2].toLowerCase();
      switch (unit) {
        case 'kib':
        case 'kb':
          return value / 1024;
        case 'mib':
        case 'mb':
          return value;
        case 'gib':
        case 'gb':
          return value * 1024;
        default:
          return value;
      }
    };

    const cpuPercent = parseFloat(parts[1].replace('%', '')) || 0;

    return {
      memoryUsageMb: parseMemory(memParts[0]),
      memoryLimitMb: parseMemory(memParts[1]),
      cpuPercent,
    };
  } catch {
    return null;
  }
}

/**
 * Monitor container stats periodically and track peak memory
 * Returns a cleanup function to stop monitoring
 */
export function startContainerMonitor(
  containerId: string,
  onStats: (stats: { memoryUsageMb: number; cpuPercent: number }) => void,
  intervalMs: number = 5000
): () => void {
  let running = true;
  let peakMemoryMb = 0;

  const poll = async () => {
    while (running) {
      const stats = await getDockerStats(containerId);
      if (stats) {
        if (stats.memoryUsageMb > peakMemoryMb) {
          peakMemoryMb = stats.memoryUsageMb;
        }
        onStats({
          memoryUsageMb: stats.memoryUsageMb,
          cpuPercent: stats.cpuPercent,
        });
      }
      await new Promise(resolve => setTimeout(resolve, intervalMs));
    }
  };

  poll().catch(() => {}); // Fire and forget

  return () => {
    running = false;
  };
}

/**
 * Link a backtest to its usage tracking record
 */
export async function linkBacktestToUsage(
  backtestId: string,
  usageTrackingId: string
): Promise<void> {
  await execute(
    `UPDATE qc_backtests SET usage_tracking_id = $2 WHERE id = $1 OR qc_backtest_id = $1`,
    [backtestId, usageTrackingId]
  );
}

/**
 * Link an optimization to its usage tracking record
 */
export async function linkOptimizationToUsage(
  optimizationId: string,
  usageTrackingId: string
): Promise<void> {
  await execute(
    `UPDATE optimizations SET usage_tracking_id = $2 WHERE id = $1`,
    [optimizationId, usageTrackingId]
  );
}

/**
 * Clean up orphaned "running" records on service startup.
 * These are records from jobs that were running when the service crashed/restarted.
 * Also kills any orphaned Docker containers.
 */
export async function cleanupOrphanedJobs(): Promise<number> {
  // First, get all "running" records to check their containers
  const runningJobs = await query<{
    id: string;
    container_id: string | null;
    started_at: Date;
    backtest_id: string | null;
    optimization_id: string | null;
  }>(
    `SELECT id, container_id, started_at, backtest_id, optimization_id
     FROM usage_tracking
     WHERE status = 'running'`
  );

  let cleanedCount = 0;

  for (const job of runningJobs) {
    let containerRunning = false;

    // Check if the Docker container is actually running
    if (job.container_id) {
      try {
        const { stdout } = await execAsync(
          `docker inspect -f '{{.State.Running}}' ${job.container_id} 2>/dev/null || echo "false"`,
          { timeout: 5000 }
        );
        containerRunning = stdout.trim() === 'true';

        // If container exists but job is too old (>30 min), kill it
        if (containerRunning && job.started_at) {
          const ageMinutes = (Date.now() - new Date(job.started_at).getTime()) / 60000;
          if (ageMinutes > 30) {
            console.log(`[Usage Cleanup] Killing stale container ${job.container_id} (${ageMinutes.toFixed(0)} min old)`);
            await execAsync(`docker kill ${job.container_id}`, { timeout: 10000 }).catch(() => {});
            containerRunning = false;
          }
        }
      } catch {
        // Container doesn't exist or can't be inspected
        containerRunning = false;
      }
    }

    // If container is not running, mark the job as aborted
    if (!containerRunning) {
      const ageMinutes = job.started_at
        ? (Date.now() - new Date(job.started_at).getTime()) / 60000
        : 0;

      console.log(
        `[Usage Cleanup] Aborting orphaned job ${job.id} ` +
        `(backtest: ${job.backtest_id || 'N/A'}, optimization: ${job.optimization_id || 'N/A'}, ` +
        `age: ${ageMinutes.toFixed(0)} min, container: ${job.container_id || 'none'})`
      );

      await execute(
        `UPDATE usage_tracking SET
          status = 'aborted',
          completed_at = NOW(),
          error_message = 'Orphaned job cleaned up on service restart'
        WHERE id = $1`,
        [job.id]
      );

      cleanedCount++;
    }
  }

  if (cleanedCount > 0) {
    console.log(`[Usage Cleanup] Cleaned up ${cleanedCount} orphaned jobs`);
  }

  return cleanedCount;
}

/**
 * Abort stale jobs that have been running too long.
 * Call this periodically to catch jobs that got stuck.
 * @param maxAgeMinutes - Max age in minutes before a job is considered stale (default 30)
 */
export async function abortStaleJobs(maxAgeMinutes: number = 30): Promise<number> {
  const result = await execute(
    `UPDATE usage_tracking SET
      status = 'aborted',
      completed_at = NOW(),
      error_message = $2
    WHERE status = 'running'
      AND started_at < NOW() - ($1 || ' minutes')::interval
    RETURNING id`,
    [maxAgeMinutes, `Job exceeded ${maxAgeMinutes} minute timeout`]
  );

  // execute doesn't return rows, so we need to count separately
  const countResult = await queryOne<{ count: number }>(
    `SELECT COUNT(*)::int as count FROM usage_tracking
     WHERE status = 'aborted'
       AND error_message LIKE 'Job exceeded % minute timeout'
       AND completed_at > NOW() - INTERVAL '1 minute'`
  );

  const abortedCount = countResult?.count || 0;
  if (abortedCount > 0) {
    console.log(`[Usage Cleanup] Aborted ${abortedCount} stale jobs (>${maxAgeMinutes} min)`);
  }

  return abortedCount;
}

/**
 * Start periodic cleanup of stale jobs.
 * Returns a cleanup function to stop the interval.
 */
export function startPeriodicCleanup(intervalMinutes: number = 5): () => void {
  const intervalMs = intervalMinutes * 60 * 1000;

  const cleanup = async () => {
    try {
      await abortStaleJobs(30); // 30 min timeout
    } catch (error) {
      console.error('[Usage Cleanup] Periodic cleanup error:', error);
    }
  };

  const intervalId = setInterval(cleanup, intervalMs);

  // Run once immediately
  cleanup();

  return () => clearInterval(intervalId);
}
