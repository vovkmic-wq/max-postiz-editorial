CREATE TABLE hub_recurring_tasks (
	id VARCHAR(64) NOT NULL, 
	profile_id VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	interval_seconds INTEGER NOT NULL, 
	lookback_hours INTEGER NOT NULL, 
	due_at FLOAT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	owner VARCHAR(36), 
	lease_until FLOAT NOT NULL, 
	last_slot VARCHAR(64), 
	attempts INTEGER NOT NULL, 
	error VARCHAR(64), 
	PRIMARY KEY (id)
);
CREATE TABLE hub_source_archive (
	id VARCHAR(36) NOT NULL, 
	source_id VARCHAR(64) NOT NULL, 
	response_hash VARCHAR(64) NOT NULL, 
	blob_path TEXT NOT NULL, 
	metadata_json JSON NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (source_id, response_hash)
);
CREATE INDEX ix_hub_source_archive_source_id ON hub_source_archive (source_id);
CREATE TABLE hub_targets (
	id VARCHAR(64) NOT NULL, 
	profile_id VARCHAR(64) NOT NULL, 
	platform VARCHAR(16) NOT NULL, 
	route VARCHAR(32) NOT NULL, 
	destination TEXT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	paused BOOLEAN NOT NULL, 
	binding_hash VARCHAR(64) NOT NULL, 
	options JSON NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE hub_variants (
	id VARCHAR(36) NOT NULL, 
	draft_id VARCHAR(36) NOT NULL, 
	target_id VARCHAR(64) NOT NULL, 
	revision INTEGER NOT NULL, 
	title TEXT NOT NULL, 
	body TEXT NOT NULL, 
	media JSON NOT NULL, 
	sources JSON NOT NULL, 
	source_revision INTEGER NOT NULL, 
	config_hash VARCHAR(64) NOT NULL, 
	binding_hash VARCHAR(64) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	updated_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (draft_id, target_id), 
	FOREIGN KEY(draft_id) REFERENCES drafts (id), 
	FOREIGN KEY(target_id) REFERENCES hub_targets (id)
);
CREATE TABLE hub_approvals (
	id VARCHAR(36) NOT NULL, 
	variant_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	snapshot_hash VARCHAR(64) NOT NULL, 
	scheduled_at FLOAT NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	revoked BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(variant_id) REFERENCES hub_variants (id)
);
CREATE TABLE hub_remote_drafts (
	variant_id VARCHAR(36) NOT NULL, 
	external_id TEXT, 
	state VARCHAR(32) NOT NULL, 
	remote_hash VARCHAR(64), 
	synced_revision INTEGER NOT NULL, 
	correlation VARCHAR(64) NOT NULL, 
	updated_at FLOAT NOT NULL, 
	PRIMARY KEY (variant_id), 
	FOREIGN KEY(variant_id) REFERENCES hub_variants (id), 
	UNIQUE (correlation)
);
CREATE TABLE hub_variant_revisions (
	id VARCHAR(36) NOT NULL, 
	variant_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	payload JSON NOT NULL, 
	actor VARCHAR(64) NOT NULL, 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (variant_id, revision), 
	FOREIGN KEY(variant_id) REFERENCES hub_variants (id)
);
CREATE TABLE hub_dispatches (
	id VARCHAR(36) NOT NULL, 
	variant_id VARCHAR(36) NOT NULL, 
	approval_id VARCHAR(36) NOT NULL, 
	revision INTEGER NOT NULL, 
	snapshot_hash VARCHAR(64) NOT NULL, 
	snapshot JSON NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	due_at FLOAT NOT NULL, 
	owner VARCHAR(36), 
	lease_until FLOAT NOT NULL, 
	attempts INTEGER NOT NULL, 
	external_id TEXT, 
	permalink TEXT, 
	error VARCHAR(64), 
	verified BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (variant_id, revision, snapshot_hash), 
	FOREIGN KEY(variant_id) REFERENCES hub_variants (id), 
	FOREIGN KEY(approval_id) REFERENCES hub_approvals (id)
);
CREATE TABLE hub_dispatch_attempts (
	id VARCHAR(36) NOT NULL, 
	dispatch_id VARCHAR(36) NOT NULL, 
	result VARCHAR(32) NOT NULL, 
	error VARCHAR(64), 
	created_at FLOAT NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(dispatch_id) REFERENCES hub_dispatches (id)
);
CREATE INDEX hub_material_search ON materials USING gin (to_tsvector('russian', title || ' ' || text));
