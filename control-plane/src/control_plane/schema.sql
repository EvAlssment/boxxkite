-- boxkite control-plane schema (PostgreSQL).
--
-- AUTO-GENERATED from src/control_plane/models_orm.py by
-- scripts/generate_schema_sql.py — do NOT edit by hand. Run that script after
-- changing the ORM models and commit the result.
--
-- This file is a convenience mirror for operators who want to inspect or apply
-- the schema directly (e.g. via `psql`). It is NOT what the running app
-- executes: at startup db.init_schema applies the Alembic migration chain
-- (migrations/versions/), which is the authoritative source of truth. When in
-- doubt about the exact live schema, read the migrations, not this file.
--
-- All statements use IF NOT EXISTS so this is safe to run against an
-- already-initialized database.


CREATE TABLE IF NOT EXISTS accounts (
	id VARCHAR(36) NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	password_hash VARCHAR(255), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	github_id VARCHAR(64), 
	google_id VARCHAR(64), 
	sso_provider_user_id VARCHAR(191), 
	sso_organization_id VARCHAR(191), 
	sso_connection_id VARCHAR(191), 
	scim_directory_user_id VARCHAR(191), 
	scim_deactivated_at TIMESTAMP WITH TIME ZONE, 
	custom_allowed_commands JSON, 
	email_verified_at TIMESTAMP WITH TIME ZONE, 
	is_admin BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_email ON accounts (email);

CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_github_id ON accounts (github_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_google_id ON accounts (google_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_scim_directory_user_id ON accounts (scim_directory_user_id);

CREATE INDEX IF NOT EXISTS ix_accounts_sso_connection_id ON accounts (sso_connection_id);

CREATE INDEX IF NOT EXISTS ix_accounts_sso_organization_id ON accounts (sso_organization_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_sso_provider_user_id ON accounts (sso_provider_user_id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
	scope_hash VARCHAR(64) NOT NULL, 
	request_fingerprint VARCHAR(64) NOT NULL, 
	response_status INTEGER, 
	response_body BYTEA, 
	response_media_type VARCHAR(128), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (scope_hash)
);

CREATE INDEX IF NOT EXISTS ix_idempotency_keys_created_at ON idempotency_keys (created_at);

CREATE TABLE IF NOT EXISTS oauth_clients (
	id VARCHAR(36) NOT NULL, 
	client_id VARCHAR(64) NOT NULL, 
	client_name VARCHAR(200) NOT NULL, 
	redirect_uris JSON NOT NULL, 
	client_type VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_clients_client_id ON oauth_clients (client_id);

CREATE TABLE IF NOT EXISTS rate_limit_windows (
	key VARCHAR(300) NOT NULL, 
	window_start INTEGER NOT NULL, 
	count INTEGER NOT NULL, 
	PRIMARY KEY (key, window_start)
);

CREATE TABLE IF NOT EXISTS revoked_preview_tokens (
	jti VARCHAR(64) NOT NULL, 
	session_id VARCHAR(64) NOT NULL, 
	port INTEGER NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (jti)
);

CREATE INDEX IF NOT EXISTS ix_revoked_preview_tokens_expires_at ON revoked_preview_tokens (expires_at);

CREATE INDEX IF NOT EXISTS ix_revoked_preview_tokens_session_id ON revoked_preview_tokens (session_id);

CREATE TABLE IF NOT EXISTS admin_access_log (
	id VARCHAR(36) NOT NULL, 
	admin_account_id VARCHAR(36) NOT NULL, 
	endpoint VARCHAR(200) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(admin_account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_admin_access_log_admin_account_id ON admin_access_log (admin_account_id);

CREATE INDEX IF NOT EXISTS ix_admin_access_log_admin_created ON admin_access_log (admin_account_id, created_at);

CREATE TABLE IF NOT EXISTS api_keys (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	prefix VARCHAR(64) NOT NULL, 
	key_hash VARCHAR(64) NOT NULL, 
	role VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_api_keys_account_id ON api_keys (account_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys (key_hash);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_account_created ON email_verification_tokens (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_account_id ON email_verification_tokens (account_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_email_verification_tokens_token_hash ON email_verification_tokens (token_hash);

CREATE TABLE IF NOT EXISTS mcp_connections (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	label VARCHAR(200) NOT NULL, 
	catalog_id VARCHAR(100) NOT NULL, 
	host VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_mcp_connections_account_label UNIQUE (account_id, label), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_mcp_connections_account_created ON mcp_connections (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_mcp_connections_account_id ON mcp_connections (account_id);

CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
	id VARCHAR(36) NOT NULL, 
	code VARCHAR(128) NOT NULL, 
	client_id VARCHAR(64) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	redirect_uri VARCHAR(2048) NOT NULL, 
	code_challenge VARCHAR(128) NOT NULL, 
	code_challenge_method VARCHAR(16) NOT NULL, 
	scope VARCHAR(200), 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	consumed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_oauth_auth_codes_expires_at ON oauth_authorization_codes (expires_at);

CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_account_id ON oauth_authorization_codes (account_id);

CREATE INDEX IF NOT EXISTS ix_oauth_authorization_codes_client_id ON oauth_authorization_codes (client_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_authorization_codes_code ON oauth_authorization_codes (code);

CREATE TABLE IF NOT EXISTS oauth_tokens (
	id VARCHAR(36) NOT NULL, 
	refresh_token_hash VARCHAR(64) NOT NULL, 
	client_id VARCHAR(64) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	rotated_from VARCHAR(36), 
	PRIMARY KEY (id), 
	FOREIGN KEY(client_id) REFERENCES oauth_clients (client_id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE, 
	FOREIGN KEY(rotated_from) REFERENCES oauth_tokens (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_oauth_tokens_account_created ON oauth_tokens (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_oauth_tokens_account_id ON oauth_tokens (account_id);

CREATE INDEX IF NOT EXISTS ix_oauth_tokens_client_id ON oauth_tokens (client_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_oauth_tokens_refresh_token_hash ON oauth_tokens (refresh_token_hash);

CREATE TABLE IF NOT EXISTS organizations (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	created_by_account_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(created_by_account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_organizations_created_by_account_id ON organizations (created_by_account_id);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_account_created ON password_reset_tokens (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_account_id ON password_reset_tokens (account_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_password_reset_tokens_token_hash ON password_reset_tokens (token_hash);

CREATE TABLE IF NOT EXISTS refresh_tokens (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_refresh_tokens_account_created ON refresh_tokens (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_refresh_tokens_account_id ON refresh_tokens (account_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_refresh_tokens_token_hash ON refresh_tokens (token_hash);

CREATE TABLE IF NOT EXISTS sandbox_images (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	label VARCHAR(200), 
	base VARCHAR(64) NOT NULL, 
	python_packages JSON NOT NULL, 
	apt_packages JSON NOT NULL, 
	npm_packages JSON NOT NULL, 
	cache_key VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	digest VARCHAR(128), 
	registry_ref VARCHAR(500), 
	scan_result JSON, 
	failure_reason VARCHAR(500), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_sandbox_images_account_cache_key ON sandbox_images (account_id, cache_key, status);

CREATE INDEX IF NOT EXISTS ix_sandbox_images_account_created ON sandbox_images (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_sandbox_images_account_id ON sandbox_images (account_id);

CREATE INDEX IF NOT EXISTS ix_sandbox_images_cache_key ON sandbox_images (cache_key);

CREATE TABLE IF NOT EXISTS sandbox_sessions (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	pod_name VARCHAR(255), 
	label VARCHAR(200), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	destroyed_at TIMESTAMP WITH TIME ZONE, 
	duration_seconds FLOAT, 
	destroyed_reason VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_sandbox_sessions_account_active ON sandbox_sessions (account_id, destroyed_at);

CREATE INDEX IF NOT EXISTS ix_sandbox_sessions_account_id ON sandbox_sessions (account_id);

CREATE TABLE IF NOT EXISTS sandbox_volumes (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	label VARCHAR(200), 
	size_gb FLOAT NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	pvc_name VARCHAR(200), 
	failure_reason VARCHAR(500), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_sandbox_volumes_account_created ON sandbox_volumes (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_sandbox_volumes_account_id ON sandbox_volumes (account_id);

CREATE TABLE IF NOT EXISTS secrets (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	ciphertext VARCHAR NOT NULL, 
	nonce VARCHAR(64) NOT NULL, 
	wrapped_data_key VARCHAR NOT NULL, 
	encryption_key_id VARCHAR(200) NOT NULL, 
	allowed_hosts JSON NOT NULL, 
	trust_tier VARCHAR(20), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_used_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_secrets_account_name UNIQUE (account_id, name), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_secrets_account_created ON secrets (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_secrets_account_id ON secrets (account_id);

CREATE TABLE IF NOT EXISTS webhook_subscriptions (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	url VARCHAR(2048) NOT NULL, 
	description VARCHAR(200), 
	event_types JSON NOT NULL, 
	ciphertext VARCHAR NOT NULL, 
	nonce VARCHAR(64) NOT NULL, 
	wrapped_data_key VARCHAR NOT NULL, 
	encryption_key_id VARCHAR(200) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	payload_format VARCHAR(32) NOT NULL, 
	hec_token_ciphertext VARCHAR, 
	hec_token_nonce VARCHAR(64), 
	hec_token_wrapped_data_key VARCHAR, 
	hec_token_encryption_key_id VARCHAR(200), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_triggered_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_webhook_subscriptions_account_created ON webhook_subscriptions (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_webhook_subscriptions_account_id ON webhook_subscriptions (account_id);

CREATE TABLE IF NOT EXISTS exec_log_entries (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	operation VARCHAR(32) NOT NULL, 
	detail JSON NOT NULL, 
	exit_code INTEGER, 
	output_truncated VARCHAR, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	duration_ms INTEGER NOT NULL, 
	row_hash VARCHAR(64), 
	prev_hash VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES sandbox_sessions (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_exec_log_entries_account_id ON exec_log_entries (account_id);

CREATE INDEX IF NOT EXISTS ix_exec_log_entries_session_id ON exec_log_entries (session_id);

CREATE INDEX IF NOT EXISTS ix_exec_log_entries_session_started ON exec_log_entries (session_id, started_at);

CREATE TABLE IF NOT EXISTS organization_invites (
	id VARCHAR(36) NOT NULL, 
	organization_id VARCHAR(36) NOT NULL, 
	email VARCHAR(320) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	invited_by_account_id VARCHAR(36) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	accepted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(invited_by_account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_organization_invites_organization_id ON organization_invites (organization_id);

CREATE UNIQUE INDEX IF NOT EXISTS ix_organization_invites_token_hash ON organization_invites (token_hash);

CREATE TABLE IF NOT EXISTS organization_members (
	id VARCHAR(36) NOT NULL, 
	organization_id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	role VARCHAR(20) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_org_member UNIQUE (organization_id, account_id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_organization_members_account ON organization_members (account_id);

CREATE INDEX IF NOT EXISTS ix_organization_members_organization_id ON organization_members (organization_id);

CREATE TABLE IF NOT EXISTS snapshots (
	id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36), 
	label VARCHAR(200), 
	storage_key_prefix VARCHAR(500) NOT NULL, 
	size_bytes INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	deleted_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE, 
	FOREIGN KEY(session_id) REFERENCES sandbox_sessions (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_snapshots_account_created ON snapshots (account_id, created_at);

CREATE INDEX IF NOT EXISTS ix_snapshots_account_id ON snapshots (account_id);

CREATE INDEX IF NOT EXISTS ix_snapshots_session_id ON snapshots (session_id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
	id VARCHAR(36) NOT NULL, 
	subscription_id VARCHAR(36) NOT NULL, 
	account_id VARCHAR(36) NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	attempt_count INTEGER NOT NULL, 
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	last_attempt_at TIMESTAMP WITH TIME ZONE, 
	response_status_code INTEGER, 
	response_body_truncated VARCHAR, 
	failure_reason VARCHAR(500), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	FOREIGN KEY(subscription_id) REFERENCES webhook_subscriptions (id) ON DELETE CASCADE, 
	FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_account_id ON webhook_deliveries (account_id);

CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_status_next_attempt ON webhook_deliveries (status, next_attempt_at);

CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_subscription_created ON webhook_deliveries (subscription_id, created_at);

CREATE INDEX IF NOT EXISTS ix_webhook_deliveries_subscription_id ON webhook_deliveries (subscription_id);
