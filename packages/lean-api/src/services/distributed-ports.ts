/**
 * Distributed Port Allocation Service
 * Uses Redis for coordinating port allocation across multiple worker nodes
 *
 * This enables horizontal scaling by ensuring workers on different nodes
 * don't allocate the same ports for ZeroMQ streaming.
 */

import { Redis } from 'ioredis';

// Redis key for the port allocation set
const PORT_LOCK_PREFIX = 'lean:port_lock:';
const PORT_TTL_SECONDS = 600; // 10 minutes - ports auto-release if worker dies

let redis: Redis | null = null;

function getRedis(): Redis {
  if (!redis) {
    redis = new Redis({
      host: process.env.REDIS_HOST || 'localhost',
      port: parseInt(process.env.REDIS_PORT || '6379', 10),
      maxRetriesPerRequest: 3,
      lazyConnect: true,
    });

    redis.on('error', (err: Error) => {
      console.error('[Distributed Ports] Redis error:', err);
    });
  }
  return redis;
}

/**
 * Allocate a port from the configured range
 * Uses Redis SETNX for atomic allocation across nodes
 *
 * @param portBase - Start of port range (default from env)
 * @param portMax - End of port range (default from env)
 * @param workerId - Identifier for this worker (for debugging)
 * @returns Allocated port number, or null if no ports available
 */
export async function allocatePort(
  portBase: number = parseInt(process.env.LEAN_STREAMING_PORT_BASE || '5680', 10),
  portMax: number = parseInt(process.env.LEAN_STREAMING_PORT_MAX || '5780', 10),
  workerId: string = process.env.WORKER_NODE_ID || 'unknown'
): Promise<number | null> {
  const r = getRedis();

  // Try each port in range
  for (let port = portBase; port <= portMax; port++) {
    const lockKey = `${PORT_LOCK_PREFIX}${port}`;

    // Try to atomically acquire this port
    // SET NX (only if not exists) with TTL for auto-cleanup
    const acquired = await r.set(lockKey, workerId, 'EX', PORT_TTL_SECONDS, 'NX');

    if (acquired === 'OK') {
      console.log(`[Distributed Ports] Allocated port ${port} for worker ${workerId}`);
      return port;
    }
  }

  console.warn(`[Distributed Ports] No free ports in range ${portBase}-${portMax}`);
  return null;
}

/**
 * Release a previously allocated port
 * Only releases if the port was allocated by this worker (prevents stealing)
 *
 * @param port - Port number to release
 * @param workerId - Identifier for this worker
 */
export async function releasePort(
  port: number,
  workerId: string = process.env.WORKER_NODE_ID || 'unknown'
): Promise<void> {
  const r = getRedis();
  const lockKey = `${PORT_LOCK_PREFIX}${port}`;

  // Only delete if we own this port (Lua script for atomicity)
  const script = `
    if redis.call("GET", KEYS[1]) == ARGV[1] then
      return redis.call("DEL", KEYS[1])
    else
      return 0
    end
  `;

  const result = await r.eval(script, 1, lockKey, workerId);
  if (result === 1) {
    console.log(`[Distributed Ports] Released port ${port} for worker ${workerId}`);
  } else {
    console.warn(`[Distributed Ports] Could not release port ${port} (not owned by ${workerId})`);
  }
}

/**
 * Extend the TTL on an allocated port (heartbeat)
 * Call periodically during long-running backtests to prevent expiration
 *
 * @param port - Port number to refresh
 * @param workerId - Identifier for this worker
 */
export async function refreshPort(
  port: number,
  workerId: string = process.env.WORKER_NODE_ID || 'unknown'
): Promise<boolean> {
  const r = getRedis();
  const lockKey = `${PORT_LOCK_PREFIX}${port}`;

  // Only refresh if we own this port
  const script = `
    if redis.call("GET", KEYS[1]) == ARGV[1] then
      return redis.call("EXPIRE", KEYS[1], ARGV[2])
    else
      return 0
    end
  `;

  const result = await r.eval(script, 1, lockKey, workerId, PORT_TTL_SECONDS);
  return result === 1;
}

/**
 * Get list of currently allocated ports (for debugging/monitoring)
 */
export async function getAllocatedPorts(): Promise<{ port: number; worker: string }[]> {
  const r = getRedis();
  const portBase = parseInt(process.env.LEAN_STREAMING_PORT_BASE || '5680', 10);
  const portMax = parseInt(process.env.LEAN_STREAMING_PORT_MAX || '5780', 10);

  const allocated: { port: number; worker: string }[] = [];

  for (let port = portBase; port <= portMax; port++) {
    const lockKey = `${PORT_LOCK_PREFIX}${port}`;
    const worker = await r.get(lockKey);
    if (worker) {
      allocated.push({ port, worker });
    }
  }

  return allocated;
}

/**
 * Clean up stale port allocations (called on worker startup)
 * This handles cases where workers crashed without releasing ports
 * Note: TTL-based expiration handles this automatically, but this is faster
 */
export async function cleanupStalePorts(workerId: string): Promise<number> {
  const r = getRedis();
  const portBase = parseInt(process.env.LEAN_STREAMING_PORT_BASE || '5680', 10);
  const portMax = parseInt(process.env.LEAN_STREAMING_PORT_MAX || '5780', 10);

  let cleaned = 0;

  for (let port = portBase; port <= portMax; port++) {
    const lockKey = `${PORT_LOCK_PREFIX}${port}`;
    const owner = await r.get(lockKey);

    // Release ports owned by this worker (from previous crash)
    if (owner === workerId) {
      await r.del(lockKey);
      cleaned++;
      console.log(`[Distributed Ports] Cleaned up stale port ${port} from previous ${workerId} session`);
    }
  }

  return cleaned;
}

/**
 * Close Redis connection (for graceful shutdown)
 */
export async function closeDistributedPorts(): Promise<void> {
  if (redis) {
    await redis.quit();
    redis = null;
  }
}
