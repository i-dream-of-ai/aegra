import pg from 'pg';
const { Pool } = pg;

// Lazy-initialized connection pool
let pool: pg.Pool | null = null;

function getPool(): pg.Pool {
  if (!pool) {
    console.log('[Database] Initializing pool with URL:', process.env.DATABASE_URL?.substring(0, 50) + '...');
    pool = new Pool({
      connectionString: process.env.DATABASE_URL,
      ssl: { rejectUnauthorized: false },  // Required for Supabase
      max: 20,
      idleTimeoutMillis: 30000,
      connectionTimeoutMillis: 10000,
    });

    // Log connection errors
    pool.on('error', (err) => {
      console.error('[Database] Unexpected error on idle client:', err);
    });
  }
  return pool;
}

/**
 * Convert snake_case keys to camelCase
 */
function snakeToCamel(str: string): string {
  return str.replace(/_([a-z])/g, (_, letter) => letter.toUpperCase());
}

/**
 * Transform a row from snake_case to camelCase keys
 */
function transformRow<T>(row: Record<string, unknown>): T {
  const result: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(row)) {
    result[snakeToCamel(key)] = value;
  }
  return result as T;
}

// Export query helper with automatic camelCase transformation
export async function query<T>(text: string, params?: unknown[]): Promise<T[]> {
  const result = await getPool().query(text, params);
  return result.rows.map(row => transformRow<T>(row));
}

export async function queryOne<T>(text: string, params?: unknown[]): Promise<T | null> {
  const result = await getPool().query(text, params);
  if (!result.rows[0]) return null;
  return transformRow<T>(result.rows[0]);
}

export async function execute(text: string, params?: unknown[]): Promise<number> {
  const result = await getPool().query(text, params);
  return result.rowCount || 0;
}

// Transaction helper
export async function transaction<T>(
  callback: (client: pg.PoolClient) => Promise<T>
): Promise<T> {
  const client = await getPool().connect();
  try {
    await client.query('BEGIN');
    const result = await callback(client);
    await client.query('COMMIT');
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

/**
 * Transaction-aware query helpers for use within transaction callbacks
 * These use the client passed to the transaction callback instead of the pool
 */
export function clientQuery<T>(client: pg.PoolClient) {
  return async (text: string, params?: unknown[]): Promise<T[]> => {
    const result = await client.query(text, params);
    return result.rows.map(row => transformRow<T>(row));
  };
}

export function clientQueryOne<T>(client: pg.PoolClient) {
  return async (text: string, params?: unknown[]): Promise<T | null> => {
    const result = await client.query(text, params);
    if (!result.rows[0]) return null;
    return transformRow<T>(result.rows[0]);
  };
}

export function clientExecute(client: pg.PoolClient) {
  return async (text: string, params?: unknown[]): Promise<number> => {
    const result = await client.query(text, params);
    return result.rowCount || 0;
  };
}

// Health check - returns both status and error for proper logging
export async function checkHealth(): Promise<{ healthy: boolean; error?: string }> {
  try {
    await getPool().query('SELECT 1');
    return { healthy: true };
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err);
    return { healthy: false, error };
  }
}

// Graceful shutdown
export async function closePool(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
  }
}

export { getPool as pool };
