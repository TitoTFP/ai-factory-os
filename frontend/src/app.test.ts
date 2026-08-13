import { describe, expect, it } from "vitest";

describe("factory UI contract", () => {
	it("covers onboarding, floor, detail, lifecycle, scopes, usage, and live status", async () => {
		const source = await import("./main.tsx?raw");
		for (const marker of [
			"Factory Floor",
			"Onboarding",
			"Artifacts",
			"DetailPanel",
			"Resume factory",
			"tool_permissions",
			"cost_usd",
			"new WebSocket",
			"Autonomy",
		]) {
			expect(source.default).toContain(marker);
		}
	});

	it("exposes the API contracts used by the live floor", async () => {
		const source = await import("./api.ts?raw");
		for (const marker of [
			"usage",
			"organization",
			"updateCredentials",
			"resume",
			"/events",
		]) {
			expect(source.default).toContain(marker);
		}
	});
});
