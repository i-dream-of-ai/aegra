/**
 * Authentication Middleware
 * Uses Supabase SDK to validate JWT tokens
 */

import { Request, Response, NextFunction } from 'express';
import { createClient, SupabaseClient } from '@supabase/supabase-js';
import { logError } from '../utils/errors.js';

// Extend Express Request type
declare global {
  namespace Express {
    interface Request {
      userId: string;
    }
  }
}

// Singleton Supabase client
let supabaseClient: SupabaseClient | null = null;

function getSupabaseClient(): SupabaseClient {
  if (!supabaseClient) {
    const supabaseUrl = process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL;
    const supabaseServiceKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

    if (!supabaseUrl) {
      throw new Error('SUPABASE_URL or NEXT_PUBLIC_SUPABASE_URL is required');
    }
    if (!supabaseServiceKey) {
      throw new Error('SUPABASE_SERVICE_ROLE_KEY is required for token validation');
    }

    supabaseClient = createClient(supabaseUrl, supabaseServiceKey, {
      auth: {
        autoRefreshToken: false,
        persistSession: false,
      },
    });
  }
  return supabaseClient;
}

/**
 * Validate Supabase JWT token using the SDK
 * This properly verifies the token signature against Supabase
 */
async function validateSupabaseToken(token: string): Promise<{ valid: boolean; userId?: string; error?: string }> {
  try {
    const supabase = getSupabaseClient();

    // Use getUser with the token to validate it
    // This makes a call to Supabase to verify the token is valid
    const { data: { user }, error } = await supabase.auth.getUser(token);

    if (error) {
      return { valid: false, error: error.message };
    }

    if (!user) {
      return { valid: false, error: 'No user found for token' };
    }

    return { valid: true, userId: user.id };
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err);
    logError('validateSupabaseToken', err);
    return { valid: false, error: `Token validation failed: ${error}` };
  }
}

/**
 * Authentication middleware
 * Expects Bearer token with Supabase JWT
 * Also supports internal service auth via X-Internal-Service header
 */
export function authMiddleware(req: Request, res: Response, next: NextFunction): void {
  const authHeader = req.headers.authorization;
  const internalServiceHeader = req.headers['x-internal-service'];
  const internalUserId = req.headers['x-user-id'];
  const requestContext = {
    method: req.method,
    path: req.path,
    hasAuthHeader: !!authHeader,
  };

  // Internal service-to-service auth (aegra -> lean-api on same Docker network)
  const internalSecret = process.env.INTERNAL_SERVICE_SECRET;
  if (internalServiceHeader && internalSecret && internalServiceHeader === internalSecret) {
    // Use provided user ID or default to internal service user
    req.userId = (internalUserId as string) || '__internal_service__';
    return next();
  }

  // Must have Authorization header
  if (!authHeader) {
    // Development mode: allow unauthenticated requests with test user
    if (process.env.NODE_ENV === 'development' && process.env.DEV_USER_ID) {
      req.userId = process.env.DEV_USER_ID;
      return next();
    }

    res.status(401).json({
      success: false,
      errors: ['Authentication required. Provide Bearer token with Supabase JWT.'],
    });
    return;
  }

  // Must be Bearer token
  if (!authHeader.startsWith('Bearer ')) {
    const scheme = authHeader.split(' ')[0];
    const error = `Unsupported authentication scheme: ${scheme}. Use Bearer token.`;
    logError('authMiddleware', new Error(error), requestContext);
    res.status(401).json({
      success: false,
      errors: [error],
    });
    return;
  }

  const token = authHeader.substring(7);

  if (!token) {
    res.status(401).json({
      success: false,
      errors: ['Missing token in Bearer header'],
    });
    return;
  }

  // Validate token with Supabase
  validateSupabaseToken(token)
    .then(result => {
      if (result.valid && result.userId) {
        req.userId = result.userId;
        return next();
      }
      const error = result.error || 'Invalid authentication token';
      logError('authMiddleware:tokenValidation', new Error(error), requestContext);
      res.status(401).json({
        success: false,
        errors: [error],
      });
    })
    .catch(err => {
      logError('authMiddleware:tokenValidation', err, requestContext);
      res.status(401).json({
        success: false,
        errors: ['Authentication error: ' + (err instanceof Error ? err.message : String(err))],
      });
    });
}

/**
 * Optional auth middleware - doesn't fail if no auth provided
 */
export function optionalAuthMiddleware(req: Request, res: Response, next: NextFunction): void {
  const authHeader = req.headers.authorization;

  if (!authHeader) {
    req.userId = '__anonymous__';
    return next();
  }

  authMiddleware(req, res, next);
}
