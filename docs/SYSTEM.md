# Airport Categorisation, backend

How this API behaves, and the decisions behind it. Update this when the behaviour changes.

The frontend notes live in that repo at `docs/SYSTEM.md`.

## What this service does

FastAPI and MongoDB. It stores organisations, users, jobs, and templates. Identity comes from SelfBrief CMS. This service does not store passwords.

A request is authenticated with the CMS access token (`Authorization: Bearer`). On every call we ask CMS who that token belongs to, then load or create the matching local user.

## Sign-in

The browser signs in with Auth.js against the CMS OIDC provider (`categorisation`). The frontend then calls this API with the access token. Before it keeps a session, it calls `GET /api/v1/me`. A 401 or 403 there means the person is not signed in.

There is no local signup, invite, or password reset. People are added in SelfBrief and appear here after they sign in.

People are keyed by `cms_user_id` (the OIDC subject), not by email. Email can change on the CMS side and we overwrite the stored email on the next successful profile load.

## What CMS tells us

`GET {SELFBRIEF_BASE_URL}/api/v1/sso/me/` with the access token.

- `401` means only that this user account is disabled or suspended. There is no profile in that response.
- `403` means the token is not allowed to use this application.
- A 200 profile includes `is_super_admin` and `organisations[]`. Each organisation has `role` (`user` or `admin`) and `is_active`.

`is_active` on an organisation means "this person can use categorisation for this operator". It is false in two different situations:

- Categorisation is switched off for the operator. The role is still present.
- This person has no access to categorisation. The role is null.

A super-admin has `is_super_admin: true` and an empty organisation list. They are stored against the internal organisation SelfBrief Aero (`selfbrief-aero`) with role `super-admin`, so their jobs have an organisation to belong to.

There is no organisation switcher yet. If the person already belongs to an organisation that is still active for them, we keep that one. Otherwise we use the first active organisation on the profile. The session ends only when no organisation on the profile has `is_active: true`.

## How each case is handled

| What happened in CMS | What we do |
|---|---|
| Person can sign in, with an active organisation | Upsert the user. `users.is_active` becomes true. Role and organisation are taken from the profile. |
| Operator categorisation switched off, and they have no other active organisation | End the session (403, not permitted). If this person is already in our database, set `users.is_active` to false. Do not change the organisation row, and do not change other people. |
| Role is No Access, and they have no other active organisation | Same as the row above. |
| User account suspended | CMS returns 401 and no profile. End the session. If the access token matches a hash we stored on a previous successful call, or the sign-in ID token verifies to a known `cms_user_id`, set that user's `is_active` to false. Do not create a row for someone we have never seen. |
| They sign in successfully again later | The next successful profile load sets `users.is_active` back to true. |

We remember up to eight SHA-256 hashes of recent access tokens on the user (`cms_token_hashes`). That is how a later 401 can be tied to a person, because the 401 body does not include a user id. The sign-in request can also send `X-SelfBrief-Id-Token`. We verify that JWT against the CMS issuer (RS256, audience not checked, because this API does not hold the OIDC client id).

A suspension that never reaches this API (CMS blocks the browser before the callback) cannot update a row. The flag changes on the next request that does reach us, or stays as it was.

## Organisation live and Archive

The organisation list in the product is live when `organizations.is_active` is true, and Archive when it is false. That flag is ours.

Logging in does not write it. CMS has said a scheduled task will keep organisation status, separate from the user profile. That task is not wired here yet, because there is no endpoint or payload for it. Until it exists, switching categorisation off in CMS ends that person's session and marks them inactive, but the organisation stays on the live list.

A super-admin can still archive an organisation from this app (`PATCH /organisations/{id}/deactivate`). That sets the organisation inactive, marks its members inactive with `deactivated_by_org`, and restores only those members when the organisation is reactivated. The internal SelfBrief Aero organisation cannot be archived. This is separate from the CMS operator switch.

Archiving an organisation sticks. The profile load does not write `organizations.is_active`, and a later request still gets 403 if that organisation is inactive.

Deactivating one person locally does not stick while CMS still allows them. The next successful profile load sets `users.is_active` back to true and clears `deactivated_by_org`. CMS is the source of whether that person may use the app.

## Roles

| CMS | Stored role | What they can do |
|---|---|---|
| Organisation `role: user` | `user` | Jobs for their organisation |
| Organisation `role: admin` | `admin` | Same job scope as a user, plus organisation user admin where the routes allow it |
| `is_super_admin: true` | `super-admin` | All jobs, templates, airports, organisations, team accounts |

A company writer or other CMS staff role is not an admin here. Only the categorisation role on the profile is used.

Jobs created by a super-admin are stored with `organization_id` of SelfBrief Aero and `created_by_user_id` of that user. Admins and users only see jobs for their organisation.

## Users in the database

A row is created on the first successful sign-in. People who have never got in are not listed.

`users.is_active` is what the user list treats as active or inactive. The list defaults to active people. Inactive people stay hidden until that filter is turned on.

`eula_accepted_version` is a short hash of `legal/eula.txt`. Editing that file makes every user accept again. `GET /api/v1/me` and `POST /api/v1/me/eula` are the only authenticated routes that work before acceptance.

Job results are advisory. Accepting the notice on a job is stored per user per job. It is not a global acknowledgement.

## Jobs and email

Creating a job stores the request and a worker polls for work (`JOB_POLL_INTERVAL_SECONDS`, at least 15 seconds). Completion and failure emails include the job title and the airport as code and name. They go to the person who created the job.

## Identity we no longer use

Sign-in used to be email and password through SuperTokens, including invites and password reset. That package, those routes, and the `supertokens_user_id` and `invite_status` fields are gone. On startup the API drops the old index if it is still there and unsets those fields on existing user documents.
