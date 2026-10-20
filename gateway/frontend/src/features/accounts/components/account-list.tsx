import { ChevronDown, ChevronUp, Plus, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AccountListItem } from "@/features/accounts/components/account-list-item";
import { AddAccountDialog } from "@/features/accounts/components/add-account-dialog";
import { WindowsOauthHelp } from "@/features/accounts/components/windows-oauth-help";
import { ACCOUNT_CATEGORIES, sourceAccountCategory, sourceSelectionId, sourceStatus, sourceToAccountView, type AccountCategory } from "@/features/accounts/source-selection";
import type { ModelSource } from "@/features/model-sources/schemas";
import type { AccountSummary } from "@/features/accounts/schemas";
import {
  ACCOUNT_SORT_OPTIONS,
  DEFAULT_ACCOUNT_SORT_MODE,
  sortAccountsForDisplay,
  type AccountSortMode,
} from "@/features/accounts/sorting";
import { useAccountQuotaDisplayStore } from "@/hooks/use-account-quota-display";
import { cn } from "@/lib/utils";
import { formatSlug } from "@/utils/formatters";

const STATUS_FILTER_OPTIONS = ["all", "active", "paused", "rate_limited", "quota_exceeded", "reauth_required", "deactivated", "unknown", "stale", "error"];

export type AccountListProps = {
  accounts: AccountSummary[];
  sources?: ModelSource[];
  category?: AccountCategory;
  onCategoryChange?: (category: AccountCategory) => void;
  selectedAccountId: string | null;
  onSelect: (accountId: string) => void;
  onOpenImport: () => void;
  onOpenOauth: () => void;
  onOpenSource?: (provider: "openai" | "anthropic" | "claude" | "custom") => void;
  sortMode?: AccountSortMode;
  onSortModeChange?: (sortMode: AccountSortMode) => void;
  showResetCreditBadges?: boolean;
  readOnly?: boolean;
};

export function AccountList({
  accounts,
  sources = [],
  category: controlledCategory,
  onCategoryChange,
  selectedAccountId,
  onSelect,
  onOpenImport,
  onOpenOauth,
  onOpenSource,
  sortMode,
  onSortModeChange,
  showResetCreditBadges = true,
  readOnly = false,
}: AccountListProps) {
  const { t } = useTranslation();
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("all");
  const [localCategory, setLocalCategory] = useState<AccountCategory>("all");
  const category = controlledCategory ?? localCategory;
  const [helpOpen, setHelpOpen] = useState(false);
  const [chooserOpen, setChooserOpen] = useState(false);
  const quotaDisplay = useAccountQuotaDisplayStore((s) => s.quotaDisplay);
  const activeSortMode = sortMode ?? DEFAULT_ACCOUNT_SORT_MODE;

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (category !== "all" && category !== "codex") return [];
    return sortAccountsForDisplay(accounts, quotaDisplay, activeSortMode).filter((account) => {
      if (statusFilter !== "all" && account.status !== statusFilter) {
        return false;
      }
      if (!needle) {
        return true;
      }
      return (
        account.email.toLowerCase().includes(needle) ||
        (account.alias?.toLowerCase().includes(needle) ?? false) ||
        account.displayName.toLowerCase().includes(needle) ||
        account.accountId.toLowerCase().includes(needle) ||
        account.planType.toLowerCase().includes(needle)
      );
    });
  }, [accounts, category, quotaDisplay, search, statusFilter, activeSortMode]);

  const filteredSources = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return sources.filter((source) => {
      if (category !== "all" && sourceAccountCategory(source) !== category) return false;
      if (statusFilter !== "all" && sourceStatus(source) !== statusFilter) return false;
      return !needle || [source.name, source.id, source.provider, source.authType, source.billingMode, source.accountLabel, source.subscriptionType, source.accountUuid, source.organizationUuid, ...source.models.map((model) => model.model)].some((value) => value?.toLowerCase().includes(needle));
    });
  }, [sources, category, search, statusFilter]);
  const categoryCounts = { all: accounts.length + sources.length, codex: accounts.length, claude: 0, api: 0 };
  for (const source of sources) categoryCounts[sourceAccountCategory(source)] += 1;

  return (
    <div className="flex max-h-[calc(100dvh-15rem)] min-h-0 min-w-0 flex-1 flex-col space-y-3">
      <div role="group" aria-label={t("accounts.list.categoryFilterAria")} className="grid grid-cols-4 gap-1 rounded-lg bg-muted/60 p-1">
        {ACCOUNT_CATEGORIES.map((value) => (
          <Button
            key={value}
            type="button"
            variant={category === value ? "secondary" : "ghost"}
            size="sm"
            aria-label={`${t(`accounts.categories.${value}`)} ${categoryCounts[value]}`}
            aria-pressed={category === value}
            className="h-8 min-w-0 gap-1 px-1 text-xs"
            onClick={() => (onCategoryChange ?? setLocalCategory)(value)}
          >
            {t(`accounts.categories.${value}`)}
            <span className="text-[10px] tabular-nums text-muted-foreground">{categoryCounts[value]}</span>
          </Button>
        ))}
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <div className="relative min-w-0 sm:col-span-2">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground/60" aria-hidden />
          <Input
            placeholder={t("accounts.list.searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="h-8 pl-8"
          />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger
            size="sm"
            className="w-full min-w-0"
            aria-label={t("accounts.list.statusFilterAria")}
          >
            <SelectValue placeholder={t("accounts.list.statusPlaceholder")} />
          </SelectTrigger>
          <SelectContent>
            {STATUS_FILTER_OPTIONS.map((option) => (
              <SelectItem key={option} value={option}>
                {option === "all" ? t("accounts.list.allStatuses") : option === "stale" || option === "error" ? t(`sourceMonitoring.status.${option}`) : t(`accounts.statusFilters.${option}`, { defaultValue: formatSlug(option) })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={activeSortMode}
          onValueChange={(nextMode) => onSortModeChange?.(nextMode as AccountSortMode)}
        >
          <SelectTrigger
            size="sm"
            className="w-full min-w-0"
            aria-label={t("accounts.list.sortAria")}
          >
            <SelectValue placeholder={t("accounts.list.sortPlaceholder")} />
          </SelectTrigger>
          <SelectContent>
            {ACCOUNT_SORT_OPTIONS.map((option) => (
              <SelectItem key={option.value} value={option.value}>
                {t(`accounts.sort.${option.value}`, { defaultValue: option.label })}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className={cn("flex flex-wrap items-center gap-3", readOnly ? "justify-end" : "justify-between")}>
        {readOnly ? null : (
          <Button
            type="button"
            variant="link"
            size="sm"
            className="h-auto px-0 text-xs"
            onClick={() => setHelpOpen((current) => !current)}
          >
            {t("accounts.list.needHelp")}
            {helpOpen ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
          </Button>
        )}
        <Button
          type="button"
          size="sm"
          className="gap-1.5"
          disabled={readOnly}
          onClick={() => setChooserOpen(true)}
        >
          <Plus className="h-3.5 w-3.5" />
          {t("accounts.list.addAccount")}
        </Button>
      </div>

      {helpOpen && !readOnly ? <WindowsOauthHelp /> : null}

      <div
        className="flex-1 min-h-0 space-y-1 overflow-y-auto p-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        data-testid="account-list-scroll-region"
      >
        {filtered.length + filteredSources.length === 0 ? (
          <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed p-6 text-center">
            <p className="text-sm font-medium text-muted-foreground">
              {accounts.length + sources.length === 0 ? t("accounts.list.emptyTitle") : t("accounts.list.noMatches")}
            </p>
            <p className="text-xs text-muted-foreground/70">
              {accounts.length + sources.length === 0 ? t("accounts.list.emptyDescription") : t("accounts.list.adjustFilters")}
            </p>
          </div>
        ) : (
          <>
          {(["codex", "claude", "api"] as const).map((sourceCategory) => {
            const group = filteredSources.filter((source) => sourceAccountCategory(source) === sourceCategory);
            const groupAccounts = sourceCategory === "codex" ? filtered : [];
            const count = groupAccounts.length + group.length;
            return count > 0 ? (
              <section key={sourceCategory} aria-label={t(`accounts.categories.${sourceCategory}`)} className="space-y-1">
                {category === "all" ? <h3 className="px-3 pt-3 pb-1 text-xs font-medium text-muted-foreground">{t(`accounts.categories.${sourceCategory}`)} <span className="tabular-nums">{count}</span></h3> : null}
                {groupAccounts.map((account) => (
                  <AccountListItem
                    key={account.accountId}
                    account={account}
                    selected={account.accountId === selectedAccountId}
                    showAccountId={account.isEmailDuplicate === true}
                    showResetCreditBadge={showResetCreditBadges}
                    onSelect={onSelect}
                  />
                ))}
                {group.map((source) => <AccountListItem key={sourceSelectionId(source.id)} account={sourceToAccountView(source)} selected={selectedAccountId === sourceSelectionId(source.id)} onSelect={onSelect} />)}
              </section>
            ) : null;
          })}
          </>
        )}
      </div>

      {accounts.length > 0 ? (
        <p className="px-1 text-[11px] leading-snug text-muted-foreground/80">
          {t("accounts.list.statusEligibilityNote")}
        </p>
      ) : null}

      <AddAccountDialog
        open={chooserOpen && !readOnly}
        onOpenChange={setChooserOpen}
        onImport={onOpenImport}
        onAddAccount={onOpenOauth}
        onAddSource={onOpenSource}
      />
    </div>
  );
}
