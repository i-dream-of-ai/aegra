/**
 * Centralized Error Handling Utilities
 *
 * IMPORTANT: Always return REAL error messages to API consumers.
 * This is an internal API - developers need actual error details to debug.
 * No sanitization, no hiding - full error details in responses.
 */

/**
 * Application-specific error with context
 */
export class AppError extends Error {
  public readonly statusCode: number;
  public readonly context?: Record<string, unknown>;

  constructor(
    message: string,
    statusCode: number = 500,
    context?: Record<string, unknown>
  ) {
    super(message);
    this.name = 'AppError';
    this.statusCode = statusCode;
    this.context = context;
    Error.captureStackTrace(this, this.constructor);
  }
}

/**
 * Format error for logging - includes full details with stack trace
 */
export function formatErrorForLog(error: unknown, context?: Record<string, unknown>): string {
  const lines: string[] = [];

  if (error instanceof AppError) {
    lines.push(`[AppError] ${error.message}`);
    lines.push(`  Status: ${error.statusCode}`);
    if (error.context) {
      lines.push(`  Context: ${JSON.stringify(error.context)}`);
    }
    if (error.stack) {
      lines.push(`  Stack: ${error.stack}`);
    }
  } else if (error instanceof Error) {
    lines.push(`[Error] ${error.name}: ${error.message}`);

    // Include all error properties for debugging
    const errorObj = error as unknown as Record<string, unknown>;
    const props = ['code', 'errno', 'syscall', 'address', 'port', 'hostname', 'detail', 'hint', 'position', 'where', 'schema', 'table', 'column', 'constraint', 'routine'];
    for (const key of props) {
      if (key in errorObj && errorObj[key] !== undefined) {
        lines.push(`  ${key}: ${errorObj[key]}`);
      }
    }

    if (error.stack) {
      lines.push(`  Stack: ${error.stack}`);
    }
  } else {
    lines.push(`[Unknown Error] ${String(error)}`);
  }

  if (context) {
    lines.push(`  Request Context: ${JSON.stringify(context)}`);
  }

  return lines.join('\n');
}

/**
 * Format error for API response - FULL error details for developers
 * No sanitization - return everything so developers can debug
 */
export function formatErrorForResponse(error: unknown): string {
  if (error instanceof AppError) {
    const parts: string[] = [error.message];
    if (error.context) {
      parts.push(`Context: ${JSON.stringify(error.context)}`);
    }
    return parts.join(' | ');
  }

  if (error instanceof Error) {
    const parts: string[] = [error.message];
    const errorObj = error as unknown as Record<string, unknown>;

    // Include ALL relevant error properties
    if (errorObj.code) {
      parts.push(`code: ${errorObj.code}`);
    }
    if (errorObj.detail) {
      parts.push(`detail: ${errorObj.detail}`);
    }
    if (errorObj.hint) {
      parts.push(`hint: ${errorObj.hint}`);
    }
    if (errorObj.constraint) {
      parts.push(`constraint: ${errorObj.constraint}`);
    }
    if (errorObj.table) {
      parts.push(`table: ${errorObj.table}`);
    }
    if (errorObj.column) {
      parts.push(`column: ${errorObj.column}`);
    }
    if (errorObj.address !== undefined && errorObj.port !== undefined) {
      parts.push(`address: ${errorObj.address}:${errorObj.port}`);
    }
    if (errorObj.syscall) {
      parts.push(`syscall: ${errorObj.syscall}`);
    }

    return parts.join(' | ');
  }

  return String(error);
}

/**
 * Get HTTP status code from error
 */
export function getErrorStatusCode(error: unknown): number {
  if (error instanceof AppError) {
    return error.statusCode;
  }

  if (error instanceof Error) {
    const errorObj = error as unknown as Record<string, unknown>;
    const message = error.message.toLowerCase();

    // PostgreSQL error codes - return appropriate HTTP status
    if (errorObj.code === '23505') return 409; // unique_violation
    if (errorObj.code === '23503') return 400; // foreign_key_violation
    if (errorObj.code === '23502') return 400; // not_null_violation
    if (errorObj.code === '42P01') return 500; // undefined_table
    if (errorObj.code === '42703') return 500; // undefined_column
    if (errorObj.code === '28P01') return 503; // invalid_password (db auth)
    if (errorObj.code === '3D000') return 503; // invalid_catalog_name (db doesn't exist)
    if (errorObj.code === '57P03') return 503; // cannot_connect_now

    // Network errors
    if (errorObj.code === 'ECONNREFUSED') return 503;
    if (errorObj.code === 'ETIMEDOUT') return 504;
    if (errorObj.code === 'ENOTFOUND') return 503;
    if (errorObj.code === 'ECONNRESET') return 503;

    // Message-based detection
    if (message.includes('not found')) return 404;
    if (message.includes('unauthorized') || message.includes('authentication')) return 401;
    if (message.includes('forbidden') || message.includes('access denied')) return 403;
    if (message.includes('invalid') || message.includes('required')) return 400;
    if (message.includes('duplicate') || message.includes('already exists')) return 409;
    if (message.includes('timeout')) return 504;
  }

  return 500;
}

/**
 * Log error with full context to console
 */
export function logError(
  location: string,
  error: unknown,
  context?: Record<string, unknown>
): void {
  const timestamp = new Date().toISOString();
  console.error(`\n[${timestamp}] ERROR in ${location}:`);
  console.error(formatErrorForLog(error, context));
}

/**
 * Create standardized error response with REAL error details
 */
export function createErrorResponse(error: unknown): {
  success: false;
  errors: string[];
} {
  return {
    success: false,
    errors: [formatErrorForResponse(error)],
  };
}
