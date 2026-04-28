# MatrixMatch — Role-Based UI & Route Restrictions

This document tracks every UI element and route that is hidden or blocked based on the logged-in user's role. It exists so future-you doesn't have to dig through templates and Flask routes to figure out *why* something doesn't appear for a given account.

The pattern used everywhere is consistent:

- **Sidebar / dashboard tile** → wrapped in a Jinja `{% if session.get('role') ... %}` conditional in `base.html` (or in the dashboard template).
- **Route URL** → guarded inside the Flask view function with an early role check, a `flash()`, and a `redirect()` to a sensible landing page.

Both layers are applied together so a restricted user can't reach the feature by typing the URL directly.

---

## 1. Restrictions on the **Admin** role

Admins are restricted from doing things only researchers should do (running comparisons, viewing personal history). They keep all admin-only management features.

### 1.1 — 🔍 New Comparison

| Layer | Where | Behavior |
|---|---|---|
| Sidebar link | [base.html](templates/base.html) | Wrapped in `{% if session.get('role') != 'Admin' %}` so it's hidden for admins. |
| Route guard | [comparison_new()](app.py) | If the logged-in user has role `Admin`, flash *"Admins cannot create comparisons. Use the Manage Documents page instead."* and redirect to `/admin/dashboard`. |
| Backend functionality | [matcher.run_stage1()](matcher.py), [matcher.compute_stage1_matches()](matcher.py) | Untouched. The comparison engine remains callable from any other code path that ever needs it. |

**Why:** Admins are responsible for managing the document repository, not running their own thesis comparisons. Their dashboard and Manage Documents page are the right starting points.

---

### 1.2 — 🕒 View History (the personal history list)

| Layer | Where | Behavior |
|---|---|---|
| Sidebar link | [base.html](templates/base.html) | Wrapped in `{% if session.get('role') != 'Admin' %}` so it's hidden for admins. |
| Route guard | [history()](app.py) | If the logged-in user has role `Admin`, flash *"Admins view comparison history from the admin dashboard or per-researcher pages."* and redirect to `/admin/dashboard`. |
| Dashboard link | [dashboard_admin.html](templates/dashboard_admin.html) | The "View Full History →" link next to "Recent Comparison Runs" was removed (it pointed to `/history` and would have just bounced the admin). |
| `history_detail.html` Back button | [history_detail.html](templates/history_detail.html) | Made role-aware: admins see *"← Back to Admin Dashboard"* (linking to `/admin/dashboard`); researchers see *"← Back to History"* (linking to `/history`). |

**Why:** `/history` shows the *currently logged-in user's own* comparison runs. Admins don't run comparisons themselves, so the page would always be empty for them. Admins still have two legitimate ways to browse comparison history:

1. **Admin Dashboard → Recent Comparison Runs** table (everyone's recent runs, all in one place).
2. **Manage Users → click "History" on a researcher** → `/admin/researchers/<id>/history` (one researcher's full history).

`/history/<id>` (the **detail** view of a single run) is **not** blocked for admins — they reach it via either of the two paths above and the page renders correctly. Only the bare `/history` *list* page is gated.

---

## 2. Restrictions on the **Researcher** role

Researchers are restricted from admin-only management features.

### 2.1 — 📤 Upload Study

| Layer | Where | Behavior |
|---|---|---|
| Sidebar link | [base.html](templates/base.html) | Wrapped in `{% if session.get('role') == 'Admin' %}` so it's hidden for researchers. |
| Route guard | [upload_document()](app.py) | If the logged-in user is not an admin, flash *"Only admins can upload documents."* and redirect to `/dashboard`. |
| Manage Documents page links | [manage_documents.html](templates/manage_documents.html) | Two upload links (header `+ Upload New` and empty-state `+ Upload First Document`) live on an admin-only page, so researchers never see them. No conditional needed. |

**Why:** Documents in the repository are the corpus that every researcher's comparisons are scored against. Admins curate it; researchers consume it. Allowing researchers to upload would let any account inject data into the repository.

---

## 3. Restrictions on **Guests** (not logged in)

Guests get a deliberately reduced experience: they can run a comparison, but the run is held in memory only and never written to the database.

### 3.1 — Everything except `/`, `/login`, `/register`, `/comparison/new`, `/guest/result`, and the `/api/guest/...` endpoints

| Layer | Where | Behavior |
|---|---|---|
| Sidebar | [base.html](templates/base.html) | Sidebar isn't rendered at all for guests — base.html falls into the `{% else %}` branch and shows a sidebar-less layout. |
| Route guards | every route protected by `@login_required` | Guests are bounced to `/login` with *"Please log in to continue."* |
| Forced password change | [login_required decorator](app.py) | If a researcher logs in with an admin-issued temporary password, they're locked on `/account/change-password` until they pick a new password. Logout is still allowed. |

**Why:** Guests are a low-friction onboarding path. They can try the system end-to-end without an account, but anything that requires persistence (history, upload, recalculate, admin features) is gated. See `templates/guest_result.html` and the in-memory `_guest_runs` store in `app.py` for how the guest path works.

---

## 4. Locations of the role check helper

If you need to add another role-based restriction, the patterns to follow are:

| Need | How |
|---|---|
| Hide a sidebar/dashboard link | Wrap in `{% if session.get('role') == 'Admin' %}` or `{% if session.get('role') != 'Admin' %}` in the relevant template. Use `session.get('role')` (not `user.role`) so it works even on pages that don't pass a `user` object to the template. |
| Block a Flask route | Inside the route function, after `user = get_current_user()`, check `if not user or user.get("role") != "Admin": flash(...); return redirect(...)`. Place this check **before** any DB queries or business logic. |
| Block a guest from a logged-in route | The existing `@login_required` decorator already does this. No extra code needed. |
| Block a logged-in user from a guest-only route | Use `if not is_guest(): return jsonify({"error": "..."}), 400` (see `api_guest_gap_analysis` in app.py for the pattern). |

---

## 5. Quick reference table

| Route / UI | Researcher | Admin | Guest |
|---|---|---|---|
| `/` | ✅ | ✅ | ✅ |
| `/login`, `/register` | ✅ | ✅ | ✅ |
| `/comparison/new` | ✅ | ❌ flash + redirect | ✅ (in-memory only) |
| `/guest/result` | bounced to /login | bounced to /login | ✅ |
| `/history` (list) | ✅ | ❌ flash + redirect | bounced to /login |
| `/history/<id>` (detail) | ✅ (own only) | ✅ (any) | bounced to /login |
| `/history/<id>/recalculate` | ✅ (own) | ✅ (any) | bounced to /login |
| `/documents/upload` | ❌ flash + redirect | ✅ | bounced to /login |
| `/admin/dashboard` | bounced | ✅ | bounced to /login |
| `/admin/researchers` | bounced | ✅ | bounced to /login |
| `/admin/researchers/<id>/reset` | bounced | ✅ | bounced to /login |
| `/admin/researchers/<id>/delete` | bounced | ✅ | bounced to /login |
| `/admin/researchers/<id>/history` | bounced | ✅ | bounced to /login |
| `/admin/documents` | bounced | ✅ | bounced to /login |
| `/admin/documents/<id>/edit` | bounced | ✅ | bounced to /login |
| `/admin/documents/<id>/delete` | bounced | ✅ | bounced to /login |
| `/api/history/<h>/gap_analysis/<d>` | ✅ (own) | ✅ (any) | rejected |
| `/api/history/<h>/feature_highlight` | ✅ (own) | ✅ (any) | rejected |
| `/api/guest/gap_analysis/<d>` | rejected | rejected | ✅ |
| `/api/guest/feature_highlight` | rejected | rejected | ✅ |
| `/account/change-password` | ✅ (when forced) | ✅ (when forced) | bounced to /login |

---

## 6. How to read this file

- **✅** = the role can use this normally.
- **❌ flash + redirect** = the role hits the route → server redirects them with an explanatory flash message.
- **bounced** = `@login_required` or an admin-only check kicks them out without ceremony.
- **rejected** = the API endpoint returns a JSON error (`400` or similar) instead of rendering a page.

When you add a new restriction, append a new sub-section to the right role and update the quick-reference table at the bottom. Keep the *Why* line — it's the part that future-you (or a panel reviewer) actually cares about.
