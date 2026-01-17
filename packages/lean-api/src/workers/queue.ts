/**
 * BullMQ Job Queue Setup
 * Handles async processing of backtests and optimizations
 */

import { Queue } from 'bullmq';
import type { BacktestJobData, OptimizationJobData } from '../types/index.js';

// Redis connection options - using simple config object
// This avoids version conflicts between ioredis versions
const getRedisConnection = () => ({
  host: process.env.REDIS_HOST || 'localhost',
  port: parseInt(process.env.REDIS_PORT || '6379', 10),
  maxRetriesPerRequest: null as null, // Required for BullMQ
});

// Backtest Queue
let backtestQueue: Queue | null = null;

export function getBacktestQueue(): Queue {
  if (!backtestQueue) {
    backtestQueue = new Queue('backtests', {
      connection: getRedisConnection(),
      defaultJobOptions: {
        attempts: 3,
        backoff: {
          type: 'exponential',
          delay: 1000,
        },
      },
    });
  }
  return backtestQueue;
}

// Optimization Queue
let optimizationQueue: Queue | null = null;

export function getOptimizationQueue(): Queue {
  if (!optimizationQueue) {
    optimizationQueue = new Queue('optimizations', {
      connection: getRedisConnection(),
      defaultJobOptions: {
        attempts: 3,
        backoff: {
          type: 'exponential',
          delay: 1000,
        },
      },
    });
  }
  return optimizationQueue;
}

// Graceful shutdown
export async function closeQueues(): Promise<void> {
  if (backtestQueue) {
    await backtestQueue.close();
    backtestQueue = null;
  }
  if (optimizationQueue) {
    await optimizationQueue.close();
    optimizationQueue = null;
  }
}
