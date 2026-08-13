// @vitest-environment jsdom

import React from "react";
import {
	fireEvent,
	render,
	screen,
	waitFor,
	cleanup,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./main";
import type { Factory, Snapshot } from "./api";

const { apiMock } = vi.hoisted(() => ({
	apiMock: {
		register: vi.fn(),
		login: vi.fn(),
		factories: vi.fn(),
		createFactory: vi.fn(),
		snapshot: vi.fn(),
		architect: vi.fn(),
		run: vi.fn(),
		organization: vi.fn(),
		eventsUrl: vi.fn(),
		updateCredentials: vi.fn(),
		oauthStart: vi.fn(),
	},
}));

vi.mock("./api", () => ({ api: apiMock }));

const factoryA: Factory = {
	id: "factory-a",
	name: "Alpha",
	mission: "Ship useful signals",
	primary_objective: "Publish one report",
	constraints: [],
	autonomy: "mostly_autonomous",
	status: "draft",
};
const factoryB: Factory = {
	id: "factory-b",
	name: "Beta",
	mission: "Review useful signals",
	primary_objective: "Publish two reports",
	constraints: [],
	autonomy: "supervised",
	status: "draft",
};

function snapshotFor(factory: Factory, run: Snapshot["run"] = null): Snapshot {
	return {
		factory,
		spaces: [
			{
				id: `${factory.id}-space`,
				factory_id: factory.id,
				name: "Research",
				purpose: "Find reliable signals",
				status: "active",
			},
		],
		agents: [
			{
				id: `${factory.id}-agent`,
				factory_id: factory.id,
				space_id: `${factory.id}-space`,
				name: "Scout",
				role: "Researcher",
				objective: "Find signals",
				responsibilities: ["search"],
				model: "gpt-4o-mini",
				status: "working",
				current_task_id: null,
			},
		],
		goals: [
			{
				id: `${factory.id}-goal`,
				factory_id: factory.id,
				title: "First report",
				objective: "Publish a report",
				criteria: ["artifact exists"],
				status: "in_progress",
				completion_note: "",
			},
		],
		tasks: [
			{
				id: `${factory.id}-task`,
				factory_id: factory.id,
				goal_id: `${factory.id}-goal`,
				parent_id: null,
				assignee_id: `${factory.id}-agent`,
				title: "Collect sources",
				description: "Collect the first set of sources",
				status: "todo",
				inputs: {},
				outputs: {},
				retry_count: 0,
				error: "",
			},
		],
		messages: [],
		artifacts: [],
		events: [
			{
				id: `${factory.id}-event`,
				factory_id: factory.id,
				actor_type: "user",
				actor_id: "user-1",
				event_type: "factory_created",
				payload: { name: factory.name },
			},
		],
		run,
		usage: {
			factory_id: factory.id,
			provider: "openai-compatible",
			model: "gpt-4o-mini",
			prompt_tokens: 10,
			completion_tokens: 5,
			total_tokens: 15,
			cost_usd: 0.01,
			requests: 1,
		},
	};
}

const snapshotA = snapshotFor(factoryA);
const snapshotB = snapshotFor(factoryB);

class TestWebSocket {
	static instances: TestWebSocket[] = [];
	onopen: (() => void) | null = null;
	onmessage: (() => void) | null = null;
	onerror: (() => void) | null = null;
	onclose: (() => void) | null = null;
	close = vi.fn();

	constructor(readonly url: string) {
		TestWebSocket.instances.push(this);
	}

	open() {
		this.onopen?.();
	}

	message() {
		this.onmessage?.();
	}

	fail() {
		this.onerror?.();
	}

	end() {
		this.onclose?.();
	}
}

function resetApi() {
	for (const mock of Object.values(apiMock)) mock.mockReset();
	apiMock.eventsUrl.mockReturnValue("ws://factory.test/events");
	apiMock.factories.mockResolvedValue([]);
	apiMock.snapshot.mockResolvedValue(snapshotA);
	apiMock.createFactory.mockResolvedValue(factoryB);
	apiMock.architect.mockResolvedValue({});
	apiMock.run.mockResolvedValue({
		id: "run-1",
		factory_id: factoryA.id,
		status: "running",
		last_error: "",
	});
}

function renderFactoryApp(factories: Factory[] = [factoryA]) {
	localStorage.setItem("factory_token", "token-1");
	apiMock.factories.mockResolvedValue(factories);
	apiMock.snapshot.mockImplementation(async (_token: string, id: string) =>
		id === factoryB.id ? snapshotB : snapshotA,
	);
	return render(React.createElement(App));
}

function fillOnboardingForm() {
	fireEvent.change(screen.getByLabelText("Factory name"), {
		target: { value: "Beta" },
	});
	fireEvent.change(screen.getByLabelText("What does this factory do?"), {
		target: { value: "Review useful signals" },
	});
	fireEvent.change(screen.getByLabelText("Primary objective"), {
		target: { value: "Publish two reports" },
	});
	fireEvent.change(screen.getByLabelText("Autonomy"), {
		target: { value: "supervised" },
	});
	fireEvent.change(screen.getByLabelText("API key"), {
		target: { value: "secret-key" },
	});
}

beforeEach(() => {
	resetApi();
	localStorage.clear();
	TestWebSocket.instances = [];
	vi.stubGlobal("WebSocket", TestWebSocket);
});

afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
	localStorage.clear();
});

describe("factory UI interactions", () => {
	it("submits onboarding with scoped tools and starts the created architecture", async () => {
		apiMock.snapshot.mockResolvedValue(snapshotB);
		localStorage.setItem("factory_token", "token-1");
		render(React.createElement(App));

		expect(
			await screen.findByRole("heading", { name: "Factory brief" }),
		).not.toBeNull();
		fillOnboardingForm();
		fireEvent.click(screen.getByLabelText("web_fetch"));
		fireEvent.change(
			screen.getByPlaceholderText("Budget, market, principles…"),
			{
				target: { value: "Keep the scope lean\n" },
			},
		);
		fireEvent.click(
			screen.getByRole("button", { name: /Create factory architecture/ }),
		);

		await waitFor(() => expect(apiMock.createFactory).toHaveBeenCalledTimes(1));
		expect(apiMock.createFactory).toHaveBeenCalledWith(
			"token-1",
			expect.objectContaining({
				name: "Beta",
				constraints: ["Keep the scope lean"],
				autonomy: "supervised",
				tool_permissions: ["workspace", "http"],
				provider_api_key: "secret-key",
			}),
		);
		expect(apiMock.architect).toHaveBeenCalledWith("token-1", factoryB.id);
		expect(
			await screen.findByRole("heading", { name: "Factory Floor" }),
		).not.toBeNull();
	});

	it("counts only unread messages in the navigation badge", async () => {
		const withMessages = snapshotFor(factoryA);
		withMessages.messages = [
			{
				id: "read-message",
				factory_id: factoryA.id,
				sender_agent_id: null,
				recipient_agent_id: null,
				message_type: "MESSAGE",
				subject: "Read",
				body: "read",
				payload: {},
				status: "read",
			},
			{
				id: "new-message",
				factory_id: factoryA.id,
				sender_agent_id: null,
				recipient_agent_id: null,
				message_type: "MESSAGE",
				subject: "New",
				body: "new",
				payload: {},
				status: "delivered",
			},
		];
		apiMock.snapshot.mockResolvedValue(withMessages);
		localStorage.setItem("factory_token", "token-1");
		apiMock.factories.mockResolvedValue([factoryA]);
		render(React.createElement(App));
		await screen.findByRole("heading", { name: "Factory Floor" });
		expect(screen.getByLabelText("Unread messages").textContent).toBe("1");
	});

	it("switches factories from the existing factory menu", async () => {
		renderFactoryApp([factoryA, factoryB]);
		await screen.findByRole("heading", { name: "Factory Floor" });

		fireEvent.click(screen.getByRole("button", { name: /Switch factory/ }));
		fireEvent.click(screen.getByRole("menuitem", { name: "Beta" }));

		await waitFor(() =>
			expect(screen.getByText(factoryB.mission)).not.toBeNull(),
		);
		expect(apiMock.snapshot).toHaveBeenCalledWith("token-1", factoryB.id);
	});

	it("opens factory creation from the switcher and returns to the new floor", async () => {
		renderFactoryApp();
		await screen.findByRole("heading", { name: "Factory Floor" });
		fireEvent.click(screen.getByRole("button", { name: /Switch factory/ }));
		fireEvent.click(screen.getByRole("button", { name: /Create factory/ }));

		expect(
			await screen.findByRole("heading", { name: "Factory brief" }),
		).not.toBeNull();
		fillOnboardingForm();
		fireEvent.click(
			screen.getByRole("button", { name: /Create another factory/ }),
		);

		await waitFor(() =>
			expect(apiMock.architect).toHaveBeenCalledWith("token-1", factoryB.id),
		);
		expect(await screen.findByText(factoryB.mission)).not.toBeNull();
	});

	it("retries architecture without creating a duplicate factory", async () => {
		localStorage.setItem("factory_token", "token-1");
		apiMock.factories.mockResolvedValue([factoryA]);
		apiMock.createFactory.mockResolvedValue(factoryB);
		apiMock.architect.mockRejectedValueOnce(new Error("provider unavailable"));
		render(React.createElement(App));
		await screen.findByRole("heading", { name: "Factory Floor" });
		fireEvent.click(screen.getByRole("button", { name: /Switch factory/ }));
		fireEvent.click(screen.getByRole("button", { name: /Create factory/ }));
		fillOnboardingForm();
		fireEvent.click(screen.getByRole("button", { name: /Create another factory/ }));
		await screen.findByRole("alert");
		expect(apiMock.createFactory).toHaveBeenCalledTimes(1);
		fireEvent.click(screen.getByRole("button", { name: /Retry architecture/ }));
		await waitFor(() => expect(apiMock.architect).toHaveBeenCalledTimes(2));
		expect(apiMock.createFactory).toHaveBeenCalledTimes(1);
	});

	it("opens and closes a detail panel from a floor space", async () => {
		renderFactoryApp();
		await screen.findByRole("heading", { name: "Factory Floor" });
		fireEvent.click(screen.getByRole("button", { name: /Research/ }));

		const details = await screen.findByRole("dialog", {
			name: "Record details",
		});
		expect(details.textContent).toContain('"name": "Research"');
		fireEvent.click(screen.getByRole("button", { name: "Close" }));
		expect(screen.queryByRole("dialog", { name: "Record details" })).toBeNull();
	});

	it("runs the factory lifecycle through start, pause, and resume", async () => {
		let current = snapshotA;
		apiMock.snapshot.mockImplementation(async () => current);
		apiMock.run.mockImplementation(
			async (_token: string, _id: string, action: string) => {
				const status = action === "pause" ? "paused" : "running";
				current = snapshotFor(
					{ ...factoryA, status },
					{ id: "run-1", factory_id: factoryA.id, status, last_error: "" },
				);
				return current.run;
			},
		);
		renderFactoryApp();
		await screen.findByRole("heading", { name: "Factory Floor" });
		apiMock.snapshot.mockImplementation(async () => current);

		fireEvent.click(screen.getByRole("button", { name: /Start factory/ }));
		await screen.findByRole("button", { name: "Pause" });
		fireEvent.click(screen.getByRole("button", { name: "Pause" }));
		await screen.findByRole("button", { name: /Resume factory/ });
		fireEvent.click(screen.getByRole("button", { name: /Resume factory/ }));
		await screen.findByRole("button", { name: "Pause" });

		expect(apiMock.run).toHaveBeenNthCalledWith(
			1,
			"token-1",
			factoryA.id,
			"run",
		);
		expect(apiMock.run).toHaveBeenNthCalledWith(
			2,
			"token-1",
			factoryA.id,
			"pause",
		);
		expect(apiMock.run).toHaveBeenNthCalledWith(
			3,
			"token-1",
			factoryA.id,
			"resume",
		);
	});

	it("does not refresh after a stale socket closes", async () => {
		renderFactoryApp();
		await screen.findByRole("heading", { name: "Factory Floor" });
		const socket = TestWebSocket.instances[0];
		const callsBefore = apiMock.factories.mock.calls.length;
		fireEvent.click(screen.getByRole("button", { name: /Switch factory/ }));
		fireEvent.click(screen.getByRole("menuitem", { name: "Alpha" }));
		socket.end();
		await new Promise((resolve) => setTimeout(resolve, 10));
		expect(apiMock.factories.mock.calls.length).toBe(callsBefore);
	});

	it("reports live WebSocket status and refreshes the floor on an event", async () => {
		let current = snapshotA;
		apiMock.snapshot.mockImplementation(async () => current);
		renderFactoryApp();
		await screen.findByRole("heading", { name: "Factory Floor" });
		apiMock.snapshot.mockImplementation(async () => current);
		const socket = TestWebSocket.instances[0];
		expect(socket.url).toBe("ws://factory.test/events");

		socket.open();
		await waitFor(() =>
			expect(screen.getByText(/Runtime live/)).not.toBeNull(),
		);
		current = snapshotFor({ ...factoryA, mission: "Live mission update" });
		socket.message();
		await screen.findByText("Live mission update");

		socket.fail();
		await waitFor(() =>
			expect(screen.getByText(/Runtime offline/)).not.toBeNull(),
		);
	});
});
