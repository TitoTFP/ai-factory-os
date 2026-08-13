export type User = { id: string; email: string; name: string };
export type Factory = {
	id: string;
	name: string;
	mission: string;
	primary_objective: string;
	constraints: string[];
	autonomy: string;
	status: string;
};

export type FactoryCreateInput = {
	name: string;
	mission: string;
	primary_objective: string;
	constraints: string[];
	autonomy: "mostly_autonomous" | "fully_autonomous" | "supervised";
	provider_api_key: string;
	provider_base_url: string;
	provider_model: string;
	tool_permissions: string[];
};
export type Space = {
	id: string;
	factory_id: string;
	name: string;
	purpose: string;
	status: string;
};
export type Agent = {
	id: string;
	factory_id: string;
	space_id: string;
	name: string;
	role: string;
	objective: string;
	responsibilities: string[];
	model: string;
	status: string;
	current_task_id: string | null;
};
export type Goal = {
	id: string;
	factory_id: string;
	title: string;
	objective: string;
	criteria: string[];
	status: string;
	completion_note: string;
};
export type Task = {
	id: string;
	factory_id: string;
	goal_id: string | null;
	parent_id: string | null;
	assignee_id: string | null;
	title: string;
	description: string;
	status: string;
	inputs: Record<string, unknown>;
	outputs: Record<string, unknown>;
	retry_count: number;
	error: string;
};
export type Message = {
	id: string;
	factory_id: string;
	sender_agent_id: string | null;
	recipient_agent_id: string | null;
	message_type: string;
	subject: string;
	body: string;
	payload: Record<string, unknown>;
	status: string;
};
export type Artifact = {
	id: string;
	factory_id: string;
	space_id: string | null;
	agent_id: string | null;
	task_id: string | null;
	name: string;
	kind: string;
	content: string;
	uri: string;
	extra: Record<string, unknown>;
};
export type Event = {
	id: string;
	factory_id: string;
	actor_type: string;
	actor_id: string;
	event_type: string;
	payload: Record<string, unknown>;
};
export type Run = {
	id: string;
	factory_id: string;
	status: string;
	last_error: string;
};
export type Usage = {
	factory_id: string;
	provider: string;
	model: string;
	prompt_tokens: number;
	completion_tokens: number;
	total_tokens: number;
	cost_usd: number;
	requests: number;
};

export type Snapshot = {
	factory: Factory;
	spaces: Space[];
	agents: Agent[];
	goals: Goal[];
	tasks: Task[];
	messages: Message[];
	artifacts: Artifact[];
	events: Event[];
	run: Run | null;
	usage: Usage;
};

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
	const response = await fetch(`${API_URL}${path}`, {
		...init,
		headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
	});
	const body = await response.json().catch(() => ({}));
	if (response.status === 401) {
		localStorage.removeItem("factory_token");
	}
	if (!response.ok)
		throw new Error(body.detail ?? `Request failed (${response.status})`);
	return body as T;
}

export const api = {
	register: (input: { email: string; password: string; name: string }) =>
		request<{ access_token: string; user: User }>("/api/auth/register", {
			method: "POST",
			body: JSON.stringify(input),
		}),
	login: (input: { email: string; password: string }) =>
		request<{ access_token: string; user: User }>("/api/auth/login", {
			method: "POST",
			body: JSON.stringify(input),
		}),
	factories: (token: string) =>
		request<Factory[]>("/api/factories", {
			headers: { Authorization: `Bearer ${token}` },
		}),
	createFactory: (token: string, input: FactoryCreateInput) =>
		request<Factory>("/api/factories", {
			method: "POST",
			headers: { Authorization: `Bearer ${token}` },
			body: JSON.stringify(input),
		}),
	snapshot: (token: string, id: string) =>
		request<Snapshot>(`/api/factories/${id}`, {
			headers: { Authorization: `Bearer ${token}` },
		}),
	architect: (token: string, id: string) =>
		request(`/api/factories/${id}/architect`, {
			method: "POST",
			headers: { Authorization: `Bearer ${token}` },
		}),
	run: (
		token: string,
		id: string,
		action: "run" | "pause" | "stop" | "resume",
	) =>
		request<Run>(`/api/factories/${id}/${action}`, {
			method: "POST",
			headers: { Authorization: `Bearer ${token}` },
		}),
	organization: (token: string, id: string, input: Record<string, unknown>) =>
		request<void>(`/api/factories/${id}/organization`, {
			method: "POST",
			headers: { Authorization: `Bearer ${token}` },
			body: JSON.stringify(input),
		}),
	eventsUrl: (token: string, id: string) =>
		`${API_URL.replace(/^http/, "ws")}/api/factories/${id}/events?token=${encodeURIComponent(token)}`,
	updateCredentials: (
		token: string,
		id: string,
		input: Record<string, unknown>,
	) =>
		request<void>(`/api/factories/${id}/credentials`, {
			method: "PUT",
			headers: { Authorization: `Bearer ${token}` },
			body: JSON.stringify(input),
		}),
	oauthStart: (provider: "github" | "google") =>
		request<{ authorization_url: string; state: string }>(
			"/api/auth/oauth/start",
			{ method: "POST", body: JSON.stringify({ provider }) },
		),
};
