/**
 * QuantConnect API-compatible types
 * These types match QC's API response formats exactly for drop-in compatibility
 */

// Base response format - all QC responses follow this pattern
export interface QCBaseResponse {
  success: boolean;
  errors: string[];
}

// Project types
export interface QCProject {
  projectId: number;
  name: string;
  created: string;      // ISO date string
  modified: string;     // ISO date string
  language: 'Py' | 'C#';
}

export interface QCProjectsResponse extends QCBaseResponse {
  projects: QCProject[];
}

export interface QCProjectCreateResponse extends QCBaseResponse {
  projects: QCProject[];
}

// File types
export interface QCFile {
  name: string;
  content: string;
  modified: string;     // ISO date string
  isLibrary: boolean;
}

export interface QCFilesResponse extends QCBaseResponse {
  files: QCFile[];
}

// Compile types
export interface QCCompileResponse extends QCBaseResponse {
  compileId: string;
  state: 'InQueue' | 'Building' | 'BuildSuccess' | 'BuildError';
  logs: string[];
}

// Backtest types
export interface QCBacktest {
  backtestId: string;
  projectId: number;
  name: string;
  created: string;
  completed: boolean;
  progress: number;     // 0-1
  result?: QCBacktestResult;
  error?: string;
}

export interface QCBacktestResult {
  // Statistics
  TotalPerformance: {
    TradeStatistics: {
      TotalNumberOfTrades: number;
      WinRate: number;
      LossRate: number;
      ProfitLossRatio: number;
    };
    PortfolioStatistics: {
      SharpeRatio: number;
      CompoundingAnnualReturn: number;  // CAGR
      TotalNetProfit: number;
      Drawdown: number;
    };
  };
  // Rolling window for charts
  // Using unknown for flexibility in internal storage
  RollingWindow?: Record<string, unknown>;
  // Orders
  Orders?: Record<string, QCOrder>;
}

export interface QCChartSeries {
  Name: string;
  Unit: string;
  Index: number;
  Values: Array<{
    x: number;  // Unix timestamp
    y: number;
  }>;
  SeriesType: number;
  Color: string;
  ScatterMarkerSymbol: string;
}

export interface QCOrder {
  Id: number;
  Symbol: string;
  Quantity: number;
  Price: number;
  Time: string;
  Status: string;
  Type: string;
}

export interface QCBacktestsResponse extends QCBaseResponse {
  backtests: QCBacktest[];
}

export interface QCBacktestResponse extends QCBaseResponse {
  backtest: QCBacktest;
}

export interface QCBacktestCreateResponse extends QCBaseResponse {
  backtestId: string;
}

export interface QCChartResponse extends QCBaseResponse {
  chart: Record<string, QCChartSeries>;
}

// Optimization types
export interface QCOptimization {
  optimizationId: string;
  projectId: number;
  name: string;
  created: string;
  status: 'New' | 'Running' | 'Completed' | 'Error' | 'Aborted';
  runtimeStatistics?: {
    Completed: number;
    Failed: number;
    Running: number;
    InQueue: number;
  };
  backtests?: QCOptimizationBacktest[];
}

export interface QCOptimizationBacktest {
  id: string;
  name: string;
  exitCode: number;
  parameterSet: Record<string, number | string>;
  statistics: {
    SharpeRatio: number;
    CompoundingAnnualReturn: number;
    TotalNetProfit: number;
    Drawdown: number;
    TotalNumberOfTrades: number;
    WinRate: number;
  };
}

export interface QCOptimizationsResponse extends QCBaseResponse {
  optimizations: QCOptimization[];
}

export interface QCOptimizationResponse extends QCBaseResponse {
  optimization: QCOptimization;
}

// Request body types
export interface ProjectsReadRequest {
  projectId?: number;
}

export interface ProjectsCreateRequest {
  name: string;
  language?: 'Py' | 'C#';
}

export interface ProjectsDeleteRequest {
  projectId: number;
}

export interface FilesReadRequest {
  projectId: number;
  fileName?: string;
}

export interface FilesUpdateRequest {
  projectId: number;
  name: string;
  content: string;
}

export interface CompileCreateRequest {
  projectId: number;
}

export interface BacktestsCreateRequest {
  projectId: number;
  compileId: string;
  backtestName: string;
}

export interface BacktestsListRequest {
  projectId: number;
}

export interface BacktestsReadRequest {
  projectId: number;
  backtestId: string;
}

export interface BacktestsDeleteRequest {
  projectId: number;
  backtestId: string;
}

export interface BacktestsChartReadRequest {
  projectId: number;
  backtestId: string;
  name?: string;        // Chart name, defaults to 'Strategy Equity'
  start?: number;       // Start index
  end?: number;         // End index
}

export interface OptimizationsListRequest {
  projectId: number;
}

export interface OptimizationsReadRequest {
  projectId: number;
  optimizationId: string;
}
