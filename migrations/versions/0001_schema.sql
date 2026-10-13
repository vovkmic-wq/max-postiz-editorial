CREATE TABLE approvals (
	id VARCHAR(36) NOT NULL, 
	draft_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	content_hash VARCHAR(64) NOT NULL, 
	config_id VARCHAR(64) NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	revoked BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE audit_events (
	id VARCHAR(36) NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	action VARCHAR(64) NOT NULL, 
	entity VARCHAR(128) NOT NULL, 
	details JSON NOT NULL, 
	at FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE budget_ledger (
	id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	action_key VARCHAR(128) NOT NULL, 
	amount FLOAT NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (action_key)
);
CREATE TABLE campaigns (
	id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	data JSON NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE channels (
	id VARCHAR(64) NOT NULL, 
	chat_id VARCHAR(64), 
	paused BOOLEAN NOT NULL, 
	capabilities JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (chat_id)
);
CREATE TABLE config_versions (
	id VARCHAR(64) NOT NULL, 
	snapshot JSON NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE draft_revisions (
	id VARCHAR(36) NOT NULL, 
	draft_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	payload JSON NOT NULL, 
	config_id VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (draft_id, revision)
);
CREATE TABLE drafts (
	id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	story_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	state VARCHAR(64) NOT NULL, 
	current JSON NOT NULL, 
	config_id VARCHAR(64) NOT NULL, 
	scheduled_at FLOAT, 
	commercial BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (channel_id, story_id)
);
CREATE TABLE generation_runs (
	id VARCHAR(36) NOT NULL, 
	task_type VARCHAR(32) NOT NULL, 
	model VARCHAR(128) NOT NULL, 
	runtime VARCHAR(32) NOT NULL, 
	config_id VARCHAR(64) NOT NULL, 
	state VARCHAR(64) NOT NULL, 
	usage JSON NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE inbox_events (
	id VARCHAR(36) NOT NULL, 
	platform VARCHAR(32) NOT NULL, 
	event_key VARCHAR(256) NOT NULL, 
	payload JSON NOT NULL, 
	processed BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (platform, event_key)
);
CREATE TABLE jobs (
	id VARCHAR(36) NOT NULL, 
	key VARCHAR(256) NOT NULL, 
	kind VARCHAR(64) NOT NULL, 
	subject_id VARCHAR(64) NOT NULL, 
	config_id VARCHAR(64) NOT NULL, 
	state VARCHAR(64) NOT NULL, 
	due_at FLOAT NOT NULL, 
	lease_until FLOAT NOT NULL, 
	owner VARCHAR(36), 
	attempts INTEGER NOT NULL, 
	error VARCHAR(64), 
	PRIMARY KEY (id), 
	UNIQUE (key)
);
CREATE TABLE material_media (
	id VARCHAR(36) NOT NULL, 
	material_id VARCHAR(36) NOT NULL, 
	url TEXT NOT NULL, 
	mime VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE materials (
	id VARCHAR(36) NOT NULL, 
	source_id VARCHAR(64) NOT NULL, 
	external_id TEXT NOT NULL, 
	story_id VARCHAR(36) NOT NULL, 
	title TEXT NOT NULL, 
	text TEXT NOT NULL, 
	url TEXT NOT NULL, 
	text_hash VARCHAR(64) NOT NULL, 
	published_at FLOAT, 
	language VARCHAR(16) NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source_id, external_id, text_hash)
);
CREATE TABLE metrics (
	id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	value FLOAT NOT NULL, 
	origin VARCHAR(32) NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE publication_attempts (
	id VARCHAR(36) NOT NULL, 
	intent_id VARCHAR(36) NOT NULL, 
	result VARCHAR(64) NOT NULL, 
	at FLOAT NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE publication_intents (
	id VARCHAR(36) NOT NULL, 
	draft_id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	state VARCHAR(64) NOT NULL, 
	external_id VARCHAR(128), 
	permalink TEXT, 
	send_started_at FLOAT, 
	published_at FLOAT, 
	body_hash VARCHAR(64) NOT NULL, 
	retry_after FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (draft_id)
);
CREATE TABLE source_cursors (
	id VARCHAR(64) NOT NULL, 
	data JSON NOT NULL, 
	last_success FLOAT, 
	next_due FLOAT NOT NULL, 
	status VARCHAR(64) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE stories (
	id VARCHAR(36) NOT NULL, 
	text_hash VARCHAR(64) NOT NULL, 
	title TEXT NOT NULL, 
	text TEXT NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (text_hash)
);
CREATE TABLE video_assets (
	id VARCHAR(36) NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	path TEXT NOT NULL, 
	frames JSON NOT NULL, 
	quality JSON NOT NULL, 
	provenance JSON NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (sha256)
);
CREATE TABLE video_projects (
	id VARCHAR(36) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	revision INTEGER NOT NULL, 
	plan JSON NOT NULL, 
	config_id VARCHAR(64) NOT NULL, 
	approved_hash VARCHAR(64), 
	state VARCHAR(64) NOT NULL, 
	draft_id VARCHAR(36), 
	error VARCHAR(64), 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (draft_id)
);
CREATE TABLE video_scenes (
	id VARCHAR(36) NOT NULL, 
	project_id VARCHAR(36) NOT NULL, 
	index INTEGER NOT NULL, 
	request_id VARCHAR(36) NOT NULL, 
	backend_hash VARCHAR(64), 
	remote_id VARCHAR(128), 
	state VARCHAR(64) NOT NULL, 
	artifact JSON NOT NULL, 
	error VARCHAR(64), 
	started_at FLOAT, 
	PRIMARY KEY (id), 
	UNIQUE (project_id, index), 
	UNIQUE (request_id)
);
