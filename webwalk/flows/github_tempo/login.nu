# github-tempo login — establish a corp GitHub (github.corp.clover.com) session.
#
# Same shape as the SCW flow's login.nu: credentials come from env vars, the
# username is prepopulated into the sign-in form, and MFA is left to the human.
# The browser profile persists, so once you've logged in the session is usually
# remembered and this whole script no-ops straight to the dashboard.
#
# Run it against a server you've already started (WW_SERVER points the CLI at it):
#   $env.WW_SERVER = "http://192.168.90.107:22133"
#   nu webwalk/flows/github_tempo/login.nu

# Where the CLI finds the running server. Leave your own value if already set.
if "WW_SERVER" not-in $env {
    $env.WW_SERVER = "http://192.168.90.107:22133"
}

# Who we're logging in as. Prepopulated into the GitHub sign-in form's username
# field so the cold path goes straight to password/SSO.
if "GH_USER" not-in $env { $env.GH_USER = "matt-rixman" }
# The email used if corp GitHub bounces to Microsoft Azure AD SSO (same IdP as SCW).
if "FISERV_USER" not-in $env { $env.FISERV_USER = "Matthew.Rixman@Fiserv.com" }

# Jira/Tempo (jira.corp.clover.com) uses its own login form and account. The
# timecard write side needs this session in the same browser profile.
if "JIRA_USER" not-in $env { $env.JIRA_USER = "matt.rixman" }

if "TS_PW" not-in $env {
    echo "corp GitHub / SSO password:"
    $env.TS_PW = (input -s)
}
if "JIRA_PASSWORD" not-in $env {
    echo "Jira password:"
    $env.JIRA_PASSWORD = (input -s)
}

# Open the sign-in page. A remembered profile may redirect straight to the
# dashboard — that's fine, the final wait-for still succeeds.
uv run github-tempo connect https://github.corp.clover.com/login

# --- Path A: GitHub Enterprise built-in form (username + password on one page) ---
# Prepopulate the username, then fill the password and submit. Wrapped in `try`
# so the SSO-redirect path (Path B) can take over when the form isn't shown.
# NOTE: verified on a cold login 2026-09-11 — corp GitHub serves its own
# "Sign in via LDAP" form (input#login_field / input#password / the commit
# submit) and does NOT delegate to Azure AD, so Path B below just falls through.
try {
    uv run github-tempo wait-for --element "input#login_field" --timeout 8
    uv run github-tempo type $env.GH_USER --into "input#login_field"
    uv run github-tempo type --into "input#password" --env TS_PW --secret
    uv run github-tempo click --element "input[type=submit][name=commit]"
}

# --- Path B: Microsoft Azure AD SSO (if corp GitHub delegates to the IdP) ---
# Same flow the SCW login drives. Username-first, but a remembered profile jumps
# straight to the password page; probe the email field with a short timeout.
try {
    uv run github-tempo wait-for --url login.microsoftonline.com --timeout 8
    try {
        uv run github-tempo wait-for --element "input#i0116" --timeout 6
        uv run github-tempo type $env.FISERV_USER --into "input#i0116"
        uv run github-tempo click --element "input#idSIButton9"
    }
    uv run github-tempo wait-for --element "input#i0118" --timeout 60
    uv run github-tempo type --into "input#i0118" --env TS_PW --secret
    uv run github-tempo click --element "input#idSIButton9"

    # "Stay signed in?" (KMSI) — accept if present, non-fatal otherwise.
    try {
        uv run github-tempo wait-for --element "#KmsiCheckboxField" --timeout 10
        uv run github-tempo click --element "input#idSIButton9"
    }

    # SSO may prompt for an MFA code from your authenticator — that can't be
    # scripted. If the prompt shows, tell the human; the final wait blocks through
    # the manual entry.
    try {
        uv run github-tempo wait-for --element "input#idTxtBx_SAOTCC_OTC" --timeout 20
        print ""
        print "  ============================================================"
        print "  ENTER YOUR MFA CODE in the browser on the target machine,"
        print "  then submit it. Corp GitHub will finish signing you in."
        print "  ============================================================"
        print ""
    }
}

# Both paths converge on the authenticated dashboard. Gate on a visible element
# that only exists when logged in (the global "Create something new" button);
# generous timeout covers a manual MFA entry.
uv run github-tempo wait-for --element "button#global-create-menu-anchor" --timeout 180
print ""
print $"  Signed in to corp GitHub as ($env.GH_USER)."

# --- Jira / Tempo login (its own form + account) -------------------------------
# Land on the Tempo my-work page; if the session is cold Jira bounces us to the
# login form. Prepopulate the username, fill the password, submit. A remembered
# profile skips straight to Tempo and the form probe just falls through.
uv run github-tempo step navigate --value "https://jira.corp.clover.com/secure/Tempo.jspa#/my-work/week?type=LIST"
# A cold session lands on login.jsp, which now shows a chooser ("Continue with
# username and password" / "Continue with SSO") instead of the form itself.
# `?native_login=` skips the chooser; a warm session never enters this branch.
try {
    uv run github-tempo wait-for --url "login.jsp" --timeout 8
    uv run github-tempo step navigate --value "https://jira.corp.clover.com/login.jsp?native_login="
    uv run github-tempo wait-for --element "input#login-form-username" --timeout 15
    uv run github-tempo type $env.JIRA_USER --into "input#login-form-username"
    uv run github-tempo type --into "input#login-form-password" --env JIRA_PASSWORD --secret
    uv run github-tempo click --element "#login-form-submit"
    uv run github-tempo step navigate --value "https://jira.corp.clover.com/secure/Tempo.jspa#/my-work/week?type=LIST"
}
# Converge on Tempo (generous timeout covers any SSO/MFA redirects).
uv run github-tempo wait-for --url "Tempo.jspa" --timeout 180
print ""
print $"  Signed in to Jira/Tempo as ($env.JIRA_USER). Both sessions ready."
