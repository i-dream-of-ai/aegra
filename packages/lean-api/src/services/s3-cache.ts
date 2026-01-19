/**
 * LEAN Data Cache Paths
 *
 * The LEAN data cache is mounted via s3fs at /opt/lean-data-cache on the host,
 * which is bind-mounted into the container at /app/lean-data-cache.
 *
 * This file exports the path constants used by the backtest worker.
 * The actual storage is handled by the s3fs filesystem mount.
 */

// Local cache directory inside container
const LOCAL_CACHE_DIR = process.env.LEAN_DATA_CACHE_DIR || '/app/lean-data-cache';

// Host cache directory for Docker volume mounts (sibling container pattern)
const HOST_CACHE_DIR = process.env.LEAN_HOST_DATA_CACHE_DIR || '/opt/lean-data-cache';

/**
 * Get the local cache directory path (inside container)
 */
export function getLocalCacheDir(): string {
  return LOCAL_CACHE_DIR;
}

/**
 * Get the host cache directory path (for Docker volume mounts with sibling containers)
 */
export function getHostCacheDir(): string {
  return HOST_CACHE_DIR;
}
