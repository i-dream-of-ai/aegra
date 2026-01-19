/**
 * S3-Compatible Cache Service for LEAN Data (DigitalOcean Spaces)
 *
 * Stores and retrieves LEAN-format market data (zips, map files, factor files) in DO Spaces.
 * Data is generated once per symbol and reused across all backtests.
 *
 * DigitalOcean Spaces is S3-compatible, so we use the AWS SDK with DO endpoint.
 */

import {
  S3Client,
  GetObjectCommand,
  PutObjectCommand,
  HeadObjectCommand,
  ListObjectsV2Command,
} from '@aws-sdk/client-s3';
import { Upload } from '@aws-sdk/lib-storage';
import * as fs from 'fs/promises';
import * as path from 'path';
import { createReadStream, createWriteStream } from 'fs';
import { pipeline } from 'stream/promises';
import { Readable } from 'stream';

// DigitalOcean Spaces Configuration (S3-compatible)
// Spaces endpoint format: https://{region}.digitaloceanspaces.com
const DO_SPACES_REGION = process.env.DO_SPACES_REGION || 'nyc3';
const DO_SPACES_ENDPOINT = process.env.DO_SPACES_ENDPOINT || `https://${DO_SPACES_REGION}.digitaloceanspaces.com`;
const DO_SPACES_BUCKET = process.env.DO_SPACES_BUCKET || 'lean-data-cache';
const DO_SPACES_PREFIX = process.env.DO_SPACES_PREFIX || 'lean-data';

// Local cache directory (for downloads before mounting to LEAN container)
const LOCAL_CACHE_DIR = process.env.LEAN_DATA_CACHE_DIR || '/app/lean-data-cache';
const HOST_CACHE_DIR = process.env.LEAN_HOST_DATA_CACHE_DIR || LOCAL_CACHE_DIR;

// Initialize S3-compatible client for DigitalOcean Spaces
const s3Client = new S3Client({
  region: DO_SPACES_REGION,
  endpoint: DO_SPACES_ENDPOINT,
  credentials: {
    accessKeyId: process.env.DO_SPACES_KEY || '',
    secretAccessKey: process.env.DO_SPACES_SECRET || '',
  },
  forcePathStyle: false, // DO Spaces uses virtual-hosted-style URLs
});

/**
 * Check if DO Spaces storage is configured
 */
export function isS3Enabled(): boolean {
  return !!(
    process.env.DO_SPACES_KEY &&
    process.env.DO_SPACES_SECRET &&
    process.env.DO_SPACES_BUCKET
  );
}

/**
 * Get S3 key for a file path
 */
function getS3Key(relativePath: string): string {
  return `${DO_SPACES_PREFIX}/${relativePath}`;
}

/**
 * Check if a file exists in S3
 */
export async function existsInS3(relativePath: string): Promise<boolean> {
  try {
    await s3Client.send(new HeadObjectCommand({
      Bucket: DO_SPACES_BUCKET,
      Key: getS3Key(relativePath),
    }));
    return true;
  } catch (error: unknown) {
    const err = error as { name?: string };
    if (err.name === 'NotFound' || err.name === '404') {
      return false;
    }
    // For other errors, assume file doesn't exist
    console.warn(`[S3 Cache] Error checking ${relativePath}:`, error);
    return false;
  }
}

/**
 * Check if symbol data exists in S3 cache
 */
export async function isSymbolCachedInS3(symbol: string): Promise<boolean> {
  const symbolLower = symbol.toLowerCase();

  const files = [
    `equity/usa/daily/${symbolLower}.zip`,
    `equity/usa/map_files/${symbolLower}.csv`,
    `equity/usa/factor_files/${symbolLower}.csv`,
  ];

  // Check all files exist
  const results = await Promise.all(files.map(f => existsInS3(f)));
  return results.every(r => r);
}

/**
 * Upload a file to S3
 */
export async function uploadToS3(localPath: string, relativePath: string): Promise<void> {
  const fileStream = createReadStream(localPath);
  const key = getS3Key(relativePath);

  const upload = new Upload({
    client: s3Client,
    params: {
      Bucket: DO_SPACES_BUCKET,
      Key: key,
      Body: fileStream,
    },
  });

  await upload.done();
  console.log(`[S3 Cache] Uploaded ${relativePath}`);
}

/**
 * Upload content directly to S3 (without temp file)
 */
export async function uploadContentToS3(content: string | Buffer, relativePath: string): Promise<void> {
  const key = getS3Key(relativePath);

  await s3Client.send(new PutObjectCommand({
    Bucket: DO_SPACES_BUCKET,
    Key: key,
    Body: content,
  }));
  console.log(`[S3 Cache] Uploaded ${relativePath}`);
}

/**
 * Download a file from S3 to local cache
 */
export async function downloadFromS3(relativePath: string, localPath: string): Promise<void> {
  const key = getS3Key(relativePath);

  const response = await s3Client.send(new GetObjectCommand({
    Bucket: DO_SPACES_BUCKET,
    Key: key,
  }));

  if (!response.Body) {
    throw new Error(`Empty response body for ${relativePath}`);
  }

  // Ensure directory exists
  await fs.mkdir(path.dirname(localPath), { recursive: true });

  // Stream to file
  const writeStream = createWriteStream(localPath);
  await pipeline(response.Body as Readable, writeStream);

  console.log(`[S3 Cache] Downloaded ${relativePath} to ${localPath}`);
}

/**
 * Download all data for a symbol from S3 to local cache
 */
export async function downloadSymbolFromS3(symbol: string): Promise<void> {
  const symbolLower = symbol.toLowerCase();

  const files = [
    `equity/usa/daily/${symbolLower}.zip`,
    `equity/usa/map_files/${symbolLower}.csv`,
    `equity/usa/factor_files/${symbolLower}.csv`,
  ];

  await Promise.all(files.map(async (relativePath) => {
    const localPath = path.join(LOCAL_CACHE_DIR, relativePath);
    await downloadFromS3(relativePath, localPath);
  }));

  console.log(`[S3 Cache] Downloaded all files for ${symbol}`);
}

/**
 * Ensure symbol data is available locally (download from S3 if needed)
 */
export async function ensureSymbolLocallyAvailable(symbol: string): Promise<boolean> {
  const symbolLower = symbol.toLowerCase();

  // Check local cache first
  const localZip = path.join(LOCAL_CACHE_DIR, 'equity', 'usa', 'daily', `${symbolLower}.zip`);
  const localMap = path.join(LOCAL_CACHE_DIR, 'equity', 'usa', 'map_files', `${symbolLower}.csv`);
  const localFactor = path.join(LOCAL_CACHE_DIR, 'equity', 'usa', 'factor_files', `${symbolLower}.csv`);

  try {
    await Promise.all([
      fs.access(localZip),
      fs.access(localMap),
      fs.access(localFactor),
    ]);
    console.log(`[S3 Cache] ${symbol} already available locally`);
    return true;
  } catch {
    // Not in local cache, check S3
  }

  // Check S3
  const inS3 = await isSymbolCachedInS3(symbol);
  if (!inS3) {
    console.log(`[S3 Cache] ${symbol} not in S3, needs generation`);
    return false;
  }

  // Download from S3
  await downloadSymbolFromS3(symbol);
  return true;
}

/**
 * Upload symbol data to S3 after generation
 */
export async function uploadSymbolToS3(symbol: string): Promise<void> {
  const symbolLower = symbol.toLowerCase();

  const files = [
    { local: path.join(LOCAL_CACHE_DIR, 'equity', 'usa', 'daily', `${symbolLower}.zip`),
      s3: `equity/usa/daily/${symbolLower}.zip` },
    { local: path.join(LOCAL_CACHE_DIR, 'equity', 'usa', 'map_files', `${symbolLower}.csv`),
      s3: `equity/usa/map_files/${symbolLower}.csv` },
    { local: path.join(LOCAL_CACHE_DIR, 'equity', 'usa', 'factor_files', `${symbolLower}.csv`),
      s3: `equity/usa/factor_files/${symbolLower}.csv` },
  ];

  await Promise.all(files.map(async ({ local, s3 }) => {
    try {
      await fs.access(local);
      await uploadToS3(local, s3);
    } catch (error) {
      console.warn(`[S3 Cache] Could not upload ${local}:`, error);
    }
  }));

  console.log(`[S3 Cache] Uploaded all files for ${symbol} to S3`);
}

/**
 * Ensure static data (symbol-properties, market-hours) is available locally
 * Downloads from S3 or GitHub if needed
 */
export async function ensureStaticDataAvailable(): Promise<void> {
  const staticFiles = [
    { path: 'symbol-properties/symbol-properties-database.csv',
      url: 'https://raw.githubusercontent.com/QuantConnect/Lean/master/Data/symbol-properties/symbol-properties-database.csv' },
    { path: 'market-hours/market-hours-database.json',
      url: 'https://raw.githubusercontent.com/QuantConnect/Lean/master/Data/market-hours/market-hours-database.json' },
  ];

  for (const { path: relativePath, url } of staticFiles) {
    const localPath = path.join(LOCAL_CACHE_DIR, relativePath);

    // Check local
    try {
      await fs.access(localPath);
      console.log(`[S3 Cache] Static data ${relativePath} available locally`);
      continue;
    } catch {
      // Not local
    }

    // Try S3
    if (isS3Enabled()) {
      try {
        const inS3 = await existsInS3(relativePath);
        if (inS3) {
          await downloadFromS3(relativePath, localPath);
          continue;
        }
      } catch (error) {
        console.warn(`[S3 Cache] Could not download ${relativePath} from S3:`, error);
      }
    }

    // Download from GitHub
    console.log(`[S3 Cache] Downloading ${relativePath} from GitHub...`);
    const response = await fetch(url);
    if (!response.ok) {
      throw new Error(`Failed to download ${relativePath}: ${response.status}`);
    }
    const content = await response.text();

    await fs.mkdir(path.dirname(localPath), { recursive: true });
    await fs.writeFile(localPath, content);

    // Upload to S3 for future use
    if (isS3Enabled()) {
      await uploadContentToS3(content, relativePath);
    }
  }
}

/**
 * Get the local cache directory path (for Docker volume mounts)
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
