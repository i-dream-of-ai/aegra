export * from './qc-api.js';

// Internal types for the LEAN API service

export interface LeanProject {
  id: number;
  userId: string;
  name: string;
  language: 'Py' | 'C#';
  createdAt: Date;
  modifiedAt: Date;
}

export interface LeanFile {
  id: number;
  projectId: number;
  name: string;
  content: string;
  isMain: boolean;
  createdAt: Date;
  modifiedAt: Date;
}

export interface LeanBacktest {
  id: number;
  backtestId: string;
  projectId: number;
  userId: string;
  name: string;
  status: 'queued' | 'running' | 'completed' | 'error';
  progress: number;
  startedAt: Date | null;
  completedAt: Date | null;
  errorMessage: string | null;
  parameters: Record<string, unknown>;
  startDate: Date;
  endDate: Date;
  cash: number;
  netProfit: number | null;
  sharpeRatio: number | null;
  cagr: number | null;
  drawdown: number | null;
  totalTrades: number | null;
  winRate: number | null;
  profitLossRatio: number | null;
  resultJson: unknown;
  rollingWindow: unknown;
  createdAt: Date;
}

export interface LeanOptimization {
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
  totalBacktests: number | null;
  completedBacktests: number;
  results: unknown;
  bestParameters: unknown;
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
