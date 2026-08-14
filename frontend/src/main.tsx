import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
	api,
	type Factory,
	type FactoryCreateInput,
	type Snapshot,
} from "./api";
import "./styles.css";

type Screen =
	| "floor"
	| "spaces"
	| "agents"
	| "goals"
	| "tasks"
	| "messages"
	| "artifacts"
	| "activity";

const nav: Array<{ id: Screen; label: string; icon: string }> = [
	{ id: "floor", label: "Factory Floor", icon: "⌘" },
	{ id: "spaces", label: "Spaces", icon: "▦" },
	{ id: "agents", label: "Agents", icon: "◎" },
	{ id: "goals", label: "Goals", icon: "◈" },
	{ id: "tasks", label: "Tasks", icon: "✓" },
	{ id: "messages", label: "Messages", icon: "↗" },
	{ id: "artifacts", label: "Artifacts", icon: "▤" },
	{ id: "activity", label: "Activity", icon: "≡" },
];

function initialToken(): string {
	const hash = new URLSearchParams(window.location.hash.slice(1));
	const token = hash.get("access_token");
	if (token) {
		localStorage.setItem("factory_token", token);
		window.history.replaceState({}, document.title, window.location.pathname);
		return token;
	}
	return localStorage.getItem("factory_token") ?? "";
}

export function App() {
	const [token, setToken] = useState(initialToken);
	const [factories, setFactories] = useState<Factory[]>([]);
	const [factory, setFactory] = useState<Factory | null>(null);
	const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
	const [screen, setScreen] = useState<Screen>("floor");
	const [error, setError] = useState("");
	const [connection, setConnection] = useState<
		"connecting" | "live" | "offline"
	>("connecting");
	const [selected, setSelected] = useState<Record<string, unknown> | null>(
		null,
	);
	const [factoryMenuOpen, setFactoryMenuOpen] = useState(false);
	const [creatingFactory, setCreatingFactory] = useState(false);
	const [lifecycleBusy, setLifecycleBusy] = useState(false);
	const [draftFactory, setDraftFactory] = useState<Factory | null>(null);
	const [loading, setLoading] = useState(Boolean(token));
	const loadGeneration = useRef(0);

	const handleLoadError = (errorValue: Error) => {
		if (!localStorage.getItem("factory_token")) {
			setToken("");
			setFactories([]);
			setFactory(null);
			setSnapshot(null);
		} else {
			setError(errorValue.message);
		}
	};

	const loadFactories = async (
		authToken = token,
		preferredFactoryId?: string,
	) => {
		const generation = ++loadGeneration.current;
		setLoading(true);
		try {
			const availableFactories = await api.factories(authToken);
			if (generation !== loadGeneration.current) return;
			setFactories(availableFactories);
			const nextFactory =
				availableFactories.find(
					(item) => item.id === (preferredFactoryId ?? factory?.id),
				) ?? availableFactories[0];
			if (!nextFactory) {
				setFactory(null);
				setSnapshot(null);
				return;
			}
			const nextSnapshot = await api.snapshot(authToken, nextFactory.id);
			if (generation !== loadGeneration.current) return;
			setFactory(nextFactory);
			setSnapshot(nextSnapshot);
		} finally {
			if (generation === loadGeneration.current) setLoading(false);
		}
	};

	const selectFactory = async (nextFactory: Factory) => {
		setFactoryMenuOpen(false);
		setCreatingFactory(false);
		setDraftFactory(null);
		setError("");
		setFactory(nextFactory);
		setSnapshot(null);
		const selectedId = nextFactory.id;
		const generation = ++loadGeneration.current;
		try {
			const nextSnapshot = await api.snapshot(token, selectedId);
			if (generation !== loadGeneration.current) return;
			setFactory(nextFactory);
			setSnapshot(nextSnapshot);
		} catch (e) {
			if (generation === loadGeneration.current) setError((e as Error).message);
		}
	};

	const handleCreated = async (created: Factory) => {
		const generation = ++loadGeneration.current;
		setFactoryMenuOpen(false);
		setCreatingFactory(false);
		setDraftFactory(null);
		setFactories((current) => [
			created,
			...current.filter((item) => item.id !== created.id),
		]);
		setFactory(created);
		setSnapshot(null);
		try {
			const nextSnapshot = await api.snapshot(token, created.id);
			if (generation !== loadGeneration.current) return;
			setSnapshot(nextSnapshot);
		} catch (e) {
			if (generation === loadGeneration.current) setError((e as Error).message);
		}
	};

	useEffect(() => {
		if (!token) return;
		loadFactories(token).catch(handleLoadError);
	}, [token]);

	useEffect(() => {
		if (!token || !factory || creatingFactory) return;
		const selectedId = factory.id;
		let current = true;
		let reconnectAttempt = 0;
		let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
		let refreshTimer: ReturnType<typeof setTimeout> | undefined;
		let socket: WebSocket;
		const connect = () => {
			if (!current) return;
			socket = new WebSocket(api.eventsUrl(token, selectedId));
			setConnection("connecting");
			socket.onopen = () => {
				if (!current) return;
				reconnectAttempt = 0;
				setConnection("live");
			};
			socket.onmessage = () => {
				if (refreshTimer) return;
				refreshTimer = setTimeout(() => {
					refreshTimer = undefined;
					if (current) loadFactories(token, selectedId).catch(handleLoadError);
				}, 50);
			};
			socket.onerror = () => {
				if (current) setConnection("offline");
			};
			socket.onclose = () => {
				if (!current) return;
				setConnection("offline");
				const delay = Math.min(10_000, 1_000 * 2 ** reconnectAttempt++);
				reconnectTimer = setTimeout(connect, delay);
			};
		};
		connect();
		return () => {
			current = false;
			if (reconnectTimer) clearTimeout(reconnectTimer);
			if (refreshTimer) clearTimeout(refreshTimer);
			socket.close();
		};
	}, [token, factory?.id, creatingFactory]);

	if (!token)
		return (
			<Auth
				onAuth={(value) => {
					localStorage.setItem("factory_token", value);
					setToken(value);
				}}
			/>
		);
	if (loading && !factory)
		return <div className="loading">Loading factories…</div>;
	if (!factory || creatingFactory)
		return (
			<Onboarding
				token={token}
				onCreated={handleCreated}
				onCancel={
					factory
						? () => {
								setCreatingFactory(false);
								setDraftFactory(null);
							}
						: undefined
				}
				error={error}
				isCreating={Boolean(factory)}
				draftFactory={draftFactory}
				onDraft={(draft) => setDraftFactory(draft)}
			/>
		);

	const refresh = () =>
		api
			.snapshot(token, factory.id)
			.then(setSnapshot)
			.catch((e: Error) => setError(e.message));
	const invoke = async (action: () => Promise<unknown>) => {
		if (lifecycleBusy) return;
		setLifecycleBusy(true);
		setError("");
		try {
			await action();
			await refresh();
		} catch (e) {
			setError((e as Error).message);
		} finally {
			setLifecycleBusy(false);
		}
	};
	return (
		<div className="app-shell">
			<aside className="sidebar">
				<div className="brand">
					<span className="brand-mark">✦</span>
					<span>
						FACTORY<span className="muted">OS</span>
					</span>
				</div>
				<div style={{ position: "relative" }}>
					<button
						type="button"
						className="factory-switcher"
						aria-haspopup="menu"
						aria-expanded={factoryMenuOpen}
						aria-label={`Switch factory (current: ${factory.name})`}
						onClick={() => setFactoryMenuOpen((open) => !open)}
					>
						<span className="pulse" /> <span>{factory.name}</span>
						<span className="chevron">⌄</span>
					</button>
					{factoryMenuOpen && (
						<div
							className="factory-menu panel"
							role="menu"
							aria-label="Factories"
							style={{
								position: "absolute",
								top: "calc(100% + 4px)",
								left: 0,
								width: "min(218px, calc(100vw - 32px))",
								padding: 7,
								zIndex: 3,
								display: "grid",
								gap: 3,
							}}
						>
							{factories.map((item) => (
								<button
									type="button"
									className="nav-item"
									role="menuitem"
									aria-current={item.id === factory.id ? "true" : undefined}
									aria-label={item.name}
									key={item.id}
									onClick={() => selectFactory(item)}
								>
									{item.name}
								</button>
							))}
							<button
								type="button"
								className="secondary full"
								onClick={() => {
									setFactoryMenuOpen(false);
									setCreatingFactory(true);
								}}
							>
								＋ Create factory
							</button>
						</div>
					)}
				</div>
				<nav>
					{nav.map((item) => (
						<button
							className={screen === item.id ? "nav-item active" : "nav-item"}
							aria-label={item.label}
							key={item.id}
							onClick={() => setScreen(item.id)}
						>
							<span className="nav-icon">{item.icon}</span>
							{item.label}
							{item.id === "messages" &&
							snapshot?.messages.filter((message) => message.status !== "read")
								.length ? (
								<span className="nav-count" aria-label="Unread messages">
									{
										snapshot.messages.filter(
											(message) => message.status !== "read",
										).length
									}
								</span>
							) : null}
						</button>
					))}
				</nav>
				<div className="sidebar-bottom">
					<div className="live-indicator" role="status" aria-live="polite">
						<span className="pulse" /> Runtime {connection}
					</div>
					<button
						className="logout"
						onClick={() => {
							localStorage.removeItem("factory_token");
							setToken("");
						}}
					>
						Sign out
					</button>
				</div>
			</aside>
			<main className="main-content">
				<header className="topbar">
					<div>
						<div className="eyebrow">
							OPERATING SYSTEM / {screen.toUpperCase()}
						</div>
						<h1>
							{screen === "floor"
								? "Factory Floor"
								: nav.find((x) => x.id === screen)?.label}
						</h1>
					</div>
					<div className="top-actions">
						<span className={`connection ${connection}`} role="status">
							<span className="pulse" />{" "}
							{connection === "live" ? "Live" : connection}
						</span>
						<button className="avatar" type="button">
							{factory.name.slice(0, 1).toUpperCase()}
						</button>
					</div>
				</header>
				{error && (
					<div className="error-banner" role="alert" aria-live="polite">
						{error}
						<button
							type="button"
							aria-label="Dismiss error"
							onClick={() => setError("")}
						>
							×
						</button>
					</div>
				)}
				{screen === "floor" ? (
					<Floor
						snapshot={snapshot}
						onAction={invoke}
						onSelect={setSelected}
						busy={lifecycleBusy}
					/>
				) : (
					<Collection
						screen={screen}
						snapshot={snapshot}
						onSelect={setSelected}
					/>
				)}
				{selected && (
					<DetailPanel item={selected} onClose={() => setSelected(null)} />
				)}
			</main>
		</div>
	);
}

function Auth({ onAuth }: { onAuth: (token: string) => void }) {
	const [mode, setMode] = useState<"login" | "register">("register");
	const [email, setEmail] = useState("");
	const [password, setPassword] = useState("");
	const [name, setName] = useState("");
	const [error, setError] = useState("");
	const submit = async (event: React.FormEvent) => {
		event.preventDefault();
		try {
			const result =
				mode === "register"
					? await api.register({ email, password, name })
					: await api.login({ email, password });
			onAuth(result.access_token);
		} catch (e) {
			setError((e as Error).message);
		}
	};
	const oauth = async (provider: "github" | "google") => {
		try {
			const result = await api.oauthStart(provider);
			const url = new URL(result.authorization_url);
			if (!["github.com", "accounts.google.com"].includes(url.hostname))
				throw new Error("OAuth provider URL is not allowed");
			window.location.assign(url.href);
		} catch (e) {
			setError((e as Error).message);
		}
	};
	return (
		<div className="auth-page">
			<div className="auth-card">
				<div className="brand large">
					<span className="brand-mark">✦</span>
					<span>
						FACTORY<span className="muted">OS</span>
					</span>
				</div>
				<p className="kicker">A WORKSPACE FOR AUTONOMOUS TEAMS</p>
				<h1>{mode === "register" ? "Create your account" : "Welcome back"}</h1>
				<p className="subtle">
					Build a living factory around the work that matters.
				</p>
				<div className="oauth-row">
					<button className="secondary" onClick={() => oauth("github")}>
						GitHub
					</button>
					<button className="secondary" onClick={() => oauth("google")}>
						Google
					</button>
				</div>
				<div className="or">
					<span>or use email</span>
				</div>
				<form onSubmit={submit}>
					{mode === "register" && (
						<label>
							Your name
							<input
								value={name}
								onChange={(e) => setName(e.target.value)}
								placeholder="Alex Morgan"
								required
							/>
						</label>
					)}
					<label>
						Email
						<input
							type="email"
							value={email}
							onChange={(e) => setEmail(e.target.value)}
							placeholder="you@company.com"
							required
						/>
					</label>
					<label>
						Password
						<input
							type="password"
							value={password}
							onChange={(e) => setPassword(e.target.value)}
							placeholder="At least 8 characters"
							minLength={8}
							required
						/>
					</label>
					{error && (
						<p className="form-error" role="alert">
							{error}
						</p>
					)}
					<button className="primary full" type="submit">
						{mode === "register" ? "Enter the factory" : "Sign in"}{" "}
						<span>→</span>
					</button>
				</form>
				<button
					className="switch-auth"
					onClick={() => setMode(mode === "register" ? "login" : "register")}
				>
					{mode === "register"
						? "Already have an account? Sign in"
						: "New here? Create an account"}
				</button>
			</div>
		</div>
	);
}

function Onboarding({
	token,
	onCreated,
	onCancel,
	error,
	isCreating = false,
	draftFactory,
	onDraft,
}: {
	token: string;
	onCreated: (factory: Factory) => void | Promise<void>;
	onCancel?: () => void;
	error: string;
	isCreating?: boolean;
	draftFactory?: Factory | null;
	onDraft: (factory: Factory | null) => void;
}) {
	const [form, setForm] = useState<FactoryCreateInput>({
		name: "",
		mission: "",
		primary_objective: "",
		constraints: [],
		autonomy: "mostly_autonomous",
		tool_permissions: ["workspace", "web_fetch", "http"],
		provider_api_key: "",
		provider_base_url: "https://api.openai.com/v1",
		provider_model: "gpt-4o-mini",
	});
	const [busy, setBusy] = useState(false);
	const [localError, setLocalError] = useState("");
	const submit = async (event: React.FormEvent) => {
		event.preventDefault();
		setBusy(true);
		setLocalError("");
		try {
			const created = draftFactory ?? (await api.createFactory(token, form));
			onDraft(created);
			await api.architect(token, created.id);
			onCreated(created);
		} catch (e) {
			setLocalError((e as Error).message);
		} finally {
			setBusy(false);
		}
	};
	return (
		<div className="onboarding">
			<div className="onboard-copy">
				<div className="eyebrow">FIRST RUN / FACTORY ARCHITECT</div>
				<h1>
					Give your factory
					<br />
					<em>a reason to exist.</em>
				</h1>
				<p>
					Define the mission. The Architect will shape the spaces, people, and
					goals that make it real.
				</p>
				<div className="onboard-steps">
					<span className="done">01 Mission</span>
					<span>02 Architecture</span>
					<span>03 Launch</span>
				</div>
			</div>
			<form className="onboard-form" onSubmit={submit}>
				<div className="form-heading">
					<span className="step-badge">01</span>
					<div>
						<h2>Factory brief</h2>
						<p>Start with the signal. Refine the system later.</p>
					</div>
				</div>
				<label>
					Factory name
					<input
						value={form.name}
						onChange={(e) => setForm({ ...form, name: e.target.value })}
						placeholder="e.g. MemeForge"
						required
					/>
				</label>
				<label>
					What does this factory do?
					<textarea
						value={form.mission}
						onChange={(e) => setForm({ ...form, mission: e.target.value })}
						placeholder="Describe the mission in one or two sentences."
						required
					/>
				</label>
				<label>
					Primary objective
					<textarea
						value={form.primary_objective}
						onChange={(e) =>
							setForm({ ...form, primary_objective: e.target.value })
						}
						placeholder="What measurable outcome should the factory pursue?"
						required
					/>
				</label>
				<label>
					Autonomy
					<select
						value={form.autonomy}
						onChange={(e) =>
							setForm({
								...form,
								autonomy: e.target.value as FactoryCreateInput["autonomy"],
							})
						}
					>
						<option value="mostly_autonomous">Mostly autonomous</option>
						<option value="fully_autonomous">Fully autonomous</option>
						<option value="supervised">Supervised</option>
					</select>
				</label>
				<label>
					Tool scope
					<div className="scope-grid">
						{["workspace", "web_fetch", "http"].map((tool) => (
							<label key={tool}>
								<input
									type="checkbox"
									checked={form.tool_permissions.includes(tool)}
									onChange={(e) =>
										setForm({
											...form,
											tool_permissions: e.target.checked
												? [...form.tool_permissions, tool]
												: form.tool_permissions.filter(
														(value) => value !== tool,
													),
										})
									}
								/>{" "}
								{tool}
							</label>
						))}
					</div>
				</label>
				<label>
					Constraints <span className="label-hint">one per line</span>
					<textarea
						value={form.constraints.join("\n")}
						onChange={(e) =>
							setForm({
								...form,
								constraints: e.target.value
									.split("\n")
									.map((x) => x.trim())
									.filter(Boolean),
							})
						}
						placeholder="Budget, market, principles…"
					/>
				</label>
				<div className="form-divider" />
				<div className="form-heading compact">
					<span className="step-badge">AI</span>
					<div>
						<h2>Connect your model</h2>
						<p>OpenAI-compatible endpoint for the Architect.</p>
					</div>
				</div>
				<label>
					API key
					<input
						type="password"
						value={form.provider_api_key}
						onChange={(e) =>
							setForm({ ...form, provider_api_key: e.target.value })
						}
						placeholder="sk-…"
						required
					/>
				</label>
				<div className="two-col">
					<label>
						Base URL
						<input
							value={form.provider_base_url}
							onChange={(e) =>
								setForm({ ...form, provider_base_url: e.target.value })
							}
						/>
					</label>
					<label>
						Model
						<input
							value={form.provider_model}
							onChange={(e) =>
								setForm({ ...form, provider_model: e.target.value })
							}
						/>
					</label>
				</div>
				{(error || localError) && (
					<p className="form-error" role="alert">
						{error || localError}
					</p>
				)}
				<div className="onboard-actions">
					{onCancel && (
						<button
							type="button"
							className="secondary"
							onClick={onCancel}
							disabled={busy}
						>
							Cancel
						</button>
					)}
					<button className="primary full" disabled={busy}>
						{busy
							? "Creating architecture…"
							: draftFactory
								? "Retry architecture"
								: isCreating
									? "Create another factory"
									: "Create factory architecture"}{" "}
						<span>→</span>
					</button>
				</div>
			</form>
		</div>
	);
}

function Floor({
	snapshot,
	onAction,
	onSelect,
	busy,
}: {
	snapshot: Snapshot | null;
	onAction: (action: () => Promise<unknown>) => void;
	onSelect: (item: Record<string, unknown>) => void;
	busy: boolean;
}) {
	if (!snapshot) return <div className="loading">Loading factory state…</div>;
	const running = snapshot.run?.status === "running";
	const paused = snapshot.run?.status === "paused";
	const failed = Boolean(snapshot.run?.last_error);
	const token = localStorage.getItem("factory_token") ?? "";
	return (
		<div className="content-wrap">
			<section className="hero-row">
				<div>
					<div className="eyebrow">MISSION CONTROL / OVERVIEW</div>
					<h2>{snapshot.factory.mission}</h2>
					<p className="subtle max-copy">
						{snapshot.factory.primary_objective}
					</p>
				</div>
				<div className="run-controls">
					<span className={`status-pill ${running ? "working" : ""}`}>
						<span className="pulse" />{" "}
						{running ? "Running" : snapshot.factory.status}
					</span>
					{running ? (
						<>
							<button
								className="secondary"
								disabled={busy}
								onClick={() =>
									onAction(() => api.run(token, snapshot.factory.id, "pause"))
								}
							>
								Pause
							</button>
							<button
								className="secondary"
								disabled={busy}
								onClick={() => {
									if (window.confirm("Stop this factory run?")) {
										onAction(() => api.run(token, snapshot.factory.id, "stop"));
									}
								}}
							>
								Stop
							</button>
						</>
					) : (
						<button
							className="primary"
							disabled={busy}
							onClick={() =>
								onAction(() =>
									api.run(
										token,
										snapshot.factory.id,
										paused ? "resume" : "run",
									),
								)
							}
						>
							{paused ? "Resume factory" : "Start factory"} <span>→</span>
						</button>
					)}
				</div>
			</section>
			{failed && (
				<div className="error-banner" role="alert">
					<strong>Runtime error:</strong> {snapshot.run?.last_error}
					<button
						type="button"
						disabled={busy}
						onClick={() =>
							onAction(() => api.run(token, snapshot.factory.id, "resume"))
						}
					>
						Retry
					</button>
				</div>
			)}
			<section className="metric-grid">
				<Metric
					label="Active agents"
					value={snapshot.agents.filter((x) => x.status === "working").length}
					detail={`${snapshot.agents.length} total`}
				/>
				<Metric
					label="Open tasks"
					value={snapshot.tasks.filter((x) => x.status !== "done").length}
					detail={`${snapshot.tasks.filter((x) => x.status === "done").length} completed`}
				/>
				<Metric
					label="Artifacts"
					value={snapshot.artifacts.length}
					detail="Across all spaces"
				/>
				<Metric
					label="Events"
					value={snapshot.events.length}
					detail="Audited activity"
				/>
				<Metric
					label="Estimated cost"
					value={snapshot.usage.cost_usd}
					detail={`${snapshot.usage.total_tokens} tokens / ${snapshot.usage.requests} requests`}
				/>
			</section>
			{snapshot.improvement_cycles.length > 0 && (
				<section className="panel cycle-panel">
					<div className="panel-heading">
						<div>
							<span className="eyebrow">FACTORY ZERO / EVOLUTION</span>
							<h3>Self-improvement cycles</h3>
						</div>
						<span className="view-all">{snapshot.repositories.length} repo{snapshot.repositories.length === 1 ? "" : "s"}</span>
					</div>
					{snapshot.improvement_cycles.slice(0, 3).map((cycle) => (
						<div className="goal-row" key={cycle.id}>
							<div className="goal-check">{cycle.status === "completed" ? "✓" : "↻"}</div>
							<div className="goal-copy">
								<strong>{cycle.phase.replaceAll("_", " ")}</strong>
								<p>{cycle.objective}</p>
							</div>
							{cycle.pr_url ? <button type="button" className="status-text completed" onClick={() => window.open(cycle.pr_url ?? "", "_blank", "noopener,noreferrer")}>PR #{cycle.pr_number}</button> : <span className={`status-text ${cycle.status}`}>{cycle.status}</span>}
						</div>
					))}
				</section>
			)}
			<div className="floor-grid">
				<section className="panel floor-map">
					<div className="panel-heading">
						<div>
							<span className="eyebrow">SPACES / LIVE MAP</span>
							<h3>Factory floor</h3>
						</div>
						<span className="live-label">
							<span className="pulse" /> live
						</span>
					</div>
					<div className="space-list">
						{snapshot.spaces.map((space, index) => (
							<button
								type="button"
								className="space-card"
								key={space.id}
								onClick={() =>
									onSelect(space as unknown as Record<string, unknown>)
								}
							>
								<div className={`space-orb orb-${index % 4}`}>
									<span>{space.name.slice(0, 1)}</span>
								</div>
								<div className="space-info">
									<div className="space-title">
										<strong>{space.name}</strong>
										<span>
											{
												snapshot.agents.filter((a) => a.space_id === space.id)
													.length
											}{" "}
											agents
										</span>
									</div>
									<p>{space.purpose}</p>
									<div className="agent-dots">
										{snapshot.agents
											.filter((a) => a.space_id === space.id)
											.map((agent) => (
												<span
													className={`agent-chip ${agent.status}`}
													key={agent.id}
												>
													<i />
													{agent.name}
												</span>
											))}
									</div>
								</div>
								<span className="space-arrow">↗</span>
							</button>
						))}
					</div>
				</section>
				<section className="panel activity-panel">
					<div className="panel-heading">
						<div>
							<span className="eyebrow">EVENT STREAM</span>
							<h3>Recent activity</h3>
						</div>
						<span className="view-all">View all →</span>
					</div>
					<div className="event-list">
						{snapshot.events.slice(0, 7).map((event) => (
							<div className="event-row" key={event.id}>
								<div className="event-icon">
									{event.event_type === "tool_called"
										? "↗"
										: event.event_type.includes("task")
											? "✓"
											: "✦"}
								</div>
								<div>
									<strong>{event.event_type.replaceAll("_", " ")}</strong>
									<p>{JSON.stringify(event.payload)}</p>
								</div>
								<time>{event.event_type}</time>
							</div>
						))}
					</div>
				</section>
			</div>
			<section className="panel goal-panel">
				<div className="panel-heading">
					<div>
						<span className="eyebrow">GOALS / PROGRESS</span>
						<h3>Factory objectives</h3>
					</div>
				</div>
				{snapshot.goals.map((goal) => (
					<div className="goal-row" key={goal.id}>
						<div className="goal-check">
							{goal.status === "completed" ? "✓" : "○"}
						</div>
						<div className="goal-copy">
							<strong>{goal.title}</strong>
							<p>{goal.objective}</p>
						</div>
						<span className={`status-text ${goal.status}`}>{goal.status}</span>
					</div>
				))}
			</section>
		</div>
	);
}

function Metric({
	label,
	value,
	detail,
}: {
	label: string;
	value: number;
	detail: string;
}) {
	return (
		<div className="metric">
			<span>{label}</span>
			<strong>{value}</strong>
			<small>{detail}</small>
		</div>
	);
}

function Collection({
	screen,
	snapshot,
	onSelect,
}: {
	screen: Screen;
	snapshot: Snapshot | null;
	onSelect: (item: Record<string, unknown>) => void;
}) {
	if (!snapshot) return <div className="loading">Loading…</div>;
	const data =
		screen === "spaces"
			? snapshot.spaces
			: screen === "agents"
				? snapshot.agents
				: screen === "goals"
					? snapshot.goals
					: screen === "tasks"
						? snapshot.tasks
						: screen === "messages"
							? snapshot.messages
							: screen === "artifacts"
								? snapshot.artifacts
								: snapshot.events;
	return (
		<div className="content-wrap collection">
			<div className="collection-intro">
				<div>
					<div className="eyebrow">FACTORY DATA / {screen.toUpperCase()}</div>
					<h2>{nav.find((x) => x.id === screen)?.label}</h2>
					<p className="subtle">
						Everything produced, decided, and in motion inside this factory.
					</p>
				</div>
				<span className="count-badge">{data.length} records</span>
			</div>
			<div className="collection-grid">
				{data.map((item) => (
					<button
						type="button"
						className="data-card"
						key={item.id}
						onClick={() => onSelect(item as unknown as Record<string, unknown>)}
					>
						<div className="card-top">
							<span className="type-tag">{screen.slice(0, -1)}</span>
							<span className="status-text">
								{"status" in item ? String(item.status) : "logged"}
							</span>
						</div>
						<h3>
							{"name" in item
								? String(item.name)
								: "title" in item
									? String(item.title)
									: "event_type" in item
										? String(item.event_type).replaceAll("_", " ")
										: String(item.subject ?? "Message")}
						</h3>
						<p>
							{"purpose" in item
								? item.purpose
								: "objective" in item
									? item.objective
									: "body" in item
										? item.body
										: "payload" in item
											? JSON.stringify(item.payload)
											: "content" in item
												? item.content.slice(0, 180)
												: JSON.stringify(item)}
						</p>
					</button>
				))}
			</div>
		</div>
	);
}

function DetailPanel({
	item,
	onClose,
}: {
	item: Record<string, unknown>;
	onClose: () => void;
}) {
	const closeButton = useRef<HTMLButtonElement>(null);
	useEffect(() => {
		closeButton.current?.focus();
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.key === "Escape") onClose();
		};
		document.addEventListener("keydown", onKeyDown);
		return () => document.removeEventListener("keydown", onKeyDown);
	}, [onClose]);
	return (
		<div
			className="detail-panel"
			role="dialog"
			aria-modal="true"
			aria-label="Record details"
		>
			<div className="panel-heading">
				<h3>Details</h3>
				<button ref={closeButton} className="secondary" onClick={onClose}>
					Close
				</button>
			</div>
			<pre>{JSON.stringify(item, null, 2)}</pre>
		</div>
	);
}

const root = document.getElementById("root");
if (root) {
	createRoot(root).render(
		<React.StrictMode>
			<App />
		</React.StrictMode>,
	);
}
