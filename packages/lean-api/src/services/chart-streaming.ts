/**
 * Chart Streaming Service
 * Uses Redis pub/sub to stream real-time chart updates from workers to SSE routes
 *
 * Architecture:
 * - Worker receives chart updates from LEAN via ZeroMQ
 * - Worker publishes to Redis channel: backtest:{backtestId}:chart
 * - SSE route subscribes to the channel and forwards to client
 */

import { Redis } from 'ioredis';

// Redis connection factory
const getRedisConnection = () => ({
  host: process.env.REDIS_HOST || 'localhost',
  port: parseInt(process.env.REDIS_PORT || '6379', 10),
});

// Publisher client (singleton)
let publisher: Redis | null = null;

function getPublisher(): Redis {
  if (!publisher) {
    publisher = new Redis(getRedisConnection());
    publisher.on('error', (err: Error) => {
      console.error('[ChartStreaming] Publisher error:', err);
    });
  }
  return publisher;
}

/**
 * Chart update payload streamed from LEAN
 */
export interface ChartStreamPayload {
  chartName: string;
  seriesName: string;
  points: Array<{ x: number; y: number }>;
}

/**
 * Get the Redis channel name for a backtest's chart updates
 */
export function getChartChannel(backtestId: string): string {
  return `backtest:${backtestId}:chart`;
}

/**
 * Publish a chart update for a backtest
 * Called by the worker when receiving ZeroMQ messages from LEAN
 */
export async function publishChartUpdate(
  backtestId: string,
  update: ChartStreamPayload
): Promise<void> {
  const channel = getChartChannel(backtestId);
  const message = JSON.stringify(update);
  await getPublisher().publish(channel, message);
}

/**
 * Subscribe to chart updates for a backtest
 * Returns a cleanup function to unsubscribe
 *
 * @param backtestId - The backtest ID to subscribe to
 * @param onUpdate - Callback when a chart update is received
 * @returns Cleanup function
 */
export function subscribeToChartUpdates(
  backtestId: string,
  onUpdate: (update: ChartStreamPayload) => void
): () => void {
  const subscriber = new Redis(getRedisConnection());
  const channel = getChartChannel(backtestId);

  subscriber.subscribe(channel).then(() => {
    console.log(`[ChartStreaming] Subscribed to ${channel}`);
  }).catch((err) => {
    console.error(`[ChartStreaming] Failed to subscribe to ${channel}:`, err);
  });

  subscriber.on('message', (msgChannel: string, message: string) => {
    if (msgChannel === channel) {
      try {
        const update = JSON.parse(message) as ChartStreamPayload;
        onUpdate(update);
      } catch (err) {
        console.error('[ChartStreaming] Failed to parse message:', err);
      }
    }
  });

  // Return cleanup function
  return () => {
    console.log(`[ChartStreaming] Unsubscribing from ${channel}`);
    subscriber.unsubscribe(channel);
    subscriber.quit();
  };
}

/**
 * Close the publisher connection (for graceful shutdown)
 */
export async function closeChartStreaming(): Promise<void> {
  if (publisher) {
    await publisher.quit();
    publisher = null;
  }
}
