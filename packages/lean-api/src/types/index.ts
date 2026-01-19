export * from './qc-api.js';

// Internal types for the LEAN API service
// These match the consolidated database schema

/**
 * ProjectFile - stored in project_files table
 * (renamed from lean_files)
 */
export interface ProjectFile {
  id: number;
  projectId: number;
  name: string;
  content: string;
  isMain: boolean;
  createdAt: Date;
  modifiedAt: Date;
}

/**
 * Backtest - stored in qc_backtests table
 * Used for both cloud-synced and self-hosted backtests
 * (replaces LeanBacktest, using unified qc_backtests table)
 */
export interface Backtest {
  id: number;
  projectId: number;
  userId: string;
  qcBacktestId: string;       // UUID for self-hosted, QC backtest ID for cloud
  qcProjectId: number;
  name: string;
  note: string | null;
  description: string | null;
  status: string;             // 'queued' | 'running' | 'completed' | 'error'
  progress: number;
  startedAt: Date | null;
  completedAt: Date | null;
  errorMessage: string | null;
  parameters: Record<string, unknown>;
  startDate: Date;
  endDate: Date;
  cash: number;
  initialCapital: number | null;
  finalEquity: number | null;
  // Core statistics
  netProfit: number | null;
  sharpeRatio: number | null;
  cagr: number | null;
  drawdown: number | null;
  totalTrades: number | null;
  winRate: number | null;
  profitLossRatio: number | null;
  // Extended statistics
  alpha: number | null;
  beta: number | null;
  sortinoRatio: number | null;
  treynorRatio: number | null;
  informationRatio: number | null;
  trackingError: number | null;
  annualStdDev: number | null;
  annualVariance: number | null;
  totalFees: number | null;
  averageWin: number | null;
  averageLoss: number | null;
  endEquity: number | null;
  // JSON data
  resultJson: unknown;
  rollingWindow: unknown;
  ordersJson: unknown;
  insightsJson: unknown;
  extendedStats: unknown;
  // Metadata
  source: 'cloud' | 'self_hosted';
  qcCreatedAt: Date | null;
  syncedAt: Date | null;
  createdAt: Date;
}

/**
 * Optimization - stored in optimizations table
 * (renamed from lean_optimizations)
 */
export interface Optimization {
  id: number;
  optimizationId: string;
  projectId: number;
  userId: string;
  name: string;
  status: 'queued' | 'running' | 'completed' | 'error';
  progress: number;
  startedAt: Date | null;
  completedAt: Date | null;
  errorMessage: string | null;
  parameters: Record<string, unknown>;
  target: string;
  startDate: Date;
  endDate: Date;
  cash: number;
  totalBacktests: number | null;
  completedBacktests: number;
  results: unknown;
  bestParameters: unknown;
  note: string | null;
  description: string | null;
  createdAt: Date;
}

export interface MarketDataDaily {
  id: number;
  symbol: string;
  date: Date;
  open: number;
  high: number;
  low: number;
  close: number;
  adjustedClose: number;
  volume: number;
  dividend: number;
  splitCoefficient: number;
  source: string;
  fetchedAt: Date;
  // Ownership tracking
  ownerType: 'platform' | 'user';
  ownerId: string | null;
  provider: string;
}

export interface MarketDataSymbol {
  id: number;
  symbol: string;
  name: string | null;
  exchange: string | null;
  assetType: 'stock' | 'etf';
  firstDate: Date | null;
  lastDate: Date | null;
  lastFetchedAt: Date | null;
  isComplete: boolean;
  createdAt: Date;
  // Ownership tracking
  ownerType: 'platform' | 'user';
  ownerId: string | null;
  provider: string;
}

// Job types for BullMQ
export interface BacktestJobData {
  backtestId: string;
  projectId: number;
  userId: string;
  startDate: string;
  endDate: string;
  cash: number;
  parameters: Record<string, unknown>;
}

export interface OptimizationJobData {
  optimizationId: string;
  projectId: number;
  userId: string;
  parameters: Array<{ name: string; min: number; max: number; step: number }>;
  target: string;
  startDate: string;
  endDate: string;
  cash: number;
}

/**
 * Project - stored in projects table
 * Main project entity
 */
export interface Project {
  id: number;
  userId: string;
  name: string;
  description: string | null;
  qcProjectId: string | null;
  language: 'Py' | 'C#';
  status: string;
  tags: unknown;
  activeVersionId: number | null;
  deployedVersionId: number | null;
  createdAt: Date;
  updatedAt: Date;
  modifiedAt: Date;
}

// Legacy type aliases for backward compatibility during migration
export type LeanFile = ProjectFile;
export type LeanBacktest = Backtest;
export type LeanOptimization = Optimization;
export type LeanProject = Project;
