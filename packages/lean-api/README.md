# LEAN API - Self-Hosted Backtesting Service

QC-compatible REST API for self-hosted algorithmic trading backtests.

## Overview

This service provides a drop-in replacement for QuantConnect's API, allowing you to run backtests on your own infrastructure using the LEAN engine with Alpha Vantage market data.

## Features

- **QC API Compatibility**: 1:1 API compatibility with QuantConnect Cloud
- **Market Data Caching**: Fetch-on-demand with permanent caching (never fetch twice)
- **Job Queue**: BullMQ-based async backtest processing
- **User Isolation**: Per-user data isolation via Supabase RLS

## Quick Start

### Prerequisites

- Node.js 20+
- PostgreSQL (via Supabase)
- Redis (for job queue)
- Alpha Vantage API key

### Environment Variables

```bash
# Required
DATABASE_URL=postgresql://...
REDIS_URL=redis://localhost:6379
ALPHA_VANTAGE_API_KEY=your-key

# Optional
PORT=3001
LEAN_API_SECRET=your-secret
DEV_USER_ID=test-user-id  # For local development
BACKTEST_CONCURRENCY=2
```

### Running Locally

```bash
# Install dependencies
pnpm install

# Run database migrations
pnpm supabase db push

# Start the service
pnpm dev
```

### Docker Compose

```bash
cd docker/lean-worker
docker-compose up
```

## API Endpoints

All endpoints follow QuantConnect's API format exactly.

### Projects

```
POST /api/v2/projects/read    - List projects
POST /api/v2/projects/create  - Create project
POST /api/v2/projects/delete  - Delete project
```

### Files

```
POST /api/v2/files/read    - List/read files
POST /api/v2/files/update  - Create/update file
```

### Compile

```
POST /api/v2/compile/create - Validate algorithm
```

### Backtests

```
POST /api/v2/backtests/create     - Start backtest
POST /api/v2/backtests/list       - List backtests
POST /api/v2/backtests/read       - Get backtest details
POST /api/v2/backtests/delete     - Delete backtest
POST /api/v2/backtests/chart/read - Get chart data
```

### Optimizations

```
POST /api/v2/optimizations/list - List optimizations
POST /api/v2/optimizations/read - Get optimization details
```

## Authentication

Supports both:
- QC-style Basic auth: `Basic base64(userId:sha256(token:timestamp))`
- Supabase JWT: `Bearer <jwt>`
- Internal API key: `X-API-Key: <secret>`

## Switching Between QC and Self-Hosted

In your `.env.local`:

```bash
# Use QuantConnect Cloud (default)
USE_SELF_HOSTED_LEAN=false

# Use self-hosted LEAN
USE_SELF_HOSTED_LEAN=true
LEAN_API_URL=https://lean.yourdomain.com
```

No code changes required - the same API client works with both backends.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Express    │────▶│   BullMQ     │────▶│    LEAN      │
│   Server     │     │   Queue      │     │   Workers    │
└──────────────┘     └──────────────┘     └──────────────┘
       │                    │                    │
       ▼                    ▼                    ▼
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  PostgreSQL  │     │    Redis     │     │ Market Data  │
│  (Supabase)  │     │              │     │    Cache     │
└──────────────┘     └──────────────┘     └──────────────┘
```

## Development

```bash
# Run with hot reload
pnpm dev

# Build
pnpm build

# Type check
pnpm tsc --noEmit
```
