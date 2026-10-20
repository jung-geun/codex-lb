import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { getReports, getReportsOptions, getThreadIdentity, type ReportsParams } from "../api";
import { isReportDateRangeValid } from "../date";
import type { ReportsResponse, ReportsOptionsResponse, ThreadIdentityResponse } from "../schemas";

export type ReportsQueryResult = UseQueryResult<ReportsResponse>;
export type ReportsOptionsQueryResult = UseQueryResult<ReportsOptionsResponse>;
export type ThreadIdentityQueryResult = UseQueryResult<ThreadIdentityResponse>;

type ReportsFilterState = {
  startDate: string | undefined;
  endDate: string | undefined;
  accountId: string[];
  apiKeyId?: string[];
  accountType?: string[];
  model: string | undefined;
  useragent?: string | undefined;
};

export function useReports(
  filters: ReportsFilterState,
  timeZone: string | undefined,
): ReportsQueryResult {
  return useQuery<ReportsResponse>({
    enabled: isReportDateRangeValid(filters.startDate, filters.endDate),
    queryKey: ["reports", filters, timeZone],
    queryFn: () =>
      getReports({
        startDate: filters.startDate,
        endDate: filters.endDate,
        accountId: filters.accountId.length > 0 ? filters.accountId : undefined,
        apiKeyId:
          filters.apiKeyId && filters.apiKeyId.length > 0
            ? filters.apiKeyId
            : undefined,
        accountType: filters.accountType?.length ? filters.accountType : undefined,
        model: filters.model || undefined,
        useragent: filters.useragent || undefined,
        timezone: timeZone,
      }),
    staleTime: 5 * 60_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

export function useReportsOptions(filters: ReportsFilterState, timeZone: string | undefined): ReportsOptionsQueryResult {
  return useReportOptionsQuery({
    startDate: filters.startDate,
    endDate: filters.endDate,
    accountId: [...filters.accountId].sort(),
    apiKeyId: [...(filters.apiKeyId ?? [])].sort(),
    accountType: [...(filters.accountType ?? [])].sort(),
    timezone: timeZone,
  });
}

// Relax identity self-filters so selecting one source/type does not hide the rest.
export function useReportsSourceOptions(filters: ReportsFilterState, timeZone: string | undefined): ReportsOptionsQueryResult {
  return useReportOptionsQuery({
    startDate: filters.startDate,
    endDate: filters.endDate,
    accountId: [],
    accountType: [],
    apiKeyId: [...(filters.apiKeyId ?? [])].sort(),
    model: filters.model || undefined,
    useragent: filters.useragent || undefined,
    timezone: timeZone,
  });
}

function useReportOptionsQuery(scope: ReportsParams): ReportsOptionsQueryResult {
  return useQuery<ReportsOptionsResponse>({
    queryKey: ["reports-options", scope],
    enabled: isReportDateRangeValid(scope.startDate, scope.endDate),
    queryFn: () => getReportsOptions(scope),
    staleTime: 5 * 60_000,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });
}

// Raw request-log scans, so the query only runs while the card is visible and
// its result is held far longer than the cost report's.
const THREAD_IDENTITY_STALE_TIME_MS = 5 * 60_000;

export function useThreadIdentity(
  filters: Pick<ReportsFilterState, "startDate" | "endDate"> & Partial<ReportsFilterState>,
  timeZone: string | undefined,
  enabled: boolean,
): ThreadIdentityQueryResult {
  const scope = {
    startDate: filters.startDate,
    endDate: filters.endDate,
    accountId: [...(filters.accountId ?? [])].sort(),
    apiKeyId: [...(filters.apiKeyId ?? [])].sort(),
    accountType: [...(filters.accountType ?? [])].sort(),
    model: filters.model || undefined,
    useragent: filters.useragent || undefined,
    timezone: timeZone,
  };
  return useQuery({
    enabled: enabled && isReportDateRangeValid(filters.startDate, filters.endDate),
    queryKey: ["reports-thread-identity", scope],
    queryFn: () => getThreadIdentity(scope),
    staleTime: THREAD_IDENTITY_STALE_TIME_MS,
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  });
}
