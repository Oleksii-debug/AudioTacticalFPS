# Explicit Google access-token renewal

Google Advanced v3 can use either a manually supplied access token with its
real expiry, or an explicitly configured Desktop app refresh grant. The older
manual-token behavior is unchanged. Basic v2 still uses its API-key adapter.

## Configure an existing account

1. Create the Advanced v3 profile in **Tools → AI and API connections** with the
   exact numeric project, location and existing NMT model. Saving a profile
   makes no request.
2. Select that account and choose **Configure Google automatic token renewal**.
3. Enter the client ID, client secret (if applicable) and refresh token from
   your already authorized Google **Desktop app** grant. This feature does not
   obtain the initial grant, open a browser, inspect ADC files, invoke gcloud,
   import a service-account key, or implement an authorization-code exchange.
   The existing grant must authorize Cloud Translation for your chosen project.
4. Review the account and fixed token endpoint and explicitly choose Yes.
   The confirmation starts on No. Only Windows Credential Manager receives
   the credential; profile/job/export/backup JSON contains no token values.
   The existing vault size limit is retained.
5. Enable the account and use the existing selected-text/document approval and
   cost review. Configuration alone never enables an account or grants work.

Replacing a credential under an otherwise unchanged account retains its
existing vault reference and resumable job identity. Choose a separate account
when you need a different approved account identity. Removing the connection
removes its vault credential. Non-secret profile exports remain disabled and
drop all vault references.

## Request behavior

Every request first passes the existing LOCAL ONLY / remote authorization,
exact destination/data/request-hash and usage-budget checks. It then reads the
selected credential from the vault, including when an access token is cached.

A refresh uses HTTPS POST to the fixed official
https://oauth2.googleapis.com/token endpoint with URL-encoded refresh_token,
client_id, optional client_secret, and grant_type=refresh_token. It sends
no book text, model output, API-key fallback or source file. Redirects and
environment proxies are not followed. TLS uses the platform trust store.

Only a valid Bearer response with bounded expiry and content is accepted.
A returned scope, when present, must include a Cloud Translation scope.
Changed refresh-token rotation requires explicit reconnection; the running
request never silently rewrites a vault credential. Revoked grants, redirects,
truncated/oversized/duplicate responses and unsuitable expiry stop before the
translation call. Error messages contain neither response bodies nor secrets.

The existing Gateway holds at most sixteen access tokens in memory. Cache
entries are bound to the exact account, vault reference, endpoint and refresh
credential digest. Tokens are renewed sixty seconds before their declared
expiry, measured on the monotonic clock. Every lookup still reads the vault;
changed or removed credentials cannot use an old cached value. Application
restart needs a new exchange and never restores an active token from backup.

Refresh uses the same request deadline and cancellation/authority guard.
A socket read can take up to ten seconds before cancellation is observed.
After refresh, the same guard runs before any document transfer. A failed
refresh records the reserved translation as not sent, so the existing job may
be resumed after reconnection. A translation request that was actually sent
keeps the existing uncertain-charge behavior; renewal never retries it.

## Validation boundaries

The contract tests use actual Gateway, ProfileStore, TranslationService and
usage ledger code with a controlled HTTPS transport and synthetic credentials.
They do not establish live Google IAM, consent, billing or translation quality.
Native dialog tests are separate from manual Windows/NVDA qualification.
No credentials or external account are required by automated tests.

Primary protocol references:

- [Google Desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app#offline)
- [Cloud Translation authentication](https://cloud.google.com/translate/docs/authentication)
- [Advanced translateText scopes](https://cloud.google.com/translate/docs/reference/rest/v3/projects.locations/translateText)
