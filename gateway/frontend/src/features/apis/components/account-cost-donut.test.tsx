import { act, fireEvent, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { createApiKeyUsage7Day } from "@/test/mocks/factories";
import { renderWithProviders } from "@/test/utils";
import { usePrivacyStore } from "@/hooks/use-privacy";
import { useAuthStore } from "@/features/auth/hooks/use-auth";
import { ADMIN_PERMISSIONS } from "@/test/mocks/factories";

import { AccountCostDonut } from "./account-cost-donut";

vi.mock("@/components/lazy-recharts", () => ({
	Cell: () => null,
	PieChart: ({ children }: { children: ReactNode }) => <svg>{children}</svg>,
	Pie: ({
		children,
		data,
		onMouseEnter,
		onMouseLeave,
	}: {
		children: ReactNode;
		data: Array<{ id: string }>;
		onMouseEnter?: (entry: { payload: { id: string } }, index: number) => void;
		onMouseLeave?: () => void;
	}) => (
		<>
			{data.map((entry, index) => (
				<g
					key={entry.id}
					className="recharts-pie-sector"
					onMouseEnter={() => onMouseEnter?.({ payload: entry }, index)}
					onMouseLeave={() => onMouseLeave?.()}
				/>
			))}
			{children}
		</>
	),
	Sector: () => <path />,
}));

describe("AccountCostDonut", () => {
	it("highlights the matching legend row when a legend item is hovered", () => {
		const usage = createApiKeyUsage7Day({
			totalCostUsd: 0.75,
			accountCosts: [
				{ accountId: "acc-1", email: "a@example.com", costUsd: 0.45, isDeleted: false },
				{ accountId: "acc-2", email: "b@example.com", costUsd: 0.3, isDeleted: false },
			],
		});

		renderWithProviders(
			<AccountCostDonut accountCosts={usage.accountCosts} totalCostUsd={usage.totalCostUsd} />,
		);

		const legendRow = screen.getByTestId("account-cost-legend-0");
		fireEvent.mouseEnter(legendRow);
		expect(legendRow).toHaveAttribute("data-active", "true");

		fireEvent.mouseLeave(legendRow);
		expect(legendRow).toHaveAttribute("data-active", "false");
	});

	it("highlights the matching legend row when a pie slice is hovered", () => {
		const usage = createApiKeyUsage7Day({
			totalCostUsd: 0.75,
			accountCosts: [
				{ accountId: "acc-1", email: "a@example.com", costUsd: 0.45, isDeleted: false },
				{ accountId: "acc-2", email: "b@example.com", costUsd: 0.3, isDeleted: false },
			],
		});

		renderWithProviders(
			<AccountCostDonut accountCosts={usage.accountCosts} totalCostUsd={usage.totalCostUsd} />,
		);

		const slices = document.querySelectorAll(".recharts-pie-sector");
		fireEvent.mouseEnter(slices[0]!);

		expect(screen.getByTestId("account-cost-legend-0")).toHaveAttribute("data-active", "true");
	});

	it("limits the legend viewport to five visible rows before scrolling", () => {
		const usage = createApiKeyUsage7Day({
			totalCostUsd: 2.8,
			accountCosts: Array.from({ length: 6 }, (_, index) => ({
				accountId: `acc-${index}`,
				email: `user${index}@example.com`,
				costUsd: 0.4 + index * 0.05,
				isDeleted: false,
			})),
		});

		renderWithProviders(
			<AccountCostDonut accountCosts={usage.accountCosts} totalCostUsd={usage.totalCostUsd} />,
		);

		// jsdom 30 simplifies calc() during serialization; authored: calc(5 * 1.75rem + 4 * 0rem)
		expect(screen.getByTestId("account-cost-legend-list").style.maxHeight).toBe("calc(8.75rem)");
		expect(screen.getByTestId("account-cost-legend-5")).toBeInTheDocument();
	});

	it("renders the legend below the donut and omits the header total summary", () => {
		const usage = createApiKeyUsage7Day({
			totalCostUsd: 0.75,
			accountCosts: [
				{ accountId: "acc-1", email: "a@example.com", costUsd: 0.45, isDeleted: false },
				{ accountId: "acc-2", email: "b@example.com", costUsd: 0.3, isDeleted: false },
			],
		});

		renderWithProviders(
			<AccountCostDonut accountCosts={usage.accountCosts} totalCostUsd={usage.totalCostUsd} />,
		);

		expect(screen.queryByTestId("account-cost-total")).not.toBeInTheDocument();
		expect(screen.getByTestId("account-cost-legend-list")).toHaveClass("w-full");
	});

	it("blurs active account labels but not deleted account labels in privacy mode", () => {
		act(() => usePrivacyStore.setState({ blurred: true }));
		const usage = createApiKeyUsage7Day({
			totalCostUsd: 0.75,
			accountCosts: [
				{ accountId: "acc-1", email: "a@example.com", costUsd: 0.45, isDeleted: false },
				{ accountId: null, email: null, costUsd: 0.3, isDeleted: true },
			],
		});

		renderWithProviders(
			<AccountCostDonut accountCosts={usage.accountCosts} totalCostUsd={usage.totalCostUsd} />,
		);

		expect(screen.getByText("a@example.com")).toHaveClass("privacy-blur");
		expect(screen.getByText("Deleted Account")).not.toHaveClass("privacy-blur");
		act(() => usePrivacyStore.setState({ blurred: false }));
	});

	it("scrolls the hovered pie item into view in the legend list", async () => {
		const scrollIntoView = vi.fn();
		Object.defineProperty(HTMLElement.prototype, "scrollIntoView", {
			configurable: true,
			value: scrollIntoView,
		});

		const usage = createApiKeyUsage7Day({
			totalCostUsd: 5.6,
			accountCosts: Array.from({ length: 6 }, (_, index) => ({
				accountId: `acc-${index}`,
				email: `user${index}@example.com`,
				costUsd: 1 - index * 0.1,
				isDeleted: false,
			})),
		});

		renderWithProviders(
			<AccountCostDonut accountCosts={usage.accountCosts} totalCostUsd={usage.totalCostUsd} />,
		);

		const slices = document.querySelectorAll(".recharts-pie-sector");
		fireEvent.mouseEnter(slices[5]!);

		expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
	});
    it("keeps unpriced subscription source accounts visible without calling them free", () => {
        useAuthStore.setState({ permissions: ADMIN_PERMISSIONS });
        renderWithProviders(<AccountCostDonut totalCostUsd={0} accountCosts={[{
            accountId: null, email: null, costUsd: 0, isDeleted: false,
            modelSourceId: "claude", sourceName: "Claude subscription", provider: "claude", billingMode: "subscription",
            requestCount: 2, unpricedRequestCount: 2,
        }]} />);
        expect(screen.getByText("Claude subscription · claude · subscription")).toBeInTheDocument();
        expect(screen.getByText(/known · 2 unpriced/)).toBeInTheDocument();
        expect(screen.getByText(/Unknown prices are not free/)).toBeInTheDocument();
    });
    it("hides source identities in already-loaded costs after losing accounts:read", () => {
        useAuthStore.setState({ permissions: ADMIN_PERMISSIONS });
        renderWithProviders(<AccountCostDonut totalCostUsd={1} accountCosts={[{
            accountId: null, email: null, costUsd: 1, isDeleted: false,
            modelSourceId: "private-source", sourceName: "Private Claude account", provider: "claude", billingMode: "subscription",
        }]} />);
        expect(screen.getByText(/Private Claude account/)).toBeInTheDocument();
        act(() => useAuthStore.setState({ permissions: ["api_keys:read"] }));
        expect(screen.queryByText(/Private Claude account/)).not.toBeInTheDocument();
        expect(screen.queryByText(/private-source/)).not.toBeInTheDocument();
        useAuthStore.setState({ permissions: ADMIN_PERMISSIONS });
    });
});
